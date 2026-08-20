# 用药处方字段对照校验系统

> 临床药师把一张处方（或一批）丢进来，8 个字段核对子任务同时翻院内规则 / 药品字典 / 配伍库 / 过敏库，输出一张「字段 × 命中 × 证据」对照网格，确认后回写 HIS 或前置审方工单。

本系统把传统「前置审方」从单条规则串行扫一遍，拆成 8 个字段族（药品、剂量、频次、途径、适应症、过敏、配伍、人群）并行核对：每条命中都带规则编号、规则版本、证据原文与字段值，药师在浏览器里逐条确认、备注、驳回，全部动作 append-only 写入审计日志，再走 HIS / 前置审方回写。规则库、字典库、过敏库都以正式对接接口封装，本地替身实现方便脱敏样例演示与单元测试。

## 适用场景 / 目标岗位

| 岗位 | 什么时候用 | 得到什么 |
|------|------------|----------|
| 临床药师 / 审方药师 | HIS 推过来一张新处方，或前置审方拦下来一张高风险处方 | 「字段 × 命中 × 证据」对照网格 + 证据卡 + 一键确认 / 备注 / 驳回 |
| 药学部 / 处方点评小组 | 月度处方抽查、专项点评、回顾性复核 | 批量处方同时核对，每张留独立审计轨迹，可导出供医务存档 |
| 信息科 / 医保审核岗 | 医保飞检、药事专项审计，需要规则版本 + 证据可逐条回放 | 每条 finding 带 `rule_id` + `rule_version` + `evidence`，可在审计后端追到原始规则与字典条目 |
| 前置审方运维 / 规则维护 | 规则包升级后做新旧版本对比、回归测试 | 同一张处方用新旧 rule_version 各跑一遍，差异自动汇总成台账 |
| 药剂科主任 / 医务科会签 | 高危药品 / 多药联用 / 特殊人群用药，需要多视角交叉审核 | 8 字段族全跑：适应症、配伍、人群 三族联查，避免单族漏判 |

> **表格纪律**：本表只写岗位口语 + 业务时刻 + 可核对产出；规则 ID、HL7 字段、REST 路径、状态机字段名等一律放到「命令 / API / 配置说明」。

## 能力要点

- 一张处方同时派生 **8 个字段族核对 Agent**，并行查院内规则 / 药品字典 / 配伍库 / 过敏库，全部命中带规则 ID、规则版本、证据原文、字段值。
- 每条 finding 渲染成 **字段证据卡**，按字段族自动配色（药品 / 剂量 / 频次 / 途径 / 适应症 / 过敏 / 配伍 / 人群），高危命中色阶醒目，规则库缺族标黄底待人工补查。
- 药师在浏览器 Viewer 里逐条 **确认 / 备注 / 驳回**，动作 append-only 写入审计日志；HIS / 前置审方回写时携带审计摘要与本次确认结果。
- 规则包、字典库、过敏库、配伍库都以 **正式对接接口**（Protocol）封装，本地替身（Fake 适配器）打 `[FAKE]` 前缀方便脱敏样例演示；真实 HIS / 前置审方超时会自动重试 3 次，失败入本地队列待补跑。
- 单处方核对 → 落盘 → Viewer 可读，端到端 5 秒内完成；批量 5 张处方 15 秒内完成；token 鉴权保证本地 Viewer 不被局域网未授权访问。
- 整条链路区分**测试替身**（带 `[FAKE]` 前缀的本地实现）与**正式对接规范**（Protocol 抽象接口），便于上线时只替换适配器实现、不改业务主路径。

## 快速开始

```bash
# 1. 安装依赖（Python 3.10+）
cd rx-field-check
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. 准备样例处方
ls data/sample_rx/      # 应看到 rx_basic / rx_overdose / rx_allergy / rx_complex 4 张脱敏样例

# 3. 跑一条核对（CLI 入口）
rxchk check data/sample_rx/rx_overdose.json
#   → 派生 8 字段族 Agent 并发核对
#   → 落盘 runs/<rx_no>/{input.json, transcript.jsonl, findings.json, run.json}
#   → 终端打印 finding 摘要

# 4. 启动 Viewer（本地 HTTP + token 鉴权）
rxchk view <rx_no>
#   → 终端打印: Open: http://127.0.0.1:<port>/?token=<secret>
#   → 浏览器打开即可看对照网格 + 字段证据卡 + 确认模态

# 5. 验证：跑脱敏样例 e2e + 一键脚本
pytest tests/test_e2e.py -v
bash scripts/verify.sh    # 一键跑通：4 张样例 check → viewer 后台 → 9 条 curl 断言（runs 列表 / findings / confirm / audit / token 鉴权）→ viewer 自停；exit 0 表示全过
```

