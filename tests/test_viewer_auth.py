"""Viewer 后端 token 鉴权 + ThreadingHTTPServer 深度测试（任务 24）。

覆盖：
- `generate_token`：进程级一次性、长度、唯一性（50 个互不相同）
- `build_authorized_url(host, port, token)` 三参形式：URL 拼装正确
- `authorized_url(base_url, token)` 二参形式：与三参形式输出兼容
- `_require_token` 装饰器（经由 do_GET / do_POST 端到端触发）：
  - 缺失 token → 401 + JSON `{"code": "unauthorized", ...}`
  - 错误 token → 401 + 同一 JSON 结构（不区分原因，避免泄露）
  - 正确 token（?token= 渠道）→ 200
  - 正确 token（Authorization: Bearer 渠道）→ 200
  - 缺失 + 错误 token 走同一路径（响应体相同）
- 并发 10 客户端同时握手：每个拿到 200，无 token 串扰
- `main()` 启动期打印 `Open: http://127.0.0.1:<port>/?token=<secret>`
- `main()` `--serve-seconds` 自动停机路径
- `build_arg_parser` 接受合法入参

源产品能力与代码证据（github_ref）
-----------------------------------
- `strix/viewer/server.py: serve / _make_handler / _send_unauthorized` —
  启动期 `secrets.token_urlsafe` 一次性 token + 双渠道鉴权。本任务
  把鉴权判定抽成 `_extract_supplied_token` + `_check_authorization`
  两条纯函数 + `_require_token` 装饰器，便于测试直接打桩；同时保
  留原 `_authorized(query)` 兼容层让既有端点 / 测试一行不动。
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Optional

import pytest

from rxchk.check_engine import build_check_engine, check_from_path
from rxchk.viewer.server import (
    authorized_url,
    build_arg_parser,
    build_authorized_url,
    generate_token,
    main,
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
# generate_token                                                              #
# --------------------------------------------------------------------------- #


def test_generate_token_returns_non_empty_string() -> None:
    token = generate_token()
    assert isinstance(token, str)
    assert len(token) >= 16


def test_generate_token_is_unique_per_call() -> None:
    tokens = {generate_token() for _ in range(50)}
    assert len(tokens) == 50


def test_generate_token_is_urlsafe_base64() -> None:
    """`secrets.token_urlsafe(16)` 输出 URL-safe base64；不应含 `+/=` 等不安全字符。"""
    token = generate_token()
    assert re.fullmatch(r"[A-Za-z0-9_\-]+", token), token


# --------------------------------------------------------------------------- #
# build_authorized_url（3 参形式）                                             #
# --------------------------------------------------------------------------- #


def test_build_authorized_url_three_arg_form() -> None:
    url = build_authorized_url("127.0.0.1", 8080, "abc123")
    assert url == "http://127.0.0.1:8080/?token=abc123"


def test_build_authorized_url_with_random_port() -> None:
    """随机端口（>0）也要正确拼进 URL，便于 `serve` 实际绑定后回填。"""
    url = build_authorized_url("127.0.0.1", 0, "tok")
    assert url == "http://127.0.0.1:0/?token=tok"


def test_build_authorized_url_url_encodes_token() -> None:
    """URL 不安全字符（如 `+` / `=` 出现在 token 中——token_urlsafe 一般不含）也能编码。"""
    url = build_authorized_url("127.0.0.1", 8080, "a+b/c=")
    # `urllib.parse.quote` 默认不编码 `/`；但 `urlencode` 走 quote_plus
    # 编码 `+` 为 `%2B`、`/` 为 `%2F`、`=` 为 `%3D`
    assert "token=a%2Bb%2Fc%3D" in url


def test_build_authorized_url_consistent_with_authorized_url() -> None:
    """两函数对同一 host:port+token 应输出同一条链接（兼容旧调用方）。"""
    three = build_authorized_url("127.0.0.1", 9999, "xyz")
    two = authorized_url("http://127.0.0.1:9999", "xyz")
    assert three == two


# --------------------------------------------------------------------------- #
# _require_token 装饰器 —— 经 do_GET / do_POST 端到端验证                       #
# --------------------------------------------------------------------------- #


def test_require_token_missing_returns_401(live_server) -> None:
    url, _token = live_server
    status, payload = _request(f"{url}/api/runs")
    assert status == 401
    assert payload["code"] == "unauthorized"
    assert "message" in payload


def test_require_token_wrong_returns_401(live_server) -> None:
    url, _token = live_server
    status, payload = _request(f"{url}/api/runs", token="not-the-token")
    assert status == 401
    assert payload["code"] == "unauthorized"


def test_require_token_query_authorized(live_server) -> None:
    url, token = live_server
    status, payload = _request(f"{url}/api/runs", token=token)
    assert status == 200
    assert payload["count"] == len(SAMPLE_NAMES)


def test_require_token_bearer_authorized(live_server) -> None:
    """`Authorization: Bearer` 渠道：curl / 脚本免把 token 写进 URL。"""
    url, token = live_server
    status, payload = _request(f"{url}/api/runs", bearer=token)
    assert status == 200
    assert payload["count"] == len(SAMPLE_NAMES)


def test_require_token_empty_token_returns_401(live_server) -> None:
    """`?token=`（空字符串）也按缺失处理。"""
    url, _token = live_server
    status, payload = _request(f"{url}/api/runs", token="")
    assert status == 401
    assert payload["code"] == "unauthorized"


def test_require_token_post_confirm_requires_token(live_server, runs_root: Path) -> None:
    """`POST /api/run/<rx>/confirm` 走 `_require_token`：缺 token 必 401。"""
    url, _token = live_server
    status, payload = _request(
        f"{url}/api/run/SOME-RX/confirm",
        body={"action": "confirm", "operator": "药师-<EXAMPLE_OP>"},
    )
    assert status == 401
    assert payload["code"] == "unauthorized"


def test_require_token_post_confirm_authorizes(live_server, rx_of=None) -> None:
    """POST 走正确 token：进入真实 `_handle_confirm`（未必 200，但绝非 401）。"""
    url, token = live_server
    status, _payload = _request(
        f"{url}/api/run/RX-2026-<EXAMPLE>/confirm",
        token=token,
        body={"action": "confirm", "operator": "药师-<EXAMPLE_OP>"},
    )
    # 404（处方不存在）也是「鉴权通过」的证据——不是 401
    assert status != 401


def test_require_token_unauthorized_does_not_leak_existence(live_server) -> None:
    """缺 / 错 token 永远 401，不区分「路径错」与「鉴权错」，避免泄露。"""
    url, _token = live_server
    bad_path = f"{url}/api/run/RX-DOES-NOT-EXIST/findings"
    s_missing, p_missing = _request(bad_path)
    s_wrong, p_wrong = _request(bad_path, token="bogus")
    assert s_missing == 401 == s_wrong
    assert p_missing["code"] == p_wrong["code"] == "unauthorized"


# --------------------------------------------------------------------------- #
# 并发 10 客户端握手                                                           #
# --------------------------------------------------------------------------- #


def test_ten_concurrent_clients_handshake(live_server) -> None:
    """10 个客户端同时拉 `/api/runs`：每个都拿到 200，不串 token / 不 401。"""
    url, token = live_server
    results: dict[int, int] = {}
    lock = threading.Lock()

    def poll(idx: int) -> None:
        # 半数走 ?token=，半数走 Bearer——两条渠道都并发验证
        if idx % 2 == 0:
            status, _ = _request(f"{url}/api/runs", token=token)
        else:
            status, _ = _request(f"{url}/api/runs", bearer=token)
        with lock:
            results[idx] = status

    threads = [threading.Thread(target=poll, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(results) == 10
    for idx, status in sorted(results.items()):
        assert status == 200, f"第 {idx} 个客户端 status={status}"


def test_ten_concurrent_clients_mixed_token_validity(live_server) -> None:
    """10 个客户端各带不同 token（仅 1 个正确）：正确的 200，错的 401，无串扰。"""
    url, token = live_server
    correct_idx = 4
    results: dict[int, int] = {}
    lock = threading.Lock()

    def poll(idx: int) -> None:
        t = token if idx == correct_idx else f"bogus-{idx}"
        status, _ = _request(f"{url}/api/runs", token=t)
        with lock:
            results[idx] = status

    threads = [threading.Thread(target=poll, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(results) == 10
    for idx, status in sorted(results.items()):
        if idx == correct_idx:
            assert status == 200
        else:
            assert status == 401


# --------------------------------------------------------------------------- #
# main() 启动期打印 + 自动停机                                                  #
# --------------------------------------------------------------------------- #


def test_main_prints_open_link(runs_root: Path) -> None:
    """`main()` 启动期必打 `Open: http://127.0.0.1:<port>/?token=<secret>`。"""
    buf = io.StringIO()
    httpd_holder: dict[str, Any] = {}
    thread_holder: dict[str, threading.Thread] = {}

    def _run() -> None:
        # main() 内捕获 KeyboardInterrupt 走 server_close 退出
        with redirect_stdout(buf):
            try:
                main([str(runs_root), "--serve-seconds", "0.6"])
            except SystemExit as exc:  # main() 内部不会主动 exit
                httpd_holder["exit_code"] = exc.code

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    # 给 main() 一点时间把 `Open: ...` 打到 buf
    time.sleep(0.4)
    try:
        # 找到后台 httpd 实例，强制 shutdown 让 main() 走 finally
        # 由于 main() 用 serve_forever() 阻塞，这里另起一条 server 查找
        # 端口来关掉它：直接通过 buf 里的 port 拼 URL 验证
        output = buf.getvalue()
    finally:
        thread.join(timeout=8)

    output = buf.getvalue()
    assert "Open:" in output, output
    match = re.search(r"Open:\s+(http://127\.0\.0\.1:\d+/\?token=[\w\-]+)", output)
    assert match, f"启动期打印格式不对：{output!r}"
    link = match.group(1)

    # 链接能解析为合法 URL；端口与 token 各自满足长度约束
    parsed = urllib.parse.urlparse(link)
    assert parsed.scheme == "http"
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port is not None and parsed.port > 0
    token_in_link = urllib.parse.parse_qs(parsed.query)["token"][0]
    assert len(token_in_link) >= 16


def test_main_with_serve_seconds_exits_cleanly(runs_root: Path) -> None:
    """`--serve-seconds 0.3` 让 main() 走 finally.server_close() 干净退出。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main([str(runs_root), "--serve-seconds", "0.3"])
    assert rc == 0
    assert "Open:" in buf.getvalue()


def test_main_arg_parser_accepts_known_flags() -> None:
    """`build_arg_parser` 接受所有文档化的 flag，验证 CLI 表面。"""
    parser = build_arg_parser()
    args = parser.parse_args(
        ["/tmp/runs", "--host", "127.0.0.1", "--port", "9999", "--open-browser"]
    )
    assert args.run_dir == Path("/tmp/runs")
    assert args.host == "127.0.0.1"
    assert args.port == 9999
    assert args.open_browser is True
    assert args.serve_seconds == 0.0  # 默认


def test_main_via_python_module_subprocess(runs_root: Path) -> None:
    """`python -m rxchk.viewer.server` 子进程路径：能跑出 Open: 链接。"""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "rxchk.viewer.server",
            str(runs_root),
            "--serve-seconds",
            "0.6",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        stdout, _ = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert "Open:" in stdout, stdout
    assert "http://127.0.0.1:" in stdout
