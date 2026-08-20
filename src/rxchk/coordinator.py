"""字段 Agent 派发协调器：一张处方 → 根节点 + 8 个字段族子 Agent。

参考机制（github_ref 代码证据）：`strix/core/agents.py: AgentCoordinator`
的图状态主路径 —— `statuses / parent_of / names / runtimes / _lock /
wake` + `register / set_status / trigger_budget_stop / graph_snapshot /
set_snapshot_path` + 原子快照（tmp 文件 + `replace`）。

    参考侧（多 Agent 渗透编排）        本模块（处方字段核对）
    ────────────────────────────      ────────────────────────────
    根 agent = 一个扫描目标            根 agent = 一张处方（prescription_checker）
    子 agent = 专项子任务              子 agent = 一个字段族（drug / dose / …）
    statuses/parent_of/names          同名同语义
    runtimes[aid].wake  (asyncio)     同款 Event，用于预算停止唤醒
    _lock: asyncio.Lock               同款，保护 register / 状态更新
    _maybe_snapshot → resume 快照      → runs/<rx_no>/graph.json（审计单元）

与参考侧的差异（融合取舍）：
- 参考侧子 Agent 是 LLM 会话，需要 `session / stream / pending_counts /
  wait_for_message` 这类消息投递原语；本项目字段核对是**确定性**规则与
  字典查询（`ai_role=none`），没有会话，故不搬 session/stream 分支，
  保留 `wake` 事件用于预算停止（`trigger_budget_stop`）。
- 参考侧状态含 `waiting`（等消息）；本项目保留该状态位以兼容图渲染，
  但确定性核对路径只走 running → completed | failed | crashed | stopped。
- 单个字段族抛异常时：该族 `failed`，**兄弟族不受影响继续跑完**，根节点
  标 `crashed`（spec.md「参考地基」表的验收口径）。

状态机（本模块负责的节点级状态；run.json 的处方级状态见 report/state.py）：

    running ──→ completed          字段族核对正常结束
            ├─→ failed             该族内部异常（规则/字典查询报错）
            ├─→ crashed            子节点异常向父节点透传
            └─→ stopped            预算停止 / 人工中断
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Iterable, Mapping, Optional

from rxchk.models.prescription import FieldFamily, Finding


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 状态                                                                        #
# --------------------------------------------------------------------------- #


class Status(str, Enum):
    """字段 Agent 节点状态（参考侧 `Status = Literal[...]` 的枚举化）。"""

    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CRASHED = "crashed"
    STOPPED = "stopped"


#: 终态：进入后不再由派发器主动改写（合法迁移表在任务 18 深化）。
TERMINAL_STATUSES: frozenset[Status] = frozenset(
    {Status.COMPLETED, Status.FAILED, Status.CRASHED, Status.STOPPED}
)

#: 根节点 id（一张处方一个协调器实例，故根 id 固定）。
ROOT_AGENT_ID = "prescription_checker"


class CoordinatorError(RuntimeError):
    """派发器用法错误（重复注册 / 未知节点 / 非法状态值）。"""


class CoordinatorStateError(CoordinatorError):
    """非法状态迁移（终态不可逆 / 跳迁）。"""


class InterruptSource(str, Enum):
    """中断来源（任务 18 引入）。

    - `PARENT`    父节点中断透传到子节点（递归）
    - `BUDGET`    全局预算停止
    - `OPERATOR`  人工中断（药师 / 信息科）
    - `TIMEOUT`   单节点超时
    - `UNKNOWN`   未指明来源（兜底）
    """

    PARENT = "parent"
    BUDGET = "budget"
    OPERATOR = "operator"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


#: 合法状态迁移表（任务 18 引入）。
#:
#: 节点状态机只允许：
#:   - `running ↔ waiting`（同状态自迁移也合法，便于重标记）
#:   - `running / waiting → {completed, failed, crashed, stopped}`
#:   - 终态 `completed / failed / crashed / stopped` 不可再迁移
#:
#: 例外：`trigger_budget_stop()` 与 `request_stop()` 走批量 / 直接改写
#: 路径，绕过本表（其内部已做终态保护）；`mark_*` 系列在终态时是幂等 no-op。
LEGAL_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.RUNNING: frozenset(
        {
            Status.RUNNING,
            Status.WAITING,
            Status.COMPLETED,
            Status.FAILED,
            Status.CRASHED,
            Status.STOPPED,
        }
    ),
    Status.WAITING: frozenset(
        {
            Status.RUNNING,
            Status.WAITING,
            Status.COMPLETED,
            Status.FAILED,
            Status.CRASHED,
            Status.STOPPED,
        }
    ),
    # 终态 —— 不可再迁移。
    Status.COMPLETED: frozenset(),
    Status.FAILED: frozenset(),
    Status.CRASHED: frozenset(),
    Status.STOPPED: frozenset(),
}


def field_agent_id(family: FieldFamily | str) -> str:
    """字段族 → 子 Agent id（与 spec.md 的 `agent_id` 样例格式一致）。"""
    value = family.value if isinstance(family, FieldFamily) else str(family)
    if not value:
        raise CoordinatorError("family must be a non-empty string")
    return f"rx_field_{value}"


# --------------------------------------------------------------------------- #
# Runtime                                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class AgentRuntime:
    """单个字段族 Agent 的运行时句柄（参考侧 AgentRuntime 的字段核对版）。

    参考侧持有 `session / stream / interrupt_on_message`；本项目无 LLM
    会话，替换为 `family`（该节点负责的字段族）与 `findings`（该族产出的
    字段证据卡，供全部完成后聚合 findings.json）。

    任务 18 新增中断跟踪：
      - `interrupt_source` / `interrupt_reason`：最近一次中断的来源与原因
      - `interrupt_event`：异步信号，便于 `await interrupt_event.wait()`
        风格的协作式中断（确定性 Agent 不强制使用，但 run_field_agents
        可以基于此做更优雅的中断传播）
    """

    family: Optional[str] = None
    task: Optional["asyncio.Task[Any]"] = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    findings: list[Finding] = field(default_factory=list)
    interrupt_source: Optional[str] = None
    interrupt_reason: Optional[str] = None
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)


# --------------------------------------------------------------------------- #
# Coordinator                                                                 #
# --------------------------------------------------------------------------- #


class AgentCoordinator:
    """图状态的单一持有者：注册、状态迁移、快照、预算停止。

    所有可变状态的读写都在 `self._lock` 内完成，因此 8 个字段族并发
    `register` / `set_status` 不会互相踩踏（参考侧同款 `asyncio.Lock`）。
    """

    def __init__(self) -> None:
        self.statuses: dict[str, Status] = {}
        self.parent_of: dict[str, Optional[str]] = {}
        self.names: dict[str, str] = {}
        self.families: dict[str, Optional[str]] = {}
        self.metadata: dict[str, dict[str, Any]] = {}
        self.errors: dict[str, str] = {}
        self.runtimes: dict[str, AgentRuntime] = {}
        self._lock = asyncio.Lock()
        self._snapshot_path: Optional[Path] = None
        self.is_shutting_down = False
        self._budget_stopped = False
        # 任务 19：父节点 `wait_for_child` 用 per-child 完成事件（高效唤醒
        # 替代轮询）；节点进入终态时由 `set_status` / `request_stop` /
        # `trigger_budget_stop` 触发对应事件。
        self._child_done_events: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------ #
    # 快照 / 生命周期开关                                                   #
    # ------------------------------------------------------------------ #

    def set_snapshot_path(self, path: Path | str) -> None:
        """设置图快照落盘路径（约定 `runs/<rx_no>/graph.json`）。"""
        self._snapshot_path = Path(path)

    def mark_shutting_down(self) -> None:
        self.is_shutting_down = True

    @property
    def budget_stopped(self) -> bool:
        return self._budget_stopped

    async def trigger_budget_stop(self, *, reason: str = "budget stop") -> None:
        """全局停止：唤醒所有节点，并把未进终态的节点置为 `stopped`。

        参考侧只 `wake.set()`，由各 Agent 自行退出循环；本项目字段核对是
        短确定性任务、无长驻消息循环，故由协调器直接落停止状态，保证
        「停止后图上不残留 running 节点」这一审计口径。

        任务 19 协同：在同一把锁内同步设置所有 child_done_event，让
        并发的 `wait_for_child` 在本调用结束时立即返回，不会因并发
        `interrupt` / `wake` 信号被吞而阻塞。
        """
        async with self._lock:
            self._budget_stopped = True
            for agent_id, status in list(self.statuses.items()):
                if status not in TERMINAL_STATUSES:
                    self.statuses[agent_id] = Status.STOPPED
                    self.errors.setdefault(agent_id, reason)
                    # 触发对应 child_done event —— 让 wait_for_child 立即返回
                    event = self._child_done_events.get(agent_id)
                    if event is not None:
                        event.set()
            for runtime in self.runtimes.values():
                runtime.wake.set()
        logger.info("coordinator.budget_stop reason=%s", reason)
        await self._maybe_snapshot()

    # ------------------------------------------------------------------ #
    # 注册                                                                 #
    # ------------------------------------------------------------------ #

    async def register(
        self,
        agent_id: str,
        name: str,
        parent_id: Optional[str] = None,
        *,
        family: FieldFamily | str | None = None,
        task: Optional[str] = None,
        rule_version: Optional[str] = None,
    ) -> None:
        """注册一个节点（根或字段族子节点），初始状态 `running`。

        Raises:
            CoordinatorError: agent_id 为空 / 重复注册 / parent 未注册。
        """
        if not agent_id or not isinstance(agent_id, str):
            raise CoordinatorError("agent_id must be a non-empty string")
        family_value = family.value if isinstance(family, FieldFamily) else family

        async with self._lock:
            if agent_id in self.statuses:
                raise CoordinatorError(f"agent already registered: {agent_id}")
            if parent_id is not None and parent_id not in self.statuses:
                raise CoordinatorError(
                    f"cannot register {agent_id!r}: parent {parent_id!r} not registered"
                )
            self.statuses[agent_id] = Status.RUNNING
            self.parent_of[agent_id] = parent_id
            self.names[agent_id] = name
            self.families[agent_id] = family_value
            self.metadata[agent_id] = {
                "task": task or "",
                "family": family_value or "",
                "rule_version": rule_version or "",
            }
            self.runtimes.setdefault(agent_id, AgentRuntime(family=family_value))
        logger.info(
            "agent.register %s (%s) parent=%s family=%s",
            agent_id,
            name,
            parent_id or "-",
            family_value or "-",
        )
        await self._maybe_snapshot()

    async def register_root(
        self,
        prescription_no: str,
        *,
        agent_id: str = ROOT_AGENT_ID,
    ) -> str:
        """注册处方根节点（参考侧的「根 agent = 扫描目标」）。"""
        await self.register(
            agent_id,
            name=f"处方核对 {prescription_no}",
            parent_id=None,
            task=prescription_no,
        )
        return agent_id

    async def register_field_agents(
        self,
        families: Iterable[FieldFamily | str],
        *,
        parent_id: str = ROOT_AGENT_ID,
        rule_version: str = "",
    ) -> list[str]:
        """按字段族批量注册子节点，返回 agent_id 列表（注册顺序）。"""
        agent_ids: list[str] = []
        for fam in families:
            value = fam.value if isinstance(fam, FieldFamily) else str(fam)
            aid = field_agent_id(value)
            await self.register(
                aid,
                name=f"{value} 字段族核对",
                parent_id=parent_id,
                family=value,
                rule_version=rule_version,
            )
            agent_ids.append(aid)
        return agent_ids

    async def attach_runtime(
        self,
        agent_id: str,
        *,
        task: Optional["asyncio.Task[Any]"] = None,
    ) -> None:
        """挂载 asyncio.Task 句柄，便于停止时取消（参考侧同名方法的裁剪版）。"""
        async with self._lock:
            runtime = self.runtimes.setdefault(agent_id, AgentRuntime())
            if task is not None:
                runtime.task = task

    # ------------------------------------------------------------------ #
    # 状态迁移                                                             #
    # ------------------------------------------------------------------ #

    async def set_status(
        self,
        agent_id: str,
        status: Status | str,
        *,
        error: Optional[str] = None,
    ) -> None:
        """设置节点状态；未知节点静默忽略（与参考侧一致，避免竞态抛错）。

        任务 18 起，迁移前对照 `LEGAL_TRANSITIONS`：非法迁移（如
        `completed → running`、`failed → stopped`）抛 `CoordinatorStateError`。
        终态不可逆 —— 这是审计口径，保证 `graph.json` 不被并发回写污染。
        """
        resolved = self._coerce_status(status)
        async with self._lock:
            if agent_id not in self.statuses:
                logger.debug("agent.status dropped unknown agent=%s", agent_id)
                return
            current = self.statuses[agent_id]
            if resolved not in LEGAL_TRANSITIONS[current]:
                raise CoordinatorStateError(
                    f"illegal status transition for {agent_id}: "
                    f"{current.value} -> {resolved.value}; "
                    f"allowed from {current.value}: "
                    f"{sorted(s.value for s in LEGAL_TRANSITIONS[current]) or '(terminal)'}"
                )
            self.statuses[agent_id] = resolved
            if error is not None:
                self.errors[agent_id] = error
            elif resolved is Status.RUNNING:
                self.errors.pop(agent_id, None)
            self.runtimes.setdefault(agent_id, AgentRuntime()).wake.set()
            # 任务 19：终态触发 child_done event，让 wait_for_child 立即返回
            if resolved in TERMINAL_STATUSES:
                done_event = self._child_done_events.get(agent_id)
                if done_event is not None:
                    done_event.set()
        logger.info("agent.status %s=%s", agent_id, resolved.value)
        await self._maybe_snapshot()

    async def mark_running(self, agent_id: str) -> None:
        """标记为 running；终态下是幂等 no-op（防止并发回写破坏审计口径）。"""
        if await self._is_terminal_locked(agent_id):
            logger.debug("agent.mark_running noop terminal=%s", agent_id)
            return
        await self.set_status(agent_id, Status.RUNNING)

    async def mark_completed(
        self,
        agent_id: str,
        findings: Optional[Iterable[Finding]] = None,
    ) -> None:
        """标记完成，并把该族产出的字段证据卡挂到 runtime 上待聚合。

        终态下幂等：已 completed 的节点不再覆盖（避免并发 confirm 把已
        终态节点改回 running 等非法迁移）；findings 也不会被清空。
        """
        current = await self.status_of(agent_id)
        if current in TERMINAL_STATUSES:
            logger.debug("agent.mark_completed noop terminal=%s", agent_id)
            return
        if findings is not None:
            async with self._lock:
                runtime = self.runtimes.setdefault(agent_id, AgentRuntime())
                runtime.findings = list(findings)
        await self.set_status(agent_id, Status.COMPLETED)

    async def mark_failed(self, agent_id: str, error: str) -> None:
        """标记 failed；终态下幂等（保持现有错误信息不丢失）。"""
        if await self._is_terminal_locked(agent_id):
            logger.debug("agent.mark_failed noop terminal=%s", agent_id)
            return
        await self.set_status(agent_id, Status.FAILED, error=error)

    async def mark_crashed(self, agent_id: str, error: str) -> None:
        """标记 crashed；终态下幂等（关键：兄弟节点并发失败时不会互相冲突）。

        兄弟族并发失败的场景下，第一个失败把父节点打成 CRASHED；第二个
        失败再尝试 mark_crashed 父节点，若非幂等会因 `crashed → crashed`
        触发 `CoordinatorStateError`。此处的幂等保护让父节点错误信息
        保持「第一个失败者」的记录（用 `setdefault` 语义）。
        """
        if await self._is_terminal_locked(agent_id):
            logger.debug("agent.mark_crashed noop terminal=%s", agent_id)
            return
        await self.set_status(agent_id, Status.CRASHED, error=error)

    async def request_stop(self, agent_id: str, *, reason: str = "stop requested") -> None:
        """请求单个节点停止；已在终态则不改写。"""
        async with self._lock:
            current = self.statuses.get(agent_id)
            if current is None or current in TERMINAL_STATUSES:
                return
            self.statuses[agent_id] = Status.STOPPED
            self.errors.setdefault(agent_id, reason)
            self.runtimes.setdefault(agent_id, AgentRuntime()).wake.set()
            # 任务 19：触发 child_done event
            done_event = self._child_done_events.get(agent_id)
            if done_event is not None:
                done_event.set()
        await self._maybe_snapshot()

    # ------------------------------------------------------------------ #
    # 派发（融合后的产品主路径：一张处方 → 8 个字段族并发核对）              #
    # ------------------------------------------------------------------ #

    async def supervise(
        self,
        agent_id: str,
        coro: Awaitable[Any],
        *,
        propagate_crash: bool = True,
    ) -> Optional[Any]:
        """跑一个字段族 Agent 协程，把异常收敛成状态而不是往上炸。

        - 正常返回 → `completed`；返回值若是 Finding 序列则挂到 runtime
        - 抛异常   → 该节点 `failed`（记 error），父节点 `crashed`（可关）
        - 被取消   → 该节点 `stopped`，并把 CancelledError 继续上抛

        兄弟节点不受影响：调用方用 `asyncio.gather(..., return_exceptions=True)`
        或本类的 `run_field_agents` 并发驱动即可。
        """
        await self.mark_running(agent_id)
        try:
            result = await coro
        except asyncio.CancelledError:
            await self.set_status(agent_id, Status.STOPPED, error="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - 故意兜住任何族内异常
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("agent.failed %s %s", agent_id, message)
            await self.mark_failed(agent_id, message)
            if propagate_crash:
                parent_id = await self.parent_of_agent(agent_id)
                if parent_id is not None:
                    await self.mark_crashed(
                        parent_id, f"child {agent_id} failed: {message}"
                    )
            return None
        await self.mark_completed(agent_id, _as_findings(result))
        return result

    async def run_field_agents(
        self,
        jobs: Mapping[str, Awaitable[Any]],
        *,
        root_id: str = ROOT_AGENT_ID,
    ) -> dict[str, Any]:
        """并发驱动已注册的字段族 Agent，返回 {agent_id: 结果或 None}。

        单个族失败不会取消其他族（`gather(return_exceptions=True)` +
        `supervise` 内部吞异常双保险）；全部结束后按子节点结果推进根节点：
        有 failed → 根 `crashed`（由 supervise 透传），否则根 `completed`。
        """
        agent_ids = list(jobs.keys())
        results = await asyncio.gather(
            *(self.supervise(aid, jobs[aid]) for aid in agent_ids),
            return_exceptions=True,
        )
        collected: dict[str, Any] = {}
        for aid, result in zip(agent_ids, results):
            collected[aid] = None if isinstance(result, BaseException) else result

        async with self._lock:
            root_status = self.statuses.get(root_id)
        if root_status is not None and root_status not in TERMINAL_STATUSES:
            await self.set_status(root_id, Status.COMPLETED)
        return collected

    # ------------------------------------------------------------------ #
    # 查询 / 视图                                                          #
    # ------------------------------------------------------------------ #

    async def parent_of_agent(self, agent_id: str) -> Optional[str]:
        async with self._lock:
            return self.parent_of.get(agent_id)

    async def children_of(self, agent_id: str) -> list[str]:
        async with self._lock:
            return [
                child for child, parent in self.parent_of.items() if parent == agent_id
            ]

    async def status_of(self, agent_id: str) -> Optional[Status]:
        async with self._lock:
            return self.statuses.get(agent_id)

    async def view_graph(self) -> list[dict[str, Any]]:
        """图视图：每个节点一条 `{agent_id, name, parent_id, status, ...}`。

        对应参考侧 `view_agent_graph` 工具（它渲染成缩进文本；这里直出
        JSON 结构，供 `runs/<rx_no>/graph.json` 与 Viewer 端复用）。
        根节点在前，子节点按注册顺序跟随（深度优先）。

        任务 18 起额外携带 `interrupt_source` / `interrupt_reason` 字段
        （未中断时为空字符串），便于审计追溯「为何该节点停下」。
        """
        async with self._lock:
            parent_of = dict(self.parent_of)
            statuses = dict(self.statuses)
            names = dict(self.names)
            families = dict(self.families)
            errors = dict(self.errors)
            interrupts = {
                aid: (r.interrupt_source or "", r.interrupt_reason or "")
                for aid, r in self.runtimes.items()
            }

        nodes: list[dict[str, Any]] = []

        def walk(aid: str) -> None:
            src, reason = interrupts.get(aid, ("", ""))
            nodes.append(
                {
                    "agent_id": aid,
                    "name": names.get(aid, aid),
                    "parent_id": parent_of.get(aid),
                    "status": statuses[aid].value,
                    "family": families.get(aid),
                    "error": errors.get(aid, ""),
                    "interrupt_source": src,
                    "interrupt_reason": reason,
                }
            )
            for child, parent in parent_of.items():
                if parent == aid:
                    walk(child)

        for aid, parent in parent_of.items():
            if parent is None:
                walk(aid)
        # 孤儿节点（父节点已被移除的极端情况）也要出现在图上，不静默吞掉
        seen = {node["agent_id"] for node in nodes}
        for aid in parent_of:
            if aid not in seen:
                walk(aid)
        return nodes

    async def graph_summary(self) -> dict[str, int]:
        """各状态计数 + total（参考侧 `view_agent_graph` 的 summary 段）。"""
        async with self._lock:
            statuses = list(self.statuses.values())
        summary = {"total": len(statuses)}
        for status in Status:
            summary[status.value] = sum(1 for s in statuses if s is status)
        return summary

    async def collect_findings(self) -> list[Finding]:
        """聚合所有子节点产出的字段证据卡（按注册顺序，供 findings.json）。"""
        async with self._lock:
            order = list(self.statuses.keys())
            runtimes = dict(self.runtimes)
        collected: list[Finding] = []
        for aid in order:
            runtime = runtimes.get(aid)
            if runtime is not None:
                collected.extend(runtime.findings)
        return collected

    async def failed_agents(self) -> dict[str, str]:
        """失败 / 崩溃节点 → 错误信息（供完成报告的 FAILED 段）。"""
        async with self._lock:
            return {
                aid: self.errors.get(aid, "")
                for aid, status in self.statuses.items()
                if status in {Status.FAILED, Status.CRASHED}
            }

    # ------------------------------------------------------------------ #
    # 中断（任务 18 引入）                                                  #
    # ------------------------------------------------------------------ #

    async def interrupt(
        self,
        agent_id: str,
        source: InterruptSource | str = InterruptSource.UNKNOWN,
        *,
        reason: str = "",
        propagate_to_children: bool = True,
    ) -> None:
        """向节点发中断信号；默认递归透传到子节点（PARENT 来源）。

        中断**不强制**改写节点 status —— 节点可以：
          1. 调 `ack_interrupt()` 取出 `(source, reason)` 后自行决定
             `set_status(STOPPED)` 或清理后继续；
          2. 监听 `runtime.interrupt_event.wait()` 做协作式退出。

        同一节点多次 interrupt 时**保留首次来源**（后到信号不覆盖审计记录）。
        """
        source_value = (
            source.value if isinstance(source, InterruptSource) else str(source)
        )
        child_ids: list[str] = []
        async with self._lock:
            if agent_id not in self.statuses:
                logger.debug("agent.interrupt dropped unknown agent=%s", agent_id)
                return
            runtime = self.runtimes.setdefault(agent_id, AgentRuntime())
            if runtime.interrupt_source is None:
                runtime.interrupt_source = source_value
                runtime.interrupt_reason = reason or ""
                runtime.interrupt_event.set()
            else:
                logger.debug(
                    "agent.interrupt %s already interrupted by %s; keep",
                    agent_id,
                    runtime.interrupt_source,
                )
            if propagate_to_children:
                child_ids = [
                    child
                    for child, parent in self.parent_of.items()
                    if parent == agent_id
                ]
        logger.info(
            "agent.interrupt %s source=%s reason=%s children=%d",
            agent_id,
            source_value,
            reason,
            len(child_ids),
        )
        # 递归到子节点，来源改写为 PARENT（便于审计追溯中断根因）
        for child_id in child_ids:
            await self.interrupt(
                child_id,
                InterruptSource.PARENT,
                reason=f"parent {agent_id} interrupted: {reason}",
                propagate_to_children=True,
            )
        await self._maybe_snapshot()

    async def ack_interrupt(
        self, agent_id: str
    ) -> Optional[tuple[str, str]]:
        """取出并清空中断信号；已 ack 的再次 ack 返回 None。
        Agent 在收到中断后调用此方法决定下一步（推荐 `set_status(STOPPED)`）。
        """
        async with self._lock:
            runtime = self.runtimes.setdefault(agent_id, AgentRuntime())
            if runtime.interrupt_source is None:
                return None
            payload = (runtime.interrupt_source, runtime.interrupt_reason or "")
            runtime.interrupt_source = None
            runtime.interrupt_reason = None
            runtime.interrupt_event.clear()
        return payload

    async def is_interrupted(self, agent_id: str) -> bool:
        async with self._lock:
            runtime = self.runtimes.get(agent_id)
            return bool(runtime and runtime.interrupt_source)

    async def interrupt_summary(self) -> dict[str, dict[str, str]]:
        """所有节点的中断来源 / 原因（供 Viewer 调试与 graph.json）。"""
        async with self._lock:
            return {
                aid: {"source": r.interrupt_source, "reason": r.interrupt_reason or ""}
                for aid, r in self.runtimes.items()
                if r.interrupt_source is not None
            }

    # ------------------------------------------------------------------ #
    # 并发原语：wait_for_child / wake / interrupt 双信号（任务 19 引入）        #
    # ------------------------------------------------------------------ #
    #
    # 设计要点：
    # - `wait_for_child(child_id, *, timeout)` —— 父节点等待子节点进入
    #   终态；用 per-child `asyncio.Event` 实现，避免父节点忙等轮询。
    # - `wake(agent_id, *, reason)` —— 仅触发 `runtime.wake`，**不**
    #   改写节点状态；区别于 `interrupt`：wake 是「通知」语义（父节点
    #   完成 → 通知子节点），interrupt 是「停止」语义（节点应 ack 后退出）。
    # - `interrupt` 与 `trigger_budget_stop` 在同一把锁内设置 child_done
    #   event，确保并发场景下 `wait_for_child` 不会被信号竞争吞掉。

    async def wait_for_child(
        self,
        child_id: str,
        *,
        timeout: Optional[float] = None,
    ) -> Optional[Status]:
        """父节点等子节点进入终态；返回终态 `Status` 或 `None`（超时）。

        实现要点：
        - 已经在终态 → 立刻返回（不进入 await）。
        - 否则拿锁后取出 / 懒初始化 per-child `asyncio.Event`，锁外 await。
        - 节点进入终态时（`set_status` / `request_stop` /
          `trigger_budget_stop` 内部）会 `event.set()`，本方法即时唤醒。
        - 超时返回 `None`；不抛 `CoordinatorStateError`（与
          `try_complete_coordinator` 同款「不阻断父节点主流程」语义）。

        Args:
            child_id: 被等待的子节点 id（字段 Agent 通常用 `field_agent_id(family)`）。
            timeout: 秒；`None` = 永久等待。

        Raises:
            CoordinatorError: `child_id` 未注册。
        """
        async with self._lock:
            if child_id not in self.statuses:
                raise CoordinatorError(
                    f"cannot wait_for_child: unknown agent {child_id!r}"
                )
            current = self.statuses[child_id]
            if current in TERMINAL_STATUSES:
                return current
            event = self._child_done_events.setdefault(child_id, asyncio.Event())

        try:
            if timeout is None:
                await event.wait()
            else:
                await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.debug(
                "wait_for_child timeout child=%s timeout=%s", child_id, timeout
            )
            return None

        async with self._lock:
            return self.statuses.get(child_id)

    async def wake(
        self,
        agent_id: str,
        *,
        reason: str = "",
    ) -> None:
        """唤醒节点（不改变状态；仅触发 `runtime.wake` 事件）。

        与 `interrupt` 的区别：
        - `wake` —— 「通知」语义；节点被唤醒后可继续运行或自行决定下一步。
          典型场景：父节点完成 → 通知子节点有新数据 / 上游就绪。
        - `interrupt` —— 「停止」语义；节点应通过 `ack_interrupt()` 取出
          信号后决定退出或继续；多次 interrupt 保留首次来源（审计口径）。

        未知节点静默忽略（与 `set_status` / `interrupt` 保持一致语义）。
        """
        async with self._lock:
            if agent_id not in self.statuses:
                logger.debug("agent.wake dropped unknown agent=%s", agent_id)
                return
            runtime = self.runtimes.setdefault(agent_id, AgentRuntime())
            runtime.wake.set()
        logger.debug("agent.wake %s reason=%s", agent_id, reason)

    async def try_complete_coordinator(
        self,
        root_id: str = ROOT_AGENT_ID,
    ) -> Status:
        """根据子节点状态聚合根节点终态（任务 18 引入）。

        规则：
          - 根节点已在终态 → 直接返回当前状态（幂等）
          - 任一子节点 `failed` / `crashed` → 根节点 `crashed`
          - 所有子节点 `completed` → 根节点 `completed`
          - 子节点含 `stopped`（非 `PARENT` 中断引发）→ 单独评估；按
            `failed/crashed` 优先规则收敛
          - 其他情形（子节点尚有 `running` / `waiting`）→ 保持根节点
            当前状态，返回之

        不抛 `CoordinatorStateError`（根节点若已是终态或待定状态都安全）。
        """
        async with self._lock:
            current_root = self.statuses.get(root_id)
            if current_root in TERMINAL_STATUSES:
                return current_root
            children_statuses = [
                status
                for aid, status in self.statuses.items()
                if self.parent_of.get(aid) == root_id
            ]
        if not children_statuses:
            return current_root or Status.RUNNING

        # 任何 failed → 根 crashed（保留首次错误信息）
        for child_status in children_statuses:
            if child_status in {Status.FAILED, Status.CRASHED}:
                error_message = await self._format_child_error(root_id)
                await self.mark_crashed(root_id, error_message)
                return Status.CRASHED

        # 全 completed → 根 completed
        if children_statuses and all(
            s is Status.COMPLETED for s in children_statuses
        ):
            await self.mark_completed(root_id)
            return Status.COMPLETED

        # 至少一个 stopped → 收敛规则：有其他终态则取最高级，否则保持 running
        if any(s is Status.STOPPED for s in children_statuses):
            # 若有 STOPPED 但其它子节点都还在 RUNNING/WAITING，不强制终态根节点
            return current_root or Status.RUNNING

        return current_root or Status.RUNNING

    async def _format_child_error(self, root_id: str) -> str:
        async with self._lock:
            bad = [
                aid
                for aid, status in self.statuses.items()
                if self.parent_of.get(aid) == root_id
                and status in {Status.FAILED, Status.CRASHED}
            ]
            errors = {aid: self.errors.get(aid, "") for aid in bad}
        if not errors:
            return "child failed"
        # 只展示首个失败者的原因（用 setdefault 语义保首次）
        first_aid, first_msg = next(iter(errors.items()))
        return f"child {first_aid} failed: {first_msg}"

    # ------------------------------------------------------------------ #
    # 快照 / 恢复                                                          #
    # ------------------------------------------------------------------ #

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "statuses": {aid: s.value for aid, s in self.statuses.items()},
                "parent_of": dict(self.parent_of),
                "names": dict(self.names),
                "families": dict(self.families),
                "metadata": {aid: dict(md) for aid, md in self.metadata.items()},
                "errors": dict(self.errors),
                "budget_stopped": self._budget_stopped,
                "interrupts": {
                    aid: {"source": r.interrupt_source, "reason": r.interrupt_reason or ""}
                    for aid, r in self.runtimes.items()
                    if r.interrupt_source is not None
                },
            }

    async def restore(self, snap: Mapping[str, Any]) -> None:
        async with self._lock:
            self.statuses = {
                aid: self._coerce_status(value)
                for aid, value in snap.get("statuses", {}).items()
            }
            self.parent_of = dict(snap.get("parent_of", {}))
            self.names = dict(snap.get("names", {}))
            self.families = dict(snap.get("families", {}))
            self.metadata = {
                aid: dict(md) for aid, md in snap.get("metadata", {}).items()
            }
            self.errors = dict(snap.get("errors", {}))
            self._budget_stopped = bool(snap.get("budget_stopped", False))
            for aid in self.statuses:
                self.runtimes.setdefault(
                    aid, AgentRuntime(family=self.families.get(aid))
                )
            # 还原中断信号（任务 18）
            for aid, info in snap.get("interrupts", {}).items():
                if aid not in self.runtimes:
                    continue
                runtime = self.runtimes[aid]
                runtime.interrupt_source = info.get("source")
                runtime.interrupt_reason = info.get("reason", "")
                if runtime.interrupt_source is not None:
                    runtime.interrupt_event.set()

    async def _maybe_snapshot(self) -> None:
        """原子写图快照（tmp 同目录 + `replace`），失败不影响核对主流程。"""
        path = self._snapshot_path
        if path is None:
            return
        try:
            data = await self.snapshot()
            data["nodes"] = await self.view_graph()
            payload = json.dumps(data, ensure_ascii=False, default=str, indent=2)
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp.write(payload)
                tmp_path = Path(tmp.name)
            tmp_path.replace(path)
        except Exception:  # noqa: BLE001 - 快照是审计辅助，不阻断核对
            logger.exception("coordinator snapshot to %s failed", path)

    # ------------------------------------------------------------------ #
    # 内部                                                                 #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _coerce_status(status: Status | str) -> Status:
        if isinstance(status, Status):
            return status
        try:
            return Status(status)
        except ValueError as exc:
            raise CoordinatorError(
                f"unknown status {status!r}; valid: {[s.value for s in Status]}"
            ) from exc

    async def _is_terminal_locked(self, agent_id: str) -> bool:
        """检查节点是否已进终态（用于 mark_* 幂等保护）。"""
        async with self._lock:
            current = self.statuses.get(agent_id)
        return current in TERMINAL_STATUSES


def _as_findings(result: Any) -> Optional[list[Finding]]:
    """把字段族 Agent 的返回值收敛成 Finding 列表（非 Finding 一律忽略）。"""
    if isinstance(result, Finding):
        return [result]
    if isinstance(result, (list, tuple)) and all(
        isinstance(item, Finding) for item in result
    ):
        return list(result)
    return None


__all__ = [
    "LEGAL_TRANSITIONS",
    "ROOT_AGENT_ID",
    "TERMINAL_STATUSES",
    "AgentCoordinator",
    "AgentRuntime",
    "CoordinatorError",
    "CoordinatorStateError",
    "InterruptSource",
    "Status",
    "field_agent_id",
]
