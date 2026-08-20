"""字段族 Agent 抽象基类 + 完成报告构造。

参考源（github_ref 代码证据）：
- `strix/tools/agents_graph/tools.py: _render_completion_report`
  字段 Agent 完成时输出 ``{findings:[], rule_versions:[], success}``
- `strix/agents/prompt.py: render_system_prompt` 模板化思路
  （family / agent_id / rule_version 注入字段 Agent 上下文）

融合取舍
--------
- 源是 LLM Agent，需要 `session / stream / pending_counts` 等消息原语；
  本项目是确定性规则核对（`ai_role=none`），所以：
  - `_check()` 是同步计算（仍是 IO 但无 LLM），不靠会话流
  - `run()` 仍标 `async` 以便与 `coordinator.supervise` 协程接口对齐
  - `completion_report()` 仅返回 dict 供 `report.writer` 落盘
- 源 `_render_completion_report` 把 `recommendations[]` 也算入；
  本项目不输出诊疗建议（全局安全边界），只输出字段级 `findings[]`。
- 异常由基类透传给 `coordinator.supervise`，不私自吞错。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from rxchk.coordinator import field_agent_id
from rxchk.models.prescription import (
    FieldFamily,
    Finding,
    Prescription,
    UNCOVERED_FAMILY,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 上下文                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FieldAgentContext:
    """字段族 Agent 一次性核对所需的全部上下文。

    字段：
    - `prescription`：被核对处方
    - `patient_flags`：患者特殊人群标签（pregnant / pediatric /
      renal_impairment / hepatic_impairment 等）。当前 `Prescription`
      数据模型不直接携带，由调用方（`prescription_checker`，任务 14）
      从 HIS 患者档案派生；测试可显式注入。
    - `rule_version`：规则库版本号（与 `Finding.rule_version` 对齐）
    - `agent_id`：与 `coordinator.statuses` 的 key 一致
      （`field_agent_id(family)` 形式）；缺省由 family 派生
    - `source`：finding.source 字段（默认 `rule_engine`）
    """

    prescription: Prescription
    patient_flags: tuple[str, ...] = ()
    rule_version: str = ""
    agent_id: str = ""
    source: str = "rule_engine"

    def resolved_agent_id(self, family: FieldFamily) -> str:
        return self.agent_id or field_agent_id(family)


# --------------------------------------------------------------------------- #
# 工具函数                                                                    #
# --------------------------------------------------------------------------- #


def build_finding_from_hit(
    *,
    prescription_no: str,
    field_name: str,
    field_value: str,
    family: FieldFamily,
    rule_id: str,
    rule_version: str,
    hit_explanation: str,
    evidence: str,
    severity: str,
    source: str,
    agent_id: str,
) -> Finding:
    """把规则命中收敛为 Finding（spec.md 输出 schema 字段一一对应）。"""
    return Finding(
        prescription_no=prescription_no,
        field=field_name,
        field_value=field_value,
        family=family.value,
        rule_id=rule_id,
        rule_version=rule_version,
        hit_explanation=hit_explanation,
        evidence=evidence,
        severity=severity,
        source=source,
        agent_id=agent_id,
    )


def completion_report(
    *,
    family: FieldFamily,
    agent_id: str,
    findings: Iterable[Finding],
    rule_version: str = "",
    success: bool = True,
) -> dict[str, Any]:
    """构造完成报告 dict（参考源 `_render_completion_report`）。

    字段：
    - `family` / `agent_id`：节点标识
    - `findings`：本次核对产出的字段证据卡
    - `rule_versions`：用到的规则 / 字典 / 配伍 / 过敏库版本号列表
      （去重保序）
    - `success`：本次核对正常完成（未抛异常）；False 由调用方显式设置
    - `emitted_at`：报告生成时间（ISO 8601 UTC，秒级）
    """
    findings_list = list(findings)
    versions: list[str] = []
    seen: set[str] = set()
    if rule_version:
        versions.append(rule_version)
        seen.add(rule_version)
    for f in findings_list:
        if f.rule_version and f.rule_version not in seen:
            seen.add(f.rule_version)
            versions.append(f.rule_version)
    return {
        "family": family.value,
        "agent_id": agent_id,
        "findings": [f.to_dict() for f in findings_list],
        "rule_versions": versions,
        "success": success,
        "emitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def make_uncovered_finding(
    *,
    prescription_no: str,
    family: FieldFamily,
    field_name: str,
    field_value: str,
    agent_id: str,
    rule_version: str = "",
    source: str = "rule_engine",
) -> Finding:
    """构造一条降级 finding（family=uncovered，severity=info）。

    当某字段族的规则 / 字典库未载入或全部不可用时使用；不伪通过，标记
    「该族规则未启用」由 Viewer 黄底展示，药师可手动放行（带
    manual_override 标记）。
    """
    return Finding(
        prescription_no=prescription_no,
        field=field_name,
        field_value=field_value,
        family=UNCOVERED_FAMILY,
        rule_id="R-UNCOVERED",
        rule_version=rule_version or "unknown",
        hit_explanation="该字段族规则未启用或工具未注册；按 fallback 渲染",
        evidence="<UNCOVERED>",
        severity="info",
        source=source,
        agent_id=agent_id,
    )


# --------------------------------------------------------------------------- #
# 抽象基类                                                                    #
# --------------------------------------------------------------------------- #


class FieldAgent(ABC):
    """字段族 Agent 抽象基类。

    派生类须实现：
    - `family`：类属性（`FieldFamily`）
    - `_check(ctx)`：调自家族适配器返回 Finding 序列

    `run(ctx)` 是给 `coordinator.supervise` 调用的统一入口，内部走
    `_check` 并负责异常透传（不私自吞错，让协调器做状态机收敛）。
    """

    family: FieldFamily  # 派生类必须覆盖

    def __init__(
        self,
        *,
        rule_version: str = "",
        agent_id: Optional[str] = None,
        source: str = "rule_engine",
    ) -> None:
        if not isinstance(self.family, FieldFamily):
            raise TypeError(
                f"{type(self).__name__}.family must be a FieldFamily, "
                f"got {type(self.family).__name__}"
            )
        self.rule_version = rule_version
        self._agent_id_override = agent_id
        self.source = source

    # ------------------------------------------------------------------ #
    # 标识 / 完成报告                                                     #
    # ------------------------------------------------------------------ #

    @property
    def agent_id(self) -> str:
        if self._agent_id_override:
            return self._agent_id_override
        return field_agent_id(self.family)

    def render_completion_report(
        self, findings: Iterable[Finding], *, success: bool = True
    ) -> dict[str, Any]:
        return completion_report(
            family=self.family,
            agent_id=self.agent_id,
            findings=findings,
            rule_version=self.rule_version,
            success=success,
        )

    # ------------------------------------------------------------------ #
    # 主入口（与 coordinator.supervise 协程接口对齐）                      #
    # ------------------------------------------------------------------ #

    async def run(self, ctx: FieldAgentContext) -> list[Finding]:
        """跑一次字段核对。

        行为约定：
        - `_check()` 抛异常 → 透传；由 `coordinator.supervise` 收敛成
          `failed` 状态
        - `_check()` 返回非 Finding 列表 → 抛 `TypeError`
        - 返回值是 `list[Finding]`，顺序与处方 items 顺序一致；跨条目
          的命中（配伍等）按发现顺序追加
        """
        try:
            raw = self._check(ctx)
        except Exception:
            logger.exception(
                "field_agent.failed family=%s agent_id=%s rx=%s",
                self.family.value,
                self.agent_id,
                ctx.prescription.prescription_no,
            )
            raise
        findings = list(raw)
        for f in findings:
            if not isinstance(f, Finding):
                raise TypeError(
                    f"{type(self).__name__}._check must yield Finding, "
                    f"got {type(f).__name__}"
                )
        return findings

    # ------------------------------------------------------------------ #
    # 派生类须实现                                                        #
    # ------------------------------------------------------------------ #

    @abstractmethod
    def _check(self, ctx: FieldAgentContext) -> Iterable[Finding]:
        """派生类实现：调自家族适配器 → Finding 列表。"""


# --------------------------------------------------------------------------- #
# 共用辅助：处方内 items 迭代（子类复用）                                       #
# --------------------------------------------------------------------------- #


def iter_items(ctx: FieldAgentContext) -> Iterable[tuple[int, Any]]:
    """`(index, PrescriptionItem)` 顺序迭代；子类复用此避免样板代码。"""
    return enumerate(ctx.prescription.items)


__all__ = [
    "FieldAgent",
    "FieldAgentContext",
    "build_finding_from_hit",
    "completion_report",
    "iter_items",
    "make_uncovered_finding",
]
