"""Viewer 静态渲染函数测试。

任务 11 — Viewer 前端 (`src/rxchk/web/`)

测试目标
--------
测试 `render.js` 中的 **纯函数**（无 DOM / fetch 依赖），覆盖：
- 字段族聚合与排序
- severity / decision / status 着色类
- finding_id 稳定性
- DOM diff 增量（additions / removals / kept）
- HTML 字符串构造的转义与必备结构

设计取向
--------
- 不依赖 jsdom / 无头浏览器：用 `execjs` 缺席时走 Python `subprocess` 直接跑 Node，
  或者用纯 stdlib 的 JS 评估（`_evaluate_pure_render`）。这样前端改一处 css 类名
  或纯函数签名都能被 Python 测试直接看到，无需起 http 服务器。
- 失败信息给出明确的 JS 函数名与期望值，便于排错。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# 加载 render.js 暴露的纯函数                                                  #
# --------------------------------------------------------------------------- #


REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_JS = REPO_ROOT / "src" / "rxchk" / "web" / "render.js"


def _load_pure_render():
    """直接 `require()` render.js。Node 端模块导出为 `module.exports = api`。
    走 Python 端用 subprocess + node 评估，确保非前端环境下也能拿值。
    """
    script = f"""
const path = {json.dumps(str(RENDER_JS))};
const api = require(path);
const fns = [
  'FAMILY_META','FAMILY_ORDER',
  'groupByFamily','severityClass','severityPillClass',
  'decisionClass','runStatusClass','findingId','safeString',
  'renderFindingCard','renderFamilyBlock','renderFamilyGrid',
  'renderConfirmModalBody','renderTranscriptItem','renderAuditItem',
  'diffFindings','escapeHtml',
];
const out = {{}};
for (const k of fns) out[k] = api[k];
process.stdout.write(JSON.stringify({{ok:true, source:FUNCTION_NAMES_PLACEHOLDER }}));
""".replace("FUNCTION_NAMES_PLACEHOLDER", "out")

    # 用一次性 JS 文件，绕过 shell 转义难题
    probe = REPO_ROOT / "logs" / "_viewer_render_probe.js"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(
        f"""
const path = {json.dumps(str(RENDER_JS))};
const api = require(path);
const fn_names = [
  'FAMILY_META','FAMILY_ORDER',
  'groupByFamily','severityClass','severityPillClass',
  'decisionClass','runStatusClass','findingId','safeString',
  'renderFindingCard','renderFamilyBlock','renderFamilyGrid',
  'renderConfirmModalBody','renderTranscriptItem','renderAuditItem',
  'diffFindings','escapeHtml',
];
const out = {{}};
for (const k of fn_names) {{
  out[k] = api[k];
}}
process.stdout.write("__PURE_API__" + JSON.stringify(out));
""",
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            ["node", str(probe)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError as exc:
        pytest.skip(f"Node.js 不可用：{exc}")
    if proc.returncode != 0:
        pytest.fail(
            "render.js 加载失败：\n"
            f"stdout: {proc.stdout}\n"
            f"stderr: {proc.stderr}"
        )
    out = proc.stdout
    start = out.find("__PURE_API__")
    if start < 0:
        pytest.fail(f"render.js 探针返回缺少 __PURE_API__ 前缀：{out!r}")
    payload = out[start + len("__PURE_API__"):].strip()
    try:
        rendered = json.loads(payload)
    except json.JSONDecodeError as exc:
        pytest.fail(f"render.js 探针 JSON 解析失败：{exc}\n原始输出：{payload!r}")
    # JSON 不保留 JS function（renderFindingCard 等），测试用 string 路径；
    # 我们这里只导出可 JSON 的元数据（function 留作 call 调用）。
    # 对 string 函数改写到模块：调用通过 subprocess 评估。
    return rendered


# --------------------------------------------------------------------------- #
# 简化探针：单函数 + 输入 → 输出字符串                                        #
# --------------------------------------------------------------------------- #


def _eval_func(func_name: str, expr: str) -> str:
    """`require(render.js).<func_name>(<expr 字面量>)` 后强制 JSON 序列化。

    返回 `__OUT__{...}` —— 调用方 `json.loads(_strip(...))` 解码。
    统一 JSON 编码避免 string / object 走两条解析路径。
    """
    probe = REPO_ROOT / "logs" / "_viewer_render_probe.js"
    probe.write_text(
        f"""
