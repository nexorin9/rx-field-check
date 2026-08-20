"""`rxchk.coordinator` 状态机深度测试（任务 18）。

覆盖：
- 合法迁移表完整性（每条迁移都要符合 LEGAL_TRANSITIONS）
- 终态不可逆：`completed / failed / crashed / stopped` 不能再迁移
  到任何其他状态（包括同状态重标记）
- `running ↔ waiting` 互转 + 自迁移（`running → running` 是合法重标记）
- `mark_*` 系列在终态下幂等 no-op（兄弟并发失败不抛错）
- 中断机制：`interrupt` / `ack_interrupt` / `is_interrupted` /
  `interrupt_summary` + 父子透传（PARENT 标记递归到子节点）
- `try_complete_coordinator` 聚合根节点终态：
  - 全 completed → 根 completed
  - 任一 failed/crashed → 根 crashed
  - 根已在终态 → 幂等返回
- `snapshot` / `restore` 保留中断信号
- `view_graph` 暴露 `interrupt_source` / `interrupt_reason` 字段
"""

from __future__ import annotations

import asyncio
import json

import pytest

from rxchk.coordinator import (
    LEGAL_TRANSITIONS,
    ROOT_AGENT_ID,
    TERMINAL_STATUSES,
    AgentCoordinator,
    CoordinatorError,
    CoordinatorStateError,
    InterruptSource,
    Status,
    field_agent_id,
)
from rxchk.models.prescription import FieldFamily, Finding


ALL_FAMILIES = tuple(FieldFamily)
RX_NO = "RX-2026-<EXAMPLE>"


async def _build_coordinator() -> AgentCoordinator:
    """注册根 + 8 字段族子节点的协调器。"""
    coordinator = AgentCoordinator()
    await coordinator.register_root(RX_NO)
    await coordinator.register_field_agents(ALL_FAMILIES, rule_version="v2026.08")
    return coordinator


# --------------------------------------------------------------------------- #
# 合法迁移表完整性                                                              #
# --------------------------------------------------------------------------- #


def test_legal_transitions_table_shape() -> None:
    """合法迁移表：running / waiting 互通且可入终态；终态不可出。"""
    assert Status.RUNNING in LEGAL_TRANSITIONS
    assert Status.WAITING in LEGAL_TRANSITIONS
    assert Status.COMPLETED in LEGAL_TRANSITIONS
    assert Status.FAILED in LEGAL_TRANSITIONS
    assert Status.CRASHED in LEGAL_TRANSITIONS
    assert Status.STOPPED in LEGAL_TRANSITIONS

    # 终态的合法迁移集必须为空
    for terminal in TERMINAL_STATUSES:
        assert LEGAL_TRANSITIONS[terminal] == frozenset(), (
            f"terminal {terminal.value} should have empty transition set"
        )


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        (Status.RUNNING, Status.RUNNING),  # 自迁移（重标记合法）
        (Status.RUNNING, Status.WAITING),
        (Status.RUNNING, Status.COMPLETED),
        (Status.RUNNING, Status.FAILED),
        (Status.RUNNING, Status.CRASHED),
        (Status.RUNNING, Status.STOPPED),
        (Status.WAITING, Status.RUNNING),
        (Status.WAITING, Status.WAITING),
        (Status.WAITING, Status.COMPLETED),
        (Status.WAITING, Status.FAILED),
        (Status.WAITING, Status.CRASHED),
        (Status.WAITING, Status.STOPPED),
    ],
)
async def test_legal_transitions_pass(from_status: Status, to_status: Status) -> None:
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.DRUG)
    # 走到 from_status（合法路径）
    if from_status is Status.WAITING:
        await coordinator.set_status(aid, Status.WAITING)
    elif from_status is Status.RUNNING:
        await coordinator.set_status(aid, Status.RUNNING)
    # 目标状态迁移
    await coordinator.set_status(aid, to_status)
    assert await coordinator.status_of(aid) is to_status


# --------------------------------------------------------------------------- #
# 终态不可逆（关键审计口径）                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("terminal", list(TERMINAL_STATUSES))
async def test_terminal_cannot_rewind_to_running(terminal: Status) -> None:
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.INDICATION)
    await coordinator.set_status(aid, terminal)
    with pytest.raises(CoordinatorStateError, match="illegal status transition"):
        await coordinator.set_status(aid, Status.RUNNING)


