import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {uppercaseMoney,displayValue,money,resultKind} from '../../frontend/presentation.mjs';
class Element {
  constructor(tag='div') { this.tagName=tag; this.children=[]; this._value=null; this.textContent=''; this.dataset={}; this.listeners={}; this.parentElement=this; this.disabled=false; }
  get value() { return this._value ?? (this.tagName === 'option' ? this.textContent : ''); }
  set value(value) { this._value=value; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
  querySelector() { return new Element('button'); }
  addEventListener(k, fn) { this.listeners[k]=fn; }
  removeEventListener() {} scrollIntoView() {} focus() {} close() {} showModal() {} reset() {} remove() {}
  get text() { return this.textContent + this.children.map(c=>c.text).join(' '); }
}
function harness() {
 const elements=new Map(); const get=id=>{if(!elements.has(id)) elements.set(id,new Element());return elements.get(id);};
 const calls=[]; const context={uppercaseMoney,displayValue,money,resultKind,window:{addEventListener(){}},document:{getElementById:get,createElement:tag=>new Element(tag),querySelector:()=>({value:'balance'}),querySelectorAll:()=>[]},crypto:{randomUUID:()=>`uuid-${calls.length}-${Math.random()}`},localStorage:{removeItem(){}},navigator:{onLine:true},location:{search:''},URLSearchParams,console,fetch:async(path,options)=>{calls.push({path,options}); if(path==='/v1/sessions') return new Promise(()=>{}); return {ok:true,json:async()=>({kind:'result',message:'完成',status:'succeeded',data:{}})};}};
 vm.createContext(context); vm.runInContext(fs.readFileSync('frontend/app.js','utf8').replace(/^import .*;\s*$/mg,''),context);
 return {context,get,calls,run:code=>vm.runInContext(code,context)};
}
test('Chinese money fragment renders yuan and uppercase, never raw cents',()=>{const h=harness();h.run(`addBubble('result','余额',[{label:'可用余额',value_minor:123456,currency:'CNY'}])`);assert.match(h.get('messages').text,/¥1234\.56/);assert.match(h.get('messages').text,/壹仟贰佰叁拾肆元伍角陆分/);});
test('status fragment chooses status ahead of empty numeric fallback',()=>{const h=harness();h.run(`addBubble('result','卡片',[{label:'状态',value_minor:0,status:'frozen'}])`);assert.match(h.get('messages').text,/已冻结/);});
test('PIN preparation exposes confirmation and receipt, then MFA then success',async()=>{const h=harness();h.run(`showDirectResult({status:'awaiting_confirmation',record_id:'pin1',capability_id:'C08',data:{receipt_id:'receipt',display_hash:'hash'}},'PIN 已修改')`);const b=h.get('messages').children.at(-1);assert.match(b.text,/确认执行/);const button=b.children.find(c=>c.className==='card-actions').children[0];await button.onclick();const req=h.calls.at(-1);assert.equal(req.path,'/v1/capability-records/pin1/confirm');assert.deepEqual(JSON.parse(req.options.body),{receipt_id:'receipt',display_hash:'hash'});});
test('new identical messages have separate IDs',async()=>{const h=harness();await h.run(`send('查余额')`);await h.run(`send('查余额')`);const requests=h.calls.filter(c=>c.path.includes('/messages'));assert.ok(JSON.parse(requests[0].options.body).message_id);assert.notEqual(JSON.parse(requests[0].options.body).message_id,JSON.parse(requests[1].options.body).message_id);});
test('ordinary nested facts render structured labels without raw JSON or secrets',()=>{const h=harness();h.run(`addBubble('result','卡片',[],{card:{status:'frozen',limit_minor:1200,pin:'1234'}})`);const text=h.get('messages').text;assert.match(text,/已冻结/);assert.match(text,/¥12\.00/);assert.doesNotMatch(text,/[{}]|1234/);});
test('action review keeps decision details visible and groups sample sources in disclosure',()=>{
 const h=harness();h.run(`addBubble('confirmation','请核对',[],{pre_action_review:{decision:'block',subject:{name:'星河模拟理财有限公司'},registration:{detail:'本地名录登记'},qualification:{detail:'模拟名录命中'},signals:[{label:'风险',value:'未覆盖'},{label:'财务',value:'有限'},{label:'信用',value:'已核对'},{label:'口碑',value:'未覆盖'}],gaps:['缺少实时资料'],questions:['核实产品编码'],evidence_sources:['来源 A'],product:{name:'稳健产品',risk_level:'R2',term_days:30,redemption_days:2},notice:'本地模拟数据'}},'review-1')`);
 const card=h.get('messages').children.at(-1),panel=card.children.find(child=>child.className?.startsWith('pre-action-review'));
 const text=h.get('messages').text;for(const phrase of ['行动前审查','星河模拟理财有限公司','资质核验','风险','财务','信用','口碑','待核实信息','建议确认','本地模拟数据','稳健产品','来源 A'])assert.ok(text.includes(phrase),phrase);
 assert.ok(panel.children.some(child=>child.className?.includes('review-gaps')));
 assert.ok(panel.children.some(child=>child.className?.includes('review-questions')));
 const sources=panel.children.find(child=>child.className==='review-source-details');assert.equal(sources.tagName,'details');
 assert.match(sources.children[0].text,/查看本地样本来源/);assert.ok(sources.children.some(child=>child.className?.includes('review-sources')));
 assert.doesNotMatch(text,/source_mode|local_simulation/);
});

test('ordinary result facts use semantic label and value groups',()=>{
 const h=harness();h.run(`addBubble('result','余额',[{label:'可用余额',value_minor:123456,currency:'CNY'}])`);
 const card=h.get('messages').children.at(-1),group=card.children.find(child=>child.className==='facts-grid');
 assert.equal(group.tagName,'dl');assert.equal(group.children[0].children[0].tagName,'dt');assert.equal(group.children[0].children[1].tagName,'dd');
});

test('transaction rows emphasize merchant and amount before date and metadata',()=>{
 const h=harness();h.run(`addBubble('result','交易记录',[],{transactions:[{merchant:'模拟超市',posted_at:'2026-11-02',category:'购物',direction:'支出',amount_minor:1280}]})`);
 const card=h.get('messages').children.at(-1),list=card.children.find(child=>child.className==='transaction-list'),row=list.children[0];
 assert.match(row.children[0].text,/模拟超市/);assert.match(row.children[0].text,/¥12\.80/);
 assert.equal(row.children[1].tagName,'time');assert.match(row.children[2].text,/购物/);assert.match(row.children[2].text,/支出/);
});

test('customer result cards hide execution internals and translate authorization',()=>{const h=harness();h.run(`addBubble('result','完成',[],{plan_id:'plan-internal',capability_id:'T01',assurance:'confirm',transfer_result:{status:'succeeded',ledger_entry_ids:['ledger-internal']}})`);const text=h.get('messages').text;assert.doesNotMatch(text,/plan-internal|ledger-internal|ledger_entry_ids|T01|confirm/);assert.match(text,/用户确认|已完成/);});

test('PIN submit chooses loaded card and offers confirmation instead of false success',async()=>{
 const h=harness();h.get('newPin').value='2468';h.get('confirmPin').value='2468';h.get('pinCard').value='card-user2';
 h.run(`state.cards=[{id:'card-user2'}]`);
 h.context.fetch=async(path,options)=>{h.calls.push({path,options});return {ok:true,json:async()=>({capability_id:'C08',status:'awaiting_confirmation',record_id:'pin2',data:{receipt_id:'r2',display_hash:'h2'}})}};
 await h.context.changePin({preventDefault(){}});
 assert.equal(JSON.parse(h.calls.at(-1).options.body).payload.card_id,'card-user2');
 assert.match(h.get('messages').text,/确认执行/);assert.doesNotMatch(h.get('messages').text,/PIN 已修改/);
 h.run(`showDirectResult({status:'awaiting_mfa',capability_id:'C08',record_id:'pin2'},'完成')`);assert.match(h.get('messages').text,/输入验证码/);
});
test('PIN rejects stale or cross-user card selection without network request',async()=>{
 const h=harness(),before=h.calls.length;h.get('pinCard').value='card_001';h.run(`state.cards=[{id:'card_002'}]`);
 await h.context.changePin({preventDefault(){}});assert.equal(h.calls.length,before);assert.match(h.get('pinError').text,/当前账户/);
});
test('offline API operations fail before fetch',async()=>{const h=harness();h.context.navigator.onLine=false;const before=h.calls.length;await assert.rejects(h.run(`api('/v1/cards')`),/离线/);assert.equal(h.calls.length,before);});
test('in-flight response from previous bank session is not displayed after switch',async()=>{
 const h=harness();let release;
 h.context.fetch=async()=>new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>({kind:'result',message:'旧账户私密余额',data:{available_minor:99999}})})});
 const pending=h.run(`send('查余额')`);h.run(`state.generation++`);release();await pending;assert.doesNotMatch(h.get('messages').text,/旧账户私密/);
});
for (const method of ['verify','verifyDirect']) test(`${method} never submits OTP collected for old bank session`,async()=>{
 const h=harness();let release;h.context.pendingOtp=new Promise(resolve=>{release=resolve});h.run(`requestOtp=()=>pendingOtp`);const before=h.calls.length,pending=h.run(`${method}('old-record')`);h.run('state.generation++');release('123456');await pending;assert.equal(h.calls.length,before);
});
test('transfer reported succeeded without authoritative transfer result remains pending',()=>{const h=harness();h.run(`showDirectResult({status:'succeeded',capability_id:'T01',data:{}},'完成')`);assert.match(h.get('messages').children.at(-1).className,/pending/);});
for (const status of ['awaiting_confirmation','awaiting_mfa']) test(`linked ${status} plan exposes bank authorization controls`,async()=>{
 const h=harness();h.context.location.search='?plan_id=plan-red';h.context.fetch=async(path,options)=>{h.calls.push({path,options});return {ok:true,json:async()=>({plan_id:'plan-red',status,receipt_id:'plan-receipt',display_hash:'plan-hash',assurance:'mfa',display:[{label:'操作',value:'订阅取消'}]})}};
 await h.run('loadLinkedRecord()');assert.equal(h.calls.at(-1).path,'/v1/plans/plan-red');const card=h.get('messages').children.find(c=>c.className.includes('bubble'));assert.match(card.text,status==='awaiting_mfa'?/输入验证码/:/确认执行/);
 if(status==='awaiting_confirmation'){await card.children.find(c=>c.className==='card-actions').children[0].onclick();const request=h.calls.at(-1);assert.match(request.path,/\/conversations\/.+\/records\/plan-red\/confirm/);assert.deepEqual(JSON.parse(request.options.body),{receipt_id:'plan-receipt',display_hash:'plan-hash'});}
});

