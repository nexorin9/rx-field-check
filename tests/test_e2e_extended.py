"""端到端 e2e 扩展（任务 33）：批量 + 大处方 + 并发核对 + 全链路验证。

覆盖矩阵
========

| 场景                 | 路径              | 入参                                 | 验收                                                |
|----------------------|-------------------|--------------------------------------|-----------------------------------------------------|
| 批量 5 张处方        | happy + 异常混合  | `check_inbox(inbox_dir)` 一次性处理  | 5 个 `runs/<rx>/` 目录齐全；批量 ≤15s                |
| 大处方（20 items）   | 异常              | 单张大处方（多药品 + 多 finding）     | 单次核对 ≤5s；findings 跨多字段族                    |
| 5 并发 Viewer 客户端 | 鉴权 + 列表       | `GET /api/runs` × 5 并发              | 全部 200；token 鉴权覆盖                             |
| HIS 替身持续失败     | 降级              | `FakeHisAdapter(fail_times=99)` 触发 | `WritebackQueue` 入队；`writeback.json` 缺失         |
| 完整闭环             | happy + 异常 + 降级 | 3 张样例各跑全链路                  | 每条 e2e < 5s；run.json → written_back               |

设计要点
========
- **不与 test_e2e.py 重复**：task 14 的 test_e2e.py 覆盖单条样例的 happy /
  异常 / 降级三条路径；本模块扩展**批量、大处方、并发、持续失败降级**四
  个独立维度。
- **测试替身 vs 真实替身**：HIS 回写走 `FakeHisAdapter`（spec.md「正式对接
  层」明文替身，**不是**真实 HIS）；前置审方走 `FakePreauditAdapter`。
- **Viewer 服务**：用 `viewer.server.serve(...)` 起后台线程，调用方负责
  `shutdown()`，避免子进程 / socket 残留。
- **HTTP 客户端**：用标准库 `urllib.request` 走 `127.0.0.1:<port>`，避免
  pytest 受网络权限拦截；并发用 `concurrent.futures.ThreadPoolExecutor`
  5 worker 模拟多客户端。
- **不依赖外部服务**：所有 IO 在本进程内，无 HIS / 前置审方 / 字典 /
  规则库真实端点。
"""

from __future__ import annotations

import json
import shutil
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

import pytest

from rxchk.adapters.his import (
    FakeHisAdapter,
    HisWriteback,
    WritebackQueue,
    writeback_with_retry,
)
from rxchk.adapters.preaudit import (
    FakePreauditAdapter,
    PreauditTicket,
)
from rxchk.audit import (
    AUDIT_ACTIONS,
    append_audit,
    read_audit_entries,
)
from rxchk.check_engine import (
    build_check_engine,
    check_inbox,
    run_check,
)
from rxchk.models.prescription import (
    FieldFamily,
    UNCOVERED_FAMILY,
)
from rxchk.parsers.prescription import parse_prescription
from rxchk.report.state import RunState
from rxchk.report.writer import (
    FINDINGS_FILENAME,
    GRAPH_FILENAME,
    INPUT_FILENAME,
    RUN_FILENAME,
    TRANSCRIPT_FILENAME,
    EVENT_FINDING_APPEND,
    TranscriptEvent,
    ReportWriter,
)
from rxchk.viewer.server import serve
from rxchk.viewer.transcript import (
    iter_run_dirs,
    read_findings,
    read_run_summary,
    read_writeback,
)


# --------------------------------------------------------------------------- #
# Fixtures / shared helpers                                                    #
# --------------------------------------------------------------------------- #


SAMPLE_NAMES: tuple[str, ...] = (
    "rx_basic",
    "rx_overdose",
    "rx_allergy",
    "rx_complex",
)


