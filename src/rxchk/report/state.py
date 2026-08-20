"""处方级状态机（`uploaded → parsing → running → awaiting_review →
confirmed | overridden → written_back`）。

与 `rxchk.coordinator.Status` 节点级状态机的关系
-----------------------------------------------
- 协调器节点级状态：`running / waiting / completed / failed / crashed /
  stopped`（描述**单个字段族 Agent** 的状态）
- 本模块处方级状态：`uploaded / parsing / running / awaiting_review /
  confirmed / overridden / written_back`（描述**整张处方核对流水**）

独立是因为：
- 一张处方常常有多个字段族 Agent 并发跑，处方级状态由事件聚合推进
  （如「8 个字段族全部完成 → awaiting_review」是处方级迁移，但每个
  Agent 是协调器里的节点级 completed）
- 药师确认 / 驳回是处方级终态推进（`confirmed | overridden`），与
  协调器无关
- `written_back` 是 HIS / 前置审方回写后的处方级终态（任务 13）

合法迁移表（与 spec.md「输出闭环」表一致）
-----------------------------------------

    uploaded        ──→ parsing
    parsing         ──→ running
    running         ──→ running       （同状态去重）
    running         ──→ awaiting_review
    running         ──→ failed_parse  （任务 14 异常路径扩展，本任务不引入）
    awaiting_review ──→ confirmed
    awaiting_review ──→ overridden
    confirmed       ──→ written_back
    overridden      ──→ written_back

    非法迁移（抛 `RunStateError`）：
    - 任意状态 → uploaded（不可回滚到起点）
    - confirmed / overridden → awaiting_review（已决不回退）
    - written_back → 任意（终态）
    - 同名同状态幂等迁移（去重；不抛错；与协调器 set_status 一致）

任务 14（端到端）会扩展 `failed_parse` / `partial_failure` 等异常路径
状态，本任务只搭骨架 + 验收关键迁移。
"""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet


# --------------------------------------------------------------------------- #
# 处方级状态                                                                  #
# --------------------------------------------------------------------------- #


class RunState(str, Enum):
    """处方级状态机节点。字符串值小写，与 `run.json` 输出对齐。"""

    UPLOADED = "uploaded"
    PARSING = "parsing"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    CONFIRMED = "confirmed"
    OVERRIDDEN = "overridden"
    WRITTEN_BACK = "written_back"


#: 终态：进入后不再迁移（与协调器 TERMINAL_STATUSES 同语义）。
TERMINAL_RUN_STATES: frozenset[RunState] = frozenset(
    {RunState.WRITTEN_BACK}
)

#: 合法迁移表（from → {to}）。同状态去重不在表里 —— `is_valid_transition`
#: 对 `from == to` 始终返回 True（幂等迁移），调用方视业务决定是否落盘。
RUN_STATE_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.UPLOADED: frozenset({RunState.PARSING}),
    RunState.PARSING: frozenset({RunState.RUNNING}),
    RunState.RUNNING: frozenset({RunState.AWAITING_REVIEW}),
    RunState.AWAITING_REVIEW: frozenset(
        {RunState.CONFIRMED, RunState.OVERRIDDEN}
    ),
    RunState.CONFIRMED: frozenset({RunState.WRITTEN_BACK}),
    RunState.OVERRIDDEN: frozenset({RunState.WRITTEN_BACK}),
    RunState.WRITTEN_BACK: frozenset(),  # 终态
}


# --------------------------------------------------------------------------- #
# 异常                                                                        #
# --------------------------------------------------------------------------- #


class RunStateError(ValueError):
    """非法处方级状态迁移。"""


# --------------------------------------------------------------------------- #
# 校验                                                                        #
# --------------------------------------------------------------------------- #


def is_valid_transition(src: RunState | str, dst: RunState | str) -> bool:
    """判定 `src → dst` 是否合法。

    规则：
    - 同状态幂等 → True（去重不抛错；非终态允许幂等写入）
    - 终态 → 任意 → False（**含**同状态幂等；终态完全冻结）
    - 否则查 `RUN_STATE_TRANSITIONS` 表
    """
    src_r = _coerce(src)
    dst_r = _coerce(dst)
    if src_r in TERMINAL_RUN_STATES:
        # 终态完全冻结：同状态幂等亦不允许
        return False
    if src_r is dst_r:
        # 非终态同状态幂等迁移允许
        return True
    return dst_r in RUN_STATE_TRANSITIONS.get(src_r, frozenset())


def assert_transition(src: RunState | str, dst: RunState | str) -> None:
    """非法迁移抛 `RunStateError`；合法（含非终态幂等）静默通过。

    终态幂等也视为非法迁移（与 `is_valid_transition` 一致：终态冻结）。
    """
    if not is_valid_transition(src, dst):
        src_v = _coerce(src).value
        dst_v = _coerce(dst).value
        raise RunStateError(
            f"illegal run-state transition: {src_v} -> {dst_v}"
        )


# --------------------------------------------------------------------------- #
# 内部                                                                       #
# --------------------------------------------------------------------------- #


def _coerce(state: RunState | str) -> RunState:
    """`str` / `RunState` → `RunState`；非法字符串抛 `RunStateError`。"""
    if isinstance(state, RunState):
        return state
    if isinstance(state, str):
        try:
            return RunState(state)
        except ValueError as exc:
            raise RunStateError(
                f"unknown run state: {state!r}; "
                f"expected one of {[s.value for s in RunState]}"
            ) from exc
    raise RunStateError(
        f"run state must be RunState or str, got {type(state).__name__}"
    )