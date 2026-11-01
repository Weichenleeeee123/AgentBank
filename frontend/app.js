import {uppercaseMoney, displayValue, money, resultKind} from './presentation.js';
const state = {token: null, userId: null, conversationId: crypto.randomUUID(), cards: [], generation: 0};
const authorizationControls = new Map();
const $ = id => document.getElementById(id);
const secretKey = /pin|otp|password|secret|token|验证码|密码/i;
const internalFields = new Set(['plan_id','capability_id','ledger_entry_ids','query_hash','operation_template',
  'mandate_id','receipt_id','display_hash','action_hash','idempotency_key','trace_id','pre_action_review']);

function node(tag, text = "", className = "") {
  const element = document.createElement(tag);
  if (text !== "") element.textContent = String(text);
  if (className) element.className = className;
  return element;
}

function addBubble(kind, text, fragments = [], data = {}, recordId = null, displayHash = "") {
  for (const invalidated of data?.invalidated_record_ids || []) {
    for (const control of authorizationControls.get(invalidated) || []) {
      control.disabled = true;
      control.onclick = null;
      control.textContent = '已作废';
    }
    authorizationControls.delete(invalidated);
  }
  const bubble = node("article", "", `bubble ${kind}`);
  const title = kind === "user" ? "你" : ["confirmation", "plan_confirmation"].includes(kind) ? "待确认" :
    kind === "mfa" ? "需要强验证" : kind === "error" ? "处理失败" : kind === "review" ? "行动前审查" : "AgentBank";
  bubble.append(node("h3", title), node("p", text || ""));

  const seen = new Set();
  const appendFact = (label, value, currency = "", minorUnits = label.endsWith("_minor")) => {
    if (value === undefined || value === null || secretKey.test(label)) return;
    if (typeof value === "object") {
      for (const [key, child] of Object.entries(value)) {
        if (internalFields.has(key) || key === 'payload' || secretKey.test(key)) continue;
        appendFact(`${label} · ${factLabels[key] || key}`, child, currency, key.endsWith("_minor"));
      }
      return;
    }
    const rendered = minorUnits && Number.isFinite(Number(value))
      ? `${money(value, currency)}${!currency || currency === "CNY" ? `（${uppercaseMoney(Number(value))}）` : ""}`
      : displayValue(value);
    if (!rendered || seen.has(`${label}:${rendered}`)) return;
    seen.add(`${label}:${rendered}`);
    const row = node("div", "", "fact");
    row.append(node("span", label || "事实"), node("strong", rendered));
    bubble.append(row);
  };
  if (data?.pre_action_review) {
    const review = data.pre_action_review;
    const panel = node("section", "", `pre-action-review ${review.decision === "block" ? "blocked" : "cleared"}`);
    panel.append(node("h4", "行动前审查"));
    const subject = node("p", "", "review-subject");
    subject.append(node("strong", review.subject?.name || review.subject?.query || "未确认主体"));
    subject.append(node("span", review.decision === "pass" ? "可继续授权" : "暂不办理"));
    panel.append(subject);
    if (review.registration) {
      const registration = node("div", "", "review-line");
      registration.append(node("strong", "登记信息"), node("span", review.registration.detail || "未覆盖"));
      panel.append(registration);
    }
    const qualification = node("div", "", "review-line");
    qualification.append(node("strong", "资质核验"), node("span", review.qualification?.detail || "未覆盖"));
    panel.append(qualification);
    const signals = node("div", "", "review-signals");
    for (const signal of review.signals || []) {
      const item = node("div", "", "review-signal");
      item.append(node("strong", signal.label), node("span", signal.value));
      signals.append(item);
    }
    panel.append(signals);
    const addList = (title, items, className) => {
      if (!items?.length) return;
      const group = node("div", "", `review-list ${className}`);
      group.append(node("strong", title));
      const list = node("ul");
      for (const value of items) list.append(node("li", value));
      group.append(list);
      panel.append(group);
    };
    addList("待核实信息", review.gaps, "review-gaps");
    addList("建议确认", review.questions, "review-questions");
    addList("本地样本来源", review.evidence_sources, "review-sources");
    if (review.product) {
      const product = node("div", "", "review-product");
      product.append(node("strong", review.product.name || "产品信息"));
      product.append(node("span", `${review.product.issuer || "发行主体未确认"} · 风险 ${review.product.risk_level || "未知"} · 期限 ${review.product.term_days ?? "未知"} 天 · 赎回 ${review.product.redemption_days ?? "未知"} 天`));
      panel.append(product);
    }
    panel.append(node("p", review.notice || "", "review-notice"));
    bubble.append(panel);
  }
  for (const fact of fragments || []) {
    appendFact(fact.label || "事实", fact.status ?? fact.text ?? fact.value_minor, fact.currency, fact.status == null && fact.text == null && fact.value_minor != null);
  }
  if (Array.isArray(data?.display)) {
    for (const item of data.display) {
      const inFen = /[（(]分[）)]$/.test(item.label || "") && /^-?[0-9]+$/.test(String(item.value));
      appendFact(inFen ? item.label.replace(/[（(]分[）)]$/, "") : item.label || "详情", item.value, "", inFen);
      const yuan = typeof item.value === "string" ? item.value.match(/^[¥￥]([0-9]+(?:\.[0-9]{1,2})?)$/) : null;
      if (yuan) appendFact(`${item.label || "金额"}（大写）`, uppercaseMoney(Math.round(Number(yuan[1]) * 100)));
    }
  }
  for (const [key, value] of Object.entries(data || {})) {
    if (internalFields.has(key) || ["payload", "display", "transactions", "direct_form", "rows", "questionnaire", "invalidated_record_ids", "answers", "questionnaire_version"].includes(key)) continue;
    appendFact(factLabels[key] || key, value, "", key.endsWith("_minor"));
  }
  if (Array.isArray(data?.transactions)) appendTransactions(bubble, data.transactions);
  if (data?.questionnaire) bubble.append(questionnaireForm(data.questionnaire));

  if (recordId && ["confirmation", "plan_confirmation", "mfa"].includes(kind)) {
    const actions = node("div", "", "card-actions");
    const button = node("button", kind === "mfa" ? "输入验证码" : "确认执行");
    button.type = "button";
    button.onclick = () => data?.direct_form
      ? kind === "mfa" ? verifyDirect(recordId) : confirmDirect(recordId, displayHash, data?.receipt_id || "")
      : kind === "mfa" ? verify(recordId) : confirm(recordId, displayHash, data?.receipt_id || "");
    actions.append(button);
    const controls = authorizationControls.get(recordId) || [];
    controls.push(button);
    authorizationControls.set(recordId, controls);
    if (data?.direct_form) {
      const cancel = node("button", "取消操作", "ghost");
      cancel.onclick = async () => {
        try { const response = await api(`/v1/capability-records/${recordId}/invalidate`, {method: "POST"});
          actions.replaceChildren(node("span", "已取消。")); showDirectResult(response, "操作已取消。");
        } catch (error) { addBubble("error", error.message); }
      };
      actions.append(cancel);
    }
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
  bubble.scrollIntoView?.({behavior: "smooth", block: "nearest"});
}

function questionnaireForm(questionnaire) {
  const generation = state.generation;
  const form = node('form', '', 'risk-questionnaire');
  form.append(node('h4', questionnaire.title), node('p', questionnaire.description || '请逐题选择。'));
  const selects = [];
  for (const question of questionnaire.questions) {
    const label = node('label', question.text);
    const select = node('select');
    select.required = true;
    const placeholder = node('option', '请选择');
    placeholder.value = '';
    select.append(placeholder);
    for (const answer of question.options) {
      const option = node('option', answer.text);
      option.value = String(answer.value);
      select.append(option);
    }
    select.value = '';
    selects.push(select);
    label.append(select);
    form.append(label);
  }
  const error = node('p', '', 'form-error');
  const submit = node('button', '提交答案并核对');
  submit.type = 'submit';
  form.append(error, submit);
  form.onsubmit = async event => {
    event.preventDefault();
    if (generation !== state.generation) return;
    if (selects.length !== 10 || selects.some(select => !/^[0-5]$/.test(select.value))) {
      error.textContent = '请完成全部10题后提交。';
      return;
    }
    submit.disabled = true;
    try {
      const response = await api('/v1/risk/submit', {method: 'POST', body: JSON.stringify({
        questionnaire_version: questionnaire.version, answers: selects.map(select => Number(select.value))})});
      if (generation !== state.generation) return;
      for (const select of selects) select.disabled = true;
      showDirectResult(response, '风险测评已保存。');
    } catch (failure) {
      if (generation !== state.generation) return;
      error.textContent = failure.message;
      submit.disabled = false;
    }
  };
  return form;
}

const transactionLabels = {posted_at: "日期", merchant: "商户", category: "类别", direction: "类型", amount_minor: "金额"};
const factLabels = {level: "风险等级", score: "测评分数", expires_on: "有效期至", plan_type: "计划类型", revision: "修订版本", available_minor: "可用余额", ledger_balance_minor: "账面余额", currency: "币种",
  capability_id: "服务", account_id: "账户", record_id: "记录", status: "状态", masked_name: "收款人",
  phone_tail: "手机号尾号", is_new: "新收款人", tier_display: "风险级别", assurance: "验证方式",
  card: "卡片", cards: "卡片", limit_minor: "限额", transfer_result: "转账结果", amount_minor: "金额", total_minor: "总额",
  operation_id: "操作编号", row_count: "统计分组数", next_due: "首次执行日期", max_uses: "剩余执行次数",
  period: "执行周期", delivery_date: "送达日期", dispatch_on: "下单日期", expected_settlement_at: "预计到账时间"};

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
  const generation = state.generation;
  if (navigator.onLine === false) throw new Error("当前离线，仅可查看页面外壳；联网后可继续银行操作。");
  const response = await fetch(path, {
    ...options,
    cache: "no-store",
    headers: {"Content-Type": "application/json", ...(state.token ? {Authorization: `Bearer ${state.token}`} : {}), ...(options.headers || {})}
  });
  const data = await response.json();
  if (generation !== state.generation) throw new Error("账户已切换，旧请求结果已忽略。");
  if (!response.ok) throw new Error(data.code || data.detail?.code || "请求失败");
  return data;
}

