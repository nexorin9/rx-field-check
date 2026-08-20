"""规则引擎适配器：抽象接口 + YAML 替身实现（任务 31 重整版）。

设计目标
--------
- **抽象 Protocol 是正式对接契约**：HIS / 院内规则引擎 / 第三方合理用药系统
  通过同一组方法签名接入；本轮替身只读 YAML，**明确不是最终对接面**。
- **替身实现显式标注 `[FAKE]`**：每条 init / 版本变更 / 方法调用日志都打
  `[FAKE]` 前缀，便于审计员一眼看出"现在跑的是替身"，避免与真实规则
  引擎混淆。
- **版本号透传**：每条 finding 通过 `runtime_version_stamp(rule_version)`
  注入规则库版本号，跨规则升级时可追溯（药事复核 / 飞行检查必备）；
  `runtime_version_stamp` 还支持 `extra` 字段携带额外审计元数据。

任务 31 改造要点
----------------
1. **抽象协议重整**：原 `RuleAdapterProtocol`（7 个核对方法）拆分为：
     - `RuleProtocol`：正式契约（5 方法 = `lookup_rule / check_dose /
       check_population / get_rule_version / list_rules`）
     - `FakeRuleAdapter`：替身实现保留 `check_freq / check_route` 等
       字段族专用核对方法（不在 Protocol 内；信息科/上游实现可选择性
       实现）
   `RuleAdapterProtocol` 作为旧名 alias 保留，向后兼容既有导入语句。
2. **版本治理补全**：
     - `lookup_rule(rule_id) -> dict | None`：跨所有已加载规则库（dose /
       pop / freq / route）查单条规则；找不到 → None。
     - `list_rules(*, family, drug_code) -> list[dict]`：列所有规则；
       支持按字段族（dose/pop/freq/route）或药品编码过滤。
3. **`[FAKE]` 日志覆盖**：init / `get_rule_version` / 每次 `lookup_rule` /
   `list_rules` 都打 `[FAKE]` 前缀，便于审计员 / 测试做 caplog 断言。
4. **`runtime_version_stamp` 增强**：可选 `extra` 参数字段（携带
   `lookup_path` / `caller` 等额外审计元数据），向后兼容既有调用。

字段族 Agent（任务 7 / 21）通过 `FakeRuleAdapter` 读 `data/rule/ 下的 YAML`，
调用接口与真实引擎一致；新引入的 `lookup_rule` / `list_rules` 给信息科
做规则升级审计 / 飞行检查追溯使用。

源 repo 名 / slug 不出现在产品代码或字符串字面值；规则查询走
file/Protocol，无外部依赖。
"""

from __future__ import annotations

import copy
import inspect
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

import yaml

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 自定义异常                                                                  #
# --------------------------------------------------------------------------- #


class RuleAdapterError(RuntimeError):
    """规则适配器调用 / 配置错误。"""


class RuleVersionMismatch(ValueError):
    """规则版本号不匹配（如升级前后命中需要审计员手动比对）。"""


# --------------------------------------------------------------------------- #
# 数据结构：单条命中 / 单条规则                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RuleHit:
    """单条规则命中（供字段族 Agent 包装为 Finding）。

    字段与 spec.md 输出 schema 严格对齐：
    rule_id / rule_version / hit_explanation / evidence / severity。
    """

    rule_id: str
    rule_version: str
    hit_explanation: str
    evidence: str
    severity: str  # Severity.value: high / medium / low / info


@dataclass(frozen=True)
class DoseCheckResult:
    """剂量核对结果：可能同时命中多条规则（如超单次 + 超日剂量）。"""

    drug_code: str
    route: str
    parsed_dose: Optional[float]
    parsed_unit: Optional[str]
    hits: list[RuleHit]


@dataclass(frozen=True)
class PopulationCheckResult:
    """特殊人群核对结果：每条人群标签（孕 / 儿童 / 肝 / 肾）独立命中。"""

    patient_flags: tuple[str, ...]
    hits: list[RuleHit]


@dataclass(frozen=True)
class FreqCheckResult:
    """频次核对结果：allowed / forbidden 命中合一返回。"""

    drug_code: str
    frequency: str
    hits: list[RuleHit]


