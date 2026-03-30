const State = {
  recording: false,
  startedAt: 0,
  startUrl: "",
  steps: [],
  lastUrl: "",
  live: null
};

let disabledTokenMap = {};

function now() {
  return Date.now();
}

function reset() {
  State.recording = false;
  State.startedAt = 0;
  State.startUrl = "";
  State.steps = [];
  State.lastUrl = "";
  State.live = null;
}

function exportPayload() {
  return {
    version: "qa-recorder-0.1",
    started_at_ms: State.startedAt,
    start_url: State.startUrl,
    steps: State.steps.slice(0, 5000)
  };
}

async function notifyRecordingChanged(tabId, recording) {
  try {
    if (typeof tabId !== "number") return;
    await chrome.tabs.sendMessage(tabId, {
      type: "qa_recorder_recording",
      recording: !!recording,
      live: State.live && State.live.token && State.live.host ? State.live : null
    });
  } catch (e) {}
}

function parseLiveFromUrl(url) {
  try {
    const u = new URL(String(url || ""));
    const t = u.searchParams.get("__qa_recorder_token") || "";
    const h = u.searchParams.get("__qa_recorder_host") || "";
    if (!t || !h) return null;
    if (!/^https?:\/\//i.test(h)) return null;
    return { token: t, host: h };
  } catch (e) {
    return null;
  }
}

async function markTokenDisabled(token) {
  const t = String(token || "").trim();
  if (!t) return;
  try {
    const res = await chrome.storage.local.get(["qa_recorder_disabled_tokens"]);
    const obj = (res && res.qa_recorder_disabled_tokens && typeof res.qa_recorder_disabled_tokens === "object") ? res.qa_recorder_disabled_tokens : {};
    obj[t] = now();
    disabledTokenMap = obj;
    await chrome.storage.local.set({ qa_recorder_disabled_tokens: obj });
  } catch (e) {}
}

function isTokenDisabled(token) {
  const t = String(token || "").trim();
  if (!t) return false;
  const ts = disabledTokenMap && disabledTokenMap[t] ? Number(disabledTokenMap[t]) : 0;
  if (!ts) return false;
  return (now() - ts) < 2 * 3600 * 1000;
}

async function refreshDisabledTokenMap() {
  try {
    const res = await chrome.storage.local.get(["qa_recorder_disabled_tokens"]);
    const obj = (res && res.qa_recorder_disabled_tokens && typeof res.qa_recorder_disabled_tokens === "object") ? res.qa_recorder_disabled_tokens : {};
    disabledTokenMap = obj;
  } catch (e) {
    disabledTokenMap = {};
  }
}

refreshDisabledTokenMap();

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  try {
    if (msg && msg.type === "qa_recorder_start") {
      reset();
      State.recording = true;
      State.startedAt = now();
      State.startUrl = String(msg.startUrl || "");
      State.lastUrl = State.startUrl;
      const t = String(msg.liveToken || "");
      const h = String(msg.liveHost || "");
      if (t && h) {
        State.live = { token: t, host: h, updatedAt: now() };
      }
      const tabId = (msg && typeof msg.tabId === "number") ? msg.tabId : (sender && sender.tab && typeof sender.tab.id === "number" ? sender.tab.id : null);
      notifyRecordingChanged(tabId, true);
      sendResponse({ ok: true, recording: true });
      return true;
    }
    if (msg && msg.type === "qa_recorder_live_attach") {
      const t = String(msg.liveToken || "");
      const h = String(msg.liveHost || "");
      if (t && h && /^https?:\/\//i.test(h)) {
        if (!State.recording) {
          reset();
          State.recording = true;
          State.startedAt = now();
        }
        State.startUrl = State.startUrl || String(msg.startUrl || "");
        State.lastUrl = State.lastUrl || State.startUrl;
        State.live = { token: t, host: h, updatedAt: now() };
        try { chrome.storage.local.set({ qa_recorder_live: State.live }); } catch (e) {}
      }
      const tabId = (msg && typeof msg.tabId === "number") ? msg.tabId : (sender && sender.tab && typeof sender.tab.id === "number" ? sender.tab.id : null);
      notifyRecordingChanged(tabId, true);
      sendResponse({ ok: true, recording: true });
      return true;
    }
    if (msg && msg.type === "qa_recorder_stop") {
      State.recording = false;
      const tabUrl = String((msg && msg.tabUrl) ? msg.tabUrl : "");
      const liveFromUrl = parseLiveFromUrl(tabUrl);
      if (liveFromUrl && liveFromUrl.token) {
        markTokenDisabled(liveFromUrl.token);
      } else if (State.live && State.live.token) {
        markTokenDisabled(State.live.token);
      }
      const tabId = (msg && typeof msg.tabId === "number") ? msg.tabId : (sender && sender.tab && typeof sender.tab.id === "number" ? sender.tab.id : null);
      notifyRecordingChanged(tabId, false);
      sendResponse({ ok: true, recording: false, payload: exportPayload() });
      return true;
    }
    if (msg && msg.type === "qa_recorder_stop_silent") {
      State.recording = false;
      const t = String(msg.token || "").trim();
      if (t) markTokenDisabled(t);
      const tabId = (msg && typeof msg.tabId === "number") ? msg.tabId : (sender && sender.tab && typeof sender.tab.id === "number" ? sender.tab.id : null);
      notifyRecordingChanged(tabId, false);
      sendResponse({ ok: true, recording: false });
      return true;
    }
    if (msg && msg.type === "qa_recorder_disable_token") {
      const t = String(msg.token || "").trim();
      if (t) markTokenDisabled(t);
      sendResponse({ ok: true });
      return true;
    }
    if (msg && msg.type === "qa_recorder_status") {
      sendResponse({ ok: true, recording: State.recording, count: State.steps.length, startedAt: State.startedAt });
      return true;
    }
    if (msg && msg.type === "qa_recorder_get_payload") {
      sendResponse({ ok: true, payload: exportPayload() });
      return true;
    }
    if (msg && msg.type === "qa_recorder_step") {
      if (!State.recording) {
        sendResponse({ ok: false, error: "not_recording" });
        return true;
      }
      const step = msg.step || {};
      const safe = {
        ts_ms: now(),
        url: String(step.url || ""),
        action: String(step.action || ""),
        selector: String(step.selector || ""),
        by: String(step.by || ""),
        value: typeof step.value === "string" ? step.value : step.value == null ? "" : String(step.value),
        meta: step.meta && typeof step.meta === "object" ? step.meta : {}
      };
      if (safe.url && safe.url !== State.lastUrl) {
        State.lastUrl = safe.url;
        State.steps.push({ ts_ms: safe.ts_ms, url: safe.url, action: "goto", by: "url", selector: "", value: safe.url, meta: {} });
      }
      State.steps.push(safe);
      if (State.steps.length > 5000) State.steps = State.steps.slice(-5000);
      sendResponse({ ok: true, recording: true, count: State.steps.length });
      return true;
    }
    if (msg && msg.type === "qa_recorder_set_live") {
      const t = String(msg.liveToken || "");
      const h = String(msg.liveHost || "");
      if (t && h) {
        State.live = { token: t, host: h, updatedAt: now() };
        try {
          chrome.storage.local.set({ qa_recorder_live: State.live });
        } catch (e) {}
      }
      sendResponse({ ok: true });
      return true;
    }
    if (msg && msg.type === "qa_recorder_get_live") {
      if (State.live && State.live.token && State.live.host) {
        sendResponse({ ok: true, live: State.live });
        return true;
      }
      try {
        chrome.storage.local.get(["qa_recorder_live"], (res) => {
          const live = res && res.qa_recorder_live ? res.qa_recorder_live : null;
          sendResponse({ ok: true, live });
        });
        return true;
      } catch (e) {
        sendResponse({ ok: true, live: null });
        return true;
      }
    }
  } catch (e) {
    sendResponse({ ok: false, error: String(e && e.message ? e.message : e) });
    return true;
  }
  sendResponse({ ok: false, error: "unknown_message" });
  return true;
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  try {
    const url = String((changeInfo && changeInfo.url) ? changeInfo.url : "");
    if (!url) return;
    const live = parseLiveFromUrl(url);
    if (!live) return;
    if (isTokenDisabled(live.token)) return;
    State.live = { token: live.token, host: live.host, updatedAt: now() };
    try { chrome.storage.local.set({ qa_recorder_live: State.live }); } catch (e) {}
    try {
      if (!State.recording) {
        State.recording = true;
        State.startedAt = State.startedAt || now();
        State.startUrl = State.startUrl || url;
      }
    } catch (e) {}
    notifyRecordingChanged(tabId, true);
  } catch (e) {}
});
