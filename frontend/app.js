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
  const title = kind === "user" ? "你" : ["confirmation", "plan_confirmation"].includes(kind) ? "待确认" :
    kind === "mfa" ? "需要强验证" : kind === "error" ? "处理失败" : "AgentBank";
  bubble.append(node("h3", title), node("p", text || ""));

  const seen = new Set();
  const appendFact = (label, value, currency = "", minorUnits = label.endsWith("_minor")) => {
    if (value === undefined || value === null || secretKey.test(label)) return;
    const rendered = minorUnits && Number.isFinite(Number(value))
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
  if (Array.isArray(data?.display)) {
    for (const item of data.display) appendFact(item.label || "详情", item.value);
  }
  for (const [key, value] of Object.entries(data || {})) {
    if (["payload", "display", "transactions", "receipt_id", "display_hash", "action_hash", "direct_form"].includes(key)) continue;
    appendFact(factLabels[key] || key, value, "", key.endsWith("_minor"));
  }
  if (Array.isArray(data?.transactions)) appendTransactions(bubble, data.transactions);

  if (recordId && ["confirmation", "plan_confirmation", "mfa"].includes(kind)) {
    const actions = node("div", "", "card-actions");
    const button = node("button", kind === "mfa" ? "输入验证码" : "确认执行");
    button.type = "button";
    button.onclick = () => data?.direct_form
      ? kind === "mfa" ? verifyDirect(recordId) : confirmDirect(recordId, displayHash, data?.receipt_id || "")
      : kind === "mfa" ? verify(recordId) : confirm(recordId, displayHash, data?.receipt_id || "");
    actions.append(button);
    if (data?.direct_form && kind === "confirmation" && data?.capability_id === "T01") {
      const revise = node("button", "修改转账");
      revise.type = "button";
      revise.className = "ghost";
      revise.onclick = () => reviseDirect(recordId, bubble);
      actions.append(revise);
    }
    bubble.append(actions);
  }
  const transferState = data?.transfer_result;
  if (transferState?.status === "cooling_required") {
    const actions = node("div", "", "card-actions");
    const release = node("button", "查看冷静期并重新验证");
    release.onclick = async () => {
      try {
        const cooling = await api(`/v1/cooling/${encodeURIComponent(transferState.operation_id)}`);
        if (cooling.remaining_seconds > 0) {
          addBubble("risk", `冷静期尚余 ${Math.ceil(cooling.remaining_seconds / 60)} 分钟，资金尚未转出。`);
          return;
        }
        await api(`/v1/cooling/${encodeURIComponent(cooling.cooling_id)}/release`, {
          method: "POST", body: JSON.stringify({reconfirmed: true, action_hash: cooling.action_hash,
            plan_revision: cooling.plan_revision})});
        if (data?.direct_form) await verifyDirect(recordId); else await verify(recordId);
      } catch (error) { addBubble("error", error.message); }
    };
    const cancel = node("button", "取消并释放资金", "ghost");
    cancel.onclick = async () => {
      try {
        await api(`/v1/cooling/${encodeURIComponent(transferState.operation_id)}/cancel`, {method: "POST"});
        actions.replaceChildren(node("span", "已取消，资金占用已释放。"));
      } catch (error) { addBubble("error", error.message); }
    };
    actions.append(release, cancel);
    bubble.append(actions);
  }
  $("messages").append(bubble);
  bubble.scrollIntoView?.({behavior: "smooth"});
}

const transactionLabels = {posted_at: "日期", merchant: "商户", category: "类别", direction: "类型", amount_minor: "金额"};
const factLabels = {available_minor: "可用余额", ledger_balance_minor: "账面余额", currency: "币种",
  capability_id: "服务", account_id: "账户", record_id: "记录", status: "状态", masked_name: "收款人",
  phone_tail: "手机号尾号", is_new: "新收款人", tier_display: "风险级别", assurance: "验证方式",
  operation_id: "操作编号"};

