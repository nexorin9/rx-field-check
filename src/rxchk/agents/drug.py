"""drug 字段族 Agent：核对药品编码 / 药名合法性。

调用接口：
- `drug_dict.check_name(drug_code)` → bool（药品字典中存在该编码）
- `drug_dict.check_encode(drug_code)` → bool（编码字符 / 长度合法）

命中规则（确定）：
- 编码不合法（`check_encode=False`）→ 命中（severity=high，规则 ID
  `R-DRUG-ENCODE`）
- 字典不存在该编码（`check_name=False`）→ 命中（severity=high，规则 ID
  `R-DRUG-NAME`）
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


#: 编码非法规则 ID（供 test 引用）
RULE_ENCODE_INVALID = "R-DRUG-ENCODE"
#: 字典查无此编码规则 ID
RULE_NAME_NOT_FOUND = "R-DRUG-NAME"


class DrugAgent(FieldAgent):
    """药品字段族 Agent：每条 item 调 drug_dict 查编码 / 药名。"""

    family = FieldFamily.DRUG

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

    # ------------------------------------------------------------------ #
    # 核对                                                                #
    # ------------------------------------------------------------------ #

    def _check(self, ctx: FieldAgentContext) -> Iterable[Finding]:
        rx_no = ctx.prescription.prescription_no
        agent_id = ctx.resolved_agent_id(self.family)
        findings: list[Finding] = []

        for idx, item in enumerate(ctx.prescription.items):
            # 1. 编码合法性：长度 / 字符 / 大小写
            if not self._drug_dict.check_encode(item.drug_code):
                findings.append(
                    build_finding_from_hit(
                        prescription_no=rx_no,
                        field_name=f"items[{idx}].drug_code",
                        field_value=item.drug_code,
                        family=self.family,
                        rule_id=RULE_ENCODE_INVALID,
                        rule_version=self.rule_version,
                        hit_explanation="药品编码不合法（长度或字符不在 [A-Z0-9_-]）",
                        evidence=f"<DRUG_DICT:encode_check/{item.drug_code}>",
                        severity="high",
                        source=self.source,
                        agent_id=agent_id,
                    )
                )
                # 编码不合法后续查字典无意义，直接跳过
                continue

            # 2. 字典查不到：缺主数据
            if not self._drug_dict.check_name(item.drug_code):
                findings.append(
                    build_finding_from_hit(
                        prescription_no=rx_no,
                        field_name=f"items[{idx}].drug_code",
                        field_value=item.drug_code,
                        family=self.family,
                        rule_id=RULE_NAME_NOT_FOUND,
                        rule_version=self.rule_version,
                        hit_explanation="药品字典未收录该编码，疑似字典缺项或编码错录",
                        evidence=f"<DRUG_DICT:lookup/{item.drug_code}>",
                        severity="high",
                        source=self.source,
                        agent_id=agent_id,
                    )
                )

        return findings


__all__ = ["DrugAgent", "RULE_ENCODE_INVALID", "RULE_NAME_NOT_FOUND"]
