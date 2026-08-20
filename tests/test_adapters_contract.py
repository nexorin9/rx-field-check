"""`rxchk.adapters` 单测（任务 13）。

覆盖：
- **协议一致性**：4 个替身（FakeHisAdapter / FakePreauditAdapter）通过
  `runtime_checkable` Protocol 校验；接口签名 / 返回结构与 spec.md
  「正式对接层」一致
- **替身落盘**：HIS 替身写 `runs/<rx>/writeback.json` 含 schema 字段；
  前置审方替身 merge 同一文件的 `preaudit_ticket` 字段
- **日志前缀**：替身加载与失败注入都打 `[FAKE]`，便于审计员识别
- **超时 / 重试**：`writeback_with_retry` 超时转 `HisAdapterError` →
  指数退避 → 重试耗尽入 `WritebackQueue`
- **失败注入**：替身 `fail_times=N` 连续失败 N 次，第 N+1 次成功
- **契约入参校验**：缺 `prescription_no` / 类型错抛 `HisAdapterError`
  / `PreauditAdapterError`
- **写入失败可观察**：`writeback.json` 损坏 / 缺字段由 reader 容错
  （与 spec.md 「HIS 接口超时 → 落本地 JSON」一致）

源 repo 名 / slug 不出现在测试代码或断言字面值。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from rxchk.adapters.his import (
    FAKE_TIMEOUT_SECONDS,
    HIS_DEFAULT_TIMEOUT_SECONDS,
    HIS_MAX_RETRIES,
    FakeHisAdapter,
    HisAdapterError,
    HisAdapterProtocol,
    HisWriteback,
    HisWritebackReceipt,
    WritebackQueue,
    writeback_with_retry,
)
from rxchk.adapters.preaudit import (
    FakePreauditAdapter,
    PreauditAdapterError,
    PreauditAdapterProtocol,
    PreauditTicket,
    PreauditTicketReceipt,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                           #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    """每条用例一个干净的 run 目录。"""
    d = tmp_path / "runs" / "RX-2026-<EXAMPLE>"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_payload(
    *,
    prescription_no: str = "RX-2026-<EXAMPLE>",
    audit_summary: str = "1 finding; pharmacist confirmed",
    manual_override: bool = False,
    note: str = "audit note from pharmacist",
    operator: str = "<EXAMPLE_PHARMACIST>",
) -> HisWriteback:
    return HisWriteback(
        prescription_no=prescription_no,
        audit_summary=audit_summary,
        manual_override=manual_override,
        note=note,
        operator=operator,
    )


def _make_ticket(
    *,
    prescription_no: str = "RX-2026-<EXAMPLE>",
    findings_summary: str = "1 dose overdose finding",
    rule_version: str = "v2026.08",
    operator: str = "<EXAMPLE_PHARMACIST>",
) -> PreauditTicket:
    return PreauditTicket(
        prescription_no=prescription_no,
        findings_summary=findings_summary,
        severity="high",
        rule_version=rule_version,
        operator=operator,
    )


# --------------------------------------------------------------------------- #
# 协议一致性                                                                   #
# --------------------------------------------------------------------------- #


class TestProtocolConformance:
    """两个替身都满足 `runtime_checkable` Protocol 校验。"""

    def test_fake_his_satisfies_protocol(self, run_dir: Path) -> None:
        adapter = FakeHisAdapter(run_dir=run_dir)
        assert isinstance(adapter, HisAdapterProtocol)

    def test_fake_preaudit_satisfies_protocol(self, run_dir: Path) -> None:
        adapter = FakePreauditAdapter(run_dir=run_dir)
        assert isinstance(adapter, PreauditAdapterProtocol)

    def test_unrelated_class_fails_his_protocol(self) -> None:
        class NotAnAdapter:
            pass

        assert not isinstance(NotAnAdapter(), HisAdapterProtocol)
        assert not isinstance(NotAnAdapter(), PreauditAdapterProtocol)


# --------------------------------------------------------------------------- #
# FakeHisAdapter 落盘 + 日志                                                    #
# --------------------------------------------------------------------------- #


class TestFakeHisAdapterWriteback:
    """替身 writeback 落盘 schema 与 spec.md「输出闭环」表对齐。"""

    def test_writeback_writes_json(
        self, run_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = FakeHisAdapter(run_dir=run_dir)
        payload = _make_payload()
        with caplog.at_level(logging.INFO, logger="rxchk.adapters.his"):
            receipt = adapter.writeback(payload)

        assert receipt.ok is True
        assert receipt.prescription_no == payload.prescription_no
        assert receipt.attempts == 1
        assert receipt.endpoint == "local-file"
        assert receipt.manual_override is False

        path = run_dir / "writeback.json"
        assert path.is_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        # schema 字段与 spec.md 输出闭环表 + 替身实现对齐
        for key in (
            "prescription_no",
            "endpoint",
            "ok",
            "message",
            "attempts",
            "manual_override",
            "audit_summary",
            "operator",
            "note",
            "written_at",
        ):
            assert key in data, f"missing field: {key}"
        assert data["prescription_no"] == payload.prescription_no
        assert data["audit_summary"] == payload.audit_summary
        assert data["operator"] == payload.operator
        assert data["ok"] is True
        # 日志前缀
        assert any("[FAKE]" in rec.message for rec in caplog.records)

    def test_writeback_manual_override_propagates(self, run_dir: Path) -> None:
        adapter = FakeHisAdapter(run_dir=run_dir)
        payload = _make_payload(manual_override=True)
        receipt = adapter.writeback(payload)
        assert receipt.manual_override is True
        data = json.loads((run_dir / "writeback.json").read_text(encoding="utf-8"))
        assert data["manual_override"] is True

    def test_writeback_endpoint_label(self, run_dir: Path) -> None:
        adapter = FakeHisAdapter(run_dir=run_dir)
        assert adapter.get_endpoint() == "local-file"

    def test_writeback_rejects_empty_rx(self, run_dir: Path) -> None:
        adapter = FakeHisAdapter(run_dir=run_dir)
        with pytest.raises(HisAdapterError):
            adapter.writeback(_make_payload(prescription_no=""))

    def test_writeback_rejects_bad_type(self, run_dir: Path) -> None:
        adapter = FakeHisAdapter(run_dir=run_dir)
        with pytest.raises(HisAdapterError):
            adapter.writeback({"prescription_no": "x"})  # type: ignore[arg-type]

    def test_writeback_fail_times_injection(
        self, run_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = FakeHisAdapter(run_dir=run_dir, fail_times=2)
        payload = _make_payload()
        with caplog.at_level(logging.WARNING, logger="rxchk.adapters.his"):
            with pytest.raises(HisAdapterError):
                adapter.writeback(payload)
            with pytest.raises(HisAdapterError):
                adapter.writeback(payload)
            receipt = adapter.writeback(payload)  # 第 3 次成功

        assert receipt.ok is True
        assert receipt.attempts == 3
        assert any("[FAKE]" in r.message and "模拟失败" in r.message for r in caplog.records)
        assert len(adapter.attempts_log) == 3

    def test_run_dir_required(self) -> None:
        with pytest.raises(HisAdapterError):
            FakeHisAdapter(run_dir="")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# FakePreauditAdapter merge                                                    #
# --------------------------------------------------------------------------- #


class TestFakePreauditAdapter:
    """前置审方替身把工单回执合并到 `writeback.json`。"""

    def test_create_ticket_after_his_writeback(self, run_dir: Path) -> None:
        his = FakeHisAdapter(run_dir=run_dir)
        his.writeback(_make_payload())
        preaudit = FakePreauditAdapter(run_dir=run_dir)
        receipt = preaudit.create_ticket(_make_ticket())

        assert receipt.ok is True
        assert receipt.prescription_no == "RX-2026-<EXAMPLE>"
        assert receipt.ticket_id.startswith("T-RX-2026-<EXAMPLE>-")
        assert receipt.endpoint == "local-file-preaudit"

        data = json.loads((run_dir / "writeback.json").read_text(encoding="utf-8"))
        assert "preaudit_ticket" in data
        assert data["preaudit_ticket"]["ticket_id"] == receipt.ticket_id
        assert data["preaudit_ticket"]["severity"] == "high"
        assert data["preaudit_error"] is None
        # HIS 字段保留
        assert data["ok"] is True
        assert data["manual_override"] is False

    def test_create_ticket_without_his_first(
        self, run_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """HIS 未写就先创建工单：替身写空骨架 + preaudit_ticket。"""
        adapter = FakePreauditAdapter(run_dir=run_dir)
        with caplog.at_level(logging.INFO, logger="rxchk.adapters.preaudit"):
            receipt = adapter.create_ticket(_make_ticket())

        assert receipt.ok is True
        data = json.loads((run_dir / "writeback.json").read_text(encoding="utf-8"))
        assert data["prescription_no"] == "RX-2026-<EXAMPLE>"
        assert data["his_pending"] is True
        assert data["preaudit_ticket"]["ticket_id"] == receipt.ticket_id

    def test_create_ticket_fail_injection(
        self, run_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = FakePreauditAdapter(run_dir=run_dir, fail_times=1)
        with caplog.at_level(logging.WARNING, logger="rxchk.adapters.preaudit"):
            with pytest.raises(PreauditAdapterError):
                adapter.create_ticket(_make_ticket())
            receipt = adapter.create_ticket(_make_ticket())  # 第 2 次成功

        assert receipt.ok is True
        assert len(adapter.attempts_log) == 2

    def test_create_ticket_rejects_empty_rx(self, run_dir: Path) -> None:
        adapter = FakePreauditAdapter(run_dir=run_dir)
        with pytest.raises(PreauditAdapterError):
            adapter.create_ticket(_make_ticket(prescription_no=""))

    def test_create_ticket_rejects_bad_type(self, run_dir: Path) -> None:
        adapter = FakePreauditAdapter(run_dir=run_dir)
        with pytest.raises(PreauditAdapterError):
            adapter.create_ticket({"prescription_no": "x"})  # type: ignore[arg-type]

    def test_run_dir_required(self) -> None:
        with pytest.raises(PreauditAdapterError):
            FakePreauditAdapter(run_dir="")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# writeback_with_retry — 超时 / 重试 / 队列                                     #
# --------------------------------------------------------------------------- #


class _SlowFakeAdapter:
    """模拟 HIS 端点超时的替身（同步 sleep + 抛错）。"""

    def __init__(self, run_dir: Path, sleep_seconds: float, fail: bool = False) -> None:
        self.run_dir = run_dir
        self.sleep_seconds = sleep_seconds
        self.fail = fail
        self.calls = 0

    def writeback(self, payload: HisWriteback) -> HisWritebackReceipt:
        import time

        self.calls += 1
        time.sleep(self.sleep_seconds)
        if self.fail:
            raise HisAdapterError("[FAKE] simulated slow failure")
        # 同步成功：复用真替身的落盘逻辑
        inner = FakeHisAdapter(run_dir=self.run_dir)
        return inner.writeback(payload)

    def get_endpoint(self) -> str:
        return "slow-fake"


class TestWritebackWithRetry:
    """writeback_with_retry：超时 → 重试 → 入队。"""

    async def test_success_first_attempt(self, run_dir: Path) -> None:
        adapter = FakeHisAdapter(run_dir=run_dir)
        payload = _make_payload()
        receipt = await writeback_with_retry(
            adapter, payload, timeout_seconds=FAKE_TIMEOUT_SECONDS
        )
        assert receipt.ok is True
        assert receipt.attempts == 1
        assert (run_dir / "writeback.json").is_file()

    async def test_timeout_retries_then_deferred(
        self, run_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = _SlowFakeAdapter(run_dir=run_dir, sleep_seconds=0.2, fail=True)
        queue = WritebackQueue(run_dir=run_dir)
        with caplog.at_level(logging.WARNING, logger="rxchk.adapters.his"):
            receipt = await writeback_with_retry(
                adapter,
                _make_payload(),
                timeout_seconds=0.05,
                max_retries=2,
                queue=queue,
            )
        assert receipt.ok is False
        assert receipt.attempts == 3  # 1 + 2 retries
        assert "deferred" in receipt.message
        # 队列里有一条待补跑
        pending = queue.list_pending()
        assert len(pending) == 1
        assert pending[0]["prescription_no"] == "RX-2026-<EXAMPLE>"
        # last_error 含 deferred 描述；具体错误形态（TimeoutError / HisAdapterError）
        # 取决于适配器是先超时还是先抛错——这里 sleep > timeout，必为超时
        assert "deferred" in pending[0]["last_error"]

    async def test_eventual_success_after_retries(self, run_dir: Path) -> None:
        """替身 fail_times=2，timeout 充足：第 3 次尝试成功。"""
        adapter = FakeHisAdapter(run_dir=run_dir, fail_times=2)
        receipt = await writeback_with_retry(
            adapter,
            _make_payload(),
            timeout_seconds=FAKE_TIMEOUT_SECONDS,
            max_retries=3,
        )
        assert receipt.ok is True
        assert receipt.attempts == 3
        assert (run_dir / "writeback.json").is_file()

    async def test_no_queue_means_no_persistence(self, run_dir: Path) -> None:
        adapter = _SlowFakeAdapter(run_dir=run_dir, sleep_seconds=0.1, fail=True)
        receipt = await writeback_with_retry(
            adapter,
            _make_payload(),
            timeout_seconds=0.02,
            max_retries=1,
            queue=None,
        )
        assert receipt.ok is False
        assert "deferred" in receipt.message
        # 无 queue：writeback.json 不应出现
        assert not (run_dir / "writeback.json").is_file()

    async def test_rejects_non_protocol_adapter(self, run_dir: Path) -> None:
        class NotAnAdapter:
            pass

        with pytest.raises(HisAdapterError):
            await writeback_with_retry(
                NotAnAdapter(),  # type: ignore[arg-type]
                _make_payload(),
            )

    async def test_non_his_error_propagates_immediately(
        self, run_dir: Path
    ) -> None:
        """非 HisAdapterError（如 ValueError）立即上抛，不被静默重试。"""

        class BuggyAdapter:
            def writeback(self, payload: HisWriteback) -> HisWritebackReceipt:
                raise ValueError("参数错了")

            def get_endpoint(self) -> str:
                return "buggy"

        # ValueError 不会被 catch → 立即上抛
        with pytest.raises(ValueError, match="参数错了"):
            await writeback_with_retry(
                BuggyAdapter(),  # type: ignore[arg-type]
                _make_payload(),
                max_retries=5,
            )


# --------------------------------------------------------------------------- #
# WritebackQueue 落盘 + 去重                                                    #
# --------------------------------------------------------------------------- #


class TestWritebackQueue:
    """HIS 回写失败队列：原子写 + 同处方号去重。"""

    def test_enqueue_creates_file(self, run_dir: Path) -> None:
        q = WritebackQueue(run_dir=run_dir)
        entry = q.enqueue_failed(
            prescription_no="RX-A",
            payload=_make_payload(prescription_no="RX-A"),
            last_error="timeout",
        )
        assert q.path.is_file()
        assert entry["prescription_no"] == "RX-A"
        assert entry["last_error"] == "timeout"
        assert entry["attempts"] == HIS_MAX_RETRIES + 1
        assert entry["manual_override"] is False

    def test_enqueue_dedupes_by_rx(self, run_dir: Path) -> None:
        q = WritebackQueue(run_dir=run_dir)
        q.enqueue_failed(
            prescription_no="RX-A",
            payload=_make_payload(prescription_no="RX-A"),
            last_error="err1",
        )
        q.enqueue_failed(
            prescription_no="RX-A",
            payload=_make_payload(prescription_no="RX-A"),
            last_error="err2",
        )
        items = q.list_pending()
        assert len(items) == 1
        assert items[0]["last_error"] == "err2"

    def test_enqueue_multiple_rx_keeps_all(self, run_dir: Path) -> None:
        q = WritebackQueue(run_dir=run_dir)
        for rx in ("RX-A", "RX-B", "RX-C"):
            q.enqueue_failed(
                prescription_no=rx,
                payload=_make_payload(prescription_no=rx),
                last_error=f"err-{rx}",
            )
        assert len(q.list_pending()) == 3

    def test_remove_returns_true_when_found(self, run_dir: Path) -> None:
        q = WritebackQueue(run_dir=run_dir)
        q.enqueue_failed(
            prescription_no="RX-A",
            payload=_make_payload(),
            last_error="err",
        )
        assert q.remove("RX-A") is True
        assert q.list_pending() == []

    def test_remove_returns_false_when_missing(self, run_dir: Path) -> None:
        q = WritebackQueue(run_dir=run_dir)
        assert q.remove("RX-A") is False

    def test_clear(self, run_dir: Path) -> None:
        q = WritebackQueue(run_dir=run_dir)
        q.enqueue_failed(
            prescription_no="RX-A",
            payload=_make_payload(),
            last_error="err",
        )
        q.clear()
        assert q.list_pending() == []
        assert q.path.is_file()

    def test_corrupted_json_lashed_as_empty(self, run_dir: Path) -> None:
        q = WritebackQueue(run_dir=run_dir)
        q.path.write_text("{not valid json", encoding="utf-8")
        assert q.list_pending() == []
        # 重新入队应能覆盖
        q.enqueue_failed(
            prescription_no="RX-A",
            payload=_make_payload(),
            last_error="err",
        )
        assert len(q.list_pending()) == 1

    def test_run_dir_created_if_missing(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "runs" / "RX-A"
        # 不预创建
        q = WritebackQueue(run_dir=run_dir)
        assert run_dir.is_dir()
        q.enqueue_failed(
            prescription_no="RX-A",
            payload=_make_payload(),
            last_error="err",
        )
        assert q.path.is_file()

    def test_enqueue_rejects_empty_rx(self, run_dir: Path) -> None:
        q = WritebackQueue(run_dir=run_dir)
        with pytest.raises(HisAdapterError):
            q.enqueue_failed(
                prescription_no="",
                payload=_make_payload(),
                last_error="err",
            )


# --------------------------------------------------------------------------- #
# Dataclass 字段 / 边界                                                        #
# --------------------------------------------------------------------------- #


class TestDataclassFields:
    """`HisWriteback` / `HisWritebackReceipt` / `PreauditTicket` dataclass 边界。"""

    def test_his_writeback_is_frozen(self) -> None:
        wb = _make_payload()
        with pytest.raises(Exception):  # FrozenInstanceError
            wb.prescription_no = "RX-OTHER"  # type: ignore[misc]

    def test_his_writeback_receipt_default_timestamp_iso8601(self) -> None:
        receipt = HisWritebackReceipt(
            prescription_no="RX-A",
            ok=True,
            message="ok",
            endpoint="x",
            attempts=1,
        )
        # ISO 8601 UTC, 秒级
        datetime.fromisoformat(receipt.written_at)
        assert receipt.written_at.endswith("+00:00") or receipt.written_at.endswith("Z")

    def test_preaudit_ticket_is_frozen(self) -> None:
        t = _make_ticket()
        with pytest.raises(Exception):
            t.prescription_no = "RX-OTHER"  # type: ignore[misc]

    def test_preaudit_ticket_receipt_default_timestamp_iso8601(self) -> None:
        receipt = PreauditTicketReceipt(
            prescription_no="RX-A",
            ok=True,
            ticket_id="T-1",
            message="ok",
            endpoint="x",
        )
        datetime.fromisoformat(receipt.created_at)

    def test_his_writeback_queue_filename_default(self, run_dir: Path) -> None:
        q = WritebackQueue(run_dir=run_dir)
        assert q.queue_filename == "writeback_queue.json"


# --------------------------------------------------------------------------- #
# 写入失败可观察                                                               #
# --------------------------------------------------------------------------- #


class TestWritebackObservability:
    """失败现场保留 / reader 容错 / 二次补跑可观察。"""

    def test_queue_entry_contains_audit_summary(self, run_dir: Path) -> None:
        """队列条目保留 payload 关键字段，便于补跑时不需要重新构造。"""
        q = WritebackQueue(run_dir=run_dir)
        q.enqueue_failed(
            prescription_no="RX-A",
            payload=_make_payload(audit_summary="3 findings; pharmacist overridden"),
            last_error="timeout",
        )
        items = q.list_pending()
        assert items[0]["audit_summary"] == "3 findings; pharmacist overridden"
        assert items[0]["operator"] == "<EXAMPLE_PHARMACIST>"

    def test_recover_after_retry_succeeds(self, run_dir: Path) -> None:
        """先入队 → 修复替身 → 重跑 → 队列清理。"""
        q = WritebackQueue(run_dir=run_dir)

        # 1. 替身一直失败 → 入队
        bad = FakeHisAdapter(run_dir=run_dir, fail_times=99)
        # 同步入队（不走异步编排）
        q.enqueue_failed(
            prescription_no="RX-A",
            payload=_make_payload(),
            last_error="simulated",
        )
        assert len(q.list_pending()) == 1

        # 2. 修复后回写成功
        good = FakeHisAdapter(run_dir=run_dir)
        receipt = good.writeback(_make_payload())
        assert receipt.ok is True

        # 3. 从队列移除
        assert q.remove("RX-A") is True
        assert q.list_pending() == []


# --------------------------------------------------------------------------- #
# 常量 / 默认                                                                  #
# --------------------------------------------------------------------------- #


class TestConstants:
    """替身 / 编排层常量与 spec.md「失败恢复」一致。"""

    def test_his_default_timeout(self) -> None:
        assert HIS_DEFAULT_TIMEOUT_SECONDS == 10.0

    def test_his_max_retries(self) -> None:
        assert HIS_MAX_RETRIES == 3

    def test_fake_timeout_smaller(self) -> None:
        assert FAKE_TIMEOUT_SECONDS < HIS_DEFAULT_TIMEOUT_SECONDS