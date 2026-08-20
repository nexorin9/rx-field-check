"""数据模型单元测试。

覆盖：
- 必填字段校验（Prescription / PrescriptionItem / Diagnosis）
- FieldFamily 枚举 + FAMILY_PREFIX 同族前缀
- Finding 序列化（to_dict / to_json）+ 字段顺序与 spec.md 一致
- Severity 枚举 + SEVERITIES
- 降级 family=uncovered 字符串
"""

from __future__ import annotations

import json

import pytest

from rxchk.models.prescription import (
    Diagnosis,
    FAMILY_PREFIX,
    FieldFamily,
    Finding,
    Prescription,
    PrescriptionItem,
    SEVERITIES,
    Severity,
    UNCOVERED_FAMILY,
    from_dict,
)


# --------------------------------------------------------------------------- #
# Prescription / PrescriptionItem / Diagnosis — 必填校验                     #
# --------------------------------------------------------------------------- #


class TestDiagnosis:
    def test_valid_diagnosis(self) -> None:
        d = Diagnosis(code="<EXAMPLE_ICD>", name="示例诊断")
        assert d.code == "<EXAMPLE_ICD>"
        assert d.name == "示例诊断"

    @pytest.mark.parametrize("code,name", [("", "示例"), ("<EXAMPLE>", ""), (None, "示例")])
    def test_invalid_diagnosis(self, code: str, name: str) -> None:
        with pytest.raises(ValueError):
            Diagnosis(code=code, name=name)


class TestPrescriptionItem:
    def test_valid_item(self) -> None:
        it = PrescriptionItem(
            drug_code="<EXAMPLE_DRUG_A>",
            dose="0.5g",
            frequency="bid",
            route="po",
            duration_days=7,
        )
        assert it.drug_code == "<EXAMPLE_DRUG_A>"
        assert it.duration_days == 7

    def test_duration_days_optional(self) -> None:
        it = PrescriptionItem(
            drug_code="<EXAMPLE_DRUG_A>",
            dose="0.5g",
            frequency="bid",
            route="po",
        )
        assert it.duration_days is None

    def test_negative_duration_rejected(self) -> None:
        with pytest.raises(ValueError, match="duration_days"):
            PrescriptionItem(
                drug_code="<EXAMPLE_DRUG_A>",
                dose="0.5g",
                frequency="bid",
                route="po",
                duration_days=-1,
            )

    def test_missing_required_fields(self) -> None:
        with pytest.raises(ValueError, match="missing required fields"):
            PrescriptionItem(
                drug_code="",
                dose="0.5g",
                frequency="bid",
                route="po",
            )


class TestPrescription:
    def _valid_dict(self) -> dict:
        return {
            "prescription_no": "RX-2026-<EXAMPLE>",
            "patient_id": "<EXAMPLE_PATIENT>",
            "visit_no": "<EXAMPLE_VISIT>",
            "doctor_id": "<EXAMPLE_DOCTOR>",
            "items": [
                {
                    "drug_code": "<EXAMPLE_DRUG_A>",
                    "dose": "0.5g",
                    "frequency": "bid",
                    "route": "po",
                    "duration_days": 7,
                }
            ],
            "diagnoses": [{"code": "<EXAMPLE_ICD>", "name": "示例诊断"}],
            "allergies": ["<EXAMPLE_ALLERGEN>"],
        }

    def test_from_dict_roundtrip(self) -> None:
        rx = from_dict(self._valid_dict())
        assert rx.prescription_no == "RX-2026-<EXAMPLE>"
        assert len(rx.items) == 1
        assert rx.items[0].drug_code == "<EXAMPLE_DRUG_A>"
        assert rx.diagnoses[0].code == "<EXAMPLE_ICD>"
        assert rx.allergies == ["<EXAMPLE_ALLERGEN>"]

    def test_empty_diagnoses_allergies_allowed(self) -> None:
        data = self._valid_dict()
        data["diagnoses"] = []
        data["allergies"] = []
        rx = from_dict(data)
        assert rx.diagnoses == []
        assert rx.allergies == []

    def test_missing_prescription_no_rejected(self) -> None:
        with pytest.raises(ValueError, match="missing required fields"):
            Prescription(
                prescription_no="",
                patient_id="<EXAMPLE_PATIENT>",
                visit_no="<EXAMPLE_VISIT>",
                doctor_id="<EXAMPLE_DOCTOR>",
                items=[
                    PrescriptionItem(
                        drug_code="<EXAMPLE_DRUG_A>",
                        dose="0.5g",
                        frequency="bid",
                        route="po",
                    )
                ],
                diagnoses=[],
                allergies=[],
            )

    def test_empty_items_rejected(self) -> None:
        with pytest.raises(ValueError, match="items must contain at least one"):
            Prescription(
                prescription_no="RX-2026-<EXAMPLE>",
                patient_id="<EXAMPLE_PATIENT>",
                visit_no="<EXAMPLE_VISIT>",
                doctor_id="<EXAMPLE_DOCTOR>",
                items=[],
                diagnoses=[],
                allergies=[],
            )


