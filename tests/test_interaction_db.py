"""`rxchk.tools.interaction_db` 深度单元测试。

任务 29 验收范围：

1. **契约一致性**：`FakeInteractionDb` 满足抽象 `InteractionDbProtocol`
   （`runtime_checkable`）。
2. **两药配伍命中（two_drug_major / two_drug_minor）**：
   - 已知 `DRUG_WAR + DRUG_ASP` → severity=major → finding severity="high"
   - 已知 `DRUG_AMOX + DRUG_IBUP` → severity=minor → finding severity="low"
3. **三药累加命中（three_drug_contra）**：
   - `check([DRUG_ASP, DRUG_IBUP, DRUG_WAR])` 含 contraindicated 命中
   - 缺一药 → 不命中该 triple
4. **未知组合（unknown_combo）**：
   - 不在配置中的两药 / 三药 → `check_pair` 返回 None；
     `check` 返回空 list
5. **严重度等级映射（severity_escalation）**：
   - `SEVERITY_TO_FINDING` 四档映射正确
   - `severity_level()` 返回 finding severity（high / medium / low）
6. **双向命中（bidirectional）**：`check_pair(A, B)` 与 `check_pair(B, A)` 等价。
7. **替身日志 [FAKE] 前缀**（caplog 验证）。
8. **错误路径**：YAML 缺失 / 解析失败 / pair 长度错误 → 抛 `InteractionDbError`。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from rxchk.tools.interaction_db import (
    FakeInteractionDb,
    InteractionDbError,
    InteractionDbProtocol,
    InteractionHit,
    InteractionSeverity,
    SEVERITY_TO_FINDING,
)


# --------------------------------------------------------------------------- #
# 路径 / fixture                                                               #
# --------------------------------------------------------------------------- #


DATA_DIR = Path(__file__).resolve().parent.parent / "src" / "rxchk" / "data"
INTERACTION_DB_PATH = DATA_DIR / "interaction_db.yaml"


@pytest.fixture(scope="module")
def interaction_db() -> FakeInteractionDb:
    return FakeInteractionDb(INTERACTION_DB_PATH)


# --------------------------------------------------------------------------- #
# 契约一致性                                                                   #
# --------------------------------------------------------------------------- #


class TestProtocolConformance:
    def test_fake_satisfies_protocol(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        assert isinstance(interaction_db, InteractionDbProtocol)

    def test_protocol_exposes_required_methods(self) -> None:
        required = {"get_db_version", "check_pair", "check", "severity_level"}
        proto_methods = {
            name
            for name in dir(InteractionDbProtocol)
            if not name.startswith("_")
        }
        assert required.issubset(proto_methods), (
            f"InteractionDbProtocol missing methods: {required - proto_methods}"
        )


# --------------------------------------------------------------------------- #
# 版本号                                                                       #
# --------------------------------------------------------------------------- #


class TestVersion:
    def test_get_db_version_returns_yaml_version(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        version = interaction_db.get_db_version()
        assert version == "v2026.08"
        assert isinstance(version, str)
        assert version != "unknown"


# --------------------------------------------------------------------------- #
# 两药配伍命中                                                                 #
# --------------------------------------------------------------------------- #


class TestTwoDrugMajor:
    """两药配伍命中 major 档（R-INT-001 DRUG_WAR+DRUG_ASP）。"""

    def test_check_pair_returns_major_hit(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        hit = interaction_db.check_pair("DRUG_WAR", "DRUG_ASP")
        assert hit is not None
        assert isinstance(hit, InteractionHit)
        assert hit.drug_a == "DRUG_WAR"
        assert hit.drug_b == "DRUG_ASP"
        assert hit.severity == InteractionSeverity.MAJOR
        assert hit.rule_id == "R-INT-001"
        assert "出血" in hit.clinical_note
        assert hit.evidence.startswith("<INTERACTION_DB:")

    def test_severity_level_for_major_pair_returns_high(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        level = interaction_db.severity_level("DRUG_WAR", "DRUG_ASP")
        assert level == "high"


class TestTwoDrugMinor:
    """两药配伍命中 minor 档（R-INT-007 DRUG_AMOX+DRUG_IBUP）。"""

    def test_check_pair_returns_minor_hit(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        hit = interaction_db.check_pair("DRUG_AMOX", "DRUG_IBUP")
        assert hit is not None
        assert hit.severity == InteractionSeverity.MINOR
        assert hit.rule_id == "R-INT-007"

    def test_severity_level_for_minor_pair_returns_low(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        level = interaction_db.severity_level("DRUG_AMOX", "DRUG_IBUP")
        assert level == "low"


class TestContraindicatedPair:
    """两药配伍命中 contraindicated 档（R-INT-009 DRUG_IBUP+DRUG_WAR）。"""

    def test_check_pair_returns_contraindicated(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        hit = interaction_db.check_pair("DRUG_IBUP", "DRUG_WAR")
        assert hit is not None
        assert hit.severity == InteractionSeverity.CONTRAINDICATED
        assert hit.rule_id == "R-INT-009"

    def test_contraindicated_maps_to_high(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        level = interaction_db.severity_level("DRUG_IBUP", "DRUG_WAR")
        assert level == "high"


# --------------------------------------------------------------------------- #
# 双向匹配                                                                     #
# --------------------------------------------------------------------------- #


class TestBidirectional:
    def test_forward_and_reverse_match_same_hit(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        forward = interaction_db.check_pair("DRUG_WAR", "DRUG_ASP")
        reverse = interaction_db.check_pair("DRUG_ASP", "DRUG_WAR")
        assert forward is not None
        assert reverse is not None
        assert forward.rule_id == reverse.rule_id
        assert forward.severity == reverse.severity

    def test_same_drug_pair_returns_none(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """同一药品不构成配伍冲突。"""
        hit = interaction_db.check_pair("DRUG_AMOX", "DRUG_AMOX")
        assert hit is None


# --------------------------------------------------------------------------- #
# 未知组合                                                                     #
# --------------------------------------------------------------------------- #


class TestUnknownCombo:
    def test_pair_not_in_db_returns_none(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """未在配置中的两药 → None。"""
        hit = interaction_db.check_pair("DRUG_UNKNOWN_A", "DRUG_UNKNOWN_B")
        assert hit is None

    def test_partial_overlap_pair_returns_none(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """单边命中配置但反向无条目 → None。"""
        # 数据中仅配置 (DRUG_AMOX, DRUG_MET)；其他含 DRUG_AMOX 的非配置组合返回 None
        hit = interaction_db.check_pair("DRUG_AMOX", "DRUG_ASP")
        assert hit is None

    def test_check_returns_empty_for_unknown_set(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        hits = interaction_db.check(["DRUG_UNKNOWN_A", "DRUG_UNKNOWN_B"])
        assert hits == []

    def test_severity_level_for_unknown_returns_none(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        assert interaction_db.severity_level("UNKNOWN_X", "UNKNOWN_Y") is None


# --------------------------------------------------------------------------- #
# check()：处方批量查询                                                        #
# --------------------------------------------------------------------------- #


class TestCheckBatch:
    def test_check_finds_multiple_hits(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """含已知双药 + 三药 → 至少 2 个命中。"""
        hits = interaction_db.check(
            ["DRUG_ASP", "DRUG_IBUP", "DRUG_WAR"]
        )
        # R-INT-001 (WAR+ASP) + R-INT-002 (ASP+IBUP) + R-INT-101 (ASP+IBUP+WAR)
        rule_ids = {h.rule_id for h in hits}
        assert "R-INT-001" in rule_ids
        assert "R-INT-002" in rule_ids
        assert "R-INT-101" in rule_ids  # 三药累加命中

    def test_check_dedupes_drug_codes(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """重复 drug_code 不产生重复命中。"""
        hits = interaction_db.check(
            ["DRUG_WAR", "DRUG_ASP", "DRUG_ASP", "DRUG_WAR"]
        )
        rule_ids = [h.rule_id for h in hits]
        assert rule_ids.count("R-INT-001") == 1

    def test_check_with_single_drug_returns_empty(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        hits = interaction_db.check(["DRUG_AMOX"])
        assert hits == []


# --------------------------------------------------------------------------- #
# 三药累加命中                                                                 #
# --------------------------------------------------------------------------- #


class TestThreeDrugCombination:
    def test_three_drug_contraindicated_via_check(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """三药处方含 ASP+IBUP+WAR → 命中 contraindicated。"""
        hits = interaction_db.check(
            ["DRUG_ASP", "DRUG_IBUP", "DRUG_WAR"]
        )
        contra_hits = [
            h
            for h in hits
            if h.severity == InteractionSeverity.CONTRAINDICATED
        ]
        assert len(contra_hits) >= 1
        triple_hit = next(
            h for h in hits if h.rule_id == "R-INT-101"
        )
        assert triple_hit.severity == InteractionSeverity.CONTRAINDICATED
        assert "出血" in triple_hit.clinical_note

    def test_three_drug_major_via_check(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """三药处方含 AMOX+CEPH+WAR → 命中 major。"""
        hits = interaction_db.check(
            ["DRUG_AMOX", "DRUG_CEPH", "DRUG_WAR"]
        )
        triple_hits = [
            h for h in hits if h.rule_id == "R-INT-102"
        ]
        assert len(triple_hits) == 1
        assert triple_hits[0].severity == InteractionSeverity.MAJOR

    def test_three_drug_partial_returns_no_triple_hit(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """三药缺一 → triple 不命中（但 pair 可能命中）。"""
        hits = interaction_db.check(["DRUG_ASP", "DRUG_IBUP"])
        triple_rule_ids = {
            "R-INT-101",
            "R-INT-102",
            "R-INT-103",
            "R-INT-104",
        }
        for h in hits:
            assert h.rule_id not in triple_rule_ids


# --------------------------------------------------------------------------- #
# 严重度等级映射（severity_escalation）                                       #
# --------------------------------------------------------------------------- #


class TestSeverityEscalation:
    """验证 SEVERITY_TO_FINDING 四档映射。"""

    def test_severity_to_finding_complete_mapping(self) -> None:
        """四档严重度必须全部出现在映射表。"""
        for sev in InteractionSeverity:
            assert sev in SEVERITY_TO_FINDING

    def test_contraindicated_maps_to_high(self) -> None:
        assert (
            SEVERITY_TO_FINDING[InteractionSeverity.CONTRAINDICATED] == "high"
        )

    def test_major_maps_to_high(self) -> None:
        assert SEVERITY_TO_FINDING[InteractionSeverity.MAJOR] == "high"

    def test_moderate_maps_to_medium(self) -> None:
        assert SEVERITY_TO_FINDING[InteractionSeverity.MODERATE] == "medium"

    def test_minor_maps_to_low(self) -> None:
        assert SEVERITY_TO_FINDING[InteractionSeverity.MINOR] == "low"

    def test_severity_order(self) -> None:
        """枚举值顺序：contraindicated > major > moderate > minor。"""
        order = [
            InteractionSeverity.CONTRAINDICATED,
            InteractionSeverity.MAJOR,
            InteractionSeverity.MODERATE,
            InteractionSeverity.MINOR,
        ]
        values = [s.value for s in order]
        assert values == [
            "contraindicated",
            "major",
            "moderate",
            "minor",
        ]


# --------------------------------------------------------------------------- #
# 替身日志 [FAKE] 前缀                                                          #
# --------------------------------------------------------------------------- #


class TestFakeLogging:
    def test_construction_logs_fake_prefix(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="rxchk.tools.interaction_db")
        FakeInteractionDb(INTERACTION_DB_PATH)
        assert any("[FAKE]" in record.message for record in caplog.records)


# --------------------------------------------------------------------------- #
# 错误路径                                                                     #
# --------------------------------------------------------------------------- #


class TestErrorPaths:
    def test_missing_yaml_raises(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.yaml"
        with pytest.raises(InteractionDbError, match="not found"):
            FakeInteractionDb(missing)

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("invalid: : yaml: [", encoding="utf-8")
        with pytest.raises(InteractionDbError, match="invalid YAML"):
            FakeInteractionDb(bad)

    def test_root_not_mapping_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "not_mapping.yaml"
        bad.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(InteractionDbError, match="root must be mapping"):
            FakeInteractionDb(bad)


# --------------------------------------------------------------------------- #
# 数据规模核验（任务 29 要求 ≥ 12 对配伍）                                       #
# --------------------------------------------------------------------------- #


class TestDataScale:
    def test_pair_count_at_least_12(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """验证任务 29 硬指标：≥ 12 对配伍组合。"""
        # 通过 check_pair 探测所有 8 个 drug_code 的两两组合
        drug_codes = [
            "DRUG_AMOX",
            "DRUG_IBUP",
            "DRUG_PEN",
            "DRUG_CEPH",
            "DRUG_WAR",
            "DRUG_ASP",
            "DRUG_MET",
            "DRUG_PREG_CONTRA",
        ]
        pair_hits = 0
        for i, a in enumerate(drug_codes):
            for b in drug_codes[i + 1 :]:
                if interaction_db.check_pair(a, b) is not None:
                    pair_hits += 1
        assert pair_hits >= 12, (
            f"expected ≥ 12 pairs, got {pair_hits}"
        )

    def test_severity_distribution_covers_all_tiers(
        self, interaction_db: FakeInteractionDb
    ) -> None:
        """配伍库覆盖全部 4 档严重度。"""
        hits = interaction_db.check(
            [
                "DRUG_AMOX",
                "DRUG_IBUP",
                "DRUG_PEN",
                "DRUG_CEPH",
                "DRUG_WAR",
                "DRUG_ASP",
                "DRUG_MET",
            ]
        )
        severities = {h.severity for h in hits}
        # 三药累加覆盖 contraindicated + major；双药覆盖 major / moderate / minor
        assert InteractionSeverity.CONTRAINDICATED in severities
        assert InteractionSeverity.MAJOR in severities
        assert InteractionSeverity.MODERATE in severities
        assert InteractionSeverity.MINOR in severities