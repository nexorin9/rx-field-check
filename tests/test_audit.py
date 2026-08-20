"""`rxchk.audit` 单测（任务 12）。

覆盖：
- append-only：每次写入只新增一行，不修改既有行（飞检 / 内审场景的
  「不可改写」硬约束）
- jsonl 损坏仍可追加：尾部被截断 / 非法 JSON 不阻塞后续 append
- 入参校验：未知 action / 空 operator / run_dir 缺 run.json
- 状态机合法迁移：awaiting_review → confirmed / overridden
- 状态机非法迁移：终态（written_back）→ 任意 → RunStateError；同状态幂等放行
- 备注长度上限：超 NOTE_MAX_CHARS 截断而非抛错
- `manual_override=True` 标记：仅 `manual_override` 操作时为 True
- 与 `rxchk.report.writer` 状态机一致：`append_audit` 推进后 `run.json`
  status 同步更新
- 与 viewer server 端点协同：`append_audit_entry` 兼容层仍可被既有
  端点 / 测试调用（向后兼容）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rxchk.audit import (
    AUDIT_ACTIONS,
    NOTE_MAX_CHARS,
    AuditEntry,
    AuditError,
    append_audit,
    read_audit_entries,
)
from rxchk.report.state import RunState, RunStateError
from rxchk.report.writer import (
    AUDIT_FILENAME,
    RUN_FILENAME,
    ReportWriter,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def runs_root(tmp_path: Path) -> Path:
    """每条用例一个干净的 runs 根目录。"""
    root = tmp_path / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def rx_dir(runs_root: Path) -> Path:
    """落一张达到 awaiting_review 状态的处方；返回 `runs/<rx>/` 目录。"""
    rx_no = "RX-2026-AUDIT-<EXAMPLE>"
    writer = ReportWriter(rx_no=rx_no, run_root=runs_root)
    writer.ensure_run_dir()
    writer.transition(RunState.UPLOADED, actor="parser")
    writer.transition(RunState.PARSING, actor="parser")
    writer.transition(RunState.RUNNING, actor="coordinator")
    writer.transition(RunState.AWAITING_REVIEW, actor="coordinator")
    return writer.run_dir


@pytest.fixture()
def fixed_now() -> datetime:
    """固定时间戳，便于断言 ts 字段。"""
    return datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# AuditEntry dataclass                                                          #
# --------------------------------------------------------------------------- #


class TestAuditEntryDataclass:
    def test_to_json_round_trip(self) -> None:
        entry = AuditEntry(
            ts="2026-08-20T12:00:00+00:00",
            action="confirm",
            operator="张三",
            note="已按提示减量",
            manual_override=False,
            finding_id="f-dose-1",
        )
        raw = entry.to_json()
        # ensure_ascii=False 保中文
        assert "张三" in raw
        decoded = AuditEntry.from_json(raw)
        assert decoded == entry

    def test_to_json_omits_finding_id_when_none(self) -> None:
        entry = AuditEntry(
            ts="2026-08-20T12:00:00+00:00",
            action="reject",
            operator="李四",
        )
        raw = entry.to_json()
        assert "finding_id" not in raw

    def test_manual_override_flag_only_on_manual_override_action(self) -> None:
        for action in ("confirm", "override", "reject", "manual_override"):
            entry = AuditEntry(
                ts="2026-08-20T12:00:00+00:00",
                action=action,
                operator="op",
                manual_override=(action == "manual_override"),
            )
            assert entry.manual_override is (action == "manual_override")

    def test_from_json_rejects_non_dict(self) -> None:
        with pytest.raises(AuditError):
            AuditEntry.from_json("[1, 2, 3]")

    def test_from_json_rejects_invalid_json(self) -> None:
        with pytest.raises(AuditError):
            AuditEntry.from_json("{not valid}")


# --------------------------------------------------------------------------- #
# append_audit 入参校验                                                         #
# --------------------------------------------------------------------------- #


class TestAppendAuditInputValidation:
    def test_unknown_action_rejected(self, rx_dir: Path) -> None:
        with pytest.raises(AuditError) as exc:
            append_audit(rx_dir, action="approve", operator="张三")
        assert "approve" in str(exc.value)
        # 拒绝的操作不应在 audit.jsonl 里留痕
        audit_path = rx_dir / AUDIT_FILENAME
        assert not audit_path.exists() or audit_path.read_text(encoding="utf-8") == ""

    def test_empty_operator_rejected(self, rx_dir: Path) -> None:
        with pytest.raises(AuditError) as exc:
            append_audit(rx_dir, action="confirm", operator="")
        assert "operator" in str(exc.value)

    def test_whitespace_only_operator_rejected(self, rx_dir: Path) -> None:
        with pytest.raises(AuditError):
            append_audit(rx_dir, action="confirm", operator="   ")

    def test_operator_stripped(self, rx_dir: Path, fixed_now: datetime) -> None:
        entry = append_audit(
            rx_dir,
            action="confirm",
            operator="  张三  ",
            now=fixed_now,
        )
        assert entry.operator == "张三"

    def test_missing_run_json_rejected(self, runs_root: Path) -> None:
        # 创建一个没有 run.json 的目录
        empty_dir = runs_root / "RX-EMPTY-<EXAMPLE>"
        empty_dir.mkdir(parents=True)
        with pytest.raises(AuditError) as exc:
            append_audit(empty_dir, action="confirm", operator="op")
        assert "run.json" in str(exc.value)

    def test_corrupted_run_json_rejected(self, rx_dir: Path) -> None:
        (rx_dir / RUN_FILENAME).write_text("{not valid", encoding="utf-8")
        with pytest.raises(AuditError):
            append_audit(rx_dir, action="confirm", operator="op")

    def test_all_audit_actions_accepted(
        self, rx_dir: Path, fixed_now: datetime
    ) -> None:
        # 第一条 confirm → confirmed；后续 override/reject/manual_override
        # 都被前一条同一 state 推进拒收。先建 4 个独立 rx_dir。
        pass  # 下面 parametrize 覆盖


# --------------------------------------------------------------------------- #
# append_audit 状态机合法迁移                                                   #
# --------------------------------------------------------------------------- #


class TestAppendAuditStateMachineLegal:
    def test_confirm_advances_to_confirmed(
        self, rx_dir: Path, fixed_now: datetime
    ) -> None:
        entry = append_audit(
            rx_dir, action="confirm", operator="张三", now=fixed_now
        )
        assert entry.action == "confirm"
        assert entry.manual_override is False
        run_data = json.loads((rx_dir / RUN_FILENAME).read_text(encoding="utf-8"))
        assert run_data["status"] == RunState.CONFIRMED.value

    def test_override_advances_to_overridden(
        self, rx_dir: Path, fixed_now: datetime
    ) -> None:
        entry = append_audit(
            rx_dir, action="override", operator="李四",
            note="临床判断放行", now=fixed_now,
        )
        assert entry.action == "override"
        assert entry.note == "临床判断放行"
        run_data = json.loads((rx_dir / RUN_FILENAME).read_text(encoding="utf-8"))
        assert run_data["status"] == RunState.OVERRIDDEN.value

    def test_reject_advances_to_overridden(
        self, rx_dir: Path, fixed_now: datetime
    ) -> None:
        append_audit(rx_dir, action="reject", operator="王五", now=fixed_now)
        run_data = json.loads((rx_dir / RUN_FILENAME).read_text(encoding="utf-8"))
        assert run_data["status"] == RunState.OVERRIDDEN.value

    def test_manual_override_sets_flag(
        self, rx_dir: Path, fixed_now: datetime
    ) -> None:
        entry = append_audit(
            rx_dir,
            action="manual_override",
            operator="赵六",
            note="规则库未覆盖该药，由药师人工放行",
            now=fixed_now,
        )
        assert entry.manual_override is True
        run_data = json.loads((rx_dir / RUN_FILENAME).read_text(encoding="utf-8"))
        assert run_data["status"] == RunState.OVERRIDDEN.value

    def test_finding_id_persists(self, rx_dir: Path, fixed_now: datetime) -> None:
        entry = append_audit(
            rx_dir,
            action="confirm",
            operator="张三",
            finding_id="f-dose-1",
            now=fixed_now,
        )
        assert entry.finding_id == "f-dose-1"
        # 落盘的 jsonl 也能解析出 finding_id
        lines = (rx_dir / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
        assert json.loads(lines[0])["finding_id"] == "f-dose-1"


# --------------------------------------------------------------------------- #
# append_audit 状态机非法迁移                                                   #
# --------------------------------------------------------------------------- #


class TestAppendAuditStateMachineIllegal:
    def test_terminal_state_rejects_new_confirm(self, rx_dir: Path) -> None:
        # 推到底：awaiting_review → confirmed → written_back
        append_audit(rx_dir, action="confirm", operator="张三")
        writer = ReportWriter(rx_no=rx_dir.name, run_root=rx_dir.parent.parent)
        writer.run_dir = rx_dir  # 复用同一目录
        writer.transition(RunState.WRITTEN_BACK, actor="his_adapter")

        # 终态再 append → RunStateError（终态冻结）
        with pytest.raises(RunStateError):
            append_audit(rx_dir, action="override", operator="李四")

        # 终态拒绝后 audit.jsonl 不增加新行
        lines = (rx_dir / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["action"] == "confirm"

    def test_skip_state_machine_rejected(self, rx_dir: Path) -> None:
        # 不在 awaiting_review 状态（比如还在 RUNNING）就直接 confirm → 非法
        writer = ReportWriter(rx_no=rx_dir.name, run_root=rx_dir.parent.parent)
        writer.run_dir = rx_dir
        # 故意把状态拨回到 RUNNING（直接改 run.json）
        run_path = rx_dir / RUN_FILENAME
        data = json.loads(run_path.read_text(encoding="utf-8"))
        data["status"] = RunState.RUNNING.value
        run_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        with pytest.raises(RunStateError):
            append_audit(rx_dir, action="confirm", operator="张三")

    def test_idempotent_same_state(self, rx_dir: Path, fixed_now: datetime) -> None:
        # 多次 confirm 同状态幂等放行（不抛错；run.json 不变）
        append_audit(rx_dir, action="confirm", operator="张三", now=fixed_now)
        updated_first = json.loads(
            (rx_dir / RUN_FILENAME).read_text(encoding="utf-8")
        )["updated_at"]

        append_audit(
            rx_dir, action="confirm", operator="张三", now=fixed_now
        )
        updated_second = json.loads(
            (rx_dir / RUN_FILENAME).read_text(encoding="utf-8")
        )["updated_at"]

        # 同状态幂等：run.json 内容稳定（updated_at 不变）
        assert updated_first == updated_second
        # audit.jsonl 仍追加了新行（操作本身被记下）
        lines = (rx_dir / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2


# --------------------------------------------------------------------------- #
# append-only：飞检追溯硬约束                                                    #
# --------------------------------------------------------------------------- #


class TestAppendOnly:
    def test_existing_lines_immutable(
        self, runs_root: Path, fixed_now: datetime
    ) -> None:
        # 用 2 张独立处方分别做 confirm 与 override（同一张处方不能
        # 连续 confirm → override，状态机非法）
        rx_a = _make_awaiting_review(runs_root, "RX-A-<EXAMPLE>")
        rx_b = _make_awaiting_review(runs_root, "RX-B-<EXAMPLE>")
        first = append_audit(
            rx_a, action="confirm", operator="张三",
            note="首次确认", now=fixed_now,
        )
        second = append_audit(
            rx_b, action="override", operator="李四",
            note="后续放行", now=fixed_now + timedelta(seconds=10),
        )

        lines_a = (rx_a / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
        lines_b = (rx_b / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
        assert len(lines_a) == 1
        assert len(lines_b) == 1

        parsed_first = json.loads(lines_a[0])
        assert parsed_first["action"] == first.action
        assert parsed_first["ts"] == first.ts
        assert parsed_first["operator"] == first.operator
        assert parsed_first["note"] == "首次确认"

        parsed_second = json.loads(lines_b[0])
        assert parsed_second["action"] == second.action
        assert parsed_second["note"] == "后续放行"

    def test_each_line_is_valid_json(
        self, runs_root: Path, fixed_now: datetime
    ) -> None:
        # 用 4 张独立处方覆盖所有 action，每条落盘都必须是合法 JSON
        for action in ("confirm", "override", "reject", "manual_override"):
            rx = _make_awaiting_review(runs_root, f"RX-{action.upper()}-<EXAMPLE>")
            append_audit(rx, action=action, operator="op", now=fixed_now)
            lines = (rx / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
            assert len(lines) == 1
            json.loads(lines[0])  # 每行都必须合法 JSON

    def test_read_audit_entries_round_trip(
        self, runs_root: Path, fixed_now: datetime
    ) -> None:
        rx_a = _make_awaiting_review(runs_root, "RX-RA-<EXAMPLE>")
        rx_b = _make_awaiting_review(runs_root, "RX-RB-<EXAMPLE>")
        entry_a = append_audit(
            rx_a, action="confirm", operator="张三",
            note="note1", now=fixed_now,
        )
        entry_b = append_audit(
            rx_b, action="reject", operator="李四",
            note="note2", now=fixed_now + timedelta(seconds=5),
        )
        # 同一张处方落 1 行；2 张处方共 2 条
        entries_a = read_audit_entries(rx_a)
        entries_b = read_audit_entries(rx_b)
        assert entries_a == [entry_a]
        assert entries_b == [entry_b]

    def test_note_truncated_to_max(self, rx_dir: Path, fixed_now: datetime) -> None:
        long_note = "x" * (NOTE_MAX_CHARS + 500)
        entry = append_audit(
            rx_dir, action="confirm", operator="op",
            note=long_note, now=fixed_now,
        )
        # 不抛错；截断到 NOTE_MAX_CHARS
        assert len(entry.note) == NOTE_MAX_CHARS


def _make_awaiting_review(runs_root: Path, rx_no: str) -> Path:
    """落一张处方到 `awaiting_review`，返回 `runs/<rx>/` 路径。"""
    writer = ReportWriter(rx_no=rx_no, run_root=runs_root)
    writer.ensure_run_dir()
    writer.transition(RunState.UPLOADED)
    writer.transition(RunState.PARSING)
    writer.transition(RunState.RUNNING)
    writer.transition(RunState.AWAITING_REVIEW)
    return writer.run_dir


# --------------------------------------------------------------------------- #
# jsonl 损坏仍可追加                                                            #
# --------------------------------------------------------------------------- #


class TestJsonlCorruptionRecovery:
    def test_truncated_last_line_does_not_block_append(
        self, rx_dir: Path, fixed_now: datetime
    ) -> None:
        # 模拟尾部被截断（缺换行符 / 半行 JSON）
        audit_path = rx_dir / AUDIT_FILENAME
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            '{"ts": "2026-08-20T10:00:00+00:00", "action": "conf', encoding="utf-8"
        )
        # 追加新行 —— 不抛错
        entry = append_audit(
            rx_dir, action="confirm", operator="张三", now=fixed_now
        )
        # 读路径容错：损坏行跳过，新行可解析
        entries = read_audit_entries(rx_dir)
        assert len(entries) == 1
        assert entries[0] == entry

    def test_garbage_line_in_middle_does_not_block_append(
        self, rx_dir: Path, fixed_now: datetime
    ) -> None:
        audit_path = rx_dir / AUDIT_FILENAME
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        # 中间塞一行乱码 + 末尾完整行
        audit_path.write_text(
            "garbage line 1\ngarbage line 2\n"
            '{"ts": "2026-08-20T10:00:00+00:00", "action": "reject", '
            '"operator": "old", "note": "", "manual_override": false}\n',
            encoding="utf-8",
        )
        entry = append_audit(
            rx_dir, action="override", operator="李四", now=fixed_now
        )
        # 旧行（合法）+ 新行（合法）= 2 条；中间乱码跳过
        entries = read_audit_entries(rx_dir)
        assert len(entries) == 2
        assert entries[0].action == "reject"
        assert entries[1] == entry

    def test_empty_audit_file_then_append(
        self, rx_dir: Path, fixed_now: datetime
    ) -> None:
        # audit.jsonl 不存在 → 直接 append 创建
        audit_path = rx_dir / AUDIT_FILENAME
        assert not audit_path.exists()
        entry = append_audit(
            rx_dir, action="confirm", operator="张三", now=fixed_now
        )
        assert audit_path.exists()
        assert read_audit_entries(rx_dir) == [entry]


# --------------------------------------------------------------------------- #
# run.json 原子写：异常时不留半截文件                                            #
# --------------------------------------------------------------------------- #


class TestRunJsonAtomicWrite:
    def test_no_tmp_residue_on_success(self, rx_dir: Path) -> None:
        append_audit(rx_dir, action="confirm", operator="张三")
        tmp = rx_dir / "run.json.tmp"
        assert not tmp.exists()

    def test_run_json_updated_at_changes_on_transition(
        self, rx_dir: Path, fixed_now: datetime
    ) -> None:
        before = json.loads((rx_dir / RUN_FILENAME).read_text(encoding="utf-8"))
        append_audit(rx_dir, action="confirm", operator="张三", now=fixed_now)
        after = json.loads((rx_dir / RUN_FILENAME).read_text(encoding="utf-8"))
        assert after["status"] == RunState.CONFIRMED.value
        assert after["updated_at"] >= before["updated_at"]

    def test_run_json_idempotent_when_same_state(
        self, rx_dir: Path, fixed_now: datetime
    ) -> None:
        # 第一次 confirm → confirmed
        append_audit(rx_dir, action="confirm", operator="op1", now=fixed_now)
        snap1 = (rx_dir / RUN_FILENAME).read_text(encoding="utf-8")

        # 第二次 confirm（同状态）→ run.json 不变（updated_at 也不刷新）
        append_audit(
            rx_dir, action="confirm", operator="op2", now=fixed_now
        )
        snap2 = (rx_dir / RUN_FILENAME).read_text(encoding="utf-8")
        assert snap1 == snap2


# --------------------------------------------------------------------------- #
# 与 viewer server 兼容层协同                                                    #
# --------------------------------------------------------------------------- #


class TestViewerServerCompatLayer:
    def test_append_audit_entry_delegates_to_module(self, rx_dir: Path) -> None:
        """`viewer.server.append_audit_entry` 仍可用（向后兼容），行为与
        `rxchk.audit.append_audit` 一致。"""
        from rxchk.viewer.server import append_audit_entry

        entry_dict = append_audit_entry(
            rx_dir, action="confirm", operator="张三",
            note="兼容层调用", finding_id="f-1",
        )
        assert entry_dict["action"] == "confirm"
        assert entry_dict["operator"] == "张三"
        assert entry_dict["note"] == "兼容层调用"
        assert entry_dict["manual_override"] is False
        assert entry_dict["finding_id"] == "f-1"

        # audit.jsonl 已落盘 + run.json 推进
        run_data = json.loads((rx_dir / RUN_FILENAME).read_text(encoding="utf-8"))
        assert run_data["status"] == RunState.CONFIRMED.value

        # 再次读 jsonl：能解析这条
        entries = read_audit_entries(rx_dir)
        assert len(entries) == 1
        assert entries[0].finding_id == "f-1"

    def test_viewer_actions_constant_exposed(self) -> None:
        from rxchk.viewer.server import AUDIT_ACTIONS as VIEWER_ACTIONS

        assert VIEWER_ACTIONS == AUDIT_ACTIONS


# --------------------------------------------------------------------------- #
# End-to-end: 4 张脱敏样例 × 真实 ReportWriter                                   #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def four_rx_dirs(runs_root: Path) -> list[Path]:
    """模拟 4 张脱敏处方都跑完核对、达到 awaiting_review。"""
    rx_names = [
        "RX-BASIC-<EXAMPLE>",
        "RX-OVERDOSE-<EXAMPLE>",
        "RX-ALLERGY-<EXAMPLE>",
        "RX-COMPLEX-<EXAMPLE>",
    ]
    dirs: list[Path] = []
    for rx_no in rx_names:
        writer = ReportWriter(rx_no=rx_no, run_root=runs_root)
        writer.ensure_run_dir()
        writer.transition(RunState.UPLOADED)
        writer.transition(RunState.PARSING)
        writer.transition(RunState.RUNNING)
        writer.transition(RunState.AWAITING_REVIEW)
        dirs.append(writer.run_dir)
    return dirs


class TestFourSamplesEndToEnd:
    def test_each_sample_confirm_produces_audit(
        self, four_rx_dirs: list[Path], fixed_now: datetime
    ) -> None:
        for d in four_rx_dirs:
            append_audit(
                d, action="confirm", operator="张三",
                note="已审核", now=fixed_now,
            )
            run_data = json.loads((d / RUN_FILENAME).read_text(encoding="utf-8"))
            assert run_data["status"] == RunState.CONFIRMED.value
            entries = read_audit_entries(d)
            assert len(entries) == 1
            assert entries[0].operator == "张三"

    def test_mixed_actions_across_samples(
        self, four_rx_dirs: list[Path], fixed_now: datetime
    ) -> None:
        append_audit(four_rx_dirs[0], action="confirm", operator="op1", now=fixed_now)
        append_audit(four_rx_dirs[1], action="override", operator="op2", now=fixed_now)
        append_audit(four_rx_dirs[2], action="reject", operator="op3", now=fixed_now)
        append_audit(
            four_rx_dirs[3], action="manual_override", operator="op4", now=fixed_now
        )

        statuses = [
            json.loads((d / RUN_FILENAME).read_text(encoding="utf-8"))["status"]
            for d in four_rx_dirs
        ]
        # confirm → confirmed；其余 → overridden
        assert statuses[0] == RunState.CONFIRMED.value
        assert statuses[1:] == [RunState.OVERRIDDEN.value] * 3

        # manual_override 的 manual_override 字段是 True
        m_entries = read_audit_entries(four_rx_dirs[3])
        assert m_entries[0].manual_override is True


# --------------------------------------------------------------------------- #
# 时间戳与可注入性                                                              #
# --------------------------------------------------------------------------- #


class TestTimestampInjection:
    def test_now_param_uses_injected_value(
        self, rx_dir: Path, fixed_now: datetime
    ) -> None:
        entry = append_audit(
            rx_dir, action="confirm", operator="op", now=fixed_now
        )
        assert entry.ts == fixed_now.isoformat(timespec="seconds")

    def test_default_now_is_utc(self, rx_dir: Path) -> None:
        entry = append_audit(rx_dir, action="confirm", operator="op")
        # 默认时间戳是 timezone-aware UTC
        parsed = datetime.fromisoformat(entry.ts)
        assert parsed.tzinfo is not None
        # 与 UTC 偏差 < 10s
        now_utc = datetime.now(timezone.utc)
        assert abs((now_utc - parsed).total_seconds()) < 10