# --------------------------------------------------------------------------- #
# FieldFamily — 枚举 + 同族前缀常量                                            #
# --------------------------------------------------------------------------- #


class TestFieldFamily:
    def test_eight_field_families(self) -> None:
        assert {f.name for f in FieldFamily} == {
            "DRUG",
            "DOSE",
            "FREQUENCY",
            "ROUTE",
            "INDICATION",
            "ALLERGY",
            "INTERACTION",
            "POPULATION",
        }

    def test_lowercase_string_values_match_spec(self) -> None:
        assert FieldFamily.DRUG.value == "drug"
        assert FieldFamily.DOSE.value == "dose"
        assert FieldFamily.FREQUENCY.value == "frequency"
        assert FieldFamily.ROUTE.value == "route"
        assert FieldFamily.INDICATION.value == "indication"
        assert FieldFamily.ALLERGY.value == "allergy"
        assert FieldFamily.INTERACTION.value == "interaction"
        assert FieldFamily.POPULATION.value == "population"

    def test_family_prefix_lookup(self) -> None:
        assert FAMILY_PREFIX[FieldFamily.DRUG] == "drug_"
        assert FAMILY_PREFIX[FieldFamily.FREQUENCY] == "freq_"
        assert FAMILY_PREFIX[FieldFamily.POPULATION] == "pop_"

    def test_uncovered_marker(self) -> None:
        assert UNCOVERED_FAMILY == "uncovered"
        # UNCOVERED_FAMILY is a string, not a FieldFamily member.
        assert UNCOVERED_FAMILY not in {f.value for f in FieldFamily}


# --------------------------------------------------------------------------- #
# Severity — 4 档枚举                                                          #
# --------------------------------------------------------------------------- #


class TestSeverity:
    def test_four_severities(self) -> None:
        assert {s.name for s in Severity} == {"HIGH", "MEDIUM", "LOW", "INFO"}
        assert SEVERITIES == (Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO)


# --------------------------------------------------------------------------- #
# Finding — 序列化 + 字段顺序 + 校验                                            #
# --------------------------------------------------------------------------- #


class TestFinding:
    def _kwargs(self, **overrides) -> dict:
        base = {
            "prescription_no": "RX-2026-<EXAMPLE>",
            "field": "dose",
            "field_value": "0.5g",
            "family": FieldFamily.DOSE.value,
            "rule_id": "R-DOSE-001",
            "rule_version": "v2026.08",
            "hit_explanation": "日剂量超过药品字典上限",
            "evidence": "<DRUG_DICT:drug_A.max_daily_dose>",
            "severity": Severity.HIGH.value,
            "source": "rule_engine",
            "agent_id": "<rx_field_dose>",
        }
        base.update(overrides)
        return base

    def test_valid_finding(self) -> None:
        f = Finding(**self._kwargs())
        assert f.family == "dose"
        assert f.severity == "high"
        assert f.created_at.endswith("Z") is False  # ISO with offset, not literal Z

    def test_to_dict_field_order_matches_spec(self) -> None:
        f = Finding(**self._kwargs())
        d = f.to_dict()
        # 与 spec.md 输出 JSON schema 字段顺序一致
        assert list(d.keys()) == [
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
        ]

    def test_to_json_roundtrip(self) -> None:
        f = Finding(**self._kwargs())
        text = f.to_json()
        loaded = json.loads(text)
        assert loaded["prescription_no"] == "RX-2026-<EXAMPLE>"
        assert loaded["hit_explanation"] == "日剂量超过药品字典上限"

    def test_uncovered_family_accepted(self) -> None:
        f = Finding(**self._kwargs(family=UNCOVERED_FAMILY, severity="info"))
        assert f.family == "uncovered"

    @pytest.mark.parametrize("bad_family", ["", "unknown_family", "DRUG"])
    def test_invalid_family_rejected(self, bad_family: str) -> None:
        with pytest.raises(ValueError, match="family"):
            Finding(**self._kwargs(family=bad_family))

    @pytest.mark.parametrize("bad_sev", ["", "critical", "HIGH", None])
    def test_invalid_severity_rejected(self, bad_sev: object) -> None:
        with pytest.raises(ValueError, match="severity"):
            Finding(**self._kwargs(severity=bad_sev))  # type: ignore[arg-type]

    def test_missing_prescription_no_rejected(self) -> None:
        with pytest.raises(ValueError, match="prescription_no"):
            Finding(**self._kwargs(prescription_no=""))

    def test_missing_agent_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="agent_id"):
            Finding(**self._kwargs(agent_id=""))
