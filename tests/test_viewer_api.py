"""Viewer HTTP 服务测试（任务 10）。

覆盖：
- token 鉴权：缺失 / 错误 → 401；`?token=` 与 `Authorization: Bearer` 双渠道通过
- JSON 端点：`/api/runs`、`/api/run/<rx>`、`.../findings`、`.../transcript`、
  `.../audit`、`POST .../confirm`
- 未知处方 → 404；路径穿越（`../`）→ 不端出目录外文件
- `/api/runs` 按 `run.json` mtime 倒序
- 确认 / 驳回 → `audit.jsonl` append-only + 处方级状态推进
- 并发 5 客户端同时轮询：各拿各的处方，不串数据

真实数据：run 目录由 `rxchk check` 主路径跑 4 张脱敏样例处方产生，
不手搓 findings.json —— 端点读的就是产品真实落盘结构。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

import pytest

from rxchk.check_engine import build_check_engine, check_from_path
from rxchk.report.writer import AUDIT_FILENAME, RUN_FILENAME
from rxchk.viewer.server import (
    append_audit_entry,
    authorized_url,
    build_runs_payload,
    generate_token,
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
    """样例文件名 → 真实处方号（run 目录名）。"""
    mapping: dict[str, str] = {}
    for name in SAMPLE_NAMES:
        raw = json.loads(
            (pkg_data_root / "sample_rx" / f"{name}.json").read_text(encoding="utf-8")
        )
        mapping[name] = raw["prescription_no"]
    return mapping


@pytest.fixture()
def live_server(runs_root: Path):
    """启动 Viewer，测试结束关停。"""
    httpd, url, token = serve(runs_root, host="127.0.0.1", port=0)
    try:
        yield url, token
    finally:
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------- #
# HTTP 辅助                                                                    #
# --------------------------------------------------------------------------- #


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
    """发一条请求；返回 `(status, 解析后的 JSON 或原始文本)`。"""
    if token is not None:
        url = f"{url}{'&' if '?' in url else '?'}token={token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
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
# token 鉴权                                                                    #
# --------------------------------------------------------------------------- #


def test_missing_token_returns_401(live_server) -> None:
    url, _token = live_server
    status, payload = _request(f"{url}/api/runs")
    assert status == 401
    assert payload["code"] == "unauthorized"


def test_wrong_token_returns_401(live_server) -> None:
    url, _token = live_server
    status, payload = _request(f"{url}/api/runs", token="not-the-token")
    assert status == 401
    assert payload["code"] == "unauthorized"


def test_query_token_authorizes(live_server) -> None:
    url, token = live_server
    status, payload = _request(f"{url}/api/runs", token=token)
    assert status == 200
    assert payload["count"] == len(SAMPLE_NAMES)


def test_bearer_token_authorizes(live_server) -> None:
    """curl / 脚本走 Authorization 头，不必把 token 放进 URL（免落日志）。"""
    url, token = live_server
    status, payload = _request(f"{url}/api/runs", bearer=token)
    assert status == 200
    assert payload["count"] == len(SAMPLE_NAMES)


def test_generate_token_is_unguessable_and_unique() -> None:
    tokens = {generate_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= 16 for t in tokens)


def test_authorized_url_carries_token() -> None:
    assert authorized_url("http://127.0.0.1:8080", "abc123") == (
        "http://127.0.0.1:8080/?token=abc123"
    )


# --------------------------------------------------------------------------- #
# JSON 端点                                                                     #
# --------------------------------------------------------------------------- #


def test_runs_listing_sorted_by_mtime_desc(runs_root: Path) -> None:
    payload = build_runs_payload(runs_root)
    assert payload["count"] == len(SAMPLE_NAMES)
    mtimes = [
        (runs_root / entry["run_name"] / RUN_FILENAME).stat().st_mtime
        for entry in payload["runs"]
    ]
    assert mtimes == sorted(mtimes, reverse=True)
    for entry in payload["runs"]:
        assert entry["prescription_no"]
        assert entry["status"] == "awaiting_review"
        assert isinstance(entry["findings_count"], int)


def test_run_detail_endpoint(live_server, rx_of: dict[str, str]) -> None:
    url, token = live_server
    status, payload = _request(_run_url(url, rx_of["rx_basic"]), token=token)
    assert status == 200
    assert payload["status"] == "awaiting_review"
    assert payload["input"]["items"], "详情端点须带回入参处方，药师要对着原方看"


def test_findings_endpoint_returns_evidence_cards(
    live_server, rx_of: dict[str, str]
) -> None:
    url, token = live_server
    status, payload = _request(
        _run_url(url, rx_of["rx_overdose"], "findings"), token=token
    )
    assert status == 200
    assert payload["count"] == len(payload["findings"])
    assert payload["count"] > 0, "超剂量样例必须有命中"
    card = payload["findings"][0]
    for key in ("family", "field", "rule_id", "severity", "hit_explanation"):
        assert key in card, f"证据卡缺字段 {key}"


def test_transcript_endpoint(live_server, rx_of: dict[str, str]) -> None:
    url, token = live_server
    status, payload = _request(
        _run_url(url, rx_of["rx_allergy"], "transcript"), token=token
    )
    assert status == 200
    events = payload["events"]
    assert events, "transcript 应含 input_received / state_change / agent_finish 事件"
    assert {e["event"] for e in events} & {"agent_finish", "state_change"}


def test_audit_endpoint_starts_empty(live_server, rx_of: dict[str, str]) -> None:
    url, token = live_server
    status, payload = _request(_run_url(url, rx_of["rx_basic"], "audit"), token=token)
    assert status == 200
    assert payload["entries"] == []


def test_unknown_run_returns_404(live_server) -> None:
    url, token = live_server
    status, payload = _request(_run_url(url, "RX-NOT-EXIST", "findings"), token=token)
    assert status == 404
    assert payload["code"] == "unknown_run"


def test_unknown_endpoint_returns_404(live_server, rx_of: dict[str, str]) -> None:
    url, token = live_server
    status, payload = _request(
        _run_url(url, rx_of["rx_basic"], "whatever"), token=token
    )
    assert status == 404
    assert payload["code"] == "not_found"


def test_unauthorized_beats_not_found(live_server) -> None:
    """没 token 时连「这个处方存不存在」都不该泄露。"""
    url, _token = live_server
    status, payload = _request(_run_url(url, "RX-NOT-EXIST", "findings"))
    assert status == 401
    assert payload["code"] == "unauthorized"


# --------------------------------------------------------------------------- #
# 路径穿越                                                                     #
# --------------------------------------------------------------------------- #


def test_resolve_run_dir_rejects_traversal(
    runs_root: Path, rx_of: dict[str, str]
) -> None:
    assert resolve_run_dir(runs_root, "../../etc") is None
    assert resolve_run_dir(runs_root, f"{rx_of['rx_basic']}/..") is None
    assert resolve_run_dir(runs_root, "") is None
    assert (
        resolve_run_dir(runs_root, rx_of["rx_basic"])
        == (runs_root / rx_of["rx_basic"]).resolve()
    )


def test_static_traversal_does_not_leak_files(live_server, tmp_path: Path) -> None:
    """`/../` 拿不到前端目录之外的文件；退回首页而不是端出文件内容。"""
    secret = tmp_path / "secret.txt"
    secret.write_text("患者过敏史", encoding="utf-8")
    url, _token = live_server
    status, payload = _request(f"{url}/../../../{secret.name}")
    assert status == 200
    assert "患者过敏史" not in str(payload)


# --------------------------------------------------------------------------- #
# 药师确认 / 驳回                                                               #
# --------------------------------------------------------------------------- #


def test_confirm_appends_audit_and_advances_state(
    live_server, runs_root: Path, rx_of: dict[str, str]
) -> None:
    url, token = live_server
    rx = rx_of["rx_overdose"]
    status, payload = _request(
        _run_url(url, rx, "confirm"),
        token=token,
        body={
            "action": "confirm",
            "operator": "药师-<EXAMPLE_OP>",
            "note": "已联系开方医师",
        },
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["status"] == "confirmed"

    lines = (
        (runs_root / rx / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
    )
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["action"] == "confirm"
    assert entry["operator"] == "药师-<EXAMPLE_OP>"
    assert entry["manual_override"] is False
    assert entry["ts"]

    run_record = json.loads(
        (runs_root / rx / RUN_FILENAME).read_text(encoding="utf-8")
    )
    assert run_record["status"] == "confirmed"


def test_reject_marks_prescription_overridden(
    live_server, rx_of: dict[str, str]
) -> None:
    url, token = live_server
    status, payload = _request(
        _run_url(url, rx_of["rx_allergy"], "confirm"),
        token=token,
        body={
            "action": "reject",
            "operator": "药师-<EXAMPLE_OP>",
            "note": "误报，已核对过敏史",
        },
    )
    assert status == 200
    assert payload["status"] == "overridden"


def test_manual_override_flag_is_recorded(
    runs_root: Path, rx_of: dict[str, str]
) -> None:
    """规则族未覆盖时的人工放行须留 `manual_override=true`（回写时带走）。"""
    entry = append_audit_entry(
        runs_root / rx_of["rx_complex"],
        action="manual_override",
        operator="药师-<EXAMPLE_OP>",
        note="配伍族规则未启用，人工放行",
    )
    assert entry["manual_override"] is True


def test_audit_is_append_only(
    live_server, runs_root: Path, rx_of: dict[str, str]
) -> None:
    """多条操作逐条追加，先前的行不被重写 —— 飞检要能逐条回放。"""
    url, token = live_server
    rx = rx_of["rx_complex"]
    for finding_id in ("f-1", "f-2"):
        status, _ = _request(
            _run_url(url, rx, "confirm"),
            token=token,
            body={
                "action": "confirm",
                "operator": "药师-<EXAMPLE_OP>",
                "finding_id": finding_id,
            },
        )
        assert status == 200
    lines = (
        (runs_root / rx / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
    )
    assert [json.loads(x)["finding_id"] for x in lines] == ["f-1", "f-2"]


def test_confirm_rejects_unknown_action(live_server, rx_of: dict[str, str]) -> None:
    url, token = live_server
    status, payload = _request(
        _run_url(url, rx_of["rx_basic"], "confirm"),
        token=token,
        body={"action": "approve_everything", "operator": "药师-<EXAMPLE_OP>"},
    )
    assert status == 400
    assert payload["code"] == "invalid_request"


def test_confirm_requires_operator(live_server, rx_of: dict[str, str]) -> None:
    """审计日志必须能追到人，缺 operator 直接拒收。"""
    url, token = live_server
    status, payload = _request(
        _run_url(url, rx_of["rx_basic"], "confirm"),
        token=token,
        body={"action": "confirm"},
    )
    assert status == 400
    assert "operator" in payload["message"]


def test_confirm_on_unknown_run_returns_404(live_server) -> None:
    url, token = live_server
    status, payload = _request(
        _run_url(url, "RX-NOT-EXIST", "confirm"),
        token=token,
        body={"action": "confirm", "operator": "药师-<EXAMPLE_OP>"},
    )
    assert status == 404
    assert payload["code"] == "unknown_run"


def test_confirmed_prescription_cannot_be_flipped(
    live_server, runs_root: Path, rx_of: dict[str, str]
) -> None:
    """已确认的处方不能再改成驳回：这是业务冲突（409），不是格式错。"""
    url, token = live_server
    rx = rx_of["rx_overdose"]
    ok_status, _ = _request(
        _run_url(url, rx, "confirm"),
        token=token,
        body={"action": "confirm", "operator": "药师-<EXAMPLE_OP>"},
    )
    assert ok_status == 200
    status, payload = _request(
        _run_url(url, rx, "confirm"),
        token=token,
        body={"action": "reject", "operator": "药师-<EXAMPLE_OP>"},
    )
    assert status == 409
    assert payload["code"] == "invalid_state"

    # 被拒绝的操作不留痕：否则 audit.jsonl 里会出现一条从未生效的「驳回」
    lines = (
        (runs_root / rx / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
    )
    assert [json.loads(x)["action"] for x in lines] == ["confirm"]


# --------------------------------------------------------------------------- #
# 并发                                                                         #
# --------------------------------------------------------------------------- #


def test_five_concurrent_clients_poll_consistently(
    live_server, rx_of: dict[str, str]
) -> None:
    """5 个药师工作站同时 ~1s 轮询：各拿各的处方，不串数据、不 500。"""
    url, token = live_server
    expected = [rx_of[SAMPLE_NAMES[i % len(SAMPLE_NAMES)]] for i in range(5)]
    results: dict[int, tuple[int, str]] = {}
    lock = threading.Lock()

    def poll(idx: int) -> None:
        status, payload = _request(
            _run_url(url, expected[idx], "findings"), token=token
        )
        with lock:
            results[idx] = (status, payload.get("prescription_no", ""))

    threads = [threading.Thread(target=poll, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(results) == 5
    for idx, (status, rx_no) in sorted(results.items()):
        assert status == 200
        assert rx_no == expected[idx], f"第 {idx} 个客户端拿到了别人的处方"
