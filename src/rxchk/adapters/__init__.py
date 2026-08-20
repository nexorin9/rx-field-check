"""HIS / 前置审方回写适配器（任务 13）。

按 spec.md「正式对接层」要求：
- 抽象 Protocol 是正式对接契约 —— 院内 HIS / 前置审方接入同一组方法签名
- 替身（`FakeHisAdapter` / `FakePreauditAdapter`）只用于开发与端到端测试，
  日志显式打 `[FAKE]`，**明确不是最终对接面**
- 超时（10s）+ 重试 3 次后入 `failed` 队列（HIS 不可达时），待恢复后补跑
- 回执落 `runs/<rx_no>/writeback.json`，状态推进到 `written_back`

源 repo 名 / slug 不出现在产品代码或字符串字面值。
"""

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

__all__ = [
    # his
    "FAKE_TIMEOUT_SECONDS",
    "HIS_DEFAULT_TIMEOUT_SECONDS",
    "HIS_MAX_RETRIES",
    "FakeHisAdapter",
    "HisAdapterError",
    "HisAdapterProtocol",
    "HisWriteback",
    "HisWritebackReceipt",
    "WritebackQueue",
    "writeback_with_retry",
    # preaudit
    "FakePreauditAdapter",
    "PreauditAdapterError",
    "PreauditAdapterProtocol",
    "PreauditTicket",
    "PreauditTicketReceipt",
]