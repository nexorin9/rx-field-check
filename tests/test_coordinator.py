"""`rxchk.coordinator` 单元测试（字段 Agent 派发协调器）。

覆盖：
- 根节点 + 8 字段族子节点注册；`view_graph()` 输出
  `{agent_id, name, parent_id, status}` 结构且父子关系正确。
- 并发：8 个字段族并发 `register` 无丢失、无锁竞争；同 id 并发注册只有
  一个成功；并发 `set_status` 与 `view_graph` 读写一致。
- 单个字段族抛异常 → 该族 `failed`、兄弟族照常 `completed`、根节点
  `crashed`（spec.md「参考地基」验收口径）。
- `trigger_budget_stop()` 后所有未进终态的节点变 `stopped`，终态不被改写。
- 图快照原子落盘 `runs/<rx_no>/graph.json` + `restore()` 往返。
- 错误路径：重复注册 / 未知父节点 / 非法状态值 / 未知节点静默忽略。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from rxchk.coordinator import (
    ROOT_AGENT_ID,
    TERMINAL_STATUSES,
    AgentCoordinator,
    CoordinatorError,
    Status,
    field_agent_id,
)
from rxchk.models.prescription import FieldFamily, Finding


RX_NO = "RX-2026-<EXAMPLE>"

ALL_FAMILIES = tuple(FieldFamily)


def make_finding(family: FieldFamily, *, severity: str = "high") -> Finding:
    """构造一条脱敏字段证据卡（占位符风格与 spec.md 一致）。"""
    return Finding(
        prescription_no=RX_NO,
        field=family.value,
        field_value="<EXAMPLE_VALUE>",
        family=family.value,
        rule_id=f"R-{family.value.upper()}-001",
        rule_version="v2026.08",
        hit_explanation="样例命中说明",
        evidence=f"<RULE:{family.value}>",
        severity=severity,
        source="rule_engine",
        agent_id=field_agent_id(family),
    )


async def build_coordinator() -> AgentCoordinator:
    """注册根节点 + 8 个字段族子节点。"""
    coordinator = AgentCoordinator()
    await coordinator.register_root(RX_NO)
    await coordinator.register_field_agents(ALL_FAMILIES, rule_version="v2026.08")
    return coordinator


# --------------------------------------------------------------------------- #
# 注册与图视图                                                                #
# --------------------------------------------------------------------------- #


async def test_register_root_and_eight_field_agents() -> None:
    coordinator = await build_coordinator()

    nodes = await coordinator.view_graph()
    assert len(nodes) == 9  # 根 + 8 字段族

    root = nodes[0]
    assert root["agent_id"] == ROOT_AGENT_ID
    assert root["parent_id"] is None
    assert RX_NO in root["name"]
    assert root["status"] == "running"

    children = nodes[1:]
    assert [n["agent_id"] for n in children] == [
        field_agent_id(f) for f in ALL_FAMILIES
    ]
    assert all(n["parent_id"] == ROOT_AGENT_ID for n in children)
    assert {n["family"] for n in children} == {f.value for f in ALL_FAMILIES}


async def test_view_graph_node_shape() -> None:
    coordinator = await build_coordinator()
    nodes = await coordinator.view_graph()

    for node in nodes:
        assert {"agent_id", "name", "parent_id", "status"} <= set(node)
        assert isinstance(node["agent_id"], str) and node["agent_id"]
        assert node["status"] in {s.value for s in Status}


async def test_graph_summary_counts_by_status() -> None:
    coordinator = await build_coordinator()
    await coordinator.mark_completed(field_agent_id(FieldFamily.DRUG))
    await coordinator.mark_failed(field_agent_id(FieldFamily.DOSE), "规则库读取失败")

    summary = await coordinator.graph_summary()
    assert summary["total"] == 9
    assert summary["completed"] == 1
    assert summary["failed"] == 1
    assert summary["running"] == 7


def test_field_agent_id_naming() -> None:
    assert field_agent_id(FieldFamily.DOSE) == "rx_field_dose"
    assert field_agent_id("population") == "rx_field_population"
    with pytest.raises(CoordinatorError):
        field_agent_id("")


# --------------------------------------------------------------------------- #
# 并发注册 / 状态更新（无锁竞争）                                              #
# --------------------------------------------------------------------------- #


async def test_eight_families_concurrent_register_no_loss() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register_root(RX_NO)

    await asyncio.gather(
        *(
            coordinator.register(
                field_agent_id(family),
                name=f"{family.value} 字段族核对",
                parent_id=ROOT_AGENT_ID,
                family=family,
            )
            for family in ALL_FAMILIES
        )
    )

    # 8 个族一个都不能少，且每个内部字典 key 集合一致（无部分写入）
    expected = {field_agent_id(f) for f in ALL_FAMILIES} | {ROOT_AGENT_ID}
    assert set(coordinator.statuses) == expected
    assert set(coordinator.parent_of) == expected
    assert set(coordinator.names) == expected
    assert set(coordinator.runtimes) == expected
    assert all(s is Status.RUNNING for s in coordinator.statuses.values())


async def test_duplicate_concurrent_register_only_one_wins() -> None:
    coordinator = AgentCoordinator()
    await coordinator.register_root(RX_NO)

    results = await asyncio.gather(
        *(
            coordinator.register(
                "rx_field_dose", name=f"重复注册 {i}", parent_id=ROOT_AGENT_ID
            )
            for i in range(8)
        ),
        return_exceptions=True,
    )
    succeeded = [r for r in results if not isinstance(r, BaseException)]
    failed = [r for r in results if isinstance(r, CoordinatorError)]
    assert len(succeeded) == 1
    assert len(failed) == 7
    assert len(await coordinator.view_graph()) == 2


async def test_concurrent_status_updates_stay_consistent() -> None:
    coordinator = await build_coordinator()
    agent_ids = [field_agent_id(f) for f in ALL_FAMILIES]

    async def flip(agent_id: str) -> None:
        for _ in range(20):
            await coordinator.mark_running(agent_id)
            await coordinator.set_status(agent_id, Status.WAITING)
        await coordinator.mark_completed(agent_id)

    async def read_graph() -> None:
        for _ in range(20):
            nodes = await coordinator.view_graph()
            # 读视图期间不得看到半成品节点（缺 name / status）
            assert len(nodes) == 9
            assert all(n["name"] and n["status"] for n in nodes)
            await asyncio.sleep(0)

    await asyncio.gather(*(flip(a) for a in agent_ids), read_graph())

    for agent_id in agent_ids:
        assert await coordinator.status_of(agent_id) is Status.COMPLETED


async def test_register_unknown_parent_rejected() -> None:
    coordinator = AgentCoordinator()
    with pytest.raises(CoordinatorError, match="parent"):
        await coordinator.register("rx_field_dose", name="剂量", parent_id="nope")


async def test_set_status_unknown_agent_is_ignored() -> None:
    coordinator = await build_coordinator()
    await coordinator.set_status("rx_field_ghost", Status.COMPLETED)  # 不抛错
    assert len(await coordinator.view_graph()) == 9


async def test_set_status_rejects_invalid_value() -> None:
    coordinator = await build_coordinator()
    with pytest.raises(CoordinatorError, match="unknown status"):
        await coordinator.set_status(ROOT_AGENT_ID, "done")


# --------------------------------------------------------------------------- #
# 派发：单族异常不阻塞兄弟族                                                   #
# --------------------------------------------------------------------------- #


async def test_one_failing_family_does_not_block_siblings() -> None:
    coordinator = await build_coordinator()
    broken = field_agent_id(FieldFamily.INTERACTION)

    async def ok(family: FieldFamily) -> list[Finding]:
        await asyncio.sleep(0)
        return [make_finding(family)]

    async def boom() -> list[Finding]:
        raise RuntimeError("配伍库连接超时")

    jobs = {
        field_agent_id(f): (boom() if field_agent_id(f) == broken else ok(f))
        for f in ALL_FAMILIES
    }
    results = await coordinator.run_field_agents(jobs)

    assert await coordinator.status_of(broken) is Status.FAILED
    for family in ALL_FAMILIES:
        agent_id = field_agent_id(family)
        if agent_id == broken:
            assert results[agent_id] is None
            continue
        assert await coordinator.status_of(agent_id) is Status.COMPLETED
        assert len(results[agent_id]) == 1

    # 父节点崩溃透传，但兄弟族全部跑完
    assert await coordinator.status_of(ROOT_AGENT_ID) is Status.CRASHED
    failed = await coordinator.failed_agents()
    assert broken in failed and "配伍库连接超时" in failed[broken]

    findings = await coordinator.collect_findings()
    assert len(findings) == len(ALL_FAMILIES) - 1
    assert {f.family for f in findings} == {
        f.value for f in ALL_FAMILIES if f is not FieldFamily.INTERACTION
    }


async def test_all_families_succeed_root_completed() -> None:
    coordinator = await build_coordinator()

    async def ok(family: FieldFamily) -> list[Finding]:
        return [make_finding(family)]

    await coordinator.run_field_agents(
        {field_agent_id(f): ok(f) for f in ALL_FAMILIES}
    )

    assert await coordinator.status_of(ROOT_AGENT_ID) is Status.COMPLETED
    assert len(await coordinator.collect_findings()) == len(ALL_FAMILIES)
    assert await coordinator.failed_agents() == {}


async def test_supervise_without_crash_propagation_keeps_root_running() -> None:
    coordinator = await build_coordinator()
    agent_id = field_agent_id(FieldFamily.DOSE)

    async def boom() -> None:
        raise ValueError("剂量规则缺失")

    result = await coordinator.supervise(agent_id, boom(), propagate_crash=False)

    assert result is None
    assert await coordinator.status_of(agent_id) is Status.FAILED
    assert await coordinator.status_of(ROOT_AGENT_ID) is Status.RUNNING


async def test_cancelled_family_marked_stopped() -> None:
    coordinator = await build_coordinator()
    agent_id = field_agent_id(FieldFamily.ALLERGY)
    started = asyncio.Event()

    async def slow() -> None:
        started.set()
        await asyncio.sleep(10)

    task = asyncio.create_task(coordinator.supervise(agent_id, slow()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await coordinator.status_of(agent_id) is Status.STOPPED


# --------------------------------------------------------------------------- #
# 预算停止                                                                    #
# --------------------------------------------------------------------------- #


async def test_trigger_budget_stop_marks_everything_stopped() -> None:
    coordinator = await build_coordinator()
    assert coordinator.budget_stopped is False

    await coordinator.trigger_budget_stop(reason="单张处方核对预算耗尽")

    assert coordinator.budget_stopped is True
    statuses = {n["agent_id"]: n["status"] for n in await coordinator.view_graph()}
    assert set(statuses.values()) == {Status.STOPPED.value}
    assert all(runtime.wake.is_set() for runtime in coordinator.runtimes.values())


async def test_budget_stop_does_not_rewrite_terminal_states() -> None:
    coordinator = await build_coordinator()
    done = field_agent_id(FieldFamily.DRUG)
    broken = field_agent_id(FieldFamily.DOSE)
    await coordinator.mark_completed(done, [make_finding(FieldFamily.DRUG)])
    await coordinator.mark_failed(broken, "字典未载入")

    await coordinator.trigger_budget_stop()

    assert await coordinator.status_of(done) is Status.COMPLETED
    assert await coordinator.status_of(broken) is Status.FAILED
    assert await coordinator.status_of(ROOT_AGENT_ID) is Status.STOPPED
    assert TERMINAL_STATUSES == {
        Status.COMPLETED,
        Status.FAILED,
        Status.CRASHED,
        Status.STOPPED,
    }


async def test_request_stop_single_agent() -> None:
    coordinator = await build_coordinator()
    agent_id = field_agent_id(FieldFamily.ROUTE)

    await coordinator.request_stop(agent_id, reason="药师中断")
    assert await coordinator.status_of(agent_id) is Status.STOPPED

    # 已在终态的节点不被再次改写
    await coordinator.request_stop(agent_id, reason="重复中断")
    assert coordinator.errors[agent_id] == "药师中断"


# --------------------------------------------------------------------------- #
# 快照 / 恢复                                                                 #
# --------------------------------------------------------------------------- #


async def test_snapshot_written_atomically_to_graph_json(tmp_path) -> None:
    graph_path = tmp_path / "runs" / RX_NO / "graph.json"
    coordinator = AgentCoordinator()
    coordinator.set_snapshot_path(graph_path)

    await coordinator.register_root(RX_NO)
    await coordinator.register_field_agents(ALL_FAMILIES)
    await coordinator.mark_completed(
        field_agent_id(FieldFamily.DRUG), [make_finding(FieldFamily.DRUG)]
    )

    assert graph_path.exists()
    payload = json.loads(graph_path.read_text(encoding="utf-8"))
    assert payload["statuses"][field_agent_id(FieldFamily.DRUG)] == "completed"
    assert len(payload["nodes"]) == 9
    assert payload["nodes"][0]["agent_id"] == ROOT_AGENT_ID

    # 原子写：目录下不残留 .graph.json.*.tmp 临时文件
    leftovers = [p.name for p in graph_path.parent.iterdir() if p.name != "graph.json"]
    assert leftovers == []


async def test_snapshot_restore_roundtrip() -> None:
    coordinator = await build_coordinator()
    await coordinator.mark_failed(field_agent_id(FieldFamily.ALLERGY), "过敏库缺字段")
    snap = await coordinator.snapshot()

    revived = AgentCoordinator()
    await revived.restore(snap)

    assert await revived.status_of(field_agent_id(FieldFamily.ALLERGY)) is Status.FAILED
    assert await revived.status_of(ROOT_AGENT_ID) is Status.RUNNING
    assert [n["agent_id"] for n in await revived.view_graph()] == [
        n["agent_id"] for n in await coordinator.view_graph()
    ]
    assert revived.errors[field_agent_id(FieldFamily.ALLERGY)] == "过敏库缺字段"


async def test_snapshot_failure_does_not_break_dispatch(tmp_path) -> None:
    # 快照路径指向一个已存在的文件所在的非法目录（父路径是文件）→ 写盘必失败
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    coordinator = AgentCoordinator()
    coordinator.set_snapshot_path(blocker / "graph.json")

    await coordinator.register_root(RX_NO)  # 不得抛错
    assert await coordinator.status_of(ROOT_AGENT_ID) is Status.RUNNING
