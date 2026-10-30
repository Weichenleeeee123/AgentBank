const state = {token: null, userId: null, conversationId: crypto.randomUUID()};
const $ = id => document.getElementById(id);
const secretKey = /pin|otp|password|secret|token|验证码|密码/i;

function node(tag, text = "", className = "") {
  const element = document.createElement(tag);
  if (text !== "") element.textContent = String(text);
  if (className) element.className = className;
  return element;
}

function addBubble(kind, text, fragments = [], data = {}, recordId = null, displayHash = "") {
  const bubble = node("article", "", `bubble ${kind}`);
  const title = kind === "user" ? "你" : kind === "confirmation" ? "待确认" :
    kind === "mfa" ? "需要强验证" : kind === "error" ? "处理失败" : "AgentBank";
  bubble.append(node("h3", title), node("p", text || ""));

  const seen = new Set();
  const appendFact = (label, value, currency = "") => {
    if (value === undefined || value === null || secretKey.test(label)) return;
    const rendered = label.endsWith("_minor") && Number.isFinite(Number(value))
      ? `${currency === "CNY" || currency === "" ? "¥" : `${currency} `}${(Number(value) / 100).toFixed(2)}`
      : typeof value === "object" ? JSON.stringify(value) : String(value);
    if (!rendered || seen.has(`${label}:${rendered}`)) return;
    seen.add(`${label}:${rendered}`);
    const row = node("div", "", "fact");
    row.append(node("span", label || "事实"), node("strong", rendered));
    bubble.append(row);
  };
  for (const fact of fragments || []) {
    appendFact(fact.label || "事实", fact.text ?? fact.value_minor ?? fact.status, fact.currency);
  }
  const payload = data?.payload || {};
  for (const [key, value] of Object.entries(payload)) appendFact(key, value);
  for (const [key, value] of Object.entries(data || {})) {
    if (["payload", "display", "receipt_id", "display_hash", "action_hash"].includes(key)) continue;
    appendFact(key, value);
  }

  if (recordId && ["confirmation", "mfa"].includes(kind)) {
    const actions = node("div", "", "card-actions");
    const button = node("button", kind === "mfa" ? "输入验证码" : "确认执行");
    button.type = "button";
    button.onclick = () => kind === "mfa" ? verify(recordId) : confirm(recordId, displayHash, data?.receipt_id || "");
    actions.append(button);
    bubble.append(actions);
  }
  $("messages").append(bubble);
  bubble.scrollIntoView?.({behavior: "smooth"});
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {"Content-Type": "application/json", ...(state.token ? {Authorization: `Bearer ${state.token}`} : {}), ...(options.headers || {})}
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.code || data.detail?.code || "请求失败");
  return data;
}

async function start() {
  const userId = $("userSelect").value;
  state.userId = userId;
  state.conversationId = crypto.randomUUID();
  localStorage.removeItem("agentbank_token");
  const session = await api("/v1/sessions", {method: "POST", body: JSON.stringify({user_id: userId})});
  state.token = session.access_token;
  localStorage.agentbank_token = state.token;
  localStorage.agentbank_user = userId;
  $("status").textContent = `已连接模拟银行 · ${userId}`;
  $("messages").replaceChildren();
  addBubble("agent", "我可以帮你查余额、转账、分析账单、管理理财、卡片和订阅。");
  await loadLinkedRecord();
  await loadNotifications();
}

async function send(text) {
  const message = text.trim();
  if (!message) return;
  addBubble("user", message);
  try {
    const response = await api(`/v1/conversations/${state.conversationId}/messages`, {
      method: "POST", body: JSON.stringify({text: message})
    });
    addBubble(response.kind, response.message, response.fragments, response.data,
      response.record_id, response.display_hash);
  } catch (error) {
    addBubble("error", error.message);
  }
}

async function confirm(recordId, displayHash, receiptId) {
  try {
    const response = await api(`/v1/conversations/${state.conversationId}/records/${recordId}/confirm`, {
      method: "POST", body: JSON.stringify({receipt_id: receiptId, display_hash: displayHash})
    });
    addBubble(response.kind, response.message, response.fragments, response.data,
      response.record_id || recordId, response.display_hash || displayHash);
  } catch (error) {
    addBubble("error", error.message);
  }
}

