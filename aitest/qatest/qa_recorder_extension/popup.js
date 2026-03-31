async function getActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs && tabs.length ? tabs[0] : null;
}

function setText(el, text) {
  if (el) el.textContent = text;
}

async function bg(msg) {
  return await chrome.runtime.sendMessage(msg);
}

async function refresh() {
  const stat = document.getElementById("stat");
  const out = document.getElementById("output");
  const res = await bg({ type: "qa_recorder_status" });
  if (!res || !res.ok) {
    setText(stat, "状态：异常");
    return;
  }
  const rec = !!res.recording;
  const count = Number(res.count || 0);
  const t = rec ? "录制中" : "未录制";
  setText(stat, `状态：${t}，步骤：${count}`);
  if (!rec && out && !out.value) {
    const payloadRes = await bg({ type: "qa_recorder_get_payload" });
    if (payloadRes && payloadRes.ok) out.value = JSON.stringify(payloadRes.payload, null, 2);
  }
  document.getElementById("btnStart").disabled = rec;
  document.getElementById("btnStop").disabled = !rec;
}

async function start() {
  const out = document.getElementById("output");
  if (out) out.value = "";
  const tab = await getActiveTab();
  const url = tab && tab.url ? String(tab.url) : "";
  const tabId = tab && typeof tab.id === "number" ? tab.id : null;
  await bg({ type: "qa_recorder_start", startUrl: url, tabId });
  await refresh();
}

async function stop() {
  const tab = await getActiveTab();
  const tabId = tab && typeof tab.id === "number" ? tab.id : null;
  const tabUrl = tab && tab.url ? String(tab.url) : "";
  const res = await bg({ type: "qa_recorder_stop", tabId, tabUrl });
  const out = document.getElementById("output");
  if (out && res && res.ok && res.payload) out.value = JSON.stringify(res.payload, null, 2);
  await refresh();
}

async function copyJson() {
  const out = document.getElementById("output");
  const text = out ? out.value : "";
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    try {
      out.select();
      document.execCommand("copy");
    } catch (e2) {}
  }
}

document.getElementById("btnStart").addEventListener("click", start);
document.getElementById("btnStop").addEventListener("click", stop);
document.getElementById("btnCopy").addEventListener("click", copyJson);

refresh();
