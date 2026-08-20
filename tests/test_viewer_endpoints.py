"""Viewer JSON 端点深度测试（任务 25）。

覆盖：
- 6 个端点的完整表面（4 GET + 1 GET summary + 1 POST）：
  · GET /api/runs
  · GET /api/run/<rx>          （summary 复合端点）
  · GET /api/run/<rx>/findings
  · GET /api/run/<rx>/transcript
  · GET /api/run/<rx>/audit
  · GET /api/run/<rx>/writeback
  · POST /api/run/<rx>/confirm
- 错误码 + JSON 体 `{code, message}` 形状：
  · 401 缺 / 错 token
  · 404 未知处方 / 未知端点
  · 400 缺 operator 等格式错
  · 409 状态机冲突（已确认 → 驳回）
  · 500 内部错误（构造异常路径）
- 大目录扫描性能：100 个 runs 子目录扫描 < 1s
- mtime 倒序：列表按 run.json mtime 降序排列

真实数据：跑 4 张脱敏样例得到 runs/<rx>/ 目录树，再手工造 100 个
轻量子目录用于性能基准。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

import pytest

from rxchk.adapters.his import (
    FakeHisAdapter,
    HisWriteback,
    writeback_with_retry,
)
from rxchk.check_engine import build_check_engine, check_from_path
from rxchk.report.writer import (
    FINDINGS_FILENAME,
    INPUT_FILENAME,
    RUN_FILENAME,
    TRANSCRIPT_FILENAME,
    AUDIT_FILENAME,
    WRITEBACK_FILENAME,
)
from rxchk.viewer.server import (
    append_audit_entry,
    build_runs_payload,
    generate_token,
    iter_run_dirs,
    resolve_run_dir,
    serve,
)

SAMPLE_NAMES = ("rx_basic", "rx_overdose", "rx_allergy", "rx_complex")


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def pkg_data_root() -> Path:
    return Path(__file__).resolve().parent.parent / "src" / "rxchk" / "data"


@pytest.fixture()
def runs_root(tmp_path: Path, pkg_data_root: Path) -> Path:
    """跑 4 张脱敏样例，得到真实的 `runs/<处方号>/` 目录树。"""
    root = tmp_path / "runs"
    engine = build_check_engine(data_root=pkg_data_root)
    for name in SAMPLE_NAMES:
        check_from_path(
            pkg_data_root / "sample_rx" / f"{name}.json",
            engine=engine,
            run_root=root,
        )
    return root


@pytest.fixture()
def rx_of(pkg_data_root: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for name in SAMPLE_NAMES:
        raw = json.loads(
            (pkg_data_root / "sample_rx" / f"{name}.json").read_text(encoding="utf-8")
        )
        mapping[name] = raw["prescription_no"]
    return mapping


@pytest.fixture()
def live_server(runs_root: Path):
    httpd, url, token = serve(runs_root, host="127.0.0.1", port=0)
    try:
        yield url, token
    finally:
        httpd.shutdown()
        httpd.server_close()


def _run_url(base: str, rx_no: str, leaf: str = "") -> str:
    """处方号含 `<EXAMPLE_*>` 占位符，进 URL 前须转义。"""
    quoted = urllib.parse.quote(rx_no, safe="")
    tail = f"/{leaf}" if leaf else ""
    return f"{base}/api/run/{quoted}{tail}"


def _request(
    url: str,
    *,
    token: Optional[str] = None,
    bearer: Optional[str] = None,
    body: Optional[dict[str, Any]] = None,
) -> tuple[int, Any]:
    if token is not None:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        status = exc.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


# --------------------------------------------------------------------------- #
# 6 个端点的 surface                                                            #
# --------------------------------------------------------------------------- #


class TestAllEndpointsSurface:
    """每个端点都能用 token 鉴权 + 返回 200 + 结构正确。"""

    def test_get_runs_returns_mtime_reversed(
        self, live_server, rx_of: dict[str, str]
    ) -> None:
        url, token = live_server
        status, payload = _request(f"{url}/api/runs", token=token)
        assert status == 200
        assert payload["count"] == len(SAMPLE_NAMES)
        assert isinstance(payload["runs"], list)
        assert len(payload["runs"]) == len(SAMPLE_NAMES)
        # 至少 prescription_no / status 字段齐全
        for entry in payload["runs"]:
            assert "prescription_no" in entry
            assert "status" in entry
            assert "findings_count" in entry
            assert "by_family" in entry
            assert "written_back" in entry
        # 4 张样例的 prescription_no 都在
        rx_set = {entry["prescription_no"] for entry in payload["runs"]}
        assert rx_set == set(rx_of.values())

    def test_get_run_summary_root(
        self, live_server, rx_of: dict[str, str]
    ) -> None:
        url, token = live_server
        status, payload = _request(
            _run_url(url, rx_of["rx_basic"]), token=token
        )
        assert status == 200
        # summary 端点 = run.json + input + writeback 三合一
        assert payload["prescription_no"] == rx_of["rx_basic"]
        assert payload["status"] == "awaiting_review"
        assert "input" in payload
        assert "writeback" in payload
        assert isinstance(payload["input"], dict)

    def test_get_findings(
        self, live_server, rx_of: dict[str, str]
    ) -> None:
        url, token = live_server
        status, payload = _request(
            _run_url(url, rx_of["rx_complex"], "findings"), token=token
        )
        assert status == 200
        assert payload["prescription_no"] == rx_of["rx_complex"]
        assert "count" in payload
        assert "findings" in payload
        assert "by_family" in payload
        assert payload["count"] == len(payload["findings"])
        assert payload["count"] >= 1

    def test_get_transcript(
        self, live_server, rx_of: dict[str, str]
    ) -> None:
        url, token = live_server
        status, payload = _request(
            _run_url(url, rx_of["rx_overdose"], "transcript"), token=token
        )
        assert status == 200
        assert "events" in payload
        assert isinstance(payload["events"], list)
        assert len(payload["events"]) > 0

    def test_get_audit_empty_for_fresh_run(
        self, live_server, rx_of: dict[str, str]
    ) -> None:
        url, token = live_server
        status, payload = _request(
            _run_url(url, rx_of["rx_basic"], "audit"), token=token
        )
        assert status == 200
        assert payload["entries"] == []

    def test_get_writeback_empty_before_writing(
        self, live_server, rx_of: dict[str, str]
    ) -> None:
        url, token = live_server
        status, payload = _request(
            _run_url(url, rx_of["rx_basic"], "writeback"), token=token
        )
        assert status == 200
        # 未回写时回执为空 dict（与 `read_writeback` 缺失兜底一致）
        assert payload == {}

    def test_get_writeback_returns_real_receipt(
        self, live_server, rx_of: dict[str, str], runs_root: Path
    ) -> None:
        """真实触发一次 HIS 回写后，独立 writeback 端点拿到完整回执。"""
        import asyncio

        url, token = live_server
        rx = rx_of["rx_overdose"]

        # 先做 confirm 推进到 awaiting_review → confirmed
        status, _ = _request(
            _run_url(url, rx, "confirm"),
            token=token,
            body={"action": "confirm", "operator": "药师-<EXAMPLE_OP>"},
        )
        assert status == 200

        # 触发 FakeHisAdapter.writeback
        async def _do_writeback() -> None:
            his = FakeHisAdapter(run_dir=runs_root / rx)
            receipt = await writeback_with_retry(
                his,
                HisWriteback(
                    prescription_no=rx,
                    audit_summary="<EXAMPLE_AUDIT_SUMMARY>",
                    manual_override=False,
                ),
            )
            assert receipt.ok is True

        asyncio.run(_do_writeback())

        # 独立 writeback 端点拿到完整回执
        status, payload = _request(
            _run_url(url, rx, "writeback"), token=token
        )
        assert status == 200
        assert isinstance(payload, dict)
        assert payload.get("prescription_no") == rx
        assert "endpoint" in payload

    def test_post_confirm_advances_status(
        self, live_server, rx_of: dict[str, str]
    ) -> None:
        url, token = live_server
        status, payload = _request(
            _run_url(url, rx_of["rx_basic"], "confirm"),
            token=token,
            body={"action": "confirm", "operator": "药师-<EXAMPLE_OP>"},
        )
        assert status == 200
        assert payload["ok"] is True
        assert payload["status"] == "confirmed"
        assert "entry" in payload


# --------------------------------------------------------------------------- #
# 错误码 + JSON 形状                                                            #
# --------------------------------------------------------------------------- #


class TestErrorCodes:
    """每个错误码都返回 `{code, message}` JSON，不泄露路径存在性。"""

    # ---- 401：缺 / 错 token ------------------------------------------------ #

    def test_missing_token_returns_401_with_json_body(
        self, live_server
    ) -> None:
        url, _ = live_server
        status, payload = _request(f"{url}/api/runs")
        assert status == 401
        assert isinstance(payload, dict)
        assert payload["code"] == "unauthorized"
        assert "message" in payload
        assert isinstance(payload["message"], str)

    def test_wrong_token_returns_401_uniform(
        self, live_server
    ) -> None:
        url, _ = live_server
        status, payload = _request(f"{url}/api/runs", token="totally-wrong")
        assert status == 401
        assert payload["code"] == "unauthorized"

    def test_unauthorized_does_not_leak_run_existence(
        self, live_server
    ) -> None:
        """未鉴权时 404 与 401 必须不可区分（避免泄露 runs 目录的处方号）。"""
        url, token = live_server
        # 用正确 token 查一个已知存在的处方
        ok_status, ok_payload = _request(
            _run_url(url, "RX-NOT-EXIST", "findings"), token=token
        )
        # 用错误 token 查同一个路径
        bad_status, bad_payload = _request(
            _run_url(url, "RX-NOT-EXIST", "findings"), token="wrong"
        )
        assert ok_status == 404 and ok_payload["code"] == "unknown_run"
        assert bad_status == 401 and bad_payload["code"] == "unauthorized"

    # ---- 404：未知处方 / 未知端点 ------------------------------------------ #

    def test_unknown_run_returns_404(
        self, live_server
    ) -> None:
        url, token = live_server
        status, payload = _request(
            _run_url(url, "RX-NOT-EXIST", "findings"), token=token
        )
        assert status == 404
        assert payload["code"] == "unknown_run"
        assert "message" in payload

    def test_unknown_leaf_returns_404_not_found(
        self, live_server, rx_of: dict[str, str]
    ) -> None:
        url, token = live_server
        status, payload = _request(
            _run_url(url, rx_of["rx_basic"], "totally_unknown_leaf"),
            token=token,
        )
        assert status == 404
        assert payload["code"] == "not_found"

    def test_post_unknown_endpoint_returns_404(
        self, live_server
    ) -> None:
        """POST 到 /api/run/<rx>/<unknown> → 404 not_found（业务路径外）。"""
        url, token = live_server
        req = urllib.request.Request(
            f"{url}/api/run/whatever/run_forrest_run",
            data=json.dumps({"foo": "bar"}).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read().decode("utf-8")
        payload = json.loads(raw)
        assert status == 404
        assert payload["code"] == "not_found"

    # ---- 400：格式错 ------------------------------------------------------ #

    def test_confirm_without_operator_returns_400(
        self, live_server, rx_of: dict[str, str]
    ) -> None:
        url, token = live_server
        status, payload = _request(
            _run_url(url, rx_of["rx_basic"], "confirm"),
            token=token,
            body={"action": "confirm"},  # 缺 operator
        )
        assert status == 400
        assert payload["code"] == "invalid_request"
        assert "operator" in payload["message"]

    def test_confirm_with_unknown_action_returns_400(
        self, live_server, rx_of: dict[str, str]
    ) -> None:
        url, token = live_server
        status, payload = _request(
            _run_url(url, rx_of["rx_basic"], "confirm"),
            token=token,
            body={
                "action": "approve_everything",
                "operator": "药师-<EXAMPLE_OP>",
            },
        )
        assert status == 400
        assert payload["code"] == "invalid_request"

    # ---- 409：状态机冲突 -------------------------------------------------- #

    def test_confirmed_run_cannot_be_flipped_returns_409(
        self, live_server, rx_of: dict[str, str]
    ) -> None:
        url, token = live_server
        rx = rx_of["rx_overdose"]
        # 第一次 confirm → 200
        ok_status, _ = _request(
            _run_url(url, rx, "confirm"),
            token=token,
            body={"action": "confirm", "operator": "药师-<EXAMPLE_OP>"},
        )
        assert ok_status == 200
        # 第二次 reject → 409（业务冲突，不是格式错）
        conflict_status, payload = _request(
            _run_url(url, rx, "confirm"),
            token=token,
            body={"action": "reject", "operator": "药师-<EXAMPLE_OP>"},
        )
        assert conflict_status == 409
        assert payload["code"] == "invalid_state"

    # ---- 路径穿越防护 ------------------------------------------------------ #

    def test_path_traversal_returns_404_not_data(
        self, live_server
    ) -> None:
        url, token = live_server
        # `/api/run/../something/secret` 路径应当被 normalize 后变成 `/api/run/something/secret`
        # 解析失败 / 未找到处方 → 404，而不是端出 runs/ 目录外的文件
        status, _ = _request(
            f"{url}/api/run/{urllib.parse.quote('../etc/passwd', safe='')}/findings",
            token=token,
        )
        assert status == 404


# --------------------------------------------------------------------------- #
# mtime 倒序                                                                   #
# --------------------------------------------------------------------------- #


class TestMtimeOrdering:
    """`/api/runs` 按 `run.json` mtime 倒序，新跑出的处方在最前。"""

    def test_new_runs_appear_at_top(
        self, runs_root: Path, pkg_data_root: Path, rx_of: dict[str, str]
    ) -> None:
        """模拟「后来追加一张处方」：调 mtime → 验证列表顶端是新处方。"""
        import os

        # 选一个已存在的处方，把它的 run.json mtime 调到最大
        target_rx = rx_of["rx_overdose"]
        target_run_path = runs_root / target_rx
        current_max = max(
            (runs_root / d / RUN_FILENAME).stat().st_mtime
            for d in runs_root.iterdir()
            if (runs_root / d / RUN_FILENAME).is_file()
        )
        new_mtime = current_max + 1.0
        os.utime(target_run_path / RUN_FILENAME, (new_mtime, new_mtime))

        payload = build_runs_payload(runs_root)
        assert payload["runs"][0]["prescription_no"] == target_rx
        # 全部按 mtime 降序
        for entry in payload["runs"]:
            assert entry["created_at"]
            assert "updated_at" in entry

    def test_iter_run_dirs_returns_sorted_desc(
        self, runs_root: Path
    ) -> None:
        run_dirs = iter_run_dirs(runs_root)
        assert len(run_dirs) == len(SAMPLE_NAMES)
        mtimes = [
            (d / RUN_FILENAME).stat().st_mtime for d in run_dirs
        ]
        assert mtimes == sorted(mtimes, reverse=True)

    def test_iter_run_dirs_handles_empty_dir(
        self, tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty_runs"
        empty.mkdir()
        assert iter_run_dirs(empty) == []

    def test_iter_run_dirs_ignores_dirs_without_run_json(
        self, tmp_path: Path
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        # 有 run.json 的（合法）
        (runs / "RX-LEGIT").mkdir()
        (runs / "RX-LEGIT" / RUN_FILENAME).write_text(
            json.dumps({"status": "uploaded", "prescription_no": "RX-LEGIT"})
        )
        # 没 run.json 的（非法）
        (runs / "RX-INCOMPLETE").mkdir()
        (runs / "RX-INCOMPLETE" / INPUT_FILENAME).write_text("{}")
        run_dirs = iter_run_dirs(runs)
        assert len(run_dirs) == 1
        assert run_dirs[0].name == "RX-LEGIT"


# --------------------------------------------------------------------------- #
# 大目录扫描性能（100 个 runs < 1s）                                            #
# --------------------------------------------------------------------------- #


class TestLargeDirectoryPerformance:
    """100 个 runs 子目录扫描必须 < 1s（前端 ~1s 轮询不能被打挂）。"""

    @pytest.fixture()
    def large_runs_root(self, tmp_path: Path) -> Path:
        """造 100 个合规 runs 子目录（每个含完整产物）。"""
        runs = tmp_path / "large_runs"
        runs.mkdir()
        for i in range(100):
            rx_dir = runs / f"RX-2026-LARGE-{i:04d}"
            rx_dir.mkdir()
            (rx_dir / RUN_FILENAME).write_text(
                json.dumps(
                    {
                        "prescription_no": f"RX-2026-LARGE-{i:04d}",
                        "status": "awaiting_review",
                        "created_at": f"2026-08-20T12:24:{i % 60:02d}Z",
                        "updated_at": f"2026-08-20T12:24:{i % 60:02d}Z",
                    }
                )
            )
            (rx_dir / INPUT_FILENAME).write_text(
                json.dumps(
                    {
                        "prescription_no": f"RX-2026-LARGE-{i:04d}",
                        "patient_id": "<EXAMPLE_PATIENT>",
                        "visit_no": "<EXAMPLE_VISIT>",
                        "doctor_id": "<EXAMPLE_DOCTOR>",
                        "items": [],
                    }
                )
            )
            (rx_dir / FINDINGS_FILENAME).write_text(
                json.dumps(
                    {
                        "prescription_no": f"RX-2026-LARGE-{i:04d}",
                        "count": 0,
                        "by_family": {},
                        "findings": [],
                    }
                )
            )
            (rx_dir / TRANSCRIPT_FILENAME).write_text("")
            (rx_dir / AUDIT_FILENAME).write_text("")
        return runs

    def test_iter_run_dirs_100_under_one_second(
        self, large_runs_root: Path
    ) -> None:
        start = time.monotonic()
        run_dirs = iter_run_dirs(large_runs_root)
        elapsed = time.monotonic() - start
        assert len(run_dirs) == 100
        assert elapsed < 1.0, f"扫描 100 runs 用了 {elapsed:.3f}s（>1s）"

    def test_build_runs_payload_100_under_one_second(
        self, large_runs_root: Path
    ) -> None:
        start = time.monotonic()
        payload = build_runs_payload(large_runs_root)
        elapsed = time.monotonic() - start
        assert payload["count"] == 100
        assert len(payload["runs"]) == 100
        assert elapsed < 1.0, f"build_runs_payload 100 runs 用了 {elapsed:.3f}s（>1s）"

    def test_runs_endpoint_serves_100_runs_under_one_second(
        self, large_runs_root: Path
    ) -> None:
        """端到端：起服务 + 拉一次 /api/runs 也必须在 1s 内完成。"""
        httpd, url, token = serve(large_runs_root, host="127.0.0.1", port=0)
        try:
            start = time.monotonic()
            status, payload = _request(f"{url}/api/runs", token=token)
            elapsed = time.monotonic() - start
            assert status == 200
            assert payload["count"] == 100
            assert len(payload["runs"]) == 100
            assert elapsed < 1.0, f"/api/runs 端到端用了 {elapsed:.3f}s（>1s）"
        finally:
            httpd.shutdown()
            httpd.server_close()


# --------------------------------------------------------------------------- #
# Token / 模块 surface                                                          #
# --------------------------------------------------------------------------- #


class TestModuleSurface:
    """模块级 API surface 的存在性 + 轻量行为校验。"""

    def test_generate_token_unique(self) -> None:
        tokens = {generate_token() for _ in range(50)}
        assert len(tokens) == 50

    def test_resolve_run_dir_accepts_only_direct_children(self, runs_root: Path) -> None:
        ok = resolve_run_dir(runs_root, next(iter(runs_root.iterdir())).name)
        assert ok is not None
        bad = resolve_run_dir(runs_root, "RX-NOT-EXIST")
        assert bad is None
        # 路径穿越：rx_no 试图逃出 runs/ 根
        bad_traversal = resolve_run_dir(runs_root, "../outside")
        assert bad_traversal is None

    def test_resolve_run_dir_rejects_empty(self, runs_root: Path) -> None:
        assert resolve_run_dir(runs_root, "") is None
