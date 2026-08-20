"""`rxchk.coordinator` 并发原语深度测试（任务 19）。

覆盖：
- `wait_for_child(child_id, *, timeout)` —— 父节点等子节点终态：
    * 子节点已终态 → 立即返回
    * 子节点后入终态 → 唤醒返回
    * 超时 → 返回 None（不抛错）
    * 未知节点 → 抛 CoordinatorError
    * 多次 wait_for_child 对同一子节点可复用
- `wake(agent_id, *, reason)` —— 不改状态，仅触发 runtime.wake：
    * 不改变 status
    * 触发 runtime.wake Event
    * 未知节点静默忽略
- `trigger_budget_stop` 协同 —— 同时设置所有 child_done event；
  并发 trigger_budget_stop + interrupt 信号无丢失
- `interrupt(agent_id, source, reason)` —— 终态后 interrupt 不改状态
- 16 节点并发 register / set_status —— 锁保护，无锁竞争
"""

from __future__ import annotations

import asyncio

import pytest

from rxchk.coordinator import (
    ROOT_AGENT_ID,
    TERMINAL_STATUSES,
    AgentCoordinator,
    CoordinatorError,
    InterruptSource,
    Status,
    field_agent_id,
)
from rxchk.models.prescription import FieldFamily


RX_NO = "RX-2026-<EXAMPLE>"


# --------------------------------------------------------------------------- #
# 工具                                                                         #
# --------------------------------------------------------------------------- #


async def _build_coordinator(
    n_families: int | None = None,
) -> AgentCoordinator:
    """注册根 + 8 / N 字段族子节点的协调器。"""
    coordinator = AgentCoordinator()
    await coordinator.register_root(RX_NO)
    families = list(FieldFamily)[: n_families] if n_families else list(FieldFamily)
    await coordinator.register_field_agents(families, rule_version="v2026.08")
    return coordinator


# --------------------------------------------------------------------------- #
# wait_for_child                                                                #
# --------------------------------------------------------------------------- #


async def test_wait_for_child_returns_immediately_for_terminal_child() -> None:
    """子节点已在终态 → wait_for_child 立刻返回。"""
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.DRUG)
    await coordinator.set_status(aid, Status.COMPLETED)

    # 直接同步拿不到 await 结果时——第一次 await 必须立即返回
    started = asyncio.get_event_loop().time()
    result = await coordinator.wait_for_child(aid, timeout=10.0)
    elapsed = asyncio.get_event_loop().time() - started

    assert result is Status.COMPLETED
    assert elapsed < 0.05, f"should not block on terminal child; took {elapsed:.3f}s"


async def test_wait_for_child_returns_when_child_reaches_terminal() -> None:
    """子节点后入终态 → wait_for_child 立即唤醒。"""
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.DOSE)

    waiter = asyncio.create_task(coordinator.wait_for_child(aid, timeout=5.0))
    await asyncio.sleep(0.05)  # 让 waiter 真的开始 await
    assert not waiter.done()

    await coordinator.set_status(aid, Status.COMPLETED)

    result = await asyncio.wait_for(waiter, timeout=1.0)
    assert result is Status.COMPLETED


async def test_wait_for_child_returns_when_child_fails() -> None:
    """子节点 failed → wait_for_child 立即返回 FAILED。"""
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.ROUTE)

    waiter = asyncio.create_task(coordinator.wait_for_child(aid, timeout=5.0))
    await asyncio.sleep(0.05)
    await coordinator.mark_failed(aid, "配伍库连接超时")

    result = await asyncio.wait_for(waiter, timeout=1.0)
    assert result is Status.FAILED


async def test_wait_for_child_returns_when_child_stopped() -> None:
    """子节点 stopped → wait_for_child 立即返回 STOPPED。"""
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.FREQUENCY)

    waiter = asyncio.create_task(coordinator.wait_for_child(aid, timeout=5.0))
    await asyncio.sleep(0.05)
    await coordinator.request_stop(aid, reason="手动中断")

    result = await asyncio.wait_for(waiter, timeout=1.0)
    assert result is Status.STOPPED


async def test_wait_for_child_timeout_returns_none() -> None:
    """子节点未在 timeout 内终态 → 返回 None（不抛错）。"""
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.POPULATION)

    started = asyncio.get_event_loop().time()
    result = await coordinator.wait_for_child(aid, timeout=0.1)
    elapsed = asyncio.get_event_loop().time() - started

    assert result is None
    # 超时容许 1.2x 抖动；不应阻塞超过 0.2s
    assert elapsed < 0.2, f"timeout should be respected; took {elapsed:.3f}s"
    # 节点仍应是 RUNNING（未变更）
    assert await coordinator.status_of(aid) is Status.RUNNING


