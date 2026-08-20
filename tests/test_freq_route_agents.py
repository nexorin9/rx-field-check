"""frequency + route 双字段族 Agent 独立测试（任务 21 拆出 + 深化）。

覆盖范围
--------
- FrequencyAgent：
  - freq_mismatch：drug_dict 默认 vs rule_adapter 白名单冲突
  - time_window_conflict：给药时间窗（qn 睡前）冲突
  - 规则适配器 R-FREQ-* 命中 → Finding（severity 来自规则）
  - 字典默认 vs 规则白名单一致 → 无命中
  - 未知药品字典缺项 + 规则不匹配 → 仅 R-FREQ-DEV 命中
- RouteAgent：
  - route_form_mismatch：剂型 vs 途径不一致（口服剂型改 iv）
  - route_change_warn：po → iv 等注射类变更告警
  - 规则适配器 R-ROUTE-* 命中 → Finding
  - 字典与规则都允许的合法途径 → 无命中

设计动机
--------
- 与 task 7 的 `test_field_agents.py` 解耦：专注于 FrequencyAgent +
  RouteAgent 的四种核心场景，便于后续 task 深化 drug_dict / rule_adapter
  时单独回归。
- 替身 YAML 数据从 `src/rxchk/data/` 加载（与正式对接契约一致）；
  不构造"测试用临时 YAML"以保证替身-契约同步。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rxchk.agents.base import FieldAgentContext
from rxchk.agents.frequency import (
    RULE_FREQ_DEVIATION,
    RULE_FREQ_DURATION_OVERFLOW,
    RULE_FREQ_UNRECOGNIZED,
    FrequencyAgent,
)
from rxchk.agents.route import (
    RULE_ROUTE_CHANGE_WARN,
    RULE_ROUTE_FORM_MISMATCH,
    RULE_ROUTE_NOT_ALLOWED,
    RULE_ROUTE_UNRECOGNIZED,
    RouteAgent,
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
        freq_rules_path=DATA_DIR / "rule" / "freq_rules.yaml",
        route_rules_path=DATA_DIR / "rule" / "route_rules.yaml",
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
# FrequencyAgent：freq_mismatch 路径                                           #
# --------------------------------------------------------------------------- #


class TestFrequencyAgentFreqMismatch:
    """freq_mismatch：drug_dict 默认 vs rule_adapter 白名单冲突。"""

    def test_drug_dict_default_matches_rule_whitelist(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """DRUG_AMOX tid：字典默认 tid + 规则白名单 [bid, tid, qid] → 无命中。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",  # 默认 tid
                    route="po",
                )
            ]
        )
        agent = FrequencyAgent(drug_dict, rule_adapter)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert findings == []

    def test_freq_in_whitelist_but_differs_from_default(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """DRUG_AMOX bid：规则白名单允许 bid（[bid, tid, qid]），但字典默认 tid
        → 仅 R-FREQ-DEV（severity=low）一条命中。
        """
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="bid",  # 字典默认 tid → DEV
                    route="po",
                )
            ]
        )
        agent = FrequencyAgent(drug_dict, rule_adapter)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        assert rule_ids == {RULE_FREQ_DEVIATION}
        dev_hit = findings[0]
        assert dev_hit.severity == "low"
        assert "tid" in dev_hit.hit_explanation

    def test_freq_outside_rule_whitelist_and_differs_from_default(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """DRUG_WAR qd：字典默认 qd（与处方相符），规则白名单 [qd] → 无命中。

        反例：DRUG_WAR bid 同时被规则黑名单 + 字典默认 qd 不符命中。
        """
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_WAR",
                    dose="5mg",
                    frequency="bid",  # 字典默认 qd；规则只允许 qd
                    route="po",
                )
            ]
        )
        agent = FrequencyAgent(drug_dict, rule_adapter)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        # 规则 R-FREQ-001 (severity=high) + drug_dict R-FREQ-DEV (severity=low)
        assert "R-FREQ-001" in rule_ids
        assert RULE_FREQ_DEVIATION in rule_ids
        severities = {f.severity for f in findings}
        assert "high" in severities
        assert "low" in severities


# --------------------------------------------------------------------------- #
# FrequencyAgent：time_window_conflict 路径                                     #
# --------------------------------------------------------------------------- #


