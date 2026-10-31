const form = document.getElementById("run");
const result = document.getElementById("result");
form.onsubmit = async event => {
  event.preventDefault();
  const button = form.querySelector("button");
  button.disabled = true;
  result.textContent = "正在运行独立用例，请稍候…";
  try {
    const response = await fetch("/admin/v1/self-test", {method:"POST",
      headers:{"X-Admin-Token":document.getElementById("token").value}});
    const report = await response.json();
    if (!response.ok) throw new Error(report.code || report.detail?.code || "自测失败");
    const summary = `门禁 ${report.failed === 0 ? "通过" : "未通过"} · ${report.case_passed}/${report.case_total} 用例\n` +
      `账本已实际检查：${report.ledger_measured ? "是" : "否"}；未授权分录：${report.unauthorized_ledger_writes ?? "未测量"}\n` +
      `审计完整：${report.audit_verified ? "是" : "否"}；误拦：${report.false_positive.failed}/${report.false_positive.total}\n`;
    result.textContent = summary + "\n" + JSON.stringify(report, null, 2);
    const download = document.createElement("a");
    const blobUrl = URL.createObjectURL(new Blob([JSON.stringify(report,null,2)], {type:"application/json"}));
    download.href = blobUrl;
    download.download = `${report.demo_run_id}.json`;
    download.textContent = "下载本次完整证据";
    result.before(download);
  } catch (error) { result.textContent = error.message; }
  finally { button.disabled = false; }
};