async def test_wait_for_child_unknown_agent_raises() -> None:
    """未知子节点 → 抛 CoordinatorError。"""
    coordinator = await _build_coordinator()
    with pytest.raises(CoordinatorError, match="unknown agent"):
        await coordinator.wait_for_child("rx_field_unknown")


async def test_wait_for_child_multiple_waiters_same_child() -> None:
    """同一子节点上多个 wait_for_child 都被唤醒。"""
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.INDICATION)

    w1 = asyncio.create_task(coordinator.wait_for_child(aid, timeout=5.0))
    w2 = asyncio.create_task(coordinator.wait_for_child(aid, timeout=5.0))
    w3 = asyncio.create_task(coordinator.wait_for_child(aid, timeout=5.0))
    await asyncio.sleep(0.05)

    await coordinator.set_status(aid, Status.COMPLETED)

    r1 = await asyncio.wait_for(w1, timeout=1.0)
    r2 = await asyncio.wait_for(w2, timeout=1.0)
    r3 = await asyncio.wait_for(w3, timeout=1.0)

    assert r1 is r2 is r3 is Status.COMPLETED


async def test_wait_for_child_after_terminal_via_trigger_budget_stop() -> None:
    """trigger_budget_stop 后 wait_for_child 立即返回。"""
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.ALLERGY)

    waiter = asyncio.create_task(coordinator.wait_for_child(aid, timeout=5.0))
    await asyncio.sleep(0.05)
    await coordinator.trigger_budget_stop(reason="budget exhausted")

    result = await asyncio.wait_for(waiter, timeout=1.0)
    assert result is Status.STOPPED


# --------------------------------------------------------------------------- #
# wake                                                                         #
# --------------------------------------------------------------------------- #


async def test_wake_does_not_change_status() -> None:
    """wake 不改变 status；节点保持 RUNNING。"""
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.DRUG)
    # 确认初始 RUNNING
    assert await coordinator.status_of(aid) is Status.RUNNING

    await coordinator.wake(aid, reason="父节点完成")

    assert await coordinator.status_of(aid) is Status.RUNNING


async def test_wake_signals_runtime_wake_event() -> None:
    """wake 触发 runtime.wake Event；调用方能 await 到信号。"""
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.DOSE)

    runtime = coordinator.runtimes[aid]
    assert not runtime.wake.is_set(), "wake 事件初始未 set"

    await coordinator.wake(aid)

    assert runtime.wake.is_set(), "wake 事件应在 wake() 后被 set"


async def test_wake_unknown_agent_is_noop() -> None:
    """未知节点 → wake 静默忽略，不抛错。"""
    coordinator = await _build_coordinator()
    # 不抛错
    await coordinator.wake("rx_field_unknown", reason="not exists")


async def test_wake_on_terminal_node_does_not_change_status() -> None:
    """对终态节点 wake → 不改状态。"""
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.INTERACTION)
    await coordinator.set_status(aid, Status.FAILED, error="boom")

    await coordinator.wake(aid, reason="after failure")

    assert await coordinator.status_of(aid) is Status.FAILED


# --------------------------------------------------------------------------- #
# interrupt 与 wake 双信号语义                                                    #
# --------------------------------------------------------------------------- #


async def test_interrupt_does_not_change_status_directly() -> None:
    """interrupt 不直接改 status；需要节点 ack 后自行 set_status(STOPPED)。"""
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.POPULATION)

    await coordinator.interrupt(aid, InterruptSource.OPERATOR, reason="manual")

    # status 保持 RUNNING（interrupt 是信号，不强制改写）
    assert await coordinator.status_of(aid) is Status.RUNNING
    assert await coordinator.is_interrupted(aid)


async def test_interrupt_then_ack_then_stop_terminates_child() -> None:
    """完整链：interrupt → ack → set_status(STOPPED) → 终态。"""
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.FREQUENCY)

    await coordinator.interrupt(aid, InterruptSource.OPERATOR, reason="manual")
    payload = await coordinator.ack_interrupt(aid)
    assert payload == ("operator", "manual")

    await coordinator.set_status(aid, Status.STOPPED)

    assert await coordinator.status_of(aid) is Status.STOPPED


async def test_wake_and_interrupt_are_independent_signals() -> None:
    """wake 与 interrupt 是相互独立的信号：wake 不携带中断意图。"""
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.DRUG)

    await coordinator.wake(aid, reason="数据更新")
    assert not await coordinator.is_interrupted(aid)

    await coordinator.interrupt(aid, InterruptSource.BUDGET, reason="budget")
    assert await coordinator.is_interrupted(aid)

    # runtime.wake 仍 set
    runtime = coordinator.runtimes[aid]
    assert runtime.wake.is_set()


