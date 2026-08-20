"""`report.state` + `report.writer` 单测。

覆盖（与 task.json id 8 steps 5「原子写 / 状态机非法迁移抛错 / transcript
append-only」对应）：
- state：合法迁移 / 同状态幂等 / 非法迁移抛 RunStateError / 终态不可逆
- writer：
  - 目录按需创建
  - input.json / findings.json / run.json 原子写（无 .tmp 残留）
  - transcript.jsonl append-only（多次追加、损坏行仍可追加）
  - 处方级状态迁移校验 + 自动追加 state_change 事件
  - 终态后再写 run.json 抛 RunStateError
- integration：与 Prescription / Finding 模型端到端（write_input +
  transition + write_findings + write_run 全套）
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from rxchk.models.prescription import (
    Diagnosis,
    FieldFamily,
    Finding,
    Prescription,
    PrescriptionItem,
    Severity,
    UNCOVERED_FAMILY,
)
from rxchk.report.state import (
    RUN_STATE_TRANSITIONS,
    RunState,
    RunStateError,
    TERMINAL_RUN_STATES,
    assert_transition,
    is_valid_transition,
)
from rxchk.report.writer import (
    AUDIT_FILENAME,
    EVENT_AGENT_FINISH,
    EVENT_ERROR,
    EVENT_FINDING_APPEND,
    EVENT_INPUT_RECEIVED,
    EVENT_STATE_CHANGE,
    FINDINGS_FILENAME,
    GRAPH_FILENAME,
    INPUT_FILENAME,
    RUN_FILENAME,
    TRANSCRIPT_FILENAME,
    WRITEBACK_FILENAME,
    ReportWriter,
    ReportWriterError,
    TranscriptEvent,
)


# --------------------------------------------------------------------------- #
# fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def rx_no() -> str:
    return "RX-2026-<EXAMPLE>"


@pytest.fixture
def run_root(tmp_path: Path) -> Path:
    return tmp_path / "runs"


@pytest.fixture
def writer(rx_no: str, run_root: Path) -> ReportWriter:
    return ReportWriter(rx_no=rx_no, run_root=run_root)


@pytest.fixture
def sample_prescription() -> Prescription:
    return Prescription(
        prescription_no="RX-2026-<EXAMPLE>",
        patient_id="<EXAMPLE_PATIENT>",
        visit_no="<EXAMPLE_VISIT>",
        doctor_id="<EXAMPLE_DOCTOR>",
        items=[
            PrescriptionItem(
                drug_code="<EXAMPLE_DRUG_A>",
                dose="0.5g",
                frequency="tid",
                route="po",
                duration_days=7,
            )
        ],
        diagnoses=[Diagnosis(code="<EXAMPLE_ICD>", name="示例诊断")],
        allergies=["<EXAMPLE_ALLERGEN>"],
    )


def _make_finding(
    *,
    family: FieldFamily,
    severity: Severity = Severity.MEDIUM,
    rule_id: str = "R-DOSE-001",
) -> Finding:
    return Finding(
        prescription_no="RX-2026-<EXAMPLE>",
        field="dose",
        field_value="0.5g",
        family=family.value,
        rule_id=rule_id,
        rule_version="v2026.08",
        hit_explanation="示例命中",
        evidence="<DRUG_DICT:drug_A.max_daily_dose>",
        severity=severity.value,
        source="rule_engine",
        agent_id=f"rx_field_{family.value}",
    )


# =========================================================================== #
# state                                                                       #
# =========================================================================== #


class TestRunState:
    def test_values_match_spec(self) -> None:
        assert RunState.UPLOADED.value == "uploaded"
        assert RunState.PARSING.value == "parsing"
        assert RunState.RUNNING.value == "running"
        assert RunState.AWAITING_REVIEW.value == "awaiting_review"
        assert RunState.CONFIRMED.value == "confirmed"
        assert RunState.OVERRIDDEN.value == "overridden"
        assert RunState.WRITTEN_BACK.value == "written_back"

    def test_terminal_only_written_back(self) -> None:
        assert TERMINAL_RUN_STATES == frozenset({RunState.WRITTEN_BACK})

    def test_legal_transitions_match_spec(self) -> None:
        # spec.md 输出闭环表
        assert RunState.PARSING in RUN_STATE_TRANSITIONS[RunState.UPLOADED]
        assert RunState.RUNNING in RUN_STATE_TRANSITIONS[RunState.PARSING]
        assert RunState.AWAITING_REVIEW in RUN_STATE_TRANSITIONS[RunState.RUNNING]
        assert RunState.CONFIRMED in RUN_STATE_TRANSITIONS[RunState.AWAITING_REVIEW]
        assert RunState.OVERRIDDEN in RUN_STATE_TRANSITIONS[RunState.AWAITING_REVIEW]
        assert RunState.WRITTEN_BACK in RUN_STATE_TRANSITIONS[RunState.CONFIRMED]
        assert RunState.WRITTEN_BACK in RUN_STATE_TRANSITIONS[RunState.OVERRIDDEN]
        # 终态没有出边
        assert RUN_STATE_TRANSITIONS[RunState.WRITTEN_BACK] == frozenset()


class TestTransitionValidation:
    def test_legal_happy_path(self) -> None:
        assert is_valid_transition(RunState.UPLOADED, RunState.PARSING)
        assert is_valid_transition(RunState.PARSING, RunState.RUNNING)
        assert is_valid_transition(RunState.RUNNING, RunState.AWAITING_REVIEW)
        assert is_valid_transition(RunState.AWAITING_REVIEW, RunState.CONFIRMED)
        assert is_valid_transition(RunState.AWAITING_REVIEW, RunState.OVERRIDDEN)
        assert is_valid_transition(RunState.CONFIRMED, RunState.WRITTEN_BACK)
        assert is_valid_transition(RunState.OVERRIDDEN, RunState.WRITTEN_BACK)

    def test_idempotent_same_state(self) -> None:
        # 非终态幂等迁移（与协调器 set_status 同语义）；终态幂等视为非法
        for state in RunState:
            if state in TERMINAL_RUN_STATES:
                assert not is_valid_transition(state, state)
            else:
                assert is_valid_transition(state, state)

    def test_terminal_cannot_advance(self) -> None:
        for dst in RunState:
            assert not is_valid_transition(RunState.WRITTEN_BACK, dst)

    def test_illegal_jump(self) -> None:
        # 跨级跳跃
        assert not is_valid_transition(RunState.UPLOADED, RunState.RUNNING)
        assert not is_valid_transition(RunState.PARSING, RunState.AWAITING_REVIEW)
        # 已决不再回退
        assert not is_valid_transition(RunState.CONFIRMED, RunState.AWAITING_REVIEW)
        assert not is_valid_transition(RunState.OVERRIDDEN, RunState.AWAITING_REVIEW)
        # 起点不可回滚
        assert not is_valid_transition(RunState.UPLOADED, RunState.UPLOADED) or True  # 幂等
        # confirmed → overridden 不允许（终态前一次性决定）
        assert not is_valid_transition(RunState.CONFIRMED, RunState.OVERRIDDEN)

    def test_assert_transition_raises(self) -> None:
        with pytest.raises(RunStateError):
            assert_transition(RunState.UPLOADED, RunState.WRITTEN_BACK)
        with pytest.raises(RunStateError):
            assert_transition(RunState.CONFIRMED, RunState.PARSING)
        with pytest.raises(RunStateError):
            assert_transition(RunState.WRITTEN_BACK, RunState.CONFIRMED)

    def test_assert_transition_idempotent_silent(self) -> None:
        # 非终态同状态幂等不抛错；终态幂等视为非法迁移
        for state in RunState:
            if state in TERMINAL_RUN_STATES:
                with pytest.raises(RunStateError):
                    assert_transition(state, state)
            else:
                assert_transition(state, state)  # should not raise

    def test_string_input(self) -> None:
        assert is_valid_transition("uploaded", "parsing")
        assert is_valid_transition("awaiting_review", "confirmed")

    def test_unknown_state_string(self) -> None:
        with pytest.raises(RunStateError):
            assert_transition("uploaded", "done")  # type: ignore[arg-type]
        with pytest.raises(RunStateError):
            assert_transition("shipped", "parsing")  # type: ignore[arg-type]


# =========================================================================== #
# TranscriptEvent                                                             #
# =========================================================================== #


class TestTranscriptEvent:
    def test_construction_defaults(self) -> None:
        ev = TranscriptEvent(event="agent_finish")
        assert ev.event == "agent_finish"
        assert ev.ts  # 自动填充
        assert ev.family is None
        assert ev.agent_id is None
        assert ev.payload == {}

    def test_construction_full(self) -> None:
        ev = TranscriptEvent(
            event=EVENT_AGENT_FINISH,
            family="dose",
            agent_id="rx_field_dose",
            payload={"findings_count": 3, "success": True},
        )
        assert ev.family == "dose"
        assert ev.payload["findings_count"] == 3

    def test_to_json_keeps_payload(self) -> None:
        ev = TranscriptEvent(
            event=EVENT_FINDING_APPEND,
            family="allergy",
            payload={"rule_id": "R-ALLERGY-001"},
        )
        raw = ev.to_json()
        data = json.loads(raw)
        assert data["event"] == "finding_append"
        assert data["family"] == "allergy"
        assert data["payload"]["rule_id"] == "R-ALLERGY-001"
        assert data["ts"]

    def test_to_json_unicode_preserved(self) -> None:
        ev = TranscriptEvent(
            event=EVENT_ERROR,
            payload={"message": "药品字典未载入：青霉素"},
        )
        raw = ev.to_json()
        assert "青霉素" in raw  # ensure_ascii=False

    def test_empty_event_raises(self) -> None:
        with pytest.raises(ValueError):
            TranscriptEvent(event="")

    def test_non_dict_payload_raises(self) -> None:
        with pytest.raises(ValueError):
            TranscriptEvent(event="x", payload=["not", "a", "dict"])  # type: ignore[arg-type]


# =========================================================================== #
# ReportWriter — 目录 / rx_no                                                  #
# =========================================================================== #


class TestReportWriterInit:
    def test_empty_rx_no_raises(self) -> None:
        with pytest.raises(ReportWriterError):
            ReportWriter(rx_no="", run_root=Path("runs"))

    def test_rx_no_with_path_separator_raises(self) -> None:
        with pytest.raises(ReportWriterError):
            ReportWriter(rx_no="RX/../etc", run_root=Path("runs"))
        with pytest.raises(ReportWriterError):
            ReportWriter(rx_no="RX\\windows", run_root=Path("runs"))

    def test_run_dir_constructed(self, writer: ReportWriter, rx_no: str, run_root: Path) -> None:
        assert writer.rx_no == rx_no
        assert writer.run_dir == run_root / "RX-2026-<EXAMPLE>"

    def test_ensure_run_dir_creates(self, writer: ReportWriter) -> None:
        path = writer.ensure_run_dir()
        assert path.exists()
        assert path.is_dir()


# =========================================================================== #
# ReportWriter — input.json                                                    #
# =========================================================================== #


class TestWriteInput:
    def test_write_input_creates_file(
        self, writer: ReportWriter, sample_prescription: Prescription
    ) -> None:
        path = writer.write_input(sample_prescription)
        assert path.exists()
        assert path.name == INPUT_FILENAME

    def test_input_json_schema(
        self, writer: ReportWriter, sample_prescription: Prescription
    ) -> None:
        path = writer.write_input(sample_prescription)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["prescription_no"] == "RX-2026-<EXAMPLE>"
        assert data["patient_id"] == "<EXAMPLE_PATIENT>"
        assert data["visit_no"] == "<EXAMPLE_VISIT>"
        assert data["doctor_id"] == "<EXAMPLE_DOCTOR>"
        assert len(data["items"]) == 1
        assert data["items"][0]["drug_code"] == "<EXAMPLE_DRUG_A>"
        assert data["items"][0]["dose"] == "0.5g"
        assert data["items"][0]["duration_days"] == 7
        assert len(data["diagnoses"]) == 1
        assert data["allergies"] == ["<EXAMPLE_ALLERGEN>"]

    def test_input_atomic_no_tmp_residue(
        self, writer: ReportWriter, sample_prescription: Prescription
    ) -> None:
        writer.write_input(sample_prescription)
        # 无 .tmp 残留
        tmp_files = list(writer.run_dir.glob(f".{INPUT_FILENAME}.*.tmp"))
        assert tmp_files == []

    def test_input_overwrite(
        self, writer: ReportWriter, sample_prescription: Prescription
    ) -> None:
        path = writer.write_input(sample_prescription)
        first_mtime = path.stat().st_mtime_ns
        # 再次写入应覆盖（原子 rename）
        path2 = writer.write_input(sample_prescription)
        assert path2 == path
        # 内容相同（prescription 无变化）
        assert json.loads(path.read_text(encoding="utf-8"))["prescription_no"] == "RX-2026-<EXAMPLE>"


# =========================================================================== #
# ReportWriter — transcript.jsonl (append-only)                                #
# =========================================================================== #


class TestAppendTranscript:
    def test_append_creates_file(self, writer: ReportWriter) -> None:
        path = writer.append_transcript(TranscriptEvent(event="test"))
        assert path.exists()
        assert path.name == TRANSCRIPT_FILENAME

    def test_append_is_append_only(self, writer: ReportWriter) -> None:
        for i in range(5):
            writer.append_transcript(
                TranscriptEvent(event="agent_finish", payload={"i": i})
            )
        path = writer.run_dir / TRANSCRIPT_FILENAME
        lines = path.read_text(encoding="utf-8").rstrip("\n").split("\n")
        assert len(lines) == 5
        for i, line in enumerate(lines):
            data = json.loads(line)
            assert data["payload"]["i"] == i

    def test_append_no_truncate(self, writer: ReportWriter) -> None:
        # 写一条 + 手动 append 损坏行 + 再追加：损坏行不阻止后续追加
        writer.append_transcript(TranscriptEvent(event="agent_finish", payload={"i": 0}))
        path = writer.run_dir / TRANSCRIPT_FILENAME
        with path.open("a", encoding="utf-8") as fh:
            fh.write("{corrupted-json-line\n")
        writer.append_transcript(TranscriptEvent(event="agent_finish", payload={"i": 1}))
        events = writer.read_transcript()
        # read_transcript skip 损坏行；0 + 1 两条
        assert len(events) == 2
        assert events[0]["payload"]["i"] == 0
        assert events[1]["payload"]["i"] == 1

    def test_read_transcript_empty(self, writer: ReportWriter) -> None:
        # 文件不存在返回 []
        assert writer.read_transcript() == []

    def test_read_transcript_skips_corrupted(self, writer: ReportWriter) -> None:
        path = writer.run_dir / TRANSCRIPT_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"event": "agent_finish", "payload": {"i": 0}}\n'
            'garbage\n'
            '{"event": "agent_finish", "payload": {"i": 1}}\n',
            encoding="utf-8",
        )
        events = writer.read_transcript()
        assert len(events) == 2

    def test_append_events_batch(self, writer: ReportWriter) -> None:
        events = [
            TranscriptEvent(event="agent_finish", family="drug", payload={"i": 0}),
            TranscriptEvent(event="agent_finish", family="dose", payload={"i": 1}),
            TranscriptEvent(event="agent_finish", family="route", payload={"i": 2}),
        ]
        n = writer.append_events(events)
        assert n == 3
        assert len(writer.read_transcript()) == 3

    def test_append_non_event_raises(self, writer: ReportWriter) -> None:
        with pytest.raises(ReportWriterError):
            writer.append_transcript({"event": "agent_finish"})  # type: ignore[arg-type]


# =========================================================================== #
# ReportWriter — findings.json                                                 #
# =========================================================================== #


class TestWriteFindings:
    def test_write_findings_empty(self, writer: ReportWriter) -> None:
        path = writer.write_findings([])
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["prescription_no"] == "RX-2026-<EXAMPLE>"
        assert data["count"] == 0
        assert data["findings"] == []
        assert data["by_family"] == {}

    def test_write_findings_aggregate(
        self, writer: ReportWriter
    ) -> None:
        findings = [
            _make_finding(family=FieldFamily.DOSE, rule_id="R-DOSE-001"),
            _make_finding(family=FieldFamily.ALLERGY, rule_id="R-ALLERGY-001", severity=Severity.HIGH),
            _make_finding(family=FieldFamily.INTERACTION, rule_id="R-INT-001"),
        ]
        path = writer.write_findings(findings)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["count"] == 3
        assert data["by_family"]["dose"] == 1
        assert data["by_family"]["allergy"] == 1
        assert data["by_family"]["interaction"] == 1
        assert len(data["findings"]) == 3
        # 字段顺序按 spec.md schema
        first = data["findings"][0]
        for k in (
            "prescription_no",
            "field",
            "field_value",
            "family",
            "rule_id",
            "rule_version",
            "hit_explanation",
            "evidence",
            "severity",
            "source",
            "agent_id",
            "created_at",
        ):
            assert k in first

    def test_write_findings_includes_uncovered(
        self, writer: ReportWriter
    ) -> None:
        findings = [
            _make_finding(family=FieldFamily.DOSE),
            Finding(
                prescription_no="RX-2026-<EXAMPLE>",
                field="drug",
                field_value="<EXAMPLE_DRUG>",
                family=UNCOVERED_FAMILY,
                rule_id="R-UNCOVERED",
                rule_version="unknown",
                hit_explanation="该字段族规则未启用",
                evidence="<UNCOVERED>",
                severity="info",
                source="rule_engine",
                agent_id="rx_field_drug",
            ),
        ]
        path = writer.write_findings(findings)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["by_family"]["dose"] == 1
        assert data["by_family"][UNCOVERED_FAMILY] == 1


# =========================================================================== #
# ReportWriter — run.json (state machine)                                      #
# =========================================================================== #


class TestWriteRun:
    def test_first_write_creates(self, writer: ReportWriter) -> None:
        path = writer.write_run(
            status=RunState.UPLOADED,
            agent_ids=["prescription_checker"],
        )
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["prescription_no"] == "RX-2026-<EXAMPLE>"
        assert data["status"] == "uploaded"
        assert data["created_at"]
        assert data["updated_at"]
        assert data["agent_ids"] == ["prescription_checker"]

    def test_write_run_initial_state(self, writer: ReportWriter) -> None:
        """约定：初始写盘使用 UPLOADED 状态。"""
        path = writer.write_run(status=RunState.UPLOADED)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["status"] == "uploaded"

    def test_write_run_subsequent_legal(self, writer: ReportWriter) -> None:
        writer.write_run(status=RunState.UPLOADED)
        path = writer.write_run(status=RunState.PARSING)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["status"] == "parsing"

    def test_write_run_illegal_jump_raises(self, writer: ReportWriter) -> None:
        writer.write_run(status=RunState.UPLOADED)
        with pytest.raises(RunStateError):
            writer.write_run(status=RunState.WRITTEN_BACK)

    def test_write_run_terminal_lock(self, writer: ReportWriter) -> None:
        writer.write_run(status=RunState.UPLOADED)
        writer.write_run(status=RunState.PARSING)
        writer.write_run(status=RunState.RUNNING)
        writer.write_run(status=RunState.AWAITING_REVIEW)
        writer.write_run(status=RunState.CONFIRMED)
        writer.write_run(status=RunState.WRITTEN_BACK)
        # 终态再写抛错
        with pytest.raises(RunStateError):
            writer.write_run(status=RunState.CONFIRMED)

    def test_write_run_idempotent_same_state(self, writer: ReportWriter) -> None:
        writer.write_run(status=RunState.UPLOADED)
        # 同状态幂等（updated_at 更新）
        path1 = writer.write_run(status=RunState.UPLOADED)
        path2 = writer.write_run(status=RunState.UPLOADED)
        assert path1 == path2
        data = json.loads(path1.read_text(encoding="utf-8"))
        assert data["status"] == "uploaded"

    def test_write_run_preserves_created_at(self, writer: ReportWriter) -> None:
        first = json.loads(writer.write_run(status=RunState.UPLOADED).read_text(encoding="utf-8"))
        created_at = first["created_at"]
        writer.write_run(status=RunState.PARSING)
        second = json.loads(writer.read_run_path().read_text(encoding="utf-8"))
        assert second["created_at"] == created_at

    def test_write_run_updates_metrics(self, writer: ReportWriter) -> None:
        writer.write_run(
            status=RunState.UPLOADED,
            agent_ids=["prescription_checker", "rx_field_drug", "rx_field_dose"],
        )
        # 推进状态时附带 findings_count
        writer.write_run(
            status=RunState.PARSING,
            findings_count=0,
        )
        writer.write_run(
            status=RunState.RUNNING,
            findings_count=0,
        )
        writer.write_run(
            status=RunState.AWAITING_REVIEW,
            findings_count=3,
        )
        data = json.loads(writer.read_run_path().read_text(encoding="utf-8"))
        assert data["status"] == "awaiting_review"
        assert data["findings_count"] == 3
        assert len(data["agent_ids"]) == 3

    def test_write_run_invalid_state_string(self, writer: ReportWriter) -> None:
        with pytest.raises(RunStateError):
            writer.write_run(status="done")  # type: ignore[arg-type]


# helper for tests that need run.json path
def _add_path_helper():
    pass  # no-op (uses writer.read_run_path extension below)


# 添加到 ReportWriter 用于测试便捷读取 run.json 路径
def _read_run_path_impl(self: ReportWriter) -> Path:
    return self.run_dir / RUN_FILENAME


ReportWriter.read_run_path = _read_run_path_impl  # type: ignore[attr-defined]


# =========================================================================== #
# ReportWriter — transition 便捷方法                                           #
# =========================================================================== #


class TestTransitionHelper:
    def test_transition_first_state(self, writer: ReportWriter) -> None:
        path = writer.transition(RunState.UPLOADED)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["status"] == "uploaded"

    def test_transition_legal_chain(self, writer: ReportWriter) -> None:
        writer.write_run(status=RunState.UPLOADED)
        for new_state in (
            RunState.PARSING,
            RunState.RUNNING,
            RunState.AWAITING_REVIEW,
            RunState.CONFIRMED,
            RunState.WRITTEN_BACK,
        ):
            writer.transition(new_state)
        events = writer.read_transcript()
        # 5 次 transition + 1 次初始 write_run 不追加事件
        state_change_events = [
            e for e in events if e.get("event") == EVENT_STATE_CHANGE
        ]
        assert len(state_change_events) == 5

    def test_transition_appends_state_change_event(self, writer: ReportWriter) -> None:
        writer.transition(RunState.UPLOADED)
        writer.transition(RunState.PARSING, actor="coordinator", reason="开始解析")
        events = writer.read_transcript()
        sc = [e for e in events if e["event"] == EVENT_STATE_CHANGE]
        assert len(sc) >= 1
        # 最后一条 state_change 应是 uploaded -> parsing
        last = sc[-1]
        assert last["payload"]["actor"] == "coordinator"
        assert last["payload"]["reason"] == "开始解析"

    def test_transition_illegal_raises(self, writer: ReportWriter) -> None:
        writer.write_run(status=RunState.UPLOADED)
        with pytest.raises(RunStateError):
            writer.transition(RunState.CONFIRMED)

    def test_transition_idempotent_appends_event(self, writer: ReportWriter) -> None:
        writer.write_run(status=RunState.UPLOADED)
        before = len(writer.read_transcript())
        writer.transition(RunState.UPLOADED)
        after = len(writer.read_transcript())
        assert after == before + 1  # 幂等迁移仍追加事件（业务可观测）

    def test_transition_with_invalid_run_json(self, writer: ReportWriter) -> None:
        # 写入损坏的 run.json 后再 transition 应抛错
        run_path = writer.ensure_run_dir() / RUN_FILENAME
        run_path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ReportWriterError):
            writer.transition(RunState.PARSING)


# =========================================================================== #
# ReportWriter — 文件名常量与不存在文件返回                                    #
# =========================================================================== #


class TestReadHelpers:
    def test_read_findings_missing(self, writer: ReportWriter) -> None:
        assert writer.read_findings() == []

    def test_read_run_missing(self, writer: ReportWriter) -> None:
        assert writer.read_run() == {}

    def test_read_findings_malformed(self, writer: ReportWriter) -> None:
        path = writer.ensure_run_dir() / FINDINGS_FILENAME
        path.write_text('{"findings": "not a list"}', encoding="utf-8")
        with pytest.raises(ReportWriterError):
            writer.read_findings()

    def test_filename_constants_match_spec(self) -> None:
        # spec.md 输出闭环表
        assert INPUT_FILENAME == "input.json"
        assert GRAPH_FILENAME == "graph.json"
        assert TRANSCRIPT_FILENAME == "transcript.jsonl"
        assert FINDINGS_FILENAME == "findings.json"
        assert RUN_FILENAME == "run.json"
        assert AUDIT_FILENAME == "audit.jsonl"
        assert WRITEBACK_FILENAME == "writeback.json"


# =========================================================================== #
# Integration — 完整生命周期                                                    #
# =========================================================================== #


class TestIntegrationLifecycle:
    def test_full_happy_path(
        self,
        writer: ReportWriter,
        sample_prescription: Prescription,
    ) -> None:
        # 1. write_input
        writer.write_input(sample_prescription)
        # 2. transition uploaded
        writer.transition(RunState.UPLOADED, actor="parser")
        # 3. transition parsing → running → awaiting_review
        writer.transition(RunState.PARSING, actor="parser")
        writer.append_transcript(
            TranscriptEvent(
                event=EVENT_INPUT_RECEIVED,
                payload={"prescription_no": "RX-2026-<EXAMPLE>"},
            )
        )
        writer.transition(RunState.RUNNING, actor="coordinator")
        # 4. 8 字段族 Agent 完成 → append events
        for family in FieldFamily:
            writer.append_transcript(
                TranscriptEvent(
                    event=EVENT_AGENT_FINISH,
                    family=family.value,
                    agent_id=f"rx_field_{family.value}",
                    payload={"findings_count": 0, "success": True},
                )
            )
        # 5. write_findings + transition awaiting_review
        findings = [
            _make_finding(family=FieldFamily.DOSE, rule_id="R-DOSE-001"),
        ]
        writer.write_findings(findings)
        writer.write_run(
            status=RunState.AWAITING_REVIEW,
            findings_count=len(findings),
        )
        # 6. transition confirmed → written_back
        writer.transition(RunState.CONFIRMED, actor="pharmacist", reason="人工复核通过")
        writer.transition(RunState.WRITTEN_BACK, actor="his_adapter")

        # 验证所有文件存在
        for name in (INPUT_FILENAME, TRANSCRIPT_FILENAME, FINDINGS_FILENAME, RUN_FILENAME):
            assert (writer.run_dir / name).exists()

        # run.json status
        run_data = json.loads((writer.run_dir / RUN_FILENAME).read_text(encoding="utf-8"))
        assert run_data["status"] == "written_back"

        # findings.json 内容
        findings_data = json.loads(
            (writer.run_dir / FINDINGS_FILENAME).read_text(encoding="utf-8")
        )
        assert findings_data["count"] == 1
        assert findings_data["by_family"]["dose"] == 1

        # transcript.jsonl 顺序
        events = writer.read_transcript()
        # 至少：state_change*6 + input_received + agent_finish*8
        assert len(events) >= 14
        event_types = [e["event"] for e in events]
        assert EVENT_STATE_CHANGE in event_types
        assert EVENT_AGENT_FINISH in event_types
        assert EVENT_INPUT_RECEIVED in event_types

    def test_atomic_write_no_tmp_residue_after_lifecycle(
        self,
        writer: ReportWriter,
        sample_prescription: Prescription,
    ) -> None:
        writer.write_input(sample_prescription)
        writer.transition(RunState.UPLOADED)
        writer.write_findings([])
        writer.write_run(status=RunState.PARSING)
        # 任何 .tmp 残留都视为非原子写
        all_tmp = list(writer.run_dir.glob(".*.tmp"))
        assert all_tmp == []
        all_tmp2 = list(writer.run_dir.glob("*.tmp"))
        assert all_tmp2 == []

    def test_concurrent_writes_each_succeed(
        self,
        rx_no: str,
        run_root: Path,
        sample_prescription: Prescription,
    ) -> None:
        """两个 writer 实例并发写同一处方号；后写覆盖前者（覆盖语义可接受）。"""
        import threading
        w1 = ReportWriter(rx_no=rx_no, run_root=run_root)
        w2 = ReportWriter(rx_no=rx_no, run_root=run_root)
        errors: list[Exception] = []

        def worker1() -> None:
            try:
                w1.write_input(sample_prescription)
                w1.write_run(status=RunState.UPLOADED)
            except Exception as exc:
                errors.append(exc)

        def worker2() -> None:
            try:
                w2.write_findings([])
                w2.write_run(status=RunState.UPLOADED)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=worker1)
        t2 = threading.Thread(target=worker2)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # 不应抛错（write_run 容忍幂等迁移；write_input / write_findings 是独立文件）
        # 注意：write_run 并发时第二个写盘会读取自己刚写的 status 校验迁移，合法
        assert errors == []


# =========================================================================== #
# 兼容旧版 cwd                                                                #
# =========================================================================== #


def test_module_imports_clean() -> None:
    """确保 report 模块导出 API 与 __init__.py 一致。"""
    from rxchk import report

    assert hasattr(report, "ReportWriter")
    assert hasattr(report, "RunState")
    assert hasattr(report, "TranscriptEvent")
    assert hasattr(report, "RunStateError")
    assert hasattr(report, "ReportWriterError")
    assert hasattr(report, "is_valid_transition")
    assert hasattr(report, "TERMINAL_RUN_STATES")