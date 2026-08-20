"""frequency 字段族 Agent：核对给药频次 / 给药时间窗。

调用接口：
- `drug_dict.frequency_default(drug_code)` → 字典默认频次
- `rule_adapter.check_freq(drug_code, frequency, route)` → 规则适配器
  命中（白名单 / 黑名单 / 给药时间窗冲突）

双源核对（任务 21 拆出 + 深化）
----------------------------------
- `drug_dict.frequency_default` 给出药品主数据的推荐频次，是更稳定的默认；
  当处方频次与字典不符时打 `R-FREQ-DEV` 提示（severity=low）。
- `rule_adapter.check_freq` 反映本院当前规则集（白名单 / 黑名单 / 给药
  时间窗冲突）；命中时打 `R-FREQ-001 / R-FREQ-002 / R-FREQ-004` 等带
  `R-FREQ-*` 前缀的规则 ID（severity 由规则决定）。
- 两者并行：规则适配器是制度层（动态调整），药品字典是主数据层（稳定）；
  规则升级空窗或字典缺项时另一层仍可命中。

命中规则（确定）：
- 处方频次 ≠ 药品字典默认频次 → 命中（severity=low，规则 ID `R-FREQ-DEV`）
- 频次为未识别字符串（如 `prn_extra`）→ 命中（severity=low，规则 ID
  `R-FREQ-UNREC`）
- 给药时间窗冲突：规则配置 `routes=po` + `forbidden_frequencies=[qn]`
  等时间窗场景命中（severity 来自规则）
- 频次 × 疗程超 1 年（极端值）→ 命中（severity=low，规则 ID
  `R-FREQ-DUR-OVERFLOW`）
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


#: 频次与字典默认不符规则 ID
RULE_FREQ_DEVIATION = "R-FREQ-DEV"
#: 频次字符串未在白名单
RULE_FREQ_UNRECOGNIZED = "R-FREQ-UNREC"
#: 频次 × 疗程超 1 年异常
RULE_FREQ_DURATION_OVERFLOW = "R-FREQ-DUR-OVERFLOW"


#: 常见频次白名单（与 rule_adapter._parse_dose 中的频次伪剂量一致）
RECOGNIZED_FREQUENCIES: frozenset[str] = frozenset(
    {"qd", "bid", "tid", "qid", "qn", "qod", "prn", "q4h", "q6h", "q8h", "q12h"}
)


#: 频次字符串 → 每日给药次数（用于 duration 推断）
FREQUENCY_PER_DAY: dict[str, int] = {
    "qd": 1,
    "bid": 2,
    "tid": 3,
    "qid": 4,
    "qn": 1,
    "qod": 1,  # 隔日按 0.5；保守取 1 上限
    "q4h": 6,
    "q6h": 4,
    "q8h": 3,
    "q12h": 2,
}


class FrequencyAgent(FieldAgent):
    """频次字段族 Agent：drug_dict 主数据 + rule_adapter 制度层双源核对。

    双源动机：
    - `drug_dict.frequency_default` 反映药品主数据（稳定可审计）；
    - `rule_adapter.check_freq` 反映本院当前频次规则（动态调整）；
    两者并行核对，规则升级空窗或字典缺项时另一层仍可命中。
    """

    family = FieldFamily.FREQUENCY

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
            freq = item.frequency.strip().lower()

            # 0. 频次字符串是否在白名单
            if freq not in RECOGNIZED_FREQUENCIES:
                findings.append(
                    build_finding_from_hit(
                        prescription_no=rx_no,
                        field_name=f"items[{idx}].frequency",
                        field_value=item.frequency,
                        family=self.family,
                        rule_id=RULE_FREQ_UNRECOGNIZED,
                        rule_version=self.rule_version,
                        hit_explanation=(
                            f"频次 {item.frequency!r} 不在白名单；"
                            "请确认医师意图或联系药事"
                        ),
                        evidence="<FREQ_WHITELIST>",
                        severity="low",
                        source=self.source,
                        agent_id=agent_id,
                    )
                )
                # 未识别频次不再做后续对照
                continue

            # 1. 规则适配器：制度层频次核对（白名单 / 黑名单 / 时间窗冲突）
            freq_result = self._rule_adapter.check_freq(
                item.drug_code, freq, item.route
            )
            for hit in freq_result.hits:
                findings.append(
                    build_finding_from_hit(
                        prescription_no=rx_no,
                        field_name=f"items[{idx}].frequency",
                        field_value=item.frequency,
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

            # 2. 频次 vs 字典默认（主数据层）
            default = self._drug_dict.frequency_default(item.drug_code)
            if default and default.strip().lower() != freq:
                findings.append(
                    build_finding_from_hit(
                        prescription_no=rx_no,
                        field_name=f"items[{idx}].frequency",
                        field_value=item.frequency,
                        family=self.family,
                        rule_id=RULE_FREQ_DEVIATION,
                        rule_version=self.rule_version,
                        hit_explanation=(
                            f"频次 {item.frequency!r} 与药品字典默认 {default!r} 不符"
                        ),
                        evidence=f"<DRUG_DICT:{item.drug_code}.default_frequency>",
                        severity="low",
                        source=self.source,
                        agent_id=agent_id,
                    )
                )

            # 3. 频次 × 疗程 超 1 年（极端值）
            if item.duration_days is not None and item.duration_days > 0:
                per_day = FREQUENCY_PER_DAY.get(freq, 1)
                if item.duration_days * per_day > 365:
                    findings.append(
                        build_finding_from_hit(
                            prescription_no=rx_no,
                            field_name=f"items[{idx}].duration_days",
                            field_value=str(item.duration_days),
                            family=self.family,
                            rule_id=RULE_FREQ_DURATION_OVERFLOW,
                            rule_version=self.rule_version,
                            hit_explanation=(
                                f"疗程 {item.duration_days}d × 每日 {per_day} 次"
                                f" = {item.duration_days * per_day} 次，"
                                "已超 365 次/年阈值，请人工复核"
                            ),
                            evidence=(
                                f"<FREQ_DURATION:"
                                f"{item.drug_code}/{freq}/{item.duration_days}>"
                            ),
                            severity="low",
                            source=self.source,
                            agent_id=agent_id,
                        )
                    )

        return findings


__all__ = [
    "FREQUENCY_PER_DAY",
    "FrequencyAgent",
    "RECOGNIZED_FREQUENCIES",
    "RULE_FREQ_DEVIATION",
    "RULE_FREQ_DURATION_OVERFLOW",
    "RULE_FREQ_UNRECOGNIZED",
]