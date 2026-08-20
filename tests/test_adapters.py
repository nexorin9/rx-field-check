"""`rxchk.tools.rule_adapter / drug_dict / interaction_db / allergy_db` 单元测试。

任务 5 验收范围：
1. **契约一致性**：所有替身实现满足抽象 `Protocol`（`runtime_checkable`）。
2. **替身命中样例**：
   - 规则：`DRUG_AMOX 1g` 单次超上限 → 命中 R-DOSE-001 / severity=high
   - 过敏：`DRUG_AMOX` + `penicillin` 过敏 → 直接命中（severe）
   - 过敏：`DRUG_CEPH` + `penicillin` 过敏 → 家族交叉（cross=true）
   - 配伍：`[DRUG_ASP, DRUG_IBUP]` → 命中 R-INT-002（major）
3. **替身日志打 `[FAKE]` 前缀**（caplog 验证）。
4. **替身不抛错兜底**：未知 drug_code / 解析失败剂量 → 走空 hits 或 info 级。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from rxchk.tools import (
    ALLERGY_SEVERITY_TO_FINDING,
    AllergyDbProtocol,
    DrugDictProtocol,
    FakeAllergyDb,
    FakeDrugDict,
    FakeInteractionDb,
    FakeRuleAdapter,
    InteractionDbProtocol,
    InteractionSeverity,
    INTERACTION_SEVERITY_TO_FINDING,
    RuleAdapterProtocol,
)


# --------------------------------------------------------------------------- #
# 路径                                                                        #
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


@pytest.fixture(scope="module")
def interaction_db() -> FakeInteractionDb:
    return FakeInteractionDb(DATA_DIR / "interaction_db.yaml")


@pytest.fixture(scope="module")
def allergy_db(drug_dict: FakeDrugDict) -> FakeAllergyDb:
    return FakeAllergyDb(DATA_DIR / "allergy_db.yaml", drug_dict=drug_dict)


# --------------------------------------------------------------------------- #
# 契约一致性：runtime_checkable Protocol 验证                                    #
# --------------------------------------------------------------------------- #


class TestProtocolConformance:
    def test_fake_rule_adapter_satisfies_protocol(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        assert isinstance(rule_adapter, RuleAdapterProtocol)

    def test_fake_drug_dict_satisfies_protocol(self, drug_dict: FakeDrugDict) -> None:
        assert isinstance(drug_dict, DrugDictProtocol)

    def test_fake_interaction_db_satisfies_protocol(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        assert isinstance(interaction_db, InteractionDbProtocol)

    def test_fake_allergy_db_satisfies_protocol(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        assert isinstance(allergy_db, AllergyDbProtocol)


# --------------------------------------------------------------------------- #
# 替身加载 + 日志 [FAKE] 前缀                                                   #
# --------------------------------------------------------------------------- #


class TestFakeLogging:
    def test_fake_drug_dict_logs_with_prefix(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="rxchk.tools.drug_dict"):
            FakeDrugDict(DATA_DIR / "drug_dict.yaml")
        # 至少有一条 [FAKE] 前缀日志
        assert any("[FAKE]" in r.message for r in caplog.records)

    def test_fake_rule_adapter_logs_with_prefix(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="rxchk.tools.rule_adapter"):
            FakeRuleAdapter(
                DATA_DIR / "rule" / "dose_rules.yaml",
                DATA_DIR / "rule" / "population_rules.yaml",
            )
        assert any("[FAKE]" in r.message for r in caplog.records)

    def test_fake_interaction_db_logs_with_prefix(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="rxchk.tools.interaction_db"):
            FakeInteractionDb(DATA_DIR / "interaction_db.yaml")
        assert any("[FAKE]" in r.message for r in caplog.records)

    def test_fake_allergy_db_logs_with_prefix(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="rxchk.tools.allergy_db"):
            FakeAllergyDb(DATA_DIR / "allergy_db.yaml")
        assert any("[FAKE]" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# 规则适配器：超剂量命中样例                                                    #
# --------------------------------------------------------------------------- #


class TestRuleAdapterDose:
    def test_overdose_amox_single(self, rule_adapter: FakeRuleAdapter) -> None:
        # DRUG_AMOX 单次 1.0g > 0.5g 单次上限 → 命中 R-DOSE-001 single
        result = rule_adapter.check_dose("DRUG_AMOX", "1g", "po")
        rule_ids = [h.rule_id for h in result.hits]
        assert any("SINGLE" in rid or "DAILY" in rid for rid in rule_ids), (
            f"expected single/daily hit, got {rule_ids}"
        )
        # 至少一条 high severity
        assert any(h.severity == "high" for h in result.hits)
        assert result.parsed_dose == 1.0
        assert result.parsed_unit == "g"

    def test_under_limit_amox_no_hit(self, rule_adapter: FakeRuleAdapter) -> None:
        result = rule_adapter.check_dose("DRUG_AMOX", "0.5g", "po")
        assert result.hits == []

    def test_warfarin_under_strict_window(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        # 5mg = 0.005g；上限 0.005g；恰好等于上限（不超）→ 无命中
        result = rule_adapter.check_dose("DRUG_WAR", "5mg", "po")
        assert result.hits == []

    def test_warfarin_above_strict_window(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        # 7.5mg = 0.0075g；上限 0.0075g；恰好等于上限（不超）
        # 10mg = 0.010g；超上限 → 命中
        result = rule_adapter.check_dose("DRUG_WAR", "10mg", "po")
        rule_ids = [h.rule_id for h in result.hits]
        assert any("SINGLE" in rid or "DAILY" in rid for rid in rule_ids)

    def test_unparseable_dose_returns_info(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        # "bid" 不是剂量字符串 → 解析失败，但保留 evidence 提示（low）
        result = rule_adapter.check_dose("DRUG_AMOX", "bid", "po")
        assert result.parsed_dose is None
        assert result.parsed_unit is None
        # 至少一条提示性命中
        assert any(h.severity == "low" for h in result.hits)

    def test_unknown_drug_no_hit(self, rule_adapter: FakeRuleAdapter) -> None:
        # 字典中没有的药品 → 无命中（不抛错）
        result = rule_adapter.check_dose("DRUG_GHOST", "0.5g", "po")
        assert result.hits == []

    def test_route_not_allowed(self, rule_adapter: FakeRuleAdapter) -> None:
        # DRUG_AMOX 仅允许 po；iv 不在 routes → 不命中规则
        result = rule_adapter.check_dose("DRUG_AMOX", "0.5g", "iv")
        assert result.hits == []

    def test_unit_mg_normalized(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        # 1000mg = 1g > 0.5g → 应命中
        result = rule_adapter.check_dose("DRUG_AMOX", "1000mg", "po")
        rule_ids = [h.rule_id for h in result.hits]
        assert any("SINGLE" in rid or "DAILY" in rid for rid in rule_ids)

    def test_rule_version_stamped(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        # version 透传至命中条目
        result = rule_adapter.check_dose("DRUG_AMOX", "1g", "po")
        for h in result.hits:
            assert h.rule_version == rule_adapter.get_rule_version()
            assert h.rule_version == "v2026.08"


class TestRuleAdapterPopulation:
    def test_pregnancy_contra(self, rule_adapter: FakeRuleAdapter) -> None:
        # DRUG_PREG_CONTRA × pregnant → 命中 R-POP-001
        result = rule_adapter.check_population(
            "DRUG_PREG_CONTRA", ("pregnant",)
        )
        assert len(result.hits) >= 1
        assert any(h.severity == "high" for h in result.hits)
        assert any("孕妇" in h.hit_explanation or "禁忌" in h.hit_explanation for h in result.hits)

    def test_pediatric_ibuprofen_warn(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        result = rule_adapter.check_population("DRUG_IBUP", ("pediatric",))
        assert len(result.hits) == 1
        assert result.hits[0].severity == "medium"

    def test_no_population_match(self, rule_adapter: FakeRuleAdapter) -> None:
        result = rule_adapter.check_population("DRUG_AMOX", ("pregnant",))
        assert result.hits == []


# --------------------------------------------------------------------------- #
# 药品字典：编码 / 上下限 / 途径                                                #
# --------------------------------------------------------------------------- #


class TestDrugDict:
    def test_lookup_basic(self, drug_dict: FakeDrugDict) -> None:
        rec = drug_dict.lookup_drug("DRUG_AMOX")
        assert rec is not None
        assert rec.name == "阿莫西林胶囊"
        assert "po" in rec.routes
        assert rec.max_single_dose_g == 0.5
        assert rec.max_daily_dose_g == 2.0

    def test_lookup_unknown_returns_none(self, drug_dict: FakeDrugDict) -> None:
        assert drug_dict.lookup_drug("DRUG_GHOST") is None

    def test_check_encode_valid(self, drug_dict: FakeDrugDict) -> None:
        assert drug_dict.check_encode("DRUG_AMOX") is True
        assert drug_dict.check_encode("ABC-123") is True

    def test_check_encode_invalid(self, drug_dict: FakeDrugDict) -> None:
        assert drug_dict.check_encode("ab") is False  # 太短
        assert drug_dict.check_encode("drug amox") is False  # 含空格
        assert drug_dict.check_encode("DRUG@amox") is False  # 含 @
        assert drug_dict.check_encode("") is False

    def test_max_single_dose_per_route(self, drug_dict: FakeDrugDict) -> None:
        assert drug_dict.max_single_dose("DRUG_PEN", "iv") == 4.0
        assert drug_dict.max_single_dose("DRUG_PEN", "im") == 2.0
        # 未声明途径 → 回退到药品级
        assert drug_dict.max_single_dose("DRUG_AMOX", "iv") == drug_dict.lookup_drug("DRUG_AMOX").max_single_dose_g

    def test_max_daily_dose_per_route(self, drug_dict: FakeDrugDict) -> None:
        assert drug_dict.max_daily_dose("DRUG_PEN", "iv") == 12.0
        assert drug_dict.max_daily_dose("DRUG_PEN", "im") == 8.0

    def test_frequency_default(self, drug_dict: FakeDrugDict) -> None:
        assert drug_dict.frequency_default("DRUG_AMOX") == "tid"
        assert drug_dict.frequency_default("DRUG_GHOST") is None

    def test_route_form_match(self, drug_dict: FakeDrugDict) -> None:
        assert drug_dict.route_form_match("DRUG_AMOX", "po") is True
        assert drug_dict.route_form_match("DRUG_AMOX", "iv") is False
        assert drug_dict.route_form_match("DRUG_GHOST", "po") is False

    def test_indications_for(self, drug_dict: FakeDrugDict) -> None:
        assert drug_dict.indications_for("DRUG_AMOX") == ("J18.900", "J20.900", "N39.000")
        assert drug_dict.indications_for("DRUG_GHOST") == ()

    def test_dict_version(self, drug_dict: FakeDrugDict) -> None:
        assert drug_dict.get_dict_version() == "v2026.08"


# --------------------------------------------------------------------------- #
# 配伍库：双药 / 三药累加                                                       #
# --------------------------------------------------------------------------- #


class TestInteractionDb:
    def test_two_drug_major_hit(self, interaction_db: FakeInteractionDb) -> None:
        hit = interaction_db.check_pair("DRUG_WAR", "DRUG_ASP")
        assert hit is not None
        assert hit.severity == InteractionSeverity.MAJOR
        # 严重度映射到 finding severity 字段 = high
        assert INTERACTION_SEVERITY_TO_FINDING[hit.severity] == "high"

    def test_two_drug_pair_is_symmetric(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        a = interaction_db.check_pair("DRUG_WAR", "DRUG_ASP")
        b = interaction_db.check_pair("DRUG_ASP", "DRUG_WAR")
        assert a is not None and b is not None
        assert a.rule_id == b.rule_id

    def test_no_pair_returns_none(self, interaction_db: FakeInteractionDb) -> None:
        assert interaction_db.check_pair("DRUG_AMOX", "DRUG_MET") is None

    def test_same_drug_returns_none(self, interaction_db: FakeInteractionDb) -> None:
        assert interaction_db.check_pair("DRUG_AMOX", "DRUG_AMOX") is None

    def test_check_returns_multiple_hits(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        hits = interaction_db.check(["DRUG_WAR", "DRUG_ASP", "DRUG_IBUP"])
        # WAR+ASP major + ASP+IBUP major + 三药 contraindicated
        assert len(hits) >= 2
        sevs = {h.severity for h in hits}
        assert InteractionSeverity.MAJOR in sevs

    def test_triple_contraindicated(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        hits = interaction_db.check(["DRUG_ASP", "DRUG_IBUP", "DRUG_WAR"])
        # 三药累加命中
        assert any(h.severity == InteractionSeverity.CONTRAINDICATED for h in hits)

    def test_severity_level_helper(self, interaction_db: FakeInteractionDb) -> None:
        assert interaction_db.severity_level("DRUG_WAR", "DRUG_ASP") == "high"
        assert interaction_db.severity_level("DRUG_MET", "DRUG_IBUP") == "medium"
        assert interaction_db.severity_level("DRUG_AMOX", "DRUG_MET") is None


# --------------------------------------------------------------------------- #
# 过敏库：直接命中 + 家族交叉                                                   #
# --------------------------------------------------------------------------- #


class TestAllergyDb:
    def test_direct_conflict_penicillin(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        # DRUG_AMOX 在 allergen names 中 → 直接命中
        hits = allergy_db.check("DRUG_AMOX", ["penicillin"])
        assert len(hits) == 1
        assert hits[0].cross_reactivity is False
        assert hits[0].severity.value == "severe"

    def test_family_cross_cephalosporin(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        # 患者对 penicillin 过敏；DRUG_CEPH 在 cephalosporin 家族 → 交叉
        hits = allergy_db.check("DRUG_CEPH", ["penicillin"])
        assert len(hits) == 1
        assert hits[0].cross_reactivity is True
        # 家族交叉：severe → moderate
        assert hits[0].severity.value == "moderate"

    def test_no_allergy_no_hit(self, allergy_db: FakeAllergyDb) -> None:
        assert allergy_db.check("DRUG_AMOX", []) == []

    def test_unknown_allergy_safe(self, allergy_db: FakeAllergyDb) -> None:
        # 患者过敏「花粉」不在库 → 不命中（不伪通过）
        hits = allergy_db.check("DRUG_AMOX", ["花粉"])
        assert hits == []

    def test_unknown_drug_safe(self, allergy_db: FakeAllergyDb) -> None:
        # 药品不在字典 → 不命中（drug_dict 注入；非药品名匹配）
        hits = allergy_db.check("DRUG_GHOST", ["penicillin"])
        # 没匹配 drug_name 即便有 penicillin 过敏，也无 cross hit
        assert hits == []

    def test_family_cross_helper(self, allergy_db: FakeAllergyDb) -> None:
        # family_cross_reactivity 助手：仅返回交叉命中
        cross = allergy_db.family_cross_reactivity("DRUG_CEPH", "penicillin")
        assert cross is not None
        assert cross.cross_reactivity is True
        # 直接命中 None
        assert allergy_db.family_cross_reactivity("DRUG_AMOX", "penicillin") is None

    def test_severity_for_helper(self, allergy_db: FakeAllergyDb) -> None:
        assert allergy_db.severity_for("penicillin") == "high"
        assert allergy_db.severity_for("aspirin") == "medium"
        assert allergy_db.severity_for("ibuprofen") == "low"
        assert allergy_db.severity_for("花粉") is None

    def test_multiple_allergies_one_drug(self, allergy_db: FakeAllergyDb) -> None:
        hits = allergy_db.check("DRUG_AMOX", ["penicillin", "aspirin"])
        # penicillin 命中；aspirin 与 DRUG_AMOX 不在 aspirin names → 不命中
        rule_ids = [h.rule_id for h in hits]
        assert any("PENICILLIN" in rid for rid in rule_ids)


# --------------------------------------------------------------------------- #
# YAML 缺失 / 错误兜底                                                          #
# --------------------------------------------------------------------------- #


class TestAdapterErrorPaths:
    def test_missing_rule_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(Exception):  # RuleAdapterError
            FakeRuleAdapter(
                tmp_path / "missing_dose.yaml",
                tmp_path / "missing_pop.yaml",
            )

    def test_missing_drug_dict_raises(self, tmp_path: Path) -> None:
        from rxchk.tools.drug_dict import DrugDictError
        with pytest.raises(DrugDictError):
            FakeDrugDict(tmp_path / "missing.yaml")

    def test_missing_interaction_db_raises(self, tmp_path: Path) -> None:
        from rxchk.tools.interaction_db import InteractionDbError
        with pytest.raises(InteractionDbError):
            FakeInteractionDb(tmp_path / "missing.yaml")

    def test_missing_allergy_db_raises(self, tmp_path: Path) -> None:
        from rxchk.tools.allergy_db import AllergyDbError
        with pytest.raises(AllergyDbError):
            FakeAllergyDb(tmp_path / "missing.yaml")

    def test_runtime_version_stamp(self) -> None:
        from rxchk.tools.rule_adapter import runtime_version_stamp
        meta = runtime_version_stamp("v2026.08")
        assert meta == {"rule_version": "v2026.08"}