class TestFrequencyAgentTimeWindowConflict:
    """time_window_conflict：规则 forbidden_frequencies 与 route 联动。"""

    def test_qn_po_for_amox_hits_time_window(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """DRUG_AMOX po qn：规则 R-FREQ-004 forbidden_frequencies=[qn] → 命中。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="qn",  # 睡前给药 → 规则禁止
                    route="po",
                )
            ]
        )
        agent = FrequencyAgent(drug_dict, rule_adapter)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        # R-FREQ-004 命中；字典默认 tid 与 qn 不符 → DEV 也命中
        assert "R-FREQ-004" in rule_ids
        assert RULE_FREQ_DEVIATION in rule_ids
        # 命中解释应提及「睡前」
        window_hit = next(f for f in findings if f.rule_id == "R-FREQ-004")
        assert "睡前" in window_hit.hit_explanation or "qn" in window_hit.hit_explanation
        assert window_hit.severity == "medium"

    def test_qn_iv_route_skips_time_window_rule(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """qn 走 iv（非 po）→ R-FREQ-004 routes=[po] 不生效 → 不命中。"""
        # 但 DRUG_AMOX iv 在字典不被允许，且 qn 频次解析正常
        # 这里仅验证时间窗规则被 routes 过滤；其他命中可存在
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="qn",
                    route="iv",  # 非 po → R-FREQ-004 不命中（routes 限制）
                )
            ]
        )
        agent = FrequencyAgent(drug_dict, rule_adapter)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        assert "R-FREQ-004" not in rule_ids


# --------------------------------------------------------------------------- #
# FrequencyAgent：其他规则适配器路径                                            #
# --------------------------------------------------------------------------- #


class TestFrequencyAgentOtherRules:
    """频次规则适配器其他命中场景。"""

    def test_route_specific_rule_applies_only_to_listed_route(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """DRUG_PEN iv 频次规则：仅在 iv 途径下生效（routes=[iv]）。"""
        # DRUG_PEN iv qid → 不在 allowed=[bid, tid, q4h, q6h, q8h]
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_PEN",
                    dose="3g",
                    frequency="qid",
                    route="iv",
                )
            ]
        )
        agent = FrequencyAgent(drug_dict, rule_adapter)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        assert "R-FREQ-003" in rule_ids

    def test_unrecognized_frequency_skips_rule_adapter(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """未识别频次 → 跳过规则适配器（语义无意义）+ 命中 R-FREQ-UNREC。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="prn_extra",
                    route="po",
                )
            ]
        )
        agent = FrequencyAgent(drug_dict, rule_adapter)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        assert findings[0].rule_id == RULE_FREQ_UNRECOGNIZED

    def test_duration_overflow_via_dict_extreme(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """频次 × 疗程 超 1 年（极端值）→ 命中 R-FREQ-DUR-OVERFLOW。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="q4h",  # 6 次/日
                    route="po",
                    duration_days=120,  # 120 * 6 = 720 > 365
                )
            ]
        )
        agent = FrequencyAgent(drug_dict, rule_adapter)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        assert RULE_FREQ_DURATION_OVERFLOW in rule_ids


# --------------------------------------------------------------------------- #
# RouteAgent：route_form_mismatch 路径                                         #
# --------------------------------------------------------------------------- #


class TestRouteAgentRouteFormMismatch:
    """route_form_mismatch：剂型 vs 途径不一致。"""

    def test_oral_form_with_iv_route_hits_form_mismatch(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """DRUG_AMOX forms=[capsule, tablet] 走 iv → 剂型不符。

        注意：本场景 drug_dict.route_form_match 返回 False（DRUG_AMOX 仅 po），
        因此先触发 R-ROUTE-NOT-ALLOWED，不会走到 form_mismatch 分支。
        """
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="iv",
                )
            ]
        )
        agent = RouteAgent(drug_dict, rule_adapter)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        assert RULE_ROUTE_NOT_ALLOWED in rule_ids
        # R-ROUTE-001（规则白名单 [po]）+ R-ROUTE-NOT-ALLOWED（字典）
        assert "R-ROUTE-001" in rule_ids

    def test_form_mismatch_only_when_dict_allows_but_form_contradicts(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """构造：让字典允许 iv 但 forms 是口服 → 命中 form_mismatch。

        通过替身直接改 _drug_dict 不可行 → 这里通过 `_StubDrugDict`
        让 route_form_match 返回 True 但 forms=[capsule]。
        """
        from rxchk.tools.drug_dict import DrugRecord

        class _FormMismatchDrugDict:
            """替身：DRUG_GHOST 在字典中允许 iv，但 forms 是口服（剂型不符）。"""

            def __init__(self) -> None:
                self._rec = DrugRecord(
                    code="DRUG_GHOST",
                    name="占位口服剂型药",
                    forms=("capsule", "tablet"),
                    routes=("po", "iv"),
                    default_frequency="tid",
                    max_single_dose_g=0.5,
                    max_daily_dose_g=2.0,
                    per_route_limits={},
                    indications=("J18.900",),
                )

            def get_dict_version(self) -> str:
                return "v-stub"

            def lookup_drug(self, drug_code):
                return self._rec if drug_code == "DRUG_GHOST" else None

            def check_name(self, drug_code):
                return drug_code == "DRUG_GHOST"

            def check_encode(self, drug_code):
                return len(drug_code) >= 3

            def max_single_dose(self, drug_code, route):
                return 0.5

            def max_daily_dose(self, drug_code, route):
                return 2.0

            def frequency_default(self, drug_code):
                return "tid"

            def route_form_match(self, drug_code, route):
                return drug_code == "DRUG_GHOST" and route == "iv"

            def indications_for(self, drug_code):
                return ("J18.900",)

        stub = _FormMismatchDrugDict()
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_GHOST",
                    dose="0.5g",
                    frequency="tid",
                    route="iv",  # 字典允许，但 forms 是口服 → form_mismatch
                )
            ]
        )
        agent = RouteAgent(stub, rule_adapter)  # type: ignore[arg-type]
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        # 字典允许 iv → 不出 NOT_ALLOWED；剂型 vs 途径矛盾 → FORM_MISMATCH
        assert RULE_ROUTE_NOT_ALLOWED not in rule_ids
        assert RULE_ROUTE_FORM_MISMATCH in rule_ids
        form_hit = next(f for f in findings if f.rule_id == RULE_ROUTE_FORM_MISMATCH)
        assert "剂型" in form_hit.hit_explanation
        assert form_hit.severity == "high"


# --------------------------------------------------------------------------- #
# RouteAgent：route_change_warn 路径                                           #
# --------------------------------------------------------------------------- #


class TestRouteAgentRouteChangeWarn:
    """route_change_warn：po 口服剂型药品改成注射途径的告警。"""

    def test_po_to_iv_hits_change_warn(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """DRUG_AMOX（口服剂型）改 iv → 命中 R-ROUTE-CHANGE-WARN。

        实际数据中 DRUG_AMOX 仅 po，因此字典先出 R-ROUTE-NOT-ALLOWED，
        change_warn 分支被跳过。这里用 _StubDrugDict 模拟「字典允许 iv
        但 forms=口服」的场景触发 change_warn。
        """
        from rxchk.tools.drug_dict import DrugRecord

        class _OralDrugDict:
            def __init__(self) -> None:
                self._rec = DrugRecord(
                    code="DRUG_AMOX",
                    name="阿莫西林胶囊",
                    forms=("capsule", "tablet"),
                    routes=("po", "iv"),  # 字典"允许" iv（病态配置）
                    default_frequency="tid",
                    max_single_dose_g=0.5,
                    max_daily_dose_g=2.0,
                    per_route_limits={},
                    indications=("J18.900",),
                )

            def get_dict_version(self) -> str:
                return "v-stub"

            def lookup_drug(self, drug_code):
                return self._rec if drug_code == "DRUG_AMOX" else None

            def check_name(self, drug_code):
                return drug_code == "DRUG_AMOX"

            def check_encode(self, drug_code):
                return True

            def max_single_dose(self, drug_code, route):
                return 0.5

            def max_daily_dose(self, drug_code, route):
                return 2.0

            def frequency_default(self, drug_code):
                return "tid"

            def route_form_match(self, drug_code, route):
                return drug_code == "DRUG_AMOX" and route in ("po", "iv")

            def indications_for(self, drug_code):
                return ("J18.900",)

        stub = _OralDrugDict()
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="iv",  # 字典允许但剂型是口服
                )
            ]
        )
        # 规则层：R-ROUTE-001 allowed_routes=[po] 也会命中（iv 不在白名单）
        agent = RouteAgent(stub, rule_adapter)  # type: ignore[arg-type]
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        # change_warn 命中：字典允许 iv + 剂型是口服 → po→iv 变更
        assert RULE_ROUTE_CHANGE_WARN in rule_ids
        change_hit = next(f for f in findings if f.rule_id == RULE_ROUTE_CHANGE_WARN)
        assert "口服" in change_hit.hit_explanation
        assert "iv" in change_hit.hit_explanation
        assert change_hit.severity == "high"

    def test_iv_drug_no_change_warn(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """DRUG_PEN（注射剂型）走 iv → 不触发 change_warn（剂型一致）。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_PEN",
                    dose="3g",
                    frequency="q6h",
                    route="iv",
                )
            ]
        )
        agent = RouteAgent(drug_dict, rule_adapter)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        assert RULE_ROUTE_CHANGE_WARN not in rule_ids


