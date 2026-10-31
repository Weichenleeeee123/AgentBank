const token = document.getElementById("staffToken");
const status = document.getElementById("status");
const cases = document.getElementById("cases");
const metrics = document.getElementById("metrics");
async function request(path, options = {}) {
  const response = await fetch(path, {...options, headers: {"X-Staff-Token": token.value,
    "Content-Type": "application/json"}});
  const data = await response.json();
  if (!response.ok) throw new Error(data.code || data.detail?.code || "请求失败");
  return data;
}
async function load() {
  const [data, observed] = await Promise.all([
    request("/staff/v1/handoffs"), request("/staff/v1/metrics")
  ]);
  metrics.replaceChildren();
  const rows = [
    ["保留请求 / 服务端错误", `${observed.requests.count} / ${observed.requests.server_errors}`],
    ["模型调用失败率", observed.model.failure_rate === null ? "未测量" : `${(observed.model.failure_rate * 100).toFixed(1)}%`],
    ["当前用户熔断", observed.user_breakers.active],
    ["工具熔断", observed.tool_breakers.status === "not_measured" ? "未测量" : JSON.stringify(observed.tool_breakers)],
    ["本小时模型调用 / 上限", `${observed.llm_budget.calls} / ${observed.llm_budget.limit}`]
  ];
  for (const [label, value] of rows) {
    const p = document.createElement("p"); p.textContent = `${label}：${value}`; metrics.append(p);
  }
  cases.replaceChildren();
  for (const item of data.cases) {
    const article = document.createElement("article"); article.className = "bubble";
    const heading = document.createElement("h2"); heading.textContent = item.id;
    const context = document.createElement("pre"); context.textContent = JSON.stringify(item, null, 2);
    const form = document.createElement("form");
    const note = document.createElement("textarea"); note.required = true;
    note.maxLength = 2000; note.setAttribute("aria-label", "处理记录");
    note.placeholder = "核实结果与交还用户的下一步，不要填写验证码或密码";
    const userMessage = document.createElement("textarea"); userMessage.maxLength = 2000;
    userMessage.setAttribute("aria-label", "交还用户的建议");
    userMessage.placeholder = "交还用户的建议（仅建议；用户须重新发起并授权）";
    let savedContext = {};
    try { savedContext = JSON.parse(item.context_json || "{}"); } catch { /* Server validates context. */ }
    note.value = savedContext.staff_draft?.note || "";
    userMessage.value = savedContext.staff_draft?.user_message || "";
    const draft = document.createElement("button"); draft.type = "button";
    draft.textContent = "保存建议草稿";
    const retry = document.createElement("button"); retry.type = "button";
    retry.textContent = savedContext.retry_request ? "已交还用户，等待重新发起" : "交还用户重新发起";
    retry.disabled = !savedContext.staff_draft || Boolean(savedContext.retry_request);
    draft.disabled = Boolean(savedContext.retry_request);
    const submit = document.createElement("button"); submit.textContent = "记录处理结果";
    form.append(note, userMessage, draft, retry, submit); article.append(heading, context, form); cases.append(article);
    draft.onclick = async () => {
      if (!form.reportValidity()) return;
      draft.disabled = true;
      try {
        await request(`/staff/v1/handoffs/${encodeURIComponent(item.id)}/draft`, {
          method: "POST", body: JSON.stringify({note: note.value, user_message: userMessage.value})});
        await load();
      } catch (error) { status.textContent = error.message; draft.disabled = false; }
    };
    retry.onclick = async () => {
      if (!form.reportValidity()) return;
      retry.disabled = true;
      try {
        await request(`/staff/v1/handoffs/${encodeURIComponent(item.id)}/retry-request`, {
          method: "POST", body: JSON.stringify({note: note.value})});
        await load();
      } catch (error) { status.textContent = error.message; retry.disabled = false; }
    };
    form.onsubmit = async event => {
      event.preventDefault(); submit.disabled = true;
      try { await request(`/staff/v1/handoffs/${encodeURIComponent(item.id)}/resolve`, {
        method: "POST", body: JSON.stringify({note: note.value})}); await load(); }
      catch (error) { status.textContent = error.message; submit.disabled = false; }
    };
  }
  status.textContent = `${data.cases.length} 个待处理工单`;
}
document.getElementById("login").onsubmit = async event => {
  event.preventDefault();
  try { await load(); } catch (error) { status.textContent = error.message; }
};