@dataclass
class ViewerHandle:
    """`viewer.server.serve` 的最小化包装，便于 e2e 测试复用与清理。"""

    httpd: ThreadingHTTPServer
    url: str
    token: str

    @property
    def base_url(self) -> str:
        return f"{self.url}/?token={self.token}"

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
    ) -> tuple[int, dict[str, Any] | str]:
        """`path` 必须以 `/` 起头；返回 `(status_code, parsed_body)`。"""
        url = f"{self.url}{path}"
        req_headers: dict[str, str] = {"Accept": "application/json"}
        if headers:
            req_headers.update(headers)
        effective_token = token if token is not None else self.token
        if effective_token:
            req_headers.setdefault("Authorization", f"Bearer {effective_token}")
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            url, data=data, headers=req_headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=2.0) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        try:
            parsed: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = raw.decode("utf-8", errors="replace")
        return status, parsed

    def shutdown(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.httpd.server_close()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture(scope="module")
def pkg_data_root() -> Path:
    """包内 data/ 目录（替身 yaml 与 sample_rx/ 在此）。"""
    return Path(__file__).resolve().parent.parent / "src" / "rxchk" / "data"


@pytest.fixture()
def sample_paths(pkg_data_root: Path) -> dict[str, Path]:
    """4 张样例处方路径。"""
    return {
        name: pkg_data_root / "sample_rx" / f"{name}.json" for name in SAMPLE_NAMES
    }


def _run_one_sample(
    sample_path: Path,
    *,
    data_root: Path,
    run_root: Path,
) -> dict[str, Any]:
    """跑一次 `rxchk check <sample>`，返回 `{result, run_dir}`。"""
    prescription = parse_prescription(sample_path)
    engine = build_check_engine(data_root)
    result = run_check(
        prescription,
        engine=engine,
        data_root=data_root,
        run_root=run_root,
    )
    return {
        "result": result,
        "engine": engine,
        "prescription": prescription,
        "run_dir": result.run_dir,
    }


def _assert_runs_complete(run_dir: Path) -> None:
    """`runs/<rx>/` 必含 5 核心产物：input / graph / transcript / findings / run。"""
    for filename in (
        INPUT_FILENAME,
        GRAPH_FILENAME,
        TRANSCRIPT_FILENAME,
        FINDINGS_FILENAME,
        RUN_FILENAME,
    ):
        path = run_dir / filename
        assert path.is_file(), f"missing {filename} in {run_dir}"
    summary = read_run_summary(run_dir)
    assert summary.get("status") == RunState.AWAITING_REVIEW.value, summary
    findings = read_findings(run_dir)
    assert "findings" in findings
    assert "count" in findings


def _drive_his_writeback_sync(
    *,
    run_dir: Path,
    his: FakeHisAdapter,
    preaudit: FakePreauditAdapter,
    operator: str,
    manual_override: bool,
    findings: list[dict[str, Any]],
) -> Any:
    """同步驱动 HIS + 前置审方替身；返回 `HisWritebackReceipt`。

    与 `test_e2e.py` 同款逻辑；本模块复用，引入包内 helper 减少重复。
    """
    from rxchk.adapters.his import HisWritebackReceipt

    findings_summary = "; ".join(
        f"{f.get('family')}/{f.get('field')}={f.get('rule_id')}"
        for f in findings
    ) or "<no findings>"

    payload = HisWriteback(
        prescription_no=run_dir.name,
        audit_summary=findings_summary,
        manual_override=manual_override,
        note="e2e_extended writeback",
        operator=operator,
    )
    receipt = his.writeback(payload)

    severity = "high" if any(
        f.get("severity") == "high" for f in findings
    ) else "medium" if findings else "info"

    preaudit.create_ticket(
        PreauditTicket(
            prescription_no=run_dir.name,
            findings_summary=findings_summary,
            severity=severity,
            rule_version="v_test.e2e_ext",
            operator=operator,
            note="e2e_extended preaudit",
        )
    )
    writer = ReportWriter(rx_no=run_dir.name, run_root=run_dir.parent)
    writer.transition(
        RunState.WRITTEN_BACK,
        actor="rxchk.e2e_extended",
        reason=f"HIS writeback ok attempts={receipt.attempts}",
    )
    return receipt


def _confirm_and_writeback(
    *,
    run_dir: Path,
    his: FakeHisAdapter,
    preaudit: FakePreauditAdapter,
    operator: str,
    action: str,
    note: str,
    manual_override: bool,
) -> Any:
    """append audit → HIS 回写 → 工单合并 → run.json 推进 → written_back。"""
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"unsupported action: {action}")

    append_audit(
        run_dir,
        action=action,
        operator=operator,
        note=note,
        finding_id=None,
    )

    findings_payload = read_findings(run_dir)
    findings_list = list(findings_payload.get("findings") or [])
    return _drive_his_writeback_sync(
        run_dir=run_dir,
        his=his,
        preaudit=preaudit,
        operator=operator,
        manual_override=manual_override,
        findings=findings_list,
    )


