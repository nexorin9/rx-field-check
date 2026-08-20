"""CSS 样式表契约测试。

viewer 字段证据卡渲染依赖 style.css 中的类名 / 变量 / 选择器；
这是面向浏览器的纯静态文件，须保证：

- 8 字段族配色变量（含 uncovered 兜底）均存在
- severity 4 档变量 + 选择器类名均存在
- 字段证据卡布局 / 药师确认模态相关类名均存在
- CSS 关键选择器在文件内可被 grep 命中（防止误删）
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "rxchk" / "web"
CSS_PATH = WEB_DIR / "style.css"


def _read_css() -> str:
    assert CSS_PATH.exists(), f"style.css missing: {CSS_PATH}"
    return CSS_PATH.read_text(encoding="utf-8")


# ───────────────────────────────────────────── 8 字段族配色 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "family",
    [
        "drug",
        "dose",
        "frequency",
        "route",
        "indication",
        "allergy",
        "interaction",
        "population",
        "uncovered",
    ],
)
def test_family_color_variable_declared(family: str) -> None:
    """每个字段族必须有 --family-<name> 变量声明。"""
    css = _read_css()
    pattern = rf"--family-{re.escape(family)}\s*:"
    assert re.search(pattern, css), f"--family-{family} 变量未声明"


@pytest.mark.parametrize(
    "family",
    [
        "drug",
        "dose",
        "frequency",
        "route",
        "indication",
        "allergy",
        "interaction",
        "population",
        "uncovered",
    ],
)
def test_family_block_selector_present(family: str) -> None:
    """所有字段族都应有 [data-family='<name>'] 选择器。"""
    css = _read_css()
    pattern = rf'\[data-family=["\']?{re.escape(family)}["\']?\]'
    assert re.search(pattern, css), f"data-family='{family}' 选择器缺失"


def test_all_eight_color_families_get_renderer() -> None:
    """8 字段族 + uncovered 必须独立配色，不能共享一个变量。"""
    css = _read_css()
    families = [
        "drug",
        "dose",
        "frequency",
        "route",
        "indication",
        "allergy",
        "interaction",
        "population",
        "uncovered",
    ]
    declared = set()
    for fam in families:
        if re.search(rf"--family-{re.escape(fam)}\s*:", css):
            declared.add(fam)
    assert declared == set(families), f"声明不全：{set(families) - declared}"


# ───────────────────────────────────────────── severity 4 档 ─────────────────────────────────────────────


@pytest.mark.parametrize("level", ["high", "medium", "low", "info"])
def test_severity_variable_declared(level: str) -> None:
    css = _read_css()
    pattern = rf"--severity-{re.escape(level)}\s*:"
    assert re.search(pattern, css), f"--severity-{level} 变量缺失"


@pytest.mark.parametrize("level", ["high", "medium", "low", "info"])
def test_severity_class_selector_present(level: str) -> None:
    """字段证据卡与 severity-pill 都需要 .severity-<level> 选择器。"""
    css = _read_css()
    matches = re.findall(rf"\.severity-{re.escape(level)}\b", css)
    assert matches, f".severity-{level} 选择器缺失"


def test_severity_pill_component_present() -> None:
    """severity 色阶组件类 .severity-pill 必须存在。"""
    css = _read_css()
    assert ".severity-pill" in css
    # 至少 4 档 level 都应用到 pill
    for level in ["high", "medium", "low", "info"]:
        assert re.search(rf"\.severity-pill\.severity-{level}", css), f"pill.{level} 缺失"


# ───────────────────────────────────────────── uncovered 黄底 ─────────────────────────────────────────────


def test_uncovered_yellow_background_present() -> None:
    """未覆盖字段族须有黄底提示（用于视觉降级而非吞错）。"""
    css = _read_css()
    # 兜底族既有 --family-uncovered 变量，也应有黄底 class
    assert re.search(r"\.uncovered\s*\{", css), ".uncovered 类缺失"
    # 黄底特征：含 #fef9c3 / #fffbe6 / #facc15 / yellow / #fef3c7 之一
    yellow_palette = [
        "#fef9c3",
        "#fffbe6",
        "#facc15",
        "#fef3c7",
        "#fde68a",
    ]
    assert any(color in css.lower() for color in yellow_palette), "uncovered 缺失黄底配色"


def test_field_card_uncovered_combined() -> None:
    """字段证据卡与 uncovered 必须联动：.field-card.uncovered。"""
    css = _read_css()
    assert re.search(r"\.field-card\.uncovered", css), ".field-card.uncovered 联动选择器缺失"


# ───────────────────────────────────────────── 字段证据卡布局 ─────────────────────────────────────────────


def test_field_card_layout_classes() -> None:
    """字段证据卡布局依赖一组核心类名。"""
    css = _read_css()
    required = [
        ".field-card",
        ".family-grid",
        ".family-block",
        ".family-head",
        ".family-cards",
        ".card-row",
        ".card-label",
        ".card-value",
        ".card-evidence",
        ".card-explanation",
        ".card-source",
    ]
    for cls in required:
        assert cls in css, f"布局类 {cls} 缺失"


def test_field_card_uses_grid_layout() -> None:
    """字段证据卡网格应有 auto-fill 自适应布局。"""
    css = _read_css()
    assert re.search(r"grid-template-columns\s*:\s*repeat\(auto-fill", css), \
        "字段卡 grid 布局缺失（auto-fill）"


def test_family_block_uses_data_family_attribute() -> None:
    """族配色通过 [data-family='...'] 选择器动态注入。"""
    css = _read_css()
    matches = re.findall(r'\[data-family=["\']?[a-z]+["\']?\]', css)
    # 至少 9 条（8 字段族 + uncovered 兜底）
    assert len(matches) >= 9, f"data-family 选择器仅 {len(matches)} 条（>=9）"


# ───────────────────────────────────────────── 确认模态 ─────────────────────────────────────────────


def test_modal_classes_present() -> None:
    """确认 / 备注 / 驳回模态所需类。"""
    css = _read_css()
    required = [
        ".modal-mask",
        ".modal",
        ".modal-header",
        ".modal-body",
        ".modal-form",
        ".modal-actions",
        ".modal-close",
        ".modal-evidence",
        ".modal-hint",
    ]
    for cls in required:
        assert cls in css, f"模态类 {cls} 缺失"


def test_modal_overlay_full_screen() -> None:
    """遮罩层应覆盖整屏（fixed + inset 0）。"""
    css = _read_css()
    m = re.search(r"\.modal-mask\s*\{([^}]+)\}", css)
    assert m, ".modal-mask 规则未找到"
    body = m.group(1)
    assert "fixed" in body and "inset: 0" in body.replace("inset:0", "inset: 0"), \
        ".modal-mask 缺少 fixed / inset:0"


def test_modal_actions_three_buttons() -> None:
    """模态三个动作按钮（确认 / 放行 / 驳回）样式齐备。"""
    css = _read_css()
    for cls in [".btn-primary", ".btn-warning", ".btn-danger"]:
        assert cls in css, f"按钮类 {cls} 缺失"


# ───────────────────────────────────────────── 状态机 / 连接态颜色 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "token",
    [
        "is-confirmed",
        "is-overridden",
        "is-rejected",
        "is-awaiting_review",
        "is-running",
        "is-written_back",
    ],
)
def test_status_class_present(token: str) -> None:
    """状态机 / 药师决策 CSS hook 必须都在。"""
    css = _read_css()
    assert token in css, f"状态类 {token} 缺失"


def test_connection_state_classes() -> None:
    """连接态：ok / stalled / error / unauthed 四态。"""
    css = _read_css()
    for state in ["is-ok", "is-stalled", "is-error", "is-unauthed"]:
        assert state in css, f"连接态 {state} 缺失"


# ───────────────────────────────────────────── CSS 体积与可读性 ─────────────────────────────────────────────


def test_css_not_empty() -> None:
    """非空校验——防空白文件通过。"""
    text = _read_css()
    assert len(text) > 2000, f"style.css 过小（{len(text)} 字符），可能未写完整"


def test_css_has_documentation_header() -> None:
    """文件应有文档头注释，便于维护者看懂色板语义。"""
    head = _read_css().lstrip()
    assert head.startswith("/*") or head.startswith("/**"), "缺少文档头注释"


def test_no_hardcoded_severity_color_outside_vars() -> None:
    """severity 4 档颜色应在 :root 变量里集中管理，不应大量散落硬编码。"""
    css = _read_css()
    # 除 :root 块外，severity 4 档的 hex 字面量应很少（容忍 1 次，比如 fallback）
    body_part = re.sub(r":root\s*\{[^}]*\}", "", css, flags=re.S)
    drift_count = sum(
        body_part.lower().count(hexc.lower())
        for hexc in ["#dc2626", "#ea580c", "#2563eb", "#6b7280"]
    )
    assert drift_count <= 4, f"severity 颜色疑似散落硬编码（计数 {drift_count}）"