@pytest.mark.parametrize("terminal", list(TERMINAL_STATUSES))
async def test_terminal_cannot_self_remark(terminal: Status) -> None:
    """终态自重标记也要抛错（同状态重写也算非法迁移）。"""
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.INDICATION)
    await coordinator.set_status(aid, terminal)
    with pytest.raises(CoordinatorStateError, match="illegal status transition"):
        await coordinator.set_status(aid, terminal)


async def test_completed_cannot_transition_to_failed() -> None:
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.ALLERGY)
    await coordinator.set_status(aid, Status.COMPLETED)
    with pytest.raises(CoordinatorStateError):
        await coordinator.set_status(aid, Status.FAILED)


# --------------------------------------------------------------------------- #
# `mark_*` 幂等（兄弟并发失败不互相冲突）                                       #
# --------------------------------------------------------------------------- #


async def test_mark_completed_is_idempotent_on_terminal() -> None:
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.DRUG)
    await coordinator.set_status(aid, Status.COMPLETED)
    # 再次 mark_completed 不得抛错
    await coordinator.mark_completed(aid)
    assert await coordinator.status_of(aid) is Status.COMPLETED


async def test_mark_crashed_is_idempotent_under_concurrent_failures() -> None:
    """兄弟节点同时失败 → 父节点 mark_crashed 被调用两次，第二次幂等。"""
    coordinator = await _build_coordinator()
    aid_a = field_agent_id(FieldFamily.DOSE)
    aid_b = field_agent_id(FieldFamily.ALLERGY)

    async def boom_a() -> None:
        raise RuntimeError("dose 字典未载入")

    async def boom_b() -> None:
        raise RuntimeError("allergy 库连接超时")

    await asyncio.gather(
        coordinator.supervise(aid_a, boom_a()),
        coordinator.supervise(aid_b, boom_b()),
    )

    # 两个子节点都失败
    assert await coordinator.status_of(aid_a) is Status.FAILED
    assert await coordinator.status_of(aid_b) is Status.FAILED
    # 根节点 crashed（两次 mark_crashed 都安全通过幂等保护）
    assert await coordinator.status_of(ROOT_AGENT_ID) is Status.CRASHED
    failed = await coordinator.failed_agents()
    assert failed and len(failed) >= 2


async def test_mark_failed_is_idempotent_on_completed() -> None:
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.POPULATION)
    await coordinator.set_status(aid, Status.COMPLETED)
    # 已 completed 不能被 mark_failed（幂等 no-op，不抛错也不改状态）
    await coordinator.mark_failed(aid, "后续失败")
    assert await coordinator.status_of(aid) is Status.COMPLETED


async def test_mark_running_idempotent_on_completed() -> None:
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.ROUTE)
    await coordinator.set_status(aid, Status.COMPLETED)
    await coordinator.mark_running(aid)
    assert await coordinator.status_of(aid) is Status.COMPLETED


# --------------------------------------------------------------------------- #
# 中断机制：interrupt / ack_interrupt / is_interrupted / summary                #
# --------------------------------------------------------------------------- #


async def test_interrupt_basic_set_ack() -> None:
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.DRUG)

    assert await coordinator.is_interrupted(aid) is False
    await coordinator.interrupt(
        aid, InterruptSource.OPERATOR, reason="药师中断核对"
    )
    assert await coordinator.is_interrupted(aid) is True

    payload = await coordinator.ack_interrupt(aid)
    assert payload == ("operator", "药师中断核对")
    assert await coordinator.is_interrupted(aid) is False

    # 再次 ack 返回 None
    assert await coordinator.ack_interrupt(aid) is None


async def test_interrupt_preserves_first_source_under_repeats() -> None:
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.DOSE)

    await coordinator.interrupt(aid, InterruptSource.OPERATOR, reason="第一次")
    # 第二次 interrupt 不覆盖首次来源（审计保留）
    await coordinator.interrupt(aid, InterruptSource.TIMEOUT, reason="第二次")

    payload = await coordinator.ack_interrupt(aid)
    assert payload is not None
    source, reason = payload
    assert source == InterruptSource.OPERATOR.value
    assert reason == "第一次"


