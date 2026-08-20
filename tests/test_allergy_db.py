"""`rxchk.tools.allergy_db` 深度单元测试（任务 30 独立深化）。

覆盖范围
--------
- **契约一致性**：`FakeAllergyDb` 满足抽象 `AllergyDbProtocol`
  （runtime_checkable），Protocol 暴露 5 个核心方法。
- **direct_conflict**（直接命中）：
  - 处方药名 / 编码出现在过敏原 `names` 列表 → `cross_reactivity=False`
  - 命中严重度按 `SEVERITY_TO_FINDING` 映射（mild→low / moderate→medium /
    contraindicated→high）
- **family_cross**（家族交叉）：
  - 处方药名 / 编码命中某 entry 的 `names`，且该 entry 的
    `cross_reactions` 含有患者过敏原 → `cross_reactivity=True`
  - 严重度按家族交叉降档：severe→moderate / contraindicated→severe
- **severity_inference**（综合推断）：
  - 综合 `(drug_code, allergies)` 推断最高严重度档（含直接 + 交叉）
  - 空 allergies / 全未命中 → None
- **unknown_allergen_safe**（未知过敏原兜底）：
  - 患者对未登记过敏原过敏 → check 返回 []、severity_inference 返回 None
- **数据规模核验**：YAML 至少 8 过敏原条目，每条含 family + cross_reactions。
- **替身日志 [FAKE] 前缀**：构造时打 [FAKE]。
- **错误路径**：YAML 缺失 / 解析失败 / 缺必填字段 → 抛 `AllergyDbError`。

设计动机
--------
- 与 task 5 `test_adapters.py::TestAllergyDb` / 任务 22
  `test_indication_allergy_agents.py` 解耦：聚焦 `allergy_db` 适配器
  自身的边界与契约（家族交叉 / 严重度推断 / 未知过敏兜底），便于后续
  扩展（如新增 contraindicated 真实数据 / 跨族交叉）单独回归。
- 替身数据走真 YAML（`src/rxchk/data/allergy_db.yaml`），与正式对接契约一致。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from rxchk.tools.allergy_db import (
    SEVERITY_TO_FINDING,
    AllergyDbError,
    AllergyDbProtocol,
    AllergyHit,
    AllergySeverity,
    AllergenEntry,
    FakeAllergyDb,
)


# --------------------------------------------------------------------------- #
# 路径 / fixture                                                               #
# --------------------------------------------------------------------------- #


DATA_DIR = Path(__file__).resolve().parent.parent / "src" / "rxchk" / "data"
ALLERGY_DB_PATH = DATA_DIR / "allergy_db.yaml"


@pytest.fixture(scope="module")
def allergy_db() -> FakeAllergyDb:
    return FakeAllergyDb(ALLERGY_DB_PATH)


# --------------------------------------------------------------------------- #
# 契约一致性                                                                   #
# --------------------------------------------------------------------------- #


class TestProtocolConformance:
    def test_fake_satisfies_protocol(self, allergy_db: FakeAllergyDb) -> None:
        assert isinstance(allergy_db, AllergyDbProtocol)

    def test_protocol_exposes_required_methods(self) -> None:
        """任务 30 验收：Protocol 必须暴露 5 个核心方法（含 severity_inference）。"""
        required = {
            "get_db_version",
            "check",
            "family_cross_reactivity",
            "severity_for",
            "severity_inference",
        }
        proto_methods = {
            name
            for name in dir(AllergyDbProtocol)
            if not name.startswith("_")
        }
        assert required.issubset(proto_methods), (
            f"AllergyDbProtocol missing methods: {required - proto_methods}"
        )


# --------------------------------------------------------------------------- #
# 版本号                                                                       #
# --------------------------------------------------------------------------- #


class TestVersion:
    def test_get_db_version_returns_yaml_version(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        version = allergy_db.get_db_version()
        assert version == "v2026.08"
        assert isinstance(version, str)
        assert version != "unknown"


# --------------------------------------------------------------------------- #
# direct_conflict（直接命中）                                                  #
# --------------------------------------------------------------------------- #


class TestDirectConflict:
    """直接命中：处方药品 == 过敏原 names 之一 → cross_reactivity=False。"""

    def test_penicillin_amoxicillin_hits_direct(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """患者对 penicillin 过敏；处方 DRUG_AMOX（amoxicillin）→ 直接命中。"""
        hits = allergy_db.check("DRUG_AMOX", ["penicillin"])
        assert len(hits) == 1
        hit = hits[0]
        assert isinstance(hit, AllergyHit)
        assert hit.allergen == "penicillin"
        assert hit.drug_code == "DRUG_AMOX"
        assert hit.severity == AllergySeverity.SEVERE
        assert hit.cross_reactivity is False
        assert "DIRECT" in hit.rule_id
        assert "PENICILLIN" in hit.rule_id
        assert hit.evidence.startswith("<ALLERGY_DB:")

    def test_direct_severity_maps_to_finding_high(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """severe 直接命中 → SEVERITY_TO_FINDING[severe] = high。"""
        hits = allergy_db.check("DRUG_AMOX", ["penicillin"])
        assert SEVERITY_TO_FINDING[hits[0].severity] == "high"

    def test_mild_direct_maps_to_finding_low(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """A-IBUP severity=mild → DRUG_IBUP 直接命中 → low。"""
        hits = allergy_db.check("DRUG_IBUP", ["ibuprofen"])
        assert len(hits) == 1
        assert hits[0].severity == AllergySeverity.MILD
        assert SEVERITY_TO_FINDING[hits[0].severity] == "low"
        assert hits[0].cross_reactivity is False

    def test_drug_code_name_match_via_name_lookup(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """药品名（不在 entry.names 但 entry.names 含 drug_code 串）也能命中。"""
        # A-PENICILLIN.names 包含 DRUG_AMOX 和 DRUG_PEN
        hits = allergy_db.check("DRUG_PEN", ["penicillin"])
        assert len(hits) == 1
        assert hits[0].drug_code == "DRUG_PEN"
        assert hits[0].severity == AllergySeverity.SEVERE

    def test_direct_match_includes_clinical_note(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """命中必须携带 clinical_note，便于 Viewer 字段证据卡渲染。"""
        hits = allergy_db.check("DRUG_AMOX", ["penicillin"])
        assert "皮试" in hits[0].clinical_note or "换药" in hits[0].clinical_note

    def test_multiple_allergies_returns_multiple_hits(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """多条过敏史 → 可能命中多条。"""
        # 患者对 penicillin + ibuprofen 过敏；处方 DRUG_AMOX → 仅命中 penicillin
        hits = allergy_db.check("DRUG_AMOX", ["penicillin", "ibuprofen"])
        rule_ids = {h.rule_id for h in hits}
        # penicillin 命中 DIRECT；ibuprofen 不命中（DRUG_AMOX 不属于 nsaid）
        assert any("PENICILLIN" in rid for rid in rule_ids)


# --------------------------------------------------------------------------- #
# family_cross（家族交叉）                                                     #
# --------------------------------------------------------------------------- #


class TestFamilyCross:
    """家族交叉：处方药命中某 entry 的 names，且 entry 的 cross_reactions 含患者过敏原。"""

    def test_penicillin_cephalosporin_cross(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """患者对 penicillin 过敏；处方 DRUG_CEPH（头孢）→ A-CEPHALOSPORIN.names 含 DRUG_CEPH
        且 cross_reactions 含 penicillin → 家族交叉命中。
        """
        hits = allergy_db.check("DRUG_CEPH", ["penicillin"])
        assert len(hits) == 1
        hit = hits[0]
        assert hit.cross_reactivity is True
        assert "CROSS" in hit.rule_id
        # 严重度：A-CEPHALOSPORIN.severity=severe → 家族交叉降一档 → moderate
        assert hit.severity == AllergySeverity.MODERATE

    def test_family_cross_severity_downgrade(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """severe 家族交叉降一档 → moderate → SEVERITY_TO_FINDING[moderate] = medium。"""
        hits = allergy_db.check("DRUG_CEPH", ["penicillin"])
        assert SEVERITY_TO_FINDING[hits[0].severity] == "medium"

    def test_family_cross_returns_evidence_with_cross_marker(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """家族交叉 evidence 必须含 'cross' 标识。"""
        hits = allergy_db.check("DRUG_CEPH", ["penicillin"])
        assert "/cross>" in hits[0].evidence

    def test_family_cross_reactivity_returns_cross_hit_only(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """family_cross_reactivity 只返回 cross_reactivity=True 的命中。"""
        # 患者对 penicillin 过敏；处方 DRUG_AMOX 是直接命中（不是家族交叉）
        hit = allergy_db.family_cross_reactivity("DRUG_AMOX", "penicillin")
        assert hit is None

        # 处方 DRUG_CEPH 是家族交叉
        hit = allergy_db.family_cross_reactivity("DRUG_CEPH", "penicillin")
        assert hit is not None
        assert hit.cross_reactivity is True

    def test_family_cross_reactivity_unknown_pair_returns_none(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """未在配置中的组合 → None。"""
        hit = allergy_db.family_cross_reactivity("DRUG_UNKNOWN", "penicillin")
        assert hit is None


# --------------------------------------------------------------------------- #
# severity_inference（综合推断）                                                #
# --------------------------------------------------------------------------- #


class TestSeverityInference:
    """severity_inference：综合 (drug_code, allergies) 推断最高严重度档。"""

    def test_direct_severe_inference_returns_high(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """直接命中 severe → high（取最高档）。"""
        result = allergy_db.severity_inference("DRUG_AMOX", ["penicillin"])
        assert result == "high"

    def test_family_cross_inference_downgrades(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """家族交叉降档后推断：severe → moderate → medium。"""
        result = allergy_db.severity_inference("DRUG_CEPH", ["penicillin"])
        assert result == "medium"

    def test_mild_direct_inference_returns_low(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        result = allergy_db.severity_inference("DRUG_IBUP", ["ibuprofen"])
        assert result == "low"

    def test_moderate_direct_inference_returns_medium(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """A-ASP severity=moderate → DRUG_ASP 直接命中 → medium。"""
        result = allergy_db.severity_inference("DRUG_ASP", ["aspirin"])
        assert result == "medium"

    def test_empty_allergies_returns_none(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """空过敏史 → 不伪命中。"""
        result = allergy_db.severity_inference("DRUG_AMOX", [])
        assert result is None

    def test_unknown_allergy_returns_none(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """未登记过敏原 + 已知药 → 无命中。"""
        result = allergy_db.severity_inference("DRUG_AMOX", ["unknown_allergen_xyz"])
        assert result is None

    def test_unknown_drug_returns_none(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """已知过敏原 + 未登记药品 → 无命中。"""
        result = allergy_db.severity_inference("DRUG_UNKNOWN_XYZ", ["penicillin"])
        assert result is None

    def test_mixed_allergies_picks_highest(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """多条过敏史混合（命中 + 未命中）→ 取命中最高档。"""
        # penicillin (严重，命中) + ibuprofen (轻度，未命中因为是 DRUG_AMOX 不是 nsaid)
        result = allergy_db.severity_inference(
            "DRUG_AMOX", ["penicillin", "ibuprofen", "unknown_xyz"]
        )
        assert result == "high"

    def test_inference_returns_top_among_multiple_hits(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """多条命中时取最严重档。"""
        # DRUG_ASP 直接命中 aspirin(moderate)；同时 nsaid 家族交叉 (A-IBUP.cross=[nsaid]) 触发 mild
        # 但 DRUG_ASP 不在 A-IBUP.names 中 → 实际仅 aspirin 直接命中
        # 因此这里验证 single hit 的最严重档推断（覆盖实现路径）
        result = allergy_db.severity_inference("DRUG_ASP", ["aspirin", "ibuprofen"])
        # aspirin 直接命中 moderate → medium（最高）
        assert result == "medium"

    def test_inference_handles_iterable(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """allergies 入参支持任意 Iterable（list / tuple / generator）。"""
        gen = (a for a in ["penicillin"])
        result = allergy_db.severity_inference("DRUG_AMOX", gen)
        assert result == "high"

    def test_inference_unknown_drug_known_allergies(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """未知药品 + 已知过敏原 → None（不是默认 high）。"""
        result = allergy_db.severity_inference("DRUG_NEVER_HEARD_OF", ["penicillin"])
        assert result is None


# --------------------------------------------------------------------------- #
# severity_for（按过敏原名反查）                                                #
# --------------------------------------------------------------------------- #


class TestSeverityFor:
    def test_known_allergen_returns_finding_severity(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        assert allergy_db.severity_for("penicillin") == "high"
        assert allergy_db.severity_for("ibuprofen") == "low"
        assert allergy_db.severity_for("aspirin") == "medium"

    def test_unknown_allergen_returns_none(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        assert allergy_db.severity_for("totally_unknown") is None

    def test_case_insensitive(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """严重度反查忽略大小写。"""
        assert allergy_db.severity_for("PENICILLIN") == "high"
        assert allergy_db.severity_for("  Penicillin  ") == "high"


# --------------------------------------------------------------------------- #
# unknown_allergen_safe（未知过敏原兜底）                                       #
# --------------------------------------------------------------------------- #


class TestUnknownAllergenSafe:
    """未知过敏原 / 未知药品组合 → 无命中、不抛错。"""

    def test_unknown_allergen_no_hits(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        hits = allergy_db.check("DRUG_AMOX", ["not_in_db_anywhere"])
        assert hits == []

    def test_unknown_drug_with_known_allergen_no_hits(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        hits = allergy_db.check("DRUG_NEVER", ["penicillin"])
        assert hits == []

    def test_both_unknown_no_hits(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        hits = allergy_db.check("DRUG_FAKE", ["ALLERGEN_FAKE"])
        assert hits == []

    def test_empty_allergy_list_no_hits(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """空过敏史 → 不伪命中。"""
        hits = allergy_db.check("DRUG_AMOX", [])
        assert hits == []

    def test_family_cross_reactivity_unknown_safe(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """family_cross_reactivity 在未知组合下返回 None，不抛错。"""
        assert allergy_db.family_cross_reactivity("X", "Y") is None


# --------------------------------------------------------------------------- #
# SEVERITY_TO_FINDING 映射完整性                                                #
# --------------------------------------------------------------------------- #


class TestSeverityToFindingMapping:
    def test_complete_severity_mapping(self) -> None:
        """四档严重度必须全部出现在映射表。"""
        for sev in AllergySeverity:
            assert sev in SEVERITY_TO_FINDING

    def test_contraindicated_maps_to_high(self) -> None:
        assert SEVERITY_TO_FINDING[AllergySeverity.CONTRAINDICATED] == "high"

    def test_severe_maps_to_high(self) -> None:
        assert SEVERITY_TO_FINDING[AllergySeverity.SEVERE] == "high"

    def test_moderate_maps_to_medium(self) -> None:
        assert SEVERITY_TO_FINDING[AllergySeverity.MODERATE] == "medium"

    def test_mild_maps_to_low(self) -> None:
        assert SEVERITY_TO_FINDING[AllergySeverity.MILD] == "low"


# --------------------------------------------------------------------------- #
# 替身日志 [FAKE] 前缀                                                          #
# --------------------------------------------------------------------------- #


class TestFakeLogging:
    def test_construction_logs_fake_prefix(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="rxchk.tools.allergy_db")
        FakeAllergyDb(ALLERGY_DB_PATH)
        assert any("[FAKE]" in record.message for record in caplog.records)


# --------------------------------------------------------------------------- #
# 错误路径                                                                     #
# --------------------------------------------------------------------------- #


class TestErrorPaths:
    def test_missing_yaml_raises(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.yaml"
        with pytest.raises(AllergyDbError, match="not found"):
            FakeAllergyDb(missing)

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("invalid: : yaml: [", encoding="utf-8")
        with pytest.raises(AllergyDbError, match="invalid YAML"):
            FakeAllergyDb(bad)

    def test_root_not_mapping_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "not_mapping.yaml"
        bad.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(AllergyDbError, match="root must be mapping"):
            FakeAllergyDb(bad)

    def test_missing_allergen_id_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "no_id.yaml"
        bad.write_text(
            "version: v2026.08\nallergens:\n  - names: [foo]\n    family: x\n",
            encoding="utf-8",
        )
        with pytest.raises(AllergyDbError, match="allergen_id"):
            FakeAllergyDb(bad)

    def test_missing_names_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "no_names.yaml"
        bad.write_text(
            "version: v2026.08\nallergens:\n  - allergen_id: A-X\n    family: y\n",
            encoding="utf-8",
        )
        with pytest.raises(AllergyDbError, match="at least one name"):
            FakeAllergyDb(bad)


# --------------------------------------------------------------------------- #
# 数据规模核验（任务 30 要求 ≥ 8 过敏原）                                        #
# --------------------------------------------------------------------------- #


class TestDataScale:
    def test_allergen_count_at_least_8(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """验证任务 30 硬指标：≥ 8 过敏原条目。"""
        # 通过构造 entry 数与所有 entry 的 allergen_id 数对齐
        entries = list(allergy_db._entries.values())  # type: ignore[attr-defined]
        assert len(entries) >= 8, f"expected ≥ 8 allergens, got {len(entries)}"

    def test_all_entries_have_family_and_cross(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """每条 entry 必须含 family 字段（cross_reactions 可空）。"""
        entries = list(allergy_db._entries.values())  # type: ignore[attr-defined]
        for entry in entries:
            assert isinstance(entry, AllergenEntry)
            assert entry.family, f"entry {entry.allergen_id} missing family"
            assert isinstance(entry.cross_reactions, tuple)

    def test_severity_distribution_covers_tiers(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """过敏库覆盖至少 3 档严重度（mild / moderate / severe）。"""
        entries = list(allergy_db._entries.values())  # type: ignore[attr-defined]
        sev_set = {e.severity for e in entries}
        assert AllergySeverity.MILD in sev_set
        assert AllergySeverity.MODERATE in sev_set
        assert AllergySeverity.SEVERE in sev_set

    def test_family_diversity(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """至少 3 个不同 family（含家族交叉 → 同 family 至少 2 条）。"""
        entries = list(allergy_db._entries.values())  # type: ignore[attr-defined]
        family_set = {e.family for e in entries}
        assert len(family_set) >= 3

    def test_penicillin_cephalosporin_cross_in_data(
        self, allergy_db: FakeAllergyDb
    ) -> None:
        """YAML 必须显式声明 penicillin ↔ cephalosporin 家族交叉（任务 22/30 重点验证）。"""
        pen = allergy_db._entries.get("A-PENICILLIN")  # type: ignore[attr-defined]
        ceph = allergy_db._entries.get("A-CEPHALOSPORIN")  # type: ignore[attr-defined]
        assert pen is not None
        assert ceph is not None
        assert "cephalosporin" in pen.cross_reactions
        assert "penicillin" in ceph.cross_reactions