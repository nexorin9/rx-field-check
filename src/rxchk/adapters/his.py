"""HIS（医院信息系统）回写适配器：抽象接口 + 替身实现 + 重试 / 降级。

设计目标
--------
- **抽象 Protocol 是正式对接契约**：HIS 接入方（卫宁 / 创业 / 东软等）
  按同一组方法接入；本轮替身只写本地 JSON，**明确不是最终对接面**。
- **超时（10s）+ 重试 3 次**：与 spec.md「失败模式 / 降级」对齐 —— HIS
  不可达时不抛错打断药师已确认的事实，而是落本地重试队列
  `WritebackQueue` 待恢复后补跑。
- **替身实现显式标注 `[FAKE]`**：日志前缀便于审计员一眼看出"现在跑
  的是替身"，避免与真实 HIS 端点混淆。
- **回执落 `writeback.json`**：与 spec.md「输出闭环」表 `written_back`
  阶段对齐 —— 真实 HIS 回执或替身落盘都写到同一文件路径，便于 viewer
  / 飞行检查脚本读出。

融合后的产品路径
----------------
1. 药师在 Viewer 点确认 → `audit.jsonl` 追加 + run.json 推到
   `confirmed | overridden`
2. `check_engine.run_check` / e2e / 后续 CLI 复核触发
   `writeback_with_retry(his_adapter, run_dir)`
3. 调 `HisAdapter.writeback(rx_no, audit_summary)` → 替身写到
   `runs/<rx>/writeback.json`；真实实现走院内 REST / HL7 / 中间表
4. 成功 → run.json 推到 `written_back`；超时 / 失败 → 入 `WritebackQueue`
   `failed` 桶，待恢复后 `retry_pending()` 补跑

源 repo 名 / slug 不出现在产品代码或字符串字面值。
"""

from __future__ import annotations

import asyncio
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
# 常量                                                                        #
# --------------------------------------------------------------------------- #


#: HIS 单次调用超时（与 spec.md「失败恢复」一致：HIS 接口超时 10s）。
HIS_DEFAULT_TIMEOUT_SECONDS: float = 10.0

#: 替身调用默认超时（更短，便于测试 fail-fast；真实端点走 HIS_DEFAULT_TIMEOUT_SECONDS）。
FAKE_TIMEOUT_SECONDS: float = 2.0

#: 失败重试次数上限（spec.md 失败恢复「重试 3 次」）。
HIS_MAX_RETRIES: int = 3

#: 重试基础退避（秒；指数退避 1s → 2s → 4s）。
_BACKOFF_BASE_SECONDS: float = 1.0


# --------------------------------------------------------------------------- #
# 自定义异常                                                                  #
# --------------------------------------------------------------------------- #


class HisAdapterError(RuntimeError):
    """HIS 适配器调用 / 配置 / 契约错误。"""


# --------------------------------------------------------------------------- #
# 数据结构：回写请求 + 回执                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HisWriteback:
    """HIS 回写请求体：把药师确认结论写入 HIS 的 `RX_AUDIT_NOTE` 字段。

    字段与 spec.md「输出闭环」表 + 真实 HIS 接入端点约定一致：
    - `prescription_no`：处方号（HIS 主键）
    - `audit_summary`：由 `audit.jsonl` 聚合而来（findings + 操作人 + 时间）
    - `manual_override`：是否有「规则库未覆盖、由药师人工放行」操作
      （HIS 备注里追加「规则库未覆盖」字样，便于审计员二次核查）
    """

    prescription_no: str
    audit_summary: str
    manual_override: bool = False
    note: str = ""
    operator: str = ""


@dataclass(frozen=True)
class HisWritebackReceipt:
    """HIS 回执：成功 / 失败 + 端点元数据。

    - `ok`：HIS 端是否回 200 / 等价成功
    - `message`：HIS 端返回描述（成功时是 ack / 失败时是错误码）
    - `endpoint`：实际调用的端点标识（替身 = `local-file`，真实实现 =
      `http://...` 或 `hl7://...`）
    - `attempts`：实际尝试次数（含首次失败后的重试）
    - `manual_override`：透传 `HisWriteback.manual_override`，便于
      飞行检查脚本直接读 receipt 区分人工放行
    """

    prescription_no: str
    ok: bool
    message: str
    endpoint: str
    attempts: int
    manual_override: bool = False
    written_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )


