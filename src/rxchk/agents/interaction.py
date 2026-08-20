"""interaction 字段族 Agent：核对多药配伍冲突。

调用接口：
- `interaction_db.check(drug_codes)` → list[InteractionHit]
  内含 0..N 条 `InteractionHit`（双药 + 三药累加）

命中规则（确定）：
- 配伍命中 → 透传严重度：
  - contraindicated → high
  - major → high
  - moderate → medium
  - minor → low
- 配伍字段名约定：`interactions[药A+药B]`（按字典序排列）
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
from rxchk.tools.interaction_db import (
    InteractionDbProtocol,
    SEVERITY_TO_FINDING,
)

logger = logging.getLogger(__name__)


class InteractionAgent(FieldAgent):
    """配伍字段族 Agent。"""

    family = FieldFamily.INTERACTION

    def __init__(
        self,
        interaction_db: InteractionDbProtocol,
        *,
        rule_version: str = "",
        agent_id: Optional[str] = None,
        source: str = "rule_engine",
    ) -> None:
        super().__init__(
            rule_version=rule_version or interaction_db.get_db_version(),
            agent_id=agent_id,
            source=source,
        )
        self._interaction_db = interaction_db

    def _check(self, ctx: FieldAgentContext) -> Iterable[Finding]:
        rx_no = ctx.prescription.prescription_no
        agent_id = ctx.resolved_agent_id(self.family)
        findings: list[Finding] = []

        drug_codes = [item.drug_code for item in ctx.prescription.items]
        if len(drug_codes) < 2:
            # 单药处方不参与配伍核对
            return findings

        hits = self._interaction_db.check(drug_codes)
        for hit in hits:
            severity = SEVERITY_TO_FINDING[hit.severity]
            pair_label = f"{hit.drug_a}+{hit.drug_b}"
            findings.append(
                build_finding_from_hit(
                    prescription_no=rx_no,
                    field_name=f"interactions[{pair_label}]",
                    field_value=pair_label,
                    family=self.family,
                    rule_id=hit.rule_id,
                    rule_version=self.rule_version,
                    hit_explanation=(
                        f"配伍冲突（{hit.severity.value}）："
                        f"{hit.drug_a} ↔ {hit.drug_b}；{hit.clinical_note}"
                    ),
                    evidence=hit.evidence,
                    severity=severity,
                    source=self.source,
                    agent_id=agent_id,
                )
            )
        return findings


__all__ = ["InteractionAgent"]
