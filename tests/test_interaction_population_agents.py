"""interaction + population 双字段族 Agent 独立测试（任务 23 拆出 + 深化）。

覆盖范围
--------
- InteractionAgent：
  - two_drug：双药配伍冲突（DRUG_WAR + DRUG_ASP）→ R-INT-001 severity=high
  - three_drug_combo：三药累加（DRUG_ASP + DRUG_IBUP + DRUG_WAR）→
    R-INT-101 severity=contraindicated → high
  - severity_escalation：随 severity 等级递增（contraindicated / major /
    moderate / minor → high / high / medium / low）
  - single_drug_skipped：单药处方不参与配伍核对 → 无命中
  - no_interaction_in_db：药品在配伍库无 pair + 无 triple → 无命中
  - interaction_field_evidence：finding 含 evidence + pair_label 字段名
  - interaction_self_pair_skipped：同药不参与配伍
- PopulationAgent：
  - pregnancy_contra：孕妇 + DRUG_PREG_CONTRA → R-POP-001 severity=high
  - pregnancy_warfarin：孕妇 + DRUG_WAR → R-POP-002 severity=high
  - pediatric_dose：儿童 + DRUG_IBUP → R-POP-003 severity=medium
  - renal_adjust：肾损伤 + DRUG_MET → R-POP-004 severity=high
  - hepatic_warfarin：肝损伤 + DRUG_WAR → R-POP-005 severity=medium
  - no_flags_skipped：无患者标签 → 跳过（不伪命中）
  - unknown_flag_logged_only：未知 flag → 静默跳过规则适配器

设计动机
--------
- 与 task 7 的 `test_field_agents.py` 解耦：专注于 InteractionAgent +
  PopulationAgent 的核心场景，便于后续 task 深化 interaction_db /
  population_rules 时单独回归。
- 替身 YAML 数据从 `src/rxchk/data/` 加载（与正式对接契约一致）；
  不构造"测试用临时 YAML"以保证替身-契约同步。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rxchk.agents.base import FieldAgentContext
from rxchk.agents.interaction import InteractionAgent
from rxchk.agents.population import RECOGNIZED_FLAGS, PopulationAgent
from rxchk.models.prescription import (
    Diagnosis,
    FieldFamily,
    Prescription,
    PrescriptionItem,
)
from rxchk.tools.interaction_db import FakeInteractionDb, InteractionSeverity
from rxchk.tools.rule_adapter import FakeRuleAdapter


# --------------------------------------------------------------------------- #
# 路径 / fixture                                                              #
# --------------------------------------------------------------------------- #


DATA_DIR = Path(__file__).resolve().parent.parent / "src" / "rxchk" / "data"


@pytest.fixture(scope="module")
def interaction_db() -> FakeInteractionDb:
    return FakeInteractionDb(DATA_DIR / "interaction_db.yaml")


@pytest.fixture(scope="module")
def rule_adapter() -> FakeRuleAdapter:
    return FakeRuleAdapter(
        DATA_DIR / "rule" / "dose_rules.yaml",
        DATA_DIR / "rule" / "population_rules.yaml",
    )


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
# InteractionAgent：two_drug 主路径                                            #
# --------------------------------------------------------------------------- #


class TestInteractionAgentTwoDrug:
    """two_drug：双药配伍冲突 → 单条 R-INT-* 命中。"""

    def test_war_asp_hits_major(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """DRUG_WAR + DRUG_ASP：R-INT-001 severity=major → finding severity=high。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_WAR",
                    dose="5mg",
                    frequency="qd",
                    route="po",
                ),
                PrescriptionItem(
                    drug_code="DRUG_ASP",
                    dose="0.1g",
                    frequency="qd",
                    route="po",
                ),
            ]
        )
        agent = InteractionAgent(interaction_db)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        hit = findings[0]
        assert hit.family == FieldFamily.INTERACTION.value
        assert hit.rule_id == "R-INT-001"
        assert hit.severity == "high"  # major → high
        assert "出血" in hit.hit_explanation or "INR" in hit.hit_explanation
        # 双药 pair label: drug_a+drug_b
        assert hit.field == "interactions[DRUG_WAR+DRUG_ASP]"
        assert hit.field_value == "DRUG_WAR+DRUG_ASP"

    def test_amox_war_hits_moderate(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """DRUG_AMOX + DRUG_WAR：R-INT-004 severity=moderate → finding severity=medium。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                ),
                PrescriptionItem(
                    drug_code="DRUG_WAR",
                    dose="5mg",
                    frequency="qd",
                    route="po",
                ),
            ]
        )
        agent = InteractionAgent(interaction_db)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        hit = findings[0]
        assert hit.rule_id == "R-INT-004"
        assert hit.severity == "medium"

    def test_two_drug_minor_severity_low(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """DRUG_AMOX + DRUG_IBUP：R-INT-007 severity=minor → finding severity=low。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                ),
                PrescriptionItem(
                    drug_code="DRUG_IBUP",
                    dose="0.4g",
                    frequency="bid",
                    route="po",
                ),
            ]
        )
        agent = InteractionAgent(interaction_db)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        hit = findings[0]
        assert hit.rule_id == "R-INT-007"
        assert hit.severity == "low"