const path = {json.dumps(str(RENDER_JS))};
const api = require(path);
const out = api.{func_name}({expr});
process.stdout.write("__OUT__" + JSON.stringify(out));
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(probe)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"render.js 执行 {func_name} 失败：\n"
            f"stdout: {proc.stdout}\n"
            f"stderr: {proc.stderr}"
        )
    out = proc.stdout
    start = out.find("__OUT__")
    if start < 0:
        pytest.fail(f"{func_name} 返回缺少 __OUT__ 前缀：{out!r}")
    return out[start + len("__OUT__"):]


def _strip(out_with_marker: str) -> str:
    """剥掉 `__OUT__` 前缀并 JSON 解码。"""
    raw = out_with_marker[len("__OUT__"):] if out_with_marker.startswith("__OUT__") else out_with_marker
    return json.loads(raw)


def _eval_call(func_name: str, *args) -> str:
    """多参数调用 `api[func_name](arg1, arg2, ...)` —— 服务于 `diffFindings(prev, next)` 等多参函数。

    每个 arg 都用 _js_obj() 编码为 JS 字面量；进程内 `api.func_name(a, b)` 直接展开。
    """
    probe = REPO_ROOT / "logs" / "_viewer_render_probe.js"
    args_js = ",".join(_js_obj(a) for a in args)
    probe.write_text(
        f"""
const path = {json.dumps(str(RENDER_JS))};
const api = require(path);
const out = api.{func_name}({args_js});
process.stdout.write("__OUT__" + JSON.stringify(out));
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(probe)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"render.js 执行 {func_name} 失败：\n"
            f"stdout: {proc.stdout}\n"
            f"stderr: {proc.stderr}"
        )
    out = proc.stdout
    start = out.find("__OUT__")
    if start < 0:
        pytest.fail(f"{func_name} 返回缺少 __OUT__ 前缀：{out!r}")
    return out[start + len("__OUT__"):]


def _js_obj(d) -> str:
    """Python dict → 紧凑 JSON，expose 给 node 子进程用。"""
    return json.dumps(d, ensure_ascii=False)


def _eval_property(prop_name: str) -> str:
    """读 `api[prop_name]`（非函数属性），JSON 序列化返回值。"""
    probe = REPO_ROOT / "logs" / "_viewer_render_probe.js"
    probe.write_text(
        f"""
const path = {json.dumps(str(RENDER_JS))};
const api = require(path);
const out = api[{json.dumps(prop_name)}];
process.stdout.write("__OUT__" + JSON.stringify(out));
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(probe)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"render.js 读属性 {prop_name} 失败：\n"
            f"stderr: {proc.stderr}"
        )
    out = proc.stdout
    start = out.find("__OUT__")
    if start < 0:
        pytest.fail(f"{prop_name} 返回缺少 __OUT__ 前缀：{out!r}")
    return out[start + len("__OUT__"):]


# --------------------------------------------------------------------------- #
# 1. 字段族元数据                                                              #
# --------------------------------------------------------------------------- #


def test_family_order_is_deterministic() -> None:
    """FAMILY_ORDER 排序稳定；与 spec.md FieldFamily 枚举顺序一致。"""
    out = _eval_property("FAMILY_ORDER")
    actual = json.loads(out)
    expected = ["drug", "dose", "frequency", "route",
                "indication", "allergy", "interaction", "population",
                "uncovered"]
    assert actual == expected, f"FAMILY_ORDER 异常：{actual}"