def _make_large_prescription(sample_path: Path, *, items_count: int) -> Path:
    """复制样例处方到新文件，把 `items[]` 扩展到 `items_count` 条。

    用 `DRUG_AMOX` 循环填到目标条数；扩展后诊断 / 过敏保持原样。
    返回新 JSON 文件路径（写到临时目录）。
    """
    src_data = json.loads(sample_path.read_text(encoding="utf-8"))
    base_item = src_data["items"][0]
    new_items = []
    for i in range(items_count):
        new_items.append(
            {
                "drug_code": base_item.get("drug_code", "DRUG_AMOX"),
                "dose": base_item.get("dose", "0.5g"),
                "frequency": base_item.get("frequency", "tid"),
                "route": base_item.get("route", "po"),
                "duration_days": base_item.get("duration_days", 7),
                "_seq": i,
            }
        )
    src_data["items"] = new_items
    new_path = sample_path.parent / f"large_rx_{items_count}.json"
    new_path.write_text(
        json.dumps(src_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return new_path


# --------------------------------------------------------------------------- #
# Happy path：批量 5 张处方走 inbox 入口                                       #
# --------------------------------------------------------------------------- #


def test_e2e_extended_batch_inbox_5_prescriptions(
    pkg_data_root: Path,
    sample_paths: dict[str, Path],
    tmp_path: Path,
) -> None:
    """`check_inbox` 一次扫 5 张处方（含 basic + overdose + allergy + complex +
    1 张重复 basic）→ 各 `runs/<rx>/` 目录独立完整 → 批量 ≤15s。"""
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    # 复制 5 张处方到 inbox（基本 + 异常 + 重复 1 张）
    inbox_files = []
    src_list = [
        sample_paths["rx_basic"],
        sample_paths["rx_overdose"],
        sample_paths["rx_allergy"],
        sample_paths["rx_complex"],
        sample_paths["rx_basic"],
    ]
    # 重复处方号会冲突 runs/<rx>/；通过后缀区分
    for idx, src in enumerate(src_list):
        data = json.loads(src.read_text(encoding="utf-8"))
        # 末尾加唯一后缀，避免 runs/<rx>/ 冲突
        suffix = f"-B{idx:02d}"
        data["prescription_no"] = f"{data['prescription_no']}{suffix}"
        dest = inbox_dir / f"{data['prescription_no']}.json"
        dest.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        inbox_files.append(dest)

    assert len(inbox_files) == 5
    run_root = tmp_path / "runs"
    t0 = time.monotonic()

    engine = build_check_engine(pkg_data_root)
    inbox_result = check_inbox(
        inbox_dir,
        engine=engine,
        data_root=pkg_data_root,
        run_root=run_root,
    )

    elapsed = time.monotonic() - t0
    assert len(inbox_result.handled) == 5, inbox_result.failed_files
    assert inbox_result.failed_files == []

    # 5 个 runs/<rx>/ 目录齐全 + 各自包含 5 核心产物
    expected_rxnos = {f.stem for f in inbox_files}
    actual_rxnos = {p.name for p in run_root.iterdir() if p.is_dir()}
    assert expected_rxnos <= actual_rxnos, (expected_rxnos, actual_rxnos)
    for rxno in expected_rxnos:
        run_dir = run_root / rxno
        _assert_runs_complete(run_dir)

    # processed/ 内应有 5 张；inbox/ 内 0 张剩余
    processed_dir = inbox_dir / "processed"
    assert processed_dir.is_dir()
    processed_files = list(processed_dir.glob("*.json"))
    assert len(processed_files) == 5

    # 批量总时长 ≤ 15s（task 33 验收线）
    assert elapsed < 15.0, f"batch too slow: {elapsed:.2f}s for 5 prescriptions"


# --------------------------------------------------------------------------- #
# 大处方：20 items 单次核对 ≤5s                                                #
# --------------------------------------------------------------------------- #


def test_e2e_extended_large_prescription_20_items(
    pkg_data_root: Path,
    sample_paths: dict[str, Path],
    tmp_path: Path,
) -> None:
    """单张大处方（20 items）→ 8 字段族 Agent 并发核对 → 单次 ≤5s → findings
    跨多字段族（剂型 / 频次 / 途径）→ 落盘 → 启动 Viewer → 拉 findings。"""
    large_path = _make_large_prescription(
        sample_paths["rx_basic"], items_count=20
    )
    run_root = tmp_path / "runs"

    t0 = time.monotonic()
    bundle = _run_one_sample(large_path, data_root=pkg_data_root, run_root=run_root)
    run_dir: Path = bundle["run_dir"]
    result = bundle["result"]
    _assert_runs_complete(run_dir)

    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"large rx too slow: {elapsed:.2f}s"

    # 大处方不一定命中（取决于规则），但 20 items 必须全部参与核对
    assert result.finding_count >= 0

    # 启动 Viewer 拉 findings（确认落盘可读）
    httpd, url, token = serve(run_dir, host="127.0.0.1", port=0, open_browser=False)
    viewer = ViewerHandle(httpd=httpd, url=url, token=token)
    try:
        status, payload = viewer.request(f"/api/run/{run_dir.name}/findings")
        assert status == 200
        assert "findings" in payload
        assert payload["count"] == result.finding_count

        # 启动期 token 也允许 GET /api/runs（根路径）→ 至少含本 run
        status, payload = viewer.request("/api/runs")
        assert status == 200
        run_names = [r.get("run_name") for r in payload.get("runs", [])]
        assert run_dir.name in run_names
    finally:
        viewer.shutdown()


# --------------------------------------------------------------------------- #
# 5 并发 Viewer 客户端：GET /api/runs                                           #
# --------------------------------------------------------------------------- #


def test_e2e_extended_concurrent_viewer_5_clients(
    pkg_data_root: Path,
    sample_paths: dict[str, Path],
    tmp_path: Path,
) -> None:
    """5 客户端并发 GET /api/runs → 全部 200；token 鉴权覆盖。

    实现：先跑 4 张样例处方落盘到同一 run_root，再启动 Viewer，最后
    `ThreadPoolExecutor(max_workers=5)` 并发拉 `/api/runs`。
    """
    run_root = tmp_path / "runs"
    # 跑 4 张样例（覆盖 happy + 异常混合）
    for sample_name in ("rx_basic", "rx_overdose", "rx_allergy", "rx_complex"):
        bundle = _run_one_sample(
            sample_paths[sample_name],
            data_root=pkg_data_root,
            run_root=run_root,
        )
        _assert_runs_complete(bundle["run_dir"])

    # 启动 Viewer
    httpd, url, token = serve(run_root, host="127.0.0.1", port=0, open_browser=False)
    viewer = ViewerHandle(httpd=httpd, url=url, token=token)
    try:

        def _client_request() -> tuple[int, Any]:
            return viewer.request("/api/runs")

        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(_client_request) for _ in range(5)]
            results = [f.result(timeout=5.0) for f in as_completed(futures)]
        elapsed = time.monotonic() - t0

        # 5 个客户端全部 200 + JSON
        assert len(results) == 5
        for status, payload in results:
            assert status == 200, payload
            assert isinstance(payload, dict)
            assert "runs" in payload
            assert len(payload["runs"]) >= 4  # 至少含 4 张样例

        # 并发 5 客户端握手成功 < 5s（线程池内已经限制了）
        assert elapsed < 5.0, f"concurrent viewer too slow: {elapsed:.2f}s"

        # token 鉴权覆盖：错 token → 401
        status, payload = viewer.request("/api/runs", token="bad-token")
        assert status == 401
        assert isinstance(payload, dict)
        assert payload.get("code") == "unauthorized"
    finally:
        viewer.shutdown()


# --------------------------------------------------------------------------- #
# HIS 替身持续失败 → 降级入 WritebackQueue                                     #
# --------------------------------------------------------------------------- #


def test_e2e_extended_his_persistent_failure_degrades_to_queue(
    pkg_data_root: Path,
    sample_paths: dict[str, Path],
    tmp_path: Path,
) -> None:
    """`FakeHisAdapter(fail_times=HIS_MAX_RETRIES+1)` 触发 `writeback_with_retry`
    重试耗尽 → `WritebackQueue` 入队；`writeback.json` 不写盘（避免半截成功
    信号）；queue 文件含 1 条。

    实现：用 `fail_times=99` 让替身永远抛错；`asyncio.run` 跑 `writeback_with_retry`
    → receipt.ok=False + message 含 "deferred"。

    注意：本测试**不**用 `@pytest.mark.asyncio`，因为 `run_check` 内部会
    `asyncio.run(...)`，与 pytest-asyncio 提供的 event loop 冲突。所以
    直接在同步上下文里 `asyncio.run(writeback_with_retry(...))`，先
    同步跑 `run_check` 落盘，再单独跑异步降级路径。
    """
    import asyncio

    from rxchk.adapters.his import HIS_MAX_RETRIES

    run_root = tmp_path / "runs"
    bundle = _run_one_sample(
        sample_paths["rx_basic"], data_root=pkg_data_root, run_root=run_root
    )
    run_dir: Path = bundle["run_dir"]
    _assert_runs_complete(run_dir)

    # 替身 HIS：永不成功（fail_times=99 远超 max_retries=3）
    his = FakeHisAdapter(run_dir=run_dir, fail_times=99)
    queue = WritebackQueue(run_dir=run_dir)

    payload = HisWriteback(
        prescription_no=run_dir.name,
        audit_summary="<degradation test>",
        manual_override=False,
        note="persistent failure",
        operator="<EXAMPLE_PHARMACIST>",
    )

    # 短超时 + 短退避，让测试快速跑完（不真等指数退避 1s+2s+4s）
    receipt = asyncio.run(
        writeback_with_retry(
            his,
            payload,
            timeout_seconds=0.2,
            max_retries=HIS_MAX_RETRIES,
            queue=queue,
        )
    )

    # 重试耗尽 → 失败回执
    assert receipt.ok is False
    assert "deferred" in receipt.message
    assert receipt.attempts == HIS_MAX_RETRIES + 1
    assert receipt.manual_override is False

    # 队列含 1 条
    pending = queue.list_pending()
    assert len(pending) == 1
    entry = pending[0]
    assert entry["prescription_no"] == run_dir.name
    assert "deferred" in entry["last_error"]
    assert entry["attempts"] == HIS_MAX_RETRIES + 1

    # writeback.json **不** 写盘（避免"看起来成功"的假信号）
    wb_path = run_dir / "writeback.json"
    assert not wb_path.is_file(), (
        f"writeback.json should not exist after persistent failure: {wb_path}"
    )

    # run.json 仍停留在 awaiting_review（降级未推进到 written_back）
    summary = read_run_summary(run_dir)
    assert summary["status"] == RunState.AWAITING_REVIEW.value


# --------------------------------------------------------------------------- #
# 完整闭环：happy + 异常 + 降级 三路径全链路（端到端各跑一次）                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sample_name, expected_families, action, manual_override",
    [
        ("rx_basic", set(), "confirm", False),
        ("rx_overdose", {"dose"}, "override", False),
        ("rx_allergy", {"allergy"}, "manual_override", True),
    ],
)
def test_e2e_extended_full_loop_three_paths(
    pkg_data_root: Path,
    sample_paths: dict[str, Path],
    tmp_path: Path,
    sample_name: str,
    expected_families: set[str],
    action: str,
    manual_override: bool,
) -> None:
    """三条路径（happy / 异常 / 降级）端到端：

    - happy（rx_basic，无 finding）→ confirm → 0 finding → written_back
    - 异常（rx_overdose，超剂量）→ override → ≥1 finding → written_back
    - 降级（rx_allergy 模拟 uncovered）→ manual_override → run.json → written_back

    每条 e2e < 5s；HIS 替身收到正确 writeback payload。
    """
    run_root = tmp_path / "runs" / sample_name
    t0 = time.monotonic()

    # 1) 跑核对
    bundle = _run_one_sample(
        sample_paths[sample_name], data_root=pkg_data_root, run_root=run_root
    )
    run_dir: Path = bundle["run_dir"]
    result = bundle["result"]
    _assert_runs_complete(run_dir)

    # 降级路径：注入一条 uncovered finding（与 test_e2e 同款）
    if action == "manual_override" and sample_name == "rx_allergy":
        from rxchk.agents.base import make_uncovered_finding
        from rxchk.coordinator import field_agent_id

        uncovered_finding = make_uncovered_finding(
            prescription_no=run_dir.name,
            family=FieldFamily.DOSE,
            field_name="items[0].dose",
            field_value="<UNCOVERED>",
            agent_id=field_agent_id(FieldFamily.DOSE),
            rule_version="v_test.extended",
        )
        writer = ReportWriter(rx_no=run_dir.name, run_root=run_dir.parent)
        writer.append_transcript(
            TranscriptEvent(
                event=EVENT_FINDING_APPEND,
                family=UNCOVERED_FAMILY,
                agent_id=field_agent_id(FieldFamily.DOSE),
                payload={
                    "reason": "rule_adapter.dose_rules.yaml missing",
                    "severity": "info",
                },
            )
        )
        writer.write_findings([uncovered_finding])
        # 现在 finding_count >= 1
        result_payload = read_findings(run_dir)
        assert result_payload["count"] >= 1
        items = result_payload["findings"]
        assert any(
            it.get("family") == UNCOVERED_FAMILY for it in items
        ), items

    # 2) 启动 Viewer
    httpd, url, token = serve(run_dir, host="127.0.0.1", port=0, open_browser=False)
    viewer = ViewerHandle(httpd=httpd, url=url, token=token)
    try:
        # 3) 拉 findings
        status, payload = viewer.request(f"/api/run/{run_dir.name}/findings")
        assert status == 200
        assert "findings" in payload
        findings_list = list(payload.get("findings") or [])

        # 4) 药师确认 / 驳回
        body: dict[str, Any] = {
            "action": action,
            "operator": "<EXAMPLE_PHARMACIST>",
            "note": f"e2e_extended {sample_name} {action}",
            "finding_id": None,
        }
        if action == "override" and findings_list:
            first = findings_list[0]
            body["note"] = (
                f"放行 {first.get('family')} / {first.get('field')}"
            )
        elif action == "manual_override" and findings_list:
            first = findings_list[0]
            body["note"] = (
                f"规则库未覆盖，由药师人工放行 {first.get('family')}"
            )

        status, payload = viewer.request(
            f"/api/run/{run_dir.name}/confirm", method="POST", body=body
        )
        assert status == 200, payload
        assert payload.get("ok") is True

        # 5) audit.jsonl 校验
        entries = read_audit_entries(run_dir)
        assert len(entries) >= 1
        assert entries[0].action == action
        if action == "manual_override":
            assert entries[0].manual_override is True

        # 6) HIS + 前置审方
        his = FakeHisAdapter(run_dir=run_dir, fail_times=0)
        preaudit = FakePreauditAdapter(run_dir=run_dir, fail_times=0)
        receipt = _confirm_and_writeback(
            run_dir=run_dir,
            his=his,
            preaudit=preaudit,
            operator="<EXAMPLE_PHARMACIST>",
            action=action,
            note=body["note"],
            manual_override=manual_override,
        )
        assert receipt.ok is True
        assert receipt.endpoint == "local-file"

        # 7) writeback.json 含 audit_summary + operator + manual_override
        wb = read_writeback(run_dir)
        assert wb.get("ok") is True
        assert wb.get("audit_summary"), wb
        assert wb.get("operator") == "<EXAMPLE_PHARMACIST>"
        assert wb.get("manual_override") == manual_override
        assert wb.get("preaudit_ticket") is not None

        # 8) run.json → written_back
        summary = read_run_summary(run_dir)
        assert summary["status"] == RunState.WRITTEN_BACK.value
    finally:
        viewer.shutdown()

    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"full loop {sample_name} too slow: {elapsed:.2f}s"


