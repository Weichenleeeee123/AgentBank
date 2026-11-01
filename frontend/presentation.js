const digits = '零壹贰叁肆伍陆柒捌玖';
export function uppercaseMoney(minor) {
  if (!Number.isSafeInteger(Number(minor))) return '';
  minor = Number(minor);
  const sign = minor < 0 ? '负' : ''; minor = Math.abs(minor);
  const section = value => {
    let out = '', zero = false;
    for (let i = 3; i >= 0; i--) {
      const n = Math.floor(value / 10 ** i) % 10;
      if (n) { if (zero && out) out += '零'; out += digits[n] + ['', '拾', '佰', '仟'][i]; zero = false; }
      else if (out) zero = true;
    }
    return out;
  };
  let whole = Math.floor(minor / 100), text = '', gap = false, index = 0;
  while (whole) {
    const part = whole % 10000;
    if (part) { text = section(part) + ['', '万', '亿', '万亿'][index] + (gap ? '零' : '') + text; gap = part < 1000; }
    else if (text) gap = true;
    whole = Math.floor(whole / 10000); index++;
  }
  const jiao = Math.floor(minor / 10) % 10, fen = minor % 10;
  return sign + (text || '零') + '元' + (jiao ? digits[jiao] + '角' : fen ? '零' : '') + (fen ? digits[fen] + '分' : !jiao ? '整' : '');
}
const statuses = {frozen:'已冻结',active:'正常',succeeded:'已完成',failed:'失败',rejected:'已拒绝',cancelled:'已取消',awaiting_confirmation:'待确认',awaiting_mfa:'待验证',mfa_required:'待验证',cooling_required:'安全冷静期',pending:'处理中',true:'是',false:'否'};
const businessValues = {confirm:'用户确认',mfa:'设备验证码强验证',none:'无需额外验证',
  monthly:'每月',weekly:'每周',daily:'每天',yearly:'每年',once:'单次',scheduled:'已安排',processing:'处理中'};
export function displayValue(value) { return statuses[String(value)] || businessValues[String(value)] || String(value); }
export function money(value, currency = 'CNY') { return `${currency === 'CNY' || !currency ? '¥' : currency + ' '}${(Number(value) / 100).toFixed(2)}`; }
export function resultKind(status) { return ['awaiting_mfa','mfa_required'].includes(status) ? 'mfa' : status === 'awaiting_confirmation' ? 'confirmation' : ['failed','rejected','cancelled'].includes(status) ? 'error' : status === 'succeeded' ? 'result' : 'pending'; }