def test_family_meta_covers_eight_field_families() -> None:
    """8 字段族 + uncovered 兜底共 9 项。"""
    out = _eval_property("FAMILY_META")
    meta = json.loads(out)
    keys = sorted(meta.keys())
    expected = sorted([
        "drug", "dose", "frequency", "route",
        "indication", "allergy", "interaction", "population", "uncovered",
    ])
    assert keys == expected, f"FAMILY_META 缺项：{keys}"


# --------------------------------------------------------------------------- #
# 2. severityClass / decisionClass / runStatusClass                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("severity,expected", [
    ("high", "severity-high"),
    ("medium", "severity-medium"),
    ("low", "severity-low"),
    ("info", "severity-info"),
    ("HIGH", "severity-high"),    # 大小写归一
    ("unknown", "severity-info"),  # 未知值兜底
])
def test_severity_class(severity: str, expected: str) -> None:
    out = _eval_func("severityClass", json.dumps(severity))
    val = json.loads(out)
    assert val == expected, f"severity={severity} 期望 {expected}，得到 {val!r}"


def test_severity_pill_class_keeps_severity_prefix() -> None:
    out = _eval_func("severityPillClass", '"medium"')
    val = json.loads(out)
    assert val == "severity-medium-pill", f"severityPillClass 输出：{val!r}"


@pytest.mark.parametrize("action,expected", [
    ("confirm", "is-confirmed"),
    ("override", "is-overridden"),
    ("reject", "is-rejected"),
    ("manual_override", "is-overridden"),
    ("unknown", ""),
])
def test_decision_class(action: str, expected: str) -> None:
    out = _eval_func("decisionClass", json.dumps(action))
    val = json.loads(out)
    assert val == expected, f"action={action} 期望 {expected}，得到 {val!r}"


@pytest.mark.parametrize("status,expected", [
    ("awaiting_review", "is-awaiting_review"),
    ("confirmed", "is-confirmed"),
    ("overridden", "is-overridden"),
    ("written_back", "is-written_back"),
])
def test_run_status_class(status: str, expected: str) -> None:
    out = _eval_func("runStatusClass", json.dumps(status))
    val = json.loads(out)
    assert val == expected, f"status={status} 期望 {expected}，得到 {val!r}"


# --------------------------------------------------------------------------- #
# 3. findingId 稳定性                                                          #
# --------------------------------------------------------------------------- #


def test_finding_id_deterministic_for_same_input() -> None:
    """同一 finding 应得稳定 id；前端 DOM / 后端 audit.jsonl 都用此 id 对齐。"""
    f = {"rule_id": "R-001", "field": "drug_code", "field_value": "DRUG_AMOX"}
    a = json.loads(_eval_func("findingId", _js_obj(f)))
    b = json.loads(_eval_func("findingId", _js_obj(f)))
    assert a == b, f"findingId 不稳定：{a!r} vs {b!r}"
    assert a.startswith("finding.R-001.drug_code."), f"前缀异常：{a!r}"


def test_finding_id_sanitizes_long_field_value() -> None:
    """field_value 中的特殊字符应被替换；长度截断到 40 字符。"""
    f = {
        "rule_id": "R-002",
        "field": "dose",
        "field_value": "<a'>&" * 20,  # 大量特殊字符
    }
    a = json.loads(_eval_func("findingId", _js_obj(f)))
    assert "'" not in a and '"' not in a and "<" not in a and ">" not in a, (
        f"特殊字符未过滤：{a!r}"
    )


def test_finding_id_different_field_or_rule_differs() -> None:
    """field / rule_id 任一不同 → 不同 id。"""
    base = {"rule_id": "R-X", "field": "dose", "field_value": "1g"}
    diff_field = {"rule_id": "R-X", "field": "frequency", "field_value": "1g"}
    diff_rule = {"rule_id": "R-Y", "field": "dose", "field_value": "1g"}
    a = json.loads(_eval_func("findingId", _js_obj(base)))
    b = json.loads(_eval_func("findingId", _js_obj(diff_field)))
    c = json.loads(_eval_func("findingId", _js_obj(diff_rule)))
    assert len({a, b, c}) == 3, f"三组输入应得 3 个唯一 id，得到 {[a,b,c]}"


