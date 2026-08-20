"""处方数据模型与字段族定义。

设计目标：
- 处方 / 医嘱 / 诊断 / 过敏史 = 入参结构，对应 spec.md 的 JSON 入参 schema
- Finding = 字段级证据卡（参考源 `_render_completion_report` 的 `findings[]`）
- FieldFamily = 8 个字段族 + 降级 `uncovered`（任务 14 描述）
- Severity = 4 档（high / medium / low / info），与 Viewer 配色一致

字段命名 / 顺序与 spec.md 的入参 / 输出 JSON schema 严格对齐；
占位符统一使用 `<EXAMPLE_*>`，与 spec.md 占位风格一致。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Field family — 8 字段族 + 1 降级标记                                       #
# --------------------------------------------------------------------------- #


class FieldFamily(str, Enum):
    """处方字段族。Python 标识符大写，JSON 字符串值小写（与 spec.md 一致）。"""

    DRUG = "drug"
    DOSE = "dose"
    FREQUENCY = "frequency"
    ROUTE = "route"
    INDICATION = "indication"
    ALLERGY = "allergy"
    INTERACTION = "interaction"
    POPULATION = "population"


# 工具名前缀：参考源 CategoryMeta.match?: RegExp 的同族前缀思路。
FAMILY_PREFIX: dict[FieldFamily, str] = {
    FieldFamily.DRUG: "drug_",
    FieldFamily.DOSE: "dose_",
    FieldFamily.FREQUENCY: "freq_",
    FieldFamily.ROUTE: "route_",
    FieldFamily.INDICATION: "indication_",
    FieldFamily.ALLERGY: "allergy_",
    FieldFamily.INTERACTION: "interaction_",
    FieldFamily.POPULATION: "pop_",
}


# 降级标记：规则库未载入该族时 Finding.family 取此值（任务 14）。
# 字符串而非 FieldFamily 枚举值，便于在 JSON 中观察「未覆盖」与「已核对」区分。
UNCOVERED_FAMILY: str = "uncovered"


# --------------------------------------------------------------------------- #
# Severity — 字段级严重度（与 Viewer 配色 4 档一致）                          #
# --------------------------------------------------------------------------- #


class Severity(str, Enum):
    """字段证据卡严重度。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


SEVERITIES: tuple[Severity, ...] = (
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
    Severity.INFO,
)


# --------------------------------------------------------------------------- #
# Prescription / PrescriptionItem / Diagnosis — 入参 dataclass                #
# --------------------------------------------------------------------------- #


@dataclass
class Diagnosis:
    """诊断条目：ICD 编码 + 名称。"""

    code: str
    name: str

    def __post_init__(self) -> None:
        if not self.code or not isinstance(self.code, str):
            raise ValueError("Diagnosis.code must be a non-empty string")
        if not self.name or not isinstance(self.name, str):
            raise ValueError("Diagnosis.name must be a non-empty string")


@dataclass
class PrescriptionItem:
    """处方内一条医嘱 / 药品条目。"""

    drug_code: str
    dose: str
    frequency: str
    route: str
    duration_days: Optional[int] = None

    def __post_init__(self) -> None:
        missing = [
            f
            for f, v in (
                ("drug_code", self.drug_code),
                ("dose", self.dose),
                ("frequency", self.frequency),
                ("route", self.route),
            )
            if not v or not isinstance(v, str)
        ]
        if missing:
            raise ValueError(
                f"PrescriptionItem missing required fields: {', '.join(missing)}"
            )
        if self.duration_days is not None and self.duration_days < 0:
            raise ValueError("PrescriptionItem.duration_days must be >= 0")


@dataclass
class Prescription:
    """一张处方（spec.md 入参 schema）。

    字段全部必填（`diagnoses` / `allergies` 允许空列表）。
    解析阶段拒收缺字段处方，参见 parsers/prescription.py。
    """

    prescription_no: str
    patient_id: str
    visit_no: str
    doctor_id: str
    items: list[PrescriptionItem]
    diagnoses: list[Diagnosis]
    allergies: list[str]

    def __post_init__(self) -> None:
        missing = [
            f
            for f, v in (
                ("prescription_no", self.prescription_no),
                ("patient_id", self.patient_id),
                ("visit_no", self.visit_no),
                ("doctor_id", self.doctor_id),
            )
            if not v or not isinstance(v, str)
        ]
        if missing:
            raise ValueError(
                f"Prescription missing required fields: {', '.join(missing)}"
            )
        if not self.items:
            raise ValueError("Prescription.items must contain at least one item")
        if not isinstance(self.items, list) or not all(
            isinstance(it, PrescriptionItem) for it in self.items
        ):
            raise ValueError("Prescription.items must be a list of PrescriptionItem")
        if not isinstance(self.diagnoses, list) or not all(
            isinstance(d, Diagnosis) for d in self.diagnoses
        ):
            raise ValueError(
                "Prescription.diagnoses must be a list of Diagnosis (may be empty)"
            )
        if not isinstance(self.allergies, list) or not all(
            isinstance(a, str) for a in self.allergies
        ):
            raise ValueError(
                "Prescription.allergies must be a list of str (may be empty)"
            )


