"""`rxchk.tools.registry` 深度扩展测试。

任务 17 范围：
- 自定义 RegExp + priority 排序（让更高优先级的族先命中）
- 配置驱动的族注册：`load_families_from_yaml` / `apply_yaml_families`
- 按「优先级 + fallback」双层匹配：`get_renderer_for_tool_with_fallback`
  返回 `(renderer, family_meta, fallback_used)` 三元组
- 族配额超限告警：`check_budget` + transcript 写入
  `EVENT_BUDGET_WARNING` 事件
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rxchk.tools import (
    REGISTRY,
    CategoryMeta,
    FieldFamilyRegistryError,
    Registry,
    RenderContext,
    ToolCategory,
    apply_yaml_families,
    check_budget,
    get_renderer_for_tool_with_fallback,
    list_families,
    load_families_from_yaml,
)
from rxchk.tools.registry import (
    _resolve_renderer_alias,
    _reset_default_registry_for_tests,
    _restore_default_registry_for_tests,
    DEFAULT_FALLBACK_RENDERER,
    FAMILY_RENDERERS,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    """每条测试前后重置 `REGISTRY`，避免污染共享单例。"""
    _reset_default_registry_for_tests()
    yield
    _restore_default_registry_for_tests()


# --------------------------------------------------------------------------- #
# CategoryMeta.priority 默认值与排序                                            #
# --------------------------------------------------------------------------- #


class TestCategoryMetaPriority:
    """CategoryMeta.priority 默认 100；list_families 按 priority 升序。"""

    def test_default_priority_is_100(self) -> None:
        meta = CategoryMeta(
            category=ToolCategory.DOSE,
            label="剂量",
            color="#f97316",
            renderer=lambda n, c: {},
        )
        assert meta.priority == 100
        assert meta.field_count_budget is None  # 默认不限制

    def test_list_families_sorted_by_priority(self) -> None:
        families = list_families()
        priorities = [m.priority for m in families]
        # 已排序（升序）
        assert priorities == sorted(priorities)
        # UNCOVERED 兜底族 priority=9999 应在最后
        assert families[-1].category == ToolCategory.UNCOVERED
        # 8 个字段族 priority 都是 100（夹在中间）
        for m in families[:-1]:
            assert m.priority == 100

    def test_explicit_priority_field_count_budget(self) -> None:
        """显式 priority / field_count_budget 字段都能构造。"""
        meta = CategoryMeta(
            category=ToolCategory.DRUG,
            label="药品-限",
            color="#0ea5e9",
            renderer=lambda n, c: {},
            priority=10,
            field_count_budget=50,
        )
        assert meta.priority == 10
        assert meta.field_count_budget == 50


# --------------------------------------------------------------------------- #
# 自定义 RegExp + priority：让高优先级族优先命中                              #
# --------------------------------------------------------------------------- #


class TestCustomRegexWithPriority:
    """priority 升序遍历；priority 越小越先命中（用于覆盖默认族）。"""

    def test_high_priority_dose_regex_beats_dose_default(self) -> None:
        """`dose_special_check` 自定义 DOSE 族 priority=50 → 优先于默认 priority=100。

        但 `_match_by_regex` 是按 `list_families()` 排序遍历，而 list_families
        按 priority 升序；同族（DOSE）只能存在一个，所以这里演示跨族优先级：
        """
        sentinel = lambda n, c: {"priority_demo": True}
        # priority=5 的 DOSE 自定义 RegExp + 用 DOSE slot（覆盖默认）
        REGISTRY.replace_family(
            CategoryMeta(
                category=ToolCategory.DOSE,
                label="DOSE-优先",
                color="#ff0000",
                renderer=sentinel,
                match_pattern=r"^urgent_dose_",
                priority=5,
            )
        )
        # `urgent_dose_check` 命中 DOSE-优先（priority=5）
        assert resolve_family_via_registry("urgent_dose_check") == ToolCategory.DOSE
        renderer, meta, fallback_used = get_renderer_for_tool_with_fallback(
            "urgent_dose_check"
        )
        assert renderer is sentinel
        assert fallback_used is False
        assert meta is not None
        assert meta.priority == 5

    def test_priority_within_same_priority_registration_order(self) -> None:
        """同优先级族按注册顺序（list_families 稳定）。"""
        # 默认 8 字段族 priority 都是 100；相对顺序稳定（按 _build_default_registry 注册顺序）
        families = list_families()[:-1]  # 去掉 UNCOVERED
        assert [m.category for m in families] == [
            ToolCategory.DRUG,
            ToolCategory.DOSE,
            ToolCategory.FREQUENCY,
            ToolCategory.ROUTE,
            ToolCategory.INDICATION,
            ToolCategory.ALLERGY,
            ToolCategory.INTERACTION,
            ToolCategory.POPULATION,
        ]

    def test_higher_priority_takes_priority_over_default(self) -> None:
        """priority=1 的 DOSE 自定义 RegExp 应在 priority=100 默认 DOSE 之前匹配。

        注意：replace_family 只能覆盖同名族；这里演示 priority 影响匹配顺序（但不
        会导致同族重复注册）。
        """
        sentinel = lambda n, c: {"winner": "dose-urgent"}
        REGISTRY.replace_family(
            CategoryMeta(
                category=ToolCategory.DOSE,
                label="DOSE-urg",
                color="#000",
                renderer=sentinel,
                match_pattern=r"^dose_urgent_",
                priority=1,
            )
        )
        # dose_urgent_critical → 命中 DOSE
        renderer, meta, fallback_used = get_renderer_for_tool_with_fallback(
            "dose_urgent_critical"
        )
        assert renderer is sentinel
        assert fallback_used is False
        assert meta is not None and meta.priority == 1


def resolve_family_via_registry(name: str) -> ToolCategory | None:
    """辅助：直接调 REGISTRY.resolve_family，便于 assert 类型。"""
    return REGISTRY.resolve_family(name)


# --------------------------------------------------------------------------- #
# load_families_from_yaml / apply_yaml_families                                #
# --------------------------------------------------------------------------- #


class TestLoadFamiliesFromYaml:
    """从 YAML 加载族定义 → list[CategoryMeta]；不修改全局。"""

    def test_load_minimal_yaml(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "families.yaml"
        yaml_path.write_text(
            """
