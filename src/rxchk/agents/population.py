"""population 字段族 Agent：核对特殊人群（孕产 / 儿童 / 肝肾）禁忌 / 调整。

调用接口：
- `rule_adapter.check_population(drug_code, patient_flags)` → `PopulationCheckResult`
  内含 0..N 条 `RuleHit`（每条对应一类人群）

患者人群标签来源：
- 由 `FieldAgentContext.patient_flags` 提供；当前 `Prescription` 数据模型
  未直接携带，调用方（任务 14 的 `prescription_checker`）从 HIS 患者
  档案派生。
- 常见 flags：`pregnant` / `lactating` / `pediatric` / `geriatric` /
  `renal_impairment` / `hepatic_impairment`
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
from rxchk.tools.rule_adapter import RuleAdapterProtocol

logger = logging.getLogger(__name__)


#: 院内人群标签白名单（与 population_rules.yaml 一致）
RECOGNIZED_FLAGS: frozenset[str] = frozenset(
    {
        "pregnant",
        "lactating",
        "pediatric",
        "geriatric",
        "renal_impairment",
        "hepatic_impairment",
    }
)


class PopulationAgent(FieldAgent):
    """特殊人群字段族 Agent。"""

    family = FieldFamily.POPULATION

    def __init__(
        self,
        rule_adapter: RuleAdapterProtocol,
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

    def _check(self, ctx: FieldAgentContext) -> Iterable[Finding]:
        rx_no = ctx.prescription.prescription_no
        agent_id = ctx.resolved_agent_id(self.family)
        findings: list[Finding] = []

        flags = ctx.patient_flags
        if not flags:
            # 无人群标签 → 跳过；不伪命中
            return findings

        # 未识别的 flag 也透传给规则适配器（替身会忽略），便于未来扩展；
        # 此处只做白名单校验日志。
        unknown = [f for f in flags if f not in RECOGNIZED_FLAGS]
        if unknown:
            logger.info(
                "population_agent unknown_flags=%s rx=%s",
                unknown,
                rx_no,
            )

        for idx, item in enumerate(ctx.prescription.items):
            result = self._rule_adapter.check_population(item.drug_code, flags)
            for hit in result.hits:
                findings.append(
                    build_finding_from_hit(
                        prescription_no=rx_no,
                        field_name=f"items[{idx}].drug_code",
                        field_value=item.drug_code,
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
        return findings


__all__ = ["PopulationAgent", "RECOGNIZED_FLAGS"]
