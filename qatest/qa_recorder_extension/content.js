function qsAll(sel, root) {
  try {
    return Array.from((root || document).querySelectorAll(sel));
  } catch (e) {
    return [];
  }
}

function visible(el) {
  try {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (!r || r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    if (!st) return true;
    if (st.display === "none" || st.visibility === "hidden" || Number(st.opacity || 1) === 0) return false;
    return true;
  } catch (e) {
    return true;
  }
}

function attr(el, name) {
  try {
    return el.getAttribute(name) || "";
  } catch (e) {
    return "";
  }
}

function textOf(el) {
  try {
    const t = (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
    return t.slice(0, 80);
  } catch (e) {
    return "";
  }
}

function cssEscape(s) {
  try {
    return CSS.escape(s);
  } catch (e) {
    return String(s || "").replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }
}

function buildSelector(el) {
  if (!el || el.nodeType !== 1) return { by: "css", selector: "" };
  const testid = attr(el, "data-testid") || attr(el, "data-test") || attr(el, "data-qa");
  if (testid) return { by: "css", selector: `[data-testid="${cssEscape(testid)}"],[data-test="${cssEscape(testid)}"],[data-qa="${cssEscape(testid)}"]` };
  const id = el.id || "";
  if (id && id.length < 80) return { by: "css", selector: `#${cssEscape(id)}` };
  const name = attr(el, "name");
  if (name && name.length < 80) return { by: "css", selector: `${el.tagName.toLowerCase()}[name="${cssEscape(name)}"]` };
  const aria = attr(el, "aria-label");
  if (aria && aria.length < 80) return { by: "text", selector: aria };
  const ph = attr(el, "placeholder");
  if (ph && ph.length < 80) return { by: "text", selector: ph };
  const t = textOf(el);
  if (t) return { by: "text", selector: t };
  const path = [];
  let cur = el;
  for (let i = 0; i < 4 && cur && cur.nodeType === 1; i++) {
    const tag = cur.tagName.toLowerCase();
    const cls = (cur.className || "").toString().split(/\s+/).filter(Boolean).filter(c => c.length < 32 && !/\d{4,}/.test(c)).slice(0, 2);
    let part = tag;
    if (cls.length) part += "." + cls.map(cssEscape).join(".");
    const parent = cur.parentElement;
    if (parent) {
      const sib = Array.from(parent.children).filter(x => x.tagName === cur.tagName);
      if (sib.length > 1) part += `:nth-of-type(${sib.indexOf(cur) + 1})`;
    }
    path.unshift(part);
    cur = cur.parentElement;
  }
  return { by: "css", selector: path.join(" > ") };
}

function normalizeInputValue(el) {
  try {
    if (!el) return "";
    if (el.type === "password") return "***";
    return String(el.value == null ? "" : el.value).slice(0, 200);
  } catch (e) {
    return "";
  }
}

function isEditable(el) {
  if (!el) return false;
  const tag = (el.tagName || "").toLowerCase();
  if (tag === "textarea") return true;
  if (tag === "input") return true;
  if (attr(el, "contenteditable") === "true") return true;
  return false;
}

let lastInputTs = 0;
let lastInputKey = "";
let liveEndpoint = "";
let cmdEndpoint = "";
let cmdTimer = null;
let recordingEnabled = false;
let cmdPollInFlight = false;

try {
  const params = new URLSearchParams(location.search || "");
  const t = params.get("__qa_recorder_token") || "";
  const h = params.get("__qa_recorder_host") || "";
  if (t && h && /^https?:\/\//i.test(h)) {
    liveTokenFromUrl = t;
    liveHostFromUrl = h;
    liveEndpoint = `${h.replace(/\/+$/, "")}/autotest/record/session/${encodeURIComponent(t)}/event/`;
    cmdEndpoint = `${h.replace(/\/+$/, "")}/autotest/record/session/${encodeURIComponent(t)}/commands/poll/`;
    try {
      chrome.runtime.sendMessage({ type: "qa_recorder_set_live", liveToken: t, liveHost: h });
    } catch (e) {}
  }
} catch (e) {
  liveEndpoint = "";
  cmdEndpoint = "";
}

async function readDisabledTokenOnce() {
  const t = String(liveTokenFromUrl || "").trim();
  if (!t) return false;
  try {
    const res = await chrome.storage.local.get(["qa_recorder_disabled_tokens"]);
    const obj = (res && res.qa_recorder_disabled_tokens && typeof res.qa_recorder_disabled_tokens === "object") ? res.qa_recorder_disabled_tokens : {};
    const ts = obj && obj[t] ? Number(obj[t]) : 0;
    if (!ts) return false;
    return (Date.now() - ts) < 2 * 3600 * 1000;
  } catch (e) {
    return false;
  }
}

async function initFromStoredLive() {
  if (liveEndpoint && cmdEndpoint) return;
  try {
    const res = await chrome.runtime.sendMessage({ type: "qa_recorder_get_live" });
    const live = res && res.ok ? res.live : null;
    const t = live && live.token ? String(live.token) : "";
    const h = live && live.host ? String(live.host) : "";
    if (t && h && /^https?:\/\//i.test(h)) {
      if (!liveTokenFromUrl) liveTokenFromUrl = t;
      if (!liveHostFromUrl) liveHostFromUrl = h;
      liveEndpoint = `${h.replace(/\/+$/, "")}/autotest/record/session/${encodeURIComponent(t)}/event/`;
      cmdEndpoint = `${h.replace(/\/+$/, "")}/autotest/record/session/${encodeURIComponent(t)}/commands/poll/`;
    }
  } catch (e) {}
}

let lastPostTs = 0;
let liveMode = false;
let liveTokenFromUrl = "";
let liveHostFromUrl = "";
let liveDisabled = false;

async function postStep(step) {
  if (!recordingEnabled) return;
  if (!liveEndpoint) return;
  const now = Date.now();
  if (now - lastPostTs < 60) return;
  lastPostTs = now;
  try {
    await fetch(liveEndpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ step })
    });
  } catch (e) {}
}

