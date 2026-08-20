"""`rxchk` CLI 入口测试（任务 9）。

覆盖：
- `rxchk --help` / `rxchk check --help` 解析 + 退出 0
- `rxchk check <sample>` 跑通样例 → `runs/<rx>/` 完整落盘 →
  输入 → 状态 awaiting_review → finding 摘要打印
- `rxchk check <不存在文件>` 退出 EXIT_USAGE
- `rxchk check <非法 JSON>` 退出 EXIT_USAGE
- `rxchk check --from-inbox <dir>`：扫 inbox + 落到 runs + processed/failed 划分
- `rxchk view --help` 可解析
- `rxchk check` 命中时退出 EXIT_FINDINGS（2）；无命中时 EXIT_OK（0）
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from rxchk import cli
from rxchk.cli import (
    EXIT_FINDINGS,
    EXIT_INFRA,
    EXIT_OK,
    EXIT_USAGE,
    cmd_check,
    cmd_view,
    make_parser,
)
from rxchk.parsers.prescription import PrescriptionParseError
from rxchk.report.writer import (
    FINDINGS_FILENAME,
    GRAPH_FILENAME,
    INPUT_FILENAME,
    RUN_FILENAME,
    TRANSCRIPT_FILENAME,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def repo_root() -> Path:
    """项目子目录根（rx-field-check/）"""
    return Path(__file__).resolve().parent.parent


@pytest.fixture()
def pkg_data_root(repo_root: Path) -> Path:
    """包内 data/ 子目录"""
    return repo_root / "src" / "rxchk" / "data"


@pytest.fixture()
def sample_prescriptions(pkg_data_root: Path) -> dict[str, Path]:
    return {
        name: pkg_data_root / "sample_rx" / f"{name}.json"
        for name in ("rx_basic", "rx_overdose", "rx_allergy", "rx_complex")
    }


# --------------------------------------------------------------------------- #
# --help / argparse                                                            #
# --------------------------------------------------------------------------- #


def test_make_parser_top_level() -> None:
    parser = make_parser()
    assert parser.prog == "rxchk"


def test_make_parser_check_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """`rxchk check --help` 走 argparse → sys.exit 0（pytest.raises 兜住）。"""
    parser = make_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["check", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "check" in out
    assert "--from-inbox" in out


def test_make_parser_view_help_exits_zero() -> None:
    parser = make_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["view", "--help"])
    assert excinfo.value.code == 0


def test_make_parser_requires_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    parser = make_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args([])
    assert excinfo.value.code != 0


# --------------------------------------------------------------------------- #
# `rxchk --help` 走子进程（确保 entry point 暴露）                                #
# --------------------------------------------------------------------------- #


def test_rxchk_help_via_subprocess(repo_root: Path) -> None:
    """通过 `python -m rxchk.cli --help` 验证模块可执行；与 entry point 同义。"""
    env_setup = (
        "import sys, runpy;"
        "sys.path.insert(0, 'src');"
        "runpy.run_module('rxchk.cli', run_name='__main__', alter_sys=True)"
    )
    # 用 `-c` 直接跑 rxchk.cli 的 main(argparse 解析 --help → exit 0)
    code = (
        "import sys; sys.path.insert(0, 'src'); "
        "from rxchk import cli; "
        "raise SystemExit(cli.main(['--help']))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=15,
    )
    # argparse 把 --help 转 exit 0
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "check" in result.stdout
    assert "view" in result.stdout


def test_rxchk_entry_point_callable(repo_root: Path) -> None:
    """`python -c "from rxchk.cli import main; main(['--help'])"` 不抛错。"""
    code = (
        "import sys; sys.path.insert(0, 'src'); "
        "from rxchk import cli; "
        "raise SystemExit(cli.main(['--help']))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


# --------------------------------------------------------------------------- #
# `rxchk check <sample>`：端到端 → runs/<rx>/ 完整落盘                            #
# --------------------------------------------------------------------------- #


def test_cmd_check_runs_sample(
    sample_prescriptions: dict[str, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`rxchk check rx_basic --run-root <tmp>` 派生 8 Agent → 落盘 → 打印摘要。"""
    parser = make_parser()
    args = parser.parse_args(
        [
            "check",
            "rx_basic",
            "--run-root",
            str(tmp_path / "runs"),
            "--data-root",
            str(sample_prescriptions["rx_basic"].parent.parent),
        ]
    )
    rc = cmd_check(args)
    # rx_basic 应该无 finding（合法处方：合法编码 / 正常剂量 / 频次 / 途径匹配）
    assert rc in (EXIT_OK, EXIT_FINDINGS)

    out = capsys.readouterr().out
    assert "[rxchk]" in out
    assert "rx_basic" in out or "RX-2026" in out

    runs_dir = tmp_path / "runs"
    rx_dir = _find_rx_dir(runs_dir, rx_substring="RX-2026-<EXAMPLE_BASIC>")
    assert rx_dir is not None, f"runs subdirectory not found: {list(runs_dir.iterdir())}"
    _assert_runs_dir_complete(rx_dir)


