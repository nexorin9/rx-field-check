"""处方核对 product-level 主路径：解析 → 派生 8 字段 Agent 并发 → 落盘 → 聚合 findings。

参考源（github_ref 代码证据）
============================
- `strix/interface/cli.py: run_cli` —— `args.run_name → run_strix_scan`
  → 落盘 `runs/<run_name>/` 入口主路径。
- `strix/runtime/session_manager.py` —— 根 agent 注册 + 子 agent
  并发调度的入口契约（本项目的 `AgentCoordinator.register_root` +
  `register_field_agents` 即同款职责的字段核对版）。
- `strix/report/state.py: hydrate_from_run_dir` —— 落盘 run.json
  + hydrate 状态机；本项目用 `ReportWriter.transition` 推进状态。

融合取舍
========
- 源是 LLM 渗透扫描：本项目是确定性字段核对（`ai_role=none`），故不引入
  LLM 模型网关 / 工具调用 LLM 适配层；只搬**输入 → 派发 → 落盘 → 退出**
  入口主路径，不搬 session / stream / pending_counts 等会话原语。
- 源 `run_cli` 启动 Rich Live；本项目 `rxchk check` 是同步批处理（核对
  5s 内完成），不引入 TUI / Live spinner，只打印 finding 摘要即可。
- 源 `scan_config.run_name` 由 `generate_run_name()` 自动生成；本项目
  `rxchk check <rx>` 的入参就是 `prescription_no`（已经唯一），直接复用
  作为 runs 子目录名。
- 源 Agent 异常透传：保留同款——`coordinator.supervise` 兜底，单族
  `failed` 不阻塞其他族。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from rxchk.agents import (
    AllergyAgent,
    DoseAgent,
    DrugAgent,
    FrequencyAgent,
    IndicationAgent,
    InteractionAgent,
    PopulationAgent,
    RouteAgent,
)
from rxchk.agents.base import FieldAgent, FieldAgentContext
from rxchk.coordinator import (
    AgentCoordinator,
    ROOT_AGENT_ID,
    Status,
)
from rxchk.models.prescription import FieldFamily, Finding, Prescription
from rxchk.parsers.prescription import PrescriptionParseError, parse_prescription
from rxchk.report.state import RunState
from rxchk.report.writer import (
    EVENT_AGENT_FINISH,
    EVENT_INPUT_RECEIVED,
    EVENT_STATE_CHANGE,
    ReportWriter,
    TranscriptEvent,
)
from rxchk.tools.allergy_db import FakeAllergyDb
from rxchk.tools.drug_dict import FakeDrugDict
from rxchk.tools.interaction_db import FakeInteractionDb
from rxchk.tools.rule_adapter import FakeRuleAdapter

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 路径常量                                                                    #
# --------------------------------------------------------------------------- #


#: 默认 yaml 数据根目录（与包同级的 data/ 子目录）。
DEFAULT_DATA_ROOT: str = "data"

#: runs 落盘根目录（相对当前工作目录；CLI 默认 cwd，API 测试可用 tmp_path）。
DEFAULT_RUN_ROOT: str = "runs"

#: rxchk 包内 data/ 位置（在包内 copy 一份供 fallback）。
PACKAGE_DATA_DIRNAME: str = "data"

#: 同 runs/<rx_no>/ 目录同级的 inbox 文件投递目录（用 CLI 入口而非 watcher）。
INBOX_DIRNAME: str = "inbox"


# --------------------------------------------------------------------------- #
# 结果                                                                        #
# --------------------------------------------------------------------------- #


@dataclass
class CheckResult:
    """`rxchk check` 完成汇总（供 CLI 打印 / 测试断言）。"""

    prescription_no: str
    run_dir: Path
    findings: list[Finding] = field(default_factory=list)
    agent_summary: dict[str, int] = field(default_factory=dict)
    failed_agents: dict[str, str] = field(default_factory=dict)
    graph: list[dict[str, Any]] = field(default_factory=list)
    status: str = RunState.AWAITING_REVIEW.value
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    finished_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def finding_count(self) -> int:
        return len(self.findings)

    @property
    def by_family(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.family] = counts.get(f.family, 0) + 1
        return counts

    def summary_lines(self) -> list[str]:
        """CLI 友好的多行摘要（不含 ANSI 颜色，避免 CI / 测试脆弱）。"""
        lines: list[str] = []
        lines.append(
            f"[rxchk] 处方 {self.prescription_no} 核对完成 "
            f"(state={self.status}, findings={self.finding_count})"
        )
        lines.append(f"[rxchk] run_dir: {self.run_dir}")
        if self.by_family:
            fam_str = ", ".join(
                f"{fam}={cnt}" for fam, cnt in sorted(self.by_family.items())
            )
            lines.append(f"[rxchk] by_family: {fam_str}")
        if self.failed_agents:
            failed_str = ", ".join(
                f"{aid}({err})" for aid, err in sorted(self.failed_agents.items())
            )
            lines.append(f"[rxchk] failed_agents: {failed_str}")
        return lines


# --------------------------------------------------------------------------- #
# 数据 / 适配器路径                                                            #
# --------------------------------------------------------------------------- #


def package_data_root() -> Path:
    """包内 data/ 目录绝对路径（`rxchk/data`）。

    用于在测试 / 非 cwd 场景下也能找到默认 yaml。
    """
    return Path(__file__).resolve().parent / PACKAGE_DATA_DIRNAME


def resolve_data_root(data_root: Optional[str | Path]) -> Path:
    """解析数据根目录（绝对路径或相对 cwd；不存在则 fallback 到包内 data）。"""
    if data_root is None:
        candidate = Path.cwd() / DEFAULT_DATA_ROOT
        if candidate.exists():
            return candidate.resolve()
        return package_data_root().resolve()
    candidate = Path(data_root)
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    if not candidate.exists():
        # 兜底：用户给了不存在的路径，仍尝试把包内 data 当成只读默认
        fallback = package_data_root().resolve()
        logger.warning(
            "data_root %s does not exist; falling back to package data %s",
            candidate,
            fallback,
        )
        return fallback
    return candidate


def default_paths(data_root: Optional[str | Path] = None) -> dict[str, Path]:
    """一组默认 yaml / rule 文件绝对路径（可供 `Fake*Adapter` 加载）。"""
    root = resolve_data_root(data_root)
    return {
        "drug_dict": root / "drug_dict.yaml",
        "interaction_db": root / "interaction_db.yaml",
        "allergy_db": root / "allergy_db.yaml",
        "dose_rules": root / "rule" / "dose_rules.yaml",
        "population_rules": root / "rule" / "population_rules.yaml",
        "freq_rules": root / "rule" / "freq_rules.yaml",
        "route_rules": root / "rule" / "route_rules.yaml",
    }


# --------------------------------------------------------------------------- #
# 适配器 / Agent 装配（一次实例化，多次复用）                                    #
# --------------------------------------------------------------------------- #


@dataclass
class CheckEngine:
    """`rxchk check` 所需的全部依赖（适配器 + Agent 字典）。

    由 `build_check_engine(data_root)` 构造；`run_check` 复用。

    字段：
    - `paths`：yaml 数据路径（供 `Fake*Adapter` 加载）
    - `drug_dict` / `interaction_db` / `allergy_db` / `rule_adapter`：
      适配器替身（实现对应 Protocol；正式对接时由 HIS / 院内引擎实例替换）
    - `agents`：8 字段族 Agent 字典（key = `FieldFamily.value`，value = Agent 实例）
    - `rule_version`：从 `rule_adapter.get_rule_version()` 取，用于注入
      finding / transcript

    设计动机：把构造与执行解耦——测试可替换任一适配器或 Agent，CLI 复用
    同一份构造。
    """

    paths: dict[str, Path]
    drug_dict: Any
    interaction_db: Any
    allergy_db: Any
    rule_adapter: Any
    agents: dict[str, FieldAgent] = field(default_factory=dict)
    rule_version: str = "unknown"

    def agent_for_family(self, family: FieldFamily | str) -> FieldAgent:
        value = family.value if isinstance(family, FieldFamily) else str(family)
        if value not in self.agents:
            raise KeyError(f"no agent registered for family={value!r}")
        return self.agents[value]


def build_check_engine(
    data_root: Optional[str | Path] = None,
) -> CheckEngine:
    """构造一组替身适配器 + 8 字段族 Agent（CLI / 测试共用入口）。

    行为约定：
    - 适配器构造失败（缺 yaml / 解析错）→ 让异常自然抛到调用方（CLI
      暴露给用户）
    - 不读 HIS / 前置审方接口（替身正式对接由任务 13 / 14 接入）
    - 同一 `data_root` 复用 yaml（避免反复 IO）
    """
    paths = default_paths(data_root)

    drug_dict = FakeDrugDict(paths["drug_dict"])
    interaction_db = FakeInteractionDb(paths["interaction_db"])
    allergy_db = FakeAllergyDb(paths["allergy_db"], drug_dict=drug_dict)
    rule_adapter = FakeRuleAdapter(
        paths["dose_rules"],
        paths["population_rules"],
        freq_rules_path=paths["freq_rules"],
        route_rules_path=paths["route_rules"],
    )
    rule_version = rule_adapter.get_rule_version()

    agents: dict[str, FieldAgent] = {
        FieldFamily.DRUG.value: DrugAgent(
            drug_dict, rule_version=rule_version
        ),
        FieldFamily.DOSE.value: DoseAgent(
            rule_adapter, drug_dict, rule_version=rule_version
        ),
        FieldFamily.FREQUENCY.value: FrequencyAgent(
            drug_dict, rule_adapter, rule_version=rule_version
        ),
        FieldFamily.ROUTE.value: RouteAgent(
            drug_dict, rule_adapter, rule_version=rule_version
        ),
        FieldFamily.INDICATION.value: IndicationAgent(
            drug_dict, rule_version=rule_version
        ),
        FieldFamily.ALLERGY.value: AllergyAgent(
            allergy_db, rule_version=rule_version
        ),
        FieldFamily.INTERACTION.value: InteractionAgent(
            interaction_db, rule_version=rule_version
        ),
        FieldFamily.POPULATION.value: PopulationAgent(
            rule_adapter, rule_version=rule_version
        ),
    }

    return CheckEngine(
        paths=paths,
        drug_dict=drug_dict,
        interaction_db=interaction_db,
        allergy_db=allergy_db,
        rule_adapter=rule_adapter,
        agents=agents,
        rule_version=rule_version,
    )


# --------------------------------------------------------------------------- #
# runs 落盘辅助                                                                #
# --------------------------------------------------------------------------- #


def _resolve_run_root(run_root: Optional[str | Path]) -> Path:
    """解析 runs 根目录；绝对路径直接返回，相对路径相对 cwd。"""
    if run_root is None:
        return Path.cwd() / DEFAULT_RUN_ROOT
    candidate = Path(run_root)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate


def _build_agent_jobs(
    prescription: Prescription,
    engine: CheckEngine,
) -> tuple[AgentCoordinator, dict[str, Any]]:
    """注册根节点 + 8 字段族子节点，返回 (coordinator, agent_id → job 协程)。

    Job 协程由 `FieldAgentContext` 喂入，调用 `agent.run()`。`supervise`
    内部处理状态迁移 + 异常收敛。
    """
    coordinator = AgentCoordinator()
    families = [FieldFamily.DRUG.value] + [
        f.value
        for f in (
            FieldFamily.DOSE,
            FieldFamily.FREQUENCY,
            FieldFamily.ROUTE,
            FieldFamily.INDICATION,
            FieldFamily.ALLERGY,
            FieldFamily.INTERACTION,
            FieldFamily.POPULATION,
        )
    ]
    patient_flags: tuple[str, ...] = ()  # 任务 14 从 HIS 患者档案派生；本任务占位

    async def _run_one(agent: FieldAgent, ctx: FieldAgentContext) -> list[Finding]:
        return await agent.run(ctx)

    # 注册同步发生在 event loop 中；这里默认由调用方走 `register_root` /
    # `register_field_agents` 同款（coordinator 内部是 async）
    return coordinator, {
        "families": families,
        "patient_flags": patient_flags,
        "engine": engine,
        "prescription": prescription,
        "_run_one": _run_one,
    }


# --------------------------------------------------------------------------- #
# 主入口：`rxchk check` 核心                                                    #
# --------------------------------------------------------------------------- #


async def run_check_async(
    prescription: Prescription,
    *,
    engine: Optional[CheckEngine] = None,
    data_root: Optional[str | Path] = None,
    run_root: Optional[str | Path] = None,
) -> CheckResult:
    """异步执行一次处方核对，并落盘到 `runs/<rx_no>/`。

    主路径（与源 `run_strix_scan` 对齐但裁剪）：
        parse → uploaded → parsing → running → 8 fields 并发 →
        run_field_agents → awaiting_review → write_findings →
        write_run → graph_snapshot

    Args:
        prescription: 已解析的 `Prescription`。
        engine: 复用的 `CheckEngine`（None 时按 `data_root` 构造一次）。
        data_root: yaml 数据根目录；仅 `engine is None` 时使用。
        run_root: runs 落盘根目录；None → `cwd/runs`。

    Returns:
        `CheckResult`：含 runs 路径 + findings + 图快照 + 状态。

    Raises:
        OSError: runs 目录不可写（落盘失败）。
    """
    if engine is None:
        engine = build_check_engine(data_root)

    runs_root = _resolve_run_root(run_root)
    runs_root.mkdir(parents=True, exist_ok=True)

    writer = ReportWriter(rx_no=prescription.prescription_no, run_root=runs_root)
    writer.ensure_run_dir()

    # 1. 落 input.json + transcript(input_received)
    writer.write_input(prescription)
    writer.append_transcript(
        TranscriptEvent(
            event=EVENT_INPUT_RECEIVED,
            payload={
                "items_count": len(prescription.items),
                "diagnoses_count": len(prescription.diagnoses),
                "allergies_count": len(prescription.allergies),
            },
        )
    )

    # 2. coordinator + 图快照路径
    coordinator = AgentCoordinator()
    coordinator.set_snapshot_path(writer.run_dir / "graph.json")
    await coordinator.register_root(prescription.prescription_no)
    family_values = list(engine.agents.keys())
    agent_ids = await coordinator.register_field_agents(
        family_values,
        parent_id=ROOT_AGENT_ID,
        rule_version=engine.rule_version,
    )

    # 3. 推进状态 uploaded → parsing
    writer.transition(
        RunState.PARSING,
        actor="rxchk.check",
        reason="input parsed; field agents registered",
    )
    writer.transition(
        RunState.RUNNING,
        actor="rxchk.check",
        reason=f"{len(agent_ids)} field agents dispatched",
    )

    # 4. 并发驱动 8 字段族 Agent
    job_specs = _build_agent_jobs(prescription, engine)
    families: list[str] = job_specs[1]["families"]
    patient_flags: tuple[str, ...] = job_specs[1]["patient_flags"]
    run_one = job_specs[1]["_run_one"]
    jobs = {
        aid: run_one(
            engine.agents[family],
            FieldAgentContext(
                prescription=prescription,
                patient_flags=patient_flags,
                rule_version=engine.rule_version,
                agent_id=aid,
            ),
        )
        for aid, family in zip(
            [f"rx_field_{fam}" for fam in families], families
        )
    }

    await coordinator.run_field_agents(jobs, root_id=ROOT_AGENT_ID)

    # 5. 收集 findings；每族 agent_finish → transcript event；落 findings.json
    findings: list[Finding] = []
    for family, agent in engine.agents.items():
        agent_id = f"rx_field_{family}"
        runtime = coordinator.runtimes.get(agent_id)
        family_findings = list(runtime.findings) if runtime else []
        findings.extend(family_findings)
        writer.append_transcript(
            TranscriptEvent(
                event=EVENT_AGENT_FINISH,
                family=family,
                agent_id=agent_id,
                payload={
                    "count": len(family_findings),
                    "rule_versions": [
                        engine.rule_version,
                    ],
                },
            )
        )

    # 6. 落盘 findings.json
    writer.write_findings(findings)

    # 7. 推进状态 running → awaiting_review
    summary = await coordinator.graph_summary()
    failed = await coordinator.failed_agents()
    graph = await coordinator.view_graph()
    writer.write_run(
        status=RunState.AWAITING_REVIEW,
        graph_path=writer.run_dir / "graph.json",
        agent_ids=agent_ids,
        findings_count=len(findings),
    )
    writer.append_transcript(
        TranscriptEvent(
            event=EVENT_STATE_CHANGE,
            payload={
                "actor": "rxchk.check",
                "to": RunState.AWAITING_REVIEW.value,
                "summary": summary,
                "failed_agents": failed,
            },
        )
    )

    return CheckResult(
        prescription_no=prescription.prescription_no,
        run_dir=writer.run_dir,
        findings=findings,
        agent_summary=summary,
        failed_agents=failed,
        graph=graph,
        status=RunState.AWAITING_REVIEW.value,
    )


def run_check(
    prescription: Prescription,
    *,
    engine: Optional[CheckEngine] = None,
    data_root: Optional[str | Path] = None,
    run_root: Optional[str | Path] = None,
) -> CheckResult:
    """`run_check_async` 的同步包装（供 CLI 与测试调用）。"""
    return asyncio.run(
        run_check_async(
            prescription,
            engine=engine,
            data_root=data_root,
            run_root=run_root,
        )
    )


# --------------------------------------------------------------------------- #
# 高层便捷入口：解析 + 核对（CLI / HTTP API 共用）                                #
# --------------------------------------------------------------------------- #


def check_from_dict(
    data: dict[str, Any],
    *,
    engine: Optional[CheckEngine] = None,
    data_root: Optional[str | Path] = None,
    run_root: Optional[str | Path] = None,
) -> CheckResult:
    """`dict` → 解析 → 核对 → 落盘一条龙。

    解析失败抛 `PrescriptionParseError`（不静默默认）。
    """
    prescription = parse_prescription(data)
    return run_check(
        prescription,
        engine=engine,
        data_root=data_root,
        run_root=run_root,
    )


def check_from_path(
    path: str | Path,
    *,
    engine: Optional[CheckEngine] = None,
    data_root: Optional[str | Path] = None,
    run_root: Optional[str | Path] = None,
) -> CheckResult:
    """`Path` → 解析 → 核对 → 落盘一条龙（同 `check_from_dict`）。"""
    prescription = parse_prescription(Path(path))
    return run_check(
        prescription,
        engine=engine,
        data_root=data_root,
        run_root=run_root,
    )


# --------------------------------------------------------------------------- #
# inbox 文件投递模式（CLI 入口，与 `watch_inbox` 解耦：单次扫一次）                #
# --------------------------------------------------------------------------- #


@dataclass
class InboxResult:
    """`rxchk check --from-inbox` 的批量结果。"""

    handled: list[CheckResult] = field(default_factory=list)
    failed_files: list[Path] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.handled) + len(self.failed_files)


def check_inbox(
    inbox_dir: str | Path,
    *,
    engine: Optional[CheckEngine] = None,
    data_root: Optional[str | Path] = None,
    run_root: Optional[str | Path] = None,
    processed_dirname: str = "processed",
    failed_dirname: str = "failed",
) -> InboxResult:
    """扫 `inbox_dir/*.json` → 解析 → 核对 → 落盘；成功移到 processed/，失败留 failed/。

    与 `parsers.watch_inbox` 解耦：本函数复跑 `run_check`（不调外部回调），
    失败时把 JSON 文件移到 `<inbox>/failed/` 并写 `.err` 注释，便于人工排查。
    """
    inbox = Path(inbox_dir)
    if not inbox.exists() or not inbox.is_dir():
        raise PrescriptionParseError(f"inbox path is not a directory: {inbox}")

    processed_dir = inbox / processed_dirname
    failed_dir = inbox / failed_dirname
    processed_dir.mkdir(exist_ok=True)
    failed_dir.mkdir(exist_ok=True)

    if engine is None:
        engine = build_check_engine(data_root)

    result = InboxResult()
    candidates = sorted(
        p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() == ".json"
    )

    for src in candidates:
        dest: Path
        try:
            check_result = check_from_path(
                src,
                engine=engine,
                data_root=data_root,
                run_root=run_root,
            )
            dest = processed_dir / src.name
            src.rename(dest)
            result.handled.append(check_result)
        except PrescriptionParseError as exc:
            dest = failed_dir / src.name
            src.rename(dest)
            err_note = dest.with_suffix(dest.suffix + ".err")
            err_note.write_text(
                f"{exc.__class__.__name__}: {exc}\n", encoding="utf-8"
            )
            result.failed_files.append(dest)
        except Exception as exc:  # noqa: BLE001 — 落盘核对失败也要兜底
            # 检查时崩：移到 failed/ 留人工排查
            try:
                dest = failed_dir / src.name
                if src.exists():
                    src.rename(dest)
                err_note = dest.with_suffix(dest.suffix + ".err")
                err_note.write_text(
                    f"{exc.__class__.__name__}: {exc}\n", encoding="utf-8"
                )
                result.failed_files.append(dest)
            except OSError:
                logger.exception("inbox: failed to move %s to failed/", src)

    return result


__all__ = [
    # 配置常量
    "DEFAULT_DATA_ROOT",
    "DEFAULT_RUN_ROOT",
    "INBOX_DIRNAME",
    "PACKAGE_DATA_DIRNAME",
    # 路径 / 适配器 / 引擎构造
    "CheckEngine",
    "build_check_engine",
    "default_paths",
    "package_data_root",
    "resolve_data_root",
    # 主入口
    "CheckResult",
    "InboxResult",
    "check_from_dict",
    "check_from_path",
    "check_inbox",
    "run_check",
    "run_check_async",
]


# --------------------------------------------------------------------------- #
# 兼容性：让 `CheckResult.findings_json` 等常用路径更易访问（供 test 断言）       #
# --------------------------------------------------------------------------- #

# 把 `findings` 的来源映射到 run_dir 固定文件名（与 `ReportWriter` 对齐）
for _name in (
    "input.json",
    "findings.json",
    "graph.json",
    "run.json",
    "transcript.jsonl",
):
    pass  # 仅做存在性参考；不动态绑定避免污染 dataclass


_ = json  # silence unused import in minimal environments
_ = os  # ditto
_ = Iterable  # ditto
