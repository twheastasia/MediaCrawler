// Actual OpenCLI people methods on two owned Ego Pages. No credentials/cookies
// are printed or written. Browser service is shared, each platform has one page.
import {readFile,appendFile,mkdir} from 'node:fs/promises';
import {parseEnv} from 'node:util';
import {setTimeout as sleep} from 'node:timers/promises';
import {fileURLToPath,pathToFileURL} from 'node:url';
import path from 'node:path';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const pkg=path.join(root,'data/opencli_runtime/node_modules/@jackwener/opencli');
const allowed={maimai:'search-talents',linkedin:'people-search'};

export function safeData(value) {
  if(Array.isArray(value))return value.map(safeData);
  if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value)
    .filter(([k])=>!/^(cookie|set-cookie|authorization|password|csrf.?token|access.?token|refresh.?token)$/i.test(k))
    .map(([k,v])=>[k,safeData(v)]));
  return value;
}

export function egoEvaluationSource(script) {
  // OpenCLI accepts arrow-function source strings; Ego evaluates strings as
  // expressions. Invoke only the uncalled function form, leaving IIFEs intact.
  return typeof script==='string'&&/^\s*(?:async\s+)?(?:\([^()]*\)|[A-Za-z_$][\w$]*)\s*=>/.test(script)
    ?`(${script})()`:script;
}