function sendStep(step) {
  if (!recordingEnabled) return;
  const s = step || {};
  if (!s.ts_ms) s.ts_ms = Date.now();
  try {
    chrome.runtime.sendMessage({ type: "qa_recorder_step", step: s });
  } catch (e) {}
  postStep(s);
}

function findByText(text) {
  const q = String(text || "").trim();
  if (!q) return null;
  const candidates = []
    .concat(qsAll("button"))
    .concat(qsAll("a"))
    .concat(qsAll("[role='button']"))
    .concat(qsAll("[role='link']"))
    .concat(qsAll("input[type='submit']"))
    .concat(qsAll("input[type='button']"));
  for (const el of candidates) {
    if (!visible(el)) continue;
    const t = textOf(el);
    if (!t) continue;
    if (t === q || t.indexOf(q) !== -1) return el;
  }
  return null;
}

async function execCommand(cmd) {
  const a = String((cmd && cmd.action) || "").toLowerCase();
  const by = String((cmd && cmd.by) || "").toLowerCase();
  const sel = String((cmd && cmd.selector) || "");
  const val = cmd && cmd.value != null ? String(cmd.value) : "";
  const waitMs = Number(cmd && cmd.wait_ms ? cmd.wait_ms : 0);
  if (a === "stop_recording" || a === "__stop_recording__") {
    liveMode = false;
    liveEndpoint = "";
    cmdEndpoint = "";
    setRecordingEnabled(false);
    try { chrome.runtime.sendMessage({ type: "qa_recorder_stop_silent", tabId: null, token: String(liveTokenFromUrl || "") }); } catch (e) {}
    try { chrome.runtime.sendMessage({ type: "qa_recorder_disable_token", token: String(liveTokenFromUrl || "") }); } catch (e) {}
    return;
  }
  if (a === "wait") {
    const ms = waitMs || Number(val || 0) || 200;
    await new Promise(r => setTimeout(r, Math.max(0, ms)));
    return;
  }
  if (a === "goto") {
    const u = val || String((cmd && cmd.url) || "");
    if (u) location.href = u;
    return;
  }
  let el = null;
  if (by === "css" && sel) {
    try {
      el = document.querySelector(sel);
    } catch (e) {
      el = null;
    }
  } else if (by === "text" && sel) {
    el = findByText(sel);
  }
  if (!el && sel && by !== "text") {
    el = findByText(sel);
  }
  if (a === "click") {
    if (el && typeof el.click === "function") el.click();
    return;
  }
  if (a === "type" || a === "select") {
    if (!el) return;
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "select") {
      el.value = val;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }
    if (isEditable(el)) {
      el.focus();
      el.value = val;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
    return;
  }
  if (a === "press") {
    const key = val || "Enter";
    const ev = new KeyboardEvent("keydown", { key, bubbles: true });
    document.dispatchEvent(ev);
    return;
  }
}

async function pollCommandsOnce() {
  if (!recordingEnabled) return;
  if (!cmdEndpoint) return;
  if (cmdPollInFlight) return;
  cmdPollInFlight = true;
  try {
    const u = `${cmdEndpoint}`;
    const resp = await fetch(u, { method: "GET" });
    const data = await resp.json();
    if (!data || !data.success) return;
    const cmds = data.commands || [];
    if (!cmds.length) return;
    for (const c of cmds) {
      await execCommand(c);
      await new Promise(r => setTimeout(r, 120));
    }
  } catch (e) {}
  finally { cmdPollInFlight = false; }
}

