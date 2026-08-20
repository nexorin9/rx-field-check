"""药师工作站审计：confirm / override / reject / manual_override 操作日志。

`runs/<处方号>/audit.jsonl` 是 append-only 的药师操作流水：每条记录含
时间戳、操作类型、操作人、备注、关联 finding_id 与 `manual_override`
标记。审计日志是飞检 / 内审场景下唯一可逐条回放的证据载体，因此本模块
的设计要点都是「不让任何坏操作混进来 + 不让任何合法操作被改写」。

参考源（github_ref 代码证据）
-----------------------------
- `strix/tools/agents_graph/tools.py: _render_completion_report` —— 终态
  报告 schema + append-only 事件流的同款意识
- `strix/core/agents.py: _maybe_snapshot` —— 状态变更的 tmp + rename
  原子写（run.json 不允许半截）

设计要点
--------
- **append-only**：`audit.jsonl` 只允许向后追加，不允许 truncate 既有行；
  任何「修正既有审计」的诉求都要走新追加行 + 注释，而非覆盖。
- **状态前置校验**：append 前先校验处方级状态迁移是否合法。被拒操作
  不能在 audit.jsonl 里留下「看起来已生效」的痕迹（已确认 / 已回写的
  处方尤其不能被改回）。
- **损坏可恢复**：audit.jsonl 末尾某行被截断（进程崩 / 写盘中断）时，
  下次追加直接 open("a") 在文件末尾续写 —— 损坏行由 reader 跳过，新行
  仍可被识别为合法 JSON。读路径见 `rxchk.viewer.transcript.read_audit`。
- **run.json 推进**：每次成功 append 后，处方级状态推进到
  `confirmed | overridden`，由 `_advance_run_state` 原子写。回写阶段
  （任务 13）把 `overridden` 推到 `written_back`。
- **operator 必填**：审计日志必须能追到人 —— 空 operator 直接拒收，
  与 spec.md 安全边界一致。
- **note 长度上限**：`NOTE_MAX_CHARS` 防止跑飞的客户端把 audit.jsonl
  撑爆；超出截断而非抛错（保留药师操作意图）。

产品位置
--------
融合后的审计入口由 `viewer.server._handle_confirm` 调用，本模块提供
纯函数 `append_audit`，与 HTTP 层解耦，便于任务 14 的端到端测试
不依赖网络层就能验证完整闭环。

源 repo 名 / slug 不出现在产品代码或字符串字面值。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from rxchk.report.state import (
    RunState,
    RunStateError,
    assert_transition,
)
from rxchk.report.writer import (
    AUDIT_FILENAME,
    RUN_FILENAME,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 操作枚举与映射                                                               #
# --------------------------------------------------------------------------- #


#: 药师在证据卡模态里能做的操作。
#:
#: - `confirm`：认可命中并按提示处理（推进到 `confirmed`）
#: - `override`：认可命中但按临床判断放行（推进到 `overridden`）
#: - `reject`：判定为误报（推进到 `overridden`，HIS 回写时需带备注）
#: - `manual_override`：规则族未覆盖（uncovered）时的人工放行
#:   （推进到 `overridden`，audit 行带 `manual_override=true`）
AUDIT_ACTIONS: frozenset[str] = frozenset(
    {"confirm", "override", "reject", "manual_override"}
)


#: 操作 → 处方级目标状态。
#:
#: 驳回 / 放行 / 人工放行都视作「药师改了系统结论」，统一落到 `overridden`，
#: HIS 回写（任务 13）时按 `audit.jsonl` 是否带 `manual_override=true`
#: 决定是否在备注里追加「规则库未覆盖，由药师人工放行」字样。
_ACTION_TARGET_STATE: dict[str, RunState] = {
    "confirm": RunState.CONFIRMED,
    "override": RunState.OVERRIDDEN,
    "reject": RunState.OVERRIDDEN,
    "manual_override": RunState.OVERRIDDEN,
}


#: 备注长度上限：防止跑飞的客户端把 audit.jsonl 撑爆。
NOTE_MAX_CHARS: int = 2000


class AuditError(ValueError):
    """`append_audit` 入参 / 状态 / 落盘错误。"""


# --------------------------------------------------------------------------- #
# 记录 dataclass                                                               #
# --------------------------------------------------------------------------- #


@dataclass
class AuditEntry:
    """一条 audit 记录；与 `audit.jsonl` 一行一一对应。

    字段：
    - `ts`：写入时间（ISO 8601 UTC，秒级）
    - `action`：操作类型（`AUDIT_ACTIONS` 之一）
    - `operator`：操作人（必填；审计必须能追到人）
    - `note`：药师备注（截断到 `NOTE_MAX_CHARS`）
    - `manual_override`：是否为人工放行（仅 `manual_override` 操作时为 True）
    - `finding_id`：关联的 finding id（可选；evidence card 上点确认时携带）
    """

    ts: str
    action: str
    operator: str
    note: str = ""
    manual_override: bool = False
    finding_id: Optional[str] = None

    def to_json(self) -> str:
        """序列化为 JSON 行（`ensure_ascii=False` 保中文）。"""
        payload = {
            "ts": self.ts,
            "action": self.action,
            "operator": self.operator,
            "note": self.note,
            "manual_override": self.manual_override,
        }
        if self.finding_id is not None:
            payload["finding_id"] = self.finding_id
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "AuditEntry":
        """反序列化；非法 JSON 抛 `AuditError`。"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AuditError(f"audit jsonl 行非法：{exc}") from exc
        if not isinstance(data, dict):
            raise AuditError("audit jsonl 行必须是 JSON object")
        return cls(
            ts=str(data.get("ts", "")),
            action=str(data.get("action", "")),
            operator=str(data.get("operator", "")),
            note=str(data.get("note", "")),
            manual_override=bool(data.get("manual_override", False)),
            finding_id=(
                str(data["finding_id"])
                if data.get("finding_id") is not None
                else None
            ),
        )


