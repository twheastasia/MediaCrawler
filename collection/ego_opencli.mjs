// OpenCLI's read adapters driven by an Ego Page. No Chrome endpoint, Browser
// Bridge daemon, credential export, hidden application state or write commands.
import {fileURLToPath,pathToFileURL} from 'node:url';
import path from 'node:path';
const pkg=path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../data/opencli_runtime/node_modules/@jackwener/opencli');
const postClicks='for (const expander of expanders.slice(0, 20)) {\n      try { expander.click(); } catch {}\n    }';
const timelineClicks='for (i = 0; i < moreButtons.length; i += 1) {\n      try { moreButtons[i].click(); } catch (err) {}\n    }';

export function readOnlyScript(script) {
  const result=script.replace(postClicks,'/* host controls expansion */').replace(timelineClicks,'/* host controls expansion */');
  if (/\.click\s*\(|\.submit\s*\(|\bfetch\s*\(/.test(result)) throw Object.assign(new Error(),{code:'UNCONTROLLED_ADAPTER_ACTION'});
  return result;
}

async function registry() {
  const {getRegistry}=await import(pathToFileURL(path.join(pkg,'dist/src/registry-api.js')));
  await import(pathToFileURL(path.join(pkg,'clis/linkedin/posts.js')));
  await import(pathToFileURL(path.join(pkg,'clis/linkedin/timeline.js')));
  return getRegistry();
}

export async function timelineExtractScript() {
  const methods=await registry();let script;
  const capture={goto:async()=>{},wait:async()=>{},evaluate:async s=>{script=s;throw new Error('capture_only');}};
  try{await methods.get('linkedin/timeline').func(capture,{limit:1});}catch{}
  if(!script)throw Object.assign(new Error(),{code:'OPENCLI_SCRIPT_CHANGED'});
  return localizeTimelineScript(readOnlyScript(script));
}

export function localizeTimelineScript(script) {
  // The upstream listitem reader currently recognizes only English UI labels.
  return script
    .replace("if (blocks[0] !== 'Feed post') return null;", "if (!['Feed post','信息流动态'].includes(blocks[0])) return null;")
    .replace(String.raw`return /^\d+\s*(s|m|h|d|w|mo|yr|min)(\s*[•.])?$/i.test(lower);`,
      String.raw`return /^\d+\s*(s|m|h|d|w|mo|yr|min)(\s*[•.])?$/i.test(lower) || /^(?:\d+\s*(?:分钟|小时|天|周|个月|年)前|刚刚)$/.test(lower);`)
    .replace('(follow|send message|connect|visit my website|view my newsletter|subscribe)',
      '(follow|send message|connect|visit my website|view my newsletter|subscribe|关注|发消息|建立联系|订阅)')
    .replace('(like|comment|repost|send|reply|load more comments)',
      '(like|comment|repost|send|reply|load more comments|赞|评论|重新发布|发送|回复|加载更多评论)');
}

export async function invokeLinkedIn(ego,controlled,{command='posts',args={},maxScrolls=2}={}) {
  if(!['posts','timeline'].includes(command))throw Object.assign(new Error(),{code:'READ_COMMAND_NOT_ALLOWED'});
  const methods=await registry();const calls=[];let scrolls=0,compatibilityApplied=false;
  async function guard(){
    const s=await ego.evaluate(()=>({path:location.pathname,hasPassword:!!document.querySelector('input[type=password]'),
      challenge:!!document.querySelector('#captcha-internal,iframe[src*="captcha"],iframe[src*="challenge"]')}));
    if(s.challenge||s.hasPassword||/\/(checkpoint|authwall|login)(\/|$)/.test(s.path))throw Object.assign(new Error(),{code:'AUTH_OR_ACCESS_REQUIRED'});
  }
  const page={
    goto:async url=>{
      if((await ego.url()).replace(/\/$/,'')===url.replace(/\/$/,'')){
        calls.push('reuse_current_page');await guard();return;
      }
      calls.push('goto');
      await controlled('opencli_linkedin_'+command+'_navigation',async()=>{
        await ego.goto(url);
        try{await ego.waitForFunction(()=>document.body?.innerText?.length>200&&!location.pathname.startsWith('/check/'),undefined,{timeout:15000});}catch{}
      });await guard();
    },
    wait:async()=>{
      try{await ego.waitForFunction(()=>document.body?.innerText?.length>200,undefined,{timeout:10000});}catch{}
    },
    evaluate:async script=>{
      calls.push('evaluate');await guard();
      let result=await ego.evaluate(readOnlyScript(script));
      if(command==='posts'&&result&&Array.isArray(result.rows)&&result.rows.length===0){
        // Current LinkedIn SDUI uses listitems. OpenCLI timeline already has a
        // reader for them; reuse that upstream reader for the posts command.
        const modern=await ego.evaluate(await timelineExtractScript());
        if(modern?.loginRequired)throw Object.assign(new Error(),{code:'AUTH_OR_ACCESS_REQUIRED'});
        if(Array.isArray(modern?.posts)&&modern.posts.length){
          compatibilityApplied=true;
          result={...result,rows:modern.posts.map(p=>({...p,body:p.text,raw_text:p.text}))};
        }
      }
      return result;
    },
    autoScroll:async()=>{
      if(scrolls++>=maxScrolls)throw Object.assign(new Error(),{code:'PROBE_NO_PARSED_ROWS'});
      calls.push('scroll');
      await controlled('opencli_linkedin_'+command+'_scroll',()=>ego.evaluate(()=>window.scrollBy(0,innerHeight)));
      await guard();
    },
  };
  const spec=methods.get('linkedin/'+command);
  if(spec.access!=='read')throw Object.assign(new Error(),{code:'READ_COMMAND_NOT_ALLOWED'});
  const rows=await spec.func(page,args);
  return {rows,calls,compatibilityApplied,command:'linkedin/'+command};
}