async function start() {
  state.generation++;
  const userId = $("userSelect").value;
  state.token = null; state.cards = []; $("pinButton").disabled = true;
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
  await loadCards();
  await loadLinkedRecord();
  await loadNotifications();
}

async function send(text, messageId = crypto.randomUUID(), retry = false) {
  const generation = state.generation;
  const message = text.trim();
  if (!message) return;
  if (!retry) addBubble("user", message);
  try {
    const response = await api(`/v1/conversations/${state.conversationId}/messages`, {
      method: "POST", body: JSON.stringify({text: message, message_id: messageId})
    });
    if (generation !== state.generation) return;
    addBubble(response.kind, response.message, response.fragments, response.data,
      response.record_id, response.display_hash);
  } catch (error) {
    if (generation !== state.generation) return;
    addBubble("error", error.message);
    const retryButton = node("button", "重试这条消息");
    retryButton.onclick = () => { retryButton.disabled = true; send(message, messageId, true); };
    $("messages").lastElementChild?.append(retryButton);
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
  const generation = state.generation;
  const otp = await requestOtp();
  if (!otp || generation !== state.generation) return;
  try {
    const response = await api(`/v1/conversations/${state.conversationId}/records/${recordId}/verify`, {
      method: "POST", body: JSON.stringify({otp})
    });
    addBubble(response.kind, response.message, response.fragments, response.data, response.record_id);
  } catch (error) {
    if (generation !== state.generation) return;
    addBubble("error", error.message);
  }
}

function showDirectResult(response, label) {
  const status = response.status;
  const data = {...(response.data || {}), ...(response.pre_action_review ? {pre_action_review: response.pre_action_review} : {}), capability_id: response.capability_id || response.data?.capability_id,
    direct_form: true};
  const isTransfer = ["T01", "T02", "T03"].includes(data.capability_id);
  const succeeded = status === "succeeded" && (!isTransfer || data.transfer_result);
  const kind = status === "succeeded" && !succeeded ? "pending" : resultKind(status);
  const message = status === "awaiting_confirmation" ? "请核对以下操作详情，再确认执行。" :
    ["awaiting_mfa", "mfa_required"].includes(status) ? "操作需要设备验证，请在验证码对话框中输入。" :
    ["cooling_required", "cooling"].includes(status) ? "转账仍在安全冷静期内，当前尚未完成。" :
    succeeded ? label : "操作尚未完成，请查看状态或稍后重试。";
  addBubble(kind, message, response.fragments || [], data, response.record_id,
    response.display_hash || response.data?.display_hash || "");
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
    showDirectResult(response, "操作已完成。");
  } catch (error) {
    addBubble("error", error.message);
  }
}

