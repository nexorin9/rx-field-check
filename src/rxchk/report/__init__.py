"""处方核对持久化：runs/<prescription_no>/ 落盘与状态机。

设计目标（spec.md「输出闭环」表）
---------------------------------
- `runs/<rx_no>/input.json`        入参（由 parser 写入）
- `runs/<rx_no>/graph.json`        字段 Agent 图快照（由 coordinator 写入）
- `runs/<rx_no>/transcript.jsonl`  单 Agent 完成 / 状态切换事件（append-only）
- `runs/<rx_no>/findings.json`     全部 Agent 完成后按字段族聚合
- `runs/<rx_no>/run.json`          处方级状态机 + 汇总指标
- `runs/<rx_no>/audit.jsonl`       药师确认 / 驳回 / 备注（任务 12 引入）
- `runs/<rx_no>/writeback.json`    HIS / 前置审方回执（任务 13 引入）

参考源（github_ref 代码证据）：
- `strix/core/agents.py: _maybe_snapshot` 原子写 tmp + replace
- `strix/tools/agents_graph/tools.py: _render_completion_report` 报告 schema

融合取舍
--------
- 源是漏洞扫描 runs/<run>/，本项目改为 runs/<prescription_no>/（每张处方
  一个审计单元）；目录里文件名按 spec.md 输出闭环表固定
- 处方级状态机（`uploaded → parsing → running → awaiting_review →
  confirmed | overridden → written_back`）独立于协调器节点级状态机
  （`running → completed | failed | crashed | stopped`），由本模块管理
- 写盘采用 `tempfile.NamedTemporaryFile` + `Path.replace` 实现原子
  落盘；写盘失败只记 logger.exception，不阻断核对主流程
- transcript.jsonl 永远 append-only；损坏行不阻止追加新行（任务 12 同样
  约束）

源 repo 名 / slug 不出现在产品代码或字符串字面值。
"""

from rxchk.report.state import (
    RunState,
    RunStateError,
    RUN_STATE_TRANSITIONS,
    TERMINAL_RUN_STATES,
    is_valid_transition,
)
from rxchk.report.writer import (
    ReportWriter,
    ReportWriterError,
    TranscriptEvent,
)

__all__ = [
    # state
    "RunState",
    "RunStateError",
    "RUN_STATE_TRANSITIONS",
    "TERMINAL_RUN_STATES",
    "is_valid_transition",
    # writer
    "ReportWriter",
    "ReportWriterError",
    "TranscriptEvent",
]