function appendTransactions(parent, transactions) {
  const list = node("div", "", "transaction-list");
  for (const item of transactions) {
    const row = node("article", "", "transaction-row");
    row.append(node("strong", item.merchant || "交易"), node("span", item.posted_at || ""));
    const details = node("div", "", "transaction-details");
    for (const key of ["category", "direction", "amount_minor"]) {
      const value = item[key];
      if (value === undefined || value === null) continue;
      const rendered = key === "amount_minor" ? `¥${(Number(value) / 100).toFixed(2)}` : String(value);
      details.append(node("span", `${transactionLabels[key]}：${rendered}`));
    }
    row.append(details);
    list.append(row);
  }
  if (!transactions.length) list.append(node("p", "没有符合条件的交易。", "empty-state"));
  parent.append(list);
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

function showDirectResult(response, label) {
  const status = response.status;
  const data = {...(response.data || {}), capability_id: response.capability_id || response.data?.capability_id,
    direct_form: true};
  const isTransfer = ["T01", "T02", "T03"].includes(data.capability_id);
  const succeeded = status === "succeeded" && (!isTransfer || data.transfer_result);
  const kind = ["awaiting_mfa", "mfa_required"].includes(status) ? "mfa" :
    status === "awaiting_confirmation" ? "confirmation" : ["failed", "rejected"].includes(status) ? "error" :
    succeeded ? "result" : "pending";
  const message = status === "awaiting_confirmation" ? "请核对以下收款人和金额，再确认执行。" :
    ["awaiting_mfa", "mfa_required"].includes(status) ? "操作需要设备验证，请在验证码对话框中输入。" :
    ["cooling_required", "cooling"].includes(status) ? "转账仍在安全冷静期内，当前尚未完成。" :
    succeeded ? label : "操作尚未完成，请查看状态或稍后重试。";
  addBubble(kind, message, response.fragments || [], data, response.record_id,
    response.data?.display_hash || "");
}

async function submitDirectForm(path, payload, statusId, label) {
  const status = $(statusId);
  const submit = status.parentElement.querySelector("button[type=submit]");
  status.textContent = "正在提交…";
  submit.disabled = true;
  try {
    const response = await api(`/v1/forms/${path}`, {method: "POST", body: JSON.stringify({
      ...payload, idempotency_key: `pwa-${crypto.randomUUID()}`
    })});
    status.textContent = "";
    showDirectResult(response, label);
  } catch (error) {
    status.textContent = `提交失败：${error.message}`;
  } finally {
    submit.disabled = false;
  }
}

async function confirmDirect(recordId, displayHash, receiptId) {
  try {
    const response = await api(`/v1/capability-records/${recordId}/confirm`, {
      method: "POST", body: JSON.stringify({receipt_id: receiptId, display_hash: displayHash})
    });
    showDirectResult(response, "转账已完成。");
  } catch (error) {
    addBubble("error", error.message);
  }
}

async function verifyDirect(recordId) {
  const otp = await requestOtp();
  if (!otp) return;
  try {
    const response = await api(`/v1/capability-records/${recordId}/verify`, {
      method: "POST", body: JSON.stringify({otp})
    });
    showDirectResult(response, "转账验证已完成。");
  } catch (error) {
    addBubble("error", error.message);
  }
}

async function reviseDirect(recordId, oldBubble) {
  const mode = document.querySelector('input[name="transferMode"]:checked').value;
  const amount = Number($("transferAmount").value);
  if (!Number.isFinite(amount) || amount <= 0) {
    $("transferStatus").textContent = "请输入大于 0 的金额。";
    return;
  }
  const payload = {amount_minor: Math.round(amount * 100)};
  if (mode === "name" && $("recipientCandidate").value) payload.recipient_id = $("recipientCandidate").value;
  else payload[mode === "name" ? "recipient_name" : mode === "phone" ? "phone" : "alias"] =
    mode === "name" ? $("recipientName").value.trim() : mode === "phone" ? $("recipientPhone").value.trim() : $("recipientAlias").value.trim();
  if ($("transferMemo").value.trim()) payload.memo = $("transferMemo").value.trim();
  try {
    const response = await api(`/v1/capability-records/${recordId}/revise`, {method: "POST", body: JSON.stringify({
      payload, idempotency_key: `revise-${crypto.randomUUID()}`
    })});
    oldBubble.querySelector(".card-actions")?.remove();
    showDirectResult(response, "转账已完成。");
  } catch (error) {
    addBubble("error", `无法修改转账：${error.message}`);
  }
}

function updateFormModes() {
  const queryMode = document.querySelector('input[name="queryMode"]:checked').value;
  $("transactionFilters").hidden = queryMode !== "transactions";
  $("queryForm").querySelector("button[type=submit]").textContent = queryMode === "balance" ? "查余额" : "查交易";
  const transferMode = document.querySelector('input[name="transferMode"]:checked').value;
  for (const [mode, id, input] of [["name", "recipientNameField", "recipientName"],
    ["phone", "recipientPhoneField", "recipientPhone"], ["alias", "recipientAliasField", "recipientAlias"]]) {
    const visible = mode === transferMode;
    $(id).hidden = !visible;
    $(input).required = visible;
  }
}

async function submitQuery(event) {
  event.preventDefault();
  const mode = document.querySelector('input[name="queryMode"]:checked').value;
  if (mode === "balance") return submitDirectForm("balance", {}, "queryStatus", "余额查询完成。");
  const startDate = $("startDate").value, endDate = $("endDate").value;
  if (startDate && endDate && startDate > endDate) {
    $("queryStatus").textContent = "开始日期不能晚于结束日期。";
    return;
  }
  const payload = {limit: Number($("transactionLimit").value)};
  if (startDate) payload.start_date = startDate;
  if (endDate) payload.end_date = endDate;
  if ($("categoryFilter").value.trim()) payload.category = $("categoryFilter").value.trim();
  return submitDirectForm("transactions", payload, "queryStatus", "交易查询完成。");
}

async function submitTransfer(event) {
  event.preventDefault();
  const mode = document.querySelector('input[name="transferMode"]:checked').value;
  const recipient = mode === "name" ? $("recipientName").value.trim() :
    mode === "phone" ? $("recipientPhone").value.trim() : $("recipientAlias").value.trim();
  if (!recipient) return;
  const amount = Number($("transferAmount").value);
  if (!Number.isFinite(amount) || amount <= 0) {
    $("transferStatus").textContent = "请输入大于 0 的金额。";
    return;
  }
  const payload = {amount_minor: Math.round(amount * 100)};
  const field = mode === "name" ? "recipient_name" : mode === "phone" ? "phone" : "alias";
  const candidateId = mode === "name" ? $("recipientCandidate").value : "";
  if (candidateId) payload.recipient_id = candidateId;
  else payload[field] = recipient;
  if ($("transferMemo").value.trim()) payload.memo = $("transferMemo").value.trim();
  return submitDirectForm(`transfer-${mode}`, payload, "transferStatus", "转账已处理。");
}

let payeeLookup = 0;
async function lookupPayees() {
  const name = $("recipientName").value.trim(), select = $("recipientCandidate");
  const field = $("recipientCandidateField");
  const lookup = ++payeeLookup;
  select.replaceChildren(node("option", name ? "正在查找…" : "请选择收款人"));
  select.value = "";
  field.hidden = !name;
  if (!name) return;
  try {
    const response = await api(`/v1/payees?name=${encodeURIComponent(name)}`);
    if (lookup !== payeeLookup) return;
    select.replaceChildren(node("option", "使用新收款人"));
    for (const payee of response.payees || []) {
      const option = node("option", `${payee.masked_name || name} · 尾号 ${payee.phone_tail}${payee.is_new ? " · 新收款人" : ""}`);
      option.value = payee.id;
      select.append(option);
    }
  } catch (error) {
    if (lookup === payeeLookup) select.replaceChildren(node("option", `无法查找：${error.message}`));
  }
}

async function loadLinkedRecord() {
  const recordId = new URLSearchParams(location.search).get("record_id");
  if (!recordId) return;
  try {
    const record = await api(`/v1/capability-records/${recordId}`);
    const data = {...record.data, direct_form: ["T01", "T02", "T03"].includes(record.data?.capability_id)};
    addBubble(record.status === "awaiting_mfa" ? "mfa" : "confirmation",
      "已重新载入待处理操作，请核对以下详情。", [], data,
      record.record_id, data?.display_hash || "");
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
$("queryForm").onsubmit = submitQuery;
$("transferForm").onsubmit = submitTransfer;
$("recipientName").addEventListener("input", lookupPayees);
document.querySelectorAll('input[name="queryMode"],input[name="transferMode"]').forEach(input => input.addEventListener("change", updateFormModes));
updateFormModes();
document.querySelectorAll(".quick button").forEach(button => button.onclick = () => send(button.dataset.text));
start().catch(error => { $("status").textContent = `连接失败：${error.message}`; });
