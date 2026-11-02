import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {displayValue,money,uppercaseMoney} from '../../frontend/presentation.mjs';
class Element {
 constructor(tag='div'){this.tagName=tag;this.children=[];this.value='demo_im_001';this.textContent='';this.checked=false;}
 append(...children){this.children.push(...children)} replaceChildren(...children){this.children=children} click(){this.onclick?.()} get text(){return this.textContent+this.children.map(c=>c.text).join(' ')}
}
function harness(){const elements=new Map(),get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id)},calls=[];
 const context={displayValue,money,uppercaseMoney,document:{getElementById:get,createElement:tag=>new Element(tag)},crypto:{randomUUID:()=>Math.random().toString()},navigator:{onLine:true},localStorage:{agentbank_token:'bank-session',agentbank_user:'user_002'},window:{addEventListener(){}},location:{origin:'https://bank.test'},URL,fetch:async(path,options)=>{calls.push({path,options});return {ok:true,json:async()=>({access_token:'im-token',bound_user_id:'user_002'})}}};vm.createContext(context);vm.runInContext(fs.readFileSync('frontend/webim.js','utf8').replace(/^import .*;\s*$/mg,''),context);return {context,get,calls,run:code=>vm.runInContext(code,context)};
}
test('independent IM session binds explicitly using bank session, messages use IM token without bank user payload',async()=>{
 const h=harness();await h.get('connect').onclick();assert.deepEqual(JSON.parse(h.calls[0].options.body),{identity:'demo_im_001'});
 await h.get('bind').onclick();assert.equal(h.calls[1].options.headers.Authorization,'Bearer bank-session');assert.deepEqual(JSON.parse(h.calls[1].options.body),{im_token:'im-token'});
 await h.run(`api('messages',{event_id:'e1',conversation_id:'chat1',text:'余额',group:false})`);
 const request=h.calls.at(-1);assert.equal(request.options.headers['X-IM-Token'],'im-token');assert.equal(request.options.headers.Authorization,undefined);assert.equal(JSON.parse(request.options.body).user_id,undefined);
});
test('group cards never render account fragments or interactive confirmation',()=>{
 const h=harness();h.run(`render({kind:'confirmation',message:'请返回 App',nonce:'signed',fragments:[{label:'余额',value_minor:99999}],data:{display:[{label:'收款人',value:'私人姓名'}]}},true)`);
 const card=h.get('messages');assert.doesNotMatch(card.text,/999|私人姓名|确认以上/);
});
test('private IM result facts share the semantic grouped card layout',()=>{
 const h=harness();h.run(`render({kind:'result',message:'查询完成',fragments:[{label:'可用余额',value_minor:123456,currency:'CNY'}]},false)`);
 const card=h.get('messages').children.at(-1),group=card.children.find(child=>child.className==='facts-grid');
 assert.equal(group.tagName,'dl');assert.equal(group.children[0].children[0].tagName,'dt');assert.equal(group.children[0].children[1].tagName,'dd');
 assert.match(card.text,/¥1234\.56/);
});
test('red App-only response offers safe local link without callback button',()=>{
 const h=harness();h.run(`render({kind:'app_required',message:'前往 App',nonce:'signed',app_deep_link:'/app/?record_id=r1'},false)`);
 assert.match(h.get('messages').text,/在银行 App 中继续验证/);assert.doesNotMatch(h.get('messages').text,/确认以上/);
});
test('signed yellow callback uses stable event ID when retrying',async()=>{
 const h=harness();h.run(`state.token='im-token';render({kind:'confirmation',message:'核对',nonce:'signed-nonce'},false)`);
 const button=h.get('messages').children.at(-1).children.at(-1).children[0];await button.onclick();await button.onclick();
 const requests=h.calls.filter(c=>c.path.endsWith('/callbacks'));assert.equal(requests.length,2);const first=JSON.parse(requests[0].options.body),second=JSON.parse(requests[1].options.body);assert.equal(first.event_id,second.event_id);assert.equal(first.nonce,'signed-nonce');
});
test('yellow confirmation with App fallback still offers callback, MFA never does',()=>{
 const h=harness();h.run(`render({kind:'confirmation',message:'核对',nonce:'yellow',app_deep_link:'/app/?record_id=r1'},false)`);assert.match(h.get('messages').text,/确认以上操作/);
 h.get('messages').replaceChildren();h.run(`render({kind:'confirmation',message:'需验证',nonce:'red',data:{assurance:'mfa'},app_deep_link:'/app/?record_id=r2'},false)`);assert.doesNotMatch(h.get('messages').text,/确认以上操作/);
});
for (const operation of ['callback','bind']) for (const transition of ['identity','connect','newChat']) {
 test(`in-flight ${operation} cannot update new session after ${transition}`,async()=>{
  const h=harness();h.run(`state.token='old-im';state.bound='old-bank'`);let release;
  h.context.fetch=async()=>new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>({kind:'result',message:'旧身份私密结果',bound_user_id:'old-bank',access_token:'old-im'})})});
  let pending;
  if(operation==='callback') {h.run(`render({kind:'confirmation',nonce:'old-nonce'},false)`);pending=h.get('messages').children.at(-1).children.at(-1).children[0].onclick();}
  else pending=h.get('bind').onclick();
  if(transition==='identity')h.get('identity').onchange();
  if(transition==='newChat')h.get('newChat').onclick();
  if(transition==='connect') {
   const oldRelease=release;const newer=h.get('connect').onclick();const newRelease=release;release=oldRelease;
   h.context.fetch=async()=>({ok:true,json:async()=>({})});newRelease();await newer;
  }
  const expectedStatus=h.get('imStatus').text,expectedBound=h.run('state.bound');release();await pending;
  assert.doesNotMatch(h.get('messages').text,/旧身份私密结果/);assert.equal(h.get('imStatus').text,expectedStatus);assert.equal(h.run('state.bound'),expectedBound);
 });
}
test('old network error cannot overwrite new identity status',async()=>{
 const h=harness();h.run(`state.token='old-im'`);let reject;h.context.fetch=()=>new Promise((_,r)=>{reject=r});const pending=h.get('bind').onclick();h.get('identity').onchange();const expected=h.get('imStatus').text;reject(Error('旧请求失败'));await pending;assert.equal(h.get('imStatus').text,expected);
});
test('gateway errors prefer actionable explanation and BUSY creates no same-event retry',async()=>{
 const h=harness();await h.get('connect').onclick();h.context.fetch=async()=>({ok:false,json:async()=>({code:'CONVERSATION_BUSY',explanation:{message:'上一条消息仍在处理',next_step:'稍后作为新消息发送'}})});
 await assert.rejects(h.run(`api('messages',{text:'余额'})`),/上一条消息仍在处理.*稍后作为新消息发送/);
 h.get('message').value='余额';h.get('composer').onsubmit({preventDefault(){}});await new Promise(resolve=>setImmediate(resolve));assert.match(h.get('messages').text,/尚未开始/);assert.doesNotMatch(h.get('messages').text,/重试这条消息/);
});
