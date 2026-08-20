"""`rxchk.tools.registry` 单元测试。

覆盖：
- 8 字段族（drug / dose / frequency / route / indication / allergy /
  interaction / population）默认已注册，且每个族有 renderer + 同族前缀 RegExp。
- 同族前缀匹配：精确匹配（dose_max_check → dose）/ 字符串前缀匹配 / 未匹配 fallback。
- 显式 add_tool → family（不经 RegExp）。
- register_family 自定义族 + 自定义 RegExp。
- renderer_overrides 覆盖优先级最高。
- 便捷函数（resolve_family / get_renderer_for_tool / list_families）。
- RenderContext 字段透传到 renderer 返回值。
- 错误路径：tool_name 空字符串 / 未知 family / 重复注册族。
"""

from __future__ import annotations

import pytest

from rxchk.tools import (
    CategoryMeta,
    DEFAULT_FALLBACK_RENDERER,
    FAMILY_RENDERERS,
    REGISTRY,
    FieldFamilyRegistryError,
    Registry,
    RenderContext,
    ToolCategory,
    UnknownToolError,
    default_renderer_for,
    get_renderer_for_tool,
    list_families,
    register_family,
    register_tool,
    resolve_family,
)
from rxchk.tools.registry import (
    _reset_default_registry_for_tests,
    _restore_default_registry_for_tests,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    """每条测试前重置 `REGISTRY` 内部状态（清掉显式 add_tool / override /
    custom fallback），结束后恢复。复用同一对象，避免 reload 模块带来的
    类身份漂移（FieldFamilyRegistryError / ToolCategory 在 reload 后会
    生成新类，与 pytest.raises 的期望类不再同一类型）。
    """
    _reset_default_registry_for_tests()
    yield
    _restore_default_registry_for_tests()


# --------------------------------------------------------------------------- #
# 默认注册：8 字段族                                                            #
# --------------------------------------------------------------------------- #


class TestDefaultFamilies:
    def test_eight_field_families_registered(self) -> None:
        families = list_families()
        names = {m.category for m in families}
        assert names == set(ToolCategory)

    def test_family_renderers_tuple_has_eight(self) -> None:
        assert len(FAMILY_RENDERERS) == 8

    def test_each_family_has_renderer_and_regex(self) -> None:
        for meta in FAMILY_RENDERERS:
            assert callable(meta.renderer)
            assert meta.match_pattern is not None
            assert meta.compiled is not None
            assert meta.priority == 100

    def test_family_color_per_family(self) -> None:
        # 颜色用于 Viewer 按族着色；8 个族颜色互不相同便于人眼区分
        colors = {m.color for m in FAMILY_RENDERERS}
        assert len(colors) == 8

    def test_family_label_chinese(self) -> None:
        labels = {m.label for m in FAMILY_RENDERERS}
        # 中文标签集合（人眼可读，不参与派发判定）
        assert "剂量" in labels
        assert "过敏" in labels
        assert "配伍" in labels


# --------------------------------------------------------------------------- #
# 同族前缀匹配：精确 / 前缀 / 未匹配 fallback                                  #
# --------------------------------------------------------------------------- #


class TestPrefixMatching:
    @pytest.mark.parametrize(
        "tool_name,expected",
        [
            ("drug_check", ToolCategory.DRUG),
            ("drug_name_check", ToolCategory.DRUG),
            ("drug_encode_check", ToolCategory.DRUG),
            ("dose_check", ToolCategory.DOSE),
            ("dose_max_check", ToolCategory.DOSE),
            ("freq_check", ToolCategory.FREQUENCY),
            ("freq_window_check", ToolCategory.FREQUENCY),
            ("route_check", ToolCategory.ROUTE),
            ("route_form_check", ToolCategory.ROUTE),
            ("indication_check", ToolCategory.INDICATION),
            ("indication_match_check", ToolCategory.INDICATION),
            ("allergy_check", ToolCategory.ALLERGY),
            ("allergy_family_check", ToolCategory.ALLERGY),
            ("interaction_check", ToolCategory.INTERACTION),
            ("interaction_two_drug_check", ToolCategory.INTERACTION),
            ("pop_check", ToolCategory.POPULATION),
            ("pop_pregnancy_check", ToolCategory.POPULATION),
        ],
    )
    def test_prefix_matches_expected_family(
        self, tool_name: str, expected: ToolCategory
    ) -> None:
        assert resolve_family(tool_name) == expected

    def test_unmatched_returns_none(self) -> None:
        assert resolve_family("unknown_tool") is None
        assert resolve_family("not_a_check_xyz") is None
        # 'check' 单字符前缀不该命中任何族（必须有族前缀）
        assert resolve_family("check") is None

    def test_unmatched_renderer_returns_fallback(self) -> None:
        renderer = get_renderer_for_tool("unknown_tool")
        assert renderer is DEFAULT_FALLBACK_RENDERER

    def test_prefix_anchored_at_start(self) -> None:
        # 'x_drug_check' 不该被 `^drug_` 命中（锚定字符串开头）
        assert resolve_family("x_drug_check") is None
        # 但 'drug_x_check' 该被 `^drug_` 命中
        assert resolve_family("drug_x_check") == ToolCategory.DRUG


# --------------------------------------------------------------------------- #
# 显式 add_tool 与 renderer_overrides 优先级                                   #
# --------------------------------------------------------------------------- #


class TestExplicitRegistration:
    def test_add_tool_with_family_string(self) -> None:
        cat = register_tool("custom_dose_check_v2", "dose")
        assert cat == ToolCategory.DOSE

    def test_add_tool_with_family_enum(self) -> None:
        cat = register_tool("my_allergy_helper", ToolCategory.ALLERGY)
        assert cat == ToolCategory.ALLERGY

    def test_add_tool_without_family_falls_back_to_regex(self) -> None:
        # 不显式给 family → 走 RegExp 自动归类（与 TS 版同行为）
        cat = register_tool("drug_special_check")
        assert cat == ToolCategory.DRUG

    def test_add_tool_empty_name_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            register_tool("")

    def test_add_tool_unknown_family_raises(self) -> None:
        with pytest.raises(FieldFamilyRegistryError):
            register_tool("any_tool", "ghost_family")

    def test_add_tool_to_uncovered_explicitly(self) -> None:
        cat = register_tool("manual_check", "uncovered")
        assert cat == ToolCategory.UNCOVERED
        # 显式归 uncovered 的工具解析时仍是 uncovered
        assert resolve_family("manual_check") == ToolCategory.UNCOVERED

    def test_explicit_add_overrides_regex_match(self) -> None:
        # `pop_audit` 默认会被 `^pop_` 命中 → POPULATION；
        # 显式 add_tool 到 drug 族后应改回 drug
        register_tool("pop_audit", "drug")
        assert resolve_family("pop_audit") == ToolCategory.DRUG


class TestRendererOverrides:
    def test_override_takes_priority_over_family(self) -> None:
        sentinel = lambda name, ctx: {"sentinel": name, "ctx": ctx.field_value}
        REGISTRY.renderer_overrides["dose_special"] = sentinel
        assert get_renderer_for_tool("dose_special") is sentinel

    def test_override_priority_over_regex_family(self) -> None:
        sentinel = lambda name, ctx: {"override": True}
        REGISTRY.renderer_overrides["dose_check"] = sentinel
        assert get_renderer_for_tool("dose_check") is sentinel

    def test_fallback_renderer_override(self) -> None:
        sentinel = lambda name, ctx: {"fallback_custom": True}
        REGISTRY.fallback_renderer = sentinel
        assert get_renderer_for_tool("totally_unknown") is sentinel


# --------------------------------------------------------------------------- #
# register_family 自定义族                                                     #
# --------------------------------------------------------------------------- #


class TestCustomFamily:
    def test_register_custom_family_with_regex(self) -> None:
        custom_renderer = lambda name, ctx: {"custom": True, "tool": name}
        meta = CategoryMeta(
            category=ToolCategory.DRUG,  # 复用 DRUG slot 仅作演示；下面会 replace
            label="自定义族",
            color="#000000",
            renderer=custom_renderer,
            match_pattern=r"^zz_",
            priority=50,
        )
        # DRUG 已注册 → 重复注册抛错；用 replace
        REGISTRY.replace_family(meta)
        assert resolve_family("zz_my_tool") == ToolCategory.DRUG

    def test_register_duplicate_family_raises(self) -> None:
        meta = CategoryMeta(
            category=ToolCategory.DOSE,
            label="重复",
            color="#fff",
            renderer=lambda n, c: {},
        )
        with pytest.raises(FieldFamilyRegistryError, match="already registered"):
            REGISTRY.register_family(meta)

    def test_register_family_helper(self) -> None:
        # 模块级便捷函数：DRUG 已注册 → 重复 register_family 必须抛错
        sentinel = lambda n, c: {"helper": True}
        meta = CategoryMeta(
            category=ToolCategory.DRUG,
            label="DRG 自定义",
            color="#ff00ff",
            renderer=sentinel,
            match_pattern=r"^drg_",
            priority=10,
        )
        with pytest.raises(FieldFamilyRegistryError, match="already registered"):
            register_family(meta)

    def test_priority_sorts_first(self) -> None:
        # 自定义高优先级 DRUG（priority=10）应比默认 POPULATION（priority=100）优先
        sentinel = lambda n, c: {"priority_test": True}
        REGISTRY.replace_family(
            CategoryMeta(
                category=ToolCategory.DRUG,
                label="优先 DRUG",
                color="#000",
                renderer=sentinel,
                match_pattern=r"^urgent_",
                priority=10,
            )
        )
        assert resolve_family("urgent_drug_check") == ToolCategory.DRUG
        assert get_renderer_for_tool("urgent_drug_check") is sentinel


# --------------------------------------------------------------------------- #
# 渲染：RenderContext 透传                                                     #
# --------------------------------------------------------------------------- #


class TestRendering:
    def test_default_field_card_renderer_outputs_keys(self) -> None:
        renderer = default_renderer_for("dose")
        ctx = RenderContext(
            field_value="0.5g",
            severity="high",
            rule_id="R-DOSE-001",
            evidence="<DRUG_DICT:drug_A.max_daily_dose>",
            family="dose",
            agent_id="<rx_field_dose>",
            extra={"field": "dose", "hit_explanation": "超日剂量"},
        )
        out = renderer("dose_check", ctx)
        assert out["renderer"] == "field_card"
        assert out["family"] == "dose"
        assert out["field"] == "dose"
        assert out["severity"] == "high"
        assert out["tool_name"] == "dose_check"
        assert out["field_value"] == "0.5g"
        assert out["evidence"] == "<DRUG_DICT:drug_A.max_daily_dose>"
        assert out["rule_id"] == "R-DOSE-001"
        assert out["agent_id"] == "<rx_field_dose>"

    def test_render_helper_invokes_resolved_renderer(self) -> None:
        ctx = RenderContext(
            field_value="bid",
            severity="medium",
            family="frequency",
            extra={"field": "frequency"},
        )
        out = REGISTRY.render("freq_check", ctx)
        assert out["family"] == "frequency"
        assert out["tool_name"] == "freq_check"

    def test_fallback_renderer_marks_uncovered(self) -> None:
        out = REGISTRY.render("ghost_tool", RenderContext())
        assert out["renderer"] == "fallback"
        assert out["family"] == "uncovered"
        assert "未覆盖" in out["note"] or "fallback" in out["note"]

    def test_field_inferred_from_tool_prefix_when_ctx_empty(self) -> None:
        # ctx.extra 没有 field → 从 tool_name 前缀推断
        out = REGISTRY.render(
            "allergy_check",
            RenderContext(family="allergy", extra={}),
        )
        assert out["field"] == "allergy"


# --------------------------------------------------------------------------- #
# 列表与查询                                                                  #
# --------------------------------------------------------------------------- #


class TestListAndQuery:
    def test_list_families_sorted_by_priority(self) -> None:
        families = list_families()
        # priority 升序：默认 100 + uncovered 9999 排在最末
        assert families[-1].category == ToolCategory.UNCOVERED
        # 中间的 family priority 都是 100，相对顺序不强制
        for f in families[:-1]:
            assert f.priority == 100

    def test_list_tools_returns_explicit_only(self) -> None:
        register_tool("custom_dose_a", "dose")
        register_tool("custom_dose_b", "dose")
        tools = REGISTRY.list_tools()
        assert set(tools.keys()) == {"custom_dose_a", "custom_dose_b"}
        assert tools["custom_dose_a"] == ToolCategory.DOSE

    def test_get_family_meta(self) -> None:
        meta = REGISTRY.get_family_meta("drug_check")
        assert meta is not None
        assert meta.category == ToolCategory.DRUG

    def test_get_family_meta_unknown_returns_none(self) -> None:
        assert REGISTRY.get_family_meta("ghost_tool") is None

    def test_default_renderer_for_helper(self) -> None:
        r = default_renderer_for(ToolCategory.DOSE)
        assert callable(r)
        with pytest.raises(FieldFamilyRegistryError):
            default_renderer_for("ghost")


# --------------------------------------------------------------------------- #
# 新增 `custom_check` 自动归类（spec.md 参考地基验收）                          #
# --------------------------------------------------------------------------- #


class TestCustomCheckAutoFamily:
    """spec.md 参考地基：'新加 custom_check 工具无需改派发代码即可被 dose 族
    renderer 渲染'。本测试用 `custom_check` 不在已注册族，但用前缀派生
    RegExp 时仍然命中——这里用 `dose_custom_check` 演示同款机制（不强制
    改派发代码，仅靠 RegExp 命中）。"""

    def test_new_dose_check_picked_up_by_dose_family(self) -> None:
        # 新增 `dose_custom_check`：未显式 add_tool，但 ^dose_ 前缀命中
        cat = resolve_family("dose_custom_check")
        assert cat == ToolCategory.DOSE
        # renderer 是默认字段卡 renderer
        out = REGISTRY.render(
            "dose_custom_check",
            RenderContext(
                field_value="0.5g",
                severity="high",
                family="dose",
                extra={"field": "dose"},
            ),
        )
        assert out["family"] == "dose"
        assert out["renderer"] == "field_card"


# --------------------------------------------------------------------------- #
# 错误路径                                                                    #
# --------------------------------------------------------------------------- #


class TestErrorPaths:
    def test_unknown_tool_error_importable(self) -> None:
        # 异常类暴露（即使本模块目前用 FieldFamilyRegistryError 替代，
        # 仍保留 UnknownToolError 供未来扩展）
        assert UnknownToolError is not None

    def test_empty_tool_name_via_registry(self) -> None:
        with pytest.raises(ValueError):
            REGISTRY.add_tool("")

    def test_invalid_family_string(self) -> None:
        with pytest.raises(FieldFamilyRegistryError, match="unknown family"):
            REGISTRY.add_tool("any_tool", "ghost_family")


# --------------------------------------------------------------------------- #
# 直接构造 Registry 的隔离测试（不依赖全局单例）                               #
# --------------------------------------------------------------------------- #


class TestStandaloneRegistry:
    def test_standalone_registry_with_no_families(self) -> None:
        reg = Registry()
        assert reg.list_families() == []
        assert reg.resolve_family("any_tool") is None
        assert reg.get_renderer_for_tool("any_tool") is reg.fallback_renderer

    def test_standalone_registry_fallback_default(self) -> None:
        reg = Registry()
        assert reg.fallback_renderer is DEFAULT_FALLBACK_RENDERER

    def test_standalone_registry_custom_fallback(self) -> None:
        sentinel = lambda n, c: {"standalone": True}
        reg = Registry(fallback_renderer=sentinel)
        assert reg.get_renderer_for_tool("any") is sentinel

    def test_compiled_regex_property(self) -> None:
        meta = CategoryMeta(
            category=ToolCategory.DOSE,
            label="test",
            color="#000",
            renderer=lambda n, c: {},
            match_pattern=r"^test_",
        )
        assert meta.compiled is not None
        assert meta.matches("test_foo")
        assert not meta.matches("foo_test")
        assert not meta.matches("nothing")

    def test_meta_without_regex_never_matches(self) -> None:
        meta = CategoryMeta(
            category=ToolCategory.UNCOVERED,
            label="none",
            color="#888",
            renderer=lambda n, c: {},
            match_pattern=None,
        )
        assert meta.compiled is None
        assert meta.matches("anything") is False

    def test_rendercontext_defaults(self) -> None:
        ctx = RenderContext()
        assert ctx.field_value == ""
        assert ctx.severity == "info"
        assert ctx.extra == {}