## 命令 / API / 配置说明

| 入口 | 命令 / 路径 | 说明 |
|------|------------|------|
| CLI 实时核对 | `rxchk check <rx_no_or_json_path>` | 解析处方 → 派生 8 字段族 Agent 并发 → 落盘 → 打印 finding 摘要 |
| CLI 本地视图 | `rxchk view <rx_no>` | 启动 stdlib HTTP Viewer + token 鉴权；终端打印 tokened URL |
| HTTP 投递 | `POST /api/prescriptions/<rx_no>/check` | 外部系统（前置审方 / HIS 拦截器）以 JSON 形式投递处方，异步返回核对任务 ID |
| HTTP 列表 | `GET /api/runs` | 按 mtime 倒序列出 runs/，每条含 `prescription_no` + `status`（uploaded/parsing/running/awaiting_review/confirmed/overridden） |
| HTTP finding | `GET /api/run/<rx_no>/findings` | 单张处方全部 finding（字段族 + 字段值 + 命中证据 + 严重度） |
| HTTP transcript | `GET /api/run/<rx_no>/transcript` | 单张处方逐事件流水（agent 派发 / 命中 / 异常 / 完成），append-only |
| HTTP 审计 | `GET /api/run/<rx_no>/audit` | 单张处方药师操作审计（confirm / override / reject / manual_override + 操作员 + 备注 + 时间戳） |
| HTTP 确认 | `POST /api/run/<rx_no>/confirm` | 药师对单条 finding 操作：`action` ∈ confirm / override / reject / manual_override；`operator` + `note` |
| HTTP 回写 | `GET /api/run/<rx_no>/writeback` | 查看回写 HIS / 前置审方的 payload 与状态（待补跑 / 已回写 / 失败） |
| 文件兜底 | `./inbox/<rx_no>.json` | 把处方 JSON 直接放到 inbox/ 目录，CLI 扫目录模式自动派发（断网 / HIS 不可达时的应急入口）；`rxchk check --from-inbox <dir>` 触发批量扫 |
| 一键验证 | `bash scripts/verify.sh` | 安装依赖 → 跑 4 张样例 check → 后台起 viewer（`--serve-seconds`） → 9 条 curl 断言（runs / findings / confirm / audit / token 鉴权）→ viewer 自停；exit 0 表示全过 |

**配置文件**（`rxchk.toml`，项目根目录可选）：
- `[adapters]` 段选择 HIS / 前置审方 / 规则库的真实或替身实现（`type = "fake" | "http"`）
- `[viewer]` 段配置 host（默认 127.0.0.1）、port（0 = 随机）、token 长度（默认 16）
- `[rules]` 段声明规则包版本号；运行期把这个版本号透传到每条 finding 与 transcript

**环境变量**：
- `RXCHK_RUNS_DIR`：runs/ 落盘目录（默认 `./runs`）
- `RXCHK_INBOX_DIR`：inbox/ 兜底目录（默认 `./inbox`）
- `RXCHK_LOG_LEVEL`：日志级别（默认 `INFO`，可调 `DEBUG` 看派发细节）

完整字段 schema（`Finding` / `Prescription` / `AuditEntry` / `HisWriteback`）见 `src/rxchk/models/`、`src/rxchk/audit.py`、`src/rxchk/adapters/his.py` 的 dataclass 注释与 `to_dict()`；错误码字典以 Viewer HTTP 响应 JSON 体 `code` 字段为准（`unauthed` / `unknown_run` / `invalid_action` / `run_state_conflict` 等）。HIS / 前置审方正式对接由业务侧实现 `HisAdapterProtocol` / `PreauditAdapterProtocol` 后即可替换替身，业务层无修改。

## 典型场景

**场景 1：前置审方触发的高风险处方复核**

前置审方系统拦下一张超剂量处方，推到本系统。临床药师打开 Viewer，看到「剂量」字段族命中黄色高危卡片：规则 `R-DOSE-007`（药物 `<EXAMPLE_DRUG_A>` 日剂量上限 800mg）、版本 `v2026.07`、`evidence` 给出原始字典条目。药师点确认 + 备注「与医师沟通后维持原剂量，监测肾功能」，审计日志 append 一条 `override + note`，HIS 处方审核备注回写完成，医师工作站弹出提示。

**场景 2：月度处方抽查与专项点评**