test('new payee placeholder never masquerades as selected payee ID',async()=>{
 const h=harness();h.get('recipientName').value='房东';h.context.fetch=async()=>({ok:true,json:async()=>({payees:[{id:'payee-landlord',masked_name:'房东',phone_tail:null}]})});await h.run('lookupPayees()');assert.equal(h.get('recipientCandidate').children[0].value,'');assert.doesNotMatch(h.get('recipientCandidate').text,/null/);
});
test('server display fields explicitly marked fen render yuan with uppercase',()=>{const h=harness();h.run(`addBubble('plan_confirmation','核对',[],{display:[{label:'资金预留（分）',value:'1000'}]})`);assert.match(h.get('messages').text,/¥10\.00（壹拾元整）/);});

test('risk questionnaire requires ten answers and submits actual selected values with version', async()=>{
 const h=harness();h.run(`addBubble('clarification','请填写',[],{questionnaire:{version:'v1',title:'风险测评',questions:Array.from({length:10},(_,i)=>({id:'q'+i,text:'问题'+i,options:Array.from({length:6},(_,j)=>({value:j,text:'选项'+j}))}))}})`);
 const card=h.get('messages').children.at(-1), form=card.children.find(c=>c.tagName==='form');
 assert.ok(form);assert.match(form.text,/问题9/);const before=h.calls.length;
 await form.onsubmit({preventDefault(){}});assert.equal(h.calls.length,before);
 const selects=form.children.filter(c=>c.tagName==='label').map(c=>c.children.find(x=>x.tagName==='select'));
 assert.equal(selects.length,10);selects.forEach((s,i)=>s.value=String(i%6));
 await form.onsubmit({preventDefault(){}});
 const request=h.calls.at(-1);assert.equal(request.path,'/v1/risk/submit');
 assert.deepEqual(JSON.parse(request.options.body),{questionnaire_version:'v1',answers:[0,1,2,3,4,5,0,1,2,3]});
});

test('semantic revision disables the superseded confirmation control',()=>{
 const h=harness();h.run(`addBubble('confirmation','旧确认',[],{},'old');addBubble('confirmation','新确认',[],{invalidated_record_ids:['old']},'new')`);
 const old=h.get('messages').children[0];assert.equal(old.children.find(c=>c.className==='card-actions').children[0].disabled,true);
 assert.match(old.text,/已作废/);
});