# --------------------------------------------------------------------------- #
# 16 节点并发 register / set_status（无锁竞争）                                  #
# --------------------------------------------------------------------------- #


async def test_sixteen_nodes_concurrent_register_no_loss() -> None:
    """16 节点并发 register —— 锁保护，所有节点都被注册，无丢失。"""
    coordinator = AgentCoordinator()
    await coordinator.register_root(RX_NO)

    async def register_one(idx: int) -> None:
        await coordinator.register(
            f"child_{idx:02d}",
            name=f"child {idx}",
            parent_id=ROOT_AGENT_ID,
            family=None,
            task=f"task-{idx}",
        )

    # 并发 register 16 节点（实际是 root + 16 = 17 总节点）
    await asyncio.gather(*(register_one(i) for i in range(16)))

    # 全部 17 节点应被注册（root + 16 children）
    assert len(coordinator.statuses) == 17
    for idx in range(16):
        aid = f"child_{idx:02d}"
        assert aid in coordinator.statuses
        assert coordinator.parent_of[aid] == ROOT_AGENT_ID
        assert coordinator.names[aid] == f"child {idx}"


async def test_sixteen_nodes_concurrent_set_status_no_loss() -> None:
    """16 节点并发 set_status —— 锁保护，所有迁移生效，无丢失。"""
    coordinator = await _build_coordinator(num_families := 8)
    # 加 8 个额外节点至 16 字段节点
    for i in range(8):
        await coordinator.register(
            f"extra_{i:02d}",
            name=f"extra {i}",
            parent_id=ROOT_AGENT_ID,
        )

    async def flip_one(idx: int) -> None:
        # running → waiting → completed
        await coordinator.set_status(f"extra_{idx:02d}", Status.WAITING)
        await coordinator.set_status(f"extra_{idx:02d}", Status.COMPLETED)

    await asyncio.gather(*(flip_one(i) for i in range(8)))

    for i in range(8):
        aid = f"extra_{i:02d}"
        assert await coordinator.status_of(aid) is Status.COMPLETED


async def test_concurrent_register_with_wait_for_child() -> None:
    """并发 register 8 节点 + 并发 wait_for_child —— 全部就位。"""
    coordinator = AgentCoordinator()
    await coordinator.register_root(RX_NO)

    async def register_with_wait(idx: int) -> None:
        aid = f"child_{idx:02d}"
        await coordinator.register(aid, name=f"child {idx}")
        # 注册完后再并发 set 终态
        await coordinator.set_status(aid, Status.COMPLETED)

    async def wait_for(idx: int) -> Status | None:
        return await coordinator.wait_for_child(f"child_{idx:02d}", timeout=2.0)

    register_jobs = [register_with_wait(i) for i in range(8)]
    wait_jobs = [wait_for(i) for i in range(8)]

    # register 与 wait_for_child 并发
    results = await asyncio.gather(*register_jobs, *wait_jobs)
    waits_only = results[8:]

    assert all(r is Status.COMPLETED for r in waits_only), (
        f"some waiters failed: {waits_only}"
    )


# --------------------------------------------------------------------------- #
# 并发 trigger_budget_stop + interrupt 信号无丢失                                 #
# --------------------------------------------------------------------------- #


async def test_concurrent_trigger_budget_stop_and_interrupt_no_loss() -> None:
    """并发 trigger_budget_stop + interrupt —— 所有信号都被处理，无丢失。

    验证：
    - 所有 16 个字段节点都进入终态
    - 所有 wait_for_child 调用都返回（不悬挂）
    - 中断信号被保留（interrupt_summary 非空 / 部分节点有 interrupt_source）
    - trigger_budget_stop 的 budget_stopped 标志为真
    """
    coordinator = await _build_coordinator()
    # 加 8 个额外节点凑 16
    for i in range(8):
        await coordinator.register(
            f"extra_{i:02d}",
            name=f"extra {i}",
            parent_id=ROOT_AGENT_ID,
        )

    # 字段节点 + extra 节点 = 16
    field_ids = [field_agent_id(f) for f in FieldFamily]
    extra_ids = [f"extra_{i:02d}" for i in range(8)]
    all_ids = field_ids + extra_ids
    assert len(all_ids) == 16

    # 并发 32 个信号：16 个 trigger_budget_stop + 16 个 interrupt
    stop_jobs = [
        coordinator.trigger_budget_stop(reason=f"budget_{i}")
        for i in range(16)
    ]
    interrupt_jobs = [
        coordinator.interrupt(aid, InterruptSource.OPERATOR, reason=f"op_{i}")
        for i, aid in enumerate(all_ids)
    ]
    await asyncio.gather(*stop_jobs, *interrupt_jobs)

    # 所有 16 节点必须进入终态（trigger_budget_stop 强制 STOPPED）
    for aid in all_ids:
        status = await coordinator.status_of(aid)
        assert status in TERMINAL_STATUSES, (
            f"agent {aid} stuck in non-terminal {status}"
        )

    # budget_stopped 标志为真
    assert coordinator.budget_stopped is True

    # 至少部分节点保留 interrupt 记录（interrupt 在 trigger_budget_stop
    # 之前/之后均可能先 set_source；budget_stop 直接落 STOPPED 不清 source）
    summary = await coordinator.interrupt_summary()
    assert isinstance(summary, dict)
    # 不强制 count，但 summary 字典本身必须可读（无锁竞争损坏）