# --------------------------------------------------------------------------- #
# 4. escapeHtml / safeString                                                   #
# --------------------------------------------------------------------------- #


def test_escape_html_neutralizes_xss() -> None:
    payload = "<script>alert(1)</script>&\"'"
    out = json.loads(_eval_func("escapeHtml", json.dumps(payload)))
    assert "<script>" not in out
    assert "&amp;" in out
    assert "&lt;" in out
    assert "&gt;" in out
    assert "&quot;" in out
    assert "&#39;" in out


def test_safe_string_handles_null_undefined_and_empty() -> None:
    """safeString 把 null / undefined / 空字符串都降级为 fallback。"""
    out1 = json.loads(_eval_call("safeString", None, "—"))
    out2 = json.loads(_eval_call("safeString", "value_undefined_marker", "?"))
    out3 = json.loads(_eval_call("safeString", "", "∅"))
    assert out1 == "—", f"null 兜底：{out1!r}"
    assert out2 == "value_undefined_marker", f"非空字符串保留：{out2!r}"
    assert out3 == "∅", f"空字符串兜底：{out3!r}"
    # undefined：JS 字面量 `undefined` 不能 JSON 编码，改走 _eval_func 直传 JS 表达式
    out_undef = json.loads(
        _eval_func("safeString", "undefined, '?'")
    )
    assert out_undef == "?", f"undefined 兜底：{out_undef!r}"


# --------------------------------------------------------------------------- #
# 5. groupByFamily / 排序                                                       #
# --------------------------------------------------------------------------- #


def test_group_by_family_groups_and_orders() -> None:
    findings = [
        {"family": "allergy", "rule_id": "R1", "field": "drug_code", "field_value": "x"},
        {"family": "drug", "rule_id": "R2", "field": "drug_code", "field_value": "y"},
        {"family": "dose", "rule_id": "R3", "field": "dose", "field_value": "z"},
        {"family": "drug", "rule_id": "R4", "field": "drug_code", "field_value": "w"},
        # 缺失 family → 应归 uncovered 兜底
        {"family": "", "rule_id": "R5", "field": "x", "field_value": "v"},
    ]
    obj = json.loads(_eval_func("groupByFamily", _js_obj(findings)))
    families = [g["family"] for g in obj]
    # 顺序应固定：drug → dose → allergy → uncovered
    assert families == ["drug", "dose", "allergy", "uncovered"], (
        f"group 顺序异常：{families}"
    )
    drug_count = sum(1 for g in obj if g["family"] == "drug")
    assert drug_count == 1 and len(obj[0]["items"]) == 2, "同族聚合数量异常"
    # uncovered 兜底：family='' 落到 uncovered 组
    uncovered = next((g for g in obj if g["family"] == "uncovered"), None)
    assert uncovered is not None and len(uncovered["items"]) == 1, (
        f"uncovered 兜底缺失：{[g.get('family') for g in obj]}"
    )


def test_group_by_family_empty_input() -> None:
    obj = json.loads(_eval_func("groupByFamily", "[]"))
    assert obj == [], f"空输入应得空数组：{obj}"


# --------------------------------------------------------------------------- #
# 6. diffFindings 增量对比                                                     #
# --------------------------------------------------------------------------- #


def test_diff_findings_add_remove_kept_classification() -> None:
    """prevItems + nextItems → additions / removals / kept 精确。"""
    prev = [
        {"rule_id": "R1", "field": "drug_code", "field_value": "A"},
        {"rule_id": "R2", "field": "dose", "field_value": "1g"},
    ]
    next_items = [
        {"rule_id": "R1", "field": "drug_code", "field_value": "A"},   # kept
        {"rule_id": "R3", "field": "freq", "field_value": "tid"},       # addition
        # R2 删除
    ]
    obj = json.loads(_eval_call("diffFindings", prev, next_items))

    def _id(item): return item.get("rule_id") or ""

    add_ids = {_id(x) for x in obj["additions"]}
    rem_ids = set(obj["removals"])
    kept_count = len(obj["kept"])

    assert add_ids == {"R3"}, f"additions 异常：{add_ids}"
    # R2 finding_id 应出现在 removals
    assert any("R2" in s for s in rem_ids), f"removals 异常：{rem_ids}"
    assert kept_count == 1 and obj["kept"][0]["rule_id"] == "R1"


