"""`runs/<prescription_no>/` 落盘实现。

输入 / 输出 schema 与 spec.md「输出闭环」表严格对齐：
- `input.json` —— 入参处方（与 parser 输入一致；保留原始字段便于审计）
- `graph.json` —— 字段 Agent 图快照（由 coordinator 写入；本模块负责
  仅读取 + 路径暴露）
- `transcript.jsonl` —— append-only 事件流（agent_finish / state_change /
  finding_append / error 等）
- `findings.json` —— 全部 Agent 完成后的字段证据卡聚合
- `run.json` —— 处方级状态 + 汇总指标（status / counts / created_at /
  updated_at / agent_ids）

参考源（github_ref 代码证据）
- `strix/core/agents.py: _maybe_snapshot` 原子写（`tempfile` + `replace`）
- `strix/tools/agents_graph/tools.py: _render_completion_report` 报告 schema

设计要点
--------
- **原子写**：所有 JSON 文件写盘走 `tempfile.NamedTemporaryFile(delete=False,
    dir=run_dir)` + `Path.replace` —— 同一目录内 rename 是原子操作，
    进程崩 / 写盘中断不会留半截文件
- **append-only transcript**：jsonl 文件只追加；先读最后非空行 → seek 末尾
    → 追加；不在中途 truncate
- **schema 严格**：run.json / findings.json / transcript.jsonl 每条事件
    都有 dataclass 校验；非法 family / severity / state 由 dataclass
    `__post_init__` 抛错，避免落盘坏数据
- **错误隔离**：单文件写盘失败只 logger.exception；不阻断其他落盘
  （与协调器 _maybe_snapshot 同款容错）
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from rxchk.models.prescription import (
    Finding,
    Prescription,
    UNCOVERED_FAMILY,
)
from rxchk.report.state import (
    RunState,
    RunStateError,
    assert_transition,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 文件名常量（与 spec.md 输出闭环表对齐）                                       #
# --------------------------------------------------------------------------- #


INPUT_FILENAME: str = "input.json"
GRAPH_FILENAME: str = "graph.json"
TRANSCRIPT_FILENAME: str = "transcript.jsonl"
FINDINGS_FILENAME: str = "findings.json"
RUN_FILENAME: str = "run.json"
AUDIT_FILENAME: str = "audit.jsonl"
WRITEBACK_FILENAME: str = "writeback.json"


# --------------------------------------------------------------------------- #
# 异常                                                                        #
# --------------------------------------------------------------------------- #


class ReportWriterError(RuntimeError):
    """ReportWriter 写盘 / 迁移 / 序列化错误。"""


# --------------------------------------------------------------------------- #
# Transcript 事件 schema                                                       #
# --------------------------------------------------------------------------- #


# 事件类型常量（transcript.jsonl 每行的 `event` 字段值）
EVENT_AGENT_FINISH: str = "agent_finish"
EVENT_STATE_CHANGE: str = "state_change"
EVENT_FINDING_APPEND: str = "finding_append"
EVENT_ERROR: str = "error"
EVENT_INPUT_RECEIVED: str = "input_received"
EVENT_BUDGET_WARNING: str = "budget_warning"


@dataclass
class TranscriptEvent:
    """`transcript.jsonl` 单条事件。

    字段：
    - `event`：事件类型（`agent_finish` / `state_change` /
      `finding_append` / `error` / `input_received`）
    - `ts`：事件时间（ISO 8601 UTC，秒级）
    - `family`：相关字段族（仅部分事件含；如 `agent_finish`）
    - `agent_id`：相关节点 id（如适用）
    - `payload`：事件额外负载（dict），由调用方按 event 类型约定
    """

    event: str
    ts: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    family: Optional[str] = None
    agent_id: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event or not isinstance(self.event, str):
            raise ValueError("TranscriptEvent.event must be a non-empty string")
        if not isinstance(self.ts, str) or not self.ts:
            raise ValueError("TranscriptEvent.ts must be a non-empty string")
        if self.family is not None and not isinstance(self.family, str):
            raise ValueError("TranscriptEvent.family must be a str or None")
        if self.agent_id is not None and not isinstance(self.agent_id, str):
            raise ValueError("TranscriptEvent.agent_id must be a str or None")
        if not isinstance(self.payload, dict):
            raise ValueError("TranscriptEvent.payload must be a dict")

    def to_json(self) -> str:
        """序列化为 JSON 行（`ensure_ascii=False` 保中文）。"""
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# ReportWriter                                                                #
# --------------------------------------------------------------------------- #


class ReportWriter:
    """一张处方核对产物的落盘句柄。

    用法：
        writer = ReportWriter(rx_no="RX-2026-<EXAMPLE>", run_root=Path("runs"))
        writer.write_input(prescription)
        writer.transition(RunState.PARSING)
        writer.append_transcript(TranscriptEvent(event=EVENT_INPUT_RECEIVED, ...))
        ...
        writer.write_findings(all_findings)
        writer.write_run(status=RunState.AWAITING_REVIEW)

    - run_dir 在首次写入时按需创建
    - 同一 writer 实例对应一张处方；多张处方请各 new 一个
    - 写盘失败抛 `ReportWriterError`，但单个文件失败不污染其他文件
      （每次写盘独立 try/except + logger.exception）
    """

    def __init__(
        self,
        *,
        rx_no: str,
        run_root: Path | str = Path("runs"),
    ) -> None:
        if not rx_no or not isinstance(rx_no, str):
            raise ReportWriterError("ReportWriter.rx_no must be a non-empty string")
        # 处方号作为目录名，过滤路径分隔符 / 控制字符；保留中文 / <EXAMPLE_*>
        safe_rx_no = _safe_filename(rx_no)
        self.rx_no: str = rx_no
        self.run_root: Path = Path(run_root)
        self.run_dir: Path = self.run_root / safe_rx_no
        self._created: bool = False

    # ------------------------------------------------------------------ #
    # 目录管理                                                             #
    # ------------------------------------------------------------------ #

    def ensure_run_dir(self) -> Path:
        """确保 `runs/<rx_no>/` 存在；返回 run_dir 路径。"""
        if not self._created:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self._created = True
        return self.run_dir

    # ------------------------------------------------------------------ #
    # input.json                                                          #
    # ------------------------------------------------------------------ #

    def write_input(self, prescription: Prescription) -> Path:
        """写 `input.json`（保留入参原始字段，便于审计）。

        返回写入文件路径。
        """
        run_dir = self.ensure_run_dir()
        payload = _prescription_to_input_payload(prescription)
        target = run_dir / INPUT_FILENAME
        _atomic_write_json(target, payload)
        logger.info("report.write_input rx=%s path=%s", self.rx_no, target)
        return target

    # ------------------------------------------------------------------ #
    # transcript.jsonl（append-only）                                     #
    # ------------------------------------------------------------------ #

    def append_transcript(self, event: TranscriptEvent) -> Path:
        """追加一条 transcript 事件。

        - 不 truncate；先 seek 到文件末尾再 write
        - 行尾追加 `\\n`，便于 `readlines()` 解析
        - 同一 writer 的并发追加不安全（调用方需串行化）
        """
        if not isinstance(event, TranscriptEvent):
            raise ReportWriterError(
                f"append_transcript requires TranscriptEvent, got {type(event).__name__}"
            )
        run_dir = self.ensure_run_dir()
        target = run_dir / TRANSCRIPT_FILENAME
        try:
            # append 模式打开；不存在则创建
            with target.open("a", encoding="utf-8") as fh:
                fh.write(event.to_json())
                fh.write("\n")
        except OSError as exc:
            logger.exception(
                "report.append_transcript failed rx=%s path=%s", self.rx_no, target
            )
            raise ReportWriterError(
                f"append_transcript failed: {exc}"
            ) from exc
        logger.debug(
            "report.append_transcript rx=%s event=%s", self.rx_no, event.event
        )
        return target

    def append_events(self, events: Iterable[TranscriptEvent]) -> int:
        """批量追加多条 transcript 事件；返回写入条数。"""
        count = 0
        for ev in events:
            self.append_transcript(ev)
            count += 1
        return count

    # ------------------------------------------------------------------ #
    # findings.json + run.json                                            #
    # ------------------------------------------------------------------ #

    def write_findings(self, findings: Iterable[Finding]) -> Path:
        """写 `findings.json`（按 spec.md 输出 schema 聚合所有字段证据卡）。

        返回写入文件路径。
        """
        run_dir = self.ensure_run_dir()
        items = [f.to_dict() for f in findings]
        payload: dict[str, Any] = {
            "prescription_no": self.rx_no,
            "count": len(items),
            "by_family": _count_by_family(items),
            "findings": items,
        }
        target = run_dir / FINDINGS_FILENAME
        _atomic_write_json(target, payload)
        logger.info(
            "report.write_findings rx=%s count=%d path=%s",
            self.rx_no,
            len(items),
            target,
        )
        return target

    def write_run(
        self,
        *,
        status: RunState | str,
        graph_path: Optional[Path | str] = None,
        agent_ids: Optional[Iterable[str]] = None,
        findings_count: Optional[int] = None,
        error: Optional[str] = None,
    ) -> Path:
        """写 `run.json`（处方级状态 + 汇总指标）。

        - `status`：当前处方级状态（自动校验迁移合法性）
        - `graph_path`：可选 graph.json 路径（与 RUN_STATE_TRANSITIONS 表
          对齐，调用方负责 graph.json 的实际写入）
        - `agent_ids`：8 字段族 Agent id 列表（仅首次写入时持久化；
          后续覆盖）
        - `findings_count`：findings.json 中字段证据卡条数；调用方传入
          以便 run.json 单文件含汇总
        - `error`：异常路径时附带的错误信息

        行为约定：
        - 若 run.json 已存在，校验 `current_status → new_status` 合法迁移
        - 同状态幂等（status 不变）允许重写（用于追加汇总指标）
        - 终态后再写入抛 `RunStateError`
        - created_at 首次写入时固化，updated_at 每次重写更新
        """
        run_dir = self.ensure_run_dir()
        target = run_dir / RUN_FILENAME

        new_state = _coerce_run_state(status)
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.exception(
                    "report.write_run read existing failed rx=%s path=%s",
                    self.rx_no,
                    target,
                )
                raise ReportWriterError(
                    f"write_run: cannot read existing run.json: {exc}"
                ) from exc
            current_state = _coerce_run_state(existing.get("status", RunState.UPLOADED.value))
            assert_transition(current_state, new_state)
            payload = dict(existing)  # 保留旧字段
            payload["status"] = new_state.value
            payload["updated_at"] = now_iso
            if findings_count is not None:
                payload["findings_count"] = findings_count
            if graph_path is not None:
                payload["graph"] = str(graph_path)
            if agent_ids is not None:
                payload["agent_ids"] = list(agent_ids)
            if error is not None:
                payload["error"] = error
        else:
            payload = {
                "prescription_no": self.rx_no,
                "status": new_state.value,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            if findings_count is not None:
                payload["findings_count"] = findings_count
            if graph_path is not None:
                payload["graph"] = str(graph_path)
            if agent_ids is not None:
                payload["agent_ids"] = list(agent_ids)
            if error is not None:
                payload["error"] = error

        _atomic_write_json(target, payload)
        logger.info(
            "report.write_run rx=%s status=%s path=%s",
            self.rx_no,
            new_state.value,
            target,
        )
        return target

    # ------------------------------------------------------------------ #
    # 便捷：处方级状态迁移 + transcript 自动追加                            #
    # ------------------------------------------------------------------ #

    def transition(
        self,
        new_state: RunState | str,
        *,
        actor: str = "system",
        reason: Optional[str] = None,
    ) -> Path:
        """推进处方级状态；自动追加一条 `state_change` transcript 事件。

        - 非法迁移抛 `RunStateError`
        - 终态后再迁移抛 `RunStateError`
        - 幂等迁移（同状态）仍追加 transcript 事件，但 run.json 内容
          不变（updated_at 也更新）
        """
        run_dir = self.ensure_run_dir()
        target = run_dir / RUN_FILENAME

        new_state_r = _coerce_run_state(new_state)
        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.exception(
                    "report.transition read existing failed rx=%s path=%s",
                    self.rx_no,
                    target,
                )
                raise ReportWriterError(
                    f"transition: cannot read existing run.json: {exc}"
                ) from exc
            current_state = _coerce_run_state(
                existing.get("status", RunState.UPLOADED.value)
            )
            assert_transition(current_state, new_state_r)

        # 追加 transcript 事件
        payload: dict[str, Any] = {"actor": actor}
        if reason is not None:
            payload["reason"] = reason
        self.append_transcript(
            TranscriptEvent(
                event=EVENT_STATE_CHANGE,
                payload=payload,
            )
        )
        # 推进 run.json
        return self.write_run(status=new_state_r)

    # ------------------------------------------------------------------ #
    # 读取辅助（供 viewer / audit 模块复用）                                #
    # ------------------------------------------------------------------ #

    def read_findings(self) -> list[dict[str, Any]]:
        """读取 `findings.json`；不存在返回空列表。"""
        target = self.run_dir / FINDINGS_FILENAME
        if not target.exists():
            return []
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.exception(
                "report.read_findings failed rx=%s path=%s", self.rx_no, target
            )
            raise ReportWriterError(
                f"read_findings failed: {exc}"
            ) from exc
        items = data.get("findings", [])
        if not isinstance(items, list):
            raise ReportWriterError(
                f"findings.json malformed: 'findings' must be a list"
            )
        return items

    def read_transcript(self) -> list[dict[str, Any]]:
        """读取 `transcript.jsonl`；不存在返回空列表。损坏行不阻止解析（skip）。"""
        target = self.run_dir / TRANSCRIPT_FILENAME
        if not target.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            with target.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        events.append(json.loads(raw))
                    except json.JSONDecodeError:
                        logger.warning(
                            "report.read_transcript skip corrupted line rx=%s line=%r",
                            self.rx_no,
                            raw[:80],
                        )
                        continue
        except OSError as exc:
            logger.exception(
                "report.read_transcript failed rx=%s path=%s", self.rx_no, target
            )
            raise ReportWriterError(
                f"read_transcript failed: {exc}"
            ) from exc
        return events

    def read_run(self) -> dict[str, Any]:
        """读取 `run.json`；不存在返回空 dict。"""
        target = self.run_dir / RUN_FILENAME
        if not target.exists():
            return {}
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.exception(
                "report.read_run failed rx=%s path=%s", self.rx_no, target
            )
            raise ReportWriterError(
                f"read_run failed: {exc}"
            ) from exc


# --------------------------------------------------------------------------- #
# 内部辅助                                                                    #
# --------------------------------------------------------------------------- #


def _safe_filename(name: str) -> str:
    """处方号 → 目录名。

    过滤路径分隔符（/ \\）与控制字符；保留中文 / `<EXAMPLE_*>` /
    ASCII 字母数字 / 下划线 / 短横线。
    """
    if not name or not isinstance(name, str):
        raise ReportWriterError("rx_no must be a non-empty string")
    cleaned_chars: list[str] = []
    for ch in name:
        if ch in ("/", "\\") or ord(ch) < 0x20:
            raise ReportWriterError(
                f"rx_no contains illegal character: {ch!r}"
            )
        cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars).strip()
    if not cleaned:
        raise ReportWriterError("rx_no cleaned to empty")
    return cleaned


def _atomic_write_json(target: Path, payload: Mapping[str, Any]) -> None:
    """原子写 JSON：tmp + rename。

    - tmp 与 target 同目录，确保 `Path.replace` 是原子 rename
    - 显式关闭 tmp 后再 rename，避免 Windows 上句柄占用
    - 任何 OSError 都包装为 ReportWriterError
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # NamedTemporaryFile(delete=False) → 显式 unlink 失败时的残留
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(
                    dict(payload),
                    fh,
                    ensure_ascii=False,
                    indent=2,
                    separators=(",", ": "),
                )
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            # 写盘失败清理 tmp，避免残留
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        Path(tmp_path).replace(target)
    except OSError as exc:
        logger.exception(
            "report.atomic_write_json failed path=%s err=%s", target, exc
        )
        raise ReportWriterError(
            f"atomic_write_json failed for {target}: {exc}"
        ) from exc


