import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import {uppercaseMoney} from '../../frontend/presentation.mjs';
test('Chinese uppercase handles zero, gaps, fractions and negatives',()=>{
 for(const [value,expected] of [[0,'零元整'],[100,'壹元整'],[10001,'壹佰元零壹分'],[100010001,'壹佰万零壹佰元零壹分'],[-105,'负壹元零伍分'],[100000000,'壹佰万元整']]) assert.equal(uppercaseMoney(value),expected);
});
test('service worker intercepts only public static shell, never API, query, credentials or staff',async()=>{
 const handlers={},fetched=[],cached=[];
 const context={URL,self:{location:{origin:'https://bank.test'},addEventListener:(name,handler)=>handlers[name]=handler,skipWaiting(){},clients:{claim(){}}},caches:{open:async()=>({addAll:async assets=>cached.push(...assets),match:async path=>'offline:'+path})},fetch:async request=>{fetched.push(request.url);throw Error('offline')}};
 vm.runInNewContext(fs.readFileSync('frontend/sw.js','utf8'),context);
 let install;handlers.install({waitUntil:p=>install=p});await install;assert.ok(cached.includes('/app/index.html'));assert.ok(cached.every(p=>p.startsWith('/app/')&&!p.includes('staff')));
 for(const [path,method,auth,expected] of [['/v1/accounts','GET',false,false],['/app/?token=secret','GET',false,false],['/app/index.html','POST',false,false],['/app/index.html','GET',true,false],['/app/staff.html','GET',false,false],['/app/app.js','GET',false,true]]){
  let intercepted=false,result;
  handlers.fetch({request:{url:'https://bank.test'+path,method,headers:{has:()=>auth}},respondWith:p=>{intercepted=true;result=p;}});
  assert.equal(intercepted,expected,path);if(result)assert.equal(await result,'offline:/app/app.js');
 }
});
