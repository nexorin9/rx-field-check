"""工具族注册表 + 规则 / 字典 / 配伍 / 过敏 适配器。

注册表（`registry`）负责字段族 → renderer 映射（参见 rxchk.tools.registry
的 TS 同族前缀匹配移植）。适配器层（`rule_adapter` / `drug_dict` /
`interaction_db` / `allergy_db`）定义正式对接契约 + YAML 替身实现，
供字段族 Agent（任务 7）调用。

源 repo 名 / slug 不出现在产品代码或字符串字面值；机制描述见各子模块。
"""

from rxchk.tools.allergy_db import (
    AllergyDbError,
    AllergyDbProtocol,
    AllergyHit,
    AllergySeverity,
    AllergenEntry,
    FakeAllergyDb,
    SEVERITY_TO_FINDING as ALLERGY_SEVERITY_TO_FINDING,
)
from rxchk.tools.drug_dict import (
    DrugDictError,
    DrugDictProtocol,
    DrugRecord,
    FakeDrugDict,
)
from rxchk.tools.interaction_db import (
    FakeInteractionDb,
    InteractionDbError,
    InteractionDbProtocol,
    InteractionHit,
    InteractionSeverity,
    SEVERITY_TO_FINDING as INTERACTION_SEVERITY_TO_FINDING,
)
from rxchk.tools.registry import (
    DEFAULT_FALLBACK_RENDERER,
    FAMILY_RENDERERS,
    REGISTRY,
    CategoryMeta,
    FieldFamilyRegistryError,
    Registry,
    RenderContext,
    RenderResult,
    ToolCategory,
    ToolRenderer,
    UnknownToolError,
    apply_yaml_families,
    check_budget,
    default_renderer_for,
    get_renderer_for_tool,
    get_renderer_for_tool_with_fallback,
    list_families,
    load_families_from_yaml,
    register_family,
    register_tool,
    resolve_family,
)
from rxchk.tools.rule_adapter import (
    DoseCheckResult,
    FakeRuleAdapter,
    FreqCheckResult,
    PopulationCheckResult,
    RouteCheckResult,
    RuleAdapterError,
    RuleAdapterProtocol,  # 旧名 alias，保留兼容
    RuleHit,
    RuleProtocol,  # 任务 31 新契约
    RuleVersionMismatch,
    runtime_version_stamp,
    stamp_finding_metadata,
)

__all__ = [
    # registry
    "DEFAULT_FALLBACK_RENDERER",
    "FAMILY_RENDERERS",
    "REGISTRY",
    "CategoryMeta",
    "FieldFamilyRegistryError",
    "Registry",
    "RenderContext",
    "RenderResult",
    "ToolCategory",
    "ToolRenderer",
    "UnknownToolError",
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
    # rule_adapter
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
    # drug_dict
    "DrugDictError",
    "DrugDictProtocol",
    "DrugRecord",
    "FakeDrugDict",
    # interaction_db
    "FakeInteractionDb",
    "InteractionDbError",
    "InteractionDbProtocol",
    "InteractionHit",
    "InteractionSeverity",
    "INTERACTION_SEVERITY_TO_FINDING",
    # allergy_db
    "AllergyDbError",
    "AllergyDbProtocol",
    "AllergyHit",
    "AllergySeverity",
    "AllergenEntry",
    "FakeAllergyDb",
    "ALLERGY_SEVERITY_TO_FINDING",
]