async def test_interrupt_propagates_to_children_with_parent_source() -> None:
    """父节点 interrupt → 子节点以 PARENT 来源被打断。"""
    coordinator = await _build_coordinator()
    await coordinator.interrupt(
        ROOT_AGENT_ID, InterruptSource.OPERATOR, reason="药师一键中断"
    )

    # 根 + 8 子节点都应该被打断
    assert await coordinator.is_interrupted(ROOT_AGENT_ID) is True
    for family in ALL_FAMILIES:
        assert await coordinator.is_interrupted(field_agent_id(family)) is True

    # 子节点来源是 PARENT
    payload = await coordinator.ack_interrupt(field_agent_id(FieldFamily.DOSE))
    assert payload is not None
    assert payload[0] == InterruptSource.PARENT.value
    assert "parent prescription_checker interrupted" in payload[1]


async def test_interrupt_unknown_agent_is_noop() -> None:
    coordinator = await _build_coordinator()
    # 未知节点不抛错（与参考侧一致）
    await coordinator.interrupt("rx_field_ghost", InterruptSource.OPERATOR)


async def test_interrupt_summary_includes_all_marked_agents() -> None:
    coordinator = await _build_coordinator()
    await coordinator.interrupt(
        field_agent_id(FieldFamily.DOSE), InterruptSource.TIMEOUT, reason="超时"
    )
    await coordinator.interrupt(
        field_agent_id(FieldFamily.ALLERGY),
        InterruptSource.OPERATOR,
        reason="药师中断",
    )

    summary = await coordinator.interrupt_summary()
    assert field_agent_id(FieldFamily.DOSE) in summary
    assert summary[field_agent_id(FieldFamily.DOSE)]["source"] == "timeout"
    assert summary[field_agent_id(FieldFamily.ALLERGY)]["source"] == "operator"
    assert field_agent_id(FieldFamily.DRUG) not in summary


async def test_view_graph_exposes_interrupt_fields() -> None:
    coordinator = await _build_coordinator()
    await coordinator.interrupt(
        field_agent_id(FieldFamily.INTERACTION),
        InterruptSource.OPERATOR,
        reason="配伍核对手动暂停",
    )
    nodes = await coordinator.view_graph()
    interaction_node = next(
        n for n in nodes if n["agent_id"] == field_agent_id(FieldFamily.INTERACTION)
    )
    assert interaction_node["interrupt_source"] == "operator"
    assert interaction_node["interrupt_reason"] == "配伍核对手动暂停"
    # 未中断节点字段为空字符串
    drug_node = next(
        n for n in nodes if n["agent_id"] == field_agent_id(FieldFamily.DRUG)
    )
    assert drug_node["interrupt_source"] == ""
    assert drug_node["interrupt_reason"] == ""


# --------------------------------------------------------------------------- #
# try_complete_coordinator                                                      #
# --------------------------------------------------------------------------- #


async def test_try_complete_all_completed_root_completed() -> None:
    coordinator = await _build_coordinator()

    async def ok(family: FieldFamily) -> list[Finding]:
        return [
            Finding(
                prescription_no=RX_NO,
                field=family.value,
                field_value="<EXAMPLE_VALUE>",
                family=family.value,
                rule_id=f"R-{family.value.upper()}-001",
                rule_version="v2026.08",
                hit_explanation="样例命中",
                evidence=f"<RULE:{family.value}>",
                severity="low",
                source="rule_engine",
                agent_id=field_agent_id(family),
            )
        ]

    await coordinator.run_field_agents({field_agent_id(f): ok(f) for f in ALL_FAMILIES})
    # 根已经被 run_field_agents 推到 completed；再次 try_complete 也幂等
    final = await coordinator.try_complete_coordinator()
    assert final is Status.COMPLETED
    assert await coordinator.status_of(ROOT_AGENT_ID) is Status.COMPLETED