async function verifyDirect(recordId) {
  const generation = state.generation;
  const otp = await requestOtp();
  if (!otp || generation !== state.generation) return;
  try {
    const response = await api(`/v1/capability-records/${recordId}/verify`, {
      method: "POST", body: JSON.stringify({otp})
    });
    showDirectResult(response, "操作验证已完成。");
  } catch (error) {
    if (generation !== state.generation) return;
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
    showDirectResult(response, "操作已完成。");
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
    const newPayee = node("option", "使用新收款人"); newPayee.value = "";
    select.replaceChildren(newPayee);
    for (const payee of response.payees || []) {
      const option = node("option", `${payee.masked_name || name}${payee.phone_tail ? ` · 尾号 ${payee.phone_tail}` : ""}${payee.is_new ? " · 新收款人" : ""}`);
      option.value = payee.id;
      select.append(option);
    }
  } catch (error) {
    if (lookup === payeeLookup) select.replaceChildren(node("option", `无法查找：${error.message}`));
  }
}

async function loadLinkedRecord() {
  const params = new URLSearchParams(location.search);
  const planId = params.get("plan_id");
  if (planId) {
    try {
      const plan = await api(`/v1/plans/${encodeURIComponent(planId)}`);
      const data = {status: plan.status, display: plan.display || [], receipt_id: plan.receipt_id || "",
        assurance: plan.assurance, display_hash: plan.display_hash || ""};
      const kind = plan.status === "awaiting_mfa" ? "mfa" :
        plan.status === "awaiting_confirmation" ? "plan_confirmation" : "result";
      addBubble(kind, kind === "mfa" ? "计划需要设备验证码验证。" :
        kind === "plan_confirmation" ? "请核对计划详情，再确认执行。" : "计划当前状态如下。",
        [], data, planId, plan.display_hash || "");
    } catch (error) { addBubble("error", `无法载入该计划：${error.message}`); }
    const link = node("a", "查看并管理此计划", "ghost-link");
    link.href = `/app/plan.html?plan_id=${encodeURIComponent(planId)}`;
    $("messages").append(link);
    return;
  }
  const recordId = params.get("record_id");
  if (!recordId) return;
  try {
    const record = await api(`/v1/capability-records/${recordId}`);
    showDirectResult(record, "该操作已完成。");
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
  if (!state.cards.some(card => card.id === $("pinCard").value)) { $("pinError").textContent = "请选择当前账户的卡片。"; return; }
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
        payload: {card_id: $("pinCard").value, new_pin: pin, assurance: "mfa"}})
    });
    $("pinDialog").close();
    form.reset();
    showDirectResult(response, "PIN 已修改。");
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

async function loadCards() {
  const select = $("pinCard");
  select.replaceChildren(); state.cards = [];
  try {
    const response = await api("/v1/cards"); state.cards = response.cards || [];
    for (const card of state.cards) { const option = node("option", `${card.id} · ${displayValue(card.status)}`); option.value = card.id; select.append(option); }
    $("pinButton").disabled = !state.cards.length;
  } catch (error) { $("pinButton").disabled = true; addBubble("error", `卡片暂不可用：${error.message}`); }
}
function connectionState() {
  const offline = navigator.onLine === false;
  $("offlineNotice").hidden = !offline;
  document.querySelectorAll('button, .composer input').forEach(control => {
    if (offline) { control.dataset.offlineDisabled = String(control.disabled); control.disabled = true; }
    else if (control.dataset.offlineDisabled != null) { control.disabled = control.dataset.offlineDisabled === "true"; delete control.dataset.offlineDisabled; }
  });
}
window.addEventListener("online", connectionState);
window.addEventListener("offline", connectionState);
connectionState();
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/app/sw.js", {scope:"/app/"}).catch(() => {});
