const token = document.getElementById("staffToken");
const status = document.getElementById("status");
const cases = document.getElementById("cases");
async function request(path, options = {}) {
  const response = await fetch(path, {...options, headers: {"X-Staff-Token": token.value,
    "Content-Type": "application/json"}});
  const data = await response.json();
  if (!response.ok) throw new Error(data.code || data.detail?.code || "请求失败");
  return data;
}
async function load() {
  const data = await request("/staff/v1/handoffs");
  cases.replaceChildren();
  for (const item of data.cases) {
    const article = document.createElement("article"); article.className = "bubble";
    const heading = document.createElement("h2"); heading.textContent = item.id;
    const context = document.createElement("pre"); context.textContent = JSON.stringify(item, null, 2);
    const form = document.createElement("form");
    const note = document.createElement("textarea"); note.required = true;
    note.placeholder = "核实结果与交还用户的下一步，不要填写验证码或密码";
    const submit = document.createElement("button"); submit.textContent = "记录处理结果";
    form.append(note, submit); article.append(heading, context, form); cases.append(article);
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
