"""药师工作站本地 Viewer：stdlib HTTP 服务 + 启动 token 鉴权 + JSON 端点。

参考源（github_ref 代码证据）
- `strix/viewer/server.py: serve()` —— `ThreadingHTTPServer` +
  `secrets.token_urlsafe` 启动期 token + `authorized_url()` 打印可粘贴链接
- `strix/viewer/server.py: _make_handler / _handle_api / _send_json`
  —— 每请求现读磁盘的 JSON 端点，异常不杀工作线程
- `strix/viewer/server.py: _resolve_asset` —— 静态资源路径穿越防护

融合后的产品位置
----------------
源仓库把「一次扫描」的 run 目录端出来给浏览器轮询；本系统把 target 从
「一段代码」换成「一张处方」：端出来的是 `runs/<处方号>/` 下的字段证据卡
（`findings.json`）、事件流（`transcript.jsonl`）与药师操作日志
（`audit.jsonl`），并多一条写路径 —— 药师在浏览器里确认 / 驳回，
`POST /api/run/<rx>/confirm` 追加 `audit.jsonl` 并推进处方级状态。

为什么仍用 stdlib + 轮询
------------------------
- 药师工作站是院内单机场景，负载 = 端几个 JSON 文件，异步栈没有收益；
  少一个运行时依赖，信息科部署少一层审批
- ~1s 轮询没有服务端连接状态：核对跑完前端自然停轮询，锁屏 / 网络抖动
  恢复后不需要重连逻辑

鉴权取舍
--------
服务只绑 `127.0.0.1`，但仍要求 token：同一台工作站上的其他本地进程
（浏览器里的任意页面、其他账号的程序）都能连 `127.0.0.1:<port>`，
而处方与患者过敏史不该被「能连上端口」就读走。token 由启动进程打印，
只有拿到启动链接的药师能带上。
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import mimetypes
import secrets
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from rxchk.audit import (
    AUDIT_ACTIONS,
    AuditEntry,
    append_audit,
)
from rxchk.report.state import RunStateError
from rxchk.viewer.transcript import (
    is_run_dir,
    iter_run_dirs,
    read_audit,
    read_findings,
    read_input,
    read_run_summary,
    read_transcript,
    read_writeback,
    run_list_entry,
)

logger = logging.getLogger(__name__)


#: 操作 → 处方级目标状态由 `rxchk.audit` 维护；端点只负责读 + 写，
#: 状态机校验统一在 `append_audit` 入口（避免 HTTP 与 CLI 各自一份规则）。


# token 鉴权双渠道：`?token=` 给浏览器直连，`Authorization: Bearer`
# 给 curl / 脚本；后者避免把 token 写进 URL / 访问日志。两渠道等价。
_TOKEN_QUERY_KEY = "token"
_AUTH_HEADER = "Authorization"
_AUTH_SCHEME = "Bearer "


def _extract_supplied_token(
    query: dict[str, list[str]], headers: Any
) -> str:
    """从 query + headers 抽出来访者携带的 token 字符串；都缺则为空串。

    拆出来便于 `_require_token` 装饰器与 `_authorized` 共用同一段判定逻辑。
    """
    supplied = (query.get(_TOKEN_QUERY_KEY) or [""])[0]
    if not supplied:
        header = headers.get(_AUTH_HEADER) or ""
        if header.startswith(_AUTH_SCHEME):
            supplied = header[len(_AUTH_SCHEME) :].strip()
    return supplied or ""


def _check_authorization(supplied: str, expected: str) -> bool:
    """常数时间比较（避免按字符长度泄露信息）；都为空 → 失败。"""
    if not supplied or not expected:
        return False
    return secrets.compare_digest(supplied, expected)


def generate_token() -> str:
    """生成本次启动的一次性 token（进程级，不落盘、不复用）。

    用 `secrets.token_urlsafe(16)`：16 字节随机熵（约 128 bit），
    进程级一次性，**不**沿用、不持久化——重启 viewer 等同换 token。
    """
    return secrets.token_urlsafe(16)


def authorized_url(base_url: str, token: str) -> str:
    """拼出带 token 的可粘贴链接；启动期打印给药师的就是这一条。

    兼容旧 API（`base_url` 已包含 scheme://host:port）。
    新代码优先用 `build_authorized_url(host, port, token)` 三参形式。
    """
    return f"{base_url}/?{urlencode({'token': token})}"


def build_authorized_url(host: str, port: int, token: str) -> str:
    """三参版「host + port + token → 可粘贴链接」；启动期主路径。

    与 `authorized_url(base_url, token)` 区分：本函数自己拼出 base_url，
    调用方不必先拿 `http://127.0.0.1:<port>` 再二次包装。绑定 `127.0.0.1`
    的随机端口由 `serve()` 实际分配后再回填——`serve` 内部用本函数构造
    「Open: ...」链接。
    """
    base_url = f"http://{host}:{port}"
    return f"{base_url}/?{urlencode({'token': token})}"


def web_dir() -> Path:
    """前端单页目录（任务 11 落 index.html / app.js / style.css）。"""
    return Path(__file__).resolve().parent.parent / "web"


# 前端尚未落盘时的兜底页：明确告诉使用者 JSON 端点已经可用，
# 而不是给一个 404 白屏。
_PLACEHOLDER_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>用药处方字段对照校验系统</title></head>
<body style="font-family:system-ui;margin:40px;line-height:1.7">
<h1>用药处方字段对照校验系统</h1>
<p>服务已启动，字段证据卡数据接口可用：</p>
<ul>
<li><code>GET /api/runs</code></li>
<li><code>GET /api/run/&lt;处方号&gt;</code></li>
<li><code>GET /api/run/&lt;处方号&gt;/findings</code></li>
<li><code>GET /api/run/&lt;处方号&gt;/transcript</code></li>
<li><code>GET /api/run/&lt;处方号&gt;/audit</code></li>
<li><code>GET /api/run/&lt;处方号&gt;/writeback</code></li>
<li><code>POST /api/run/&lt;处方号&gt;/confirm</code></li>
</ul>
<p>请求需带启动时打印的 <code>?token=</code>。对照网格页面由后续任务补齐。</p>
</body></html>
"""