# --------------------------------------------------------------------------- #
# 公开 API                                                                     #
# --------------------------------------------------------------------------- #


def append_audit(
    run_dir: Path | str,
    *,
    action: str,
    operator: str,
    note: str = "",
    finding_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> AuditEntry:
    """向 `runs/<处方号>/audit.jsonl` 追加一条药师操作。

    参数
    ----
    run_dir : Path | str
        `runs/<处方号>/` 目录（须已落 `run.json`）
    action : str
        操作类型，必须是 `AUDIT_ACTIONS` 之一
    operator : str
        操作人，必填且 strip 后非空
    note : str
        药师备注，超出 `NOTE_MAX_CHARS` 自动截断（不抛错）
    finding_id : str | None
        关联的 finding id（evidence card 上点确认时携带；可空）
    now : datetime | None
        时间戳注入点（默认 `datetime.now(timezone.utc)`）；测试用

    返回
    ----
    AuditEntry
        实际落盘的那条记录（含 `ts` / `manual_override`）

    异常
    ----
    AuditError
        - 未知 action
        - operator 为空
        - run_dir 缺 `run.json` / `run.json` 损坏
    RunStateError
        - 处方级状态迁移非法（终态冻结 / 跳级）
    OSError
        写盘失败透传（磁盘满 / 权限问题等）

    行为约定
    --------
    1. 写 audit.jsonl **之前**校验状态迁移：被拒操作不留痕
    2. audit.jsonl 用 open("a") + fsync，确保追加立即可见
    3. run.json 状态推进走 tmp + rename 原子写
    4. 同状态幂等迁移允许（多次 confirm 不抛错；run.json 不变）
    5. 终态（written_back）后再调用抛 `RunStateError`
    """
    # ---- 1. 入参校验 -------------------------------------------------------- #
    if action not in AUDIT_ACTIONS:
        raise AuditError(
            f"未知操作：{action!r}；可选 {sorted(AUDIT_ACTIONS)}"
        )
    operator_clean = operator.strip() if operator else ""
    if not operator_clean:
        raise AuditError("operator 必填：审计日志必须能追到人")

    run_dir_path = Path(run_dir)
    target_state = _ACTION_TARGET_STATE[action]

    # ---- 2. 状态前置校验（被拒操作不留痕） -------------------------------- #
    _assert_can_advance(run_dir_path, target_state)

    # ---- 3. 构造 audit 记录 ---------------------------------------------- #
    ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    entry = AuditEntry(
        ts=ts,
        action=action,
        operator=operator_clean,
        note=(note or "")[:NOTE_MAX_CHARS],
        manual_override=(action == "manual_override"),
        finding_id=finding_id,
    )

    # ---- 4. 追加到 audit.jsonl（append-only，损坏可恢复） ----------------- #
    audit_path = run_dir_path / AUDIT_FILENAME
    _append_jsonl_line(audit_path, entry.to_json())

    # ---- 5. 推进处方级状态（run.json 原子写） ----------------------------- #
    _advance_run_state(run_dir_path, target_state)

    logger.info(
        "audit.append rx_dir=%s action=%s operator=%s note_len=%d",
        run_dir_path,
        action,
        operator_clean,
        len(entry.note),
    )
    return entry


def read_audit_entries(run_dir: Path | str) -> list[AuditEntry]:
    """读 `audit.jsonl` 全部条目（按写入顺序）；损坏行跳过。

    与 `rxchk.viewer.transcript.read_audit` 行为一致：append-only 语义下，
    末尾被截断的损坏行由读路径容错跳过，不阻塞新追加。
    """
    path = Path(run_dir) / AUDIT_FILENAME
    if not path.is_file():
        return []
    entries: list[AuditEntry] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                entries.append(AuditEntry.from_json(line))
            except AuditError:
                logger.warning(
                    "audit.read 跳过损坏行：%s", path, exc_info=True
                )
                continue
    return entries


# --------------------------------------------------------------------------- #
# 内部：run.json 状态推进                                                       #
# --------------------------------------------------------------------------- #


def _read_run_state(run_dir: Path) -> tuple[dict[str, Any], RunState]:
    """读 `run.json` 与当前处方级状态；不可读时抛 `AuditError`。"""
    record_path = run_dir / RUN_FILENAME
    if not record_path.is_file():
        raise AuditError(
            f"run_dir 缺少 run.json：{run_dir}（须先由 ReportWriter 写入）"
        )
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"run.json 不可读：{exc}") from exc
    if not isinstance(record, dict):
        raise AuditError(f"run.json 必须是 JSON object：{record_path}")
    state_value = record.get("status", RunState.UPLOADED.value)
    try:
        state = RunState(state_value)
    except ValueError as exc:
        raise AuditError(
            f"run.json status 非法：{state_value!r}"
        ) from exc
    return record, state