药学部月度抽查 50 张抗菌药处方，批量丢进本系统。8 字段族 Agent 并行核对，每张处方独立落 `runs/<rx_no>/`，药师在 Viewer 列表页一次性浏览 50 张 finding 网格，按字段族聚类查看（所有「抗菌药 + 剂量」命中聚合），挑出 8 张需要医师二次签字的高危处方，导出审计包交医务科会签存档。

**场景 3：药事复核飞行检查追溯**

医保飞检查阅某张处方，监管要求看到「为什么这张处方没有前置审方拦截」。本系统从 `runs/<rx_no>/audit.jsonl` 拉出当时药师的 `confirm` + `note`，从 `transcript.jsonl` 还原当时 8 字段 Agent 派发顺序与命中依据（规则 ID + 版本 + 证据原文），逐条可回放，与规则包版本号交叉验证，整个链路无需依赖 HIS 当天的运行日志。

## 输出样例

**`runs/<rx_no>/findings.json`（脱敏片段）**：

```json
{
  "prescription_no": "<EXAMPLE_RX_NO>",
  "status": "awaiting_review",
  "items": [
    {
      "item_id": "item-001",
      "drug_code": "<EXAMPLE_DRUG_A>",
      "field": "dose",
      "field_value": "1200mg",
      "family": "dose",
      "rule_id": "R-DOSE-007",
      "rule_version": "v2026.07",
      "hit_explanation": "<EXAMPLE_DRUG_A> 日剂量上限 800mg，本处方 1200mg 超限 50%",
      "evidence": "drug_dict.yaml: <EXAMPLE_DRUG_A>.max_daily_dose = 800mg",
      "severity": "high",
      "source": "rule_adapter",
      "agent_id": "agent-dose-001",
      "created_at": "2026-08-20T10:30:15+08:00"
    }
  ]
}
```

**`runs/<rx_no>/audit.jsonl`（一行）**：

```json
{"ts": "2026-08-20T10:32:40+08:00", "rx_no": "<EXAMPLE_RX_NO>", "item_id": "item-001", "finding_id": "f-abc123", "action": "override", "operator": "<EXAMPLE_PHARMACIST>", "note": "与医师沟通后维持原剂量，监测肾功能", "manual_override": true}
```

**`runs/<rx_no>/writeback.json`（HIS 回写 payload）**：

```json
{
  "rx_no": "<EXAMPLE_RX_NO>",
  "audit_summary": {"confirmed": 0, "overridden": 1, "rejected": 0},
  "findings_count": 1,
  "writeback_status": "success",
  "writeback_at": "2026-08-20T10:33:02+08:00"
}
```

**Viewer 初始页面**（截图见 [`docs/screenshots/viewer_initial.png`](docs/screenshots/viewer_initial.png)）：

![Viewer 初始页面](docs/screenshots/viewer_initial.png)

> 截图说明：本环境无 GUI / 无浏览器依赖，PNG 由 `scripts/make_screenshot.py` 程序化绘制——按真实 `src/rxchk/web/index.html` 布局（顶栏 / 历史处方下拉 / 字段证据卡网格 / 核对事件流 / 药师操作记录 / 决策模态 / 页脚连接状态）渲染并按真实 `style.css` 字段族与 severity 配色上色，作为产品视觉占位供 README 阅读者参考；样式 token 与运行版本以 `src/rxchk/web/style.css` 为准。

## 架构与数据流

```
┌─────────────┐    JSON    ┌────────────┐    并发派发   ┌─────────────────────────┐
│ HIS / 前置  │ ─────────► │  处方解析  │ ────────────► │  8 字段族 Agent 池       │
│ 审方投递    │            │  Parser    │               │  drug / dose / freq /   │
└─────────────┘            └────────────┘               │  route / indication /   │
       │                          │                     │  allergy / interaction  │
       │ 文件兜底                  ▼                     │  / population           │
       │                  ┌──────────────┐               └──────────┬──────────────┘
       ▼                  │  AgentCoord  │                          │
┌─────────────┐            │  状态机 +    │                          ▼
│ ./inbox/    │            │  并发锁 +    │               ┌──────────────────────┐
│ <rx>.json   │ ─────────► │  派发协调器  │ ──────────────►│ 规则 / 字典 / 配伍 / │
└─────────────┘            └──────────────┘               │ 过敏 适配器（Protocol）│
                                   │                       │  Fake (脱敏样例)       │
                                   ▼                       │  HTTP (正式对接)        │
                          ┌─────────────────┐              └──────────┬──────────────┘
                          │  落盘           │                         │
                          │  runs/<rx>/     │ ◄───────────────────────┘
                          │  {input,        │
                          │   transcript,   │              ┌─────────────────┐
                          │   findings,     │ ────────────►│  Viewer         │
                          │   run, audit}   │              │  stdlib HTTP    │
                          └────────┬────────┘              │  + token 鉴权   │
                                   │                       │  + 字段证据卡   │
                                   ▼                       └────────┬────────┘
                          ┌─────────────────┐                       │
                          │  HIS / 前置      │ ◄─────────────────────┘
                          │  审方回写        │   POST /confirm 触发
                          │  (Adapter)       │
                          └─────────────────┘
```