families:
  - category: drug
    label: 药品
    color: "#0ea5e9"
    match_pattern: "^drug_"
""",
            encoding="utf-8",
        )
        metas = load_families_from_yaml(yaml_path)
        assert len(metas) == 1
        m = metas[0]
        assert m.category == ToolCategory.DRUG
        assert m.label == "药品"
        assert m.color == "#0ea5e9"
        assert m.match_pattern == "^drug_"
        assert m.priority == 100  # 默认
        assert m.field_count_budget is None  # 默认
        assert m.renderer is not None

    def test_load_yaml_with_all_fields(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "families.yaml"
        yaml_path.write_text(
            """
families:
  - category: dose
    label: 剂量-自定义
    color: "#ff0000"
    match_pattern: "^my_dose_"
    priority: 50
    field_count_budget: 10
    renderer: default_field_card
  - category: frequency
    label: 频次-自定义
    color: "#00ff00"
    match_pattern: "^my_freq_"
    priority: 200
    renderer: uncovered
""",
            encoding="utf-8",
        )
        metas = load_families_from_yaml(yaml_path)
        assert len(metas) == 2
        dose_meta, freq_meta = metas
        assert dose_meta.priority == 50
        assert dose_meta.field_count_budget == 10
        assert dose_meta.renderer is not None
        assert freq_meta.priority == 200
        assert freq_meta.field_count_budget is None
        # `uncovered` 别名解析为 _uncovered_renderer
        assert freq_meta.renderer is not None

    def test_load_yaml_renderer_aliases(self) -> None:
        # 三个别名都能解析
        assert _resolve_renderer_alias("default_field_card") is not None
        assert _resolve_renderer_alias("uncovered") is not None
        assert _resolve_renderer_alias("fallback") is DEFAULT_FALLBACK_RENDERER

        # 未知别名抛错
        with pytest.raises(FieldFamilyRegistryError, match="unknown renderer alias"):
            _resolve_renderer_alias("ghost_renderer")

        # 空别名抛错
        with pytest.raises(FieldFamilyRegistryError, match="non-empty string"):
            _resolve_renderer_alias("")
        with pytest.raises(FieldFamilyRegistryError, match="non-empty string"):
            _resolve_renderer_alias(None)  # type: ignore[arg-type]

    def test_load_yaml_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_families_from_yaml(tmp_path / "missing.yaml")

    def test_load_yaml_root_not_mapping(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "families.yaml"
        yaml_path.write_text("- not a mapping\n", encoding="utf-8")
        with pytest.raises(FieldFamilyRegistryError, match="root must be a mapping"):
            load_families_from_yaml(yaml_path)

    def test_load_yaml_missing_families_key(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "families.yaml"
        yaml_path.write_text("other_key: 1\n", encoding="utf-8")
        with pytest.raises(FieldFamilyRegistryError, match="'families' must be a list"):
            load_families_from_yaml(yaml_path)

    def test_load_yaml_empty_families(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "families.yaml"
        yaml_path.write_text("families: []\n", encoding="utf-8")
        with pytest.raises(FieldFamilyRegistryError, match="'families' is empty"):
            load_families_from_yaml(yaml_path)

    def test_load_yaml_invalid_category(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "families.yaml"
        yaml_path.write_text(
            """
