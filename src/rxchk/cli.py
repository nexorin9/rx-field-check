"""`rxchk` 命令行入口。

参考源（github_ref 代码证据）
============================
- `strix/interface/main.py: main()` —— argparse 顶层调度 + 子命令分发
  （`strix view [<run>]` 与 `strix <args>` 分派到不同模块）。
- `strix/interface/main.py: parse_arguments()` —— argparse 子命令
  + epilog 例子 + 同类选项齐全。
- `strix/viewer/cli.py: run_view()` —— viewer 子命令的入口契约；
  本模块 `cmd_view` 同款职责（裁剪 viewer.server.serve 调用）。

子命令
======
- `rxchk check <rx_no_or_path>` —— 解析处方 → 派生 8 字段族 Agent →
  落盘 `runs/<rx_no>/` → 打印 finding 摘要。可选 `--from-inbox <dir>`
  走 `./inbox/*.json` 文件投递模式。
- `rxchk view <rx_no>` —— 启动本地 Viewer（HTTP + token 鉴权 +
  控制台打印 tokened URL）。
- `rxchk --help` —— argparse 自动生成。

退出码
======
- 0 —— 核对成功（finding 可能非空，落盘完成即可退出 0）
- 1 —— 入参非法 / 解析失败 / 落盘失败
- 2 —— 处方核对出现 finding 命中（与源 strix 的 `vulnerability_reports`
  非空时 `sys.exit(2)` 对齐：本项目 finding 非空视为「需药师确认」）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from rxchk.check_engine import (
    DEFAULT_DATA_ROOT,
    DEFAULT_RUN_ROOT,
    INBOX_DIRNAME,
    build_check_engine,
    check_from_path,
    check_inbox,
    package_data_root,
    resolve_data_root,
    run_check,
)
from rxchk.parsers.prescription import PrescriptionParseError, parse_prescription
from rxchk.report.state import (
    RunState,
    RunStateError,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 退出码常量                                                                    #
# --------------------------------------------------------------------------- #


EXIT_OK: int = 0
EXIT_USAGE: int = 1
EXIT_FINDINGS: int = 2
EXIT_INFRA: int = 3


# --------------------------------------------------------------------------- #
# 工具函数                                                                    #
# --------------------------------------------------------------------------- #


def setup_logging(verbose: bool) -> None:
    """配置根 logger；`--verbose` 时 DEBUG，否则 INFO。"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _print_finding_summary(findings: Sequence[Any]) -> None:
    """打印 finding 列表（脱敏：仅输出 field / family / severity / hit_explanation）。"""
    if not findings:
        return
    print(f"[rxchk] ---- {len(findings)} 项字段证据卡 ----")
    for idx, f in enumerate(findings, start=1):
        # 不强制要求 finding 有 to_dict：兼容 dataclass 与 dict
        if hasattr(f, "to_dict"):
            data = f.to_dict()
        elif isinstance(f, dict):
            data = f
        else:
            data = {"_raw": repr(f)}
        print(
            f"[{idx:02d}] family={data.get('family', '?')} "
            f"field={data.get('field', '?')} "
            f"severity={data.get('severity', '?')} "
            f"rule={data.get('rule_id', '?')} "
            f"hit={data.get('hit_explanation', '')}"
        )


# --------------------------------------------------------------------------- #
# 子命令：`check`                                                              #
# --------------------------------------------------------------------------- #


