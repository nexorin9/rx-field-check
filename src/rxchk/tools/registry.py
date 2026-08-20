"""工具族注册表：字段族 → 同族前缀 RegExp → renderer 数据驱动映射。

参考源：`strix/viewer/frontend/src/components/live/tool-renderers/index.ts`
的 `CategoryMeta + match?: RegExp` 同族前缀匹配思路。本模块在 Python
侧复现这套主路径：

    TS 版本（参考）                       Python 版本（本模块）
    ─────────────────────────             ──────────────────────────
    enum ToolCategory                     class ToolCategory(str, Enum)
    interface CategoryMeta                @dataclass CategoryMeta
      renderer / icon / color /             renderer / label / color /
      match?: RegExp                        match_pattern: str
    CATEGORY_META  Record<TC, CM>          REGISTRY._families: dict[TC, CM]
    CATEGORY_TOOLS Record<TC, str[]>       REGISTRY._tools: dict[str, TC]
    RENDERER_OVERRIDES Partial<...>        Registry.renderer_overrides
    resolveCategory(name)                 resolve_family(name)
    getToolRenderer(name)                 get_renderer_for_tool(name)
    FALLBACK_META                          DEFAULT_FALLBACK_RENDERER

算法（与 TS 版本严格一致）：
    1. tool → family 直接查表（显式 add_tool(name, family)）
    2. 遍历已注册族，按 `match_pattern` RegExp 命中前缀
    3. 都未命中 → 返回 fallback renderer（默认 `uncovered` 黄底）

关键设计：
- 8 个字段族（drug / dose / frequency / route / indication / allergy /
  interaction / population）在 import 时即向 `REGISTRY` 注册，每个族
  的 `match_pattern` 与 `rxchk.models.FAMILY_PREFIX` 对齐。
- 新加 `*_check` 工具：调 `register_tool(name, family)` 显式归族，或
  仅靠同名族前缀 RegExp 自动归类（无需改派发代码）。
- `register_family(meta)` 支持自定义 RegExp（如 `pop_`）与优先级。
- `renderer` 是一个 `ToolRenderer` 可调用对象，签名
  `(tool_name, ctx: RenderContext) -> dict`，让 Viewer 端
  FieldCardRenderer 拿到一张结构化字段卡。

来源项目名 / slug 不出现在产品代码或字符串字面值；机制描述见 docstring。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, Union

from rxchk.models.prescription import FAMILY_PREFIX, FieldFamily

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 枚举与类型                                                                  #
# --------------------------------------------------------------------------- #


class ToolCategory(str, Enum):
    """工具族枚举。

    与 `rxchk.models.FieldFamily` 严格对齐（同一份字段语义在派发与
    渲染两侧的两种视角），并多一个 `UNCOVERED` 兜底族（参考源
    `FALLBACK_META = CATEGORY_META.telemetry`，本项目用 `uncovered`
    表达「规则库未载入该族」语义）。
    """

    DRUG = "drug"
    DOSE = "dose"
    FREQUENCY = "frequency"
    ROUTE = "route"
    INDICATION = "indication"
    ALLERGY = "allergy"
    INTERACTION = "interaction"
    POPULATION = "population"
    UNCOVERED = "uncovered"


# 字段族枚举 ↔ 工具前缀字符串：与 FAMILY_PREFIX 字段保持一一对应。
# 这里单独声明而不是直接复用 dict 是为了让 `CategoryMeta` 的
# `match_pattern` 可以基于 enum 推导，且族注册时可显式声明 override。
_FAMILY_TO_PREFIX: dict[ToolCategory, str] = {
    ToolCategory.DRUG: "drug_",
    ToolCategory.DOSE: "dose_",
    ToolCategory.FREQUENCY: "freq_",
    ToolCategory.ROUTE: "route_",
    ToolCategory.INDICATION: "indication_",
    ToolCategory.ALLERGY: "allergy_",
    ToolCategory.INTERACTION: "interaction_",
    ToolCategory.POPULATION: "pop_",
}


# renderer 调用签名（Python 版）：
#   renderer(tool_name: str, ctx: RenderContext) -> dict[str, Any]
# 返回 dict 与 Viewer 端 `FieldCardRenderer` 字段对齐，便于无 React
# 依赖的纯函数测试与 HTTP JSON 输出。
RenderResult = dict[str, Any]


@dataclass(frozen=True)
class RenderContext:
    """renderer 调用上下文。

    字段族 Agent 派发时把 Finding 字段值 / 严重度 / 证据片段等透传给
    renderer，便于生成字段证据卡 payload；保留 `extra` 让自定义族
    注册时不改 dataclass 字段也能注入额外数据。
    """

    field_value: str = ""
    severity: str = "info"
    rule_id: str = ""
    evidence: str = ""
    family: str = ""
    agent_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


ToolRenderer = Callable[[str, RenderContext], RenderResult]


# --------------------------------------------------------------------------- #
# 异常类型                                                                    #
# --------------------------------------------------------------------------- #


class FieldFamilyRegistryError(KeyError):
    """工具族注册表相关错误（族不存在 / 重复注册 / 工具注册参数错）。"""


class UnknownToolError(ValueError):
    """工具名不合法或无法解析到任何族，且未配置 fallback renderer。"""


# --------------------------------------------------------------------------- #
# CategoryMeta + 默认 renderer                                                #
# --------------------------------------------------------------------------- #


@dataclass
class CategoryMeta:
    """族元数据：renderer + 同族前缀 RegExp。

    与 TS 版 CategoryMeta 字段对齐：`renderer`（必有）+ `match_pattern`
    （可选 RegExp 字符串，自动编译）+ `label` / `color`（用于 Viewer
    按族着色）+ `priority`（任务 17 扩展，深度任务用）+ `field_count_budget`
    （任务 17 扩展：单族字段数配额；None = 不限制）。

    `render` 是为简化调用而设的实例方法，签名与 ToolRenderer 兼容。
    """

    category: ToolCategory
    label: str
    color: str
    renderer: ToolRenderer
    match_pattern: Optional[str] = None
    priority: int = 100
    field_count_budget: Optional[int] = None
    _compiled: Optional[re.Pattern[str]] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.match_pattern is not None:
            self._compiled = re.compile(self.match_pattern)

    @property
    def compiled(self) -> Optional[re.Pattern[str]]:
        return self._compiled

    def matches(self, tool_name: str) -> bool:
        """判断 tool_name 是否属于本族（仅看 RegExp；显式 add_tool 走 _tools 索引）。"""
        if self._compiled is None:
            return False
        return self._compiled.search(tool_name) is not None

    def render(self, tool_name: str, ctx: RenderContext) -> RenderResult:
        return self.renderer(tool_name, ctx)


# --------------------------------------------------------------------------- #
# 默认 renderer（纯函数，无外部依赖）                                          #
# --------------------------------------------------------------------------- #


def _default_field_card_renderer(tool_name: str, ctx: RenderContext) -> RenderResult:
    """8 字段族共用的「字段证据卡」renderer 工厂。

    输出与 Viewer 端 FieldCardRenderer 对齐：
    {"renderer": "field_card", "family", "field", "severity", "tool_name", ...}
    """
    return {
        "renderer": "field_card",
        "family": ctx.family or "unknown",
        "field": _infer_field_from_tool(tool_name, ctx),
        "severity": ctx.severity,
        "tool_name": tool_name,
        "rule_id": ctx.rule_id,
        "rule_version": ctx.extra.get("rule_version", ""),
        "evidence": ctx.evidence,
        "field_value": ctx.field_value,
        "hit_explanation": ctx.extra.get("hit_explanation", ""),
        "agent_id": ctx.agent_id,
    }


def _uncovered_renderer(tool_name: str, ctx: RenderContext) -> RenderResult:
    """fallback renderer（参考源 FALLBACK_META = CATEGORY_META.telemetry）。"""
    return {
        "renderer": "fallback",
        "family": ToolCategory.UNCOVERED.value,
        "field": _infer_field_from_tool(tool_name, ctx),
        "severity": ctx.severity or "info",
        "tool_name": tool_name,
        "rule_id": ctx.rule_id,
        "evidence": ctx.evidence,
        "field_value": ctx.field_value,
        "agent_id": ctx.agent_id,
        "note": "规则库未载入该字段族或工具未注册；按 fallback 渲染（不伪通过）",
    }


def _infer_field_from_tool(tool_name: str, ctx: RenderContext) -> str:
    """从工具名 / ctx 推断字段名（drug_check → 'drug'，等等）。"""
    if ctx.extra.get("field"):
        return ctx.extra["field"]
    # 工具名前缀 → 字段名
    for cat, prefix in _FAMILY_TO_PREFIX.items():
        if tool_name.startswith(prefix):
            return cat.value
    return tool_name


# 8 字段族共用同一个字段证据卡 renderer（按族在 meta.color 上着色）。
DEFAULT_FALLBACK_RENDERER: ToolRenderer = _uncovered_renderer


# --------------------------------------------------------------------------- #
# Registry                                                                     #
# --------------------------------------------------------------------------- #


class Registry:
    """工具族注册表（thread-unsafe 单例用法：进程级一个 `REGISTRY`）。

    对应 TS 版 `CATEGORY_META + CATEGORY_TOOLS + RENDERER_OVERRIDES +
    resolveCategory + getToolRenderer + getToolIcon` 的总和。

    数据结构：
        _families: dict[ToolCategory, CategoryMeta]   ← CATEGORY_META
        _tools: dict[str, ToolCategory]               ← TOOL_CATEGORY
        renderer_overrides: dict[str, ToolRenderer]    ← RENDERER_OVERRIDES
        fallback_renderer: ToolRenderer               ← FALLBACK_META.renderer
    """

    def __init__(
        self,
        *,
        fallback_renderer: ToolRenderer = DEFAULT_FALLBACK_RENDERER,
    ) -> None:
        self._families: dict[ToolCategory, CategoryMeta] = {}
        self._tools: dict[str, ToolCategory] = {}
        self.renderer_overrides: dict[str, ToolRenderer] = {}
        self.fallback_renderer = fallback_renderer

    # ------------------------------------------------------------------ #
    # 族注册                                                                #
    # ------------------------------------------------------------------ #

    def register_family(self, meta: CategoryMeta) -> None:
        """注册一个族；族已存在 → 抛 `FieldFamilyRegistryError`。"""
        if meta.category in self._families:
            raise FieldFamilyRegistryError(
                f"family already registered: {meta.category.value}"
            )
        self._families[meta.category] = meta

    def replace_family(self, meta: CategoryMeta) -> None:
        """强制覆盖一个族（用于配置重载 / 测试）。"""
        self._families[meta.category] = meta

    # ------------------------------------------------------------------ #
    # 工具注册                                                              #
    # ------------------------------------------------------------------ #

    def add_tool(
        self,
        tool_name: str,
        family: Union[ToolCategory, str, None] = None,
    ) -> ToolCategory:
        """把工具名归到一个族。

        Args:
            tool_name: 工具名（如 `dose_max_check` / `pop_pregnancy_check`）。
            family: 显式族；None → 走族前缀 RegExp 自动归类。

        Returns:
            工具解析后的族（`ToolCategory.UNCOVERED` 表示未命中任何族）。

        Raises:
            ValueError: tool_name 为空或 family 非法。
        """
        if not tool_name or not isinstance(tool_name, str):
            raise ValueError("tool_name must be a non-empty string")

        if family is None:
            resolved = self._match_by_regex(tool_name)
        else:
            cat = self._coerce_category(family)
            if cat not in self._families:
                raise FieldFamilyRegistryError(
                    f"cannot add tool {tool_name!r}: family {cat.value!r} not registered"
                )
            resolved = cat

        self._tools[tool_name] = resolved
        return resolved

    # ------------------------------------------------------------------ #
    # 查询                                                                  #
    # ------------------------------------------------------------------ #

    def resolve_family(self, tool_name: str) -> Optional[ToolCategory]:
        """解析工具名 → 族（与 TS `resolveCategory` 同算法）。

        顺序：
            1. 显式 add_tool 索引（`_tools`）
            2. 遍历 `_families`，按 `match_pattern` RegExp 命中
            3. 都未命中 → None（调用方决定 fallback）
        """
        direct = self._tools.get(tool_name)
        if direct is not None:
            return direct
        return self._match_by_regex(tool_name)

    def get_renderer_for_tool(self, tool_name: str) -> ToolRenderer:
        """解析工具名 → renderer。

        顺序：
            1. `renderer_overrides` 显式覆盖
            2. 族 renderer（按 resolve_family 结果）
            3. `fallback_renderer`
        """
        override = self.renderer_overrides.get(tool_name)
        if override is not None:
            return override
        cat = self.resolve_family(tool_name)
        if cat is not None and cat in self._families:
            return self._families[cat].renderer
        return self.fallback_renderer

    def get_renderer_for_tool_with_fallback(
        self, tool_name: str
    ) -> tuple[ToolRenderer, Optional[CategoryMeta], bool]:
        """按「优先级 + fallback」双层匹配解析工具 → renderer。

        与 `get_renderer_for_tool` 不同：本方法显式返回三元组
        `(renderer, family_meta, fallback_used)`，便于审计 / 测试判断
        「本次调用实际走了哪一层匹配」。Layer 1 命中（renderer_overrides）
        时 family_meta=None；Layer 2 命中（按优先级族 RegExp）时 family_meta
        为该族 CategoryMeta；Layer 3 命中（fallback_renderer）时
        family_meta=None / fallback_used=True。

        Args:
            tool_name: 工具名。

        Returns:
            (renderer, family_meta, fallback_used)：
            - renderer：最终调用的 ToolRenderer
            - family_meta：匹配到的族元数据（fallback 时为 None）
            - fallback_used：是否走了 fallback 层（仅 Layer 3 命中为 True）
        """
        # Layer 1：renderer_overrides 显式覆盖
        override = self.renderer_overrides.get(tool_name)
        if override is not None:
            return (override, None, False)
        # Layer 2：按优先级族 RegExp 匹配（priority 升序遍历）
        cat = self._match_by_regex(tool_name)
        if cat is not None and cat in self._families:
            return (self._families[cat].renderer, self._families[cat], False)
        # Layer 3：fallback
        return (self.fallback_renderer, None, True)

    def check_budget(
        self,
        family: Union[ToolCategory, str],
        field_count: int,
        *,
        transcript_writer: Optional[Any] = None,
    ) -> bool:
        """检查某族 `field_count` 是否超过族级配额；超限时记 warn 日志 + 可选写 transcript。

        Args:
            family: 字段族（ToolCategory 或其字符串值）。
            field_count: 实际字段数（如 finding 数 / Agent 命中数）。
            transcript_writer: 可选的 ReportWriter-like 对象；接受 `append_transcript`
                方法与 ReportWriter 同款签名。提供时，超限会追加一条
                `EVENT_BUDGET_WARNING` 事件到 transcript.jsonl。

        Returns:
            True 表示超限；False 表示未超限或族未设预算。
            **永不抛错**（超限只 warn + 写 transcript，不阻断业务流程）。

        Notes:
            - `field_count_budget=None` 的族视作不限制（始终返回 False）
            - 负数 `field_count` 视为 0
            - 未知族 → logger.warning + 返回 False（不阻断）
            - transcript_writer 提供但无 append_transcript → 静默忽略 transcript 写入
        """
        cat = self._coerce_category(family) if not isinstance(family, ToolCategory) else family
        meta = self._families.get(cat)
        if meta is None:
            logger.warning(
                "registry.check_budget unknown family=%s field_count=%s",
                cat.value,
                field_count,
            )
            return False
        if meta.field_count_budget is None:
            return False
        if not isinstance(field_count, int):
            logger.warning(
                "registry.check_budget non-int field_count=%s for family=%s",
                field_count,
                cat.value,
            )
            return False
        normalized = max(0, field_count)
        over = normalized > meta.field_count_budget
        if not over:
            return False

        logger.warning(
            "registry.budget_exceeded family=%s field_count=%d budget=%d",
            cat.value,
            normalized,
            meta.field_count_budget,
        )
        if transcript_writer is not None:
            append = getattr(transcript_writer, "append_transcript", None)
            if callable(append):
                try:
                    # 延迟导入避免 tools 模块顶层依赖 report 模块
                    from rxchk.report.writer import EVENT_BUDGET_WARNING, TranscriptEvent

                    append(
                        TranscriptEvent(
                            event=EVENT_BUDGET_WARNING,
                            family=cat.value,
                            agent_id=None,
                            payload={
                                "field_count": normalized,
                                "budget": meta.field_count_budget,
                                "over_by": normalized - meta.field_count_budget,
                                "note": "field_count 超过族配额；按 fallback 渲染或人工复核",
                            },
                        )
                    )
                except Exception as exc:  # pragma: no cover - 防御性
                    logger.warning(
                        "registry.check_budget transcript append failed: %s",
                        exc,
                    )
        return True

    def get_family_meta(self, tool_name: str) -> Optional[CategoryMeta]:
        cat = self.resolve_family(tool_name)
        if cat is None:
            return None
        return self._families.get(cat)

    def list_families(self) -> list[CategoryMeta]:
        """列出所有已注册族（按优先级升序）。"""
        return sorted(self._families.values(), key=lambda m: m.priority)

    def list_tools(self) -> dict[str, ToolCategory]:
        """所有已显式 add_tool 的工具 → 族映射（拷贝）。"""
        return dict(self._tools)

    def render(self, tool_name: str, ctx: RenderContext) -> RenderResult:
        """便捷方法：解析 → 调 renderer。"""
        return self.get_renderer_for_tool(tool_name)(tool_name, ctx)

    # ------------------------------------------------------------------ #
    # 内部                                                                  #
    # ------------------------------------------------------------------ #

    def _match_by_regex(self, tool_name: str) -> Optional[ToolCategory]:
        """按族 RegExp 顺序遍历，第一个命中即返回。"""
        # 优先级低的先匹配（priority 升序），同优先级按注册顺序。
        # 这样 `pop_*` 可以比默认 `drug_*` 更早命中（虽然前缀不冲突，
        # 但扩展自定义族时优先级是稳定排序的依据）。
        for meta in self.list_families():
            if meta.matches(tool_name):
                return meta.category
        return None

    def _coerce_category(self, family: Union[ToolCategory, str]) -> ToolCategory:
        if isinstance(family, ToolCategory):
            return family
        if isinstance(family, str):
            try:
                return ToolCategory(family)
            except ValueError as exc:
                raise FieldFamilyRegistryError(
                    f"unknown family {family!r}; "
                    f"valid: {[c.value for c in ToolCategory]}"
                ) from exc
        raise FieldFamilyRegistryError(
            f"family must be ToolCategory or str, got {type(family).__name__}"
        )


# --------------------------------------------------------------------------- #
# 默认 Registry + 8 字段族预注册                                                #
# --------------------------------------------------------------------------- #


def _build_default_registry() -> Registry:
    """构造带 8 字段族 + uncovered fallback 的默认 Registry。

    族 `match_pattern` 直接复用 `FAMILY_PREFIX`（与 `rxchk.models`
    一致），保证 Task 7 中字段族 Agent 用 `dose_*_check` 命名时，
    registry 自动归族，**无需在派发代码里硬编码 if-elif**。
    """
    reg = Registry()

    # 8 字段族按 FAMILY_PREFIX 同款前缀；render_pattern 锚定字符串开头
    # 匹配（`^prefix`），避免 `drug_check_v2` 被 `pop_` 这种更长前缀误
    # 归类（虽然同长度冲突罕见，但严格更安全）。
    family_color: dict[ToolCategory, str] = {
        ToolCategory.DRUG: "#0ea5e9",
        ToolCategory.DOSE: "#f97316",
        ToolCategory.FREQUENCY: "#a855f7",
        ToolCategory.ROUTE: "#10b981",
        ToolCategory.INDICATION: "#6366f1",
        ToolCategory.ALLERGY: "#dc2626",
        ToolCategory.INTERACTION: "#eab308",
        ToolCategory.POPULATION: "#ec4899",
    }
    for cat, prefix in _FAMILY_TO_PREFIX.items():
        meta = CategoryMeta(
            category=cat,
            label=_category_label(cat),
            color=family_color[cat],
            renderer=_default_field_card_renderer,
            match_pattern=rf"^{re.escape(prefix)}",
            priority=100,
        )
        reg.register_family(meta)

    # UNCOVERED 兜底族：故意不设置 match_pattern（永远不会被正则命中），
    # 仅作为 fallback 显式 override 时的归属。
    reg.replace_family(
        CategoryMeta(
            category=ToolCategory.UNCOVERED,
            label="未覆盖",
            color="#9ca3af",
            renderer=_uncovered_renderer,
            match_pattern=None,
            priority=9999,
        )
    )
    return reg


def _category_label(cat: ToolCategory) -> str:
    """族中文标签（用于 Viewer 卡片标题；不参与派发判定）。"""
    return {
        ToolCategory.DRUG: "药品",
        ToolCategory.DOSE: "剂量",
        ToolCategory.FREQUENCY: "频次",
        ToolCategory.ROUTE: "途径",
        ToolCategory.INDICATION: "适应症",
        ToolCategory.ALLERGY: "过敏",
        ToolCategory.INTERACTION: "配伍",
        ToolCategory.POPULATION: "人群",
        ToolCategory.UNCOVERED: "未覆盖",
    }.get(cat, cat.value)


# 全局默认注册表（与 TS 版的模块级 `CATEGORY_META` 常量同语义）。
REGISTRY: Registry = _build_default_registry()


# 暴露只读视图：8 字段族 CategoryMeta 列表（按注册顺序）。
FAMILY_RENDERERS: tuple[CategoryMeta, ...] = tuple(
    REGISTRY._families[c]
    for c in (
        ToolCategory.DRUG,
        ToolCategory.DOSE,
        ToolCategory.FREQUENCY,
        ToolCategory.ROUTE,
        ToolCategory.INDICATION,
        ToolCategory.ALLERGY,
        ToolCategory.INTERACTION,
        ToolCategory.POPULATION,
    )
)


# --------------------------------------------------------------------------- #
# 模块级便捷函数（与 TS 函数同名 API）                                          #
# --------------------------------------------------------------------------- #


def register_family(meta: CategoryMeta) -> None:
    """便捷：在默认 `REGISTRY` 上注册族。"""
    REGISTRY.register_family(meta)


def register_tool(
    tool_name: str,
    family: Union[ToolCategory, str, None] = None,
) -> ToolCategory:
    """便捷：在默认 `REGISTRY` 上注册工具。"""
    return REGISTRY.add_tool(tool_name, family)


def resolve_family(tool_name: str) -> Optional[ToolCategory]:
    """便捷：解析工具名 → 族。"""
    return REGISTRY.resolve_family(tool_name)


def get_renderer_for_tool(tool_name: str) -> ToolRenderer:
    """便捷：解析工具名 → renderer（与 TS `getToolRenderer` 同签名）。"""
    return REGISTRY.get_renderer_for_tool(tool_name)


def default_renderer_for(family: Union[ToolCategory, str]) -> ToolRenderer:
    """便捷：按族取默认 renderer（族必须已注册）。"""
    try:
        cat = family if isinstance(family, ToolCategory) else ToolCategory(family)
    except ValueError as exc:
        raise FieldFamilyRegistryError(
            f"unknown family {family!r}; "
            f"valid: {[c.value for c in ToolCategory]}"
        ) from exc
    meta = REGISTRY._families.get(cat)
    if meta is None:
        raise FieldFamilyRegistryError(f"family not registered: {cat.value}")
    return meta.renderer


def get_renderer_for_tool_with_fallback(
    tool_name: str,
) -> tuple[ToolRenderer, Optional[CategoryMeta], bool]:
    """便捷：在默认 `REGISTRY` 上按「优先级 + fallback」双层匹配解析工具。

    返回 `(renderer, family_meta, fallback_used)` 三元组；详见
    `Registry.get_renderer_for_tool_with_fallback`。
    """
    return REGISTRY.get_renderer_for_tool_with_fallback(tool_name)


def check_budget(
    family: Union[ToolCategory, str],
    field_count: int,
    *,
    transcript_writer: Optional[Any] = None,
) -> bool:
    """便捷：在默认 `REGISTRY` 上检查族配额；超限时打 warn + 可选写 transcript。

    详见 `Registry.check_budget`。永不抛错。
    """
    return REGISTRY.check_budget(
        family,
        field_count,
        transcript_writer=transcript_writer,
    )


def list_families() -> list[CategoryMeta]:
    """便捷：列出所有族（按优先级）。"""
    return REGISTRY.list_families()


# --------------------------------------------------------------------------- #
# 内部辅助（供 Task 5/7 测试用）                                                #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 配置驱动族注册（YAML 加载；任务 17 深度扩展）                                  #
# --------------------------------------------------------------------------- #


# YAML 中 renderer 字段允许的命名 token：
# - "default_field_card" → _default_field_card_renderer
# - "uncovered" → _uncovered_renderer
# - "fallback" → DEFAULT_FALLBACK_RENDERER（同 uncovered 语义，但语义上
#   表示「这就是 fallback」便于 yaml 作者区分）
_RENDERER_ALIASES: dict[str, ToolRenderer] = {
    "default_field_card": _default_field_card_renderer,
    "uncovered": _uncovered_renderer,
    "fallback": DEFAULT_FALLBACK_RENDERER,
}


def _resolve_renderer_alias(name: Optional[str]) -> ToolRenderer:
    """YAML renderer 字段 → ToolRenderer。

    Args:
        name: renderer 别名（"default_field_card" / "uncovered" /
            "fallback"）。None / 空字符串 / 未知别名 → 抛错。

    Raises:
        FieldFamilyRegistryError: 未知或空别名。
    """
    if not name or not isinstance(name, str):
        raise FieldFamilyRegistryError(
            f"renderer alias must be a non-empty string, got {name!r}"
        )
    renderer = _RENDERER_ALIASES.get(name)
    if renderer is None:
        raise FieldFamilyRegistryError(
            f"unknown renderer alias {name!r}; "
            f"valid: {sorted(_RENDERER_ALIASES.keys())}"
        )
    return renderer


def load_families_from_yaml(path: Union[str, Path]) -> list[CategoryMeta]:
    """从 YAML 文件加载族定义列表。

    YAML schema（最小集）：
        families:
          - category: drug
            label: 药品
            color: "#0ea5e9"
            match_pattern: "^drug_"
            priority: 100            # 可选，默认 100
            field_count_budget: 50   # 可选，默认 None（不限制）
            renderer: default_field_card  # 别名见 _RENDERER_ALIASES

    Args:
        path: YAML 文件路径（str / Path）。

    Returns:
        CategoryMeta 列表（按 YAML 中顺序）。

    Raises:
        FileNotFoundError: 文件不存在。
        FieldFamilyRegistryError: YAML schema 非法 / 字段缺失 / 字段类型错。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"families YAML not found: {p}")
    # 延迟导入：避免 tools.registry 顶层依赖 pyyaml（pyyaml 是可选）
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise FieldFamilyRegistryError(
            "PyYAML is required to load families YAML; install pyyaml"
        ) from exc

    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise FieldFamilyRegistryError(
            f"YAML parse failed for {p}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise FieldFamilyRegistryError(
            f"YAML root must be a mapping, got {type(data).__name__}"
        )
    families_raw = data.get("families")
    if not isinstance(families_raw, list):
        raise FieldFamilyRegistryError(
            f"YAML 'families' must be a list, got {type(families_raw).__name__}"
        )
    if not families_raw:
        raise FieldFamilyRegistryError("YAML 'families' is empty")

    metas: list[CategoryMeta] = []
    for idx, entry in enumerate(families_raw):
        if not isinstance(entry, dict):
            raise FieldFamilyRegistryError(
                f"families[{idx}] must be a mapping, got {type(entry).__name__}"
            )
        cat_str = entry.get("category")
        if not cat_str or not isinstance(cat_str, str):
            raise FieldFamilyRegistryError(
                f"families[{idx}].category must be a non-empty string"
            )
        try:
            cat = ToolCategory(cat_str)
        except ValueError as exc:
            valid = ", ".join(c.value for c in ToolCategory)
            raise FieldFamilyRegistryError(
                f"families[{idx}].category={cat_str!r} invalid; valid: {valid}"
            ) from exc

        label = entry.get("label")
        color = entry.get("color")
        if not isinstance(label, str) or not label:
            raise FieldFamilyRegistryError(
                f"families[{idx}].label must be a non-empty string"
            )
        if not isinstance(color, str) or not color:
            raise FieldFamilyRegistryError(
                f"families[{idx}].color must be a non-empty string"
            )

        priority_raw = entry.get("priority", 100)
        if not isinstance(priority_raw, int) or isinstance(priority_raw, bool):
            raise FieldFamilyRegistryError(
                f"families[{idx}].priority must be an int (got {type(priority_raw).__name__})"
            )

        budget_raw = entry.get("field_count_budget", None)
        budget: Optional[int]
        if budget_raw is None:
            budget = None
        elif isinstance(budget_raw, int) and not isinstance(budget_raw, bool):
            budget = budget_raw
        else:
            raise FieldFamilyRegistryError(
                f"families[{idx}].field_count_budget must be int or null "
                f"(got {type(budget_raw).__name__})"
            )

        match_pattern = entry.get("match_pattern", None)
        if match_pattern is not None and not isinstance(match_pattern, str):
            raise FieldFamilyRegistryError(
                f"families[{idx}].match_pattern must be a string or null"
            )

        renderer_alias = entry.get("renderer", "default_field_card")
        renderer = _resolve_renderer_alias(renderer_alias)

        metas.append(
            CategoryMeta(
                category=cat,
                label=label,
                color=color,
                renderer=renderer,
                match_pattern=match_pattern,
                priority=priority_raw,
                field_count_budget=budget,
            )
        )
    return metas