function startCmdPolling() {
  if (cmdTimer) return;
  if (!recordingEnabled) return;
  cmdTimer = setInterval(pollCommandsOnce, 500);
}

function stopCmdPolling() {
  try {
    if (cmdTimer) clearInterval(cmdTimer);
  } catch (e) {}
  cmdTimer = null;
  cmdPollInFlight = false;
}

function setRecordingEnabled(enabled) {
  recordingEnabled = !!enabled;
  if (!recordingEnabled) stopCmdPolling();
}

async function syncRecordingState() {
  if (liveMode && !liveDisabled) {
    setRecordingEnabled(true);
    return;
  }
  try {
    const res = await chrome.runtime.sendMessage({ type: "qa_recorder_status" });
    if (res && res.ok) setRecordingEnabled(!!res.recording);
  } catch (e) {}
}

chrome.runtime.onMessage.addListener((msg) => {
  try {
    if (!msg || msg.type !== "qa_recorder_recording") return;
    setRecordingEnabled(!!msg.recording);
    const live = msg.live && typeof msg.live === "object" ? msg.live : null;
    const t = live && live.token ? String(live.token) : "";
    const h = live && live.host ? String(live.host) : "";
    if (t && h && /^https?:\/\//i.test(h)) {
      liveEndpoint = `${h.replace(/\/+$/, "")}/autotest/record/session/${encodeURIComponent(t)}/event/`;
      cmdEndpoint = `${h.replace(/\/+$/, "")}/autotest/record/session/${encodeURIComponent(t)}/commands/poll/`;
    }
    if (recordingEnabled) {
      if (liveEndpoint) sendStep({ url: location.href, action: "goto", by: "url", selector: "", value: location.href });
      if (cmdEndpoint) startCmdPolling();
    }
  } catch (e) {}
});

(async () => {
  await initFromStoredLive();
  try { liveMode = !!(liveEndpoint && cmdEndpoint && liveTokenFromUrl && liveHostFromUrl); } catch (e) { liveMode = false; }
  try { liveDisabled = liveMode ? await readDisabledTokenOnce() : false; } catch (e) { liveDisabled = false; }
  if (liveMode && !liveDisabled) {
    try {
      await chrome.runtime.sendMessage({
        type: "qa_recorder_live_attach",
        liveToken: String(liveTokenFromUrl || ""),
        liveHost: String(liveHostFromUrl || ""),
        startUrl: String(location.href || ""),
        tabId: null
      });
    } catch (e) {}
  }
  await syncRecordingState();
  if (recordingEnabled) {
    if (liveEndpoint) sendStep({ url: location.href, action: "goto", by: "url", selector: "", value: location.href });
    if (cmdEndpoint) startCmdPolling();
  } else {
    stopCmdPolling();
  }
})();

document.addEventListener(
  "click",
  (e) => {
    const el = e.target instanceof Element ? e.target : null;
    if (!el) return;
    const btn = el.closest("button,a,[role='button'],[role='link'],input[type='button'],input[type='submit']") || el;
    if (!visible(btn)) return;
    const s = buildSelector(btn);
    sendStep({ url: location.href, action: "click", by: s.by, selector: s.selector, value: "" });
  },
  true
);

document.addEventListener(
  "change",
  (e) => {
    const el = e.target instanceof Element ? e.target : null;
    if (!el) return;
    if (!isEditable(el) && el.tagName.toLowerCase() !== "select") return;
    const s = buildSelector(el);
    const val = el.tagName.toLowerCase() === "select" ? attr(el.options[el.selectedIndex] || {}, "value") || (el.value || "") : normalizeInputValue(el);
    const key = `${s.by}:${s.selector}:${val}`;
    const ts = Date.now();
    if (ts - lastInputTs < 500 && key === lastInputKey) return;
    lastInputTs = ts;
    lastInputKey = key;
    sendStep({ url: location.href, action: el.tagName.toLowerCase() === "select" ? "select" : "type", by: s.by, selector: s.selector, value: val });
  },
  true
);

document.addEventListener(
  "keydown",
  (e) => {
    const k = String(e.key || "");
    if (k !== "Enter" && k !== "Escape" && k !== "Tab") return;
    sendStep({ url: location.href, action: "press", by: "", selector: "", value: k });
  },
  true
);

window.addEventListener(
  "beforeunload",
  () => {
    sendStep({ url: location.href, action: "wait", by: "", selector: "", value: "200" });
  },
  true
);
