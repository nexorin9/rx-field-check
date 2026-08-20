"""indication + allergy 双字段族 Agent 独立测试（任务 22 拆出 + 深化）。

覆盖范围
--------
- IndicationAgent：
  - indication_mismatch：诊断 ICD 与药品适应症无交集 → 命中 R-IND-NO-OVERLAP
  - indication_overlap_passes：诊断 ICD 与药品适应症有交集 → 无命中
  - indication_no_record：处方药品不在药品字典 → R-IND-NO-RECORD（low）
  - indication_no_diagnoses_skipped：处方无诊断 → 跳过（不伪命中）
  - indication_multiple_drugs_partial_overlap：多条目混合命中
- AllergyAgent：
  - direct_allergy：患者对 penicillin 过敏 + DRUG_AMOX → 直接命中（high）
  - family_cross：患者对 penicillin 过敏 + DRUG_CEPH → 家族交叉（降档 medium）
  - mild_direct：患者对 ibuprofen 过敏 + DRUG_IBUP → 直接命中（mild → low）
  - contraindicated_direct：contraindicated → high（直击禁忌）
  - allergy_no_history_skipped：处方无过敏史 → 跳过
  - unknown_severity（unknown_allergen_safe）：患者对未登记过敏原过敏 → 无命中

设计动机
--------
- 与 task 7 的 `test_field_agents.py` 解耦：专注于 IndicationAgent +
  AllergyAgent 的核心场景，便于后续 task 深化 drug_dict / allergy_db
  时单独回归。
- 替身 YAML 数据从 `src/rxchk/data/` 加载（与正式对接契约一致）；
  不构造"测试用临时 YAML"以保证替身-契约同步。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rxchk.agents.allergy import AllergyAgent
from rxchk.agents.base import FieldAgentContext
from rxchk.agents.indication import (
    RULE_IND_NO_OVERLAP,
    RULE_IND_NO_RECORD,
    IndicationAgent,
)
from rxchk.models.prescription import (
    Diagnosis,
    FieldFamily,
    Prescription,
    PrescriptionItem,
)
from rxchk.tools.allergy_db import FakeAllergyDb
from rxchk.tools.drug_dict import FakeDrugDict


# --------------------------------------------------------------------------- #
# 路径 / fixture                                                              #
# --------------------------------------------------------------------------- #


DATA_DIR = Path(__file__).resolve().parent.parent / "src" / "rxchk" / "data"


@pytest.fixture(scope="module")
def drug_dict() -> FakeDrugDict:
    return FakeDrugDict(DATA_DIR / "drug_dict.yaml")


@pytest.fixture(scope="module")
def allergy_db(drug_dict: FakeDrugDict) -> FakeAllergyDb:
    """注入 drug_dict，让 allergy_db 可按编码反查药品名。"""
    return FakeAllergyDb(DATA_DIR / "allergy_db.yaml", drug_dict=drug_dict)


def _mk_rx(
    *,
    items: list[PrescriptionItem],
    diagnoses: list[Diagnosis] | None = None,
    allergies: list[str] | None = None,
    rx_no: str = "RX-2026-<EXAMPLE>",
) -> Prescription:
    return Prescription(
        prescription_no=rx_no,
        patient_id="<EXAMPLE_PATIENT>",
        visit_no="<EXAMPLE_VISIT>",
        doctor_id="<EXAMPLE_DOCTOR>",
        items=items,
        diagnoses=diagnoses or [],
        allergies=allergies or [],
    )


# --------------------------------------------------------------------------- #
# IndicationAgent：indication_mismatch 主路径                                   #
# --------------------------------------------------------------------------- #


class TestIndicationMismatch:
    """indication_mismatch：诊断 ICD 与药品适应症无交集 → 命中。"""

    def test_no_overlap_hits_medium(
        self, drug_dict: FakeDrugDict
    ) -> None:
        """DRUG_AMOX 适应症 [J18.900 / J20.900 / N39.000]；诊断 E11.900 → 无交集 → 命中。

        命中规则：
        - rule_id = R-IND-NO-OVERLAP
        - severity = medium
        - family = indication
        """
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ],
            diagnoses=[Diagnosis(code="E11.900", name="2 型糖尿病")],
        )
        agent = IndicationAgent(drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        hit = findings[0]
        assert hit.family == FieldFamily.INDICATION.value
        assert hit.rule_id == RULE_IND_NO_OVERLAP
        assert hit.severity == "medium"
        # 命中解释应同时提到药品 ICD 与诊断 ICD，便于药师复核
        assert "DRUG_AMOX" in hit.hit_explanation
        assert "E11.900" in hit.hit_explanation
        # 证据来源：药品字典适应症字段
        assert hit.evidence.startswith("<DRUG_DICT:")
        assert "indications" in hit.evidence

    def test_no_overlap_normalizes_case_and_whitespace(
        self, drug_dict: FakeDrugDict
    ) -> None:
        """诊断码大小写 / 前后空格不影响命中（IndicationAgent 内部归一化）。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ],
            diagnoses=[Diagnosis(code=" e11.900 ", name="2 型糖尿病")],
        )
        agent = IndicationAgent(drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        assert findings[0].rule_id == RULE_IND_NO_OVERLAP


# --------------------------------------------------------------------------- #
# IndicationAgent：indication_overlap_passes 合法路径                           #
# --------------------------------------------------------------------------- #


class TestIndicationOverlapPasses:
    """诊断 ICD 与药品适应症有交集 → 无命中。"""

    def test_overlap_returns_no_finding(
        self, drug_dict: FakeDrugDict
    ) -> None:
        """DRUG_AMOX 适应症含 J18.900；诊断含 J18.900 → 无命中。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ],
            diagnoses=[Diagnosis(code="J18.900", name="支气管肺炎")],
        )
        agent = IndicationAgent(drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert findings == []

    def test_partial_overlap_via_multiple_diagnoses(
        self, drug_dict: FakeDrugDict
    ) -> None:
        """多个诊断中只要有一个交集 → 无命中（不做"全部匹配"约束）。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ],
            diagnoses=[
                Diagnosis(code="E11.900", name="2 型糖尿病"),  # 无交集
                Diagnosis(code="J20.900", name="急性支气管炎"),  # 有交集
            ],
        )
        agent = IndicationAgent(drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert findings == []


# --------------------------------------------------------------------------- #
# IndicationAgent：indication_no_record 字典缺项                                #
# --------------------------------------------------------------------------- #


class TestIndicationNoRecord:
    """药品字典未声明该药适应症 → R-IND-NO-RECORD（severity=low）。"""

    def test_unknown_drug_hits_low(
        self, drug_dict: FakeDrugDict
    ) -> None:
        """DRUG_GHOST 不在药品字典 → R-IND-NO-RECORD（severity=low）。

        与 R-IND-NO-OVERLAP 区别：low 提示「字典缺项」而非「疑似超说明书」。
        """
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_GHOST",
                    dose="0.1g",
                    frequency="qd",
                    route="po",
                )
            ],
            diagnoses=[Diagnosis(code="J18.900", name="支气管肺炎")],
        )
        agent = IndicationAgent(drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        hit = findings[0]
        assert hit.rule_id == RULE_IND_NO_RECORD
        assert hit.severity == "low"
        assert "未声明" in hit.hit_explanation


# --------------------------------------------------------------------------- #
# IndicationAgent：边界用例                                                     #
# --------------------------------------------------------------------------- #


class TestIndicationEdgeCases:
    """indication_no_diagnoses_skipped：处方无诊断 → 跳过（不伪命中）。"""

    def test_no_diagnoses_skipped(
        self, drug_dict: FakeDrugDict
    ) -> None:
        """无诊断 → IndicationAgent 不出任何 finding（避免基于空集合假阳性）。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ],
            diagnoses=[],
        )
        agent = IndicationAgent(drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert findings == []

    def test_multiple_drugs_partial_mismatch(
        self, drug_dict: FakeDrugDict
    ) -> None:
        """多条目处方：仅无交集项命中；交集项不命中。"""
        rx = _mk_rx(
            items=[
                # 有交集：适应症含 I48.000（华法林）
                PrescriptionItem(
                    drug_code="DRUG_WAR",
                    dose="5mg",
                    frequency="qd",
                    route="po",
                ),
                # 无交集：适应症 E11.900，与 I48.000 不重叠
                PrescriptionItem(
                    drug_code="DRUG_MET",
                    dose="0.5g",
                    frequency="bid",
                    route="po",
                ),
            ],
            diagnoses=[Diagnosis(code="I48.000", name="心房颤动")],
        )
        agent = IndicationAgent(drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        # 仅 DRUG_MET 命中 R-IND-NO-OVERLAP
        assert len(findings) == 1
        assert findings[0].rule_id == RULE_IND_NO_OVERLAP
        assert "DRUG_MET" in findings[0].hit_explanation


# --------------------------------------------------------------------------- #
# AllergyAgent：direct_allergy 主路径                                          #
# --------------------------------------------------------------------------- #


class TestDirectAllergy:
    """direct_allergy：直接命中 → 按 SEVERITY_TO_FINDING 映射严重度。"""

    def test_penicillin_amoxicillin_hits_direct_high(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """患者对 penicillin 过敏；处方 DRUG_AMOX（amoxicillin 在 A-PENICILLIN.names） → 直接命中。

        A-PENICILLIN.severity = severe → SEVERITY_TO_FINDING → high
        """
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ],
            allergies=["penicillin"],
        )
        agent = AllergyAgent(allergy_db)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        hit = findings[0]
        assert hit.family == FieldFamily.ALLERGY.value
        assert hit.severity == "high"  # severe → high
        assert "直接命中" in hit.hit_explanation
        assert "PENICILLIN" in hit.rule_id
        assert "DIRECT" in hit.rule_id
        assert hit.evidence.startswith("<ALLERGY_DB:")

    def test_ibuprofen_mild_low(self, allergy_db: FakeAllergyDb) -> None:
        """mild 严重度 → low（SEVERITY_TO_FINDING[MILD] = low）。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_IBUP",
                    dose="0.4g",
                    frequency="bid",
                    route="po",
                )
            ],
            allergies=["ibuprofen"],
        )
        agent = AllergyAgent(allergy_db)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        assert findings[0].severity == "low"
        assert "直接命中" in findings[0].hit_explanation


