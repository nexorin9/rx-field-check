"""route 字段族 Agent：核对给药途径与药品字典一致性。

调用接口：
- `drug_dict.route_form_match(drug_code, route)` → bool
  （route 是否在该药品的允许途径列表内）
- `rule_adapter.check_route(drug_code, route)` → 规则适配器命中
  （白名单 / 黑名单 / 剂型一致性）

双源核对（任务 21 拆出 + 深化）
----------------------------------
- `drug_dict.route_form_match` 给出药品主数据的允许途径（含 forms → 剂型
  → 途径的一致性判断），是稳定的安全冗余；
- `rule_adapter.check_route` 反映本院当前规则集（白名单 / 黑名单），
  例如「DRUG_AMOX 仅 po」「DRUG_WAR 严禁 iv」等强约束。
- 两者并行：制度层规则升级空窗时字典仍可阻断致命途径误录；
  反之字典缺药时规则层仍按本院强约束阻断。

命中规则（确定）：
- 字典允许途径不含处方 route → 命中（severity=high，规则 ID
  `R-ROUTE-NOT-ALLOWED`）
- 剂型与途径不匹配（如 po 的药改成 iv 注射）→ 命中（severity=high）
- 途径字符串不在白名单 → 命中（severity=low，规则 ID `R-ROUTE-UNREC`）
- 制度层频次白名单/黑名单 → 命中（severity 来自规则）
- 给药途径变更警告：原 po 药品改成 iv 注射 → 命中（severity=high，
  规则 ID `R-ROUTE-CHANGE-WARN`）

注：本轮剂型（capsule / tablet / injection 等）由药品字典的 `forms`
字段控制；字典未单列「剂型 vs 途径」映射，故剂型不匹配走「途径不允许」
的同一路径，但 `hit_explanation` 区分语义。
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from rxchk.agents.base import (
    FieldAgent,
    FieldAgentContext,
    build_finding_from_hit,
)
from rxchk.models.prescription import FieldFamily, Finding
from rxchk.tools.drug_dict import DrugDictProtocol
from rxchk.tools.rule_adapter import RuleAdapterProtocol

logger = logging.getLogger(__name__)


#: 途径不在字典允许列表
RULE_ROUTE_NOT_ALLOWED = "R-ROUTE-NOT-ALLOWED"
#: 途径字符串未在白名单
RULE_ROUTE_UNRECOGNIZED = "R-ROUTE-UNREC"
#: 剂型与途径不匹配（form 与 route 矛盾）
RULE_ROUTE_FORM_MISMATCH = "R-ROUTE-FORM-MISMATCH"
#: 给药途径变更警告（po → iv 等）
RULE_ROUTE_CHANGE_WARN = "R-ROUTE-CHANGE-WARN"


#: 院内给药途径白名单（与 drug_dict.yaml 的 routes 字段保持一致）
RECOGNIZED_ROUTES: frozenset[str] = frozenset({"po", "iv", "im", "ih", "sc", "pr"})


#: 「口服剂型 ↔ 注射途径」冲突表（字典里 forms=tablet/capsule 的药品
#: 一律不允许 iv/im；防止临床误录「po 的药改 iv」）
ORAL_FORMS: frozenset[str] = frozenset({"tablet", "capsule", "syrup"})
INJECTION_ROUTES: frozenset[str] = frozenset({"iv", "im", "sc", "ih"})


#: 给药途径变更警告对：原途径 → 新途径（注射类变更）
ROUTE_CHANGE_WARN_PAIRS: tuple[tuple[str, str], ...] = (
    ("po", "iv"),
    ("po", "im"),
    ("po", "sc"),
    ("po", "ih"),
    ("pr", "iv"),
    ("pr", "im"),
)


class RouteAgent(FieldAgent):
    """给药途径字段族 Agent：drug_dict 主数据 + rule_adapter 制度层双源核对。

    双源动机：
    - `drug_dict.route_form_match` + `forms` 是稳定的主数据层（含剂型 →
      途径一致性）；
    - `rule_adapter.check_route` 是动态的制度层（白名单 / 黑名单）；
    两者并行核对，避免「规则升级空窗」漏药或「字典缺药」漏规则。
    """

    family = FieldFamily.ROUTE

    def __init__(
        self,
        drug_dict: DrugDictProtocol,
        rule_adapter: RuleAdapterProtocol,
        *,
        rule_version: str = "",
        agent_id: Optional[str] = None,
        source: str = "rule_engine",
    ) -> None:
        super().__init__(
            rule_version=rule_version
            or rule_adapter.get_rule_version()
            or drug_dict.get_dict_version(),
            agent_id=agent_id,
            source=source,
        )
        self._drug_dict = drug_dict
        self._rule_adapter = rule_adapter

    def _check(self, ctx: FieldAgentContext) -> Iterable[Finding]:
        rx_no = ctx.prescription.prescription_no
        agent_id = ctx.resolved_agent_id(self.family)
        findings: list[Finding] = []

        for idx, item in enumerate(ctx.prescription.items):
            route = item.route.strip().lower()

            # 1. 途径白名单
            if route not in RECOGNIZED_ROUTES:
                findings.append(
                    build_finding_from_hit(
                        prescription_no=rx_no,
                        field_name=f"items[{idx}].route",
                        field_value=item.route,
                        family=self.family,
                        rule_id=RULE_ROUTE_UNRECOGNIZED,
                        rule_version=self.rule_version,
                        hit_explanation=(
                            f"给药途径 {item.route!r} 不在院内白名单"
                            f" ({sorted(RECOGNIZED_ROUTES)})"
                        ),
                        evidence="<ROUTE_WHITELIST>",
                        severity="low",
                        source=self.source,
                        agent_id=agent_id,
                    )
                )
                # 未识别途径后续字典匹配无意义
                continue

            # 2. 规则适配器：制度层途径核对（白名单 / 黑名单）
            route_result = self._rule_adapter.check_route(item.drug_code, route)
            rule_adapter_hits = {hit.rule_id for hit in route_result.hits}
            for hit in route_result.hits:
                findings.append(
                    build_finding_from_hit(
                        prescription_no=rx_no,
                        field_name=f"items[{idx}].route",
                        field_value=item.route,
                        family=self.family,
                        rule_id=hit.rule_id,
                        rule_version=hit.rule_version or self.rule_version,
                        hit_explanation=hit.hit_explanation,
                        evidence=hit.evidence,
                        severity=hit.severity,
                        source=self.source,
                        agent_id=agent_id,
                    )
                )

            # 3. 字典允许途径（主数据层）
            dict_allowed = self._drug_dict.route_form_match(item.drug_code, route)
            if not dict_allowed:
                findings.append(
                    build_finding_from_hit(
                        prescription_no=rx_no,
                        field_name=f"items[{idx}].route",
                        field_value=item.route,
                        family=self.family,
                        rule_id=RULE_ROUTE_NOT_ALLOWED,
                        rule_version=self.rule_version,
                        hit_explanation=(
                            f"药品字典未声明 {item.drug_code} 允许 {route!r} 途径"
                        ),
                        evidence=f"<DRUG_DICT:{item.drug_code}.routes>",
                        severity="high",
                        source=self.source,
                        agent_id=agent_id,
                    )
                )
                # 字典不允许该途径 → 剂型不匹配 / 途径变更警告已隐含，跳过
                continue

            # 4. 剂型 vs 途径一致性（口服剂型错改成注射）
            rec = self._drug_dict.lookup_drug(item.drug_code)
            if rec is not None and route in INJECTION_ROUTES:
                if any(form in ORAL_FORMS for form in rec.forms):
                    findings.append(
                        build_finding_from_hit(
                            prescription_no=rx_no,
                            field_name=f"items[{idx}].route",
                            field_value=item.route,
                            family=self.family,
                            rule_id=RULE_ROUTE_FORM_MISMATCH,
                            rule_version=self.rule_version,
                            hit_explanation=(
                                f"药品 {item.drug_code} 剂型为口服（"
                                f"{','.join(rec.forms)}），与 {route!r} 注射途径不一致"
                            ),
                            evidence=f"<DRUG_DICT:{item.drug_code}.forms>",
                            severity="high",
                            source=self.source,
                            agent_id=agent_id,
                        )
                    )

            # 5. 给药途径变更警告（po → iv 等注射类变更须复核）
            #    仅当字典主数据按口服剂型给出（如 forms 含 capsule/tablet），
            #    而处方改用注射途径时才告警；
            #    字典与规则层都允许该途径时（如 DRUG_PEN iv）不触发。
            if rec is not None and any(form in ORAL_FORMS for form in rec.forms):
                for src_route, dst_route in ROUTE_CHANGE_WARN_PAIRS:
                    if route == dst_route:
                        findings.append(
                            build_finding_from_hit(
                                prescription_no=rx_no,
                                field_name=f"items[{idx}].route",
                                field_value=item.route,
                                family=self.family,
                                rule_id=RULE_ROUTE_CHANGE_WARN,
                                rule_version=self.rule_version,
                                hit_explanation=(
                                    f"药品 {item.drug_code} 剂型为口服（"
                                    f"{','.join(rec.forms)}），处方改为 {route!r} 注射"
                                    f"（典型变更 {src_route}→{dst_route}）；"
                                    "须立即复核医嘱"
                                ),
                                evidence=(
                                    f"<ROUTE_CHANGE_WARN:{item.drug_code}/"
                                    f"{src_route}->{dst_route}>"
                                ),
                                severity="high",
                                source=self.source,
                                agent_id=agent_id,
                            )
                        )
                        # 一条警告足以；同途径不同 src 不重复
                        break

        return findings


__all__ = [
    "INJECTION_ROUTES",
    "ORAL_FORMS",
    "RECOGNIZED_ROUTES",
    "ROUTE_CHANGE_WARN_PAIRS",
    "RouteAgent",
    "RULE_ROUTE_CHANGE_WARN",
    "RULE_ROUTE_FORM_MISMATCH",
    "RULE_ROUTE_NOT_ALLOWED",
    "RULE_ROUTE_UNRECOGNIZED",
]