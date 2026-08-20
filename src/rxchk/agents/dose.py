"""dose 字段族 Agent：核对单次 / 日剂量上下限。

调用接口：
- `rule_adapter.check_dose(drug_code, dose, route)` → `DoseCheckResult`
  内含 0..N 条 `RuleHit`（每条对应一类超限）
- `drug_dict.max_single_dose(drug_code, route)` → `Optional[float]`
  主数据单次上限；无 per_route 退到药品级
- `drug_dict.max_daily_dose(drug_code, route)` → `Optional[float]`
  主数据日剂量上限；无 per_route 退到药品级

命中规则（确定）：
- 解析失败（`parsed_dose is None`）→ 已有 low 级 RuleHit，透传
- 超单次 / 日剂量 → 命中（severity 透传规则定义）
- `drug_dict` 安全冗余：即使规则库无该药规则，只要药品字典给出上限，
  仍然命中并打 `R-DOSE-DICT-*` 规则 ID —— 与规则适配器命中解耦，
  避免「规则升级空窗」漏掉致命超剂量。
- **不重复命中**：规则适配器已出 -SINGLE 时，drug_dict 的单次上限
  检查跳过；同样 -DAILY 跳过 drug_dict 的日剂量检查。
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


#: drug_dict 冗余命中 - 单次超限规则 ID
RULE_DICT_SINGLE = "R-DOSE-DICT-SINGLE"
#: drug_dict 冗余命中 - 单次超过日剂量上限（临床必警示）
RULE_DICT_OVER_DAILY = "R-DOSE-DICT-OVER-DAILY"


class DoseAgent(FieldAgent):
    """剂量字段族 Agent：每条 item 调 rule_adapter + drug_dict 双源核对。

    双源动机：
    - `rule_adapter` 反映本院剂量规则的当前启用集，可能随版本漂移
    - `drug_dict` 是药品主数据，是更稳定的安全冗余
    两者并行核对：即使规则升级空窗或规则集漏药，药品字典给出的硬上限
    仍可阻断致命超剂量（如「单次给到日剂量上限的 2 倍」这种灾难）。
    """

    family = FieldFamily.DOSE

    def __init__(
        self,
        rule_adapter: RuleAdapterProtocol,
        drug_dict: DrugDictProtocol,
        *,
        rule_version: str = "",
        agent_id: Optional[str] = None,
        source: str = "rule_engine",
    ) -> None:
        super().__init__(
            rule_version=rule_version or rule_adapter.get_rule_version(),
            agent_id=agent_id,
            source=source,
        )
        self._rule_adapter = rule_adapter
        self._drug_dict = drug_dict

    def _check(self, ctx: FieldAgentContext) -> Iterable[Finding]:
        rx_no = ctx.prescription.prescription_no
        agent_id = ctx.resolved_agent_id(self.family)
        findings: list[Finding] = []

        for idx, item in enumerate(ctx.prescription.items):
            # ---- 1) 规则适配器：命中其配置的剂量规则 ----
            result = self._rule_adapter.check_dose(
                item.drug_code, item.dose, item.route
            )
            for hit in result.hits:
                findings.append(
                    build_finding_from_hit(
                        prescription_no=rx_no,
                        field_name=f"items[{idx}].dose",
                        field_value=item.dose,
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

            # ---- 2) drug_dict 安全冗余 ----
            if result.parsed_dose is None:
                # 解析失败 / 频次伪剂量 → drug_dict 也无从对比，跳过
                continue
            parsed_dose_g = result.parsed_dose
            parsed_unit = result.parsed_unit
            if parsed_unit in {"mg", "ug", "mcg"}:
                # rule_adapter 已归一为 g；此处对齐即可
                if parsed_unit == "mg":
                    parsed_dose_g = result.parsed_dose / 1000.0
                else:
                    parsed_dose_g = result.parsed_dose / 1_000_000.0

            rule_ids_emitted = {hit.rule_id for hit in result.hits}
            rule_has_single = any(rid.endswith("-SINGLE") for rid in rule_ids_emitted)
            rule_has_daily = any(rid.endswith("-DAILY") for rid in rule_ids_emitted)

            # 2a) drug_dict 单次上限（per-route 优先 → 药品级 fallback）
            if not rule_has_single:
                max_single = self._drug_dict.max_single_dose(
                    item.drug_code, item.route
                )
                if max_single is not None and parsed_dose_g > max_single:
                    findings.append(
                        build_finding_from_hit(
                            prescription_no=rx_no,
                            field_name=f"items[{idx}].dose",
                            field_value=item.dose,
                            family=self.family,
                            rule_id=RULE_DICT_SINGLE,
                            rule_version=self.rule_version,
                            hit_explanation=(
                                f"药品字典单次上限 {max_single}g（{item.route}）；"
                                f"实际单次 {parsed_dose_g:g}g"
                            ),
                            evidence=(
                                f"<DRUG_DICT:{item.drug_code}.max_single_dose_g/"
                                f"{item.route}>"
                            ),
                            severity="high",
                            source=self.source,
                            agent_id=agent_id,
                        )
                    )

            # 2b) drug_dict 日剂量上限（per-route 优先 → 药品级 fallback）
            #     「单次已经超过日剂量上限」是临床必警示的极端情况：即便
            #     规则适配器 -DAILY 已命中，仍额外打一条带不同 rule_id 的
            #     命中，便于审计员一眼区分规则命中与药品字典硬上限命中。
            max_daily = self._drug_dict.max_daily_dose(item.drug_code, item.route)
            if max_daily is not None and parsed_dose_g > max_daily:
                findings.append(
                    build_finding_from_hit(
                        prescription_no=rx_no,
                        field_name=f"items[{idx}].dose",
                        field_value=item.dose,
                        family=self.family,
                        rule_id=RULE_DICT_OVER_DAILY,
                        rule_version=self.rule_version,
                        hit_explanation=(
                            f"单次剂量 {parsed_dose_g:g}g 已超过药品字典"
                            f"日剂量上限 {max_daily}g（{item.route}）——"
                            f"临床必警示，立即复核"
                        ),
                        evidence=(
                            f"<DRUG_DICT:{item.drug_code}.max_daily_dose_g/"
                            f"{item.route}>"
                        ),
                        severity="high",
                        source=self.source,
                        agent_id=agent_id,
                    )
                )

        return findings


__all__ = [
    "DoseAgent",
    "RULE_DICT_OVER_DAILY",
    "RULE_DICT_SINGLE",
]