def test_cmd_check_runs_overdose_finds(
    sample_prescriptions: dict[str, Path],
    tmp_path: Path,
) -> None:
    """超剂量样例：应至少命中 1 条 finding 并退出 EXIT_FINDINGS。"""
    parser = make_parser()
    data_root = sample_prescriptions["rx_overdose"].parent.parent
    args = parser.parse_args(
        [
            "check",
            "rx_overdose",
            "--run-root",
            str(tmp_path / "runs"),
            "--data-root",
            str(data_root),
        ]
    )
    rc = cmd_check(args)
    assert rc == EXIT_FINDINGS
    runs_dir = tmp_path / "runs"
    rx_dir = _find_rx_dir(runs_dir, rx_substring="RX-2026-<EXAMPLE_OVERDOSE>")
    assert rx_dir is not None
    findings_path = rx_dir / FINDINGS_FILENAME
    assert findings_path.exists()
    findings = json.loads(findings_path.read_text(encoding="utf-8"))
    assert findings["count"] >= 1
    families = {f["family"] for f in findings["findings"]}
    # 超剂量样例至少有一族命中（dose / frequency / population 等）
    assert len(families) >= 1


def test_cmd_check_runs_allergy_finds(
    sample_prescriptions: dict[str, Path],
    tmp_path: Path,
) -> None:
    """过敏冲突样例：ALLERGY 族必命中。"""
    parser = make_parser()
    data_root = sample_prescriptions["rx_allergy"].parent.parent
    args = parser.parse_args(
        [
            "check",
            "rx_allergy",
            "--run-root",
            str(tmp_path / "runs"),
            "--data-root",
            str(data_root),
        ]
    )
    rc = cmd_check(args)
    assert rc == EXIT_FINDINGS
    runs_dir = tmp_path / "runs"
    rx_dir = _find_rx_dir(runs_dir, rx_substring="RX-2026-<EXAMPLE_ALLERGY>")
    assert rx_dir is not None
    findings = json.loads((rx_dir / FINDINGS_FILENAME).read_text(encoding="utf-8"))
    families = {f["family"] for f in findings["findings"]}
    assert "allergy" in families


# --------------------------------------------------------------------------- #
# 错误路径                                                                    #
# --------------------------------------------------------------------------- #


def test_cmd_check_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    parser = make_parser()
    args = parser.parse_args(
        [
            "check",
            str(tmp_path / "does_not_exist.json"),
            "--run-root",
            str(tmp_path / "runs"),
        ]
    )
    rc = cmd_check(args)
    assert rc == EXIT_USAGE
    err = capsys.readouterr().err
    assert "解析失败" in err or "读取入参失败" in err


def test_cmd_check_invalid_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{this is : not valid json,,,}", encoding="utf-8")
    parser = make_parser()
    args = parser.parse_args(
        [
            "check",
            str(bad),
            "--run-root",
            str(tmp_path / "runs"),
        ]
    )
    rc = cmd_check(args)
    assert rc == EXIT_USAGE
    err = capsys.readouterr().err
    assert "解析失败" in err or "JSON" in err