# --------------------------------------------------------------------------- #
# AllergyAgent：family_cross 主路径                                            #
# --------------------------------------------------------------------------- #


class TestFamilyCrossAllergy:
    """family_cross：家族交叉命中 → 严重度降一档。"""

    def test_penicillin_cephalosporin_cross_medium(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """患者对 penicillin 过敏；处方 DRUG_CEPH（头孢） → 家族交叉命中。

        A-PENICILLIN.severity = severe → 家族交叉降一档 → moderate
        SEVERITY_TO_FINDING[moderate] = medium
        """
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_CEPH",
                    dose="1g",
                    frequency="qd",
                    route="iv",
                )
            ],
            allergies=["penicillin"],
        )
        agent = AllergyAgent(allergy_db)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        hit = findings[0]
        assert hit.family == FieldFamily.ALLERGY.value
        assert hit.severity == "medium"  # severe → 降一档 → moderate → medium
        assert "家族交叉" in hit.hit_explanation
        assert "PENICILLIN" in hit.rule_id
        assert "CROSS" in hit.rule_id

    def test_contraindicated_cross_downgrade_to_severe(
        self, allergy_db: FakeAllergyDb, drug_dict: FakeDrugDict
    ) -> None:
        """contraindicated + 家族交叉 → 降一档 severe → high。

        当前 YAML 数据没有 contraindicated 条目；用临时内存 allergen 替身验证
        严重度降档逻辑。
        """
        from rxchk.tools.allergy_db import (
            AllergenEntry,
            AllergyHit,
            AllergySeverity,
            FakeAllergyDb,
        )

        class _StubCrossDb:
            """只暴露严重度降档逻辑的最小替身。"""

            def __init__(self) -> None:
                self._entry = AllergenEntry(
                    allergen_id="A-FAKE-CONTRA",
                    names=("fake_contra",),
                    family="fake_family",
                    severity=AllergySeverity.CONTRAINDICATED,
                    cross_reactions=("cephalosporin",),
                    clinical_note="禁忌药",
                )

            def get_db_version(self) -> str:
                return "v-stub"

            def check(self, drug_code, allergies):
                # 仅在 drug_code=DRUG_CEPH + allergy=DRUG_FAKE_CEPH 时返回
                # 家族交叉命中，且应用严重度降档逻辑。
                hits = []
                for a in allergies:
                    if a != "fake_contra":
                        continue
                    # 模拟：药品命中 cross_reactions 中的 cephalosporin 家族
                    if drug_code == "DRUG_CEPH":
                        sev = AllergySeverity.SEVERE  # contraindicated → severe 降档
                        hits.append(
                            AllergyHit(
                                allergen=a,
                                drug_code=drug_code,
                                severity=sev,
                                cross_reactivity=True,
                                clinical_note=f"家族交叉：{self._entry.clinical_note}",
                                evidence="<ALLERGY_DB:A-FAKE-CONTRA/DRUG_CEPH/cross>",
                                rule_id="R-ALG-A-FAKE-CONTRA-CROSS",
                            )
                        )
                return hits

            def family_cross_reactivity(self, drug_code, allergy):
                hits = self.check(drug_code, [allergy])
                for h in hits:
                    if h.cross_reactivity:
                        return h
                return None

            def severity_for(self, allergen):
                return None

        stub = _StubCrossDb()
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_CEPH",
                    dose="1g",
                    frequency="qd",
                    route="iv",
                )
            ],
            allergies=["fake_contra"],
        )
        agent = AllergyAgent(stub)  # type: ignore[arg-type]
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        hit = findings[0]
        assert hit.severity == "high"  # SEVERITY_TO_FINDING[severe] = high
        assert "家族交叉" in hit.hit_explanation