# --------------------------------------------------------------------------- #
# InteractionAgent：three_drug_combo 主路径                                     #
# --------------------------------------------------------------------------- #


class TestInteractionAgentThreeDrugCombo:
    """three_drug_combo：三药累加 → R-INT-101/102 命中。"""

    def test_asp_ibup_war_triple_hits_contraindicated(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """DRUG_ASP + DRUG_IBUP + DRUG_WAR：R-INT-101 severity=contraindicated。

        注意：YAML 中此 triple 与 ASP+IBUP pair（R-INT-002）和 WAR+ASP pair
        （R-INT-001）会同时命中，所以 findings 包含 1 条 triple + 至少 1 对 pair。
        """
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_ASP",
                    dose="0.1g",
                    frequency="qd",
                    route="po",
                ),
                PrescriptionItem(
                    drug_code="DRUG_IBUP",
                    dose="0.4g",
                    frequency="bid",
                    route="po",
                ),
                PrescriptionItem(
                    drug_code="DRUG_WAR",
                    dose="5mg",
                    frequency="qd",
                    route="po",
                ),
            ]
        )
        agent = InteractionAgent(interaction_db)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        # R-INT-101 三药累加（triple 命中）
        assert "R-INT-101" in rule_ids
        # 副命中：ASP+IBUP pair（R-INT-002）+ WAR+ASP pair（R-INT-001）
        assert "R-INT-001" in rule_ids
        assert "R-INT-002" in rule_ids
        # R-INT-101 → severity=contraindicated → high
        triple_hit = next(f for f in findings if f.rule_id == "R-INT-101")
        assert triple_hit.severity == "high"
        # triple finding：pair_label 是 drug_a+drug_b，evidence 含三药
        assert "DRUG_ASP" in triple_hit.field_value
        assert "DRUG_IBUP" in triple_hit.field_value
        # 第三药仅在 evidence 显式出现（pair_label 只取 a+b）
        assert "DRUG_WAR" in triple_hit.evidence or "WAR" in triple_hit.evidence

    def test_amox_ceph_war_triple_hits_major(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """DRUG_AMOX + DRUG_CEPH + DRUG_WAR：R-INT-102 severity=major → high。

        注意：配伍库中 PEN+CEPH pair（R-INT-005）也存在，会同时命中。
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
                    drug_code="DRUG_CEPH",
                    dose="2g",
                    frequency="qd",
                    route="iv",
                ),
                PrescriptionItem(
                    drug_code="DRUG_WAR",
                    dose="5mg",
                    frequency="qd",
                    route="po",
                ),
            ]
        )
        agent = InteractionAgent(interaction_db)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        assert "R-INT-102" in rule_ids
        triple_hit = next(f for f in findings if f.rule_id == "R-INT-102")
        assert triple_hit.severity == "high"


# --------------------------------------------------------------------------- #
# InteractionAgent：severity 递增路径                                          #
# --------------------------------------------------------------------------- #


class TestInteractionAgentSeverityEscalation:
    """severity_escalation：随 severity 等级递增 finding severity。"""

    def test_severity_to_finding_mapping(self) -> None:
        """SEVERITY_TO_FINDING 映射表与 spec.md 4 档配色一致。"""
        from rxchk.tools.interaction_db import SEVERITY_TO_FINDING

        assert SEVERITY_TO_FINDING[InteractionSeverity.CONTRAINDICATED] == "high"
        assert SEVERITY_TO_FINDING[InteractionSeverity.MAJOR] == "high"
        assert SEVERITY_TO_FINDING[InteractionSeverity.MODERATE] == "medium"
        assert SEVERITY_TO_FINDING[InteractionSeverity.MINOR] == "low"

    def test_severity_order_in_enum(self) -> None:
        """InteractionSeverity 枚举顺序：contraindicated > major > moderate > minor。"""
        order = list(InteractionSeverity)
        assert order[0] == InteractionSeverity.CONTRAINDICATED
        assert order[-1] == InteractionSeverity.MINOR


# --------------------------------------------------------------------------- #
# InteractionAgent：边界路径                                                   #
# --------------------------------------------------------------------------- #


class TestInteractionAgentEdgeCases:
    """边界路径：单药跳过 / 同药跳过 / 配伍库无命中。"""

    def test_single_drug_skipped(self, interaction_db: FakeInteractionDb) -> None:
        """单药处方不参与配伍核对 → 无命中。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ]
        )
        agent = InteractionAgent(interaction_db)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert findings == []

    def test_no_interaction_pair_in_db(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """药品在配伍库无 pair + 无 triple → 无命中。

        DRUG_AMOX + DRUG_PREG_CONTRA：DRUG_PREG_CONTRA 不在 pair / triple 配置。
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
                    drug_code="DRUG_PREG_CONTRA",
                    dose="0.2g",
                    frequency="bid",
                    route="po",
                ),
            ]
        )
        agent = InteractionAgent(interaction_db)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert findings == []

    def test_self_pair_skipped(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """同药不参与配伍（check_pair a==b 返 None）。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                ),
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="bid",
                    route="po",
                ),
            ]
        )
        agent = InteractionAgent(interaction_db)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        # 去重后唯一 DRUG_AMOX → 单药路径 → 无命中
        assert findings == []

    def test_finding_includes_evidence_and_agent_id(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """finding 含 evidence 字段 + agent_id 形如 rx_field_<family>。"""
        from rxchk.coordinator import field_agent_id

        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_WAR",
                    dose="5mg",
                    frequency="qd",
                    route="po",
                ),
                PrescriptionItem(
                    drug_code="DRUG_ASP",
                    dose="0.1g",
                    frequency="qd",
                    route="po",
                ),
            ]
        )
        agent = InteractionAgent(interaction_db)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        hit = findings[0]
        assert hit.evidence
        assert hit.agent_id == field_agent_id(FieldFamily.INTERACTION)
        assert hit.agent_id == "rx_field_interaction"
        # source 字段
        assert hit.source == "rule_engine"
        # rule_version 应与 interaction_db.get_db_version() 一致
        assert hit.rule_version == interaction_db.get_db_version()

    def test_duplicate_drug_codes_deduped(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """处方内重复药品编码应被去重后再算配伍。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_WAR",
                    dose="5mg",
                    frequency="qd",
                    route="po",
                ),
                PrescriptionItem(
                    drug_code="DRUG_WAR",  # 同药重复
                    dose="5mg",
                    frequency="qd",
                    route="po",
                ),
                PrescriptionItem(
                    drug_code="DRUG_ASP",
                    dose="0.1g",
                    frequency="qd",
                    route="po",
                ),
            ]
        )
        agent = InteractionAgent(interaction_db)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        # 去重后 [WAR, ASP] → 1 条 R-INT-001；不应有 WAR+WAR
        rule_ids = [f.rule_id for f in findings]
        assert rule_ids.count("R-INT-001") == 1


# --------------------------------------------------------------------------- #
# PopulationAgent：pregnancy_contra 主路径                                     #
# --------------------------------------------------------------------------- #


class TestPopulationAgentPregnancy:
    """pregnancy_contra：孕妇 + 禁忌药 → R-POP-001/002 命中。"""

    def test_preg_contra_drug_hits_high(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        """DRUG_PREG_CONTRA + pregnant → R-POP-001 severity=high。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_PREG_CONTRA",
                    dose="0.2g",
                    frequency="bid",
                    route="po",
                )
            ]
        )
        ctx = FieldAgentContext(prescription=rx, patient_flags=("pregnant",))
        agent = PopulationAgent(rule_adapter)
        findings = asyncio.run(agent.run(ctx))
        assert len(findings) == 1
        hit = findings[0]
        assert hit.family == FieldFamily.POPULATION.value
        assert hit.rule_id == "R-POP-001"
        assert hit.severity == "high"
        assert "孕妇" in hit.hit_explanation or "禁忌" in hit.hit_explanation

    def test_warfarin_pregnancy_hits_high(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        """DRUG_WAR + pregnant → R-POP-002 severity=high（华法林妊娠致畸）。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_WAR",
                    dose="5mg",
                    frequency="qd",
                    route="po",
                )
            ]
        )
        ctx = FieldAgentContext(prescription=rx, patient_flags=("pregnant",))
        agent = PopulationAgent(rule_adapter)
        findings = asyncio.run(agent.run(ctx))
        assert len(findings) == 1
        assert findings[0].rule_id == "R-POP-002"
        assert findings[0].severity == "high"

    def test_lactating_drug_with_no_rule_no_hit(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        """lactating + DRUG_AMOX：population_rules 无此组合 → 无命中。

        注释：当前 population_rules 仅覆盖 pregnant / pediatric /
        renal_impairment / hepatic_impairment；lactating 暂无规则。
        """
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ]
        )
        ctx = FieldAgentContext(prescription=rx, patient_flags=("lactating",))
        agent = PopulationAgent(rule_adapter)
        findings = asyncio.run(agent.run(ctx))
        assert findings == []


# --------------------------------------------------------------------------- #
# PopulationAgent：pediatric_dose 主路径                                        #
# --------------------------------------------------------------------------- #


class TestPopulationAgentPediatric:
    """pediatric_dose：儿童 + DRUG_IBUP → R-POP-003 severity=medium。"""

    def test_pediatric_ibup_hits_medium(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        """DRUG_IBUP + pediatric → R-POP-003 severity=medium（按体重调整）。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_IBUP",
                    dose="0.2g",
                    frequency="qid",
                    route="po",
                )
            ]
        )
        ctx = FieldAgentContext(prescription=rx, patient_flags=("pediatric",))
        agent = PopulationAgent(rule_adapter)
        findings = asyncio.run(agent.run(ctx))
        assert len(findings) == 1
        hit = findings[0]
        assert hit.rule_id == "R-POP-003"
        assert hit.severity == "medium"
        assert "儿童" in hit.hit_explanation or "体重" in hit.hit_explanation

    def test_pediatric_other_drug_no_hit(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        """pediatric + DRUG_AMOX：population_rules 无此组合 → 无命中。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ]
        )
        ctx = FieldAgentContext(prescription=rx, patient_flags=("pediatric",))
        agent = PopulationAgent(rule_adapter)
        findings = asyncio.run(agent.run(ctx))
        assert findings == []


# --------------------------------------------------------------------------- #
# PopulationAgent：renal_adjust / hepatic_adjust 主路径                        #
# --------------------------------------------------------------------------- #


class TestPopulationAgentRenalHepatic:
    """renal_adjust / hepatic_adjust：肾 / 肝损伤 + 禁忌药 → 命中。"""

    def test_renal_impairment_metformin_hits_high(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        """DRUG_MET + renal_impairment → R-POP-004 severity=high（eGFR<30 禁用）。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_MET",
                    dose="0.5g",
                    frequency="bid",
                    route="po",
                )
            ]
        )
        ctx = FieldAgentContext(prescription=rx, patient_flags=("renal_impairment",))
        agent = PopulationAgent(rule_adapter)
        findings = asyncio.run(agent.run(ctx))
        assert len(findings) == 1
        hit = findings[0]
        assert hit.rule_id == "R-POP-004"
        assert hit.severity == "high"
        assert "肾" in hit.hit_explanation or "eGFR" in hit.hit_explanation

    def test_hepatic_impairment_warfarin_hits_medium(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        """DRUG_WAR + hepatic_impairment → R-POP-005 severity=medium（监测 INR）。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_WAR",
                    dose="5mg",
                    frequency="qd",
                    route="po",
                )
            ]
        )
        ctx = FieldAgentContext(prescription=rx, patient_flags=("hepatic_impairment",))
        agent = PopulationAgent(rule_adapter)
        findings = asyncio.run(agent.run(ctx))
        assert len(findings) == 1
        hit = findings[0]
        assert hit.rule_id == "R-POP-005"
        assert hit.severity == "medium"
        assert "肝" in hit.hit_explanation or "INR" in hit.hit_explanation

    def test_renal_other_drug_no_hit(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        """renal_impairment + DRUG_AMOX：population_rules 无此组合 → 无命中。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ]
        )
        ctx = FieldAgentContext(prescription=rx, patient_flags=("renal_impairment",))
        agent = PopulationAgent(rule_adapter)
        findings = asyncio.run(agent.run(ctx))
        assert findings == []