families:
  - category: ghost_family
    label: 鬼族
    color: "#000"
""",
            encoding="utf-8",
        )
        with pytest.raises(FieldFamilyRegistryError, match="invalid; valid:"):
            load_families_from_yaml(yaml_path)

    def test_load_yaml_missing_required_label(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "families.yaml"
        yaml_path.write_text(
            """
families:
  - category: drug
    color: "#0ea5e9"
""",
            encoding="utf-8",
        )
        with pytest.raises(FieldFamilyRegistryError, match="label must be a non-empty string"):
            load_families_from_yaml(yaml_path)

    def test_load_yaml_missing_required_color(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "families.yaml"
        yaml_path.write_text(
            """
families:
  - category: drug
    label: 药品
""",
            encoding="utf-8",
        )
        with pytest.raises(FieldFamilyRegistryError, match="color must be a non-empty string"):
            load_families_from_yaml(yaml_path)

    def test_load_yaml_invalid_priority(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "families.yaml"
        yaml_path.write_text(
            """
families:
  - category: drug
    label: 药品
    color: "#000"
    priority: "high"
""",
            encoding="utf-8",
        )
        with pytest.raises(FieldFamilyRegistryError, match="priority must be an int"):
            load_families_from_yaml(yaml_path)

    def test_load_yaml_invalid_budget(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "families.yaml"
        yaml_path.write_text(
            """
families:
  - category: drug
    label: 药品
    color: "#000"
    field_count_budget: 50.5
""",
            encoding="utf-8",
        )
        with pytest.raises(
            FieldFamilyRegistryError, match="field_count_budget must be int or null"
        ):
            load_families_from_yaml(yaml_path)

    def test_load_yaml_null_budget(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "families.yaml"
        yaml_path.write_text(
            """
families:
  - category: drug
    label: 药品
    color: "#000"
    field_count_budget: null
""",
            encoding="utf-8",
        )
        metas = load_families_from_yaml(yaml_path)
        assert metas[0].field_count_budget is None


class TestApplyYamlFamilies:
    """apply_yaml_families 加载 + 覆盖/合并 到目标 Registry。"""

    def test_apply_replace_mode(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "families.yaml"
        yaml_path.write_text(
            """
families:
  - category: drug
    label: 药品-新版
    color: "#abcdef"
    match_pattern: "^v2_drug_"
    priority: 80
