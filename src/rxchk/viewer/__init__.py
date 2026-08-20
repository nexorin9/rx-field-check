"""本地 Viewer：药师工作站看字段证据卡网格的最小 HTTP 服务。

模块划分
--------
- `rxchk.viewer.transcript` —— 直读 `runs/<rx_no>/` 磁盘产物（run.json /
  findings.json / transcript.jsonl / audit.jsonl / writeback.json），
  每次请求现读，不缓存：同一个服务既能看在跑的处方，也能看已归档的处方
- `rxchk.viewer.server` —— stdlib `ThreadingHTTPServer` + 启动期 token
  鉴权 + JSON 端点；前端（任务 11）以 ~1s 轮询这些端点渲染

参考源（github_ref 代码证据）
- `strix/viewer/server.py: serve / authorized_url / _make_handler`
"""

from rxchk.viewer.server import (
    authorized_url,
    build_authorized_url,
    build_arg_parser,
    build_runs_payload,
    generate_token,
    main,
    resolve_run_dir,
    serve,
)
from rxchk.viewer.transcript import (
    read_audit,
    read_findings,
    read_run_summary,
    read_transcript,
    read_writeback,
)

__all__ = [
    "authorized_url",
    "build_authorized_url",
    "build_arg_parser",
    "build_runs_payload",
    "generate_token",
    "main",
    "resolve_run_dir",
    "serve",
    "read_audit",
    "read_findings",
    "read_run_summary",
    "read_transcript",
    "read_writeback",
]
