import {displayValue, money, uppercaseMoney} from './presentation.js';
const $ = id => document.getElementById(id);
const state = {token:null, bound:null, conversation:crypto.randomUUID(), generation:0, busy:false};
const el = (tag,text='') => {const n=document.createElement(tag);n.textContent=String(text);return n;};
const secret = /pin|otp|password|secret|token|nonce|receipt|hash|密码|验证码/i;
function status(text) { $('imStatus').textContent=text; }
function controls() {
  $('send').disabled = !state.token || !state.bound || !navigator.onLine || state.busy;
  $('bind').disabled = !state.token || !localStorage.agentbank_token || !navigator.onLine || state.busy;
  $('bind').textContent = `绑定 App 当前账户 ${localStorage.agentbank_user || '（请先登录 App）'}`;
  $('binding').textContent = state.token ? `IM ${$('identity').value} · ${state.bound ? '已绑定 ' + state.bound : '未绑定银行账户'}` : '尚未连接';
}
async function api(path,body,bank=false) {
  const generation=state.generation, token=state.token, bankToken=localStorage.agentbank_token;
  const stale=()=>generation!==state.generation || token!==state.token || (bank && bankToken!==localStorage.agentbank_token);
  const staleError=()=>Object.assign(new Error('身份或对话已更换，旧请求结果已忽略。'),{name:'StaleSessionError'});
  if (!navigator.onLine) throw new Error('当前离线，IM 操作需联网，消息不作离线缓存。');
  try {
    const response=await fetch('/v1/im/webim/'+path,{method:body ? 'POST':'GET',cache:'no-store',headers:{'Content-Type':'application/json',...(bank ? {Authorization:`Bearer ${bankToken || ''}`} : token ? {'X-IM-Token':token}:{})},...(body ? {body:JSON.stringify(body)}:{})});
    const data=await response.json();
    if(stale()) throw staleError();
    if(!response.ok) {
      const code=data.code || data.detail?.code || '操作未完成';
      const explanation=data.explanation || data.detail?.explanation || {};
      throw Object.assign(new Error([explanation.message || code, explanation.next_step].filter(Boolean).join(' ')),{code});
    }
    return data;
  } catch(error) { if(stale()) throw staleError(); throw error; }
}
function bubble(kind,text) {const card=el('article');card.className=`bubble ${kind}`;card.append(el('h3',kind==='user'?'你':'AgentBank IM'),el('p',text));$('messages').append(card);return card;}
function render(response, group) {
  const card=bubble(response.kind,response.message || '请求已处理');
  // Group responses are server-redacted. Do not render any structured facts in group mode.
  if (!group) {
    const fact=(label,value,minor=false,currency='CNY')=>{
      if(value==null || secret.test(label)) return;
      if(typeof value==='object') {for(const [key,child] of Object.entries(value)) if(!secret.test(key) && key!=='payload') fact(`${label} · ${key}`,child,key.endsWith('_minor'));return;}
      const row=el('div');row.className='fact';row.append(el('span',label),el('strong',minor?`${money(value,currency)}${currency==='CNY'?`（${uppercaseMoney(Number(value))}）`:''}`:displayValue(value)));card.append(row);
    };
    for(const f of response.fragments || []) fact(f.label || '详情',f.status ?? f.text ?? f.value_minor,f.status==null&&f.text==null&&f.value_minor!=null,f.currency || 'CNY');
    for(const f of response.data?.display || []) fact(f.label,f.value);
  }
  const link=response.app_deep_link || response.data?.app_deep_link;
  if (link) {
    const parsed=new URL(link,location.origin);
    if(parsed.origin===location.origin && parsed.pathname.startsWith('/app/')) {const a=el('a','在银行 App 中继续验证');a.href=parsed.href;a.className='ghost-link';card.append(a);}
  }
  const nonce=response.callback_nonce || response.nonce || response.data?.callback_nonce || response.data?.nonce;
  if(!group && nonce && ['confirmation','plan_confirmation'].includes(response.kind) && response.data?.assurance !== 'mfa') {
    const button=el('button','确认以上操作'); const eventId=crypto.randomUUID(), generation=state.generation;
    button.onclick=async()=>{if(generation!==state.generation)return;button.disabled=true;try{const response=await api('callbacks',{event_id:eventId,nonce,action:'confirm',group:false});if(generation!==state.generation)return;render(response,false);}catch(error){if(error.name==='StaleSessionError')return;status(error.message);button.disabled=false;}};
    card.append(button);
  }
}
$('connect').onclick=async()=>{
  const generation=++state.generation;state.busy=false;state.token=null;state.bound=null;state.conversation=crypto.randomUUID();$('messages').replaceChildren();controls();
  try { const response=await api('sessions',{identity:$('identity').value});if(generation!==state.generation)return;
    state.token=response.access_token;state.bound=response.bound_user_id;status('模拟 IM 身份已连接。');controls();
  } catch(error){if(error.name==='StaleSessionError')return;status(error.message);}
};
$('identity').onchange=()=>{state.generation++;state.busy=false;state.token=null;state.bound=null;$('messages').replaceChildren();status('身份已更换，请重新连接。');controls();};
$('bind').onclick=async()=>{const generation=state.generation;state.busy=true;controls();try{const response=await api('bind',{im_token:state.token},true);if(generation!==state.generation)return;state.bound=response.bound_user_id;status('绑定完成。');}catch(error){if(error.name==='StaleSessionError')return;status(error.message);}finally{if(generation===state.generation){state.busy=false;controls();}}};
$('newChat').onclick=()=>{state.generation++;state.busy=false;state.conversation=crypto.randomUUID();$('messages').replaceChildren();status('已新建对话。');controls();};
$('group').onchange=()=>{$('newChat').click();status($('group').checked?'群聊模式：敏感详情隐藏，高风险操作请返回 App。':'私聊模式。');};
$('composer').onsubmit=event=>{
  event.preventDefault();const text=$('message').value.trim();if(!text || $('send').disabled)return;
  const group=$('group').checked, payload={event_id:crypto.randomUUID(),conversation_id:state.conversation,text,group}, generation=state.generation;
  $('message').value='';bubble('user',text);
  const attempt=async()=>{if(generation!==state.generation)return;state.busy=true;controls();try{const response=await api('messages',payload);if(generation===state.generation)render(response,group);}catch(error){if(generation===state.generation){const card=bubble('error',error.message);if(error.code==='CONVERSATION_BUSY'){card.append(el('p','本次消息尚未开始处理，请稍后重新发送新消息。'));return;}const retry=el('button','重试这条消息');retry.onclick=()=>{retry.disabled=true;attempt();};card.append(retry);}}finally{if(generation===state.generation){state.busy=false;controls();}}};
  attempt();
};
window.addEventListener('online',controls);window.addEventListener('offline',()=>{status('当前离线，所有 IM 操作需联网。');controls();});window.addEventListener('storage',controls);controls();
