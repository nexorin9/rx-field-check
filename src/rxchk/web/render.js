/* 用药处方字段对照校验系统 — 纯渲染函数模块（无 DOM 依赖）
   ─────────────────────────────────────────────────────────────
   浏览器与 Node 都可加载：
   - 浏览器：自动挂到 window.RxchkRender
   - Node：module.exports = RxchkRender
   测试（tests/test_viewer_static.py）以 stdlib 模拟：直接 Read 该文件，
   借 Node 的 Function 构造器执行得到 pure 函数。
*/

(function (root) {
  "use strict";

  // ───────── 字段族元数据 ─────────
  // 与 spec.md 的 FieldFamily 枚举 8 + uncovered 兜底一一对应；css var
  // 与 style.css --family-* 也以此为依据。
  var FAMILY_META = {
    drug:         { key: "drug",        label: "药品",        order: 1 },
    dose:         { key: "dose",        label: "剂量",        order: 2 },
    frequency:    { key: "frequency",   label: "频次",        order: 3 },
    route:        { key: "route",       label: "给药途径",    order: 4 },
    indication:   { key: "indication",  label: "适应症",      order: 5 },
    allergy:      { key: "allergy",     label: "过敏",        order: 6 },
    interaction:  { key: "interaction", label: "配伍",        order: 7 },
    population:   { key: "population",  label: "特殊人群",    order: 8 },
    uncovered:    { key: "uncovered",   label: "规则未覆盖",  order: 99 },
  };

  // 顺序固定，便于稳定渲染与测试断言
  var FAMILY_ORDER = [
    "drug", "dose", "frequency", "route",
    "indication", "allergy", "interaction", "population",
    "uncovered",
  ];

  // ───────── 字段族同族前缀匹配（family matcher）─────────
  // 与后端 tools/registry.py 的 CategoryMeta.match 同一套语义：
  // 一个字段族描述一次，新增的同族字段名（例如再加一个 *_dose_limit 字段）
  // 无需改派发代码即可落到对应族的渲染器上；都不命中才落 uncovered 兜底。
  var FAMILY_MATCHERS = {
    drug:        /(^|_)(drug|medicine|generic|trade)(_|$)/,
    dose:        /(^|_)(dose|dosage|amount|strength)(_|$)/,
    frequency:   /(^|_)(freq|frequency|interval|schedule)(_|$)/,
    route:       /(^|_)(route|administration|form)(_|$)/,
    indication:  /(^|_)(indication|diagnos\w*|purpose)(_|$)/,
    allergy:     /(^|_)(allergy|allergen|hypersens\w*)(_|$)/,
    interaction: /(^|_)(interaction|combo|compat\w*)(_|$)/,
    population:  /(^|_)(population|pregnan\w*|pediatric|renal|hepatic|geriatric)(_|$)/,
  };

  // 名称 → 字段族：先精确命中 FAMILY_META，再走同族前缀匹配，最后 uncovered
  function resolveFamily(name) {
    var key = String(name == null ? "" : name).toLowerCase();
    if (!key) return "uncovered";
    if (FAMILY_META[key]) return key;
    for (var i = 0; i < FAMILY_ORDER.length; i++) {
      var fam = FAMILY_ORDER[i];
      var matcher = FAMILY_MATCHERS[fam];
      if (matcher && matcher.test(key)) return fam;
    }
    return "uncovered";
  }

  // finding → 归属字段族：显式 family 优先，其次由 field 名同族匹配推断
  function familyOf(f) {
    if (!f) return "uncovered";
    if (f.family) {
      var explicit = resolveFamily(f.family);
      if (explicit !== "uncovered") return explicit;
      // 显式写了 uncovered / 未知族，保持 uncovered 语义
      if (String(f.family).toLowerCase() === "uncovered") return "uncovered";
    }
    return resolveFamily(f.field);
  }

  // ───────── Severity → CSS 类 ─────────
  function severityClass(severity) {
    var s = String(severity || "info").toLowerCase();
    return ["high", "medium", "low", "info"].indexOf(s) >= 0 ? "severity-" + s : "severity-info";
  }

  // 给证据卡用的 severity pill 类（前缀 + -pill）
  function severityPillClass(severity) {
    return severityClass(severity) + "-pill";
  }

  // ───────── 决策状态 → CSS 类 ─────────
  // audit.jsonl 的 action 决定字段证据卡的视觉状态：确认 / 放行 / 驳回 / 人工放行
  // 后端 action: confirm / override / reject / manual_override
  function decisionClass(action) {
    switch (String(action || "").toLowerCase()) {
      case "confirm":          return "is-confirmed";
      case "override":         return "is-overridden";
      case "reject":           return "is-rejected";
      case "manual_override":  return "is-overridden";
      default:                 return "";
    }
  }

  // 状态机字符串 → CSS 类（topbar 的状态徽章）
  function runStatusClass(status) {
    var s = String(status || "").toLowerCase();
    if (!s) return "";
    return "is-" + s;
  }

  // ───────── 字段族聚合 ─────────
  // 输入：findings 数组（每条含 family 字段）
  // 输出：按字段族排序的聚合数组：[{ family, label, items }, ...]
  function groupByFamily(findings) {
    var groups = {};
    var list = Array.isArray(findings) ? findings : [];
    for (var i = 0; i < list.length; i++) {
      var f = list[i] || {};
      var fam = (typeof f.family === "string" && f.family) ? f.family : "uncovered";
      if (!groups[fam]) groups[fam] = [];
      groups[fam].push(f);
    }
    var ordered = [];
    for (var j = 0; j < FAMILY_ORDER.length; j++) {
      var k = FAMILY_ORDER[j];
      if (groups[k]) {
        ordered.push({
          family: k,
          label: (FAMILY_META[k] || {}).label || k,
          items: groups[k],
        });
      }
    }
    return ordered;
  }

  // 给 evidence card 用的稳定 id：finding.{rule_id}.{field}.{field_value}
  // 后端 audit.jsonl 写入 finding_id 也用同一规则，便于前后端对齐
  function findingId(f) {
    if (!f) return "";
    return [
      "finding",
      f.rule_id || "n/a",
      f.field || "n/a",
      // field_value 可能长/含特殊字符，做一个确定性 hash 化简
      (f.field_value || "").replace(/[^a-zA-Z0-9一-龥_-]/g, "_").slice(0, 40),
    ].join(".");
  }

  // 给 evidence card 用的 severe 字符截断：空值时显示占位
  function safeString(value, fallback) {
    if (value === null || value === undefined) return fallback || "";
    var s = String(value);
    return s.length ? s : (fallback || "");
  }

  // ───────── DOM 渲染辅助（返回 HTML 字符串，便于测试与浏览器共用） ─────────
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // 一张字段证据卡 HTML
  function renderFindingCard(f) {
    if (!f) return "";
    var sev = severityClass(f.severity);
    var pill = severityPillClass(f.severity);
    var idAttr = escapeHtml(findingId(f));
    var isUncovered = (f.family === "uncovered") ? " uncovered" : "";
    return [
      '<article class="field-card ', sev, isUncovered,
      '" data-finding-id="', idAttr,
      '" data-family="', escapeHtml(f.family || "uncovered"),
      '" data-agent-id="', escapeHtml(safeString(f.agent_id, "—")),
      '" data-prescription-no="', escapeHtml(safeString(f.prescription_no, "—")),
      '" data-created-at="', escapeHtml(safeString(f.created_at, "")),
      '" data-source="', escapeHtml(safeString(f.source, "")),
      '" tabindex="0" role="button" aria-label="字段决策卡">',
        '<div class="card-row">',
          '<code>', escapeHtml(safeString(f.field, "—")), "</code>",
          '<span class="', pill, '">', escapeHtml(safeString(f.severity, "info")), "</span>",
        "</div>",
        '<div class="card-value">',
          escapeHtml(safeString(f.field_value, "—")),
        "</div>",
        '<div class="card-evidence">',
          '<span class="card-label">证据</span>',
          escapeHtml(safeString(f.evidence, "—")),
        "</div>",
        '<div class="card-explanation">',
          escapeHtml(safeString(f.hit_explanation, "")),
        "</div>",
        '<div class="card-source">',
          '<span class="card-label">规则</span>',
          escapeHtml(safeHtmlRule(f)),
        "</div>",
      "</article>",
    ].join("");
  }

  function safeHtmlRule(f) {
    var rid = safeString(f.rule_id, "—");
    var ver = f.rule_version ? " v" + escapeHtml(String(f.rule_version)) : "";
    var src = f.source ? " · " + escapeHtml(String(f.source)) : "";
    return rid + ver + src;
  }

  // 一个字段族块 HTML（head + cards）
  function renderFamilyBlock(group) {
    if (!group) return "";
    var cardsHtml = (group.items || []).map(renderFindingCard).join("");
    return [
      '<section class="family-block" data-family="', escapeHtml(group.family), '">',
        '<header class="family-head">',
          '<span class="family-name">', escapeHtml(group.label || group.family), "</span>",
          '<span class="family-count">', (group.items || []).length, " 张证据卡</span>",
        "</header>",
        '<div class="family-cards">', cardsHtml, "</div>",
      "</section>",
    ].join("");
  }

  // 整张对照网格 HTML
  function renderFamilyGrid(groups) {
    if (!groups || !groups.length) {
      return '<p class="empty-state">本处方暂无字段证据卡。</p>';
    }
    return groups.map(renderFamilyBlock).join("");
  }

  // ───────── DOM diff 增量补丁（pure function）─────────
  // 把当前 findings 列表 → 与上次的 id 集合对比 → 输出应该新增 / 移除 / 复用哪些 finding 节点
  // 输入：prevItems（旧的 id 集合，本轮仍可见） → nextItems（新拉到的 findings[]）
  // 输出：{ ids_to_add: [finding...], ids_to_remove: [oldId...], ids_kept: [finding...] }
  // 测试断言：additions 与 removals 是精确集合差；同一 rule_id+field+field_value 永远归为同 id
  function diffFindings(prevItems, nextItems) {
    var prevById = {};
    var prevList = Array.isArray(prevItems) ? prevItems : [];
    var nextList = Array.isArray(nextItems) ? nextItems : [];
    for (var i = 0; i < prevList.length; i++) {
      var p = prevList[i];
      if (!p) continue;
      prevById[findingId(p)] = p;
    }
    var nextById = {};
    for (var j = 0; j < nextList.length; j++) {
      var n = nextList[j];
      if (!n) continue;
      nextById[findingId(n)] = n;
    }
    var ids_to_add = [];
    var ids_kept = [];
    var seenNext = {};
    for (var k = 0; k < nextList.length; k++) {
      var item = nextList[k];
      var id = findingId(item);
      seenNext[id] = true;
      if (prevById[id]) {
        ids_kept.push(item);
      } else {
        ids_to_add.push(item);
      }
    }
    var ids_to_remove = [];
    for (var prevId in prevById) {
      if (!seenNext[prevId]) ids_to_remove.push(prevId);
    }
    return {
      additions: ids_to_add,
      removals: ids_to_remove,
      kept: ids_kept,
      next_by_id: nextById,
    };
  }

  // ───────── 增量补丁计划（pure function）─────────
  // 目的：1s 轮询下不再 innerHTML 全量重绘，只对真正变化的证据卡打补丁。
  // 输入：prevItems（上轮 findings[]）、nextItems（本轮 findings[]）、auditEntries（audit.jsonl 行）
  // 输出：有序 patch 列表，每项 { op, ... }：
  //   family_add     { family, label, index }        —— 新增字段族块（index = FAMILY_ORDER 中位次）
  //   family_remove  { family }                      —— 该族本轮已无证据卡
  //   family_count   { family, count }               —— 族头计数变化
  //   card_add       { family, finding_id, html }    —— 新证据卡
  //   card_update    { family, finding_id, html }    —— 同 id 内容变化（规则版本/严重度等）
  //   card_remove    { finding_id }                  —— 证据卡消失
  //   card_decision  { finding_id, class }           —— 药师决策 → is-confirmed / is-overridden / is-rejected
  // 约束：同一输入必得同一输出（顺序稳定），便于 Python 侧断言。
  function cardSignature(f) {
    if (!f) return "";
    return [
      f.family, f.field, f.field_value, f.severity,
      f.rule_id, f.rule_version, f.hit_explanation, f.evidence,
      f.source, f.agent_id, f.created_at,
    ].map(function (v) { return v == null ? "" : String(v); }).join("");
  }

  // audit.jsonl → { finding_id: 最后一次决策的 CSS 类 }
  // append-only 语义：同一 finding 多次操作时以最后一行为准。
  function decisionIndex(auditEntries) {
    var rows = Array.isArray(auditEntries) ? auditEntries : [];
    var index = {};
    for (var i = 0; i < rows.length; i++) {
      var a = rows[i];
      if (!a || !a.finding_id) continue;
      var cls = decisionClass(a.action);
      if (cls) index[a.finding_id] = cls;
      else delete index[a.finding_id];
    }
    return index;
  }

  function familyIndex(family) {
    var i = FAMILY_ORDER.indexOf(family);
    return i < 0 ? FAMILY_ORDER.length : i;
  }

  function planGridPatches(prevItems, nextItems, auditEntries) {
    var prevList = Array.isArray(prevItems) ? prevItems : [];
    var nextList = Array.isArray(nextItems) ? nextItems : [];

    var prevById = {}, prevFamilies = {}, nextById = {}, nextFamilies = {};
    var i, id, fam;

    for (i = 0; i < prevList.length; i++) {
      if (!prevList[i]) continue;
      id = findingId(prevList[i]);
      prevById[id] = prevList[i];
      fam = familyOf(prevList[i]);
      prevFamilies[fam] = (prevFamilies[fam] || 0) + 1;
    }
    for (i = 0; i < nextList.length; i++) {
      if (!nextList[i]) continue;
      id = findingId(nextList[i]);
      nextById[id] = nextList[i];
      fam = familyOf(nextList[i]);
      nextFamilies[fam] = (nextFamilies[fam] || 0) + 1;
    }

    var patches = [];

    // 1) 族块增删：按 FAMILY_ORDER 稳定顺序
    var orderedFamilies = FAMILY_ORDER.slice();
    for (i = 0; i < orderedFamilies.length; i++) {
      fam = orderedFamilies[i];
      var hadFam = !!prevFamilies[fam];
      var hasFam = !!nextFamilies[fam];
      if (hasFam && !hadFam) {
        patches.push({
          op: "family_add",
          family: fam,
          label: (FAMILY_META[fam] || {}).label || fam,
          index: familyIndex(fam),
        });
      } else if (!hasFam && hadFam) {
        patches.push({ op: "family_remove", family: fam });
      }
    }

    // 2) 证据卡移除（先删后加，避免同 id 冲突）
    var removals = [];
    for (id in prevById) {
      if (!Object.prototype.hasOwnProperty.call(prevById, id)) continue;
      if (!nextById[id]) removals.push(id);
    }
    removals.sort();
    for (i = 0; i < removals.length; i++) {
      patches.push({ op: "card_remove", finding_id: removals[i] });
    }

    // 3) 证据卡新增 / 更新：按 next 列表顺序（后端已按族 + 时间排好）
    for (i = 0; i < nextList.length; i++) {
      var item = nextList[i];
      if (!item) continue;
      id = findingId(item);
      fam = familyOf(item);
      var prev = prevById[id];
      if (!prev) {
        patches.push({ op: "card_add", family: fam, finding_id: id, html: renderFindingCard(item) });
      } else if (cardSignature(prev) !== cardSignature(item)) {
        patches.push({ op: "card_update", family: fam, finding_id: id, html: renderFindingCard(item) });
      }
    }

    // 4) 族头计数变化
    for (i = 0; i < orderedFamilies.length; i++) {
      fam = orderedFamilies[i];
      var before = prevFamilies[fam] || 0;
      var after = nextFamilies[fam] || 0;
      if (after && after !== before) {
        patches.push({ op: "family_count", family: fam, count: after });
      }
    }

    // 5) 药师决策着色（只对本轮仍在的证据卡下发）
    var decisions = decisionIndex(auditEntries);
    var decided = [];
    for (id in decisions) {
      if (!Object.prototype.hasOwnProperty.call(decisions, id)) continue;
      if (nextById[id]) decided.push(id);
    }
    decided.sort();
    for (i = 0; i < decided.length; i++) {
      patches.push({ op: "card_decision", finding_id: decided[i], "class": decisions[decided[i]] });
    }

    return patches;
  }

  // 字段证据卡模态 HTML
  function renderConfirmModalBody(f) {
    if (!f) return "";
    return [
      '<div class="modal-row"><span class="card-label">处方号</span><code>',
        escapeHtml(safeString(f.prescription_no, "—")), "</code></div>",
      '<div class="modal-row"><span class="card-label">字段</span><code>',
        escapeHtml(safeString(f.field, "—")), "</code></div>",
      '<div class="modal-row"><span class="card-label">字段值</span><span>',
        escapeHtml(safeString(f.field_value, "—")), "</span></div>",
      '<div class="modal-row"><span class="card-label">字段族</span><code>',
        escapeHtml(safeString(f.family, "—")), "</code></div>",
      '<div class="modal-row"><span class="card-label">严重度</span><span>',
        '<span class="severity-pill ', severityClass(f.severity), '">',
          escapeHtml(safeString(f.severity, "info")), "</span></div>",
      '<div class="modal-row"><span class="card-label">解释</span><span>',
        escapeHtml(safeString(f.hit_explanation, "—")), "</span></div>",
      '<div class="modal-row"><span class="card-label">证据</span>',
        '<div class="modal-evidence">', escapeHtml(safeString(f.evidence, "—")), "</div></div>",
      '<div class="modal-row"><span class="card-label">规则</span><code>',
        escapeHtml(safeHtmlRule(f)), "</code></div>",
      '<div class="modal-row"><span class="card-label">字段 Agent</span><code>',
        escapeHtml(safeString(f.agent_id, "—")), "</code></div>",
      '<div class="modal-row"><span class="card-label">命中时间</span><code>',
        escapeHtml(safeString(f.created_at, "—")), "</code></div>",
    ].join("");
  }

  // transcript 事件项 HTML（轻量字符串拼接）
  function renderTranscriptItem(e) {
    if (!e) return "";
    var event = safeString(e.event, "event");
    var ts = safeString(e.ts, "");
    var agent = e.agent_id ? " · " + escapeHtml(String(e.agent_id)) : "";
    var family = e.family ? " · " + escapeHtml(String(e.family)) : "";
    return [
      "<li><span class=\"event-pill\">", escapeHtml(event),
      "</span><time>", escapeHtml(ts), "</time>",
      "<span>", agent, family, "</span></li>",
    ].join("");
  }

  // audit 条目 HTML
  function renderAuditItem(a) {
    if (!a) return "";
    var action = safeString(a.action, "—");
    var pillCls = decisionClass(action); // is-confirmed → .action-pill.is-confirm
    var pillStyleCls = pillCls.replace(/^is-/, "is-");
    var op = safeString(a.operator, "—");
    var ts = safeString(a.ts, "");
    var note = a.note ? '<div style="color:var(--text-muted);font-size:11px;margin-top:2px;">' + escapeHtml(String(a.note)) + "</div>" : "";
    return [
      "<li>",
        "<span class=\"action-pill ", pillStyleCls, "\">", escapeHtml(action), "</span>",
        "<time>", escapeHtml(ts), "</time>",
        "<span> · ", escapeHtml(op), "</span>",
        note,
      "</li>",
    ].join("");
  }

  // 空的字段族块骨架（供增量补丁插入新族时用；卡片由 card_add 逐张填）
  function renderEmptyFamilyBlock(family, label) {
    return renderFamilyBlock({ family: family, label: label, items: [] });
  }

  // ───────── 导出 ─────────
  var api = {
    FAMILY_META: FAMILY_META,
    FAMILY_ORDER: FAMILY_ORDER,
    FAMILY_MATCHERS: FAMILY_MATCHERS,
    resolveFamily: resolveFamily,
    familyOf: familyOf,
    groupByFamily: groupByFamily,
    severityClass: severityClass,
    severityPillClass: severityPillClass,
    decisionClass: decisionClass,
    runStatusClass: runStatusClass,
    findingId: findingId,
    safeString: safeString,
    escapeHtml: escapeHtml,
    renderFindingCard: renderFindingCard,
    renderFamilyBlock: renderFamilyBlock,
    renderEmptyFamilyBlock: renderEmptyFamilyBlock,
    renderFamilyGrid: renderFamilyGrid,
    renderConfirmModalBody: renderConfirmModalBody,
    renderTranscriptItem: renderTranscriptItem,
    renderAuditItem: renderAuditItem,
    diffFindings: diffFindings,
    cardSignature: cardSignature,
    decisionIndex: decisionIndex,
    planGridPatches: planGridPatches,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.RxchkRender = api;
  }
})(typeof window !== "undefined" ? window : this);