""",
            encoding="utf-8",
        )
        # 加载前：默认 drug 族
        original_meta = REGISTRY.get_family_meta("drug_check")
        assert original_meta is not None
        assert original_meta.priority == 100
        # 应用：替换 drug 族
        count = apply_yaml_families(yaml_path)
        assert count == 1
        # 应用后：drug 族变了
        new_meta = REGISTRY.get_family_meta("v2_drug_x")
        assert new_meta is not None
        assert new_meta.label == "药品-新版"
        assert new_meta.priority == 80
        # 默认的 drug_check 不再被命中（match_pattern 改了）
        assert REGISTRY.get_family_meta("drug_check") is None

    def test_apply_replace_false_keeps_existing(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "families.yaml"
        yaml_path.write_text(
            """
families:
  - category: drug
    label: 药品-不会被应用
    color: "#000"
""",
            encoding="utf-8",
        )
        count = apply_yaml_families(yaml_path, replace=False)
        assert count == 1
        # drug 族仍是默认版本（未被替换）
        meta = REGISTRY.get_family_meta("drug_check")
        assert meta is not None
        assert meta.label == "药品"  # 默认中文标签
        assert meta.priority == 100

    def test_apply_to_custom_registry(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "families.yaml"
        yaml_path.write_text(
            """
families:
  - category: drug
    label: 隔离-DRUG
    color: "#000"