# --------------------------------------------------------------------------- #
# 抽象接口                                                                    #
# --------------------------------------------------------------------------- #


@runtime_checkable
class HisAdapterProtocol(Protocol):
    """HIS 回写适配器抽象接口（正式对接契约）。

    真实 HIS 接入（卫宁 / 创业 / 东软 / 自研）通过同一方法签名实现：
    - `writeback(...)`：单次回写；替身实现写本地 `writeback.json`，真实
      实现走院内 REST / HL7 v2.x / 中间表（接口契约已固定，切换实现
      不影响业务层）。
    - `get_endpoint()`：返回端点标识（替身 = `local-file`，真实 = URL），
      用于 `HisWritebackReceipt.endpoint` 与审计。
    """

    def writeback(self, payload: HisWriteback) -> HisWritebackReceipt: ...

    def get_endpoint(self) -> str: ...


# --------------------------------------------------------------------------- #
# 替身实现                                                                    #
# --------------------------------------------------------------------------- #


class FakeHisAdapter:
    """HIS 替身 —— 把回写结果落 `runs/<rx_no>/writeback.json`。

    实现要点
    --------
    - 显式日志前缀 `[FAKE]`，便于审计员识别替身运行
    - 默认 `writeback_file` 是 `runs/<rx_no>/writeback.json`；
      可注入测试替身路径便于隔离
    - 替身同步写盘（不阻塞事件循环；真实端点要走 asyncio.to_thread）
    - 模拟故障注入：`fail_times` 控制连续失败次数（测试用），
      `succeed_after_fail` 行为：前 N 次抛 `HisAdapterError`，第 N+1 次
      返回成功
    """

    ENDPOINT_LABEL: str = "local-file"

    def __init__(
        self,
        *,
        run_dir: Path | str,
        fail_times: int = 0,
        timeout_seconds: float = FAKE_TIMEOUT_SECONDS,
    ) -> None:
        if not run_dir:
            raise HisAdapterError("FakeHisAdapter.run_dir must not be empty")
        self.run_dir: Path = Path(run_dir)
        self.fail_times: int = max(0, int(fail_times))
        self.timeout_seconds: float = timeout_seconds
        self.attempts_log: list[int] = []
        logger.info(
            "[FAKE] HisAdapter initialized run_dir=%s endpoint=%s",
            self.run_dir,
            self.ENDPOINT_LABEL,
        )

    def get_endpoint(self) -> str:
        return self.ENDPOINT_LABEL

    def writeback(self, payload: HisWriteback) -> HisWritebackReceipt:
        """同步落 `writeback.json`；按 `fail_times` 模拟连续失败。

        - fail_times == 0：始终成功
        - fail_times == N：前 N 次抛 `HisAdapterError`，第 N+1 次成功
        """
        if not isinstance(payload, HisWriteback):
            raise HisAdapterError(
                f"writeback requires HisWriteback, got {type(payload).__name__}"
            )
        if not payload.prescription_no or not isinstance(payload.prescription_no, str):
            raise HisAdapterError(
                "writeback.prescription_no must be a non-empty string"
            )

        self.attempts_log.append(1)
        attempt_no = len(self.attempts_log)
        if attempt_no <= self.fail_times:
            logger.warning(
                "[FAKE] HisAdapter.writeback 模拟失败 rx=%s attempt=%d/%d",
                payload.prescription_no,
                attempt_no,
                self.fail_times,
            )
            raise HisAdapterError(
                f"[FAKE] simulated his failure attempt={attempt_no}"
            )

        receipt = _atomic_write_writeback(
            run_dir=self.run_dir,
            payload=payload,
            endpoint=self.ENDPOINT_LABEL,
            attempts=attempt_no,
        )
        logger.info(
            "[FAKE] HisAdapter.writeback ok rx=%s endpoint=%s attempts=%d",
            payload.prescription_no,
            self.ENDPOINT_LABEL,
            attempt_no,
        )
        return receipt


# --------------------------------------------------------------------------- #
# 替身 + 重试 / 超时编排（产品代码）                                            #
# --------------------------------------------------------------------------- #