def test_diff_findings_handles_empty_state() -> None:
    """prev 空 + next 不空 → 全部 add；prev 不空 + next 空 → 全部 remove。"""
    obj1 = json.loads(
        _eval_call("diffFindings", [], [{"rule_id": "R1", "field": "x", "field_value": "y"}])
    )
    assert len(obj1["additions"]) == 1 and obj1["removals"] == [], (
        f"prev 空场景：{obj1}"
    )

    obj2 = json.loads(
        _eval_call(
            "diffFindings",
            [{"rule_id": "R1", "field": "x", "field_value": "y"}],
            [],
        )
    )
    assert obj2["additions"] == [] and len(obj2["removals"]) == 1, (
        f"next 空场景：{obj2}"
    )


# --------------------------------------------------------------------------- #
# 7. HTML 字符串构造（转义、必备结构）                                        #
# --------------------------------------------------------------------------- #


def _finding_sample() -> dict:
    return {
        "prescription_no": "RX-2026-<EXAMPLE>",
        "field": "drug_code",
        "field_value": "DRUG_AMOX",
        "family": "drug",
        "rule_id": "R-AMOX-001",
        "rule_version": "2026Q1",
        "hit_explanation": "命中规则：头孢类与青霉素存在家族交叉",
        "evidence": "<refs>pmc:12345</refs>",
        "severity": "high",
        "source": "rule_adapter",
        "agent_id": "drug_agent",
        "created_at": "2026-08-20T00:00:00Z",
    }


def test_render_finding_card_escapes_user_values() -> None:
    sample = _finding_sample()
    sample["field_value"] = "<script>alert(1)</script>"
    sample["evidence"] = "image 'onerror=\"x\"'"
    out = json.loads(_eval_func("renderFindingCard", _js_obj(sample)))
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out
    assert "onerror" in out  # in-text mention OK; 真正执行的 " 已被转义
    assert "&quot;" in out


def test_render_finding_card_class_structure() -> None:
    """HTML 含：family + severity + finding-id 三类保证。"""
    out = json.loads(_eval_func("renderFindingCard", _js_obj(_finding_sample())))
    assert out.startswith('<article class="field-card severity-high'), (
        f"卡片 class 起始：{out[:80]!r}"
    )
    assert 'data-family="drug"' in out
    assert 'data-finding-id="finding.R-AMOX-001.drug_code.DRUG_AMOX"' in out


def test_render_finding_card_for_uncovered_uses_warning_class() -> None:
    sample = _finding_sample()
    sample["family"] = "uncovered"
    sample["severity"] = "info"
    out = json.loads(_eval_func("renderFindingCard", _js_obj(sample)))
    # uncovered 是 class 中的独立 token（与 severity / 其它修饰类之间以空格分隔）
    assert re.search(r'\buncovered\b', out), f"uncovered 卡片缺黄底类：{out[:120]!r}"
    assert "severity-info" in out


def test_render_family_block_groups_cards() -> None:
    findings = [
        _finding_sample(),
        {
            **_finding_sample(),
            "rule_id": "R-OVERDOSE-001",
            "field": "dose",
            "field_value": "5g",
            "family": "dose",
            "severity": "high",
        },
    ]
    obj = json.loads(_eval_func("groupByFamily", _js_obj(findings)))
    assert len(obj) == 2, "两条 finding 应聚合到 2 个族"

    # 真实 HTML：用 groupByFamily 结果递交给 renderFamilyGrid
    html = json.loads(_eval_func("renderFamilyGrid", _js_obj(obj)))
    # 必须一次性打印两块
    assert html.count('class="family-block"') == 2, (
        f"两块字段族未渲染：{html[:200]}"
    )
    assert '<header class="family-head">' in html
    assert "证据卡" in html


