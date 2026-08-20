"""allergy 字段族 Agent：核对患者过敏史 ↔ 处方药品冲突。

调用接口：
- `allergy_db.check(drug_code, allergies)` → list[AllergyHit]
  内含 0..N 条 `AllergyHit`（直接命中 + 家族交叉）

命中规则（确定）：
- 直接命中（cross_reactivity=False）→ 高危
  - 严重度：mild→low / moderate→medium / severe→high / contraindicated→high
- 家族交叉（cross_reactivity=True）→ 提示类（severity 由过敏库降一档决定）
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
from rxchk.tools.allergy_db import (
    AllergyDbProtocol,
    SEVERITY_TO_FINDING,
)

logger = logging.getLogger(__name__)


#: 规则 ID 前缀（与 allergy_db.check 内部生成一致）
RULE_PREFIX = "R-ALG"


class AllergyAgent(FieldAgent):
    """过敏字段族 Agent。"""

    family = FieldFamily.ALLERGY

    def __init__(
        self,
        allergy_db: AllergyDbProtocol,
        *,
        rule_version: str = "",
        agent_id: Optional[str] = None,
        source: str = "rule_engine",
    ) -> None:
        super().__init__(
            rule_version=rule_version or allergy_db.get_db_version(),
            agent_id=agent_id,
            source=source,
        )
        self._allergy_db = allergy_db

    def _check(self, ctx: FieldAgentContext) -> Iterable[Finding]:
        rx_no = ctx.prescription.prescription_no
        agent_id = ctx.resolved_agent_id(self.family)
        allergies = ctx.prescription.allergies
        findings: list[Finding] = []

        if not allergies:
            # 无过敏史 → 跳过；不伪命中
            return findings

        for idx, item in enumerate(ctx.prescription.items):
            hits = self._allergy_db.check(item.drug_code, allergies)
            for hit in hits:
                severity = SEVERITY_TO_FINDING[hit.severity]
                kind = "家族交叉" if hit.cross_reactivity else "直接命中"
                findings.append(
                    build_finding_from_hit(
                        prescription_no=rx_no,
                        field_name=f"items[{idx}].drug_code",
                        field_value=item.drug_code,
                        family=self.family,
                        rule_id=hit.rule_id,
                        rule_version=hit.evidence.split(":", 1)[-1].rstrip(">")
                        or self.rule_version,
                        hit_explanation=(
                            f"过敏冲突（{kind}）：{hit.allergen} ↔ {item.drug_code}；"
                            f"{hit.clinical_note}"
                        ),
                        evidence=hit.evidence,
                        severity=severity,
                        source=self.source,
                        agent_id=agent_id,
                    )
                )
        return findings


__all__ = ["AllergyAgent", "RULE_PREFIX"]
