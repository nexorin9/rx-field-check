"""字段族 Agent：一张处方 → 8 个字段核对子 Agent。

设计目标
--------
- 每个 FieldAgent 负责一个字段族（drug / dose / frequency / route /
  indication / allergy / interaction / population），独立调自家族
  适配器（`drug_dict` / `rule_adapter` / `interaction_db` /
  `allergy_db`），产出 `Finding` 列表。
- 完成报告参考源 `_render_completion_report`：
  ``{findings: [...], rule_versions: [...], success, family, agent_id}``；
  报告不直接落盘，由 `report.writer`（任务 8）统一写到
  `runs/<rx>/transcript.jsonl`。
- `FieldAgent` 异常由 `coordinator.supervise` 兜住 → 单族失败不阻塞
  兄弟族。

源 repo 名 / slug 不出现在产品代码或字符串字面值。
"""

from rxchk.agents.allergy import AllergyAgent
from rxchk.agents.base import (
    FieldAgent,
    FieldAgentContext,
    build_finding_from_hit,
    completion_report,
)
from rxchk.agents.dose import DoseAgent
from rxchk.agents.drug import DrugAgent
from rxchk.agents.frequency import FrequencyAgent
from rxchk.agents.indication import IndicationAgent
from rxchk.agents.interaction import InteractionAgent
from rxchk.agents.population import PopulationAgent
from rxchk.agents.route import RouteAgent

__all__ = [
    # base
    "FieldAgent",
    "FieldAgentContext",
    "build_finding_from_hit",
    "completion_report",
    # 8 字段族 Agent
    "AllergyAgent",
    "DoseAgent",
    "DrugAgent",
    "FrequencyAgent",
    "IndicationAgent",
    "InteractionAgent",
    "PopulationAgent",
    "RouteAgent",
]
