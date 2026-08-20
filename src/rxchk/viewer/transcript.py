"""直读 `runs/<rx_no>/` 磁盘产物，供 Viewer JSON 端点复用。

参考源（github_ref 代码证据）
- `strix/viewer/transcript.py: read_run_summary / read_vulnerabilities`
  —— 每次请求现读磁盘、损坏文件降级为空而不是 500

设计要点
--------
- **现读不缓存**：核对进行中的处方和已归档的处方走同一条读路径，
  前端 ~1s 轮询即可看到增量（与源仓库 Viewer 同款取舍：JSON 轮询
  而非 SSE，服务端不留连接状态）
- **降级而非抛错**：文件缺失 → 空结构；单行 jsonl 损坏 → 跳过该行并
  记 warning。药师工作站不该因为一行坏日志白屏
- **只读**：本模块不写盘；写盘走 `rxchk.report.writer` / `audit.jsonl`
  追加，保持 append-only 语义单一入口
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from rxchk.report.writer import (
    AUDIT_FILENAME,
    FINDINGS_FILENAME,
    INPUT_FILENAME,
    RUN_FILENAME,
    TRANSCRIPT_FILENAME,
    WRITEBACK_FILENAME,
)

logger = logging.getLogger(__name__)


def _read_json(path: Path, default: Any) -> Any:
    """读单个 JSON 文件；缺失或损坏都降级为 `default`。"""
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("viewer 读 JSON 失败，降级为空：%s", path)
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读 append-only jsonl；损坏行跳过，不影响其余行。"""
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("viewer 跳过损坏 jsonl 行：%s", path)
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        logger.warning("viewer 读 jsonl 失败：%s", path)
        return []
    return rows


def run_record_path(run_dir: Path) -> Path:
    """一个目录是否算「一次核对」的判定文件（对应源仓库 run record）。"""
    return run_dir / RUN_FILENAME


def is_run_dir(candidate: Path) -> bool:
    """只有落过 `run.json` 的目录才被 Viewer 认作一次处方核对。"""
    return candidate.is_dir() and run_record_path(candidate).is_file()


def read_run_summary(run_dir: Path) -> dict[str, Any]:
    """读 `run.json`；缺失返回带目录名兜底的最小结构。"""
    record = _read_json(run_record_path(run_dir), {})
    if not isinstance(record, dict):
        record = {}
    record.setdefault("prescription_no", run_dir.name)
    record.setdefault("status", "uploaded")
    return record


def read_findings(run_dir: Path) -> dict[str, Any]:
    """读 `findings.json`；缺失返回 `count=0` 的空网格（前端可直接渲染）。"""
    payload = _read_json(run_dir / FINDINGS_FILENAME, None)
    if not isinstance(payload, dict):
        return {
            "prescription_no": run_dir.name,
            "count": 0,
            "by_family": {},
            "findings": [],
        }
    items = payload.get("findings")
    if not isinstance(items, list):
        payload["findings"] = []
    payload.setdefault("prescription_no", run_dir.name)
    payload.setdefault("count", len(payload["findings"]))
    payload.setdefault("by_family", {})
    return payload


def read_transcript(run_dir: Path) -> list[dict[str, Any]]:
    """读 `transcript.jsonl` 全部事件（按写入顺序）。"""
    return _read_jsonl(run_dir / TRANSCRIPT_FILENAME)


def read_audit(run_dir: Path) -> list[dict[str, Any]]:
    """读 `audit.jsonl` 药师操作日志（按写入顺序）。"""
    return _read_jsonl(run_dir / AUDIT_FILENAME)


def read_writeback(run_dir: Path) -> dict[str, Any]:
    """读 `writeback.json` 回写回执；未回写返回空 dict。"""
    payload = _read_json(run_dir / WRITEBACK_FILENAME, {})
    return payload if isinstance(payload, dict) else {}


def read_input(run_dir: Path) -> dict[str, Any]:
    """读 `input.json` 入参处方；缺失返回空 dict。"""
    payload = _read_json(run_dir / INPUT_FILENAME, {})
    return payload if isinstance(payload, dict) else {}


def run_list_entry(run_dir: Path) -> dict[str, Any]:
    """一次处方核对在历史列表中的紧凑摘要。

    对应源仓库 `run_list_entry`：列表页只给足够药师挑选的字段
    （处方号 / 状态 / 命中数 / 按族计数 / 是否已确认），明细走 detail 端点。
    """
    record = read_run_summary(run_dir)
    findings = read_findings(run_dir)
    audit_rows = read_audit(run_dir)
    return {
        "prescription_no": record.get("prescription_no") or run_dir.name,
        "run_name": run_dir.name,
        "status": record.get("status"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "findings_count": int(findings.get("count") or 0),
        "by_family": findings.get("by_family") or {},
        "audit_count": len(audit_rows),
        "written_back": bool(read_writeback(run_dir)),
    }


def iter_run_dirs(base_dir: Path) -> list[Path]:
    """`base_dir` 下所有处方核对目录，按 `run.json` mtime 倒序（新的在前）。"""
    if not base_dir.is_dir():
        return []
    run_dirs = [child for child in base_dir.iterdir() if is_run_dir(child)]
    run_dirs.sort(
        key=lambda child: run_record_path(child).stat().st_mtime, reverse=True
    )
    return run_dirs


__all__ = [
    "is_run_dir",
    "iter_run_dirs",
    "read_audit",
    "read_findings",
    "read_input",
    "read_run_summary",
    "read_transcript",
    "read_writeback",
    "run_list_entry",
    "run_record_path",
]
