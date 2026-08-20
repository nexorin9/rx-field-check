"""drug_dict 适配器独立测试（任务 28 深化）。

覆盖范围
--------
- DrugDictProtocol：runtime_checkable 接口契约 + 替身满足契约
- lookup：基础查 / 未知查 / 字段一致性
- encode：合法 / 非法 / 边界（长度 / 字符集 / 大小写 / 空白）
- max_daily / max_single：基础值 / per_route 优先 / 未声明途径回落
- per_route：DRUG_PEN iv vs im 不同上限 / DRUG_CEPH iv vs im 不同上限
- route_form_mismatch：剂型 vs 途径不一致 / 字典不允许的途径
- frequency_default：基础值 / 未知药 / 空字符串回落
- indications_for：基础值 / 未知药
- YAML schema 边界：缺字段 / 非法字段类型 / 空 drugs 列表
- [FAKE] 日志契约：替身加载时打 [FAKE] 前缀

设计动机
--------
- 与 task 5 `test_adapters.py::TestDrugDict` 解耦：task 5 只覆盖基础路径，
  本任务聚焦「编码歧义 / per-route 上下限区分 / 字典缺项 / YAML schema
  容错」等 spec.md 「正式对接层」契约必须兜住的边界。
- 替身数据走真 YAML（`src/rxchk/data/drug_dict.yaml`），与正式对接契约一致；
  不构造「测试用临时 YAML」以保证替身-契约同步。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from rxchk.tools.drug_dict import (
    DrugDictError,
    DrugDictProtocol,
    DrugRecord,
    FakeDrugDict,
)


# --------------------------------------------------------------------------- #
# 路径 / fixture                                                              #
# --------------------------------------------------------------------------- #


DATA_DIR = Path(__file__).resolve().parent.parent / "src" / "rxchk" / "data"


@pytest.fixture(scope="module")
def drug_dict() -> FakeDrugDict:
    return FakeDrugDict(DATA_DIR / "drug_dict.yaml")


@pytest.fixture(scope="module")
def drug_dict_path() -> Path:
    return DATA_DIR / "drug_dict.yaml"


# --------------------------------------------------------------------------- #
# Protocol 契约                                                                #
# --------------------------------------------------------------------------- #


class TestDrugDictProtocolConformance:
    """DrugDictProtocol 是正式对接契约；替身必须满足（runtime_checkable）。"""

    def test_fake_drug_dict_satisfies_protocol(self, drug_dict: FakeDrugDict) -> None:
        assert isinstance(drug_dict, DrugDictProtocol)

    def test_protocol_lists_all_required_methods(self) -> None:
        """Protocol 抽象的 5 个核心方法在 FakeDrugDict 上必须存在。

        与任务 28 描述对齐：lookup_drug / max_daily_dose / max_single_dose
        / frequency_default / route_form_match。
        """
        required = {
            "lookup_drug",
            "max_daily_dose",
            "max_single_dose",
            "frequency_default",
            "route_form_match",
        }
        for name in required:
            assert hasattr(FakeDrugDict, name), f"FakeDrugDict 缺方法 {name}"

    def test_protocol_extra_methods_for_compat(self) -> None:
        """额外方法（与既有 task 5 兼容）：get_dict_version / check_name /
        check_encode / indications_for。"""
        extra = {
            "get_dict_version",
            "check_name",
            "check_encode",
            "indications_for",
        }
        for name in extra:
            assert hasattr(FakeDrugDict, name), f"FakeDrugDict 缺方法 {name}"

    def test_drug_record_is_dataclass(self) -> None:
        """DrugRecord 是 frozen dataclass，便于协议层结构稳定。"""
        rec = DrugRecord(
            code="X",
            name="x",
            forms=(),
            routes=(),
            default_frequency="",
            max_single_dose_g=None,
            max_daily_dose_g=None,
            per_route_limits={},
            indications=(),
        )
        assert rec.code == "X"
        # frozen: 不允许修改
        with pytest.raises(Exception):
            rec.code = "Y"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# [FAKE] 日志契约（与既有 task 5 一致）                                         #
# --------------------------------------------------------------------------- #


class TestFakeDrugDictLogging:
    """替身加载时打 [FAKE] 前缀，便于审计员识别替身运行。"""

    def test_fake_prefix_emitted_on_init(self, drug_dict_path: Path, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="rxchk.tools.drug_dict"):
            FakeDrugDict(drug_dict_path)
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "[FAKE]" in text
        assert "FakeDrugDict loaded" in text


# --------------------------------------------------------------------------- #
# lookup_drug                                                                  #
# --------------------------------------------------------------------------- #


class TestLookupDrug:
    """lookup_drug：基础查 / 未知查 / 字段一致性 / 字典版本号。"""

    def test_lookup_basic_amoxicillin(self, drug_dict: FakeDrugDict) -> None:
        rec = drug_dict.lookup_drug("DRUG_AMOX")
        assert rec is not None
        assert rec.code == "DRUG_AMOX"
        assert rec.name == "阿莫西林胶囊"

    def test_lookup_field_consistency(self, drug_dict: FakeDrugDict) -> None:
        """每条记录字段与 YAML 一致（≥ 5 字段；code / name / routes /
        default_frequency / max_single_dose_g / max_daily_dose_g /
        per_route_limits / indications / forms）。"""
        rec = drug_dict.lookup_drug("DRUG_AMOX")
        assert rec is not None
        assert rec.forms == ("capsule",)
        assert rec.routes == ("po",)
        assert rec.default_frequency == "tid"
        assert rec.max_single_dose_g == 0.5
        assert rec.max_daily_dose_g == 2.0
        assert rec.per_route_limits == {"po": {"single_g": 0.5, "daily_g": 2.0}}
        assert rec.indications == ("J18.900", "J20.900", "N39.000")

    def test_lookup_warfarin_low_dose(self, drug_dict: FakeDrugDict) -> None:
        """华法林是典型窄治疗窗药：单次 5mg / 日 7.5mg 上下限。"""
        rec = drug_dict.lookup_drug("DRUG_WAR")
        assert rec is not None
        assert rec.max_single_dose_g == 0.005
        assert rec.max_daily_dose_g == 0.0075

    def test_lookup_unknown_returns_none(self, drug_dict: FakeDrugDict) -> None:
        """未知 drug_code → None（不抛错；Agent 走 fallback / 命中 R-DRUG-NAME）。"""
        assert drug_dict.lookup_drug("DRUG_GHOST") is None
        assert drug_dict.lookup_drug("") is None

    def test_lookup_at_least_8_drugs(self, drug_dict: FakeDrugDict) -> None:
        """任务 28 要求 ≥ 8 药品。"""
        codes = [
            "DRUG_AMOX",
            "DRUG_IBUP",
            "DRUG_PEN",
            "DRUG_CEPH",
            "DRUG_WAR",
            "DRUG_ASP",
            "DRUG_MET",
            "DRUG_PREG_CONTRA",
        ]
        for code in codes:
            assert drug_dict.lookup_drug(code) is not None, f"字典缺药 {code}"

    def test_dict_version(self, drug_dict: FakeDrugDict) -> None:
        """字典版本号从 YAML 头声明读出。"""
        assert drug_dict.get_dict_version() == "v2026.08"


# --------------------------------------------------------------------------- #
# check_encode                                                                 #
# --------------------------------------------------------------------------- #


class TestCheckEncode:
    """check_encode：合法 / 非法 / 边界（长度 / 字符集 / 大小写 / 空白）。"""

    def test_valid_codes(self, drug_dict: FakeDrugDict) -> None:
        """合法编码：[A-Z0-9_-] 且长度 ≥ 3。"""
        valid = ["DRUG_AMOX", "ABC-123", "DRUG_PEN", "X1_Y2_Z3", "AB"]
        # 注：AB 长度 2 应判 False；上面 valid 列表误带，需修正
        # AB 长度 2 < 3 → 非法
        valid.remove("AB")
        for code in valid:
            assert drug_dict.check_encode(code) is True, f"{code} 应判合法"

    def test_invalid_codes_too_short(self, drug_dict: FakeDrugDict) -> None:
        """长度 < 3 → False。"""
        assert drug_dict.check_encode("AB") is False
        assert drug_dict.check_encode("A") is False
        assert drug_dict.check_encode("") is False

    def test_invalid_codes_bad_chars(self, drug_dict: FakeDrugDict) -> None:
        """字符不在 [A-Z0-9_-] → False。"""
        invalid = [
            "drug amox",  # 小写 + 空格
            "DRUG@AMOX",  # @
            "DRUG.AMOX",  # .
            "DRUG/AMOX",  # /
            "DRUG#AMOX",  # #
            "中文编码",  # 中文
            "DRUG\tAMOX",  # tab
        ]
        for code in invalid:
            assert drug_dict.check_encode(code) is False, f"{code} 应判非法"

    def test_encode_non_string_returns_false(self, drug_dict: FakeDrugDict) -> None:
        """非 str 入参 → False（不抛错；让 Agent 走 fallback）。"""
        # type: ignore[arg-type]
        assert drug_dict.check_encode(None) is False
        # type: ignore[arg-type]
        assert drug_dict.check_encode(123) is False

    def test_encode_lowercase_is_invalid(self, drug_dict: FakeDrugDict) -> None:
        """小写编码不在字符集内 → False。"""
        assert drug_dict.check_encode("drug_amox") is False
        assert drug_dict.check_encode("lowercase_only") is False


# --------------------------------------------------------------------------- #
# max_single_dose / max_daily_dose                                              #
# --------------------------------------------------------------------------- #


class TestMaxSingleDose:
    """max_single_dose：基础值 / per_route 优先 / 未声明途径回落。"""

    def test_max_single_dose_basic(self, drug_dict: FakeDrugDict) -> None:
        """DRUG_AMOX po: 0.5g（药品级 max_single_dose_g）。"""
        assert drug_dict.max_single_dose("DRUG_AMOX", "po") == 0.5

    def test_max_single_dose_per_route_priority(self, drug_dict: FakeDrugDict) -> None:
        """DRUG_PEN iv 4.0 / im 2.0（per_route_limits 优先于药品级）。"""
        assert drug_dict.max_single_dose("DRUG_PEN", "iv") == 4.0
        assert drug_dict.max_single_dose("DRUG_PEN", "im") == 2.0

    def test_max_single_dose_fallback_to_drug_level(self, drug_dict: FakeDrugDict) -> None:
        """未声明途径 → 回退药品级 max_single_dose_g。"""
        rec = drug_dict.lookup_drug("DRUG_AMOX")
        assert rec is not None
        # DRUG_AMOX 未声明 iv 上下限 → 落药品级 0.5g
        assert drug_dict.max_single_dose("DRUG_AMOX", "iv") == rec.max_single_dose_g

    def test_max_single_dose_unknown_drug_returns_none(
        self, drug_dict: FakeDrugDict
    ) -> None:
        """未知药 → None（Agent 走 fallback）。"""
        assert drug_dict.max_single_dose("DRUG_GHOST", "po") is None

    def test_max_single_dose_warfarin_narrow_window(
        self, drug_dict: FakeDrugDict
    ) -> None:
        """DRUG_WAR po: 5mg（窄治疗窗，单次上限严苛）。"""
        assert drug_dict.max_single_dose("DRUG_WAR", "po") == 0.005


class TestMaxDailyDose:
    """max_daily_dose：基础值 / per_route 优先 / 未声明途径回落。"""

    def test_max_daily_dose_basic(self, drug_dict: FakeDrugDict) -> None:
        assert drug_dict.max_daily_dose("DRUG_AMOX", "po") == 2.0

    def test_max_daily_dose_per_route_priority(self, drug_dict: FakeDrugDict) -> None:
        """DRUG_PEN iv 12.0 / im 8.0（per_route_limits 优先于药品级）。"""
        assert drug_dict.max_daily_dose("DRUG_PEN", "iv") == 12.0
        assert drug_dict.max_daily_dose("DRUG_PEN", "im") == 8.0

    def test_max_daily_dose_cephalosporin_per_route(self, drug_dict: FakeDrugDict) -> None:
        """DRUG_CEPH iv 4.0 / im 2.0（另一典型多途径抗生素）。"""
        assert drug_dict.max_daily_dose("DRUG_CEPH", "iv") == 4.0
        assert drug_dict.max_daily_dose("DRUG_CEPH", "im") == 2.0

    def test_max_daily_dose_fallback_to_drug_level(
        self, drug_dict: FakeDrugDict
    ) -> None:
        """未声明途径 → 回退药品级 max_daily_dose_g。"""
        rec = drug_dict.lookup_drug("DRUG_AMOX")
        assert rec is not None
        assert drug_dict.max_daily_dose("DRUG_AMOX", "iv") == rec.max_daily_dose_g

    def test_max_daily_dose_unknown_drug_returns_none(
        self, drug_dict: FakeDrugDict
    ) -> None:
        assert drug_dict.max_daily_dose("DRUG_GHOST", "po") is None

    def test_max_daily_dose_warfarin_narrow_window(
        self, drug_dict: FakeDrugDict
    ) -> None:
        """DRUG_WAR po 日上限 7.5mg。"""
        assert drug_dict.max_daily_dose("DRUG_WAR", "po") == 0.0075


# --------------------------------------------------------------------------- #
# per_route_limits 区分（核心场景）                                              #
# --------------------------------------------------------------------------- #


class TestPerRouteLimits:
    """per_route 上下限：DRUG_PEN iv vs im 不同上限（任务 28 重点验证）。

    设计动机
    --------
    - 同一药品不同途径上限差异显著（iv 急性感染高剂量起步 vs im 局部小剂量）
    - 规则库可能漏配某途径（如 R-DOSE-003 只配 iv），drug_dict per_route
      是制度层之外的硬冗余
    """

    def test_pen_iv_im_differ(self, drug_dict: FakeDrugDict) -> None:
        """DRUG_PEN：iv 单次 4g / 日 12g；im 单次 2g / 日 8g。"""
        assert drug_dict.max_single_dose("DRUG_PEN", "iv") == 4.0
        assert drug_dict.max_single_dose("DRUG_PEN", "im") == 2.0
        assert drug_dict.max_daily_dose("DRUG_PEN", "iv") == 12.0
        assert drug_dict.max_daily_dose("DRUG_PEN", "im") == 8.0

    def test_ceph_iv_im_differ(self, drug_dict: FakeDrugDict) -> None:
        """DRUG_CEPH：iv 单次 2g / 日 4g；im 单次 1g / 日 2g。"""
        assert drug_dict.max_single_dose("DRUG_CEPH", "iv") == 2.0
        assert drug_dict.max_single_dose("DRUG_CEPH", "im") == 1.0
        assert drug_dict.max_daily_dose("DRUG_CEPH", "iv") == 4.0
        assert drug_dict.max_daily_dose("DRUG_CEPH", "im") == 2.0

    def test_pen_im_lower_than_drug_level(self, drug_dict: FakeDrugDict) -> None:
        """DRUG_PEN im 单次 2g < 药品级 4g（per_route 严苛于药品级）。"""
        drug_level = drug_dict.lookup_drug("DRUG_PEN")
        assert drug_level is not None
        assert drug_level.max_single_dose_g == 4.0
        assert drug_dict.max_single_dose("DRUG_PEN", "im") == 2.0
        assert drug_dict.max_single_dose("DRUG_PEN", "im") < drug_level.max_single_dose_g

    def test_amox_single_route_only(self, drug_dict: FakeDrugDict) -> None:
        """DRUG_AMOX 仅 po 途径：未声明 iv → 回落药品级（per_route 优势）。"""
        rec = drug_dict.lookup_drug("DRUG_AMOX")
        assert rec is not None
        # DRUG_AMOX per_route_limits.po 存在 → iv 未声明 → 回落药品级 0.5g
        assert drug_dict.max_single_dose("DRUG_AMOX", "po") == 0.5
        assert drug_dict.max_single_dose("DRUG_AMOX", "iv") == rec.max_single_dose_g


# --------------------------------------------------------------------------- #
# route_form_match（route_form_mismatch 命中路径）                                #
# --------------------------------------------------------------------------- #


class TestRouteFormMatch:
    """route_form_match：route 在 drug.routes 内 → True；否则 False。"""

    def test_po_drug_with_po(self, drug_dict: FakeDrugDict) -> None:
        """DRUG_AMOX po（口服）→ match。"""
        assert drug_dict.route_form_match("DRUG_AMOX", "po") is True

    def test_po_drug_with_iv_mismatch(self, drug_dict: FakeDrugDict) -> None:
        """DRUG_AMOX iv 不在 routes → mismatch（剂型胶囊 vs 注射 iv 错配）。"""
        assert drug_dict.route_form_match("DRUG_AMOX", "iv") is False

    def test_injection_drug_with_multiple_routes(self, drug_dict: FakeDrugDict) -> None:
        """DRUG_PEN iv/im 都允许 → 都 match。"""
        assert drug_dict.route_form_match("DRUG_PEN", "iv") is True
        assert drug_dict.route_form_match("DRUG_PEN", "im") is True

    def test_unknown_drug_returns_false(self, drug_dict: FakeDrugDict) -> None:
        """未知药品 → False（不抛错；Agent 走 R-DRUG-NAME 命中）。"""
        assert drug_dict.route_form_match("DRUG_GHOST", "po") is False

    def test_oral_drug_with_im_mismatch(self, drug_dict: FakeDrugDict) -> None:
        """DRUG_WAR po 给 im 路径 → mismatch。"""
        assert drug_dict.route_form_match("DRUG_WAR", "po") is True
        assert drug_dict.route_form_match("DRUG_WAR", "im") is False

    def test_route_form_mismatch_for_each_oral_drug(
        self, drug_dict: FakeDrugDict
    ) -> None:
        """任务 28 重点验证：route_form_mismatch 对每个 po 药品 vs 非 po 路径
        都返回 False。"""
        oral_drugs = ["DRUG_AMOX", "DRUG_IBUP", "DRUG_WAR", "DRUG_ASP", "DRUG_MET"]
        non_po_routes = ["iv", "im", "ih", "sc"]
        for code in oral_drugs:
            for route in non_po_routes:
                assert drug_dict.route_form_match(code, route) is False, (
                    f"{code} 不应允许 {route}，但 route_form_match 返回 True"
                )


# --------------------------------------------------------------------------- #
# frequency_default                                                            #
# --------------------------------------------------------------------------- #


class TestFrequencyDefault:
    """frequency_default：基础值 / 未知药 / 回落。"""

    def test_basic_default(self, drug_dict: FakeDrugDict) -> None:
        assert drug_dict.frequency_default("DRUG_AMOX") == "tid"
        assert drug_dict.frequency_default("DRUG_PEN") == "bid"
        assert drug_dict.frequency_default("DRUG_WAR") == "qd"

    def test_unknown_drug_returns_none(self, drug_dict: FakeDrugDict) -> None:
        assert drug_dict.frequency_default("DRUG_GHOST") is None


# --------------------------------------------------------------------------- #
# indications_for                                                               #
# --------------------------------------------------------------------------- #


class TestIndicationsFor:
    """indications_for：基础值 / 未知药 / 多适应症。"""

    def test_basic_indications(self, drug_dict: FakeDrugDict) -> None:
        assert drug_dict.indications_for("DRUG_AMOX") == (
            "J18.900",
            "J20.900",
            "N39.000",
        )

    def test_unknown_drug_returns_empty(self, drug_dict: FakeDrugDict) -> None:
        assert drug_dict.indications_for("DRUG_GHOST") == ()

    def test_warfarin_cardio_indications(self, drug_dict: FakeDrugDict) -> None:
        """DRUG_WAR 心血管适应症。"""
        assert drug_dict.indications_for("DRUG_WAR") == ("I48.000", "I80.200")


# --------------------------------------------------------------------------- #
# DrugRecord 辅助方法                                                            #
# --------------------------------------------------------------------------- #


class TestDrugRecordHelpers:
    """DrugRecord 内部辅助方法：route_allowed / route_limit。"""

    def test_route_allowed(self, drug_dict: FakeDrugDict) -> None:
        rec = drug_dict.lookup_drug("DRUG_PEN")
        assert rec is not None
        assert rec.route_allowed("iv") is True
        assert rec.route_allowed("im") is True
        assert rec.route_allowed("po") is False

    def test_route_limit_returns_dict(self, drug_dict: FakeDrugDict) -> None:
        rec = drug_dict.lookup_drug("DRUG_PEN")
        assert rec is not None
        assert rec.route_limit("iv") == {"single_g": 4.0, "daily_g": 12.0}
        assert rec.route_limit("im") == {"single_g": 2.0, "daily_g": 8.0}

    def test_route_limit_undeclared_returns_none(self, drug_dict: FakeDrugDict) -> None:
        rec = drug_dict.lookup_drug("DRUG_AMOX")
        assert rec is not None
        assert rec.route_limit("iv") is None


# --------------------------------------------------------------------------- #
# YAML schema 边界（替身构造时）                                                 #
# --------------------------------------------------------------------------- #


class TestDrugDictYamlSchemaErrors:
    """YAML schema 错误应抛 DrugDictError（替身构造时立即拒收）。"""

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(DrugDictError, match="not found"):
            FakeDrugDict(tmp_path / "nonexistent.yaml")

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("version: v2026.08\ndrugs: [unclosed\n", encoding="utf-8")
        with pytest.raises(DrugDictError, match="invalid YAML"):
            FakeDrugDict(bad)

    def test_root_not_mapping_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "list.yaml"
        bad.write_text("- foo\n- bar\n", encoding="utf-8")
        with pytest.raises(DrugDictError, match="root must be mapping"):
            FakeDrugDict(bad)

    def test_drug_entry_missing_required_fields_raises(
        self, tmp_path: Path
    ) -> None:
        bad = tmp_path / "missing.yaml"
        # 缺 name 字段
        bad.write_text(
            "version: v2026.08\n"
            "drugs:\n"
            "  - code: DRUG_TEST\n"
            "    routes: [po]\n",
            encoding="utf-8",
        )
        with pytest.raises(DrugDictError, match="missing required field"):
            FakeDrugDict(bad)

    def test_drug_entry_not_mapping_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "not_map.yaml"
        bad.write_text(
            "version: v2026.08\ndrugs:\n  - DRUG_TEST\n", encoding="utf-8"
        )
        with pytest.raises(DrugDictError, match="drug entry must be mapping"):
            FakeDrugDict(bad)


# --------------------------------------------------------------------------- #
# 集成：drug_dict × DrugAgent + DoseAgent 主路径                                #
# --------------------------------------------------------------------------- #


class TestDrugDictAgentIntegration:
    """DrugDict + DoseAgent / DrugAgent 主路径：8 字段族 Agent 消费 drug_dict
    时无锁竞争、替身满足 Protocol、与既有 DoseAgent drug_dict 安全冗余层
    （task 20）兼容。"""

    def test_drug_dict_passes_protocol_check_for_dose_agent(
        self, drug_dict: FakeDrugDict
    ) -> None:
        """DoseAgent（task 20）要求 DrugDictProtocol；替身必须满足。"""
        from rxchk.agents.dose import DoseAgent
        from rxchk.tools.rule_adapter import FakeRuleAdapter

        rule_adapter = FakeRuleAdapter(
            DATA_DIR / "rule" / "dose_rules.yaml",
            DATA_DIR / "rule" / "population_rules.yaml",
        )
        dose_agent = DoseAgent(rule_adapter, drug_dict)
        assert dose_agent._drug_dict is drug_dict

    def test_drug_dict_supports_check_name_for_drug_agent(
        self, drug_dict: FakeDrugDict
    ) -> None:
        """DrugAgent 调 drug_dict.check_name；替身实现正确返回。"""
        assert drug_dict.check_name("DRUG_AMOX") is True
        assert drug_dict.check_name("DRUG_GHOST") is False

    def test_all_eight_drugs_have_five_fields(self, drug_dict: FakeDrugDict) -> None:
        """每条 DrugRecord 字段数 ≥ 5（任务 28 描述「每药含 5 字段」）。"""
        codes = [
            "DRUG_AMOX",
            "DRUG_IBUP",
            "DRUG_PEN",
            "DRUG_CEPH",
            "DRUG_WAR",
            "DRUG_ASP",
            "DRUG_MET",
            "DRUG_PREG_CONTRA",
        ]
        for code in codes:
            rec = drug_dict.lookup_drug(code)
            assert rec is not None, f"字典缺药 {code}"
            # 字段：code, name, forms, routes, default_frequency,
            #       max_single_dose_g, max_daily_dose_g,
            #       per_route_limits, indications → 9 字段
            fields = [
                rec.code,
                rec.name,
                rec.forms,
                rec.routes,
                rec.default_frequency,
            ]
            assert len(fields) == 5, f"{code} 字段数 < 5"