def apply_yaml_families(
    path: Union[str, Path],
    *,
    registry: Optional[Registry] = None,
    replace: bool = True,
) -> int:
    """从 YAML 加载族并应用到指定 Registry（默认 REGISTRY）。

    Args:
        path: YAML 文件路径。
        registry: 目标 Registry；None → 模块级 REGISTRY。
        replace: True → 已存在的族覆盖；False → 保留旧族（同名跳过）。

    Returns:
        实际应用到 registry 的族数。
    """
    metas = load_families_from_yaml(path)
    reg = registry if registry is not None else REGISTRY
    if replace:
        for meta in metas:
            reg.replace_family(meta)
    else:
        for meta in metas:
            if meta.category in reg._families:
                continue
            reg.register_family(meta)
    return len(metas)


def _reset_default_registry_for_tests() -> Registry:
    """仅供单元测试：清空默认 `REGISTRY` 的可变状态。

    只清空显式 add_tool / renderer_overrides / 自定义 fallback，并还
    原被 `replace_family` 改过的族（备份族元数据，测试结束恢复）。8
    个默认族的 renderer / color / priority 全部回到 import 时的快照。

    调用方负责在测试结束后调 `_restore_default_registry_for_tests` 恢复。

    默认场景下不要调用；模块级 `REGISTRY` 是进程级共享单例。
    """
    _SAVED_REGISTRY_STATE["families"] = {
        cat: meta for cat, meta in REGISTRY._families.items()
    }
    _SAVED_REGISTRY_STATE["tools"] = dict(REGISTRY._tools)
    _SAVED_REGISTRY_STATE["overrides"] = dict(REGISTRY.renderer_overrides)
    _SAVED_REGISTRY_STATE["fallback"] = REGISTRY.fallback_renderer
    REGISTRY._tools.clear()
    REGISTRY.renderer_overrides.clear()
    REGISTRY.fallback_renderer = DEFAULT_FALLBACK_RENDERER
    return REGISTRY