# --------------------------------------------------------------------------- #
# PopulationAgent：边界路径                                                   #
# --------------------------------------------------------------------------- #


class TestPopulationAgentEdgeCases:
    """边界路径：无 flags 跳过 / 多 flags 命中 / 多药品 命中。"""

    def test_no_flags_skipped(self, rule_adapter: FakeRuleAdapter) -> None:
        """无 patient_flags → 跳过（不伪命中）。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_PREG_CONTRA",
                    dose="0.2g",
                    frequency="bid",
                    route="po",
                )
            ]
        )
        ctx = FieldAgentContext(prescription=rx)  # 默认 patient_flags=()
        agent = PopulationAgent(rule_adapter)
        findings = asyncio.run(agent.run(ctx))
        assert findings == []

    def test_multi_flags_multi_hits(self, rule_adapter: FakeRuleAdapter) -> None:
        """多个 flags → 多个 hits（每个 flag × drug 独立命中）。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_WAR",
                    dose="5mg",
                    frequency="qd",
                    route="po",
                )
            ]
        )
        # DRUG_WAR 同时匹配 pregnant(R-POP-002) + hepatic_impairment(R-POP-005)
        ctx = FieldAgentContext(
            prescription=rx, patient_flags=("pregnant", "hepatic_impairment")
        )
        agent = PopulationAgent(rule_adapter)
        findings = asyncio.run(agent.run(ctx))
        rule_ids = {f.rule_id for f in findings}
        assert "R-POP-002" in rule_ids
        assert "R-POP-005" in rule_ids

    def test_multi_items_each_checked(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        """多药品逐条核对；每条独立命中。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_PREG_CONTRA",
                    dose="0.2g",
                    frequency="bid",
                    route="po",
                ),
                PrescriptionItem(
                    drug_code="DRUG_WAR",
                    dose="5mg",
                    frequency="qd",
                    route="po",
                ),
            ]
        )
        ctx = FieldAgentContext(prescription=rx, patient_flags=("pregnant",))
        agent = PopulationAgent(rule_adapter)
        findings = asyncio.run(agent.run(ctx))
        rule_ids = {f.rule_id for f in findings}
        # DRUG_PREG_CONTRA + pregnant → R-POP-001
        # DRUG_WAR + pregnant → R-POP-002
        assert "R-POP-001" in rule_ids
        assert "R-POP-002" in rule_ids
        # 字段名应带 index 区分
        field_names = {f.field for f in findings}
        assert "items[0].drug_code" in field_names
        assert "items[1].drug_code" in field_names

    def test_unknown_flag_logged_only(
        self, rule_adapter: FakeRuleAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """未知 flag：白名单校验 logger.info 提示；规则适配器不命中。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ]
        )
        ctx = FieldAgentContext(prescription=rx, patient_flags=("unknown_flag",))
        agent = PopulationAgent(rule_adapter)
        with caplog.at_level("INFO", logger="rxchk.agents.population"):
            findings = asyncio.run(agent.run(ctx))
        # 未知 flag + 无对应规则 → 无命中
        assert findings == []
        # logger 应提示未知 flag（白名单校验日志）
        assert any("unknown_flags" in r.message for r in caplog.records)

    def test_recognized_flags_constant(self) -> None:
        """RECOGNIZED_FLAGS 与 population_rules.yaml 患者标签一致。"""
        assert RECOGNIZED_FLAGS == frozenset(
            {
                "pregnant",
                "lactating",
                "pediatric",
                "geriatric",
                "renal_impairment",
                "hepatic_impairment",
            }
        )