export async function peopleSearch(taskSpace,{spaceId,pageLabel,platform,keyword,pageNumber=0,size=20,output}) {
  if(!allowed[platform])throw new Error('unsupported_people_platform');
  const dir=path.resolve(output,platform);await mkdir(dir,{recursive:true});
  const log=path.join(dir,'requests.jsonl');
  async function controlled(operation,fn){
    let last=0;
    try{for(const line of (await readFile(log,'utf8')).trim().split('\n').reverse()){
      const r=JSON.parse(line);if(r.event==='end'){last=r.time;break;}
    }}catch(e){if(e.code!=='ENOENT')throw e;}
    if(last+15000>Date.now())await sleep(last+15000-Date.now());
    await appendFile(log,JSON.stringify({event:'start',time:Date.now(),operation,gap_ms:last?Date.now()-last:null})+'\n');
    try{return await fn();}finally{await appendFile(log,JSON.stringify({event:'end',time:Date.now(),operation})+'\n');}
  }
  const task=await taskSpace(spaceId);const ego=task.page(pageLabel);
  const {getRegistry}=await import(pathToFileURL(path.join(pkg,'dist/src/registry-api.js')));
  await import(pathToFileURL(path.join(pkg,`clis/${platform}/${allowed[platform]}.js`)));
  const spec=getRegistry().get(platform+'/'+allowed[platform]);
  if(!spec||spec.access!=='read')throw new Error('read_adapter_missing');
  let rawRecords=[],rawPayload=null,loggedInAgain=false,explicitEmpty=false,apiShape=null;
  let nextPageUrl=null;
  function error(code){return Object.assign(new Error(),{code});}
  async function state(){
    return ego.evaluate(()=>{
      const visible=e=>e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
      const t=document.body?.innerText||'';
      return {path:location.pathname,login:[...document.querySelectorAll('input[type=password],input[name=session_key],input[placeholder*=手机号]')].some(visible),
        captcha:[...document.querySelectorAll('#captcha-internal,iframe[src*=captcha],iframe[src*=challenge],.geetest_panel')].some(visible),
        limited:/commercial use limit|搜索次数已达|达到.*搜索.*上限|访问过于频繁|访问受限|安全验证|人机验证|access denied|too many requests/i.test(t),
        empty:/未找到任何结果|暂无搜索结果|没有找到.*人才|没有符合条件的|no results found|no matching results/i.test(t),
        hasPeople:!!document.querySelector('a[href*="/in/"]'),ready:!!document.body&&t.trim().length>40};
    });
  }
  async function guard(){
    const s=await state();
    if(s.captcha||s.limited)throw error('ACCESS_RESTRICTED');
    if(s.login||/\/(login|checkpoint|authwall|check)(\/|$)/.test(s.path))throw error('AUTH_REQUIRED');
    return s;
  }
  async function navigate(url){
    const u=new URL(url),domain=platform==='maimai'?'maimai.cn':'linkedin.com';
    if(u.protocol!=='https:'||!(u.hostname===domain||u.hostname.endsWith('.'+domain)))throw error('UNEXPECTED_SITE');
    if(platform==='linkedin'&&pageNumber>0){
      const current=new URL(await ego.url());
      if(current.pathname===u.pathname&&current.searchParams.get('keywords')===keyword&&
         Number(current.searchParams.get('page')||1)===pageNumber+1){await guard();return;}
      if(current.pathname!==u.pathname||current.searchParams.get('keywords')!==keyword){
        await controlled('opencli_linkedin_search_start',()=>ego.goto(u.href));
        await guard();
      }
      const label=`第 ${pageNumber+1} 页`;
      const selector='button[aria-label='+JSON.stringify(label)+']';
      const offered=await ego.evaluate(({label,n})=>[...document.querySelectorAll('button')].some(b=>!b.disabled&&
        (b.getAttribute('aria-label')===label||b.getAttribute('aria-label')===`Page ${n}`)),{label,n:pageNumber+1});
      if(!offered)throw error('PAGINATION_NOT_EXPOSED');
      await controlled('opencli_linkedin_people_page',async()=>{
        const before=await ego.evaluate(()=>[...document.querySelectorAll('a[href*="/in/"]')].map(a=>a.href).join('|'));
        const chinese=await ego.evaluate(s=>!!document.querySelector(s),selector);
        await ego.click(chinese?selector:`button[aria-label="Page ${pageNumber+1}"]`);
        await ego.waitForFunction(({n,before})=>Number(new URL(location.href).searchParams.get('page')||1)===n&&
          [...document.querySelectorAll('a[href*="/in/"]')].map(a=>a.href).join('|')!==before,
          {n:pageNumber+1,before},{timeout:15000});
      });await guard();return;
    }
    await controlled('opencli_'+platform+'_navigation',async()=>{
      await ego.goto(u.href);
      try{await ego.waitForFunction(()=>document.body&&document.body.innerText.trim().length>80&&!location.pathname.startsWith('/check/'),undefined,{timeout:15000});}catch{}
    });await guard();
  }
  async function loginLinkedIn(){
    if(loggedInAgain)throw error('AUTH_REQUIRED');loggedInAgain=true;
    await controlled('linkedin_login_navigation',()=>ego.goto('https://www.linkedin.com/login'));
    const s=await state();if(s.captcha||s.limited)throw error('ACCESS_RESTRICTED');
    const form=await ego.evaluate(()=>!!document.querySelector('input[name=session_key]')&&!!document.querySelector('input[name=session_password]'));
    if(!form)throw error('LOGIN_FORM_UNAVAILABLE');
    const cfg=parseEnv(await readFile(path.join(root,'.env'),'utf8'));
    if(!cfg.LINKEDIN_USERNAME||!cfg.LINKEDIN_PASSWORD)throw error('LOGIN_CREDENTIALS_MISSING');
    await controlled('linkedin_login_once',async()=>{
      await ego.fill('input[name=session_key]',cfg.LINKEDIN_USERNAME);
      await ego.fill('input[name=session_password]',cfg.LINKEDIN_PASSWORD);
      await ego.click('button[aria-label="Sign in"]');
      try{await ego.waitForFunction(()=>!document.querySelector('input[name=session_password]')&&!location.pathname.startsWith('/check/'),undefined,{timeout:20000});}catch{}
    });await guard();
  }
  const browser={
    goto:navigate,
    wait:async()=>{
      try{await ego.waitForFunction(()=>document.body&&document.body.innerText.trim().length>100,undefined,{timeout:12000});}catch{}
      await guard();
    },
    waitForTimeout:async()=>{await guard();},
    getCookies:async({url})=>{
      await guard();const expected=platform==='maimai'?'maimai.cn':'www.linkedin.com';
      if(new URL(url).hostname!==expected)throw error('COOKIE_DOMAIN_MISMATCH');
      const value=await ego.cdp('Network.getCookies',{urls:[url]});return value.cookies||[];
    },
    evaluate:async script=>{
      await guard();
      if(/\.click\s*\(|\.submit\s*\(/.test(script))throw error('UNCONTROLLED_ADAPTER_ACTION');
      let value;
      const source=egoEvaluationSource(script);
      if(/\bfetch\s*\(/.test(script))value=await controlled('opencli_'+platform+'_api',()=>ego.evaluate(source));
      else value=await ego.evaluate(source);
      await guard();
      if(platform==='maimai'&&value&&typeof value==='object'){
        const shape=v=>v&&typeof v==='object'?Object.fromEntries(Object.entries(v)
          .filter(([k])=>!/token|cookie|password|authorization/i.test(k))
          .map(([k,v])=>[k,Array.isArray(v)?{type:'array',length:v.length}:v===null?'null':typeof v])):null;
        apiShape={root:shape(value),data:shape(value.data)};
        const list=[value.data?.list,value.data?.talent_list,value.list,value.talent_list].find(Array.isArray);
        if(Array.isArray(list)){rawRecords=list;explicitEmpty=list.length===0;}
      }
      if(platform==='linkedin'&&Array.isArray(value?.rows)){rawRecords=value.rows;rawPayload=value;}
      return value;
    },
  };
  const args=platform==='maimai'?{query:keyword,page:pageNumber,size:Math.min(20,size),sortby:0,is_direct_chat:0}:
    {keywords:keyword,limit:Math.min(10,size)};
  const defaults=Object.fromEntries(spec.args.filter(a=>a.default!==undefined).map(a=>[a.name,a.default]));
  async function call(){rawRecords=[];rawPayload=null;return spec.func(browser,{...defaults,...args});}
  try{
    let rows;
    try{rows=await call();}catch(e){
      const s=await state();
      if(platform==='linkedin'&&(e.code==='AUTH_REQUIRED'||s.login)&&!s.captcha&&!s.limited){await loginLinkedIn();rows=await call();}
      else throw e;
    }
    if(platform==='linkedin'){
      rawRecords=rows.map(row=>rawRecords.find(r=>r.profile_url===row.profile_url)||row);
      const nav=await ego.evaluate(()=>{
        const list=[...document.querySelectorAll('a[href*="/search/results/people/"]')];
        const next=list.find(a=>/^(?:下一页|Next)$/i.test((a.innerText||'').trim())||/下一页|Next/i.test(a.getAttribute('aria-label')||''));
        return {next:next?.href||null};
      });nextPageUrl=nav.next;
    }
    return {ok:true,platform,keyword,pageNumber,command:platform+'/'+allowed[platform],rows:safeData(rows),
      rawRecords:safeData(rawRecords),rawPayload:safeData(rawPayload),nextPageUrl,loggedInAgain,explicitEmpty,
      cookiesSaved:false,credentialsSaved:false};
  }catch(e){
    let s={};try{s=await state();}catch{}
    let code=e.code||'ADAPTER_ERROR';
    if(s.captcha||s.limited)code='ACCESS_RESTRICTED';
    else if(s.login||/\/(login|authwall|checkpoint)(\/|$)/.test(s.path||'')||/需要登录|登录失效|unauthorized/i.test(e.message||''))code='AUTH_REQUIRED';
    if(e.code==='EMPTY_RESULT'&&(explicitEmpty||s.empty))return {ok:true,platform,keyword,pageNumber,command:platform+'/'+allowed[platform],rows:[],rawRecords:[],explicitEmpty:true,loggedInAgain};
    return {ok:false,platform,keyword,pageNumber,errorCode:code,errorType:e.name||'Error',loggedInAgain,
      state:{path:s.path||null,login:!!s.login,captcha:!!s.captcha,limited:!!s.limited},apiShape,requiresUserAction:true};
  }
}