**核心数据流**：JSON 处方 → Parser 校验字段 → AgentCoordinator 派发 8 字段族 Agent → 每族 Agent 调自家族适配器 → 命中产 Finding → ReportWriter 落盘 → Viewer 渲染字段证据卡 → 药师确认 → Audit append → HIS 适配器回写。

**字段族识别**：每个 Agent 只调自家族前缀的工具（参考源里 tool-renderer 的同族前缀匹配机制，本项目用 `family` 枚举 + 字段名 pattern 双层匹配），新加一族规则无需改派发代码即可被对应族 Agent 拾取。

## 安全与合规边界

- **仅做文书 / 字段核对**，不输出诊断或治疗建议；所有命中（finding）只是「规则 ID + 证据 + 严重度」的客观事实陈述，最终确认 / 驳回由临床药师人工完成。
- **规则版本透传**：每条 finding 携带 `rule_version`，跨规则版本回溯时可直接核对；规则包升级必须走配置切换，不允许在生产期静默换规则。
- **审计 append-only**：`audit.jsonl` 与 `transcript.jsonl` 一旦写入不可修改；HIS / 前置审方回写时携带审计摘要与本次确认结果，回写 payload 与 finding 一一对应。
- **本地 Viewer 鉴权**：stdlib HTTP 服务仅绑定 127.0.0.1，启动时随机生成 16 位 token（URL `?token=<secret>`）；token 缺失 / 错误返回 401，避免局域网未授权访问。
- **替身与真实实现明确分离**：本地替身（Fake Adapter）日志显式 `[FAKE]` 前缀；正式对接接口以 Protocol 抽象签名约束；HIS / 前置审方超时（10s）→ 重试 3 次 → 入 failed 队列待补跑，不静默重写业务状态。

## 项目结构

```
rx-field-check/
├── README.md
├── LICENSE                  # MIT
├── pyproject.toml
├── .gitignore
├── qrcode.jpg
├── src/
│   └── rxchk/
│       ├── models/          # 处方 / Finding / 字段族定义
│       ├── parsers/         # 处方 JSON 解析 + inbox 扫描
│       ├── tools/           # 规则 / 字典 / 配伍 / 过敏 适配器
│       ├── agents/          # 8 字段族 Agent 子类（drug / dose / freq / route / indication / allergy / interaction / population）
│       ├── coordinator.py   # AgentCoordinator 派发协调器
│       ├── report/          # 落盘 writer + 状态机
│       ├── audit.py         # 药师操作 append-only 审计
│       ├── adapters/        # HIS / 前置审方 回写适配器（Protocol + Fake 替身）
│       ├── viewer/          # stdlib HTTP Viewer + 鉴权
│       ├── web/             # 前端 index.html + app.js + style.css
│       ├── check_engine.py  # 处方核对 product-level 主路径
│       └── cli.py           # rxchk check / rxchk view 入口
├── src/rxchk/data/
│   ├── drug_dict.yaml
│   ├── interaction_db.yaml
│   ├── allergy_db.yaml
│   ├── rule/
│   │   ├── dose_rules.yaml
│   │   └── population_rules.yaml
│   └── sample_rx/           # 4 张脱敏样例：rx_basic / rx_overdose / rx_allergy / rx_complex
├── tests/                   # 单元测试 + e2e（469 个测试）
├── scripts/
│   ├── verify.sh            # 一键验证脚本（check × 4 → viewer 后台 → 9 条 curl 断言 → kill；exit 0 全过）
│   └── make_screenshot.py   # 程序化生成 Viewer 截图（无浏览器依赖）
├── docs/
│   └── screenshots/
│       └── viewer_initial.png  # Viewer 初始页面布局占位（程序化绘制）
├── inbox/                   # 处方文件兜底投递目录（CLI 扫目录模式入口）
└── runs/                    # 核对结果落盘目录（运行时生成，gitignored）
```

## License

MIT

---

## 关注我们

欢迎扫码关注公众号，获取项目更新与交流加群：

![关注我们](qrcode.jpg)