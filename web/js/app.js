/* Haqq: the contract check. Talks to the JSON API in webapi.py and fills the case file live. */
(function () {
  "use strict";

  var $ = function (sel, scope) { return (scope || document).querySelector(sel); };
  var $$ = function (sel, scope) { return Array.prototype.slice.call((scope || document).querySelectorAll(sel)); };
  var motion = function () { return window.HaqqMotion || {}; };
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

  var AGENTS = [
    ["Orchestrator", "orchestrator"], ["Contract Analyst", "analyst"], ["Calculators", "calculators"],
    ["Law Researcher", "law"], ["Drafter", "drafter"], ["Critic", "critic"], ["Notice Reader", "reader"],
  ];
  var LANG_LABELS = { en: "English", ar: "العربية", both: "Both" };
  var SEVERITY = { high: "High", medium: "Medium", low: "Low" };

  var state = {
    token: null, meta: null, mode: document.documentElement.getAttribute("data-mode") || "demo",
    contract: null, running: false, letter: null, letterTab: "en", lastLanguage: "en",
    caseNo: null, spent: 0, turnCost: 0, steps: 0, timer: null, startedAt: 0, firstRun: true,
  };

  /* ------------------------------------------------------------- tiny DOM kit */
  function h(tag, props) {
    var node = document.createElement(tag);
    var kids = Array.prototype.slice.call(arguments, 2);
    if (props) Object.keys(props).forEach(function (k) {
      var v = props[k];
      if (v == null || v === false) return;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;   // only for server-rendered markdown (raw HTML escaped)
      else node.setAttribute(k, v === true ? "" : v);
    });
    kids.forEach(function (c) {
      if (c == null || c === false) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }
  function icon(key, cls) {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", cls || "ico");
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#i-" + key);
    svg.appendChild(use);
    return svg;
  }
  function iconFor(agent) {
    for (var i = 0; i < AGENTS.length; i++) if (AGENTS[i][0] === agent) return AGENTS[i][1];
    return "orchestrator";
  }
  function aed(x) { return "AED " + Math.round(Number(x) || 0).toLocaleString("en-US"); }
  function usd(x, digits) { return "$" + (Number(x) || 0).toFixed(digits == null ? 4 : digits); }
  function pct(x) { var n = Number(x); return (Number.isInteger(n) ? n : n.toFixed(1)) + "%"; }
  function nice(value) {
    var m = String(value || "").match(/(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return String(value || "");
    var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return Number(m[3]) + " " + months[Number(m[2]) - 1] + " " + m[1];
  }
  function humanize(s) { return String(s || "").replace(/_/g, " "); }
  function cap(s) { s = String(s || ""); return s.charAt(0).toUpperCase() + s.slice(1); }
  function prettyModel(m) {
    return String(m || "").replace(/^claude-/, "Claude ").replace(/-(\d+)-(\d+)$/, " $1.$2")
      .replace(/(^|\s)([a-z])/g, function (_, s, c) { return s + c.toUpperCase(); });
  }

  var toastTimer;
  function toast(text) {
    var t = $("[data-toast]");
    t.textContent = text;
    t.classList.add("is-on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove("is-on"); }, 2600);
  }
  function msg(key, text, isError) {
    var el = $('[data-msg="' + key + '"]');
    if (!el) return;
    el.textContent = text || "";
    el.classList.toggle("is-error", !!isError);
  }

  /* -------------------------------------------------------------------- API */
  function headers(extra) {
    var hd = extra || {};
    if (state.token) hd["X-Haqq-Session"] = state.token;
    return hd;
  }
  function remember(res, data) {
    var tok = res.headers.get("X-Haqq-Session") || (data && data.session);
    if (tok) state.token = tok;
  }
  function errorText(res, data) {
    if (data && typeof data.error === "string") return data.error;
    if (data && Array.isArray(data.detail)) return "Some of the values in the form could not be read. Check the numbers and the date.";
    if (res.status === 413) return "That file is too big.";
    return "The server answered " + res.status + ". Please try again.";
  }
  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || "GET", headers: headers() };
    if (opts.json !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.json); }
    if (opts.form) init.body = opts.form;
    return fetch(path, init).catch(function () {
      throw new Error("The server could not be reached. Check your connection and try again.");
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        remember(res, data);
        if (!res.ok) throw new Error(errorText(res, data));
        return data;
      });
    });
  }
  /* POST + server-sent events, read from the fetch stream (EventSource can't POST). */
  function stream(path, payload, onEvent) {
    return fetch(path, { method: "POST", headers: headers({ "Content-Type": "application/json" }), body: JSON.stringify(payload) })
      .catch(function () { throw new Error("The server could not be reached. Check your connection and try again."); })
      .then(function (res) {
        remember(res, null);
        if (!res.ok || !res.body) {
          return res.json().catch(function () { return {}; }).then(function (data) { throw new Error(errorText(res, data)); });
        }
        var reader = res.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        function pump() {
          return reader.read().then(function (r) {
            if (r.done) return;
            buffer += decoder.decode(r.value, { stream: true }).replace(/\r\n/g, "\n");
            var cut;
            while ((cut = buffer.indexOf("\n\n")) >= 0) {
              var chunk = buffer.slice(0, cut);
              buffer = buffer.slice(cut + 2);
              var name = "message", data = "";
              chunk.split("\n").forEach(function (line) {
                if (line.indexOf("event:") === 0) name = line.slice(6).trim();
                else if (line.indexOf("data:") === 0) data += line.slice(5).replace(/^ /, "");
              });
              if (data) { try { onEvent(name, JSON.parse(data)); } catch (e) { console.error(e); } }
            }
            return pump();
          });
        }
        return pump();
      });
  }

  /* A queue that shows events at a readable pace. Demo mode answers in milliseconds, so the
     rows would otherwise land all at once; the timestamps on them stay the real ones. */
  function Pacer(gap) { this.gap = gap; this.queue = []; this.busy = false; this.waiters = []; }
  Pacer.prototype.push = function (fn) { this.queue.push(fn); this.run(); };
  Pacer.prototype.run = function () {
    var self = this;
    if (self.busy) return;
    self.busy = true;
    (function next() {
      if (!self.queue.length) {
        self.busy = false;
        self.waiters.splice(0).forEach(function (w) { w(); });
        return;
      }
      var fn = self.queue.shift();
      Promise.resolve(fn()).then(function () { return sleep(self.gap); }).then(next, next);
    })();
  };
  Pacer.prototype.idle = function () {
    var self = this;
    return new Promise(function (r) { if (!self.busy && !self.queue.length) r(); else self.waiters.push(r); });
  };

  /* ------------------------------------------------------------- meta + form */
  function applyMeta(meta) {
    state.meta = meta;
    state.mode = meta.mode;
    document.documentElement.setAttribute("data-mode", meta.mode);
    var model = prettyModel(meta.model);
    $("[data-mode-chip] span").textContent = meta.mode === "llm" ? "Live · " + model : "Demo mode";
    var L = meta.limits;
    $("[data-upload-limits]").textContent = "PDF or TXT · up to " + L.max_upload_mb + " MB · " + L.max_pdf_pages + " pages";
    var note = $("[data-mode-note]");
    note.innerHTML = "";
    if (meta.mode === "llm") {
      note.appendChild(h("strong", { text: "Live. " }));
      note.appendChild(document.createTextNode(model + " leads the team at " + meta.effort +
        " effort. The Critic attacks every letter and the Drafter revises it once. Limits: " + L.max_cost_per_run +
        " per analysis, " + L.max_requests_per_session + " questions per session."));
      $("[data-ask-lead]").textContent = "A follow-up about the same contract: deposits, eviction, repairs, a complaint to the RDSC. The team keeps the whole conversation.";
    } else {
      note.appendChild(h("strong", { text: "Demo mode. " }));
      note.appendChild(document.createTextNode("The AI team is off on this server, so the code-only specialists run in a fixed order: the contract reader, the calculators, the law search, the letter template and the Critic's automatic check. Nothing here is decided by an AI model, and it costs nothing."));
      $("[data-ask-lead]").textContent = "In demo mode a question runs the law search only: you get the articles of the official law that best match it, word for word, not an AI answer.";
    }
    $$("[data-meta]").forEach(function (el) {
      var path = el.getAttribute("data-meta").split(".");
      var v = path.reduce(function (o, k) { return o && o[k]; }, meta);
      if (v != null) { el.textContent = String(v); if (el.hasAttribute("data-count")) el.setAttribute("data-count", String(v)); }
    });
    $$("[data-disclaimer]").forEach(function (el) { el.textContent = meta.disclaimer; });
    $("[data-colophon-mode]").textContent = meta.mode === "llm"
      ? "This server runs the full AI team (" + model + ")."
      : "This server runs in demo mode: the AI team is off.";
    buildForm(meta);
    updateRunButton();
    updateAskLimit(meta.requests_used);
  }

  function buildSeg(name, options, value) {
    var seg = $('[data-seg="' + name + '"]');
    seg.innerHTML = "";
    seg.setAttribute("role", "group");
    options.forEach(function (o) {
      var b = h("button", { type: "button", "data-value": o.value, "aria-pressed": String(o.value === value), lang: o.value === "ar" ? "ar" : null, text: o.label });
      b.addEventListener("click", function () { setSeg(name, o.value); });
      seg.appendChild(b);
    });
  }
  function setSeg(name, value) {
    $$('[data-seg="' + name + '"] button').forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-value") === value));
    });
  }
  function segValue(name) {
    var b = $('[data-seg="' + name + '"] button[aria-pressed="true"]');
    return b ? b.getAttribute("data-value") : null;
  }
  function buildForm(meta) {
    buildSeg("notice_type", meta.notice_types, segValue("notice_type") || "none");
    buildSeg("letter_language", meta.letter_languages.map(function (l) {
      return { value: l.value, label: LANG_LABELS[l.value] || l.label };
    }), segValue("letter_language") || "en");
    var sel = $("#f-method");
    var current = sel.value || "unknown";
    sel.innerHTML = "";
    meta.delivery_methods.forEach(function (d) { sel.appendChild(h("option", { value: d.value, text: d.label })); });
    sel.value = current;
    if (meta.mode !== "llm") {
      $("[data-lang-help]").textContent = "Arabic letters are written by the Drafter (Claude), so they need the AI team, which is off in demo mode. The English letter still works.";
    }
  }
  function num(v) {
    var n = Number(String(v || "").replace(/[^\d.]/g, ""));
    return isFinite(n) && n > 0 ? n : null;
  }
  function formatNumberInput(input) {
    var n = num(input.value);
    input.value = n ? n.toLocaleString("en-US") : input.value.trim() ? input.value : "";
  }
  function formValues() {
    return {
      rera_average_rent: num($("#f-rera").value),
      proposed_new_rent: num($("#f-proposed").value),
      notice_type: segValue("notice_type") || "none",
      notice_date: $("#f-date").value || null,
      delivery_method: $("#f-method").value || "unknown",
      letter_language: segValue("letter_language") || "en",
    };
  }
  function flash(input) {
    input.classList.remove("is-filled");
    void input.offsetWidth;
    input.classList.add("is-filled");
  }
  function fill(values) {
    if (!values) return;
    if (values.rera_average_rent != null) { $("#f-rera").value = Number(values.rera_average_rent).toLocaleString("en-US"); flash($("#f-rera")); }
    if (values.proposed_new_rent != null) { $("#f-proposed").value = Number(values.proposed_new_rent).toLocaleString("en-US"); flash($("#f-proposed")); }
    if (values.notice_type) setSeg("notice_type", values.notice_type);
    if (values.notice_date) { $("#f-date").value = values.notice_date; flash($("#f-date")); }
    if (values.delivery_method) { $("#f-method").value = values.delivery_method; flash($("#f-method")); }
    updateRunNote();
  }

  function updateRunButton() {
    var btn = $('[data-action="run"]');
    btn.disabled = !state.contract || state.running;
    btn.classList.toggle("is-running", state.running);
    $(".btn-run-label", btn).textContent = state.running ? "The team is working" : (state.firstRun ? "Run the check" : "Run it again");
    var sub = $("[data-run-sub]");
    if (state.running) sub.textContent = "Watch the case file";
    else if (!state.contract) sub.textContent = "Load a contract first";
    else sub.textContent = state.mode === "llm" ? "Spend limit " + state.meta.limits.max_cost_per_run + " per analysis" : "Free · demo mode";
    updateRunNote();
  }
  function updateRunNote() {
    var f = formValues(), notes = [];
    if (!f.rera_average_rent) notes.push("Without the RERA average rent the rent cap can't be checked.");
    if (f.notice_type !== "none" && !f.notice_date) notes.push("Add the date you received the notice to check the deadline.");
    $("[data-run-note]").textContent = state.contract ? notes.join(" ") : "Step 01 needs a contract: upload one, paste the text, or use the sample.";
  }
  function updateAskLimit(used) {
    var el = $("[data-ask-limit]");
    if (!state.meta) return;
    el.textContent = state.mode === "llm"
      ? (used || 0) + " of " + state.meta.limits.max_requests_per_session + " questions used this session"
      : "Demo mode · law search only";
  }

  /* ----------------------------------------------------------- step 01: contract */
  function today() {
    return new Date().toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
  }
  function showContract(info) {
    state.contract = info;
    var sheet = $('[data-drop="contract"]');
    sheet.classList.add("is-loaded");
    $(".sheet-empty", sheet).hidden = true;
    $(".sheet-loaded", sheet).hidden = false;
    $("[data-contract-label]").textContent = info.label;
    $("[data-contract-status]").textContent = info.status;
    $("[data-contract-preview]").textContent = info.preview || "(Scanned or Arabic PDF: Claude will read the pages.)";
    $("[data-received-date]").textContent = today();
    var stateEl = $('[data-state="contract"]');
    stateEl.textContent = "Loaded";
    stateEl.classList.add("is-done");
    motion().stamp && motion().stamp($("[data-received]"), -7);
    msg("contract", "");
    clearTurnUI();
    updateRunButton();
  }
  function loadSample() {
    msg("contract", "Loading the sample contract…");
    api("api/contract/sample", { method: "POST" }).then(function (data) {
      showContract(data.contract);
      fill(data.form);
      msg("contract", "The fictional sample: a 1-bedroom flat in Dubai Marina at AED 85,000 a year. The form below now holds a demo scenario (RERA average AED 100,000, the landlord asks AED 93,500).");
    }).catch(function (e) { msg("contract", e.message, true); });
  }
  function uploadContract(file) {
    if (!file) return;
    var limit = state.meta ? state.meta.limits.max_upload_mb : 5;
    if (file.size > limit * 1048576) { msg("contract", "That file is " + (file.size / 1048576).toFixed(1) + " MB; the limit is " + limit + " MB.", true); return; }
    var form = new FormData();
    form.append("file", file, file.name);
    msg("contract", "Reading " + file.name + "…");
    api("api/contract", { method: "POST", form: form }).then(function (data) { showContract(data.contract); })
      .catch(function (e) { msg("contract", e.message, true); });
  }
  function usePaste() {
    var text = $("#paste-text").value;
    if (!text.trim()) { msg("contract", "Paste the contract text first.", true); return; }
    var form = new FormData();
    form.append("text", text);
    api("api/contract", { method: "POST", form: form }).then(function (data) {
      showContract(data.contract);
      $("#paste-box").hidden = true;
      $('[data-action="toggle-paste"]').setAttribute("aria-expanded", "false");
    }).catch(function (e) { msg("contract", e.message, true); });
  }

  /* ------------------------------------------------------------ step 02: notice */
  var DELIVERY_LABELS = { notary_public: "Notary public", registered_mail: "Registered mail", email: "Email", whatsapp_or_sms: "WhatsApp / SMS", hand_delivered: "Handed over", other: "Other", unknown: "Not clear" };
  function showReading(data) {
    var box = $("[data-reading]");
    var list = $("[data-reading-list]");
    list.innerHTML = "";
    var r = data.reading;
    function row(label, value) { if (value) list.appendChild(h("div", null, h("dt", { text: label }), h("dd", { text: value }))); }
    row("Notice type", cap(humanize(r.notice_type)));
    row("Proposed rent", r.proposed_annual_rent_aed ? aed(r.proposed_annual_rent_aed) + " a year" + (r.current_annual_rent_aed ? " (now " + aed(r.current_annual_rent_aed) + ")" : "") : "not stated");
    row("Sent or received", r.notice_date ? nice(r.notice_date) + (r.date_evidence ? ", from the " + r.date_evidence : "") : "not found");
    row("Delivered by", DELIVERY_LABELS[r.delivery_method] || r.delivery_method);
    row("From", r.sender);
    row("Key words", r.key_quote ? "“" + r.key_quote + "”" : null);
    var warn = $("[data-reading-warning]");
    var warnText = $("[data-reading-warning-text]");
    warnText.textContent = (data.warnings || []).join(" ");
    warn.hidden = !(data.warnings && data.warnings.length);
    box.hidden = false;
    motion().reveal && motion().reveal([box]);
    var stateEl = $('[data-state="notice"]');
    stateEl.textContent = "Read";
    stateEl.classList.add("is-done");
  }
  function handleNotice(promise, imageSrc) {
    var img = $(".shot-img");
    var drop = $('[data-drop="notice"]');
    if (imageSrc) { img.src = imageSrc; img.hidden = false; $(".sheet-empty", drop).hidden = true; drop.classList.add("is-loaded"); }
    msg("notice", "The Notice Reader is reading the screenshot…");
    caseDivider("The landlord's notice");
    return promise.then(function (data) {
      if (data.image_url) { img.src = data.image_url; img.hidden = false; $(".sheet-empty", drop).hidden = true; drop.classList.add("is-loaded"); }
      (data.events || []).forEach(function (ev) { addRow(ev); });
      var cost = String(data.cost || "").match(/\$([\d.]+)/);
      if (cost) addSpend(Number(cost[1]));
      if (data.ok) {
        showReading(data);
        fill(data.form);
        msg("notice", "");
      } else {
        $("[data-reading]").hidden = true;
        var tmp = h("div", { html: data.html });
        msg("notice", tmp.textContent.trim(), true);
      }
      setStatus(state.running ? "Working" : "Idle");
    }).catch(function (e) { msg("notice", e.message, true); });
  }
  function readNoticeFile(file) {
    if (!file) return;
    var limit = state.meta ? state.meta.limits.max_upload_mb : 5;
    if (file.size > limit * 1048576) { msg("notice", "That image is " + (file.size / 1048576).toFixed(1) + " MB; the limit is " + limit + " MB.", true); return; }
    var form = new FormData();
    form.append("file", file, file.name);
    handleNotice(api("api/notice", { method: "POST", form: form }), URL.createObjectURL(file));
  }

  /* --------------------------------------------------------------- case file */
  function caseNumber() {
    if (!state.caseNo) {
      var d = new Date();
      state.caseNo = "HQ-" + String(d.getMonth() + 1).padStart(2, "0") + String(d.getDate()).padStart(2, "0") + "-" +
        String(state.token || "0000").replace(/[^A-Za-z0-9]/g, "").slice(0, 4).toUpperCase();
    }
    $("[data-cf-id]").textContent = state.caseNo;
  }
  function setStatus(text) {
    var s = $("[data-cf-status]");
    s.textContent = text;
    s.classList.toggle("is-working", text === "Working");
    s.classList.toggle("is-closed", text === "Closed");
  }
  function buildRail() {
    var rail = $("[data-cf-rail]");
    rail.innerHTML = "";
    AGENTS.forEach(function (a) {
      var li = h("li", { "data-agent": a[0], title: a[0] });
      li.appendChild(icon(a[1]));
      li.appendChild(h("span", { text: a[0].split(" ")[0] }));
      rail.appendChild(li);
    });
  }
  function railActive(agent) {
    $$("[data-cf-rail] li").forEach(function (li) {
      if (li.classList.contains("is-active")) li.classList.add("is-done");
      li.classList.toggle("is-active", li.getAttribute("data-agent") === agent);
    });
  }
  function railReset() { $$("[data-cf-rail] li").forEach(function (li) { li.classList.remove("is-active", "is-done"); }); }
  function logEl() {
    var log = $("[data-cf-log]");
    var empty = $(".cf-empty", log);
    if (empty) empty.remove();
    return log;
  }
  function scrollLog() { var log = $("[data-cf-log]"); log.scrollTop = log.scrollHeight; }
  function caseDivider(text) {
    caseNumber();
    var li = h("li", { class: "cf-row is-divider" }, h("p", { text: text }));
    logEl().appendChild(li);
    scrollLog();
  }
  function detailText(ev) {
    var d = ev.detail;
    if (d == null || d === "") return "";
    return typeof d === "string" ? d : JSON.stringify(d, null, 2);
  }
  function addRow(ev) {
    var log = logEl();
    var tSec = ev.t != null ? "+" + Number(ev.t).toFixed(1) + "s" : "";
    var durText = ev.duration_ms != null ? (ev.duration_ms < 1000 ? ev.duration_ms + " ms" : (ev.duration_ms / 1000).toFixed(1) + " s") : "";
    railActive(ev.agent);

    if (ev.kind === "tool_result") {
      var pending = $$('.cf-row.is-pending[data-title="' + CSS.escape(ev.title) + '"]', log).pop();
      if (pending) {
        pending.classList.remove("is-pending");
        if (ev.is_error) pending.classList.add("is-error");
        $(".cf-line", pending).textContent = ev.line;
        $(".cf-who span", pending).textContent = durText;
        var pre = $(".cf-detail", pending);
        pre.textContent = pre.textContent + "\n\nResult:\n" + detailText(ev);
        motion().rowIn && motion().rowIn($(".cf-line", pending));
        scrollLog();
        return;
      }
    }
    var cls = "cf-row";
    if (ev.kind === "tool_call") cls += " is-pending";
    if (ev.kind === "thinking" || ev.kind === "assistant_text") cls += " is-thinking";
    if (ev.is_error || ev.kind === "error") cls += " is-error";
    if (ev.title === "Cost" || ev.title === "Cost by agent") cls += " is-cost";
    var who = h("p", { class: "cf-who" }, h("b", { text: ev.agent + (ev.kind === "thinking" ? " · thinking" : "") }), h("span", { text: durText }));
    var body = h("div", null, who, h("p", { class: "cf-line", text: ev.line }));
    var detail = detailText(ev);
    if (detail) {
      var pre2 = h("pre", { class: "cf-detail", hidden: true, "data-lenis-prevent": true, text: (ev.kind === "tool_call" ? "Input:\n" : "") + detail });
      var more = h("button", { class: "cf-more", type: "button", "aria-expanded": "false", text: "Details" });
      more.addEventListener("click", function () {
        pre2.hidden = !pre2.hidden;
        more.setAttribute("aria-expanded", String(!pre2.hidden));
        more.textContent = pre2.hidden ? "Details" : "Hide";
      });
      body.appendChild(more);
      body.appendChild(pre2);
    }
    var li = h("li", { class: cls, "data-title": ev.title }, h("span", { class: "cf-t", text: tSec }), icon(iconFor(ev.agent)), body);
    log.appendChild(li);
    if (ev.kind === "tool_call") { state.steps += 1; $("[data-cf-steps]").textContent = String(state.steps); }
    motion().rowIn && motion().rowIn(li);
    scrollLog();
  }
  function showCost(value) {
    var el = $("[data-cf-cost]");
    var from = Number(el.getAttribute("data-value") || 0);
    el.setAttribute("data-value", String(value));
    if (value === from) { el.textContent = usd(value); return; }
    el.classList.add("is-ticking");
    if (motion().count) motion().count(el, value, { from: from, decimals: 4, prefix: "$", duration: 0.8 });
    else el.textContent = usd(value);
    clearTimeout(showCost.t);
    showCost.t = setTimeout(function () { el.classList.remove("is-ticking"); }, 900);
  }
  function addSpend(v) { state.spent += v; showCost(state.spent); }
  function startTimer() {
    state.startedAt = performance.now();
    clearInterval(state.timer);
    state.timer = setInterval(function () {
      $("[data-cf-time]").textContent = ((performance.now() - state.startedAt) / 1000).toFixed(1) + "s";
    }, 100);
  }
  function stopTimer(seconds) {
    clearInterval(state.timer);
    if (seconds != null) $("[data-cf-time]").textContent = Number(seconds).toFixed(1) + "s";
  }

  /* ------------------------------------------------------------------ results */
  function footnoteIndex(law) {
    var byRule = {}, byCite = {};
    law.forEach(function (a, i) {
      var n = i + 1;
      (a.rule_ids || []).forEach(function (r) { (byRule[r] = byRule[r] || []).push(n); });
      byCite[a.citation] = n;
    });
    return {
      refs: function (ruleIds, citations) {
        var ns = [];
        (ruleIds || []).forEach(function (r) { (byRule[r] || []).forEach(function (n) { if (ns.indexOf(n) < 0) ns.push(n); }); });
        (citations || []).forEach(function (c) { var n = byCite[c]; if (n && ns.indexOf(n) < 0) ns.push(n); });
        ns.sort(function (a, b) { return a - b; });
        if (!ns.length) return null;
        var sup = h("sup", { class: "fn" });
        ns.forEach(function (n, i) {
          if (i) sup.appendChild(document.createTextNode(","));
          var a = h("a", { href: "#fn-" + n, "aria-label": "Footnote " + n, text: String(n) });
          a.addEventListener("click", function (e) {
            e.preventDefault();
            var target = document.getElementById("fn-" + n);
            if (!target) return;
            motion().scrollTo ? motion().scrollTo(target) : target.scrollIntoView();
            target.classList.add("is-target");
            setTimeout(function () { target.classList.remove("is-target"); }, 1800);
          });
          sup.appendChild(a);
        });
        return sup;
      },
    };
  }
  function verdict(opts) {
    var v = h("article", { class: "verdict " + (opts.bad ? "is-bad" : "is-good") });
    v.appendChild(h("p", { class: "v-kicker", text: opts.kicker }));
    if (opts.stamp) v.appendChild(h("p", { class: "stamp stamp-red", "data-stamp": true, text: opts.stamp }));
    var fig = h("p", { class: "v-figure" });
    if (opts.figureSmall) fig.appendChild(h("small", { text: opts.figureSmall + " " }));
    fig.appendChild(document.createTextNode(opts.figure));
    if (opts.figureUnit) fig.appendChild(h("small", { text: " " + opts.figureUnit }));
    v.appendChild(fig);
    var caption = h("p", { class: "v-caption", text: opts.caption });
    v.appendChild(caption);
    var text = h("p", { class: "v-text" });
    opts.text.forEach(function (part) {
      if (typeof part === "string") text.appendChild(document.createTextNode(part));
      else if (part && part.b) text.appendChild(h("b", { text: part.b }));
      else if (part) text.appendChild(part);
    });
    v.appendChild(text);
    return v;
  }
  function renderVerdicts(d, fn) {
    var wrap = $("[data-verdicts]");
    wrap.innerHTML = "";
    var r = d.rent, n = d.notice;
    if (r && r.max_increase_percent != null) {
      var refs = fn.refs(r.rule_ids, r.citations);
      if (r.is_within_cap === false) {
        wrap.appendChild(verdict({
          bad: true, kicker: "The rent increase", stamp: "Over the cap", figureSmall: "AED",
          figure: Math.round(r.excess_over_cap).toLocaleString("en-US"), caption: "over the legal cap",
          text: ["Your landlord asks ", { b: aed(r.proposed_new_rent) }, ", a rise of " + pct(r.proposed_increase_percent) + ". Your rent of " +
            aed(r.current_annual_rent) + " is " + pct(r.percent_below_average) + " below the RERA average of " + aed(r.rera_average_rent) +
            ", so the cap is ", { b: pct(r.max_increase_percent) }, ": at most ", { b: aed(r.max_allowed_new_rent) }, " a year.", refs],
        }));
      } else if (r.is_within_cap === true) {
        wrap.appendChild(verdict({
          bad: false, kicker: "The rent increase", figure: "Within", caption: "the legal cap",
          text: ["The landlord asks " + aed(r.proposed_new_rent) + " (" + pct(r.proposed_increase_percent) + "). With your rent " +
            pct(r.percent_below_average) + " below the RERA average, the cap is " + pct(r.max_increase_percent) + ", up to " +
            aed(r.max_allowed_new_rent) + " a year.", refs],
        }));
      } else {
        wrap.appendChild(verdict({
          bad: false, kicker: "The rent cap for your home", figure: pct(r.max_increase_percent), caption: r.max_increase_percent ? "the most your rent may rise" : "no increase allowed",
          text: ["Your rent is " + pct(r.percent_below_average) + " below the RERA average (" + r.band + "), so at renewal it may go up to " +
            aed(r.max_allowed_new_rent) + " a year. Add the landlord's proposed rent to check a specific figure.", refs],
        }));
      }
    }
    if (n && n.last_valid_notice_date) {
      var nrefs = fn.refs(n.rule_ids, n.citations);
      var delivery = n.delivery_note ? " " + n.delivery_note : "";
      if (n.notice_on_time === false) {
        wrap.appendChild(verdict({
          bad: true, kicker: "The notice", stamp: "Late", figure: String(n.days_late != null ? n.days_late : "?"),
          figureUnit: n.days_late === 1 ? "day" : "days", caption: "too late",
          text: ["It arrived on " + nice(n.notice_date) + ", " + n.notice_days_before_expiry + " days before your contract ends on " +
            nice(n.contract_end_date) + ". The rule is " + n.requirement + ", so the last valid date was ", { b: nice(n.last_valid_notice_date) }, "." + delivery, nrefs],
        }));
      } else if (n.notice_on_time === true) {
        wrap.appendChild(verdict({
          bad: n.delivery_method_ok === false, kicker: "The notice", stamp: n.delivery_method_ok === false ? "Wrong delivery" : null,
          figure: "On time", caption: n.notice_days_before_expiry + " days before expiry",
          text: ["The rule is " + n.requirement + "; the last valid date was " + nice(n.last_valid_notice_date) + "." + delivery, nrefs],
        }));
      } else {
        wrap.appendChild(verdict({
          bad: false, kicker: "The notice deadline", figure: nice(n.last_valid_notice_date), caption: "the last valid date for a notice",
          text: ["The rule is " + n.requirement + ". Your contract ends on " + nice(n.contract_end_date) + ". Add the date you received a notice to check it.", nrefs],
        }));
      }
    }
    if (!wrap.children.length) {
      wrap.appendChild(verdict({
        bad: false, kicker: "No rent or notice check", figure: "—", caption: "nothing to measure yet",
        text: ["Add the RERA average rent and the landlord's notice in step 02, then run the check again to see whether the increase and the timing are legal."],
      }));
    }
  }
  function renderTerms(d) {
    var t = d.terms, wrap = $("[data-terms-wrap]"), grid = $("[data-terms]");
    grid.innerHTML = "";
    if (!t) { wrap.hidden = true; return; }
    function row(label, value) { if (value) grid.appendChild(h("div", null, h("dt", { text: label }), h("dd", { text: value }))); }
    row("Landlord", t.landlord);
    row("Tenant", t.tenant);
    row("Property", t.property);
    row("Annual rent", t.annual_rent_aed ? aed(t.annual_rent_aed) : null);
    row("Period", t.start_date || t.end_date ? nice(t.start_date) + " → " + nice(t.end_date) : null);
    var dep = d.derived && d.derived.deposit_percent_of_annual_rent;
    row("Security deposit", t.security_deposit_aed ? aed(t.security_deposit_aed) + (dep ? " (" + pct(dep) + " of the rent)" : "") : null);
    row("Cheques", t.number_of_cheques ? String(t.number_of_cheques) : null);
    if (t.mentions_ejari != null) row("Mentions Ejari", t.mentions_ejari ? "Yes" : "No");
    wrap.hidden = !grid.children.length;
  }
  function renderFlags(d, fn) {
    var wrap = $("[data-flags-wrap]"), list = $("[data-flags]");
    list.innerHTML = "";
    var flags = d.flags || [];
    $("[data-flags-label]").textContent = flags.length ? "Red flags · " + flags.length + " found by the clause scan" : "Red flags";
    flags.forEach(function (f) {
      var refs = fn.refs(f.rule_ids, f.citations);
      var content = h("div", { class: "flag-body" },
        h("h4", { text: f.title }),
        h("p", { class: "flag-where", text: f.clause_number ? "Clause " + f.clause_number : "The whole contract" }),
        f.clause_excerpt ? h("blockquote", { text: "“" + f.clause_excerpt + "”" }) : null,
        h("p", { class: "flag-why" }, f.why_it_matters, refs));
      list.appendChild(h("li", { class: "flag" }, h("p", { class: "sev sev-" + f.severity, text: SEVERITY[f.severity] || f.severity }), content));
    });
    wrap.hidden = !flags.length;
  }
  function renderLaw(d) {
    var wrap = $("[data-law-wrap]"), list = $("[data-law]");
    list.innerHTML = "";
    (d.law || []).forEach(function (a, i) {
      var text = a.text.length > 520 ? a.text.slice(0, 517).replace(/\s+\S*$/, "") + "…" : a.text;
      list.appendChild(h("li", { class: "note", id: "fn-" + (i + 1) },
        h("p", { class: "note-n", text: String(i + 1) }),
        h("div", null,
          h("p", { class: "note-cite", text: a.citation }),
          h("p", { class: "note-title", text: a.title }),
          h("p", { class: "note-text", text: "“" + text + "”" }),
          a.source_url ? h("a", { class: "note-link", href: a.source_url, target: "_blank", rel: "noopener", text: "Official text (PDF) ↗" }) : null)));
    });
    wrap.hidden = !(d.law && d.law.length);
  }
  function renderBill(d) {
    var bill = $("[data-bill]");
    bill.innerHTML = "";
    bill.appendChild(h("p", { class: "bill-title", text: "What this check cost" }));
    function line(name, sub, amt, cls) {
      bill.appendChild(h("p", { class: "bill-row " + (cls || "") }, h("span", { text: name }), h("span", { class: "lead" }), h("span", { text: sub }), h("span", { class: "amt", text: amt })));
    }
    var byAgent = d.cost.by_agent || {};
    var names = Object.keys(byAgent);
    if (state.mode === "llm" && names.length) {
      names.forEach(function (n) {
        var r = byAgent[n];
        line(n, r.requests + (r.requests === 1 ? " call" : " calls"), usd(r.cost_usd));
      });
      (d.agents || []).forEach(function (n) { if (!byAgent[n]) line(n, "code", "$0.0000"); });
    } else {
      (d.agents || []).forEach(function (n) { line(n, "code", "$0.0000"); });
    }
    line("Total", d.cost.tokens ? d.cost.tokens.toLocaleString("en-US") + " tokens" : "", usd(d.cost.usd), "bill-total");
    var note;
    if (d.cached) note = "Answered from the local cache: this exact request was run before, so no API call was made.";
    else if (state.mode === "llm") note = d.cost.summary + (d.cost.cache_share != null ? " Prompt caching re-reads shared context at a tenth of the price." : "");
    else note = "Demo mode makes no API calls. With the AI team on, a full check with a reviewed letter measured about $0.017 on the sample.";
    bill.appendChild(h("p", { class: "bill-note", text: note }));
  }

  /* The letter: English and Arabic (right to left), copy and download. */
  function letterText(tab) {
    var L = state.letter;
    if (!L) return "";
    return tab === "ar" ? (L.body_arabic || "") : (L.body || "");
  }
  function renderLetter(defaultTab) {
    var L = state.letter, wrap = $("[data-letter-wrap]");
    if (!L) { wrap.hidden = true; return; }
    wrap.hidden = false;
    var version = String(L.version || "draft 1 (the template, filled in by code)");
    $("[data-letter-version]").textContent = cap(version.replace(/\s*\((.*)\)$/, ", $1")) + ".";
    setLetterTab(defaultTab || state.letterTab || "en");
    var review = $("[data-review]");
    review.innerHTML = "";
    var objections = L.objections || [];
    if (objections.length) {
      var list = h("ol", { class: "review-list" });
      objections.forEach(function (o) {
        list.appendChild(h("li", null, h("p", { class: "sev sev-" + (o.severity || "medium"), text: (o.source === "Critic" ? "Critic · " : "Check · ") + (SEVERITY[o.severity] || "") }),
          h("div", null, o.quote ? h("q", { text: o.quote }) : null, h("p", { text: o.problem }), o.fix ? h("p", { text: "Fix: " + o.fix }) : null)));
      });
      review.appendChild(h("div", { class: "review-block" }, h("p", { class: "block-label", text: "What the landlord's lawyer said about draft 1" }), list));
    }
    if (L.changes_made && L.changes_made.length) {
      var ch = h("ul", { class: "review-list" });
      L.changes_made.forEach(function (c) { ch.appendChild(h("li", null, h("p", { class: "sev sev-low", text: "Changed" }), h("p", { text: c }))); });
      review.appendChild(h("div", { class: "review-block" }, h("p", { class: "block-label", text: "What the Drafter changed in draft 2" }), ch));
    }
    var checks = (state.digest && state.digest.checks) || [];
    var checkLines = checks.map(function (c) { return c.title; });
    if (state.mode !== "llm") checkLines.push("The Critic's Claude review and the Drafter's revision are off in demo mode; with the AI team on, the landlord's lawyer reads every draft first.");
    if (L.unverified && L.unverified.length) checkLines.push("Could not verify: " + L.unverified.map(function (u) { return u.quote; }).join("; ") + ". Check these before sending.");
    if (checkLines.length) {
      var cb = h("div", { class: "review-block" }, h("p", { class: "block-label", text: "Checks on this letter" }));
      checkLines.forEach(function (t) { cb.appendChild(h("p", { class: "review-plain", text: t })); });
      review.appendChild(cb);
    }
  }
  function setLetterTab(tab) {
    state.letterTab = tab;
    $$("[data-letter-tab]").forEach(function (b) { b.setAttribute("aria-selected", String(b.getAttribute("data-letter-tab") === tab)); });
    var L = state.letter, body = $("[data-letter-body]"), note = $("[data-letter-note]");
    if (!L) return;
    note.innerHTML = "";
    note.classList.remove("is-top");
    if (tab === "ar" && L.body_arabic) {
      body.setAttribute("dir", "rtl");
      body.setAttribute("lang", "ar");
      body.textContent = L.body_arabic;
      note.appendChild(h("strong", { text: "The Arabic text of Dubai's laws is the original and prevails. " }));
      note.appendChild(document.createTextNode("Haqq quotes the official English translation; the citations in this Arabic letter were converted by code (law number, year and article), not by the AI. Check them against the Arabic originals: "));
      (state.meta.arabic_sources || []).forEach(function (s, i) {
        if (i) note.appendChild(document.createTextNode(", "));
        note.appendChild(h("a", { href: s.url, target: "_blank", rel: "noopener", lang: "ar", text: s.name }));
      });
      note.appendChild(document.createTextNode(". Not legal advice."));
      note.hidden = false;
    } else if (tab === "ar") {
      body.removeAttribute("dir");
      body.removeAttribute("lang");
      body.textContent = L.body;
      note.appendChild(h("strong", { text: "No Arabic version this time. " }));
      note.appendChild(document.createTextNode((L.arabic_unavailable || "") + " The English letter is shown below."));
      note.classList.add("is-top");
      note.hidden = false;
    } else {
      body.removeAttribute("dir");
      body.removeAttribute("lang");
      body.textContent = L.body;
      note.hidden = true;
    }
  }
  function copyLetter() {
    var text = letterText(state.letterTab === "ar" && state.letter && state.letter.body_arabic ? "ar" : "en");
    var done = function () { toast("Letter copied"); };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback);
    else fallback();
    function fallback() {
      var ta = h("textarea", { style: "position:fixed;opacity:0" });
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); done(); } catch (e) { toast("Select the letter and copy it"); }
      ta.remove();
    }
  }
  function downloadLetter() {
    var ar = state.letterTab === "ar" && state.letter && state.letter.body_arabic;
    var blob = new Blob(["﻿" + letterText(ar ? "ar" : "en")], { type: "text/plain;charset=utf-8" });
    var a = h("a", { href: URL.createObjectURL(blob), download: "haqq-letter-" + (ar ? "arabic" : "english") + ".txt" });
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }

  function renderResults(d) {
    state.digest = d;
    var fn = footnoteIndex(d.law || []);
    var results = $("[data-results]");
    results.hidden = false;
    var bits = [d.steps + " steps", d.agents.length + " agents", d.elapsed_s < 0.1 ? "under 0.1s" : d.elapsed_s + "s", usd(d.cost.usd)];
    if (d.cached) bits.push("from the cache");
    $("[data-results-meta]").textContent = "Case " + state.caseNo + " · " + bits.join(" · ");
    renderVerdicts(d, fn);
    renderTerms(d);
    renderFlags(d, fn);
    var answerWrap = $("[data-answer-wrap]"), reportWrap = $("[data-report-wrap]");
    if (state.mode === "llm") {
      $("[data-answer]").innerHTML = d.answer_html;
      answerWrap.hidden = !d.answer_text;
      reportWrap.hidden = true;
    } else {
      answerWrap.hidden = true;
      $("[data-report]").innerHTML = d.answer_html;
      reportWrap.hidden = !d.answer_text;
    }
    externalLinks(results);
    state.letter = d.letter || state.letter;
    var lang = state.lastLanguage;
    renderLetter(state.letter && state.letter.body_arabic && lang === "ar" ? "ar" : "en");
    renderLaw(d);
    renderBill(d);
    if (motion().refresh) motion().refresh();
    if (motion().reveal) motion().reveal($$(".results > *:not([hidden])"), 0.07);
    watchStamps();
  }
  function externalLinks(scope) {
    $$(".prose a[href^='http']", scope).forEach(function (a) { a.target = "_blank"; a.rel = "noopener"; });
  }
  function watchStamps() {
    var stamps = $$("[data-stamp]");
    stamps.forEach(function (s) { if (motion().animate) s.style.opacity = "0"; });
    if (!("IntersectionObserver" in window) || !motion().animate) { stamps.forEach(function (s) { s.style.opacity = ""; }); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en, i) {
        if (!en.isIntersecting) return;
        io.unobserve(en.target);
        setTimeout(function () { motion().stamp(en.target, -5); }, 350 + i * 220);
      });
    }, { threshold: 0.6 });
    stamps.forEach(function (s) { io.observe(s); });
  }

  /* -------------------------------------------------------------- run a turn */
  function clearTurnUI() {
    $("[data-results]").hidden = true;
    state.letter = null;
    state.digest = null;
    $("[data-thread]").innerHTML = "";
  }
  function setRunning(on) {
    state.running = on;
    updateRunButton();
    $("[data-ask-send]").disabled = on;
    $$('[data-action="sample-contract"], [data-action="use-paste"], [data-action="reset"]').forEach(function (b) { b.disabled = on; });
  }
  function runTurn(path, payload, label, onFinal) {
    var pacer = new Pacer(motion().reduce ? 0 : state.mode === "llm" ? 60 : 130);
    var turnBase = state.spent;
    var failed = null;
    setRunning(true);
    caseNumber();
    caseDivider(label);
    setStatus("Working");
    railReset();
    state.steps = 0;
    $("[data-cf-steps]").textContent = "0";
    startTimer();
    return stream(path, payload, function (name, data) {
      if (name === "step") {
        pacer.push(function () {
          addRow(data);
          if (data.cost_usd) showCost(turnBase + data.cost_usd);
        });
      } else if (name === "final") {
        pacer.push(function () {
          stopTimer(data.elapsed_s);
          state.spent = turnBase + (data.cost.usd || 0);
          showCost(state.spent);
          railActive(null);
          setStatus("Closed");
          updateAskLimit(data.requests_used);
          return onFinal(data);
        });
      } else if (name === "error") {
        failed = data.message;
      }
    }).then(function () { return pacer.idle(); }).catch(function (e) { failed = e.message; }).then(function () {
      stopTimer();
      setRunning(false);
      if (failed) { setStatus("Stopped"); throw new Error(failed); }
    });
  }
  function runCheck() {
    if (state.running || !state.contract) return;
    var form = formValues();
    state.lastLanguage = form.letter_language;
    state.letter = null;
    $("[data-results]").hidden = true;
    msg("run", "");
    if (motion().scrollTo) motion().scrollTo(window.matchMedia("(max-width: 960px)").matches ? $("[data-casefile]") : $(".workspace"));
    runTurn("api/analyze", form, "Analysis · " + new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }), function (d) {
      state.firstRun = false;
      renderResults(d);
      return sleep(motion().reduce ? 0 : 450).then(function () { if (motion().scrollTo) motion().scrollTo($("[data-results]")); });
    }).catch(function (e) { msg("run", e.message, true); });
  }
  function ask(question) {
    question = (question || "").trim();
    if (!question || state.running) return;
    var thread = $("[data-thread]");
    var answer = h("div", { class: "a prose" });
    var working = h("p", { class: "a-working", text: "The team is working…" });
    var item = h("li", null, h("p", { class: "q", text: question }), working, answer);
    thread.appendChild(item);
    if (motion().reveal) motion().reveal([item]);
    $("#ask-input").value = "";
    autosize();
    var payload = formValues();
    payload.message = question;
    state.lastLanguage = payload.letter_language;
    runTurn("api/ask", payload, "Question · " + (question.length > 46 ? question.slice(0, 45) + "…" : question), function (d) {
      working.remove();
      answer.innerHTML = d.answer_html;
      externalLinks(item);
      var meta = [d.steps + (d.steps === 1 ? " step" : " steps"), d.elapsed_s + "s", usd(d.cost.usd)];
      item.appendChild(h("p", { class: "a-meta", text: meta.join(" · ") }));
      if (d.letter) {
        state.digest = state.digest || d;
        state.letter = d.letter;
        $("[data-results]").hidden = false;
        renderLetter(d.letter.body_arabic && state.lastLanguage === "ar" ? "ar" : "en");
        toast("The letter panel has a new draft");
      }
    }).catch(function (e) {
      working.textContent = e.message;
      working.classList.add("is-error");
    });
  }
  function resetConversation() {
    if (state.running) return;
    api("api/reset", { method: "POST" }).then(function () {
      clearTurnUI();
      $("[data-cf-log]").innerHTML = "";
      $("[data-cf-log]").appendChild(h("li", { class: "cf-empty" }, h("p", { text: "New conversation, same contract. Run the check or ask a question." })));
      railReset();
      setStatus("Idle");
      toast("New conversation started");
    }).catch(function (e) { toast(e.message); });
  }
  function autosize() {
    var ta = $("#ask-input");
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 240) + "px";
  }

  /* ------------------------------------------------------------------ wiring */
  function wireDrop(zone, input, handler) {
    zone.addEventListener("click", function (e) {
      if (zone.classList.contains("is-loaded") && zone.getAttribute("data-drop") === "contract") return;
      if (e.target.closest("button, a")) return;
      input.click();
    });
    zone.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
    });
    ["dragenter", "dragover"].forEach(function (t) {
      zone.addEventListener(t, function (e) { e.preventDefault(); zone.classList.add("is-over"); });
    });
    ["dragleave", "drop"].forEach(function (t) {
      zone.addEventListener(t, function (e) { e.preventDefault(); zone.classList.remove("is-over"); });
    });
    zone.addEventListener("drop", function (e) {
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) handler(f);
    });
    input.addEventListener("change", function () { if (input.files[0]) handler(input.files[0]); input.value = ""; });
  }

  function init() {
    buildRail();
    wireDrop($('[data-drop="contract"]'), $("#contract-file"), uploadContract);
    wireDrop($('[data-drop="notice"]'), $("#notice-file"), readNoticeFile);
    document.addEventListener("click", function (e) {
      var b = e.target.closest("[data-action]");
      if (!b || b.disabled) return;
      var act = b.getAttribute("data-action");
      if (act === "pick-contract") $("#contract-file").click();
      else if (act === "toggle-paste") {
        var box = $("#paste-box");
        box.hidden = !box.hidden;
        b.setAttribute("aria-expanded", String(!box.hidden));
        if (!box.hidden) $("#paste-text").focus();
      }
      else if (act === "use-paste") usePaste();
      else if (act === "sample-contract") loadSample();
      else if (act === "pick-notice") $("#notice-file").click();
      else if (act === "sample-notice") handleNotice(api("api/notice/sample", { method: "POST" }));
      else if (act === "run") runCheck();
      else if (act === "copy-letter") copyLetter();
      else if (act === "download-letter") downloadLetter();
      else if (act === "reset") resetConversation();
    });
    $$("[data-letter-tab]").forEach(function (b) {
      b.addEventListener("click", function () { setLetterTab(b.getAttribute("data-letter-tab")); });
      b.addEventListener("keydown", function (e) {
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
          var next = b.getAttribute("data-letter-tab") === "en" ? "ar" : "en";
          setLetterTab(next);
          $('[data-letter-tab="' + next + '"]').focus();
        }
      });
    });
    ["#f-rera", "#f-proposed"].forEach(function (id) {
      $(id).addEventListener("blur", function () { formatNumberInput($(id)); updateRunNote(); });
    });
    $("#f-date").addEventListener("change", updateRunNote);
    $("[data-form]").addEventListener("click", function (e) { if (e.target.closest(".seg button")) setTimeout(updateRunNote, 0); });
    $("[data-form]").addEventListener("submit", function (e) { e.preventDefault(); });
    var askForm = $("[data-ask-form]");
    askForm.addEventListener("submit", function (e) { e.preventDefault(); ask($("#ask-input").value); });
    $("#ask-input").addEventListener("input", autosize);
    $("#ask-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask($("#ask-input").value); }
    });
    $$("[data-suggest] button").forEach(function (b) { b.addEventListener("click", function () { ask(b.textContent); }); });

    api("api/meta").then(applyMeta).catch(function (e) {
      msg("contract", e.message, true);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