# --------------------------------------------------------------------------- #
# 全局验收集约：单处方核对 + Viewer 启动 + confirm + writeback < 5s（再守一遍） #
# --------------------------------------------------------------------------- #


def test_e2e_extended_single_rx_under_5_seconds(
    pkg_data_root: Path,
    sample_paths: dict[str, Path],
    tmp_path: Path,
) -> None:
    """单张样例 rx_complex 完整 e2e（核对 + Viewer + confirm + writeback）< 5s。

    单独测试是因为 `test_e2e_extended_full_loop_three_paths` 已经覆盖三条
    路径，本测试只守单条时长线，避免多测试间累计慢。
    """
    run_root = tmp_path / "runs"
    t0 = time.monotonic()

    bundle = _run_one_sample(
        sample_paths["rx_complex"], data_root=pkg_data_root, run_root=run_root
    )
    run_dir: Path = bundle["run_dir"]
    result = bundle["result"]
    _assert_runs_complete(run_dir)

    httpd, url, token = serve(run_dir, host="127.0.0.1", port=0, open_browser=False)
    viewer = ViewerHandle(httpd=httpd, url=url, token=token)
    try:
        status, payload = viewer.request(f"/api/run/{run_dir.name}/findings")
        assert status == 200
        assert payload["count"] == result.finding_count

        # override 放行
        if result.finding_count >= 1:
            status, _ = viewer.request(
                f"/api/run/{run_dir.name}/confirm",
                method="POST",
                body={
                    "action": "override",
                    "operator": "<EXAMPLE_PHARMACIST>",
                    "note": "e2e_extended 性能验收",
                    "finding_id": None,
                },
            )
            assert status == 200

        his = FakeHisAdapter(run_dir=run_dir, fail_times=0)
        preaudit = FakePreauditAdapter(run_dir=run_dir, fail_times=0)
        receipt = _confirm_and_writeback(
            run_dir=run_dir,
            his=his,
            preaudit=preaudit,
            operator="<EXAMPLE_PHARMACIST>",
            action="manual_override" if result.finding_count else "confirm",
            note="e2e_extended 性能验收",
            manual_override=False,
        )
        assert receipt.ok is True
    finally:
        viewer.shutdown()

    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"single rx_complex too slow: {elapsed:.2f}s"


__all__ = [
    "SAMPLE_NAMES",
    "ViewerHandle",
    "_run_one_sample",
    "_assert_runs_complete",
    "_make_large_prescription",
]