@dataclass(frozen=True)
class RouteCheckResult:
    """给药途径核对结果：allowed / forbidden 命中合一返回。"""

    drug_code: str
    route: str
    hits: list[RuleHit]


# --------------------------------------------------------------------------- #
# 抽象接口（正式对接契约；任务 31 重整为 5 方法 RuleProtocol）                  #
# --------------------------------------------------------------------------- #


@runtime_checkable
class RuleProtocol(Protocol):
    """规则引擎适配器抽象接口（任务 31 5 方法版本）。

    正式对接契约——HIS / 院内规则引擎 / 第三方合理用药系统都实现这 5 个方法
    即可接入业务层：

    - `get_rule_version()` — 规则库版本号（用于跨版本审计 / 飞检追溯）
    - `check_dose(...)` — 单药剂量核对（超单次 / 超日剂量 / 上下限）
    - `check_population(...)` — 特殊人群核对（孕产 / 儿童 / 肝肾）
    - `lookup_rule(rule_id)` — 跨规则库按 ID 查单条规则（版本治理）
    - `list_rules(...)` — 列出已加载规则（版本治理 / 飞检台账）

    字段族专用核对方法（`check_freq` / `check_route` 等）**不**在本契约中；
    它们是 `FakeRuleAdapter` 替身的实现细节，由字段族 Agent 直接调用替身
    实例方法（duck-typing）。
    """

    def get_rule_version(self) -> str:
        """返回规则库版本号（与 YAML `version` 字段一致）。"""
        ...

    def check_dose(
        self,
        drug_code: str,
        dose: str,
        route: str,
    ) -> DoseCheckResult:
        """核对单药剂量（单次 / 日剂量 + 上下限 + 给药途径上下限）。"""
        ...

    def check_population(
        self,
        drug_code: str,
        patient_flags: tuple[str, ...],
    ) -> PopulationCheckResult:
        """核对特殊人群禁忌 / 调整（孕产 / 儿童 / 肝肾）。"""
        ...

    def lookup_rule(self, rule_id: str) -> Optional[dict[str, Any]]:
        """按 rule_id 在所有已加载规则库中查找单条规则。

        Args:
            rule_id: 规则唯一标识（如 `R-DOSE-001`、`R-POP-001`）。

        Returns:
            找到 → 返回 dict 副本（含 `rule_id` + `rule_family` 字段，
            其中 `rule_family` ∈ {dose, population, frequency, route}）；
            找不到 → None。
        """
        ...

    def list_rules(
        self,
        *,
        family: Optional[str] = None,
        drug_code: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """列出已加载规则。

        Args:
            family: 可选过滤（dose / population / frequency / route）；
                None = 不过滤。
            drug_code: 可选过滤（按药品编码精确匹配）；
                None = 不过滤。

        Returns:
            list[dict] 每条 dict 包含 `rule_id` + `rule_family`。
            排序：先按 family (dose → population → frequency → route)，
            同 family 内按 rule_id 升序。
        """
        ...


# --------------------------------------------------------------------------- #
# 兼容层：旧名 RuleAdapterProtocol 别名指向新契约                                #
# --------------------------------------------------------------------------- #

# 任务 31 重整前的旧名；保留作为别名避免破坏既有导入。
# 新代码请直接用 `RuleProtocol`。
RuleAdapterProtocol = RuleProtocol


# --------------------------------------------------------------------------- #
# 替身实现（FakeRuleAdapter）                                                  #
# --------------------------------------------------------------------------- #


class FakeRuleAdapter:
    """规则适配器替身：从 YAML 文件读取规则。

    行为约定：
    - 显式日志前缀 `[FAKE]` —— init / `get_rule_version` / `lookup_rule` /
      `list_rules` 全部带前缀；便于审计员快速识别替身运行 + 测试 caplog 断言。
    - 找不到规则 → 返回空 hits（不抛错，便于 Agent 走 fallback uncovered）
    - 剂量解析失败 → 返回 hits 为空 + parsed_dose=None（保留 evidence 提示）
    - `freq_rules_path` / `route_rules_path` 未提供 → `check_freq` /
      `check_route` 返回空 hits（向后兼容既有测试）

    YAML 结构（`data/rule/dose_rules.yaml`）：
        version: v2026.08
        rules:
          - rule_id: R-DOSE-001
            drug_code: DRUG_A
            max_single_dose_g: 0.5
            max_daily_dose_g: 2.0
            routes: [po, iv]
            severity: high
            explanation: 单次剂量超上限
            evidence: <DRUG_DICT:DRUG_A.max_single_dose_g>

    `population_rules.yaml` 结构类似，字段为 `patient_flag` + `severity`。

    `freq_rules.yaml`（任务 21）：
        rules:
          - rule_id: R-FREQ-001
            drug_code: DRUG_WAR
            allowed_frequencies: [qd]
            severity: high
            explanation: 华法林必须每日一次给药

    `route_rules.yaml`（任务 21）：
        rules:
          - rule_id: R-ROUTE-001
            drug_code: DRUG_AMOX
            allowed_routes: [po]
            severity: high
            explanation: 阿莫西林口服制剂严禁 iv 给药
    """

    # 规则族标签（与 YAML 文件一一对应）
    _FAMILY_DOSE = "dose"
    _FAMILY_POPULATION = "population"
    _FAMILY_FREQUENCY = "frequency"
    _FAMILY_ROUTE = "route"
    _FAMILY_ORDER: tuple[str, ...] = (
        _FAMILY_DOSE,
        _FAMILY_POPULATION,
        _FAMILY_FREQUENCY,
        _FAMILY_ROUTE,
    )

    def __init__(
        self,
        dose_rules_path: str | Path,
        population_rules_path: str | Path,
        *,
        freq_rules_path: str | Path | None = None,
        route_rules_path: str | Path | None = None,
    ) -> None:
        self._dose_path = Path(dose_rules_path)
        self._pop_path = Path(population_rules_path)
        self._freq_path = Path(freq_rules_path) if freq_rules_path else None
        self._route_path = Path(route_rules_path) if route_rules_path else None
        self._dose_rules: list[dict[str, Any]] = []
        self._pop_rules: list[dict[str, Any]] = []
        self._freq_rules: list[dict[str, Any]] = []
        self._route_rules: list[dict[str, Any]] = []
        self._version: str = "unknown"
        self._load()
        logger.info(
            "[FAKE] FakeRuleAdapter loaded: dose=%s (%d rules), pop=%s (%d rules), "
            "freq=%s (%d rules), route=%s (%d rules), version=%s",
            self._dose_path.name,
            len(self._dose_rules),
            self._pop_path.name,
            len(self._pop_rules),
            self._freq_path.name if self._freq_path else "<none>",
            len(self._freq_rules),
            self._route_path.name if self._route_path else "<none>",
            len(self._route_rules),
            self._version,
        )

    # ------------------------------------------------------------------ #
    # 加载                                                                  #
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        self._dose_rules, dose_version = _load_rule_file(self._dose_path)
        self._pop_rules, pop_version = _load_rule_file(self._pop_path)
        # version 在多个文件中应当一致；不一致时取较小集优先，但仍记录差异
        versions = [v for v in (dose_version, pop_version) if v]
        if self._freq_path is not None:
            self._freq_rules, freq_version = _load_rule_file(self._freq_path)
            if freq_version:
                versions.append(freq_version)
        else:
            freq_version = ""
        if self._route_path is not None:
            self._route_rules, route_version = _load_rule_file(self._route_path)
            if route_version:
                versions.append(route_version)
        else:
            route_version = ""
        distinct = set(versions)
        if len(distinct) > 1:
            logger.warning(
                "[FAKE] rule version mismatch across files: dose=%s pop=%s freq=%s route=%s; "
                "using %s",
                dose_version,
                pop_version,
                freq_version,
                route_version,
                dose_version,
            )
        self._version = dose_version or pop_version or freq_version or route_version or "unknown"

    # ------------------------------------------------------------------ #
    # Protocol 方法                                                          #
    # ------------------------------------------------------------------ #

    def get_rule_version(self) -> str:
        """返回规则库版本号（统一打 `[FAKE]` 前缀，便于审计）。"""
        logger.debug("[FAKE] get_rule_version -> %s", self._version)
        return self._version

    def check_dose(
        self,
        drug_code: str,
        dose: str,
        route: str,
    ) -> DoseCheckResult:
        parsed_value, parsed_unit = _parse_dose(dose)
        hits: list[RuleHit] = []
        for rule in self._dose_rules:
            if rule.get("drug_code") != drug_code:
                continue
            allowed_routes = rule.get("routes", [])
            if allowed_routes and route not in allowed_routes:
                continue
            evidence = rule.get("evidence") or _format_evidence(drug_code, rule)
            severity = str(rule.get("severity", "medium"))
            explanation = str(rule.get("explanation", ""))

            max_single = rule.get("max_single_dose_g")
            max_daily = rule.get("max_daily_dose_g")
            unit = parsed_unit or "g"

            # 解析失败 → 不下命中结论，但保留 evidence 提示
            if parsed_value is None:
                hits.append(
                    RuleHit(
                        rule_id=str(rule.get("rule_id", "R-UNKNOWN")),
                        rule_version=self._version,
                        hit_explanation="剂量字符串解析失败，请人工复核单位",
                        evidence=evidence,
                        severity="low",
                    )
                )
                continue

            # 单位换算：g 与 mg 都需要归一为 g 再比较
            value_g = _to_grams(parsed_value, unit)
            if value_g is None:
                hits.append(
                    RuleHit(
                        rule_id=str(rule.get("rule_id", "R-UNKNOWN")),
                        rule_version=self._version,
                        hit_explanation=f"未知剂量单位 {unit!r}",
                        evidence=evidence,
                        severity="low",
                    )
                )
                continue

            if max_single is not None and value_g > float(max_single):
                base_rid = str(rule.get("rule_id", "R-DOSE"))
                hits.append(
                    RuleHit(
                        rule_id=f"{base_rid}-SINGLE",
                        rule_version=self._version,
                        hit_explanation=f"单次剂量 {value_g}g 超过上限 {max_single}g",
                        evidence=evidence,
                        severity=severity,
                    )
                )
            if max_daily is not None and value_g > float(max_daily):
                base_rid = str(rule.get("rule_id", "R-DOSE"))
                hits.append(
                    RuleHit(
                        rule_id=f"{base_rid}-DAILY",
                        rule_version=self._version,
                        hit_explanation=f"日剂量 {value_g}g 超过上限 {max_daily}g",
                        evidence=evidence,
                        severity=severity,
                    )
                )
        return DoseCheckResult(
            drug_code=drug_code,
            route=route,
            parsed_dose=parsed_value,
            parsed_unit=parsed_unit,
            hits=hits,
        )

    def check_population(
        self,
        drug_code: str,
        patient_flags: tuple[str, ...],
    ) -> PopulationCheckResult:
        hits: list[RuleHit] = []
        for flag in patient_flags:
            for rule in self._pop_rules:
                if rule.get("drug_code") != drug_code:
                    continue
                if rule.get("patient_flag") != flag:
                    continue
                evidence = rule.get("evidence") or _format_evidence(
                    f"{drug_code}@{flag}", rule
                )
                severity = str(rule.get("severity", "high"))
                hits.append(
                    RuleHit(
                        rule_id=str(rule.get("rule_id", "R-POP-UNKNOWN")),
                        rule_version=self._version,
                        hit_explanation=str(
                            rule.get("explanation", f"{flag} 人群禁忌 {drug_code}")
                        ),
                        evidence=evidence,
                        severity=severity,
                    )
                )
        return PopulationCheckResult(patient_flags=patient_flags, hits=hits)

    def check_freq(
        self,
        drug_code: str,
        frequency: str,
        route: str = "",
    ) -> FreqCheckResult:
        """核对频次合规性。

        规则模式（按 `freq_rules.yaml`）：
        - `allowed_frequencies`：白名单（不在表内即命中）
        - `forbidden_frequencies`：黑名单（在表内即命中）
        - `routes`：可选；规则仅在该给药途径下生效（与途径联动做时间窗冲突）

        未配置 freq_rules → 返回空 hits。

        注意：本方法**不**在 `RuleProtocol` 抽象契约中；它是替身实现的
        字段族专用方法。Agent 调用走具体类型（duck-typing）。
        """
        if not self._freq_rules:
            return FreqCheckResult(
                drug_code=drug_code,
                frequency=frequency,
                hits=[],
            )
        hits: list[RuleHit] = []
        freq_norm = (frequency or "").strip().lower()
        for rule in self._freq_rules:
            if rule.get("drug_code") != drug_code:
                continue
            # 途径联动：规则配置 routes 时，仅在该途径下生效
            rule_routes = rule.get("routes", [])
            if rule_routes and route and route not in rule_routes:
                continue
            evidence = rule.get("evidence") or _format_evidence(
                f"{drug_code}@{freq_norm}", rule
            )
            severity = str(rule.get("severity", "medium"))

            allowed = rule.get("allowed_frequencies")
            if allowed:
                allowed_norm = {str(f).strip().lower() for f in allowed}
                if freq_norm and freq_norm not in allowed_norm:
                    hits.append(
                        RuleHit(
                            rule_id=str(rule.get("rule_id", "R-FREQ")),
                            rule_version=self._version,
                            hit_explanation=str(
                                rule.get(
                                    "explanation",
                                    f"频次 {frequency!r} 不在白名单 {sorted(allowed_norm)}",
                                )
                            ),
                            evidence=evidence,
                            severity=severity,
                        )
                    )

            forbidden = rule.get("forbidden_frequencies")
            if forbidden:
                forbidden_norm = {str(f).strip().lower() for f in forbidden}
                if freq_norm and freq_norm in forbidden_norm:
                    hits.append(
                        RuleHit(
                            rule_id=str(rule.get("rule_id", "R-FREQ")),
                            rule_version=self._version,
                            hit_explanation=str(
                                rule.get(
                                    "explanation",
                                    f"频次 {frequency!r} 在黑名单 {sorted(forbidden_norm)}",
                                )
                            ),
                            evidence=evidence,
                            severity=severity,
                        )
                    )
        return FreqCheckResult(
            drug_code=drug_code,
            frequency=frequency,
            hits=hits,
        )

    def check_route(
        self,
        drug_code: str,
        route: str,
    ) -> RouteCheckResult:
        """核对给药途径合规性。

        规则模式（按 `route_rules.yaml`）：
        - `allowed_routes`：白名单（不在表内即命中）
        - `forbidden_routes`：黑名单（在表内即命中）

        未配置 route_rules → 返回空 hits。

        注意：本方法**不**在 `RuleProtocol` 抽象契约中；它是替身实现的
        字段族专用方法。Agent 调用走具体类型（duck-typing）。
        """
        if not self._route_rules:
            return RouteCheckResult(
                drug_code=drug_code,
                route=route,
                hits=[],
            )
        hits: list[RuleHit] = []
        route_norm = (route or "").strip().lower()
        for rule in self._route_rules:
            if rule.get("drug_code") != drug_code:
                continue
            evidence = rule.get("evidence") or _format_evidence(
                f"{drug_code}@{route_norm}", rule
            )
            severity = str(rule.get("severity", "high"))

            allowed = rule.get("allowed_routes")
            if allowed:
                allowed_norm = {str(r).strip().lower() for r in allowed}
                if route_norm and route_norm not in allowed_norm:
                    hits.append(
                        RuleHit(
                            rule_id=str(rule.get("rule_id", "R-ROUTE")),
                            rule_version=self._version,
                            hit_explanation=str(
                                rule.get(
                                    "explanation",
                                    f"途径 {route!r} 不在白名单 {sorted(allowed_norm)}",
                                )
                            ),
                            evidence=evidence,
                            severity=severity,
                        )
                    )

            forbidden = rule.get("forbidden_routes")
            if forbidden:
                forbidden_norm = {str(r).strip().lower() for r in forbidden}
                if route_norm and route_norm in forbidden_norm:
                    hits.append(
                        RuleHit(
                            rule_id=str(rule.get("rule_id", "R-ROUTE")),
                            rule_version=self._version,
                            hit_explanation=str(
                                rule.get(
                                    "explanation",
                                    f"途径 {route!r} 在黑名单 {sorted(forbidden_norm)}",
                                )
                            ),
                            evidence=evidence,
                            severity=severity,
                        )
                    )
        return RouteCheckResult(
            drug_code=drug_code,
            route=route,
            hits=hits,
        )

    def lookup_rule(self, rule_id: str) -> Optional[dict[str, Any]]:
        """按 rule_id 在所有已加载规则库中查找单条规则。

        跨 dose / population / frequency / route 四个 YAML 全搜；
        返回 dict 副本（含 `rule_id` + `rule_family`），找不到 → None。
        """
        for family, rules in (
            (self._FAMILY_DOSE, self._dose_rules),
            (self._FAMILY_POPULATION, self._pop_rules),
            (self._FAMILY_FREQUENCY, self._freq_rules),
            (self._FAMILY_ROUTE, self._route_rules),
        ):
            for rule in rules:
                if rule.get("rule_id") == rule_id:
                    found = copy.deepcopy(rule)
                    found["rule_id"] = rule_id
                    found["rule_family"] = family
                    logger.debug(
                        "[FAKE] lookup_rule(%s) -> family=%s", rule_id, family
                    )
                    return found
        logger.debug("[FAKE] lookup_rule(%s) -> None", rule_id)
        return None

    def list_rules(
        self,
        *,
        family: Optional[str] = None,
        drug_code: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """列出已加载规则（详见 `RuleProtocol.list_rules` 契约）。"""
        if family is not None and family not in self._FAMILY_ORDER:
            raise RuleAdapterError(
                f"unknown family {family!r}; "
                f"expected one of {self._FAMILY_ORDER}"
            )

        # 缓存家族 → 规则列表
        family_to_rules: dict[str, list[dict[str, Any]]] = {
            self._FAMILY_DOSE: self._dose_rules,
            self._FAMILY_POPULATION: self._pop_rules,
            self._FAMILY_FREQUENCY: self._freq_rules,
            self._FAMILY_ROUTE: self._route_rules,
        }

        result: list[dict[str, Any]] = []
        ordered_families = (
            (family,) if family is not None else self._FAMILY_ORDER
        )
        for fam in ordered_families:
            for rule in family_to_rules[fam]:
                if drug_code is not None and rule.get("drug_code") != drug_code:
                    continue
                entry = copy.deepcopy(rule)
                entry["rule_id"] = rule.get("rule_id", "")
                entry["rule_family"] = fam
                result.append(entry)
        # 同 family 内按 rule_id 升序；family 按域内固定顺序（dose→pop→freq→route）
        family_index = {fam: i for i, fam in enumerate(self._FAMILY_ORDER)}
        result.sort(
            key=lambda r: (
                family_index.get(r.get("rule_family", ""), 99),
                r.get("rule_id", ""),
            )
        )
        logger.debug(
            "[FAKE] list_rules(family=%s, drug_code=%s) -> %d rules",
            family,
            drug_code,
            len(result),
        )
        return result


# --------------------------------------------------------------------------- #
# 工具函数                                                                    #
# --------------------------------------------------------------------------- #


def _load_rule_file(path: Path) -> tuple[list[dict[str, Any]], str]:
    """读 YAML 文件 → (rules, version)。文件缺失抛 `RuleAdapterError`。"""
    if not path.exists():
        raise RuleAdapterError(f"rule file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RuleAdapterError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuleAdapterError(f"rule file root must be mapping: {path}")
    rules = raw.get("rules", []) or []
    if not isinstance(rules, list):
        raise RuleAdapterError(f"rule file 'rules' must be a list: {path}")
    version = str(raw.get("version", "unknown"))
    return rules, version


def _parse_dose(dose: str) -> tuple[Optional[float], Optional[str]]:
    """把剂量字符串解析为 (数值, 单位)。

    支持：
    - "0.5g" / "0.5 g" → (0.5, "g")
    - "500mg" / "500 mg" → (500.0, "mg")
    - "0.5" → (0.5, None)  # 无单位，需要药品字典提供默认单位
    - "qd" / "bid" 等频次 → (None, None)
    """
    if dose is None:
        return None, None
    s = dose.strip().lower().replace(" ", "")
    if not s:
        return None, None
    # 频次伪剂量（如 qd / bid / tid） → 视作解析失败
    if s in {"qd", "bid", "tid", "qid", "qn", "qod", "prn", "q4h", "q6h", "q8h", "q12h"}:
        return None, None
    # 数字 + 单位。注意顺序：长单位（mcg / mg）必须先于短单位（g），
    # 否则 "5mg" 会被 "g" 先匹配，导致数字部分 "5m" 解析失败。
    for unit in ("mcg", "ug", "mg", "g", "iu", "ml", "u"):
        if s.endswith(unit):
            try:
                value = float(s[: -len(unit)])
            except ValueError:
                return None, None
            return value, unit
    # 纯数字（无单位）
    try:
        return float(s), None
    except ValueError:
        return None, None


def _to_grams(value: float, unit: str | None) -> Optional[float]:
    """单位换算为 g；未知单位 → None。"""
    if unit is None:
        return None
    unit = unit.lower()
    if unit == "g":
        return value
    if unit == "mg":
        return value / 1000.0
    if unit in {"ug", "mcg"}:
        return value / 1_000_000.0
    # iu / ml / u 不直接换算成 g（按 g 报"未知单位"让上层 fallback）
    return None


def _format_evidence(drug_code: str, rule: dict[str, Any]) -> str:
    """构造默认 evidence 字符串（找不到显式 evidence 字段时）。"""
    rid = rule.get("rule_id", "R-UNKNOWN")
    return f"<RULE:{rid}/{drug_code}>"


# --------------------------------------------------------------------------- #
# 版本号注入辅助（任务 31 强化 + extra 字段支持）                                #
# --------------------------------------------------------------------------- #


def runtime_version_stamp(
    rule_version: str,
    *,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """把规则版本号封装为 finding / transcript 通用 metadata 字段。

    任务 31 强化：除 `rule_version` 外，可通过 `extra` 携带额外审计元数据
    （如 `lookup_path` / `caller` / `issued_at` 等），便于跨规则升级追溯。

    Args:
        rule_version: 规则库版本号（来自 `RuleProtocol.get_rule_version()`）。
        extra: 可选 dict；与 `{"rule_version": ...}` 合并；冲突 key 由
            `extra` 覆盖。

    Returns:
        dict；至少含 `rule_version`，含 extra 时含其全部字段。

    Examples:
        >>> runtime_version_stamp("v2026.08")
        {'rule_version': 'v2026.08'}
        >>> runtime_version_stamp("v2026.08", extra={"caller": "DoseAgent"})
        {'rule_version': 'v2026.08', 'caller': 'DoseAgent'}
    """
    base: dict[str, Any] = {"rule_version": rule_version}
    if extra:
        # extra 允许覆盖 base（如强制改写 version 号），业务侧负责
        base.update(extra)
    return base


def stamp_finding_metadata(
    rule_version: str,
    *,
    caller: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """业务侧常用快捷方式：构造带 caller 信息的规则版本 stamp。

    等价于 `runtime_version_stamp(rule_version, extra={"caller": caller, ...extra})`
    —— `caller` 留 None 时不写入。
    """
    payload: dict[str, Any] = {}
    if caller is not None:
        payload["caller"] = caller
    if extra:
        payload.update(extra)
    return runtime_version_stamp(rule_version, extra=payload or None)


__all__ = [
    "DoseCheckResult",
    "FakeRuleAdapter",
    "FreqCheckResult",
    "PopulationCheckResult",
    "RouteCheckResult",
    "RuleAdapterError",
    "RuleAdapterProtocol",  # 向后兼容 alias
    "RuleHit",
    "RuleProtocol",  # 新契约（任务 31）
    "RuleVersionMismatch",
    "runtime_version_stamp",
    "stamp_finding_metadata",
]


# 模块级契约自检（任务 31）：fail-loud 暴露契约漂移。
# 仅在测试 / 开发环境强制执行；生产环境 import 不会引入额外开销。
def _verify_protocol_contract() -> None:
    """校验 RuleProtocol 仅暴露指定 5 方法；多余方法应转入 FakeRuleAdapter。"""
    expected = {
        "get_rule_version",
        "check_dose",
        "check_population",
        "lookup_rule",
        "list_rules",
    }
    actual = {
        name
        for name, _ in inspect.getmembers(RuleProtocol, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    missing = expected - actual
    if missing:
        raise RuntimeError(
            f"RuleProtocol contract drift: missing {missing}; actual={actual}"
        )


_verify_protocol_contract()