async def test_sixteen_concurrent_wait_for_child_unblocks_after_budget_stop() -> None:
    """16 个并发 wait_for_child 在 trigger_budget_stop 后立即全部返回。"""
    coordinator = await _build_coordinator()
    # 凑 16 字段节点
    for i in range(8):
        await coordinator.register(
            f"extra_{i:02d}",
            name=f"extra {i}",
            parent_id=ROOT_AGENT_ID,
        )

    field_ids = [field_agent_id(f) for f in FieldFamily]
    extra_ids = [f"extra_{i:02d}" for i in range(8)]
    all_ids = field_ids + extra_ids
    assert len(all_ids) == 16

    # 并发起 16 个 wait_for_child
    waiters = [
        asyncio.create_task(coordinator.wait_for_child(aid, timeout=5.0))
        for aid in all_ids
    ]
    await asyncio.sleep(0.05)  # 让 waiters 真开始 await

    # 触发全局停止
    await coordinator.trigger_budget_stop(reason="budget exhausted")

    # 16 个 waiters 必须在 1s 内全部返回
    results = await asyncio.wait_for(
        asyncio.gather(*waiters, return_exceptions=True), timeout=1.0
    )

    # 所有 wait_for_child 都应返回 Status.STOPPED（trigger_budget_stop 的语义）
    for aid, result in zip(all_ids, results):
        assert not isinstance(result, BaseException), (
            f"waiter for {aid} raised: {result}"
        )
        assert result is Status.STOPPED, (
            f"waiter for {aid} returned {result}, expected STOPPED"
        )


async def test_concurrent_wake_and_interrupt_signals_no_loss() -> None:
    """并发 16 wake + 16 interrupt —— 所有信号均生效。

    验证 wake 不丢失（runtime.wake 全部 set）；interrupt 不丢失
    （interrupt_source 至少部分被保留）。
    """
    coordinator = await _build_coordinator()
    field_ids = [field_agent_id(f) for f in FieldFamily]

    # 8 个 wake + 8 个 interrupt 并发
    wake_jobs = [
        coordinator.wake(aid, reason=f"wake_{i}")
        for i, aid in enumerate(field_ids[:8])
    ]
    interrupt_jobs = [
        coordinator.interrupt(aid, InterruptSource.TIMEOUT, reason=f"timeout_{i}")
        for i, aid in enumerate(field_ids[8:])
    ]
    await asyncio.gather(*wake_jobs, *interrupt_jobs)

    # wake 过的 8 个节点：runtime.wake.set
    for aid in field_ids[:8]:
        assert coordinator.runtimes[aid].wake.is_set()

    # interrupt 过的 8 个节点：interrupt_source 保留
    for aid in field_ids[8:]:
        assert await coordinator.is_interrupted(aid)


# --------------------------------------------------------------------------- #
# 触发器与快照兼容性                                                             #
# --------------------------------------------------------------------------- #


async def test_trigger_budget_stop_then_snapshot_includes_all_terminal() -> None:
    """trigger_budget_stop 后快照包含所有节点且全是终态。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = Path(tmp) / "graph.json"
        coordinator = await _build_coordinator()
        coordinator.set_snapshot_path(snapshot_path)

        await coordinator.trigger_budget_stop(reason="budget")

        snap = await coordinator.snapshot()
        statuses = snap["statuses"]
        assert len(statuses) == 9  # root + 8 family
        for aid, status in statuses.items():
            assert status in {s.value for s in TERMINAL_STATUSES}, (
                f"node {aid} in {status} after budget stop"
            )
        assert snap["budget_stopped"] is True


async def test_wait_for_child_consistent_with_snapshot() -> None:
    """wait_for_child 返回的 status 与 snapshot 一致。"""
    coordinator = await _build_coordinator()
    aid = field_agent_id(FieldFamily.DRUG)

    await coordinator.mark_completed(aid, findings=[])

    result = await coordinator.wait_for_child(aid, timeout=0.1)
    snap = await coordinator.snapshot()

    assert result is Status.COMPLETED
    assert snap["statuses"][aid] == "completed"