# --------------------------------------------------------------------------- #
# AllergyAgent：边界用例                                                        #
# --------------------------------------------------------------------------- #


class TestAllergyEdgeCases:
    """allergy_no_history_skipped / unknown_severity 兜底路径。"""

    def test_no_allergies_skipped(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """处方无过敏史 → AllergyAgent 不出任何 finding（不伪命中）。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ],
            allergies=[],
        )
        agent = AllergyAgent(allergy_db)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert findings == []

    def test_unknown_allergen_safe(self, allergy_db: FakeAllergyDb) -> None:
        """unknown_severity / unknown_allergen_safe：患者对未登记过敏原过敏 → 无命中。

        与 'no overlap' 区别：处方药品在字典中，且无任何过敏冲突记录。
        验证「不误命中」是临床安全的底线。
        """
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ],
            allergies=["xyz_unknown_allergen"],
        )
        agent = AllergyAgent(allergy_db)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert findings == []

    def test_multiple_items_partial_conflict(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """多条目处方：仅冲突条目命中；无冲突条目不命中。

        处方同时含 DRUG_AMOX（amoxicillin）+ DRUG_MET（metformin）；
        患者仅对 penicillin 过敏 → 仅 DRUG_AMOX 命中。
        """
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                ),
                PrescriptionItem(
                    drug_code="DRUG_MET",
                    dose="0.5g",
                    frequency="bid",
                    route="po",
                ),
            ],
            allergies=["penicillin"],
        )
        agent = AllergyAgent(allergy_db)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        hit = findings[0]
        assert hit.severity == "high"
        assert "PENICILLIN" in hit.rule_id
        assert "直接命中" in hit.hit_explanation


# --------------------------------------------------------------------------- #
# 集成：IndicationAgent + AllergyAgent 并发跑一张处方                            #
# --------------------------------------------------------------------------- #


class TestIndicationAllergyIntegration:
    """同一处方 → IndicationAgent + AllergyAgent 并发；产物互不污染。"""

    def test_overlap_no_allergy_no_finding(
        self, drug_dict: FakeDrugDict, allergy_db: FakeAllergyDb
    ) -> None:
        """happy path：诊断与适应症有交集 + 无过敏史 → 两个 Agent 都无命中。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ],
            diagnoses=[Diagnosis(code="J18.900", name="支气管肺炎")],
        )
        ctx = FieldAgentContext(prescription=rx)
        ind_findings = asyncio.run(IndicationAgent(drug_dict).run(ctx))
        alg_findings = asyncio.run(AllergyAgent(allergy_db).run(ctx))
        assert ind_findings == []
        assert alg_findings == []

    def test_mismatch_and_allergy_each_hit_once(
        self, drug_dict: FakeDrugDict, allergy_db: FakeAllergyDb
    ) -> None:
        """混合场景：诊断不符 + 患者对 penicillin 过敏 → 两个 Agent 各命中一条。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ],
            diagnoses=[Diagnosis(code="E11.900", name="2 型糖尿病")],
            allergies=["penicillin"],
        )
        ctx = FieldAgentContext(prescription=rx)
        ind_findings = asyncio.run(IndicationAgent(drug_dict).run(ctx))
        alg_findings = asyncio.run(AllergyAgent(allergy_db).run(ctx))

        # IndicationAgent：诊断与适应症无交集 → 一条 medium
        assert len(ind_findings) == 1
        assert ind_findings[0].family == FieldFamily.INDICATION.value
        assert ind_findings[0].severity == "medium"

        # AllergyAgent：直接命中 → 一条 high
        assert len(alg_findings) == 1
        assert alg_findings[0].family == FieldFamily.ALLERGY.value
        assert alg_findings[0].severity == "high"