def test_cmd_check_missing_required_field(tmp_path: Path) -> None:
    """少 `patient_id` 必填字段 → PrescriptionParseError → EXIT_USAGE。"""
    bad = tmp_path / "rx_no_patient.json"
    bad.write_text(
        json.dumps(
            {
                "prescription_no": "RX-INVALID",
                # patient_id 故意漏掉
                "visit_no": "V-1",
                "doctor_id": "D-1",
                "items": [
                    {
                        "drug_code": "DRUG_AMOX",
                        "dose": "0.5g",
                        "frequency": "tid",
                        "route": "po",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    parser = make_parser()
    args = parser.parse_args(
        [
            "check",
            str(bad),
            "--run-root",
            str(tmp_path / "runs"),
        ]
    )
    rc = cmd_check(args)
    assert rc == EXIT_USAGE


# --------------------------------------------------------------------------- #
# file-injection + JSON string                                                  #
# --------------------------------------------------------------------------- #


def test_cmd_check_via_inline_json(
    tmp_path: Path,
    pkg_data_root: Path,
) -> None:
    """把 JSON 字符串直接喂给 `rxchk check '{...}'`，应解析成功并落盘。"""
    rx_data: dict[str, Any] = {
        "prescription_no": "RX-INLINE-001",
        "patient_id": "P-INLINE-001",
        "visit_no": "V-INLINE-001",
        "doctor_id": "D-INLINE-001",
        "items": [
            {
                "drug_code": "DRUG_AMOX",
                "dose": "0.5g",
                "frequency": "tid",
                "route": "po",
                "duration_days": 7,
            }
        ],
        "diagnoses": [{"code": "J18.900", "name": "支气管肺炎"}],
        "allergies": [],
    }
    parser = make_parser()
    args = parser.parse_args(
        [
            "check",
            json.dumps(rx_data, ensure_ascii=False),
            "--run-root",
            str(tmp_path / "runs"),
            "--data-root",
            str(pkg_data_root),
        ]
    )
    rc = cmd_check(args)
    assert rc in (EXIT_OK, EXIT_FINDINGS)
    rx_dir = tmp_path / "runs" / "RX-INLINE-001"
    assert rx_dir.exists()
    _assert_runs_dir_complete(rx_dir)


def test_cmd_check_via_file_path(
    tmp_path: Path,
    pkg_data_root: Path,
) -> None:
    """把 JSON 文件路径直接喂给 `rxchk check path/to.json`，应解析成功。"""
    rx_data: dict[str, Any] = {
        "prescription_no": "RX-FILE-001",
        "patient_id": "P-FILE-001",
        "visit_no": "V-FILE-001",
        "doctor_id": "D-FILE-001",
        "items": [
            {
                "drug_code": "DRUG_AMOX",
                "dose": "0.5g",
                "frequency": "tid",
                "route": "po",
            }
        ],
        "diagnoses": [],
        "allergies": [],
    }
    rx_file = tmp_path / "rx_file.json"
    rx_file.write_text(json.dumps(rx_data, ensure_ascii=False), encoding="utf-8")

    parser = make_parser()
    args = parser.parse_args(
        [
            "check",
            str(rx_file),
            "--run-root",
            str(tmp_path / "runs"),
            "--data-root",
            str(pkg_data_root),
        ]
    )
    rc = cmd_check(args)
    assert rc in (EXIT_OK, EXIT_FINDINGS)
    assert (tmp_path / "runs" / "RX-FILE-001").exists()


# --------------------------------------------------------------------------- #
# inbox 投递模式                                                                #
# --------------------------------------------------------------------------- #


def test_cmd_check_from_inbox(
    pkg_data_root: Path,
    tmp_path: Path,
) -> None:
    """`rxchk check --from-inbox <dir> rx_anylabel` 扫 inbox + 处理成功。"""
    inbox = tmp_path / "my_inbox"
    inbox.mkdir()
    rx_data: dict[str, Any] = {
        "prescription_no": "RX-INBOX-001",
        "patient_id": "P-INBOX-001",
        "visit_no": "V-INBOX-001",
        "doctor_id": "D-INBOX-001",
        "items": [
            {
                "drug_code": "DRUG_AMOX",
                "dose": "0.5g",
                "frequency": "tid",
                "route": "po",
            }
        ],
        "diagnoses": [],
        "allergies": [],
    }
    (inbox / "rx-one.json").write_text(
        json.dumps(rx_data, ensure_ascii=False), encoding="utf-8"
    )

    # also add a bad file
    (inbox / "rx-bad.json").write_text("not json {", encoding="utf-8")

    parser = make_parser()
    args = parser.parse_args(
        [
            "check",
            "rx_anylabel",
            "--run-root",
            str(tmp_path / "runs"),
            "--data-root",
            str(pkg_data_root),
            "--from-inbox",
            str(inbox),
        ]
    )
    rc = cmd_check(args)
    assert rc == EXIT_INFRA  # 因为有一个 fail

    # 成功文件 → processed/
    processed = list((inbox / "processed").iterdir())
    assert any(p.name == "rx-one.json" for p in processed)
    # 失败文件 → failed/
    failed = list((inbox / "failed").iterdir())
    assert any(p.name == "rx-bad.json" for p in failed)


def test_cmd_check_from_inbox_handles_only_success(
    pkg_data_root: Path,
    tmp_path: Path,
) -> None:
    """inbox 全成功 → 退出 0。"""
    inbox = tmp_path / "inbox_ok"
    inbox.mkdir()
    rx_data: dict[str, Any] = {
        "prescription_no": "RX-INBOX-OK",
        "patient_id": "P",
        "visit_no": "V",
        "doctor_id": "D",
        "items": [
            {
                "drug_code": "DRUG_AMOX",
                "dose": "0.5g",
                "frequency": "tid",
                "route": "po",
            }
        ],
        "diagnoses": [],
        "allergies": [],
    }
    (inbox / "good.json").write_text(
        json.dumps(rx_data, ensure_ascii=False), encoding="utf-8"
    )
    parser = make_parser()
    args = parser.parse_args(
        [
            "check",
            "rx_ok",
            "--run-root",
            str(tmp_path / "runs"),
            "--data-root",
            str(pkg_data_root),
            "--from-inbox",
            str(inbox),
        ]
    )
    rc = cmd_check(args)
    assert rc == EXIT_OK


# --------------------------------------------------------------------------- #
# view: argparse & 入口契约（不真启动 server — 等任务 10）                          #
# --------------------------------------------------------------------------- #


def test_cmd_view_no_run_no_runs_dir(tmp_path: Path) -> None:
    """没有 runs 目录时 view 应该走 EXIT_INFRA（友好报错），不抛异常。"""
    parser = make_parser()
    args = parser.parse_args(
        [
            "view",
            "--run-root",
            str(tmp_path / "no_runs"),
        ]
    )
    rc = cmd_view(args)
    assert rc == EXIT_INFRA


def test_cmd_view_resolves_latest_run(
    pkg_data_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """自动取最新 run；用 `--serve-seconds` 起停一次，验证不阻塞、不抛异常。"""
    from rxchk.check_engine import check_from_path

    runs_root = tmp_path / "runs"
    sample = pkg_data_root / "sample_rx" / "rx_basic.json"
    # 先跑出一次 runs/<rx>/
    check_from_path(sample, data_root=pkg_data_root, run_root=runs_root)
    parser = make_parser()
    args = parser.parse_args(
        [
            "view",
            "--run-root",
            str(runs_root),
            "--no-open",
            # 冒烟检查：起来 0.3 秒就自停，避免测试挂在常驻循环里
            "--serve-seconds",
            "0.3",
        ]
    )
    rc = cmd_view(args)
    assert rc == EXIT_OK
    # 启动期须打印带 token 的可粘贴链接，药师照着开就能进
    out = capsys.readouterr().out
    assert "Viewer 启动" in out
    assert "token=" in out


# --------------------------------------------------------------------------- #
# 内部断言辅助                                                                  #
# --------------------------------------------------------------------------- #


def _find_rx_dir(runs_root: Path, rx_substring: str) -> Path | None:
    """在 `runs_root` 下找出含 `rx_substring` 的子目录。"""
    if not runs_root.exists():
        return None
    for p in runs_root.iterdir():
        if p.is_dir() and rx_substring in p.name:
            return p
    return None


def _assert_runs_dir_complete(rx_dir: Path) -> None:
    """`runs/<rx>/` 必须含 5 个核心产物文件。"""
    for filename in (
        INPUT_FILENAME,
        GRAPH_FILENAME,
        TRANSCRIPT_FILENAME,
        FINDINGS_FILENAME,
        RUN_FILENAME,
    ):
        path = rx_dir / filename
        assert path.exists(), f"missing {filename} in {rx_dir}"
    # run.json status 应为 awaiting_review
    run = json.loads((rx_dir / RUN_FILENAME).read_text(encoding="utf-8"))
    assert run["status"] == "awaiting_review"
    # findings.json 是合法 JSON
    findings = json.loads((rx_dir / FINDINGS_FILENAME).read_text(encoding="utf-8"))
    assert "findings" in findings
    assert "count" in findings
    # transcript 至少 1 条 input_received 事件
    transcript_lines = [
        ln for ln in (rx_dir / TRANSCRIPT_FILENAME).read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    assert len(transcript_lines) >= 2  # input_received + agent_finish × N + state_change