def _assert_can_advance(run_dir: Path, target_state: RunState) -> None:
    """迁移合法性预检；非法迁移抛 `RunStateError`。"""
    _record, current = _read_run_state(run_dir)
    if current is target_state:
        # 同状态幂等放行；run.json 也不重写
        return
    assert_transition(current, target_state)


def _advance_run_state(run_dir: Path, target_state: RunState) -> None:
    """把 `run.json` 推到 `confirmed | overridden`；同状态幂等不动。

    状态机校验放在这里而不是端点里：任何写 audit 的入口（HTTP、批量
    补录、CLI 复核）都必须过同一张迁移表，避免绕过状态机把已回写的
    处方改回去。
    """
    record, current = _read_run_state(run_dir)
    if current is target_state:
        return
    assert_transition(current, target_state)
    record["status"] = target_state.value
    record["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record_path = run_dir / RUN_FILENAME
    tmp_path = record_path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(record_path)


# --------------------------------------------------------------------------- #
# 内部：append-only jsonl 写入                                                  #
# --------------------------------------------------------------------------- #


def _append_jsonl_line(path: Path, line: str) -> None:
    """追加一行 JSON 到 `audit.jsonl`；`fsync` 保证立即可见。

    - open("ab") 模式：append-only，文件末尾写
    - 文件不存在则创建（mode="a" 等同于 append + create if missing）
    - **尾部恢复**：若文件存在但末尾不以 `\\n` 结尾（写盘中断导致半行
      JSON），先补一个 `\\n` 再追加新行 —— 这样新行仍是一段完整可解析
      的 JSON 对象；旧半行由 reader 跳过，新行能被解析
    - `flush + fsync` 让药师在浏览器里立刻能看到这一行（端点返回前
      数据已落盘）
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = line if line.endswith("\n") else line + "\n"

    # 损坏尾部修复：append 前若文件末尾不是换行，先补一个换行，
    # 确保新行是独立可解析的 JSON
    needs_leading_newline = False
    if path.is_file() and path.stat().st_size > 0:
        try:
            with path.open("rb") as fh:
                fh.seek(-1, 2)  # 末字节
                last_byte = fh.read(1)
            if last_byte and last_byte != b"\n":
                needs_leading_newline = True
        except OSError:
            # 读末字节失败按常规 append 处理（不阻断）
            needs_leading_newline = False

    with path.open("a", encoding="utf-8") as fh:
        if needs_leading_newline:
            fh.write("\n")
        fh.write(payload)
        fh.flush()
        # os.fsync 仅在 FileIO 公开的 fileno() 上调用；
        # TextIOWrapper 在 CPython 上也透传 fileno
        try:
            import os  # 局部 import 保持模块导入轻

            os.fsync(fh.fileno())
        except (OSError, AttributeError):
            # 部分文件系统 / 平台 fsync 失败不阻断写入
            logger.debug("audit.append fsync 跳过：path=%s", path)


__all__ = [
    "AUDIT_ACTIONS",
    "NOTE_MAX_CHARS",
    "AuditEntry",
    "AuditError",
    "append_audit",
    "read_audit_entries",
]