# --------------------------------------------------------------------------- #
# Finding — 字段级证据卡（spec.md 输出 schema）                              #
# --------------------------------------------------------------------------- #


@dataclass
class Finding:
    """字段级证据卡：哪个字段、命中哪条规则、证据片段、严重度。

    与 spec.md 输出 JSON schema 字段一一对应：
    prescription_no / field / field_value / family / rule_id / rule_version /
    hit_explanation / evidence / severity / source / agent_id / created_at
    """

    prescription_no: str
    field: str
    field_value: str
    family: str  # FieldFamily.value 或 UNCOVERED_FAMILY
    rule_id: str
    rule_version: str
    hit_explanation: str
    evidence: str
    severity: str  # Severity.value
    source: str
    agent_id: str
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def __post_init__(self) -> None:
        if not self.prescription_no or not isinstance(self.prescription_no, str):
            raise ValueError("Finding.prescription_no must be a non-empty string")
        if not self.field or not isinstance(self.field, str):
            raise ValueError("Finding.field must be a non-empty string")
        if not isinstance(self.field_value, str):
            raise ValueError("Finding.field_value must be a string")
        if not isinstance(self.family, str) or not self.family:
            raise ValueError("Finding.family must be a non-empty string")
        if self.family not in {f.value for f in FieldFamily} | {UNCOVERED_FAMILY}:
            raise ValueError(
                f"Finding.family must be one of "
                f"{sorted({f.value for f in FieldFamily} | {UNCOVERED_FAMILY})}, "
                f"got {self.family!r}"
            )
        if not isinstance(self.severity, str) or self.severity not in {
            s.value for s in Severity
        }:
            raise ValueError(
                f"Finding.severity must be one of "
                f"{[s.value for s in Severity]}, got {self.severity!r}"
            )
        if not self.agent_id or not isinstance(self.agent_id, str):
            raise ValueError("Finding.agent_id must be a non-empty string")

    # ------------------------------------------------------------------ #
    # 序列化：与 spec.md JSON schema 字段顺序一致                            #
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        """按 spec.md 输出顺序返回 dict，便于 JSON 落盘与 Viewer 渲染。"""
        return {
            "prescription_no": self.prescription_no,
            "field": self.field,
            "field_value": self.field_value,
            "family": self.family,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "hit_explanation": self.hit_explanation,
            "evidence": self.evidence,
            "severity": self.severity,
            "source": self.source,
            "agent_id": self.agent_id,
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        """JSON 字符串输出；ensure_ascii=False 保留中文。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# 构造辅助（供解析器与 agent 复用）                                            #
# --------------------------------------------------------------------------- #


def from_dict(data: dict[str, Any]) -> Prescription:
    """从 dict 构造 Prescription（供 parsers/ 与测试共用）。

    不做必填校验前的预清洗；上层负责 dict → dict 的规范化。
    """
    return Prescription(
        prescription_no=data["prescription_no"],
        patient_id=data["patient_id"],
        visit_no=data["visit_no"],
        doctor_id=data["doctor_id"],
        items=[
            PrescriptionItem(
                drug_code=item["drug_code"],
                dose=item["dose"],
                frequency=item["frequency"],
                route=item["route"],
                duration_days=item.get("duration_days"),
            )
            for item in data["items"]
        ],
        diagnoses=[Diagnosis(code=d["code"], name=d["name"]) for d in data.get("diagnoses", [])],
        allergies=list(data.get("allergies", [])),
    )


__all__ = [
    "Diagnosis",
    "FieldFamily",
    "FAMILY_PREFIX",
    "Finding",
    "Prescription",
    "PrescriptionItem",
    "SEVERITIES",
    "Severity",
    "UNCOVERED_FAMILY",
    "from_dict",
]


# Re-export for convenient dataclasses-based introspection elsewhere
_ = asdict  # silence unused-import linters when running pyflakes