async function requestOtp() {
  const dialog = $("otpDialog"), form = $("otpForm"), input = $("otpCode");
  input.value = "";
  dialog.showModal();
  input.focus();
  return new Promise(resolve => {
    const finish = value => {
      form.removeEventListener("submit", submit);
      $("otpCancel").removeEventListener("click", cancel);
      dialog.removeEventListener("cancel", cancel);
      dialog.close();
      resolve(value);
    };
    const submit = event => { event.preventDefault(); if (form.reportValidity()) finish(input.value); };
    const cancel = () => finish(null);
    form.addEventListener("submit", submit);
    $("otpCancel").addEventListener("click", cancel);
    dialog.addEventListener("cancel", cancel, {once: true});
  });
}

async function verify(recordId) {
  const otp = await requestOtp();
  if (!otp) return;
  try {
    const response = await api(`/v1/conversations/${state.conversationId}/records/${recordId}/verify`, {
      method: "POST", body: JSON.stringify({otp})
    });
    addBubble(response.kind, response.message, response.fragments, response.data, response.record_id);
  } catch (error) {
    addBubble("error", error.message);
  }
}

async function loadLinkedRecord() {
  const recordId = new URLSearchParams(location.search).get("record_id");
  if (!recordId) return;
  try {
    const record = await api(`/v1/capability-records/${recordId}`);
    addBubble(record.status === "awaiting_mfa" ? "mfa" : "confirmation",
      "已重新载入待处理操作，请核对以下详情。", [], record.data,
      record.record_id, record.data?.display_hash || "");
  } catch (error) {
    addBubble("error", `无法载入该操作：${error.message}`);
  }
}

async function loadNotifications() {
  const list = $("notificationList");
  list.replaceChildren();
  try {
    const response = await api("/v1/notifications");
    const notifications = response.notifications || [];
    if (!notifications.length) list.append(node("p", "暂无通知。", "empty-state"));
    for (const item of notifications) {
      const entry = node("article", "", "notification");
      entry.append(node("strong", item.title || item.type || "银行通知"),
        node("p", item.message || item.body || item.content || ""));
      list.append(entry);
    }
  } catch (error) {
    list.append(node("p", `通知暂时无法读取：${error.message}`, "empty-state"));
  }
}

async function changePin(event) {
  event.preventDefault();
  const form = $("pinForm"), pin = $("newPin").value;
  if (pin !== $("confirmPin").value) {
    $("pinError").textContent = "两次输入的 PIN 不一致。";
    return;
  }
  $("pinError").textContent = "";
  const submit = form.querySelector("button[type=submit]");
  submit.disabled = true;
  try {
    const response = await api("/v1/capabilities/C08", {
      method: "POST",
      body: JSON.stringify({capability_id: "C08", idempotency_key: `pin-${crypto.randomUUID()}`,
        payload: {card_id: "card_001", new_pin: pin, assurance: "mfa"}})
    });
    $("pinDialog").close();
    form.reset();
    addBubble(response.status === "awaiting_mfa" ? "mfa" : "result",
      "PIN 修改请求已提交，需按安全验证结果完成操作。", [], response.data, response.record_id);
  } catch (error) {
    $("pinError").textContent = `无法提交：${error.message}`;
  } finally {
    submit.disabled = false;
  }
}

$("composer").onsubmit = event => {
  event.preventDefault();
  const input = $("message"), message = input.value;
  input.value = "";
  send(message);
};
$("balanceBtn").onclick = () => send("帮我查一下余额");
$("userSelect").value = localStorage.agentbank_user || "user_001";
$("switchUser").onclick = () => start().catch(error => { $("status").textContent = `连接失败：${error.message}`; });
$("refreshNotifications").onclick = loadNotifications;
$("pinButton").onclick = () => { $("pinError").textContent = ""; $("pinDialog").showModal(); $("newPin").focus(); };
$("pinCancel").onclick = () => $("pinDialog").close();
$("pinForm").onsubmit = changePin;
document.querySelectorAll(".quick button").forEach(button => button.onclick = () => send(button.dataset.text));
start().catch(error => { $("status").textContent = `连接失败：${error.message}`; });
