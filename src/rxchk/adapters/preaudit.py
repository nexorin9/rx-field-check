"""前置审方工单适配器：抽象接口 + 替身实现。

设计目标
--------
- **抽象 Protocol 是正式对接契约**：院内前置审方系统（合理用药 PASS /
  自研审方引擎 / 第三方审方）通过同一方法接入；本轮替身只 append 到
  `runs/<rx_no>/writeback.json` 的 `preaudit_ticket` 字段，**明确不是
  最终对接面**。
- **替身实现显式标注 `[FAKE]`**：与 HIS 替身一致，便于审计员识别。
- **失败不阻断 HIS 回写**：前置审方工单创建失败仅写 `preaudit_error` 字段，
  HIS 备注照常落 —— 审计员可在飞行检查时分辨「工单待补」与「HIS 待补」。

融合后的产品路径
----------------
1. 药师在 Viewer 点确认 → HIS 回写（`his.writeback_with_retry`）→ 成功
2. HIS 回执落 `runs/<rx>/writeback.json`
3. `PreauditAdapter.create_ticket(...)` 把审方工单创建结果合并写入
   同一 `writeback.json` 的 `preaudit_ticket` 字段（成功）或
   `preaudit_error` 字段（失败）
4. Viewer / 飞行检查脚本读 `writeback.json` 同时看 HIS + 前置审方两侧状态

源 repo 名 / slug 不出现在产品代码或字符串字面值。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 自定义异常                                                                  #
# --------------------------------------------------------------------------- #


class PreauditAdapterError(RuntimeError):
    """前置审方适配器调用 / 配置错误。"""


# --------------------------------------------------------------------------- #
# 数据结构：工单请求 + 回执                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PreauditTicket:
    """前置审方工单请求体（按 spec.md「正式对接层」要求）。"""

    prescription_no: str
    findings_summary: str
    severity: str  # 取值 high / medium / low / info
    rule_version: str = ""
    operator: str = ""
    note: str = ""


@dataclass(frozen=True)
class PreauditTicketReceipt:
    """前置审方工单创建回执。"""

    prescription_no: str
    ok: bool
    ticket_id: str
    message: str
    endpoint: str
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )


# --------------------------------------------------------------------------- #
# 抽象接口                                                                    #
# --------------------------------------------------------------------------- #


@runtime_checkable
class PreauditAdapterProtocol(Protocol):
    """前置审方工单适配器抽象接口（正式对接契约）。"""

    def create_ticket(
        self, payload: PreauditTicket
    ) -> PreauditTicketReceipt: ...

    def get_endpoint(self) -> str: ...


# --------------------------------------------------------------------------- #
# 替身实现                                                                    #
# --------------------------------------------------------------------------- #


class FakePreauditAdapter:
    """前置审方工单替身 —— 同步把回执 merge 到 `runs/<rx_no>/writeback.json`。

    实现要点
    --------
    - 显式日志前缀 `[FAKE]`，便于审计员识别替身运行
    - 默认读 / 写 `runs/<rx_no>/writeback.json`（由 `his.writeback_with_retry`
      写入）；`fail_times` 控制连续失败次数（测试用）
    - 同步原子写（tmp + rename）—— 真实前置审方接入走院内 REST / 中间表
    """

    ENDPOINT_LABEL: str = "local-file-preaudit"

    def __init__(
        self,
        *,
        run_dir: Path | str,
        fail_times: int = 0,
    ) -> None:
        if not run_dir:
            raise PreauditAdapterError(
                "FakePreauditAdapter.run_dir must not be empty"
            )
        self.run_dir: Path = Path(run_dir)
        self.fail_times: int = max(0, int(fail_times))
        self.attempts_log: list[int] = []
        logger.info(
            "[FAKE] PreauditAdapter initialized run_dir=%s endpoint=%s",
            self.run_dir,
            self.ENDPOINT_LABEL,
        )

    def get_endpoint(self) -> str:
        return self.ENDPOINT_LABEL

    def create_ticket(self, payload: PreauditTicket) -> PreauditTicketReceipt:
        """同步把工单回执 merge 到 `writeback.json`；按 `fail_times` 模拟连续失败。"""
        if not isinstance(payload, PreauditTicket):
            raise PreauditAdapterError(
                f"create_ticket requires PreauditTicket, "
                f"got {type(payload).__name__}"
            )
        if not payload.prescription_no or not isinstance(payload.prescription_no, str):
            raise PreauditAdapterError(
                "create_ticket.prescription_no must be a non-empty string"
            )

        self.attempts_log.append(1)
        attempt_no = len(self.attempts_log)
        if attempt_no <= self.fail_times:
            logger.warning(
                "[FAKE] PreauditAdapter.create_ticket 模拟失败 rx=%s attempt=%d/%d",
                payload.prescription_no,
                attempt_no,
                self.fail_times,
            )
            raise PreauditAdapterError(
                f"[FAKE] simulated preaudit failure attempt={attempt_no}"
            )

        receipt = _merge_preaudit_into_writeback(
            run_dir=self.run_dir,
            payload=payload,
            endpoint=self.ENDPOINT_LABEL,
        )
        logger.info(
            "[FAKE] PreauditAdapter.create_ticket ok rx=%s ticket=%s endpoint=%s",
            payload.prescription_no,
            receipt.ticket_id,
            self.ENDPOINT_LABEL,
        )
        return receipt


# --------------------------------------------------------------------------- #
# 内部：合并工单回执到 writeback.json                                          #
# --------------------------------------------------------------------------- #


def _merge_preaudit_into_writeback(
    *,
    run_dir: Path,
    payload: PreauditTicket,
    endpoint: str,
) -> PreauditTicketReceipt:
    """把工单回执原子合并到 `writeback.json`。

    - 若 `writeback.json` 不存在（HIS 还没回写就跑工单创建）：创建空骨架
      并写 `preaudit_error="his not written back yet"`，不阻断工单创建
    - 已有 `writeback.json`：在 `preaudit_ticket` 字段加一条工单回执，
      `preaudit_error` 字段清空（保持「最新一次状态」语义）

    文件 schema 合并示例：
    {
      ...已有 his 字段...,
      "preaudit_ticket": {
        "prescription_no": "RX-...",
        "ticket_id": "T-...",
        "ok": true,
        "message": "...",
        "endpoint": "...",
        "created_at": "..."
      },
      "preaudit_error": null
    }
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "writeback.json"

    # ticket_id 由处方号 + 时间戳派生（替身场景足够；真实端点由前置审方回执）
    ticket_id = (
        f"T-{payload.prescription_no}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    )
    receipt = PreauditTicketReceipt(
        prescription_no=payload.prescription_no,
        ok=True,
        ticket_id=ticket_id,
        message="preaudit ticket created",
        endpoint=endpoint,
    )

    # 读现有 writeback.json（HIS 写的那份）
    if target.is_file():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "preaudit.merge 既有 writeback.json 损坏：%s；按空对象处理",
                target,
            )
            existing = {}
    else:
        existing = {
            "prescription_no": payload.prescription_no,
            "his_pending": True,
        }

    existing["prescription_no"] = payload.prescription_no
    existing["preaudit_ticket"] = {
        "prescription_no": receipt.prescription_no,
        "ticket_id": receipt.ticket_id,
        "ok": receipt.ok,
        "message": receipt.message,
        "endpoint": receipt.endpoint,
        "severity": payload.severity,
        "findings_summary": payload.findings_summary,
        "rule_version": payload.rule_version,
        "operator": payload.operator,
        "created_at": receipt.created_at,
    }
    existing["preaudit_error"] = None

    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".writeback.",
            suffix=".tmp",
            dir=str(run_dir),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(existing, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        Path(tmp_path).replace(target)
    except OSError as exc:
        logger.exception(
            "preaudit.merge atomic_write failed rx=%s path=%s err=%s",
            payload.prescription_no,
            target,
            exc,
        )
        raise PreauditAdapterError(
            f"preaudit merge atomic_write failed for {target}: {exc}"
        ) from exc
    return receipt


__all__ = [
    "FakePreauditAdapter",
    "PreauditAdapterError",
    "PreauditAdapterProtocol",
    "PreauditTicket",
    "PreauditTicketReceipt",
]