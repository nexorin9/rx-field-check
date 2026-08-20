"""indication 字段族 Agent：核对诊断 ↔ 药品适应症匹配。

调用接口：
- `drug_dict.indications_for(drug_code)` → tuple[ICD ...]
- 处方 `diagnoses[].code` 提供患者主诊断 ICD 列表

命中规则（确定）：
- 处方药品的所有适应症 ICD 与任一诊断 ICD 无交集 → 命中
  （severity=medium，规则 ID `R-IND-NO-OVERLAP`，提示「适应症不符」）
- 药品字典未声明该药的适应症 → 命中（severity=low，规则 ID
  `R-IND-NO-RECORD`，提示「字典缺项」）
- 处方无任何诊断 → 跳过（无法判断；不伪命中）
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

logger = logging.getLogger(__name__)


RULE_IND_NO_OVERLAP = "R-IND-NO-OVERLAP"
RULE_IND_NO_RECORD = "R-IND-NO-RECORD"


class IndicationAgent(FieldAgent):
    """适应症字段族 Agent。"""

    family = FieldFamily.INDICATION

    def __init__(
        self,
        drug_dict: DrugDictProtocol,
        *,
        rule_version: str = "",
        agent_id: Optional[str] = None,
        source: str = "rule_engine",
    ) -> None:
        super().__init__(
            rule_version=rule_version or drug_dict.get_dict_version(),
            agent_id=agent_id,
            source=source,
        )
        self._drug_dict = drug_dict

    def _check(self, ctx: FieldAgentContext) -> Iterable[Finding]:
        rx_no = ctx.prescription.prescription_no
        agent_id = ctx.resolved_agent_id(self.family)
        diagnoses = ctx.prescription.diagnoses
        findings: list[Finding] = []

        if not diagnoses:
            # 无诊断 → 跳过；后续接入患者档案时再覆盖
            return findings

        diag_codes: set[str] = {d.code.strip().upper() for d in diagnoses}

        for idx, item in enumerate(ctx.prescription.items):
            inds = self._drug_dict.indications_for(item.drug_code)
            if not inds:
                findings.append(
                    build_finding_from_hit(
                        prescription_no=rx_no,
                        field_name=f"items[{idx}].drug_code",
                        field_value=item.drug_code,
                        family=self.family,
                        rule_id=RULE_IND_NO_RECORD,
                        rule_version=self.rule_version,
                        hit_explanation=(
                            f"药品字典未声明 {item.drug_code} 的适应症 ICD，无法核对"
                        ),
                        evidence=f"<DRUG_DICT:{item.drug_code}.indications>",
                        severity="low",
                        source=self.source,
                        agent_id=agent_id,
                    )
                )
                continue

            ind_set = {ic.strip().upper() for ic in inds}
            if diag_codes.isdisjoint(ind_set):
                findings.append(
                    build_finding_from_hit(
                        prescription_no=rx_no,
                        field_name=f"items[{idx}].drug_code",
                        field_value=item.drug_code,
                        family=self.family,
                        rule_id=RULE_IND_NO_OVERLAP,
                        rule_version=self.rule_version,
                        hit_explanation=(
                            f"药品 {item.drug_code} 适应症 {sorted(ind_set)!r} "
                            f"与诊断 {sorted(diag_codes)!r} 无交集，疑似超说明书用药"
                        ),
                        evidence=f"<DRUG_DICT:{item.drug_code}.indications>",
                        severity="medium",
                        source=self.source,
                        agent_id=agent_id,
                    )
                )

        return findings


__all__ = ["IndicationAgent", "RULE_IND_NO_OVERLAP", "RULE_IND_NO_RECORD"]