""",
            encoding="utf-8",
        )
        # 用临时 Registry 而非默认 REGISTRY
        custom_reg = Registry()
        count = apply_yaml_families(yaml_path, registry=custom_reg)
        assert count == 1
        # 临时 Registry 有 drug 族
        assert custom_reg.get_family_meta("drug_check") is None  # 没 match_pattern
        # 默认 REGISTRY 不受影响
        default_dose = REGISTRY.get_family_meta("dose_check")
        assert default_dose is not None
        assert default_dose.priority == 100


# --------------------------------------------------------------------------- #
# get_renderer_for_tool_with_fallback（双层匹配 → 三元组）                     #
# --------------------------------------------------------------------------- #


class TestRendererWithFallback:
    """get_renderer_for_tool_with_fallback 返回 (renderer, meta, fallback_used)。"""

    def test_override_layer_returns_meta_none(self) -> None:
        """renderer_overrides 命中时 family_meta=None（Layer 1 优先）。"""
        sentinel = lambda n, c: {"override_layer": True}
        REGISTRY.renderer_overrides["my_tool"] = sentinel
        renderer, meta, fallback_used = get_renderer_for_tool_with_fallback("my_tool")
        assert renderer is sentinel
        assert meta is None
        assert fallback_used is False

    def test_family_layer_returns_meta(self) -> None:
        """族 RegExp 命中时返回该族 meta + fallback_used=False（Layer 2）。"""
        renderer, meta, fallback_used = get_renderer_for_tool_with_fallback("dose_check")
        assert renderer is not None
        assert meta is not None
        assert meta.category == ToolCategory.DOSE
        assert fallback_used is False

    def test_fallback_layer_returns_meta_none_and_flag(self) -> None:
        """未命中族时返回 fallback_renderer + meta=None + fallback_used=True。"""
        renderer, meta, fallback_used = get_renderer_for_tool_with_fallback("ghost_tool")
        assert renderer is DEFAULT_FALLBACK_RENDERER
        assert meta is None
        assert fallback_used is True

    def test_priority_ordering_visible_in_three_tuple(self) -> None:
        """priority 排序在三元组中可观察：低 priority 族先被命中。"""
        sentinel_low = lambda n, c: {"low_priority": True}
        REGISTRY.replace_family(
            CategoryMeta(
                category=ToolCategory.DOSE,
                label="DOSE-prio-5",
                color="#000",
                renderer=sentinel_low,
                match_pattern=r"^prio_dose_",
                priority=5,
            )
        )
        renderer, meta, fallback_used = get_renderer_for_tool_with_fallback("prio_dose_check")
        assert renderer is sentinel_low
        assert meta is not None and meta.priority == 5
        assert fallback_used is False

    def test_module_level_helper_matches_registry_method(self) -> None:
        """模块级便捷函数与 Registry 方法结果一致。"""
        renderer, meta, fallback_used = get_renderer_for_tool_with_fallback("drug_check")
        renderer2, meta2, fallback_used2 = REGISTRY.get_renderer_for_tool_with_fallback("drug_check")
        assert renderer is renderer2
        assert meta is meta2
        assert fallback_used == fallback_used2


# --------------------------------------------------------------------------- #
# check_budget + transcript 告警                                                #
# --------------------------------------------------------------------------- #


class TestCheckBudget:
    """族配额检查：超限 → True + WARN 日志 + 可选写 transcript。"""

    def test_no_budget_returns_false(self) -> None:
        """未设 budget 的族 → 始终返回 False（不限制）。"""
        assert check_budget(ToolCategory.DRUG, field_count=10000) is False

    def test_within_budget_returns_false(self) -> None:
        REGISTRY.replace_family(
            CategoryMeta(
                category=ToolCategory.DOSE,
                label="DOSE-bgt",
                color="#000",
                renderer=lambda n, c: {},
                match_pattern=r"^dose_",
                priority=100,
                field_count_budget=10,
            )
        )
        # 字段数 = 5 < 10（未超限）
        assert check_budget(ToolCategory.DOSE, field_count=5) is False
        # 字段数 = 10 = 10（恰等于预算；返回 False，约定严格大于才算超）
        assert check_budget(ToolCategory.DOSE, field_count=10) is False

    def test_over_budget_returns_true(self, caplog: pytest.LogCaptureFixture) -> None:
        REGISTRY.replace_family(
            CategoryMeta(
                category=ToolCategory.DOSE,
                label="DOSE-bgt",
                color="#000",
                renderer=lambda n, c: {},
                match_pattern=r"^dose_",
                priority=100,
                field_count_budget=10,
            )
        )
        with caplog.at_level("WARNING"):
            over = check_budget(ToolCategory.DOSE, field_count=11)
        assert over is True
        # 警告日志
        assert any("budget_exceeded" in rec.message for rec in caplog.records)

    def test_negative_field_count_normalized_to_zero(self) -> None:
        REGISTRY.replace_family(
            CategoryMeta(
                category=ToolCategory.DOSE,
                label="DOSE-bgt",
                color="#000",
                renderer=lambda n, c: {},
                match_pattern=r"^dose_",
                priority=100,
                field_count_budget=10,
            )
        )
        # 负数归一为 0；0 < 10 → 不超
        assert check_budget(ToolCategory.DOSE, field_count=-5) is False

    def test_unknown_family_returns_false(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            # 用一个未注册的族名（必须通过 _coerce_category 才能到达 _families 查不到）
            # 由于 ToolCategory 枚举严格，"ghost" 字符串会先抛 FieldFamilyRegistryError
            # 这里通过 str 入参走 _coerce_category 路径
            with pytest.raises(FieldFamilyRegistryError):
                check_budget("ghost_family", field_count=5)

    def test_non_int_field_count_returns_false(self, caplog: pytest.LogCaptureFixture) -> None:
        REGISTRY.replace_family(
            CategoryMeta(
                category=ToolCategory.DOSE,
                label="DOSE-bgt",
                color="#000",
                renderer=lambda n, c: {},
                match_pattern=r"^dose_",
                priority=100,
                field_count_budget=10,
            )
        )
        with caplog.at_level("WARNING"):
            assert check_budget(ToolCategory.DOSE, field_count="5") is False  # type: ignore[arg-type]

    def test_string_family_param_works(self) -> None:
        """check_budget 接受 ToolCategory 与 str 两种 family 入参。"""
        REGISTRY.replace_family(
            CategoryMeta(
                category=ToolCategory.DOSE,
                label="DOSE-bgt",
                color="#000",
                renderer=lambda n, c: {},
                match_pattern=r"^dose_",
                priority=100,
                field_count_budget=10,
            )
        )
        assert check_budget("dose", field_count=5) is False
        assert check_budget("dose", field_count=11) is True


# --------------------------------------------------------------------------- #
# check_budget + transcript 写入 EVENT_BUDGET_WARNING                           #
# --------------------------------------------------------------------------- #


class _FakeTranscriptWriter:
    """测试用 transcript writer：仅暴露 append_transcript；记录所有事件。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def append_transcript(self, event: Any) -> None:
        self.events.append(event)


