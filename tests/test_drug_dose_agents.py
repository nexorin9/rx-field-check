"""drug + dose 双字段族 Agent 独立测试（任务 20 拆出 + 深化）。

覆盖范围：
- DrugAgent：合法编码无命中 / 非法编码命中 / 字典缺项命中 / 多 item
- DoseAgent：基本处方无命中 / 解析失败 low 级 / drug_dict 安全冗余
  (单次超 drug_dict 单次上限) / drug_dict 临床必警示 (单次超日上限) /
  per-route 上下限区分（DRUG_PEN iv vs im）/ 解析失败 → drug_dict
  检查也跳过

设计动机
--------
- 与 task 7 的 `test_field_agents.py` 解耦：专注于 DrugAgent + DoseAgent
  的四种核心场景（basic / encode_error / overdose / per_route_limits），
  便于后续 task 28 扩展 drug_dict 时单独回归。
- 替身 YAML 数据从 `src/rxchk/data/` 加载（与正式对接契约一致）；
  不构造「测试用临时 YAML」以保证替身-契约同步。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rxchk.agents.base import FieldAgentContext
from rxchk.agents.dose import (
    RULE_DICT_OVER_DAILY,
    RULE_DICT_SINGLE,
    DoseAgent,
)
from rxchk.agents.drug import (
    RULE_ENCODE_INVALID,
    RULE_NAME_NOT_FOUND,
    DrugAgent,
)
from rxchk.models.prescription import Prescription, PrescriptionItem
from rxchk.tools.drug_dict import FakeDrugDict
from rxchk.tools.rule_adapter import FakeRuleAdapter


# --------------------------------------------------------------------------- #
# 路径 / fixture                                                              #
# --------------------------------------------------------------------------- #


DATA_DIR = Path(__file__).resolve().parent.parent / "src" / "rxchk" / "data"


@pytest.fixture(scope="module")
def drug_dict() -> FakeDrugDict:
    return FakeDrugDict(DATA_DIR / "drug_dict.yaml")


@pytest.fixture(scope="module")
def rule_adapter() -> FakeRuleAdapter:
    return FakeRuleAdapter(
        DATA_DIR / "rule" / "dose_rules.yaml",
        DATA_DIR / "rule" / "population_rules.yaml",
    )


def _mk_rx(
    *,
    items: list[PrescriptionItem],
    rx_no: str = "RX-2026-<EXAMPLE>",
) -> Prescription:
    return Prescription(
        prescription_no=rx_no,
        patient_id="<EXAMPLE_PATIENT>",
        visit_no="<EXAMPLE_VISIT>",
        doctor_id="<EXAMPLE_DOCTOR>",
        items=items,
        diagnoses=[],
        allergies=[],
    )


# --------------------------------------------------------------------------- #
# DrugAgent                                                                   #
# --------------------------------------------------------------------------- #


class TestDrugAgentBasic:
    """basic 路径：合法编码 + 字典收录 → 无命中。"""

    def test_no_hit_for_valid_code(self, drug_dict: FakeDrugDict) -> None:
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
        agent = DrugAgent(drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert findings == []


class TestDrugAgentEncodeError:
    """encode_error 路径：编码不合法 → 高危命中。"""

    def test_invalid_encode_hits(self, drug_dict: FakeDrugDict) -> None:
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="bad code",  # 含空格 → 编码非法
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ]
        )
        agent = DrugAgent(drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        f = findings[0]
        assert f.rule_id == RULE_ENCODE_INVALID
        assert f.severity == "high"
        assert f.family == "drug"
        assert "drug_code" in f.field
        assert "drug_code" in f.field_value or f.field_value == "bad code"

    def test_unknown_drug_hits_when_encoding_valid(
        self, drug_dict: FakeDrugDict
    ) -> None:
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_GHOST",  # 编码合法但字典无此药
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ]
        )
        agent = DrugAgent(drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        finding = findings[0]
        assert finding.rule_id == RULE_NAME_NOT_FOUND
        assert finding.severity == "high"
        assert "字典" in finding.hit_explanation or "收录" in finding.hit_explanation

    @pytest.mark.parametrize(
        "bad_code",
        ["ab", "abc def", "drug with space", "lowercase_only", "中文"],
    )
    def test_various_invalid_encodes(
        self, drug_dict: FakeDrugDict, bad_code: str
    ) -> None:
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code=bad_code,
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                )
            ]
        )
        agent = DrugAgent(drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        # 至少一条 encode 命中（中文直接被拒绝 → 无 finding 因为 check_encode
        # 会 False 命中；其他情况也类似）
        encode_findings = [
            f for f in findings if f.rule_id == RULE_ENCODE_INVALID
        ]
        assert len(encode_findings) >= 1


class TestDrugAgentMultiItem:
    """多 item 处方：每条独立核对。"""

    def test_one_invalid_one_valid(self, drug_dict: FakeDrugDict) -> None:
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",  # 合法
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                ),
                PrescriptionItem(
                    drug_code="invalid code",  # 非法
                    dose="0.5g",
                    frequency="tid",
                    route="po",
                ),
            ]
        )
        agent = DrugAgent(drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        assert findings[0].rule_id == RULE_ENCODE_INVALID
        assert "items[1]" in findings[0].field


# --------------------------------------------------------------------------- #
# DoseAgent                                                                   #
# --------------------------------------------------------------------------- #


class TestDoseAgentBasic:
    """basic 路径：在限内 → 无命中。"""

    def test_no_hit_for_in_range(
        self, rule_adapter: FakeRuleAdapter, drug_dict: FakeDrugDict
    ) -> None:
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",  # max_single=0.5
                    frequency="tid",
                    route="po",
                )
            ]
        )
        agent = DoseAgent(rule_adapter, drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert findings == []


class TestDoseAgentOverdose:
    """overdose 路径：超单次 / 超日剂量 → 命中。"""

    def test_over_single_via_rule_adapter(
        self, rule_adapter: FakeRuleAdapter, drug_dict: FakeDrugDict
    ) -> None:
        """1g > 0.5g → 规则适配器命中 R-DOSE-001-SINGLE（drug_dict 单次检查跳过）。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="1g",
                    frequency="tid",
                    route="po",
                )
            ]
        )
        agent = DoseAgent(rule_adapter, drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        assert "R-DOSE-001-SINGLE" in rule_ids
        # 规则适配器已 -SINGLE → drug_dict 单次检查跳过 → 不重复
        assert RULE_DICT_SINGLE not in rule_ids

    def test_drug_dict_safety_when_rule_adapter_misses(
        self, rule_adapter: FakeRuleAdapter, drug_dict: FakeDrugDict
    ) -> None:
        """规则库无该药规则时，drug_dict 仍能命中。

        本测试通过构造一个空规则的 FakeRuleAdapter 替身验证 drug_dict 冗余层
        —— 即便规则升级空窗或规则集漏药，药品字典给出的硬上限也会命中。
        """
        from rxchk.tools.rule_adapter import (
            DoseCheckResult,
            PopulationCheckResult,
            RuleAdapterProtocol,
        )

        class _EmptyRuleAdapter:
            """替身：始终返回空 hits。"""

            def get_rule_version(self) -> str:
                return "v-empty"

            def check_dose(self, drug_code, dose, route):
                # 简单解析（g/mg → g）
                value_g: float | None = None
                unit: str | None = None
                s = (dose or "").strip().lower().replace(" ", "")
                for u in ("mg", "g"):
                    if s.endswith(u):
                        try:
                            v = float(s[: -len(u)])
                            if u == "mg":
                                v /= 1000.0
                            value_g = v
                            unit = u
                            break
                        except ValueError:
                            pass
                return DoseCheckResult(
                    drug_code=drug_code,
                    route=route,
                    parsed_dose=value_g,
                    parsed_unit=unit,
                    hits=[],
                )

            def check_population(self, drug_code, patient_flags):
                return PopulationCheckResult(patient_flags=patient_flags, hits=[])

        # Protocol 校验：替身仍满足 RuleAdapterProtocol（结构性子类型）
        empty: RuleAdapterProtocol = _EmptyRuleAdapter()

        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="1g",  # > max_single_dose 0.5g
                    frequency="tid",
                    route="po",
                )
            ]
        )
        agent = DoseAgent(empty, drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        # drug_dict 安全层应命中
        assert RULE_DICT_SINGLE in rule_ids
        assert all(f.severity == "high" for f in findings)
        # 规则 ID 不应来自规则适配器（因为空规则）
        assert "R-DOSE-001-SINGLE" not in rule_ids

    def test_single_over_daily_limit_emits_clinical_warning(
        self, rule_adapter: FakeRuleAdapter, drug_dict: FakeDrugDict
    ) -> None:
        """单次即超日剂量上限 → 临床必警示（R-DOSE-DICT-OVER-DAILY）。"""
        # DRUG_WAR：单次上限 0.005g (5mg)，日上限 0.0075g (7.5mg)
        # 给出 10mg = 0.010g → 单次即超过日上限
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_WAR",
                    dose="10mg",
                    frequency="qd",
                    route="po",
                )
            ]
        )
        agent = DoseAgent(rule_adapter, drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        # 规则适配器命中 -SINGLE + drug_dict 也命中 -OVER-DAILY
        assert "R-DOSE-004-SINGLE" in rule_ids
        assert RULE_DICT_OVER_DAILY in rule_ids
        # 临床必警示必须是 high 级
        over_daily = next(f for f in findings if f.rule_id == RULE_DICT_OVER_DAILY)
        assert over_daily.severity == "high"
        assert "临床必警示" in over_daily.hit_explanation


class TestDoseAgentPerRouteLimits:
    """per-route 上下限：DRUG_PEN iv vs im 不同上限。"""

    def test_pen_iv_in_range(
        self, rule_adapter: FakeRuleAdapter, drug_dict: FakeDrugDict
    ) -> None:
        """DRUG_PEN iv：max_single=4g / max_daily=12g；3g iv 不超。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_PEN",
                    dose="3g",
                    frequency="bid",
                    route="iv",
                )
            ]
        )
        agent = DoseAgent(rule_adapter, drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert findings == []

    def test_pen_im_over_single_via_drug_dict(
        self, rule_adapter: FakeRuleAdapter, drug_dict: FakeDrugDict
    ) -> None:
        """DRUG_PEN im：max_single=2g；3g im 超单次。

        规则库 R-DOSE-003 只配置 routes=[iv]，im 不在规则内 → 规则适配器不命中；
        drug_dict per_route_limits.im.single_g=2.0 应命中 R-DOSE-DICT-SINGLE。
        """
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_PEN",
                    dose="3g",  # > per_route_limits.im.single_g = 2.0
                    frequency="bid",
                    route="im",
                )
            ]
        )
        agent = DoseAgent(rule_adapter, drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        # 规则适配器无 im 规则 → 不命中
        assert "R-DOSE-003-SINGLE" not in rule_ids
        # drug_dict per-route 上限应命中
        assert RULE_DICT_SINGLE in rule_ids
        # 命中证据应指明 im 途径
        finding = next(f for f in findings if f.rule_id == RULE_DICT_SINGLE)
        assert "im" in finding.evidence
        assert finding.severity == "high"

    def test_pen_iv_over_daily_emits_over_daily(
        self, rule_adapter: FakeRuleAdapter, drug_dict: FakeDrugDict
    ) -> None:
        """DRUG_PEN iv：max_daily=12g；13g iv 单次即超日上限。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_PEN",
                    dose="13g",  # > max_daily=12g
                    frequency="qd",
                    route="iv",
                )
            ]
        )
        agent = DoseAgent(rule_adapter, drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        assert "R-DOSE-003-SINGLE" in rule_ids
        assert RULE_DICT_OVER_DAILY in rule_ids


class TestDoseAgentParseError:
    """解析失败路径：drug_dict 检查跳过，仅规则适配器 low 级命中。"""

    def test_unparseable_dose_skips_drug_dict(
        self, rule_adapter: FakeRuleAdapter, drug_dict: FakeDrugDict
    ) -> None:
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="bid",  # 频次伪剂量 → 解析失败
                    frequency="tid",
                    route="po",
                )
            ]
        )
        agent = DoseAgent(rule_adapter, drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        # 解析失败 → drug_dict 跳过 → 无 drug_dict 命中
        rule_ids = {f.rule_id for f in findings}
        assert RULE_DICT_SINGLE not in rule_ids
        assert RULE_DICT_OVER_DAILY not in rule_ids
        # 规则适配器仍出 low 级提示
        assert any(f.severity == "low" for f in findings)
        assert all(f.family == "dose" for f in findings)


class TestDoseAgentDrugNotInDict:
    """drug_dict 找不到药品时：drug_dict 检查返回 None → 不命中。"""

    def test_unknown_drug_no_drug_dict_hit(
        self, rule_adapter: FakeRuleAdapter, drug_dict: FakeDrugDict
    ) -> None:
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_GHOST",  # 字典无此药
                    dose="1g",
                    frequency="tid",
                    route="po",
                )
            ]
        )
        agent = DoseAgent(rule_adapter, drug_dict)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        # drug_dict.max_single_dose(None) → None → 不命中
        assert RULE_DICT_SINGLE not in rule_ids
        assert RULE_DICT_OVER_DAILY not in rule_ids


# --------------------------------------------------------------------------- #
# 集成：DrugAgent + DoseAgent 并发跑一张处方（不动协调器）                       #
# --------------------------------------------------------------------------- #


class TestDrugAndDoseIntegration:
    def test_complex_sample_yields_drug_and_dose_findings(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """复杂样例：DrugAgent + DoseAgent 共发 → findings 按 family 分组。"""
        from rxchk.parsers.prescription import parse_prescription

        rx_path = DATA_DIR / "sample_rx" / "rx_overdose.json"
        rx = parse_prescription(rx_path)
        ctx = FieldAgentContext(prescription=rx)
        drug_agent = DrugAgent(drug_dict)
        dose_agent = DoseAgent(rule_adapter, drug_dict)

        drug_findings = asyncio.run(drug_agent.run(ctx))
        dose_findings = asyncio.run(dose_agent.run(ctx))

        # 编码都合法 → DrugAgent 无命中
        assert drug_findings == []
        # DoseAgent 至少命中 DRUG_AMOX 单次超 + DRUG_WAR 单次超
        rule_ids = {f.rule_id for f in dose_findings}
        assert "R-DOSE-001-SINGLE" in rule_ids
        assert "R-DOSE-004-SINGLE" in rule_ids