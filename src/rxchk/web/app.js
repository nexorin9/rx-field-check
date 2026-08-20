/* 用药处方字段对照校验系统 — Viewer 主入口
   ─────────────────────────────────────────────────────────────
   依赖：render.js（pure 函数，浏览器与 Node 共享）
   功能：
     - parseQueryToken() —— 从 URL ?token= 取 token
     - setInterval 1s 轮询 /api/runs / /api/run/<rx>/findings / /api/run/<rx>/transcript / /api/run/<rx>/audit
     - 用 render.diffFindings() 算 DOM 增量补丁
     - 点 evidence card → 拉模态（确认 / 备注 / 驳回）→ POST /api/run/<rx>/confirm
     - 发现某 finding 被 audit 覆盖 → DOM 加 is-confirmed / is-overridden / is-rejected
*/

(function () {
  "use strict";

  // render.js 已挂到 window.RxchkRender；若未挂（多入口调试），直接报错
  var Render = window.RxchkRender;
  if (!Render) {
    document.body.innerHTML = "<p style='color:#b91c1c;padding:24px;'>前端资源缺失：render.js 未加载。</p>";
    return;
  }

  // ───────── 配置 ─────────
  var POLL_INTERVAL_MS = 1000;
  var REQUEST_TIMEOUT_MS = 8000;

  // ───────── State ─────────
  var State = {
    token: "",
    rxNo: "",            // url ?rx= 显式选择时使用；空表示从下拉选
    rxNoFromInput: "",   // 用户在终端开了 rx_no，但 viewer 还能供其他选择
    findings: {},        // by findingId → finding
    transcripts: [],     // 累计事件
    audits: [],          // 累计操作
    lastPollAt: 0,
    connection: "ok",    // ok / stalled / error / unauthed
    polling: null,
  };

  // ───────── URL 解析 ─────────
  function parseQueryToken() {
    var search = window.location.search || "";
    var hash = window.location.hash || "";
    var src = search ? search.slice(1) : hash.slice(1);
    if (!src) return "";
    var pairs = src.split("&");
    for (var i = 0; i < pairs.length; i++) {
      var pair = pairs[i].split("=");
      if (decodeURIComponent(pair[0]) === "token") {
        return decodeURIComponent(pair[1] || "");
      }
    }
    return "";
  }

  function parseQueryRx() {
    var search = window.location.search || "";
    if (!search) return "";
    var pairs = search.slice(1).split("&");
    for (var i = 0; i < pairs.length; i++) {
      var p = pairs[i].split("=");
      if (decodeURIComponent(p[0]) === "rx") {
        return decodeURIComponent(p[1] || "");
      }
    }
    return "";
  }

  function withToken(url) {
    if (!State.token) return url;
    var sep = url.indexOf("?") >= 0 ? "&" : "?";
    return url + sep + "token=" + encodeURIComponent(State.token);
  }

  function authHeaders() {
    var h = { "Accept": "application/json" };
    if (State.token) h["Authorization"] = "Bearer " + State.token;
    return h;
  }

  // ───────── JSON 拉取 ─────────
  function fetchJson(url, opts) {
    opts = opts || {};
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, REQUEST_TIMEOUT_MS);
    var fetchOpts = {
      method: opts.method || "GET",
      headers: Object.assign({}, authHeaders(), opts.body ? { "Content-Type": "application/json" } : {}),
      signal: controller.signal,
    };
    if (opts.body) fetchOpts.body = JSON.stringify(opts.body);
    return fetch(url, fetchOpts)
      .then(function (resp) {
        clearTimeout(timer);
        State.connection = resp.ok ? "ok" : (resp.status === 401 ? "unauthed" : "error");
        if (resp.status === 401) throw new Error("未授权：token 缺失或错误");
        if (!resp.ok) throw new Error("HTTP " + resp.status + " on " + url);
        return resp.json();
      })
      .catch(function (err) {
        clearTimeout(timer);
        State.connection = "stalled";
        throw err;
      });
  }

  // ───────── DOM 工具 ─────────
  function $(sel) { return document.querySelector(sel); }
  function $all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

  // ───────── 渲染层 ─────────
  var els = {};

  function bindEls() {
    els.rxNo = $("#rx-no");
    els.rxStatus = $("#rx-status");
    els.rxProgress = $("#rx-progress");
    els.findingsCount = $("#findings-count");
    els.emptyState = $("#empty-state");
    els.familyGrid = $("#family-grid");
    els.transcriptList = $("#transcript-list");
    els.auditList = $("#audit-list");
    els.runSelect = $("#run-select");
    els.runsHint = $("#runs-hint");
    els.connectionState = $("#connection-state");

    els.modalMask = $("#modal-mask");
    els.modalBody = $("#modal-body");
    els.modalForm = $("#modal-form");
    els.modalOperator = $("#modal-operator");
    els.modalNote = $("#modal-note");
    els.modalHint = $("#modal-hint");
    els.modalClose = $("#modal-close");
    els.modalCancel = $("#modal-cancel");
  }

  // 增删 DOM 节点（基于 planGridPatches 的增量补丁；不再每秒 innerHTML 全量重绘）
  function applyFindingsDiff(nextFindings) {
    var nextList = Array.isArray(nextFindings) ? nextFindings : [];
    var prevList = Object.keys(State.findings).map(function (k) { return State.findings[k]; });

    var patches = Render.planGridPatches(prevList, nextList, State.audits);
    applyPatches(patches);

    // 更新本地缓存（下轮 diff 的 prev）
    var byId = {};
    nextList.forEach(function (f) { byId[Render.findingId(f)] = f; });
    State.findings = byId;
    State.lastPollAt = Date.now();

    // 进度计数
    var total = nextList.length;
    var shown = total === 1 ? "1 张证据卡" : (total + " 张证据卡");
    var byFamily = countByFamily(nextList);
    var parts = [];
    Object.keys(byFamily).forEach(function (k) {
      parts.push(byFamily[k] + " " + ((Render.FAMILY_META[k] || {}).label || k));
    });
    els.findingsCount.textContent =
      "（" + shown + (parts.length ? " · " + parts.join(" · ") : "") + "）";
    els.emptyState.hidden = nextList.length > 0;
    return patches;
  }

  // 把 patch 列表落到真实 DOM 上（唯一触碰 innerHTML 的地方是单卡片/单块粒度）
  function applyPatches(patches) {
    var grid = els.familyGrid;
    if (!grid) return;
    (patches || []).forEach(function (p) {
      switch (p.op) {
        case "family_add":   insertFamilyBlock(grid, p); break;
        case "family_remove": removeNode(familyBlockOf(grid, p.family)); break;
        case "family_count":  setFamilyCount(grid, p.family, p.count); break;
        case "card_add":      insertCard(grid, p); break;
        case "card_update":   updateCard(grid, p); break;
        case "card_remove":   removeNode(cardOf(grid, p.finding_id)); break;
        case "card_decision": markDecision(grid, p.finding_id, p["class"]); break;
        default: break;
      }
    });
    // 空态占位（本地无任何族块时显示）
    if (!grid.querySelector(".family-block") && !grid.querySelector(".empty-state")) {
      grid.innerHTML = Render.renderFamilyGrid([]);
    } else if (grid.querySelector(".family-block")) {
      var placeholder = grid.querySelector(":scope > .empty-state");
      if (placeholder) removeNode(placeholder);
    }
  }

  function familyBlockOf(grid, family) {
    return grid.querySelector('.family-block[data-family="' + family + '"]');
  }

  function cardOf(grid, findingId) {
    return grid.querySelector('.field-card[data-finding-id="' + findingId + '"]');
  }

  function removeNode(node) {
    if (node && node.parentNode) node.parentNode.removeChild(node);
  }

  // 按 FAMILY_ORDER 位次插入，保证族块顺序稳定（与 render.js 排序一致）
  function insertFamilyBlock(grid, patch) {
    if (familyBlockOf(grid, patch.family)) return;
    var tmp = document.createElement("div");
    tmp.innerHTML = Render.renderEmptyFamilyBlock(patch.family, patch.label);
    var block = tmp.firstChild;
    var blocks = $all("#family-grid .family-block");
    var anchor = null;
    for (var i = 0; i < blocks.length; i++) {
      var otherIdx = Render.FAMILY_ORDER.indexOf(blocks[i].getAttribute("data-family"));
      if (otherIdx > patch.index) { anchor = blocks[i]; break; }
    }
    if (anchor) grid.insertBefore(block, anchor);
    else grid.appendChild(block);
  }

  function insertCard(grid, patch) {
    var block = familyBlockOf(grid, patch.family);
    if (!block) {
      insertFamilyBlock(grid, {
        family: patch.family,
        label: (Render.FAMILY_META[patch.family] || {}).label || patch.family,
        index: Render.FAMILY_ORDER.indexOf(patch.family),
      });
      block = familyBlockOf(grid, patch.family);
    }
    if (!block) return;
    var host = block.querySelector(".family-cards") || block;
    var existing = cardOf(grid, patch.finding_id);
    if (existing) { updateCard(grid, patch); return; }
    host.insertAdjacentHTML("beforeend", patch.html);
    reapplyDecision(grid, patch.finding_id);
  }

  // 原地替换单张卡片：保留同族块与滚动位置，不动其它节点
  function updateCard(grid, patch) {
    var node = cardOf(grid, patch.finding_id);
    if (!node) { insertCard(grid, patch); return; }
    var tmp = document.createElement("div");
    tmp.innerHTML = patch.html;
    var fresh = tmp.firstChild;
    node.parentNode.replaceChild(fresh, node);
    reapplyDecision(grid, patch.finding_id);
  }

  function setFamilyCount(grid, family, count) {
    var block = familyBlockOf(grid, family);
    if (!block) return;
    var el = block.querySelector(".family-count");
    if (el) el.textContent = count + " 张证据卡";
  }

  var DECISION_CLASSES = ["is-confirmed", "is-overridden", "is-rejected"];

  function markDecision(grid, findingId, cls) {
    var node = cardOf(grid, findingId);
    if (!node || !cls) return;
    DECISION_CLASSES.forEach(function (c) { if (c !== cls) node.classList.remove(c); });
    node.classList.add(cls);
  }

  // 卡片被重建后（card_add / card_update），把已有决策着色补回去
  function reapplyDecision(grid, findingId) {
    var index = Render.decisionIndex(State.audits);
    if (index[findingId]) markDecision(grid, findingId, index[findingId]);
  }

  function countByFamily(list) {
    var counts = {};
    (list || []).forEach(function (f) {
      var k = (f && f.family) || "uncovered";
      counts[k] = (counts[k] || 0) + 1;
    });
    return counts;
  }

  function applyTopbar(runRecord) {
    if (!runRecord) {
      els.rxNo.textContent = "—";
      els.rxStatus.textContent = "—";
      els.rxStatus.className = "status-pill";
      els.rxProgress.textContent = "未选处方";
      return;
    }
    var rx = runRecord.prescription_no || State.rxNo || "—";
    els.rxNo.textContent = rx;
    var s = (runRecord.status || "").toLowerCase();
    els.rxStatus.textContent = runRecord.status || "—";
    els.rxStatus.className = "status-pill " + Render.runStatusClass(s);
    var findingsCount = runRecord.findings_count == null ? "" : (" · " + runRecord.findings_count + " 命中");
    els.rxProgress.textContent = "核对中" + findingsCount;
  }

  function applyTranscripts(events) {
    var list = els.transcriptList;
    list.innerHTML = "";
    var evs = Array.isArray(events) ? events : [];
    State.transcripts = evs;
    evs.forEach(function (e) {
      list.insertAdjacentHTML("beforeend", Render.renderTranscriptItem(e));
    });
  }

  function applyAudits(entries) {
    var list = els.auditList;
    list.innerHTML = "";
    var rows = Array.isArray(entries) ? entries : [];
    State.audits = rows;
    rows.forEach(function (e) {
      list.insertAdjacentHTML("beforeend", Render.renderAuditItem(e));
    });
    // audit 与 findings 是两条轮询，audit 后到时把决策着色补到现存卡片上
    var index = Render.decisionIndex(rows);
    Object.keys(index).forEach(function (fid) {
      markDecision(els.familyGrid, fid, index[fid]);
    });
  }

  // ───────── Polling ─────────
  function startPolling() {
    if (State.polling) return;
    State.polling = setInterval(poll, POLL_INTERVAL_MS);
    poll();
  }

  function stopPolling() {
    if (State.polling) {
      clearInterval(State.polling);
      State.polling = null;
    }
  }

  function poll() {
    var tasks = [
      fetchRuns(),
      pollFindings(),
      pollTranscripts(),
      pollAudits(),
    ];
    Promise.allSettled(tasks).then(function () {
      updateConnectionUi();
    });
  }

  function fetchRuns() {
    return fetchJson(withToken("/api/runs"))
      .then(function (payload) {
        var runs = (payload && payload.runs) || [];
        populateRunSelect(runs);
      })
      .catch(function () { /* swallow: connection UI 提示 */ });
  }

  function populateRunSelect(runs) {
    var select = els.runSelect;
    var current = State.rxNo || select.value || "";
    select.innerHTML = '<option value="">— 选择历史处方核对 —</option>';
    runs.forEach(function (r) {
      var label = (r.prescription_no || r.run_name || "?")
        + " · " + (r.status || "—")
        + (r.findings_count ? " · " + r.findings_count + " 命中" : "")
        + (r.audit_count ? " · " + r.audit_count + " 操作" : "");
      var opt = document.createElement("option");
      opt.value = r.run_name || r.prescription_no || "";
      opt.textContent = label;
      select.appendChild(opt);
    });
    if (current) {
      select.value = current;
      State.rxNo = current;
    }
    if (!runs.length) {
      els.runsHint.textContent = "暂无核对记录：终端执行 rxchk check 后会自动出现。";
    } else {
      els.runsHint.textContent = "共 " + runs.length + " 条核对记录，按 mtime 倒序。";
    }
  }

  function pollFindings() {
    if (!State.rxNo) return Promise.resolve();
    var url = withToken("/api/run/" + encodeURIComponent(State.rxNo) + "/findings");
    return fetchJson(url)
      .then(function (payload) {
        var list = (payload && payload.findings) || [];
        applyFindingsDiff(list);
        var runRecord = {
          prescription_no: payload && payload.prescription_no,
          status: (payload && payload.run_status) || (payload && payload.status),
          findings_count: payload && payload.count,
        };
        applyTopbar(runRecord);
      });
  }

  function pollTranscripts() {
    if (!State.rxNo) return Promise.resolve();
    var url = withToken("/api/run/" + encodeURIComponent(State.rxNo) + "/transcript");
    return fetchJson(url)
      .then(function (payload) {
        var evs = (payload && payload.events) || [];
        applyTranscripts(evs);
      })
      .catch(function () { /* swallow */ });
  }

  function pollAudits() {
    if (!State.rxNo) return Promise.resolve();
    var url = withToken("/api/run/" + encodeURIComponent(State.rxNo) + "/audit");
    return fetchJson(url)
      .then(function (payload) {
        var rows = (payload && payload.entries) || [];
        applyAudits(rows);
      })
      .catch(function () { /* swallow */ });
  }

  function updateConnectionUi() {
    var node = els.connectionState;
    node.className = "connection-state";
    switch (State.connection) {
      case "ok":       node.classList.add("is-ok"); node.textContent = "●"; break;
      case "stalled":  node.classList.add("is-stalled"); node.textContent = "●"; break;
      case "error":    node.classList.add("is-error"); node.textContent = "●"; break;
      case "unauthed": node.classList.add("is-unauthed"); node.textContent = "●"; break;
      default:         node.textContent = "●";
    }
  }

  // ───────── 模态 ─────────
  var pendingFinding = null;

  function openModalFor(finding) {
    pendingFinding = finding;
    els.modalBody.innerHTML = Render.renderConfirmModalBody(finding);
    els.modalOperator.value = "";
    els.modalNote.value = "";
    els.modalMask.hidden = false;
    setTimeout(function () { els.modalOperator.focus(); }, 50);
  }

  function closeModal() {
    pendingFinding = null;
    els.modalMask.hidden = true;
  }

  function postConfirm(action) {
    if (!pendingFinding) return Promise.reject(new Error("no pending finding"));
    var operator = (els.modalOperator.value || "").trim();
    if (!operator) {
      els.modalHint.textContent = "操作药师必填，请填写后再提交。";
      els.modalOperator.focus();
      return Promise.reject(new Error("operator required"));
    }
    var note = els.modalNote.value || "";
    var fid = Render.findingId(pendingFinding);
    var url = withToken("/api/run/" + encodeURIComponent(State.rxNo) + "/confirm");
    return fetchJson(url, {
      method: "POST",
      body: {
        action: action,
        operator: operator,
        note: note,
        finding_id: fid,
      },
    })
      .then(function (resp) {
        // 立即刷新 audit list，让 modal 立刻看到新行
        pollAudits();
        // 标记 DOM（视觉确认）：确认 → .is-confirmed，放行 → .is-overridden，驳回 → .is-rejected
        var applied = (resp && resp.entry && resp.entry.action) || action;
        var cls = Render.decisionClass(applied);
        if (cls) markDecision(els.familyGrid, fid, cls);
        return resp;
      });
  }

  // ───────── 事件绑定 ─────────
  function bindEvents() {
    els.runSelect.addEventListener("change", function () {
      var v = els.runSelect.value || "";
      State.rxNo = v;
      // 切换处方后清空本地 findings，避免 id 串台
      State.findings = {};
      State.transcripts = [];
      State.audits = [];
      els.familyGrid.innerHTML = "";
      els.transcriptList.innerHTML = "";
      els.auditList.innerHTML = "";
      els.rxNo.textContent = v || "—";
      poll();
    });

    // 事件委托：点 evidence card 弹模态
    els.familyGrid.addEventListener("click", function (ev) {
      var target = ev.target;
      while (target && target !== els.familyGrid) {
        if (target.classList && target.classList.contains("field-card")) {
          var fid = target.getAttribute("data-finding-id");
          var finding = State.findings[fid];
          if (finding) openModalFor(finding);
          return;
        }
        target = target.parentNode;
      }
    });
    els.familyGrid.addEventListener("keydown", function (ev) {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      var target = ev.target;
      if (target && target.classList && target.classList.contains("field-card")) {
        ev.preventDefault();
        var fid = target.getAttribute("data-finding-id");
        var finding = State.findings[fid];
        if (finding) openModalFor(finding);
      }
    });

    els.modalClose.addEventListener("click", closeModal);
    els.modalCancel.addEventListener("click", closeModal);
    els.modalMask.addEventListener("click", function (ev) {
      if (ev.target === els.modalMask) closeModal();
    });

    els.modalForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var sub = ev.submitter || document.activeElement;
      var action = (sub && sub.getAttribute("data-action")) || "confirm";
      postConfirm(action)
        .then(function () { closeModal(); })
        .catch(function (err) { els.modalHint.textContent = "提交失败：" + (err && err.message || err); });
    });
  }

  // ───────── Bootstrap ─────────
  function init() {
    State.token = parseQueryToken();
    State.rxNo = parseQueryRx();
    if (!State.token) {
      document.body.innerHTML = "<p style='padding:24px;color:#b91c1c;'>缺少 token：从终端打印的链接打开（带 ?token=…）。</p>";
      return;
    }
    bindEls();
    bindEvents();
    startPolling();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // 暴露 token / 切换处方等给 e2e 调试脚本（仅在调试模式开启）
  window.__RxchkViewer = {
    State: State,
    applyFindingsDiff: applyFindingsDiff,
    applyPatches: applyPatches,
    markDecision: markDecision,
    openModalFor: openModalFor,
    postConfirm: postConfirm,
    setRxNo: function (rx) { State.rxNo = rx; },
  };
})();