class _ViewerState:
    """一次 Viewer 进程的运行态：看哪个 runs 根、默认哪张处方、token。"""

    def __init__(self, run_dir: Path, *, token: Optional[str] = None) -> None:
        self.run_dir = run_dir.resolve()
        # 启动时指到某张处方目录，则历史列表 = 它的父目录（runs 根）；
        # 直接指到 runs 根（还没有任何处方）时就用它自己。
        self.base_dir = (
            self.run_dir.parent if is_run_dir(self.run_dir) else self.run_dir
        )
        self.assets_dir = web_dir()
        self.token = token or generate_token()


def resolve_run_dir(base_dir: Path, rx_no: str) -> Optional[Path]:
    """`<处方号>` → 真实核对目录；拒绝路径穿越与未知处方（返回 None）。"""
    if not rx_no:
        return None
    base = Path(base_dir).resolve()
    candidate = (base / rx_no).resolve()
    # 只接受 runs 根的直接子目录，且必须落过 run.json
    if candidate.parent != base or not is_run_dir(candidate):
        return None
    return candidate


def build_runs_payload(base_dir: Path) -> dict[str, Any]:
    """`/api/runs` 负载：按 `run.json` mtime 倒序的处方核对列表。"""
    run_dirs = iter_run_dirs(Path(base_dir))
    return {
        "count": len(run_dirs),
        "runs": [run_list_entry(d) for d in run_dirs],
    }


def append_audit_entry(
    run_dir: Path,
    *,
    action: str,
    operator: str,
    note: str = "",
    finding_id: Optional[str] = None,
) -> dict[str, Any]:
    """向 `audit.jsonl` 追加一条药师操作，并推进处方级状态。

    兼容层：转调 `rxchk.audit.append_audit`（任务 12 重构后唯一真实实现
    在 audit 模块；HTTP 端点 + 测试仍走这里以避免破坏既有调用方）。
    返回 dict 与原实现保持一致，便于前端 / 测试不改一行。
    """
    entry = append_audit(
        run_dir,
        action=action,
        operator=operator,
        note=note,
        finding_id=finding_id,
    )
    return _entry_to_dict(entry)


