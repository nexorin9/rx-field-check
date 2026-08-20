"""Viewer 前端增量更新（DOM diff）纯函数测试。

任务 26 — `src/rxchk/web/app.js` + `render.js` 的增量补丁层

测试目标
--------
1s 轮询下不能每轮 `innerHTML` 全量重绘（会丢滚动位置、丢焦点、闪烁），
因此把「上轮 findings + 本轮 findings + audit.jsonl → 补丁列表」抽成
`render.js` 的纯函数 `planGridPatches()`，由 Python 直接断言：

- 新增 / 更新 / 移除 / 决策着色四类补丁精确；
- 字段族块的增删与 `FAMILY_ORDER` 位次稳定；
- 同族前缀匹配（`resolveFamily`）：新字段名无需改派发代码即可归族；
- `app.js` 侧确实按补丁打 DOM（结构性断言，非全量重绘）。

不依赖 jsdom / 无头浏览器：纯函数经 `node` 子进程求值。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "src" / "rxchk" / "web"
RENDER_JS = WEB_DIR / "render.js"
APP_JS = WEB_DIR / "app.js"


# --------------------------------------------------------------------------- #
# node 求值探针                                                                #
# --------------------------------------------------------------------------- #


def _call(func_name: str, *args):
    """`require(render.js)[func_name](...args)` → JSON 解码后的 Python 值。"""
    args_js = ",".join(json.dumps(a, ensure_ascii=False) for a in args)
    script = (
        f"const api = require({json.dumps(str(RENDER_JS))});\n"
        f"const out = api.{func_name}({args_js});\n"
        'process.stdout.write("__OUT__" + JSON.stringify(out));\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as fh:
        fh.write(script)
        probe = Path(fh.name)
    try:
        proc = subprocess.run(
            ["node", str(probe)], capture_output=True, text=True, timeout=20, check=False
        )
    except FileNotFoundError as exc:  # pragma: no cover - 环境缺 node
        pytest.skip(f"Node.js 不可用：{exc}")
    finally:
        probe.unlink(missing_ok=True)

    if proc.returncode != 0:
        pytest.fail(f"render.js 执行 {func_name} 失败：\nstderr: {proc.stderr}")
    marker = proc.stdout.find("__OUT__")
    if marker < 0:
        pytest.fail(f"{func_name} 返回缺少 __OUT__ 前缀：{proc.stdout!r}")
    return json.loads(proc.stdout[marker + len("__OUT__"):])


def _finding(rule_id: str, field: str, value: str, family: str, severity: str = "high") -> dict:
    return {
        "prescription_no": "RX-<EXAMPLE>",
        "field": field,
        "field_value": value,
        "family": family,
        "rule_id": rule_id,
        "rule_version": "2026Q1",
        "hit_explanation": "命中院内规则",
        "evidence": "规则条目摘录",
        "severity": severity,
        "source": "rule_adapter",
        "agent_id": f"{family}_agent",
        "created_at": "2026-08-20T00:00:00Z",
    }


def _ids(patches, op: str) -> list:
    return [p["finding_id"] for p in patches if p["op"] == op]


def _ops(patches) -> list:
    return [p["op"] for p in patches]


# --------------------------------------------------------------------------- #
# 1. 同族前缀匹配（CategoryMeta.match 语义）                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name,expected", [
    ("drug", "drug"),                    # 精确族名
    ("dose", "dose"),
    ("max_daily_dose", "dose"),          # 同族前缀：新字段无需改派发代码
    ("dose_limit", "dose"),
    ("admin_route", "route"),
    ("freq_per_day", "frequency"),
    ("allergen_code", "allergy"),
    ("interaction_pair", "interaction"),
    ("pregnancy_flag", "population"),
    ("renal_function", "population"),
    ("diagnosis_code", "indication"),
    ("totally_unknown_field", "uncovered"),  # 都不命中 → 兜底
    ("", "uncovered"),
])
def test_resolve_family_prefix_matching(name: str, expected: str) -> None:
    assert _call("resolveFamily", name) == expected


def test_family_of_prefers_explicit_family_then_field_name() -> None:
    """显式 family 优先；缺失时由 field 名同族推断；都不成立才 uncovered。"""
    assert _call("familyOf", _finding("R1", "whatever", "x", "allergy")) == "allergy"
    no_family = {"rule_id": "R2", "field": "max_daily_dose", "field_value": "5g"}
    assert _call("familyOf", no_family) == "dose"
    unknown = {"rule_id": "R3", "field": "zzz_unknown", "field_value": "v"}
    assert _call("familyOf", unknown) == "uncovered"


# --------------------------------------------------------------------------- #
# 2. decisionIndex：audit.jsonl append-only → 最后一次决策生效                 #
# --------------------------------------------------------------------------- #


def test_decision_index_last_write_wins() -> None:
    audits = [
        {"finding_id": "finding.R1.dose.5g", "action": "confirm", "operator": "p1"},
        {"finding_id": "finding.R1.dose.5g", "action": "override", "operator": "p2"},
        {"finding_id": "finding.R2.drug.A", "action": "reject", "operator": "p1"},
        {"action": "confirm", "operator": "p1"},  # 处方级操作：无 finding_id，忽略
    ]
    index = _call("decisionIndex", audits)
    assert index == {
        "finding.R1.dose.5g": "is-overridden",
        "finding.R2.drug.A": "is-rejected",
    }, index


def test_decision_index_ignores_unknown_action() -> None:
    index = _call("decisionIndex", [{"finding_id": "f1", "action": "noop"}])
    assert index == {}, index


# --------------------------------------------------------------------------- #
# 3. planGridPatches：新增 / 更新 / 移除 / 着色                                #
# --------------------------------------------------------------------------- #


def test_patches_from_empty_to_two_families() -> None:
    """首帧：两个族 → 2 个 family_add + 2 个 card_add + 族计数。"""
    nxt = [
        _finding("R-DRUG-1", "drug_code", "<EXAMPLE_DRUG_A>", "drug"),
        _finding("R-DOSE-1", "dose", "5g", "dose"),
    ]
    patches = _call("planGridPatches", [], nxt, [])
    ops = _ops(patches)
    assert ops.count("family_add") == 2, ops
    assert ops.count("card_add") == 2, ops
    assert ops.count("card_remove") == 0 and ops.count("card_update") == 0, ops
    # 族块按 FAMILY_ORDER 位次给出 index：drug(0) 先于 dose(1)
    fam_adds = [p for p in patches if p["op"] == "family_add"]
    assert [p["family"] for p in fam_adds] == ["drug", "dose"], fam_adds
    assert [p["index"] for p in fam_adds] == [0, 1], fam_adds
    # card_add 自带渲染好的单卡 HTML，DOM 侧只需 insertAdjacentHTML 一张
    assert all(p["html"].startswith("<article class=\"field-card") for p in patches
               if p["op"] == "card_add")


def test_patches_no_change_between_identical_polls() -> None:
    """内容未变的一轮轮询不应产生任何补丁 —— 这正是不全量重绘的意义。"""
    items = [
        _finding("R-DRUG-1", "drug_code", "<EXAMPLE_DRUG_A>", "drug"),
        _finding("R-DOSE-1", "dose", "5g", "dose"),
    ]
    patches = _call("planGridPatches", items, items, [])
    assert patches == [], f"稳态轮询产生了多余补丁：{_ops(patches)}"


def test_patches_incremental_add_only_new_card() -> None:
    """新增一条 finding → 只有 1 个 card_add，旧卡片不动。"""
    prev = [_finding("R-DRUG-1", "drug_code", "<EXAMPLE_DRUG_A>", "drug")]
    nxt = prev + [_finding("R-ALG-1", "allergen_code", "<EXAMPLE_ALLERGEN>", "allergy")]
    patches = _call("planGridPatches", prev, nxt, [])
    adds = [p for p in patches if p["op"] == "card_add"]
    assert len(adds) == 1 and adds[0]["family"] == "allergy", patches
    assert _ops(patches).count("card_update") == 0
    assert [p["family"] for p in patches if p["op"] == "family_add"] == ["allergy"]


def test_patches_update_when_same_id_content_changes() -> None:
    """同 finding_id 但严重度 / 规则版本变化 → card_update（不是 remove+add）。"""
    prev = [_finding("R-DOSE-1", "dose", "5g", "dose", severity="medium")]
    changed = _finding("R-DOSE-1", "dose", "5g", "dose", severity="high")
    changed["rule_version"] = "2026Q2"
    patches = _call("planGridPatches", prev, [changed], [])
    assert _ops(patches) == ["card_update"], patches
    assert patches[0]["finding_id"].startswith("finding.R-DOSE-1.dose."), patches
    assert "severity-high" in patches[0]["html"]


def test_patches_remove_card_and_empty_family_block() -> None:
    """最后一条 finding 消失 → card_remove + family_remove。"""
    prev = [
        _finding("R-DRUG-1", "drug_code", "<EXAMPLE_DRUG_A>", "drug"),
        _finding("R-DOSE-1", "dose", "5g", "dose"),
    ]
    nxt = [prev[0]]
    patches = _call("planGridPatches", prev, nxt, [])
    ops = _ops(patches)
    assert ops.count("card_remove") == 1, patches
    assert [p["family"] for p in patches if p["op"] == "family_remove"] == ["dose"], patches
    # 移除补丁排在新增之前，避免同 id 冲突
    assert ops.index("card_remove") < len(ops)


def test_patches_removal_precedes_addition() -> None:
    """删 + 增同帧时，card_remove 必须先于 card_add 下发。"""
    prev = [_finding("R-OLD", "dose", "5g", "dose")]
    nxt = [_finding("R-NEW", "dose", "8g", "dose")]
    ops = _ops(_call("planGridPatches", prev, nxt, []))
    assert "card_remove" in ops and "card_add" in ops
    assert ops.index("card_remove") < ops.index("card_add"), ops


def test_patches_family_count_tracks_card_number() -> None:
    prev = [_finding("R-DOSE-1", "dose", "5g", "dose")]
    nxt = prev + [_finding("R-DOSE-2", "max_daily_dose", "12g", "dose")]
    patches = _call("planGridPatches", prev, nxt, [])
    counts = [p for p in patches if p["op"] == "family_count"]
    assert counts and counts[0] == {"op": "family_count", "family": "dose", "count": 2}, patches


def test_patches_carry_decision_class_for_audited_findings() -> None:
    """药师确认 / 放行 / 驳回 → card_decision 补丁带上对应 CSS 类。"""
    items = [
        _finding("R-DOSE-1", "dose", "5g", "dose"),
        _finding("R-ALG-1", "allergen_code", "<EXAMPLE_ALLERGEN>", "allergy"),
    ]
    dose_id = _call("findingId", items[0])
    allergy_id = _call("findingId", items[1])
    audits = [
        {"finding_id": dose_id, "action": "confirm", "operator": "pharmacist-001"},
        {"finding_id": allergy_id, "action": "reject", "operator": "pharmacist-001"},
        {"finding_id": "finding.GONE.x.y", "action": "confirm"},  # 已消失的卡：不下发
    ]
    patches = _call("planGridPatches", items, items, audits)
    decisions = {p["finding_id"]: p["class"] for p in patches if p["op"] == "card_decision"}
    assert decisions == {dose_id: "is-confirmed", allergy_id: "is-rejected"}, patches


def test_patches_are_deterministic_for_same_input() -> None:
    """同输入必得同输出（顺序稳定）—— 否则 DOM 会无意义抖动。"""
    prev = [_finding("R1", "drug_code", "A", "drug")]
    nxt = [
        _finding("R1", "drug_code", "A", "drug"),
        _finding("R2", "dose", "5g", "dose"),
        _finding("R3", "allergen_code", "B", "allergy"),
    ]
    first = _call("planGridPatches", prev, nxt, [])
    second = _call("planGridPatches", prev, nxt, [])
    assert first == second, "planGridPatches 输出不稳定"


def test_patches_handle_uncovered_family() -> None:
    """规则未覆盖的字段落 uncovered 族，仍走同一条补丁通道。"""
    nxt = [_finding("R-UNC-1", "zzz_unknown", "v", "uncovered", severity="info")]
    patches = _call("planGridPatches", [], nxt, [])
    fam_adds = [p for p in patches if p["op"] == "family_add"]
    assert fam_adds and fam_adds[0]["family"] == "uncovered", patches
    card = next(p for p in patches if p["op"] == "card_add")
    assert "uncovered" in card["html"] and "severity-info" in card["html"]


# --------------------------------------------------------------------------- #
# 4. app.js 结构性断言：确实按补丁打 DOM                                       #
# --------------------------------------------------------------------------- #


def test_app_js_applies_patches_instead_of_full_repaint() -> None:
    src = APP_JS.read_text(encoding="utf-8")
    for marker in [
        "Render.planGridPatches(",       # 走纯函数算补丁
        "function applyPatches(",        # 补丁 → DOM
        'case "card_add"',
        'case "card_update"',
        'case "card_remove"',
        'case "card_decision"',
        'case "family_add"',
        'case "family_remove"',
        "insertAdjacentHTML",            # 单卡插入，非整块重写
        "replaceChild",                  # 原地替换单卡
    ]:
        assert marker in src, f"app.js 缺增量更新关键符号：{marker}"

    # 轮询热路径不得再对 #family-grid 做全量 innerHTML 重绘
    assert "grid.innerHTML = Render.renderFamilyGrid(groups)" not in src, (
        "app.js 仍在轮询中全量重绘 family-grid"
    )


def test_app_js_marks_decision_classes() -> None:
    src = APP_JS.read_text(encoding="utf-8")
    assert "function markDecision(" in src
    for cls in ["is-confirmed", "is-overridden", "is-rejected"]:
        assert cls in src, f"app.js 缺决策着色类：{cls}"
    # 确认动作提交后立即着色
    assert "Render.decisionClass(applied)" in src
    assert "POLL_INTERVAL_MS = 1000" in src


def test_index_html_loads_render_before_app() -> None:
    """render.js 必须先于 app.js 加载，否则 window.RxchkRender 缺失。"""
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'src="render.js"' in html and 'src="app.js"' in html
    assert html.index('src="render.js"') < html.index('src="app.js"'), (
        "index.html 中 render.js 应排在 app.js 之前"
    )