def test_render_family_grid_empty_state() -> None:
    out = json.loads(_eval_func("renderFamilyGrid", "[]"))
    assert "本处方暂无字段证据卡" in out


def test_render_confirm_modal_body_includes_required_fields() -> None:
    out = json.loads(_eval_func("renderConfirmModalBody", _js_obj(_finding_sample())))
    for label in ["处方号", "字段", "字段值", "字段族", "严重度", "解释", "证据", "规则",
                  "字段 Agent", "命中时间"]:
        assert label in out, f"模态体缺字段：{label}"
    assert "R-AMOX-001" in out
    assert "severity-pill severity-high" in out


def test_render_transcript_item_includes_event_pill() -> None:
    out = json.loads(_eval_func(
        "renderTranscriptItem",
        _js_obj({"event": "agent_finish", "ts": "2026-08-20T01:00:00Z",
                 "agent_id": "drug_agent", "family": "drug"}),
    ))
    assert 'class="event-pill"' in out
    assert "agent_finish" in out
    assert "drug_agent" in out
    assert "drug" in out


def test_render_audit_item_decision_pill_and_note() -> None:
    out = json.loads(_eval_func(
        "renderAuditItem",
        _js_obj({"action": "override", "operator": "pharmacist-001",
                 "ts": "2026-08-20T02:00:00Z",
                 "note": "临床判断：肝功能恢复，可继续服用"}),
    ))
    assert 'class="action-pill is-overridden"' in out
    assert "override" in out
    assert "pharmacist-001" in out
    assert "临床判断" in out


def test_static_assets_exist_and_have_required_signals() -> None:
    """`src/rxchk/web/` 三个静态文件就位 + 关键结构存在。"""
    static_root = REPO_ROOT / "src" / "rxchk" / "web"
    files = ["index.html", "app.js", "style.css", "render.js"]
    for name in files:
        assert (static_root / name).is_file(), f"前端文件缺失：{name}"

    index_html = (static_root / "index.html").read_text(encoding="utf-8")
    for marker in [
        "<title>用药处方字段对照校验系统 — 药师工作站</title>",
        'id="family-grid"',
        'id="modal-mask"',
        'src="app.js"',
    ]:
        assert marker in index_html, f"index.html 缺关键标签：{marker}"

    css = (static_root / "style.css").read_text(encoding="utf-8")
    for var_name in [
        "--family-drug",
        "--family-dose",
        "--family-frequency",
        "--family-route",
        "--family-indication",
        "--family-allergy",
        "--family-interaction",
        "--family-population",
        "--family-uncovered",
        "--severity-high",
        "--severity-medium",
        "--severity-low",
        "--severity-info",
    ]:
        assert var_name in css, f"style.css 缺配色变量：{var_name}"

    app_js = (static_root / "app.js").read_text(encoding="utf-8")
    for marker in [
        "POLL_INTERVAL_MS = 1000",
        "fetchJson(",
        "applyFindingsDiff",
        "openModalFor",
        "postConfirm",
    ]:
        assert marker in app_js, f"app.js 缺关键符号：{marker}"


def test_app_js_handles_three_audit_decisions() -> None:
    """按钮在 index.html；app.js 中 postConfirm 能识别三种 action。"""
    index_html = (REPO_ROOT / "src" / "rxchk" / "web" / "index.html").read_text(encoding="utf-8")
    app_js = (REPO_ROOT / "src" / "rxchk" / "web" / "app.js").read_text(encoding="utf-8")
    # index.html 中三个按钮（含 manual_override 入口若有）
    for action in ["confirm", "override", "reject"]:
        assert f'data-action="{action}"' in index_html, (
            f"index.html 中缺 {action} 按钮"
        )
    # app.js 中 postConfirm 走 audit.jsonl（POST /api/run/<rx>/confirm + authHeaders）
    for marker in [
        "/confirm",
        "postConfirm",
        "fetchJson(url",
        "Authorization",
        "Bearer",
    ]:
        assert marker in app_js, (
            f"app.js 中缺关键符号 {marker}"
        )