def _coerce_run_state(value: RunState | str) -> RunState:
    """`RunState` / 字符串 → `RunState`；非法字符串抛 `RunStateError`。"""
    if isinstance(value, RunState):
        return value
    if isinstance(value, str):
        try:
            return RunState(value)
        except ValueError as exc:
            raise RunStateError(
                f"unknown run state: {value!r}; "
                f"expected one of {[s.value for s in RunState]}"
            ) from exc
    raise RunStateError(
        f"run state must be RunState or str, got {type(value).__name__}"
    )


def _count_by_family(items: list[dict[str, Any]]) -> dict[str, int]:
    """findings 列表按 family 分组计数（含 uncovered 兜底）。"""
    counts: dict[str, int] = {}
    for it in items:
        fam = it.get("family")
        if not isinstance(fam, str) or not fam:
            fam = UNCOVERED_FAMILY
        counts[fam] = counts.get(fam, 0) + 1
    return counts


def _prescription_to_input_payload(prescription: Prescription) -> dict[str, Any]:
    """`Prescription` → `input.json` 序列化 dict。"""
    return {
        "prescription_no": prescription.prescription_no,
        "patient_id": prescription.patient_id,
        "visit_no": prescription.visit_no,
        "doctor_id": prescription.doctor_id,
        "items": [
            {
                "drug_code": item.drug_code,
                "dose": item.dose,
                "frequency": item.frequency,
                "route": item.route,
                **(
                    {"duration_days": item.duration_days}
                    if item.duration_days is not None
                    else {}
                ),
            }
            for item in prescription.items
        ],
        "diagnoses": [
            {"code": d.code, "name": d.name} for d in prescription.diagnoses
        ],
        "allergies": list(prescription.allergies),
    }


__all__ = [
    "ReportWriter",
    "ReportWriterError",
    "TranscriptEvent",
    # 文件名常量
    "INPUT_FILENAME",
    "GRAPH_FILENAME",
    "TRANSCRIPT_FILENAME",
    "FINDINGS_FILENAME",
    "RUN_FILENAME",
    "AUDIT_FILENAME",
    "WRITEBACK_FILENAME",
    # 事件类型常量
    "EVENT_AGENT_FINISH",
    "EVENT_STATE_CHANGE",
    "EVENT_FINDING_APPEND",
    "EVENT_ERROR",
    "EVENT_INPUT_RECEIVED",
    "EVENT_BUDGET_WARNING",
]