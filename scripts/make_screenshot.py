"""程序化生成 Viewer 初始页面截图占位 PNG。

无 GUI / 无浏览器环境下，用 Pillow 画出 viewer 布局示意：
- 顶栏（处方号 / 状态徽章 / 进度）
- 历史处方下拉
- 三栏：字段证据卡 / 核对事件流 / 药师操作记录
- 模态（确认 / 放行 / 驳回）
- 页脚连接状态

仅用于 README 截图占位（无 Playwright / Chromium 依赖）。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "screenshots"
OUT_DIR.mkdir(parents=True, exist_ok=True)

WIDTH, HEIGHT = 1280, 800

PALETTE = {
    "bg": (245, 247, 250),
    "panel": (255, 255, 255),
    "border": (217, 222, 230),
    "text": (31, 41, 55),
    "muted": (107, 114, 128),
    "primary": (37, 99, 235),
    "warning": (217, 119, 6),
    "danger": (220, 38, 38),
    "success": (22, 163, 74),
    "topbar": (243, 244, 246),
    "family_drug": (59, 130, 246),
    "family_dose": (220, 38, 38),
    "family_freq": (245, 158, 11),
    "family_route": (168, 85, 247),
    "family_indication": (14, 165, 233),
    "family_allergy": (236, 72, 153),
    "family_interaction": (34, 197, 94),
    "family_population": (124, 58, 237),
    "uncovered": (250, 204, 21),
}


def _font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _panel(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int) -> None:
    draw.rectangle([x, y, x + w, y + h], fill=PALETTE["panel"], outline=PALETTE["border"])


def _text(draw, x, y, h, text, *, color, font) -> None:
    draw.text((x, y), text, fill=color, font=font)


def render() -> Path:
    img = Image.new("RGB", (WIDTH, HEIGHT), PALETTE["bg"])
    draw = ImageDraw.Draw(img)

    f_title = _font(20)
    f_h2 = _font(16)
    f_body = _font(13)
    f_small = _font(11)
    f_tiny = _font(10)

    # 顶栏
    draw.rectangle([0, 0, WIDTH, 56], fill=PALETTE["topbar"])
    _text(draw, 24, 16, 20, "用药处方字段对照校验系统", color=PALETTE["text"], font=f_title)
    _text(draw, 360, 20, 16, "处方号  RX-2026-<EXAMPLE_COMPLEX>", color=PALETTE["muted"], font=f_body)
    # 状态徽章 awaiting_review
    badge_x, badge_y = 660, 16
    draw.rectangle([badge_x, badge_y, badge_x + 130, badge_y + 26], fill=(254, 243, 199), outline=PALETTE["warning"])
    _text(draw, badge_x + 12, badge_y + 5, 16, "状态  awaiting_review", color=PALETTE["warning"], font=f_body)
    _text(draw, 820, 20, 16, "核对  5 findings / 8 字段族", color=PALETTE["muted"], font=f_body)
    _text(draw, 1100, 20, 16, "Viewer 鉴权  ● 启用", color=PALETTE["success"], font=f_body)

    # 历史处方下拉
    _panel(draw, 24, 76, WIDTH - 48, 44)
    _text(draw, 40, 88, 14, "历史处方核对  ▼  RX-2026-<EXAMPLE_COMPLEX>", color=PALETTE["text"], font=f_body)
    _text(draw, 900, 88, 14, "（共 4 条）", color=PALETTE["muted"], font=f_small)

    # 三栏
    panel_y = 140
    panel_h = 540
    findings_x = 24
    findings_w = 760
    transcript_x = 800
    transcript_w = 220
    audit_x = 1036
    audit_w = 220

    # 字段证据卡
    _panel(draw, findings_x, panel_y, findings_w, panel_h)
    _text(draw, findings_x + 16, panel_y + 12, 16, "字段证据卡（5）", color=PALETTE["text"], font=f_h2)
    _text(draw, findings_x + 16, panel_y + 36, 12, "每张卡片对应一个字段核对命中；点击卡片由药师确认、放行或驳回。", color=PALETTE["muted"], font=f_small)

    # 8 字段族卡片网格
    families = [
        ("dose", PALETTE["family_dose"], "超日剂量上限", "R-DOSE-007-SINGLE", "high", "<EXAMPLE_DRUG_A> 1200mg > 800mg"),
        ("interaction", PALETTE["family_interaction"], "三药累加禁忌", "R-INT-004-COMBO", "high", "ASP+IBUP+WAR 出血风险 contraindicated"),
        ("allergy", PALETTE["family_allergy"], "家族交叉命中", "A-ASP-002-CROSS", "high", "aspirin 过敏 → A-ASP cross → moderate"),
        ("indication", PALETTE["family_indication"], "诊断与适应症不符", "R-IND-002", "medium", "WAR 与诊断 J18.900 无交集"),
        ("drug", PALETTE["family_drug"], "药品名称命中", "R-DRUG-NAME-001", "low", "<EXAMPLE_DRUG_A> 字典已登记"),
    ]

    card_x = findings_x + 16
    card_y = panel_y + 64
    card_w = findings_w - 32
    card_h = 88

    for i, (fam, color, title, rule_id, severity, evidence) in enumerate(families):
        y = card_y + i * (card_h + 8)
        # 字段族色条
        draw.rectangle([card_x, y, card_x + 6, y + card_h], fill=color)
        # severity 色阶
        sev_color = {"high": PALETTE["danger"], "medium": PALETTE["warning"], "low": PALETTE["primary"], "info": PALETTE["muted"]}[severity]
        draw.rectangle([card_x + 12, y + 12, card_x + 12 + 50, y + 12 + 18], fill=sev_color)
        _text(draw, card_x + 18, y + 14, 10, severity.upper(), color=(255, 255, 255), font=f_tiny)

        # 标题
        _text(draw, card_x + 80, y + 12, 16, f"[{fam}]  {title}", color=PALETTE["text"], font=f_h2)
        _text(draw, card_x + 80, y + 36, 12, f"规则  {rule_id}  ·  版本  v2026.07", color=PALETTE["muted"], font=f_small)
        _text(draw, card_x + 80, y + 54, 12, f"证据  {evidence}", color=PALETTE["text"], font=f_small)
        _text(draw, card_x + 80, y + 70, 10, "来源  rule_adapter  ·  agent-dose-001", color=PALETTE["muted"], font=f_tiny)

    # 核对事件流
    _panel(draw, transcript_x, panel_y, transcript_w, panel_h)
    _text(draw, transcript_x + 12, panel_y + 12, 14, "核对事件流", color=PALETTE["text"], font=f_h2)
    events = [
        ("10:30:14", "input", "RX-2026-<EXAMPLE_COMPLEX>"),
        ("10:30:14", "parse", "8 字段族 Agent 注册"),
        ("10:30:14", "dispatch", "drug / dose / freq / route"),
        ("10:30:14", "dispatch", "indication / allergy"),
        ("10:30:14", "dispatch", "interaction / population"),
        ("10:30:15", "agent_finish", "dose → 3 findings"),
        ("10:30:15", "agent_finish", "interaction → 1 finding"),
        ("10:30:15", "agent_finish", "allergy → 1 finding"),
        ("10:30:15", "state", "→ awaiting_review"),
    ]
    for i, (ts, kind, msg) in enumerate(events):
        y = panel_y + 40 + i * 28
        _text(draw, transcript_x + 12, y, 10, ts, color=PALETTE["muted"], font=f_tiny)
        kind_color = {
            "input": PALETTE["primary"],
            "parse": PALETTE["muted"],
            "dispatch": PALETTE["muted"],
            "agent_finish": PALETTE["success"],
            "state": PALETTE["warning"],
        }[kind]
        _text(draw, transcript_x + 12, y + 12, 11, f"{kind}  {msg}", color=kind_color, font=f_small)

    # 药师操作记录
    _panel(draw, audit_x, panel_y, audit_w, panel_h)
    _text(draw, audit_x + 12, panel_y + 12, 14, "药师操作记录", color=PALETTE["text"], font=f_h2)
    _text(draw, audit_x + 12, panel_y + 36, 10, "（飞检追溯）", color=PALETTE["muted"], font=f_tiny)
    _text(draw, audit_x + 12, panel_y + 60, 11, "暂无操作", color=PALETTE["muted"], font=f_small)
    _text(draw, audit_x + 12, panel_y + 80, 10, "每次 confirm / 备注 / 驳回", color=PALETTE["muted"], font=f_tiny)
    _text(draw, audit_x + 12, panel_y + 96, 10, "都会 append-only 写入", color=PALETTE["muted"], font=f_tiny)
    _text(draw, audit_x + 12, panel_y + 112, 10, "audit.jsonl。", color=PALETTE["muted"], font=f_tiny)

    # 模态（药师决策）
    mask = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 120))
    img.paste(mask, (0, 0), mask)
    modal_w, modal_h = 460, 280
    mx = (WIDTH - modal_w) // 2
    my = (HEIGHT - modal_h) // 2
    draw.rectangle([mx, my, mx + modal_w, my + modal_h], fill=(255, 255, 255), outline=PALETTE["border"])
    _text(draw, mx + 20, my + 16, 16, "字段决策 · dose · R-DOSE-007-SINGLE", color=PALETTE["text"], font=f_h2)
    _text(draw, mx + 20, my + 44, 12, "证据  <EXAMPLE_DRUG_A> 1200mg > 800mg（v2026.07）", color=PALETTE["muted"], font=f_small)
    _text(draw, mx + 20, my + 72, 12, "操作药师  ______________________________", color=PALETTE["text"], font=f_small)
    _text(draw, mx + 20, my + 100, 12, "备注  ______________________________________", color=PALETTE["text"], font=f_small)
    # 按钮
    btn_y = my + modal_h - 50
    draw.rectangle([mx + 20, btn_y, mx + 110, btn_y + 30], fill=PALETTE["success"])
    _text(draw, mx + 50, btn_y + 7, 14, "确认", color=(255, 255, 255), font=f_body)
    draw.rectangle([mx + 120, btn_y, mx + 220, btn_y + 30], fill=PALETTE["warning"])
    _text(draw, mx + 150, btn_y + 7, 14, "放行", color=(255, 255, 255), font=f_body)
    draw.rectangle([mx + 230, btn_y, mx + 330, btn_y + 30], fill=PALETTE["danger"])
    _text(draw, mx + 260, btn_y + 7, 14, "驳回", color=(255, 255, 255), font=f_body)
    draw.rectangle([mx + 340, btn_y, mx + 430, btn_y + 30], outline=PALETTE["muted"])
    _text(draw, mx + 370, btn_y + 7, 14, "取消", color=PALETTE["muted"], font=f_body)

    # 页脚
    _text(draw, 24, HEIGHT - 24, 11, "●  Viewer 已启用 token 鉴权 · 轮询间隔 ~1s · 字段证据卡 · 飞检追溯就绪", color=PALETTE["muted"], font=f_small)

    out_path = OUT_DIR / "viewer_initial.png"
    img.save(out_path, format="PNG", optimize=True)
    return out_path


if __name__ == "__main__":
    p = render()
    print(f"saved: {p} ({p.stat().st_size} bytes)")