def cmd_check(args: argparse.Namespace) -> int:
    """`rxchk check <rx_no_or_path>` 实现。"""
    setup_logging(args.verbose)
    prescription_arg: str = args.prescription
    target_rx: Optional[str] = args.rx_no

    data_root = resolve_data_root(args.data_root)
    run_root = (
        Path(args.run_root).resolve()
        if args.run_root
        else Path.cwd() / DEFAULT_RUN_ROOT
    )

    # 1) inbox 文件投递模式
    if args.from_inbox:
        inbox_dir = Path(args.from_inbox).resolve()
        try:
            result = check_inbox(
                inbox_dir,
                data_root=data_root,
                run_root=run_root,
            )
        except PrescriptionParseError as exc:
            print(f"[rxchk] inbox 模式失败：{exc}", file=sys.stderr)
            return EXIT_INFRA
        except Exception as exc:  # noqa: BLE001
            logger.exception("inbox check failed")
            print(f"[rxchk] inbox 模式异常：{exc}", file=sys.stderr)
            return EXIT_INFRA

        print(
            f"[rxchk] inbox={inbox_dir}：handled={len(result.handled)} "
            f"failed={len(result.failed_files)}"
        )
        for handle in result.handled:
            for line in handle.summary_lines():
                print(line)
        return EXIT_OK if not result.failed_files else EXIT_INFRA

    # 2) 解析处方入参：dict / JSON 字符串 / 文件路径
    try:
        # CLI 设计：`prescription` 是必填位置参数；rx_no 是可选标签
        # 当 prescription_arg 是处方号（不是路径 / 不含路径分隔符 / 不是 JSON），
        # 且 data/sample_rx/<arg>.json 存在，则当作 sample 加载。
        candidate = _resolve_prescription_source(prescription_arg, data_root)
        prescription = parse_prescription(candidate)
        # 强制把 CLI 标签（target_rx）和处方号同步（用于 runs/ 子目录）
        if target_rx and target_rx != prescription.prescription_no:
            # 不强改 prescription.prescription_no：审计口径以入参为准
            # 仅命令行打印时告知
            print(
                f"[rxchk] 注意：CLI 标签 rx_no={target_rx} 与处方号 "
                f"{prescription.prescription_no} 不一致；落盘以处方号为准"
            )
    except PrescriptionParseError as exc:
        print(f"[rxchk] 解析失败：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"[rxchk] 读取入参失败：{exc}", file=sys.stderr)
        return EXIT_INFRA

    # 3) 构造 engine 并跑核对
    try:
        engine = build_check_engine(data_root)
        result = run_check(prescription, engine=engine, run_root=run_root)
    except RunStateError as exc:
        logger.exception("run state transition failed")
        print(f"[rxchk] 状态机错误：{exc}", file=sys.stderr)
        return EXIT_INFRA
    except Exception as exc:  # noqa: BLE001
        logger.exception("run_check failed")
        print(f"[rxchk] 核对异常：{exc}", file=sys.stderr)
        return EXIT_INFRA

    # 4) 打印 finding 摘要
    for line in result.summary_lines():
        print(line)
    _print_finding_summary(result.findings)

    # 5) 退出码：finding 非空退出 2（让 shell / CI 知道「需药师确认」）
    return EXIT_OK if result.finding_count == 0 else EXIT_FINDINGS


def _resolve_prescription_source(
    arg: str, data_root: Path
) -> Any:
    """把 CLI `<rx_no_or_path>` 解析为 `parse_prescription` 可接受的入参。

    优先级：
    1. 路径中含 `/` 或 `\\` → 视作文件路径
    2. 以 `.json` 结尾 → 视作文件路径
    3. 是合法 JSON 字符串 → 解析为 dict
    4. 否则 → 尝试从 `data_root/sample_rx/<arg>.json` 加载（若有）
    5. 否则 → 原样交给 `parse_prescription` 让它抛错
    """
    if "/" in arg or "\\" in arg:
        return arg  # 路径
    if arg.endswith(".json"):
        return arg  # JSON 文件
    if arg.startswith("{"):
        return arg  # JSON 字符串
    sample = data_root / "sample_rx" / f"{arg}.json"
    if sample.exists():
        return sample
    # 兜底：把 arg 当作 JSON 字符串再试一次（让 parse_prescription 自己报错）
    return arg


# --------------------------------------------------------------------------- #
# 子命令：`view`                                                                #
# --------------------------------------------------------------------------- #


def cmd_view(args: argparse.Namespace) -> int:
    """`rxchk view <rx_no>`：启动本地 Viewer。"""
    setup_logging(args.verbose)

    run_root = (
        Path(args.run_root).resolve()
        if args.run_root
        else Path.cwd() / DEFAULT_RUN_ROOT
    )
    if args.run_no:
        run_dir = run_root / args.run_no
    else:
        # 默认最新一次 run（取 mtime 最大）
        run_dir = _latest_run_dir(run_root)

    if run_dir is None or not run_dir.exists() or not run_dir.is_dir():
        print(f"[rxchk] 找不到 runs 目录：{run_dir}", file=sys.stderr)
        return EXIT_INFRA

    # 延迟导入，避免 `rxchk check` 这条主路径为 view 付出 import 成本
    from rxchk.viewer.server import authorized_url, serve

    httpd, url, token = serve(
        run_dir,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
    )
    open_url = authorized_url(url, token)
    # 显式 flush：重定向到文件 / 管道时 stdout 是块缓冲的，不 flush 的话
    # 启动链接要等进程退出才出现，脚本与后台启动都拿不到 token
    print(f"[rxchk] Viewer 启动：{open_url}", flush=True)
    serve_seconds = float(getattr(args, "serve_seconds", 0.0) or 0.0)
    if serve_seconds > 0:
        print(f"[rxchk] 将在 {serve_seconds:g} 秒后自动停止", flush=True)
    else:
        print("[rxchk] 按 Ctrl-C 停止 Viewer", flush=True)
    deadline = time.monotonic() + serve_seconds if serve_seconds > 0 else None
    try:
        # 阻塞主线程；服务跑在后台线程（与源仓库 view 同款 sleep 循环）
        while deadline is None or time.monotonic() < deadline:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n[rxchk] Viewer 已停止")
    finally:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
    return EXIT_OK


def _latest_run_dir(run_root: Path) -> Optional[Path]:
    """返回 `run_root` 下 mtime 最大的子目录；空目录返回 None。"""
    if not run_root.exists() or not run_root.is_dir():
        return None
    candidates = [p for p in run_root.iterdir() if p.is_dir()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


# --------------------------------------------------------------------------- #
# argparse 顶层                                                                #
# --------------------------------------------------------------------------- #


def make_parser() -> argparse.ArgumentParser:
    """构造顶层 argparse；供 `main()` 与测试复用。"""
    parser = argparse.ArgumentParser(
        prog="rxchk",
        description=(
            "用药处方字段对照校验系统 CLI：\n"
            "  rxchk check <rx_no_or_path>   一张处方 → 8 字段族 Agent 并发核对 → 落盘\n"
            "  rxchk view  <rx_no>           启动本地 Viewer（HTTP + token）"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 跑样例处方（data/sample_rx/rx_basic.json）
  rxchk check rx_basic

  # 跑任意 JSON 文件
  rxchk check path/to/prescription.json

  # 文件投递模式（扫 ./inbox/*.json）
  rxchk check --from-inbox ./inbox rx_anylabel

  # 指定 yaml 数据根（默认 ./data；包内 data 兜底）
  rxchk check --data-root /opt/his/data rx_basic

  # 启动 Viewer 看上一次的 runs/<rx>/
  rxchk view

  # 启动 Viewer 看指定 runs/<rx>/
  rxchk view rx_basic

退出码：
  0  核对成功（无 finding / 仅查看）
  1  入参解析失败
  2  核对成功但有 finding（待药师确认）
  3  落盘 / 启动 / 其它基础设施错误
""",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="DEBUG 级日志输出"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # ----- check -----
    p_check = sub.add_parser(
        "check",
        help="解析一张处方 → 派生 8 字段族 Agent 并发核对 → 落盘",
        description="rxchk check：处方字段核对入口。",
    )
    p_check.add_argument(
        "prescription",
        help=(
            "处方入参：JSON 文件路径 / JSON 字符串 / 处方号（自动从 "
            "data/sample_rx/<rx>.json 加载）。"
        ),
    )
    p_check.add_argument(
        "rx_no",
        nargs="?",
        default=None,
        help="可选 CLI 标签；落盘仍以入参处方号为准",
    )
    p_check.add_argument(
        "--data-root",
        default=None,
        help="yaml 数据根目录（默认 ./data；不存在则回落到包内 data）",
    )
    p_check.add_argument(
        "--run-root",
        default=None,
        help="runs 落盘根目录（默认 ./runs）",
    )
    p_check.add_argument(
        "--from-inbox",
        metavar="DIR",
        default=None,
        help=f"扫描 ./<DIR>/*.json 文件投递模式（{INBOX_DIRNAME} 默认名）",
    )

    # ----- view -----
    p_view = sub.add_parser(
        "view",
        help="启动本地 Viewer（HTTP + token 鉴权）",
        description="rxchk view：浏览 runs/<rx_no>/ 的字段证据卡。",
    )
    p_view.add_argument(
        "run_no",
        nargs="?",
        default=None,
        help="runs 子目录名；缺省取最近一次（mtime 最大）",
    )
    p_view.add_argument(
        "--host",
        default="127.0.0.1",
        help="绑定主机（默认 127.0.0.1）",
    )
    p_view.add_argument(
        "--port",
        type=int,
        default=0,
        help="绑定端口（默认 0 = 随机空闲）",
    )
    p_view.add_argument(
        "--run-root",
        default=None,
        help="runs 根目录（默认 ./runs）",
    )
    p_view.add_argument(
        "--no-open",
        action="store_true",
        help="不自动打开浏览器",
    )
    p_view.add_argument(
        "--serve-seconds",
        type=float,
        default=0.0,
        help="服务运行多少秒后自动停止（默认 0 = 常驻，按 Ctrl-C 停止）；"
        "供验证脚本与冒烟检查用",
    )

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`rxchk` 入口；与 pyproject.toml `[project.scripts]` 对齐。"""
    parser = make_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        return cmd_check(args)
    if args.command == "view":
        return cmd_view(args)
    parser.print_help()
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXIT_FINDINGS",
    "EXIT_INFRA",
    "EXIT_OK",
    "EXIT_USAGE",
    "cmd_check",
    "cmd_view",
    "main",
    "make_parser",
]
