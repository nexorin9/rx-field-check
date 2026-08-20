"""`rxchk.tools.rule_adapter` 独立深度测试（任务 31 验收范围）。

任务 31 核心验收：
1. **契约一致性**：`FakeRuleAdapter` 满足 `RuleProtocol`（5 方法版本）；
   `RuleAdapterProtocol` 作为旧名 alias 仍指向同一 Protocol。
2. **版本号透传**：`get_rule_version` 返回 YAML 头声明的版本号；
   `lookup_rule` / `list_rules` 输出 dict 含 `rule_id` + `rule_family`。
3. **`[FAKE]` 日志前缀**：init + `get_rule_version` + `lookup_rule` +
   `list_rules` 全部带 `[FAKE]` 前缀（caplog 验证）。
4. **`runtime_version_stamp` 增强**：除 `rule_version` 外支持 `extra`
   元数据；`stamp_finding_metadata` 业务侧快捷方式覆盖 caller。
5. **Protocol 表面**：仅 5 方法；`check_freq` / `check_route` 作为替身
   实现细节**不**在 Protocol 内。

无 DOM / 无网络依赖；使用 pytest + caplog + tmp_path。
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path

import pytest

from rxchk.tools.rule_adapter import (
    DoseCheckResult,
    FakeRuleAdapter,
    FreqCheckResult,
    PopulationCheckResult,
    RouteCheckResult,
    RuleAdapterError,
    RuleAdapterProtocol,
    RuleHit,
    RuleProtocol,
    RuleVersionMismatch,
    runtime_version_stamp,
    stamp_finding_metadata,
)


# --------------------------------------------------------------------------- #
# 路径                                                                        #
# --------------------------------------------------------------------------- #


DATA_DIR = Path(__file__).resolve().parent.parent / "src" / "rxchk" / "data"


@pytest.fixture(scope="module")
def rule_adapter() -> FakeRuleAdapter:
    return FakeRuleAdapter(
        DATA_DIR / "rule" / "dose_rules.yaml",
        DATA_DIR / "rule" / "population_rules.yaml",
        freq_rules_path=DATA_DIR / "rule" / "freq_rules.yaml",
        route_rules_path=DATA_DIR / "rule" / "route_rules.yaml",
    )


# --------------------------------------------------------------------------- #
# Protocol 表面（5 方法契约）                                                    #
# --------------------------------------------------------------------------- #


class TestRuleProtocolSurface:
    """任务 31 契约：RuleProtocol 仅 5 方法；多余方法（如 check_freq）属替身实现细节。"""

    def test_protocol_has_exactly_five_public_methods(self) -> None:
        members = inspect.getmembers(RuleProtocol, predicate=inspect.isfunction)
        public = {n for n, _ in members if not n.startswith("_")}
        assert public == {
            "get_rule_version",
            "check_dose",
            "check_population",
            "lookup_rule",
            "list_rules",
        }

    def test_check_freq_not_in_protocol(self) -> None:
        """`check_freq` 是替身实现细节，不在抽象契约中。"""
        assert not hasattr(RuleProtocol, "check_freq")

    def test_check_route_not_in_protocol(self) -> None:
        """`check_route` 是替身实现细节，不在抽象契约中。"""
        assert not hasattr(RuleProtocol, "check_route")

    def test_old_alias_still_points_to_new_protocol(self) -> None:
        """`RuleAdapterProtocol` 作为旧名 alias 保留。"""
        assert RuleAdapterProtocol is RuleProtocol

    def test_protocol_is_runtime_checkable(self, rule_adapter: FakeRuleAdapter) -> None:
        """Protocol 必须 runtime_checkable（业务层 isinstance 校验）。"""
        assert isinstance(rule_adapter, RuleProtocol)
        assert isinstance(rule_adapter, RuleAdapterProtocol)  # alias 等价

    def test_not_an_adapter_fails_isinstance(self) -> None:
        """非适配器对象不应通过 isinstance 校验。"""

        class NotAnAdapter:
            pass

        assert not isinstance(NotAnAdapter(), RuleProtocol)


# --------------------------------------------------------------------------- #
# 契约一致性：fake 实现覆盖全部 5 个 Protocol 方法                               #
# --------------------------------------------------------------------------- #


class TestProtocolConformance:
    def test_fake_satisfies_protocol(self, rule_adapter: FakeRuleAdapter) -> None:
        assert isinstance(rule_adapter, RuleProtocol)

    def test_fake_has_all_five_protocol_methods(self, rule_adapter: FakeRuleAdapter) -> None:
        for name in (
            "get_rule_version",
            "check_dose",
            "check_population",
            "lookup_rule",
            "list_rules",
        ):
            assert hasattr(rule_adapter, name), f"missing {name}"
            assert callable(getattr(rule_adapter, name))


# --------------------------------------------------------------------------- #
# 版本号透传（get_rule_version）                                                  #
# --------------------------------------------------------------------------- #


class TestRuleVersion:
    def test_version_matches_yaml_header(self, rule_adapter: FakeRuleAdapter) -> None:
        """版本号取自 YAML 头 `version` 字段（dose 优先，pop / freq / route 一致）。"""
        version = rule_adapter.get_rule_version()
        assert version == "v2026.08"

    def test_version_is_stable_across_calls(self, rule_adapter: FakeRuleAdapter) -> None:
        """多次调用应一致（同一实例 / 同一配置）。"""
        v1 = rule_adapter.get_rule_version()
        v2 = rule_adapter.get_rule_version()
        assert v1 == v2 == "v2026.08"

    def test_version_logs_with_fake_prefix(
        self, rule_adapter: FakeRuleAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`get_rule_version` 调 DEBUG 日志带 `[FAKE]` 前缀。"""
        caplog.set_level(logging.DEBUG, logger="rxchk.tools.rule_adapter")
        rule_adapter.get_rule_version()
        msgs = [r.message for r in caplog.records]
        assert any("[FAKE] get_rule_version" in m for m in msgs)