# --------------------------------------------------------------------------- #
# 集成：Interaction + Population 双字段族并发跑一张处方                        #
# --------------------------------------------------------------------------- #


class TestInteractionPopulationIntegration:
    """Interaction + Population 双字段族协同：多药品 + 患者标签同时命中。"""

    def test_complex_prescription_hits_both(
        self,
        interaction_db: FakeInteractionDb,
        rule_adapter: FakeRuleAdapter,
    ) -> None:
        """DRUG_WAR + DRUG_ASP + pregnant：interaction + population 同时命中。

        - interaction: WAR + ASP → R-INT-001 (major → high)
        - population: WAR + pregnant → R-POP-002 (high)
        """
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_WAR",
                    dose="5mg",
                    frequency="qd",
                    route="po",
                ),
                PrescriptionItem(
                    drug_code="DRUG_ASP",
                    dose="0.1g",
                    frequency="qd",
                    route="po",
                ),
            ]
        )
        ctx = FieldAgentContext(prescription=rx, patient_flags=("pregnant",))

        int_agent = InteractionAgent(interaction_db)
        pop_agent = PopulationAgent(rule_adapter)

        int_findings = asyncio.run(int_agent.run(ctx))
        pop_findings = asyncio.run(pop_agent.run(ctx))

        # Interaction：R-INT-001
        int_rule_ids = {f.rule_id for f in int_findings}
        assert "R-INT-001" in int_rule_ids

        # Population：R-POP-002 (WAR + pregnant)
        pop_rule_ids = {f.rule_id for f in pop_findings}
        assert "R-POP-002" in pop_rule_ids

    def test_two_agent_ids_match_field_families(
        self,
        interaction_db: FakeInteractionDb,
        rule_adapter: FakeRuleAdapter,
    ) -> None:
        """agent_id 形如 rx_field_<family>，与 family 严格对齐。"""
        from rxchk.coordinator import field_agent_id

        int_agent = InteractionAgent(interaction_db)
        pop_agent = PopulationAgent(rule_adapter)
        assert int_agent.agent_id == field_agent_id(FieldFamily.INTERACTION)
        assert int_agent.agent_id == "rx_field_interaction"
        assert pop_agent.agent_id == field_agent_id(FieldFamily.POPULATION)
        assert pop_agent.agent_id == "rx_field_population"