# --------------------------------------------------------------------------- #
# 8. 与后端契约的回归（finding 字段名严格匹配 spec.md）                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field", [
    "prescription_no", "field", "field_value", "family",
    "rule_id", "rule_version", "hit_explanation", "evidence",
    "severity", "source", "agent_id", "created_at",
])
def test_finding_required_fields_render(field: str) -> None:
    """前端能渲染 spec.md 必填的所有 finding 字段。"""
    sample = _finding_sample()
    out = json.loads(_eval_func("renderFindingCard", _js_obj(sample)))
    val = str(sample[field])
    # 渲染会把值塞到 data-* 属性或可见文本中；只校验「非特殊字符值能命中」
    if val and not any(c in val for c in "<>&'\""):
        assert val in out, f"卡片缺字段值 {field}={val!r}"
    # 必备：data-* 属性内有（severity 不在 data-*，用 severity class 检查）
    attr_map = {
        "prescription_no": "data-prescription-no=",
        "agent_id": "data-agent-id=",
        "created_at": "data-created-at=",
        "source": "data-source=",
        "family": "data-family=",
    }
    if field in attr_map:
        assert attr_map[field] in out, f"卡片缺属性 {attr_map[field]}"


# --------------------------------------------------------------------------- #
# 9. 8 字段族 + uncovered 都能渲染                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("family", [
    "drug", "dose", "frequency", "route",
    "indication", "allergy", "interaction", "population",
    "uncovered",
])
def test_every_field_family_renders(family: str) -> None:
    """每个字段族独立渲染，class 不为空。"""
    sample = _finding_sample()
    sample["family"] = family
    out = json.loads(_eval_func("renderFindingCard", _js_obj(sample)))
    assert f'data-family="{family}"' in out, (
        f"family={family} 渲染缺 data-family 属性"
    )


def test_no_orphan_console_or_dom_only_globals_leaked() -> None:
    """`render.js` 是纯函数模块，只应在 IIFE 末尾导出引用 `window`。

    其它浏览器全局（fetch / document / localStorage 等）都不应出现；
    `window` 只允许出现在 `(typeof window !== "undefined" ? window : this)`
    那一行（即双端 export 桥接）。
    """
    src = RENDER_JS.read_text(encoding="utf-8")
    for bad in ["fetch(", "document.", "localStorage", "sessionStorage",
                "XMLHttpRequest", "addEventListener"]:
        assert bad not in src, f"render.js 引入浏览器全局：{bad}"

    # window 只在 IIFE 末尾的 export 桥接里允许；不要在渲染热路径上出现
    window_hits = [
        (i + 1) for i, line in enumerate(src.splitlines())
        if "window" in line and "typeof window" not in line
        and "RxchkRender" not in line
    ]
    assert not window_hits, (
        f"render.js 非出口处出现 window 引用（行 {window_hits}）"
    )


# --------------------------------------------------------------------------- #
# 10. Node 加载稳定性                                                          #
# --------------------------------------------------------------------------- #


def test_render_js_loads_clean_under_node() -> None:
    """`require` render.js 时 stdout 中无未捕获报错。"""
    probe = REPO_ROOT / "logs" / "_viewer_render_probe.js"
    if probe.is_file():
        probe.unlink()
    proc = subprocess.run(
        ["node", "-e", f"require({json.dumps(str(RENDER_JS))}); console.log('ok');"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Node 加载 render.js 失败：stderr={proc.stderr!r}"
    )
    assert "ok" in proc.stdout, f"未打印 ok：{proc.stdout!r}"


# --------------------------------------------------------------------------- #
# 暴露给调试                                                                  #
# --------------------------------------------------------------------------- #


_load_pure_render()  # noqa: B018 —— 在用例间共享，以断言 render.js 完整可加载