async def writeback_with_retry(
    adapter: HisAdapterProtocol,
    payload: HisWriteback,
    *,
    timeout_seconds: float = HIS_DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = HIS_MAX_RETRIES,
    queue: Optional["WritebackQueue"] = None,
) -> HisWritebackReceipt:
    """调 `adapter.writeback(payload)`，遇超时 / 错误按指数退避重试。

    参数
    ----
    adapter : HisAdapterProtocol
        HIS 适配器（替身或真实实现）
    payload : HisWriteback
        回写请求体
    timeout_seconds : float
        单次调用超时（默认 10s）
    max_retries : int
        最大重试次数（含首次失败 → 总尝试次数 = max_retries+1）
    queue : WritebackQueue | None
        重试耗尽后入队目标；`None` 表示不持久化（飞检场景保留可选项）

    返回
    ----
    HisWritebackReceipt
        成功时 ok=True；失败时（已入队）ok=False + message 含「queue/deferred」

    行为约定
    --------
    - `asyncio.wait_for(coro, timeout=timeout_seconds)` —— 超时转 `HisAdapterError`
    - 重试退避：1s → 2s → 4s（指数），最多 `max_retries` 次
    - 重试耗尽仍失败 → 调 `queue.enqueue_failed(payload, last_error)`，
      返回的 receipt `ok=False` + `message` 含 "deferred"
    - `HisAdapterError` 之外的异常立即上抛（参数 / 类型错误不该被静默重试）
    """
    if not isinstance(adapter, HisAdapterProtocol):
        raise HisAdapterError(
            f"writeback_with_retry requires HisAdapterProtocol, "
            f"got {type(adapter).__name__}"
        )

    last_error: Optional[BaseException] = None
    for attempt in range(max_retries + 1):
        try:
            receipt = await asyncio.wait_for(
                asyncio.to_thread(adapter.writeback, payload),
                timeout=timeout_seconds,
            )
            return receipt
        except asyncio.TimeoutError as exc:
            last_error = exc
            logger.warning(
                "his.writeback 超时 rx=%s attempt=%d/%d timeout=%.1fs",
                payload.prescription_no,
                attempt + 1,
                max_retries + 1,
                timeout_seconds,
            )
        except HisAdapterError as exc:
            last_error = exc
            logger.warning(
                "his.writeback 失败 rx=%s attempt=%d/%d err=%s",
                payload.prescription_no,
                attempt + 1,
                max_retries + 1,
                exc,
            )
        except Exception:
            # 参数 / 类型错误不该被静默重试（飞检要求「任何错误都留痕」）
            raise

        # 指数退避（最后一次失败后不再 sleep）
        if attempt < max_retries:
            backoff = _BACKOFF_BASE_SECONDS * (2 ** attempt)
            await asyncio.sleep(backoff)

    # 重试耗尽：入队 + 返回失败回执
    deferred_message = (
        f"deferred after {max_retries + 1} attempts: {last_error!r}"
        if last_error is not None
        else f"deferred after {max_retries + 1} attempts"
    )
    if queue is not None:
        queue.enqueue_failed(
            prescription_no=payload.prescription_no,
            payload=payload,
            last_error=deferred_message,
        )

    return HisWritebackReceipt(
        prescription_no=payload.prescription_no,
        ok=False,
        message=deferred_message,
        endpoint=adapter.get_endpoint(),
        attempts=max_retries + 1,
        manual_override=payload.manual_override,
    )


# --------------------------------------------------------------------------- #
# 重试队列（落盘 `runs/<rx_no>/writeback_queue.json`）                          #
# --------------------------------------------------------------------------- #