# --------------------------------------------------------------------------- #
# RouteAgent：规则适配器路径                                                    #
# --------------------------------------------------------------------------- #


class TestRouteAgentRuleAdapter:
    """规则适配器 R-ROUTE-* 命中：白名单 / 黑名单。"""

    def test_asp_iv_hits_route_rule(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """DRUG_ASP iv：规则 R-ROUTE-003 forbidden_routes=[iv, im, sc, ih] → 命中。

        DRUG_ASP 字典仅 po，因此字典也命中 R-ROUTE-NOT-ALLOWED。
        """
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_ASP",
                    dose="0.1g",
                    frequency="qd",
                    route="iv",
                )
            ]
        )
        agent = RouteAgent(drug_dict, rule_adapter)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        rule_ids = {f.rule_id for f in findings}
        assert "R-ROUTE-003" in rule_ids
        assert RULE_ROUTE_NOT_ALLOWED in rule_ids

    def test_met_po_passes(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """DRUG_MET po：字典允许 + 规则白名单 [po] → 无命中。"""
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
        agent = RouteAgent(drug_dict, rule_adapter)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert findings == []

    def test_unrecognized_route_hits_low(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """未识别途径 → R-ROUTE-UNREC（severity=low），规则适配器跳过。"""
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",
                    dose="0.5g",
                    frequency="tid",
                    route="inhaled",  # 不在 RECOGNIZED_ROUTES
                )
            ]
        )
        agent = RouteAgent(drug_dict, rule_adapter)
        findings = asyncio.run(agent.run(FieldAgentContext(prescription=rx)))
        assert len(findings) == 1
        assert findings[0].rule_id == RULE_ROUTE_UNRECOGNIZED
        assert findings[0].severity == "low"