async def test_try_complete_any_failed_root_crashed() -> None:
    coordinator = await _build_coordinator()
    broken = field_agent_id(FieldFamily.INTERACTION)

    async def ok(family: FieldFamily) -> list[Finding]:
        return []

    async def boom() -> list[Finding]:
        raise RuntimeError("配伍库不可达")

    jobs = {
        field_agent_id(f): (boom() if field_agent_id(f) == broken else ok(f))
        for f in ALL_FAMILIES
    }
    await coordinator.run_field_agents(jobs)

    # 根已因 supervise 的 propagate_crash 进入 crashed；try_complete 幂等返回
    final = await coordinator.try_complete_coordinator()
    assert final is Status.CRASHED


async def test_try_complete_is_idempotent_on_terminal_root() -> None:
    coordinator = await _build_coordinator()
    await coordinator.set_status(ROOT_AGENT_ID, Status.CRASHED)
    # 任意子节点完成都不再影响根节点
    await coordinator.set_status(
        field_agent_id(FieldFamily.DRUG), Status.COMPLETED
    )
    final = await coordinator.try_complete_coordinator()
    assert final is Status.CRASHED


async def test_try_complete_partial_completion_keeps_root_running() -> None:
    """子节点尚有 running/waiting → 根保持 running，不强制收敛。"""
    coordinator = await _build_coordinator()
    await coordinator.set_status(
        field_agent_id(FieldFamily.DRUG), Status.COMPLETED
    )
    # 其他 7 族仍在 running
    final = await coordinator.try_complete_coordinator()
    assert final is Status.RUNNING
    assert await coordinator.status_of(ROOT_AGENT_ID) is Status.RUNNING


# --------------------------------------------------------------------------- #
# 快照 / 恢复保留中断信号                                                      #
# --------------------------------------------------------------------------- #


async def test_snapshot_preserves_interrupts_and_restore_replays_them() -> None:
    coordinator = await _build_coordinator()
    await coordinator.interrupt(
        field_agent_id(FieldFamily.POPULATION),
        InterruptSource.TIMEOUT,
        reason="人群规则超时",
    )
    snap = await coordinator.snapshot()
    assert "interrupts" in snap
    assert (
        snap["interrupts"][field_agent_id(FieldFamily.POPULATION)]["source"]
        == InterruptSource.TIMEOUT.value
    )

    revived = AgentCoordinator()
    await revived.restore(snap)
    assert await revived.is_interrupted(field_agent_id(FieldFamily.POPULATION)) is True

    payload = await revived.ack_interrupt(field_agent_id(FieldFamily.POPULATION))
    assert payload is not None
    source, reason = payload
    assert source == InterruptSource.TIMEOUT.value
    assert reason == "人群规则超时"


async def test_snapshot_roundtrip_with_legal_transition_via_restore() -> None:
    """restore 后状态机仍按 LEGAL_TRANSITIONS 工作；不应引入非法迁移。"""
    coordinator = await _build_coordinator()
    await coordinator.set_status(
        field_agent_id(FieldFamily.DRUG), Status.COMPLETED
    )
    snap = await coordinator.snapshot()
    revived = AgentCoordinator()
    await revived.restore(snap)

    with pytest.raises(CoordinatorStateError):
        await revived.set_status(field_agent_id(FieldFamily.DRUG), Status.RUNNING)


# --------------------------------------------------------------------------- #
# 错误路径 / 边界                                                              #
# --------------------------------------------------------------------------- #


async def test_coordinator_state_error_is_subclass_of_coordinator_error() -> None:
    """`CoordinatorStateError` 必须可被现有 `CoordinatorError` 捕获。"""
    err = CoordinatorStateError("illegal transition")
    assert isinstance(err, CoordinatorError)
    assert isinstance(err, RuntimeError)


async def test_interrupt_then_set_status_to_stopped_is_legal() -> None:
    """中断 → ack → set_status(STOPPED) 是合法闭环。"""
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.FREQUENCY)
    await coordinator.interrupt(aid, InterruptSource.OPERATOR, reason="药师暂停")
    payload = await coordinator.ack_interrupt(aid)
    assert payload is not None
    # running → stopped 是合法迁移
    await coordinator.set_status(aid, Status.STOPPED)
    assert await coordinator.status_of(aid) is Status.STOPPED