@dataclass
class WritebackQueue:
    """HIS 回写失败队列 —— 落地 `runs/<rx_no>/writeback_queue.json`。

    设计要点
    --------
    - **append-only**：每次 enqueue 原子重写整个队列文件（队列规模很小，
      一般 ≤ 5 条；非高吞吐场景）
    - **持久化**：进程崩 / 服务重启后从落盘恢复，与 audit.jsonl 同款
      append-only 语义但操作粒度是队列而非流
    - **失败现场保留**：每条 entry 含 `last_error` + `failed_at`，便于
      信息科 / 飞行检查脚本看队列时一眼识别是什么错
    - **去重**：同一处方号重复入队只覆盖最新一条（避免队列无限膨胀）
    """

    run_dir: Path
    queue_filename: str = "writeback_queue.json"

    def __post_init__(self) -> None:
        if not self.run_dir:
            raise HisAdapterError("WritebackQueue.run_dir must not be empty")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._path: Path = self.run_dir / self.queue_filename

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "his.queue load 损坏：%s；按空队列处理", self._path
            )
            return []
        if not isinstance(data, list):
            return []
        return data

    def _save(self, items: list[dict[str, Any]]) -> None:
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{self.queue_filename}.",
                suffix=".tmp",
                dir=str(self.run_dir),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(items, fh, ensure_ascii=False, indent=2)
                    fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            Path(tmp_path).replace(self._path)
        except OSError as exc:
            logger.exception("his.queue save failed path=%s err=%s", self._path, exc)
            raise HisAdapterError(f"queue save failed: {exc}") from exc

    def enqueue_failed(
        self,
        *,
        prescription_no: str,
        payload: HisWriteback,
        last_error: str,
    ) -> dict[str, Any]:
        """入队失败请求；同处方号去重覆盖。"""
        if not prescription_no:
            raise HisAdapterError(
                "WritebackQueue.enqueue_failed requires prescription_no"
            )
        items = self._load()
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = {
            "prescription_no": prescription_no,
            "audit_summary": payload.audit_summary,
            "manual_override": payload.manual_override,
            "note": payload.note,
            "operator": payload.operator,
            "last_error": last_error,
            "failed_at": now_iso,
            "attempts": HIS_MAX_RETRIES + 1,
        }
        # 去重：同处方号覆盖最新一条
        filtered = [it for it in items if it.get("prescription_no") != prescription_no]
        filtered.append(entry)
        self._save(filtered)
        logger.warning(
            "his.queue enqueue rx=%s last_error=%s", prescription_no, last_error
        )
        return entry

    def list_pending(self) -> list[dict[str, Any]]:
        """列出当前待补跑条目（按写入顺序）。"""
        return self._load()

    def remove(self, prescription_no: str) -> bool:
        """补跑成功后从队列移除；返回是否真的删了一条。"""
        items = self._load()
        filtered = [it for it in items if it.get("prescription_no") != prescription_no]
        if len(filtered) == len(items):
            return False
        self._save(filtered)
        return True

    def clear(self) -> None:
        """清空队列（信息科运维 / 测试用）。"""
        self._save([])


# --------------------------------------------------------------------------- #
# 内部：原子落 `writeback.json`                                                #
# --------------------------------------------------------------------------- #


def _atomic_write_writeback(
    *,
    run_dir: Path,
    payload: HisWriteback,
    endpoint: str,
    attempts: int,
) -> HisWritebackReceipt:
    """原子写 `writeback.json`：tmp + replace（同目录 rename 是原子操作）。

    文件 schema（与 spec.md「输出闭环」表对齐）：
    {
      "prescription_no": "...",
      "endpoint": "...",
      "ok": true,
      "message": "...",
      "attempts": 1,
      "manual_override": false,
      "audit_summary": "...",
      "operator": "...",
      "note": "...",
      "written_at": "ISO 8601"
    }
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    receipt = HisWritebackReceipt(
        prescription_no=payload.prescription_no,
        ok=True,
        message="audit note written",
        endpoint=endpoint,
        attempts=attempts,
        manual_override=payload.manual_override,
    )
    file_payload = {
        "prescription_no": receipt.prescription_no,
        "endpoint": receipt.endpoint,
        "ok": receipt.ok,
        "message": receipt.message,
        "attempts": receipt.attempts,
        "manual_override": receipt.manual_override,
        "audit_summary": payload.audit_summary,
        "operator": payload.operator,
        "note": payload.note,
        "written_at": receipt.written_at,
    }
    target = run_dir / "writeback.json"
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".writeback.",
            suffix=".tmp",
            dir=str(run_dir),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(file_payload, fh, ensure_ascii=False, indent=2)
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
            "his.writeback atomic_write failed rx=%s path=%s err=%s",
            payload.prescription_no,
            target,
            exc,
        )
        raise HisAdapterError(
            f"writeback atomic_write failed for {target}: {exc}"
        ) from exc
    return receipt


__all__ = [
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
]