_SAVED_REGISTRY_STATE: dict[str, object] = {}


def _restore_default_registry_for_tests() -> None:
    """仅供单元测试：恢复 `_reset_default_registry_for_tests` 备份的状态。"""
    REGISTRY._families.clear()
    REGISTRY._families.update(_SAVED_REGISTRY_STATE["families"])  # type: ignore[arg-type]
    REGISTRY._tools.clear()
    REGISTRY._tools.update(_SAVED_REGISTRY_STATE["tools"])  # type: ignore[arg-type]
    REGISTRY.renderer_overrides.clear()
    REGISTRY.renderer_overrides.update(_SAVED_REGISTRY_STATE["overrides"])  # type: ignore[arg-type]
    REGISTRY.fallback_renderer = _SAVED_REGISTRY_STATE["fallback"]  # type: ignore[assignment]
    _SAVED_REGISTRY_STATE.clear()


__all__ = [
    "CategoryMeta",
    "DEFAULT_FALLBACK_RENDERER",
    "FAMILY_RENDERERS",
    "FieldFamilyRegistryError",
    "REGISTRY",
    "RenderContext",
    "RenderResult",
    "Registry",
    "ToolCategory",
    "ToolRenderer",
    "UnknownToolError",
    "_FAMILY_TO_PREFIX",
    "_reset_default_registry_for_tests",
    "_restore_default_registry_for_tests",
    "apply_yaml_families",
    "check_budget",
    "default_renderer_for",
    "get_renderer_for_tool",
    "get_renderer_for_tool_with_fallback",
    "list_families",
    "load_families_from_yaml",
    "register_family",
    "register_tool",
    "resolve_family",
]


# 防止 linter 报 unused import（FieldFamily 由 FAMILY_PREFIX 间接导入）
_ = (FieldFamily,)