# --------------------------------------------------------------------------- #
# lookup_rule 跨规则库单条查找                                                    #
# --------------------------------------------------------------------------- #


class TestLookupRule:
    def test_lookup_dose_rule(self, rule_adapter: FakeRuleAdapter) -> None:
        """在 dose 规则库找到 `R-DOSE-001`。"""
        rule = rule_adapter.lookup_rule("R-DOSE-001")
        assert rule is not None
        assert rule["rule_id"] == "R-DOSE-001"
        assert rule["rule_family"] == "dose"
        assert rule["drug_code"] == "DRUG_AMOX"

    def test_lookup_population_rule(self, rule_adapter: FakeRuleAdapter) -> None:
        """在 population 规则库找到 `R-POP-001`。"""
        rule = rule_adapter.lookup_rule("R-POP-001")
        assert rule is not None
        assert rule["rule_family"] == "population"
        assert rule["patient_flag"] == "pregnant"

    def test_lookup_freq_rule(self, rule_adapter: FakeRuleAdapter) -> None:
        """在 frequency 规则库找到一条 freq 规则。"""
        rules = rule_adapter.list_rules(family="frequency")
        if rules:
            rid = rules[0]["rule_id"]
            rule = rule_adapter.lookup_rule(rid)
            assert rule is not None
            assert rule["rule_family"] == "frequency"

    def test_lookup_route_rule(self, rule_adapter: FakeRuleAdapter) -> None:
        """在 route 规则库找到一条 route 规则。"""
        rules = rule_adapter.list_rules(family="route")
        if rules:
            rid = rules[0]["rule_id"]
            rule = rule_adapter.lookup_rule(rid)
            assert rule is not None
            assert rule["rule_family"] == "route"

    def test_lookup_unknown_returns_none(self, rule_adapter: FakeRuleAdapter) -> None:
        """不存在的 rule_id → None（不抛错）。"""
        assert rule_adapter.lookup_rule("R-NONEXISTENT-999") is None

    def test_lookup_rule_is_deepcopy(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        """返回的是 deep copy（修改返回值不污染内部状态）。"""
        rule1 = rule_adapter.lookup_rule("R-DOSE-001")
        assert rule1 is not None
        rule1["drug_code"] = "TAMPERED"
        rule2 = rule_adapter.lookup_rule("R-DOSE-001")
        assert rule2 is not None
        assert rule2["drug_code"] != "TAMPERED"

    def test_lookup_logs_with_fake_prefix(
        self, rule_adapter: FakeRuleAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`lookup_rule` 调 DEBUG 日志带 `[FAKE]` 前缀（命中 + 未命中各一次）。"""
        caplog.set_level(logging.DEBUG, logger="rxchk.tools.rule_adapter")
        rule_adapter.lookup_rule("R-DOSE-001")
        rule_adapter.lookup_rule("R-NONEXISTENT-999")
        msgs = [r.message for r in caplog.records]
        assert any(
            "[FAKE] lookup_rule(R-DOSE-001) -> family=dose" in m for m in msgs
        )
        assert any(
            "[FAKE] lookup_rule(R-NONEXISTENT-999) -> None" in m for m in msgs
        )


# --------------------------------------------------------------------------- #
# list_rules 列表 + 过滤                                                       #
# --------------------------------------------------------------------------- #


class TestListRules:
    def test_list_all_returns_all_families(self, rule_adapter: FakeRuleAdapter) -> None:
        rules = rule_adapter.list_rules()
        families = {r["rule_family"] for r in rules}
        # 默认配置 4 个 YAML 都加载了
        assert families == {"dose", "population", "frequency", "route"}
        assert len(rules) >= 5  # dose 5 + pop 5 + freq 1+ + route 1+

    def test_list_filter_by_family(self, rule_adapter: FakeRuleAdapter) -> None:
        dose_rules = rule_adapter.list_rules(family="dose")
        assert all(r["rule_family"] == "dose" for r in dose_rules)
        pop_rules = rule_adapter.list_rules(family="population")
        assert all(r["rule_family"] == "population" for r in pop_rules)

    def test_list_filter_by_drug_code(self, rule_adapter: FakeRuleAdapter) -> None:
        rules = rule_adapter.list_rules(drug_code="DRUG_AMOX")
        assert all(r["drug_code"] == "DRUG_AMOX" for r in rules)

    def test_list_filter_by_family_and_drug_code(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        rules = rule_adapter.list_rules(family="dose", drug_code="DRUG_AMOX")
        assert all(
            r["rule_family"] == "dose" and r["drug_code"] == "DRUG_AMOX"
            for r in rules
        )

    def test_list_unknown_family_raises(self, rule_adapter: FakeRuleAdapter) -> None:
        """未知 family → RuleAdapterError（不静默返回空）。"""
        with pytest.raises(RuleAdapterError):
            rule_adapter.list_rules(family="bogus")

    def test_list_empty_when_filter_no_match(self, rule_adapter: FakeRuleAdapter) -> None:
        rules = rule_adapter.list_rules(drug_code="DRUG_NO_EXIST")
        assert rules == []

    def test_list_sorted_by_family_then_rule_id(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        rules = rule_adapter.list_rules()
        family_order = {"dose": 0, "population": 1, "frequency": 2, "route": 3}
        for a, b in zip(rules, rules[1:]):
            fa, fb = family_order[a["rule_family"]], family_order[b["rule_family"]]
            assert (fa, a["rule_id"]) <= (fb, b["rule_id"])

    def test_list_logs_with_fake_prefix(
        self, rule_adapter: FakeRuleAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="rxchk.tools.rule_adapter")
        rule_adapter.list_rules()
        rule_adapter.list_rules(family="dose", drug_code="DRUG_AMOX")
        msgs = [r.message for r in caplog.records]
        assert any(
            "[FAKE] list_rules(family=None, drug_code=None)" in m for m in msgs
        )
        assert any(
            "[FAKE] list_rules(family=dose, drug_code=DRUG_AMOX)" in m for m in msgs
        )


# --------------------------------------------------------------------------- #
# `[FAKE]` 日志前缀（init + 关键方法）                                          #
# --------------------------------------------------------------------------- #


class TestFakeLogPrefix:
    def test_init_logs_with_fake_prefix(
        self, rule_adapter: FakeRuleAdapter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """初始化时打 `[FAKE] FakeRuleAdapter loaded: ...`。"""
        # rule_adapter fixture 创建时已记日志；现在再访问 caplog 时已经晚了
        # 直接构造一个新实例验证
        caplog.set_level(logging.INFO, logger="rxchk.tools.rule_adapter")
        FakeRuleAdapter(
            DATA_DIR / "rule" / "dose_rules.yaml",
            DATA_DIR / "rule" / "population_rules.yaml",
        )
        msgs = [r.message for r in caplog.records]
        assert any("[FAKE] FakeRuleAdapter loaded" in m for m in msgs)
        assert any("version=v2026.08" in m for m in msgs)


# --------------------------------------------------------------------------- #
# runtime_version_stamp + stamp_finding_metadata 增强                          #
# --------------------------------------------------------------------------- #


class TestRuntimeVersionStamp:
    def test_basic_returns_rule_version(self) -> None:
        meta = runtime_version_stamp("v2026.08")
        assert meta == {"rule_version": "v2026.08"}

    def test_empty_version(self) -> None:
        meta = runtime_version_stamp("")
        assert meta == {"rule_version": ""}

    def test_extra_metadata_merged(self) -> None:
        meta = runtime_version_stamp(
            "v2026.08", extra={"caller": "DoseAgent", "issued_at": "2026-08-20"}
        )
        assert meta == {
            "rule_version": "v2026.08",
            "caller": "DoseAgent",
            "issued_at": "2026-08-20",
        }

    def test_extra_can_override_rule_version(self) -> None:
        """`extra` 故意提供 rule_version 时覆盖 base（业务侧负责）。"""
        meta = runtime_version_stamp(
            "v2026.08", extra={"rule_version": "v2026.09-rc"}
        )
        assert meta["rule_version"] == "v2026.09-rc"

    def test_extra_none_does_not_pollute(self) -> None:
        meta = runtime_version_stamp("v2026.08", extra=None)
        assert meta == {"rule_version": "v2026.08"}

    def test_extra_empty_dict_does_not_pollute(self) -> None:
        meta = runtime_version_stamp("v2026.08", extra={})
        assert meta == {"rule_version": "v2026.08"}


class TestStampFindingMetadata:
    def test_basic_caller(self) -> None:
        meta = stamp_finding_metadata("v2026.08", caller="DoseAgent")
        assert meta == {"rule_version": "v2026.08", "caller": "DoseAgent"}

    def test_no_caller_no_field(self) -> None:
        """caller=None 时不写入 `caller` 字段（避免 None 污染）。"""
        meta = stamp_finding_metadata("v2026.08")
        assert meta == {"rule_version": "v2026.08"}
        assert "caller" not in meta

    def test_extra_merged_with_caller(self) -> None:
        meta = stamp_finding_metadata(
            "v2026.08",
            caller="DoseAgent",
            extra={"lookup_path": "data/rule/dose_rules.yaml"},
        )
        assert meta == {
            "rule_version": "v2026.08",
            "caller": "DoseAgent",
            "lookup_path": "data/rule/dose_rules.yaml",
        }

    def test_extra_overrides_caller(self) -> None:
        """`extra` 中再写 `caller` 时以后为准（业务侧负责）。"""
        meta = stamp_finding_metadata(
            "v2026.08",
            caller="DoseAgent",
            extra={"caller": "FrequencyAgent"},
        )
        assert meta["caller"] == "FrequencyAgent"


# --------------------------------------------------------------------------- #
# 替身原有核对方法（向后兼容，**不在** Protocol 内）                              #
# --------------------------------------------------------------------------- #


class TestFieldSpecificMethodsStillWork:
    """任务 31 把 `check_freq` / `check_route` 移出 Protocol；替身仍实现它们。"""

    def test_check_freq_callable(self, rule_adapter: FakeRuleAdapter) -> None:
        """替身仍可调 `check_freq`（FrequencyAgent 依赖）。"""
        result = rule_adapter.check_freq("DRUG_WAR", "bid")
        assert isinstance(result, FreqCheckResult)
        # 没配置 freq_rules 时 hits 为空；这里配置了
        assert isinstance(result.hits, list)

    def test_check_route_callable(self, rule_adapter: FakeRuleAdapter) -> None:
        """替身仍可调 `check_route`（RouteAgent 依赖）。"""
        result = rule_adapter.check_route("DRUG_AMOX", "iv")
        assert isinstance(result, RouteCheckResult)
        assert isinstance(result.hits, list)


# --------------------------------------------------------------------------- #
# 错误路径                                                                    #
# --------------------------------------------------------------------------- #


class TestErrorPaths:
    def test_missing_dose_rules_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RuleAdapterError):
            FakeRuleAdapter(
                tmp_path / "missing_dose.yaml",
                tmp_path / "missing_pop.yaml",
            )

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("version: v1\nrules: 'not-a-list'\n", encoding="utf-8")
        ok = tmp_path / "ok.yaml"
        ok.write_text("version: v1\nrules: []\n", encoding="utf-8")
        with pytest.raises(RuleAdapterError):
            FakeRuleAdapter(bad, ok)


# --------------------------------------------------------------------------- #
# __all__ 导出完整性                                                           #
# --------------------------------------------------------------------------- #


class TestModuleExports:
    def test_all_exports_present(self) -> None:
        from rxchk.tools import rule_adapter as mod

        expected = {
            "DoseCheckResult",
            "FakeRuleAdapter",
            "FreqCheckResult",
            "PopulationCheckResult",
            "RouteCheckResult",
            "RuleAdapterError",
            "RuleAdapterProtocol",
            "RuleHit",
            "RuleProtocol",
            "RuleVersionMismatch",
            "runtime_version_stamp",
            "stamp_finding_metadata",
        }
        assert expected.issubset(set(mod.__all__))


# --------------------------------------------------------------------------- #
# 业务侧集成：version 注入 Finding.rule_version                                  #
# --------------------------------------------------------------------------- #


class TestVersionPropagationToFindings:
    """spec.md 验证：finding.rule_version 来自 FakeRuleAdapter.get_rule_version()。"""

    def test_dose_hits_carry_rule_version(self, rule_adapter: FakeRuleAdapter) -> None:
        result = rule_adapter.check_dose("DRUG_AMOX", "1g", "po")
        for hit in result.hits:
            assert isinstance(hit, RuleHit)
            assert hit.rule_version == "v2026.08"

    def test_population_hits_carry_rule_version(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        result = rule_adapter.check_population("DRUG_PREG_CONTRA", ("pregnant",))
        assert len(result.hits) >= 1
        for hit in result.hits:
            assert hit.rule_version == "v2026.08"

    def test_rule_hit_can_stamp_to_finding(
        self, rule_adapter: FakeRuleAdapter
    ) -> None:
        """把 RuleHit.rule_version 经 runtime_version_stamp 注入 finding metadata。"""
        result = rule_adapter.check_dose("DRUG_AMOX", "1g", "po")
        assert result.hits
        hit = result.hits[0]
        meta = stamp_finding_metadata(
            hit.rule_version, caller="DoseAgent", extra={"rule_id": hit.rule_id}
        )
        assert meta["rule_version"] == "v2026.08"
        assert meta["caller"] == "DoseAgent"
        assert meta["rule_id"].startswith("R-DOSE-001")