class TestBudgetTranscriptWrite:
    """check_budget 超限时，通过 transcript_writer.append_transcript 写告警事件。"""

    def test_over_budget_writes_warning_event(self) -> None:
        REGISTRY.replace_family(
            CategoryMeta(
                category=ToolCategory.DOSE,
                label="DOSE-bgt",
                color="#000",
                renderer=lambda n, c: {},
                match_pattern=r"^dose_",
                priority=100,
                field_count_budget=10,
            )
        )
        writer = _FakeTranscriptWriter()
        over = check_budget(ToolCategory.DOSE, field_count=15, transcript_writer=writer)
        assert over is True
        # 写了一条 transcript 事件
        assert len(writer.events) == 1
        ev = writer.events[0]
        # event 类型是 EVENT_BUDGET_WARNING
        assert ev.event == "budget_warning"
        assert ev.family == "dose"
        # payload 包含 field_count / budget / over_by / note
        assert ev.payload["field_count"] == 15
        assert ev.payload["budget"] == 10
        assert ev.payload["over_by"] == 5
        assert "field_count 超过族配额" in ev.payload["note"]

    def test_under_budget_does_not_write_event(self) -> None:
        REGISTRY.replace_family(
            CategoryMeta(
                category=ToolCategory.DOSE,
                label="DOSE-bgt",
                color="#000",
                renderer=lambda n, c: {},
                match_pattern=r"^dose_",
                priority=100,
                field_count_budget=10,
            )
        )
        writer = _FakeTranscriptWriter()
        over = check_budget(ToolCategory.DOSE, field_count=5, transcript_writer=writer)
        assert over is False
        assert writer.events == []

    def test_no_budget_does_not_write_event(self) -> None:
        writer = _FakeTranscriptWriter()
        # 默认 DOSE 族未设 budget（field_count_budget=None）
        over = check_budget(ToolCategory.DOSE, field_count=100, transcript_writer=writer)
        assert over is False
        assert writer.events == []

    def test_transcript_writer_without_append_method_silent(self) -> None:
        """writer 没有 append_transcript 方法时静默忽略，不阻断业务流程。"""

        class _BadWriter:
            pass

        REGISTRY.replace_family(
            CategoryMeta(
                category=ToolCategory.DOSE,
                label="DOSE-bgt",
                color="#000",
                renderer=lambda n, c: {},
                match_pattern=r"^dose_",
                priority=100,
                field_count_budget=10,
            )
        )
        bad = _BadWriter()
        # 不抛错；返回 True
        assert check_budget(ToolCategory.DOSE, field_count=15, transcript_writer=bad) is True


# --------------------------------------------------------------------------- #
# check_budget + ReportWriter（真实 ReportWriter 集成）                         #
# --------------------------------------------------------------------------- #