def _entry_to_dict(entry: AuditEntry) -> dict[str, Any]:
    """`AuditEntry` → 与旧实现字段顺序一致的 dict。"""
    payload: dict[str, Any] = {
        "ts": entry.ts,
        "action": entry.action,
        "operator": entry.operator,
        "note": entry.note,
        "manual_override": entry.manual_override,
    }
    if entry.finding_id is not None:
        payload["finding_id"] = entry.finding_id
    return payload


def _make_handler(state: _ViewerState) -> type[BaseHTTPRequestHandler]:
    # 鉴权装饰器工厂：闭包捕获 `state.token`，被装饰方法须由
    # `do_GET` / `do_POST` 入口先把 query 写到 `self._rxchk_query`
    # 再分发——这样 `_require_token` 能从同一位置抽 query / headers
    # 做判定，与原 `_authorized(query)` 行为完全一致。
    def _require_token(method: Callable[..., None]) -> Callable[..., None]:
        @functools.wraps(method)
        def _wrapper(self: "ViewerHandler", *args: Any, **kwargs: Any) -> None:
            query = getattr(self, "_rxchk_query", {}) or {}
            supplied = _extract_supplied_token(query, self.headers)
            if not _check_authorization(supplied, state.token):
                self._send_unauthorized()
                return
            return method(self, *args, **kwargs)

        return _wrapper

    class ViewerHandler(BaseHTTPRequestHandler):
        server_version = "RxFieldCheckViewer/1.0"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            logger.debug("viewer %s - %s", self.address_string(), format % args)

        # ---------------- 路由 ---------------- #

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            self._rxchk_query = query
            try:
                if parts.path.startswith("/api/"):
                    self._handle_api_get(parts.path)
                else:
                    self._handle_static(parts.path, query)
            except BrokenPipeError:
                # 浏览器在两次轮询之间关掉了页面，不是错误
                logger.debug("viewer 客户端提前断开：%s", parts.path)
            except Exception:
                logger.exception("viewer 请求失败：%s", parts.path)
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"code": "internal_error", "message": "服务内部错误"},
                )

        def do_POST(self) -> None:
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            self._rxchk_query = query
            try:
                if not parts.path.startswith("/api/"):
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"code": "not_found", "message": "未知端点"},
                    )
                    return
                segments = _api_segments(parts.path)
                if len(segments) == 3 and segments[0] == "run" and segments[2] == "confirm":
                    self._handle_confirm(segments[1])
                else:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"code": "not_found", "message": "未知端点"},
                    )
            except BrokenPipeError:
                logger.debug("viewer 客户端提前断开：POST %s", parts.path)
            except Exception:
                logger.exception("viewer 请求失败：POST %s", parts.path)
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"code": "internal_error", "message": "服务内部错误"},
                )

        @_require_token
        def _handle_api_get(self, path: str) -> None:
            segments = _api_segments(path)
            if segments == ["runs"]:
                self._send_json(HTTPStatus.OK, build_runs_payload(state.base_dir))
                return
            if not segments or segments[0] != "run" or len(segments) < 2:
                self._send_json(
                    HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "未知端点"}
                )
                return

            run_dir = resolve_run_dir(state.base_dir, segments[1])
            if run_dir is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"code": "unknown_run", "message": f"找不到处方核对记录：{segments[1]}"},
                )
                return

            leaf = segments[2] if len(segments) == 3 else ""
            if leaf == "":
                # 处方级 summary：把入参 + 回写回执一并发出来，
                # 单端点拉全概览（前端 runs-bar 切处方时不发散请求）。
                payload: Any = {
                    **read_run_summary(run_dir),
                    "input": read_input(run_dir),
                    "writeback": read_writeback(run_dir),
                }
            elif leaf == "findings":
                payload = read_findings(run_dir)
            elif leaf == "transcript":
                payload = {"events": read_transcript(run_dir)}
            elif leaf == "audit":
                payload = {"entries": read_audit(run_dir)}
            elif leaf == "writeback":
                # 独立端点：审计 / 飞检追溯时只拉回写回执，
                # 不必把整份 run.json + input 一起拉回来
                payload = read_writeback(run_dir)
            else:
                self._send_json(
                    HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "未知端点"}
                )
                return
            self._send_json(HTTPStatus.OK, payload)

        @_require_token
        def _handle_confirm(self, rx_no: str) -> None:
            run_dir = resolve_run_dir(state.base_dir, rx_no)
            if run_dir is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"code": "unknown_run", "message": f"找不到处方核对记录：{rx_no}"},
                )
                return
            body = self._read_body()
            action = str(body.get("action") or "").strip()
            operator = str(body.get("operator") or "").strip()
            note = str(body.get("note") or "")
            finding_id = body.get("finding_id")
            try:
                entry = append_audit_entry(
                    run_dir,
                    action=action,
                    operator=operator,
                    note=note,
                    finding_id=str(finding_id) if finding_id else None,
                )
            except RunStateError as exc:
                # `RunStateError` 是 ValueError 的子类，必须排在前面：
                # 已确认 / 已回写的处方不能再改结论，属业务冲突而非格式错
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"code": "invalid_state", "message": str(exc)},
                )
                return
            except ValueError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"code": "invalid_request", "message": str(exc)},
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "entry": entry,
                    "status": read_run_summary(run_dir).get("status"),
                },
            )

        # ---------------- 鉴权 ---------------- #
        # 鉴权判定统一走 `_require_token` 装饰器（已在 `_handle_api_get` /
        # `_handle_confirm` 上挂）；此处的 `_authorized` 保留以兼容老
        # 调用方（比如 `test_viewer_api.py` 直接调），其内部仍走同一
        # `_extract_supplied_token` / `_check_authorization`，行为等价。
        def _authorized(self, query: dict[str, list[str]]) -> bool:
            supplied = _extract_supplied_token(query, self.headers)
            return _check_authorization(supplied, state.token)

        def _send_unauthorized(self) -> None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"code": "unauthorized", "message": "缺少或错误的 token"},
            )

        # ---------------- 静态资源 ---------------- #

        def _handle_static(self, path: str, query: dict[str, list[str]]) -> None:
            target = self._resolve_asset(path)
            if target is None:
                index = state.assets_dir / "index.html"
                if index.is_file():
                    target = index
                else:
                    self._send_html(HTTPStatus.OK, _PLACEHOLDER_HTML)
                    return
            content = target.read_bytes()
            content_type, _ = mimetypes.guess_type(str(target))
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type", content_type or "application/octet-stream"
            )
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _resolve_asset(self, path: str) -> Optional[Path]:
            rel = unquote(path).lstrip("/")
            if not rel or rel.endswith("/"):
                return None
            root = state.assets_dir.resolve()
            candidate = (root / rel).resolve()
            # 路径穿越防护：绝不端出前端目录之外的文件
            if root != candidate and root not in candidate.parents:
                logger.warning("viewer 拒绝路径穿越：%s", path)
                return None
            return candidate if candidate.is_file() else None

        # ---------------- 响应辅助 ---------------- #

        def _read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return {}
            return body if isinstance(body, dict) else {}

        def _send_json(self, status: HTTPStatus, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, status: HTTPStatus, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ViewerHandler


def _api_segments(path: str) -> list[str]:
    """`/api/run/<rx>/findings` → `["run", "<rx>", "findings"]`（已解码）。"""
    rel = path[len("/api/") :] if path.startswith("/api/") else path.lstrip("/")
    return [unquote(seg) for seg in rel.split("/") if seg]


def serve(
    run_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = False,
    token: Optional[str] = None,
) -> tuple[ThreadingHTTPServer, str, str]:
    """后台线程启动 Viewer；返回 `(server, base_url, token)`。

    默认绑 `127.0.0.1` 的随机端口（`port=0`）：药师工作站不需要对外暴露，
    随机端口也避免与院内其他自建服务撞车。指定端口被占用时退回随机端口，
    而不是让 `rxchk view` 直接失败。

    调用方负责 `shutdown()` / `server_close()`（见 `rxchk.cli.cmd_view`）。
    """
    state = _ViewerState(Path(run_dir), token=token)
    handler = _make_handler(state)

    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError:
        if port == 0:
            raise
        logger.info("端口 %s 不可用，改用随机端口", port)
        httpd = ThreadingHTTPServer((host, 0), handler)

    httpd.daemon_threads = True
    bound_port = int(httpd.server_address[1])
    url = f"http://{host}:{bound_port}"

    thread = threading.Thread(
        target=httpd.serve_forever, name="rxchk-viewer", daemon=True
    )
    thread.start()

    if open_browser:
        _open_browser(build_authorized_url(host, bound_port, state.token))

    return httpd, url, state.token


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 —— 拉浏览器是尽力而为，失败不影响服务
        logger.debug("无法打开浏览器：%s", url, exc_info=True)


def build_arg_parser() -> argparse.ArgumentParser:
    """`python -m rxchk.viewer.server` 独立运行的 CLI 参数解析。

    默认行为：起 viewer 监听随机端口，主线程常驻（按 Ctrl-C 退出）。
    `--serve-seconds` 走与 `rxchk.cli` 同款的自动停机路径（CI / 冒烟
    用）；`--open-browser` 拉浏览器显示 Open: 链接。
    """
    parser = argparse.ArgumentParser(
        prog="python -m rxchk.viewer.server",
        description="用药处方字段对照校验系统 — 本地 Viewer 独立启动",
    )
    parser.add_argument(
        "run_dir",
        type=Path,
        help="runs 根目录（含一组处方核对子目录）或单张处方目录",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="绑定 host（默认 127.0.0.1）"
    )
    parser.add_argument(
        "--port", type=int, default=0, help="绑定端口（0 = 随机端口）"
    )
    parser.add_argument(
        "--serve-seconds",
        type=float,
        default=0.0,
        help=">0 时 viewer 到时自动 shutdown（CI / 冒烟用）",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="启动后调用浏览器打开 Open: 链接（默认不拉浏览器）",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """`python -m rxchk.viewer.server` 入口。

    启动期必打一条 `Open: http://127.0.0.1:<port>/?token=<secret>`
    链接——这是药师把字段证据卡网格拉到浏览器的唯一通路，必须可读。

    `serve()` 内部已经在后台线程起 `httpd.serve_forever()`，本函数
    主线程只做两件事：
    1. 打印 Open: 链接（flush 立即可见）
    2. 阻塞到 `--serve-seconds` 到时或 Ctrl-C，期间用 `time.sleep` 轮询
       避免与 `serve_forever()` 二次启动冲突
    """
    import time

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    httpd, _url, token = serve(
        args.run_dir,
        host=args.host,
        port=args.port,
        open_browser=args.open_browser,
    )
    bound_port = int(httpd.server_address[1])
    open_link = build_authorized_url(args.host, bound_port, token)
    # flush=True：stdout 是块缓冲时（重定向 / 管道）仍能立刻拿到链接
    print(f"Open: {open_link}", flush=True)

    serve_seconds = float(args.serve_seconds or 0.0)
    deadline = time.monotonic() + serve_seconds if serve_seconds > 0 else None
    try:
        while deadline is None or time.monotonic() < deadline:
            time.sleep(0.2)
    except KeyboardInterrupt:
        logger.info("viewer 收到 Ctrl-C，准备退出")
    finally:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
    return 0


__all__ = [
    "AUDIT_ACTIONS",
    "append_audit_entry",
    "authorized_url",
    "build_arg_parser",
    "build_authorized_url",
    "build_runs_payload",
    "generate_token",
    "main",
    "resolve_run_dir",
    "serve",
    "web_dir",
]


if __name__ == "__main__":
    sys.exit(main())