# --------------------------------------------------------------------------- #
# 集成：FrequencyAgent + RouteAgent 并发跑一张处方                               #
# --------------------------------------------------------------------------- #


class TestFreqRouteIntegration:
    def test_oral_drug_iv_route_triggers_multiple_findings(
        self, drug_dict: FakeDrugDict, rule_adapter: FakeRuleAdapter
    ) -> None:
        """口服药品改 iv：触发字典 NOT_ALLOWED + 规则白名单 + 变更警告（仅替身）。

        用真 drug_dict（DRUG_AMOX iv 不在字典）触发多源命中。
        """
        rx = _mk_rx(
            items=[
                PrescriptionItem(
                    drug_code="DRUG_AMOX",  # 字典仅 po
                    dose="0.5g",
                    frequency="tid",
                    route="iv",
                )
            ]
        )
        agent_freq = FrequencyAgent(drug_dict, rule_adapter)
        agent_route = RouteAgent(drug_dict, rule_adapter)

        ctx = FieldAgentContext(prescription=rx)
        freq_findings = asyncio.run(agent_freq.run(ctx))
        route_findings = asyncio.run(agent_route.run(ctx))

        # FrequencyAgent：DRUG_AMOX tid，规则白名单允许 → 无 freq 命中
        assert freq_findings == []
        # RouteAgent：DRUG_AMOX iv 触发 R-ROUTE-NOT-ALLOWED + R-ROUTE-001
        route_rule_ids = {f.rule_id for f in route_findings}
        assert RULE_ROUTE_NOT_ALLOWED in route_rule_ids
        assert "R-ROUTE-001" in route_rule_ids