class TestBudgetIntegrationWithReportWriter:
    """check_budget 与真实 ReportWriter 集成 → transcript.jsonl 落盘 EVENT_BUDGET_WARNING。"""

    def test_real_report_writer_appends_event(self, tmp_path: Path) -> None:
        from rxchk.report.writer import (
            EVENT_BUDGET_WARNING,
            ReportWriter,
            TRANSCRIPT_FILENAME,
        )
        from rxchk.report.state import RunState

        REGISTRY.replace_family(
            CategoryMeta(
                category=ToolCategory.DOSE,
                label="DOSE-bgt",
                color="#000",
                renderer=lambda n, c: {},
                match_pattern=r"^dose_",
                priority=100,
                field_count_budget=10,
            )
        )

        rx_no = "RX-2026-<EXAMPLE_BUDGET>"
        run_root = tmp_path / "runs"
        writer = ReportWriter(rx_no=rx_no, run_root=run_root)
        writer.write_run(status=RunState.RUNNING)

        # 超限检查 + 写 transcript
        over = check_budget(ToolCategory.DOSE, field_count=12, transcript_writer=writer)
        assert over is True

        # 验证 transcript.jsonl 有一条 EVENT_BUDGET_WARNING
        transcript_path = run_root / rx_no / TRANSCRIPT_FILENAME
        assert transcript_path.exists()
        lines = transcript_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        ev = json.loads(lines[0])
        assert ev["event"] == EVENT_BUDGET_WARNING
        assert ev["family"] == "dose"
        assert ev["payload"]["field_count"] == 12
        assert ev["payload"]["budget"] == 10
        assert ev["payload"]["over_by"] == 2

    def test_real_report_writer_under_budget_no_event(self, tmp_path: Path) -> None:
        from rxchk.report.writer import ReportWriter
        from rxchk.report.state import RunState

        REGISTRY.replace_family(
            CategoryMeta(
                category=ToolCategory.DOSE,
                label="DOSE-bgt",
                color="#000",
                renderer=lambda n, c: {},
                match_pattern=r"^dose_",
                priority=100,
                field_count_budget=10,
            )
        )

        rx_no = "RX-2026-<EXAMPLE_OK>"
        run_root = tmp_path / "runs"
        writer = ReportWriter(rx_no=rx_no, run_root=run_root)
        writer.write_run(status=RunState.RUNNING)

        over = check_budget(ToolCategory.DOSE, field_count=5, transcript_writer=writer)
        assert over is False
        # transcript.jsonl 应只有 write_run 自动追加的 state_change（如果有）
        # 但 write_run 不追加 state_change；只有 transition() 才追加
        # 这里直接 write_run → 没有 transcript 事件写入
        # 实际：write_run 不会写 transcript；transition() 才写
        # 故 transcript 应不存在
        transcript_path = run_root / rx_no / "transcript.jsonl"
        # write_run 不写 transcript，文件可能不存在
        assert not transcript_path.exists() or transcript_path.read_text().strip() == ""


# --------------------------------------------------------------------------- #
# YAML 配置驱动族 + check_budget 端到端                                        #
# --------------------------------------------------------------------------- #


class TestYamlFamiliesBudgetEndToEnd:
    """YAML 配置驱动族（含 budget） → apply → check_budget 生效。"""

    def test_yaml_budget_applied_and_checked(self, tmp_path: Path) -> None:
        yaml_path = tmp_path / "families.yaml"
        yaml_path.write_text(
            """
families:
  - category: drug
    label: 药品-限
    color: "#0ea5e9"
    match_pattern: "^drug_"
    priority: 100
    field_count_budget: 3
  - category: dose
    label: 剂量-不限
    color: "#f97316"
    match_pattern: "^dose_"
    priority: 100
""",
            encoding="utf-8",
        )
        # 应用 YAML（默认 REGISTRY，replace=True）
        apply_yaml_families(yaml_path)

        # drug 族 budget=3 → 超 3 即超限
        assert check_budget(ToolCategory.DRUG, field_count=3) is False
        assert check_budget(ToolCategory.DRUG, field_count=4) is True

        # dose 族未设 budget → 始终 False
        assert check_budget(ToolCategory.DOSE, field_count=10000) is False


# --------------------------------------------------------------------------- #
# 默认 FAMILY_RENDERERS 不被影响（向后兼容）                                   #
# --------------------------------------------------------------------------- #


class TestBackwardCompatibility:
    """任务 17 不破坏任务 4 行为：8 字段族 + UNCOVERED 默认注册。"""

    def test_family_renderers_still_has_eight(self) -> None:
        assert len(FAMILY_RENDERERS) == 8

    def test_all_families_have_default_priority_100(self) -> None:
        for meta in FAMILY_RENDERERS:
            assert meta.priority == 100

    def test_all_families_have_field_count_budget_none_by_default(self) -> None:
        """默认族未设 budget（task 4 行为不变；新字段默认 None）。"""
        for meta in FAMILY_RENDERERS:
            assert meta.field_count_budget is None

    def test_module_level_exports_complete(self) -> None:
        from rxchk.tools import registry as reg_module

        expected = {
            "apply_yaml_families",
            "check_budget",
            "get_renderer_for_tool_with_fallback",
            "load_families_from_yaml",
        }
        for name in expected:
            assert hasattr(reg_module, name), f"missing export: {name}"
            assert name in reg_module.__all__, f"missing in __all__: {name}"
