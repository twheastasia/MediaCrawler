"""Normal Xiaohongshu web UI: rendered public DOM only, no API replay."""
from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import quote, urlsplit

from collection.comments import Page, CommentParseError, CommentUnavailable, normalize
from collection.runtime import AccessRestricted, LoginRequired
from collection.worker import Job, normalized
from media_downloader import MediaItem, MediaType


class WebMarkupChanged(ValueError):
    requires_user_action=True


SNAPSHOT = r'''() => {
 const text=e=>e?(e.innerText||e.textContent||'').trim():'';
 const visible=e=>!!(e&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');
 const profile=a=>a?new URL(a.href).pathname.split('/user/profile/')[1]?.split('/')[0]||null:null;
 const root=document.querySelector('.note-container');
 const overlays=[...document.querySelectorAll('[class*=captcha],[id*=captcha],.verify-container,iframe[src*=captcha],.error-container,.error-wrapper,.reds-message,.toast')].filter(visible);
 const blocked=overlays.some(e=>/captcha|verify/i.test(String(e.className)+' '+e.id+' '+(e.src||''))||/访问受限|访问频繁|操作频繁|安全验证|完成验证/.test(text(e)));
 const unavailable=!root&&(location.pathname==='/404'||/你访问的页面不见了/.test(document.title)||/当前笔记暂时无法浏览|笔记已删除|内容不存在|该内容已被删除/.test(text(document.body)));
 const cards=[...document.querySelectorAll('.note-item')].filter(visible).map(e=>{
  const link=e.querySelector('a.cover')||e.querySelector('a[href*="/explore/"],a[href*="/search_result/"]');
  const author=e.querySelector('a.author');
  return {note_id:link?.pathname.match(/\/(?:explore|search_result|discovery\/item)\/([\w-]+)/)?.[1]||'',
   source_url:link?.href||'',type:e.querySelector('.play-icon,[class*=video-icon]')?'video':'normal',
   title:text(e.querySelector('.title')),user:{user_id:profile(author),nickname:text(author)},
   pinned:/置顶/.test(text(e)),cover_url:e.querySelector('img')?.currentSrc||e.querySelector('img')?.src||''};
 }).filter(e=>e.note_id);
 const comments=root?[...root.querySelectorAll('.comment-item')].filter(visible).map(e=>{
  const parent=e.classList.contains('comment-item-sub')?e.closest('.parent-comment')?.querySelector('.comment-item:not(.comment-item-sub)'):null;
  const author=e.querySelector('a.name[href*="/user/profile/"]')||e.querySelector('a[href*="/user/profile/"]');
  return {id:e.id.replace(/^comment-/,''),parent_id:parent?.id.replace(/^comment-/,'')||null,
   content:text(e.querySelector('.content')),user:{user_id:profile(author),nickname:text(author)},
   published_display:text(e.querySelector('.date')),like_display:text(e.querySelector('.like .count')),
   images:[...e.querySelectorAll('.comment-picture img')].map(i=>i.currentSrc||i.src),source:'public_web_dom'};
 }):[];
 document.querySelectorAll('[data-mc-xhs-expand]').forEach(e=>e.removeAttribute('data-mc-xhs-expand'));
 const expand=root?[...root.querySelectorAll('div,a,button,span')].filter(e=>visible(e)&&/^展开(?:剩余)?\s*\d*\s*条?回复$|^展开更多回复$|^查看更多回复$/.test(text(e))&&!e.querySelector('a,button,div,span')):[];
 expand.forEach((e,i)=>e.setAttribute('data-mc-xhs-expand',String(i)));
 const area=root?.querySelector('.comments-container,.comments-el');
 const total=text(area).match(/^共\s*([\d,]+)\s*条评论/);
 const end=root?[...root.querySelectorAll('.end-container,.end,.end-tip,.no-more,.comments-container .bottom')].some(e=>visible(e)&&/THE END|没有更多|暂无更多|已显示全部/i.test(text(e))):false;
 const emptyNode=root?.querySelector('.no-comments');
 const noComments=visible(emptyNode)&&/这是一片荒地|暂无评论|还没有评论|快来评论/.test(text(emptyNode));
 const author=root?.querySelector('.author-container a.name[href*="/user/profile/"],.author-wrapper a.name[href*="/user/profile/"],.author a.name[href*="/user/profile/"]');
 const videos=root?[...root.querySelectorAll('video')].map(v=>v.currentSrc||v.src).filter(Boolean):[];
 const images=root?[...root.querySelectorAll('.swiper-slide img,.note-slider img')].map(i=>i.currentSrc||i.src).filter(Boolean):[];
 if(root)for(const video of root.querySelectorAll('video'))if(video.poster)images.push(video.poster);
 return {path:location.pathname,url:location.href,blocked,unavailable,cards,comments,
  emptySearch:location.pathname==='/search_result'&&/没有找到|暂无搜索结果|暂无相关笔记|没找到.*(?:内容|笔记|结果)|未找到.*(?:内容|笔记|结果)/.test(text(document.body)),
  loggedOut:[...document.querySelectorAll('.login-container,.login-modal,.login-mask')].some(visible),
  total:total?Number(total[1].replaceAll(',','')):null,end,noComments,expandCount:expand.length,
  root:!!root,note:root?{note_id:location.pathname.match(/\/(?:explore|search_result|discovery\/item)\/([\w-]+)/)?.[1]||'',
    title:text(root.querySelector('#detail-title')),desc:text(root.querySelector('#detail-desc')),
    user:{user_id:profile(author),nickname:text(author)},published_display:text(root.querySelector('.bottom-container .date,.date')),
    image_list:[...new Set(images)].map(url=>({url_default:url})),videos:[...new Set(videos)],
    type:root.querySelector('video')?'video':'normal',source:'public_web_dom',source_url:location.href}:null};
}'''


def check_snapshot(snap, barriers):
    if barriers or snap.get('blocked'):
        raise AccessRestricted('xhs_web_verification_or_access_restricted')
    if snap.get('loggedOut'):
        raise LoginRequired('xhs_web_login_required')


def card_payload(card):
    return {'id':card['note_id'],'xsec_token':'','note_card':{
        'note_id':card['note_id'],'type':card['type'],'title':card.get('title',''),
        'user':card.get('user') or {},'source_url':card['source_url'],
        'cover':{'url_default':card.get('cover_url','')},'source':'public_web_dom'}}


class XhsWebJob(Job):
    def progress(self,status,**fields):
        fields.setdefault('collection_mode','web_dom')
        if status=='blocked' and fields.get('error_type')=='WebMarkupChanged':
            fields['requires_user_action']=True
        return super().progress(status,**fields)

    async def setup(self):
        # Existing setup owns one CDP page and configures paced media transfers.
        # The API client is never called by the web adapter.
        self.web_barriers=[]
        self.web_note_id=None
        self.web_comment_step=0
        self.web_link_lookups=set()
        def observe(response):
            url=urlsplit(response.url)
            if url.hostname=='edith.xiaohongshu.com' and url.path.startswith('/api/sns/web/') and response.status in (401,403,406,412,429,461,471):
                self.web_barriers.append({'path':url.path,'status':response.status})
        self.page.response_observer=observe
        await super().setup()
        await self.snapshot()
        self.progress('session_checked',session_available=True,collection_mode='web_dom')

    async def snapshot(self):
        snap=await self.page.evaluate(SNAPSHOT)
        check_snapshot(snap,self.web_barriers)
        return snap

    async def refresh_session(self):
        await super().refresh_session()
        # Only called after the existing SMS helper confirms a real login.
        self.web_barriers.clear()

    async def visit(self,url,label):
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname!='www.xiaohongshu.com' or parsed.port not in (None,443):
            raise WebMarkupChanged('xhs_web_invalid_public_url')
        async def action():
            response=await self.page.page.goto(url,wait_until='domcontentloaded',timeout=30000)
            if response and response.status in (401,403,406,412,429,461,471):
                raise AccessRestricted('xhs_web_document_restricted')
            await self.page.page.wait_for_timeout(2000)
            deadline=time.monotonic()+12
            while True:
                snap=await self.snapshot()
                if parsed.path.rstrip('/')=='/search_result':
                    if snap['path']!='/search_result':raise WebMarkupChanged('xhs_web_search_redirect_changed')
                    ready=bool(snap['cards']) or snap.get('emptySearch')
                elif re.match(r'^/(?:explore|search_result|discovery/item)/',parsed.path):
                    ready=snap.get('root') or snap.get('unavailable')
                else:ready=True
                if ready:return snap
                if time.monotonic()>=deadline:raise WebMarkupChanged('xhs_web_render_timeout')
                await self.page.page.wait_for_timeout(250)
        value=await self.gate.action(label,action)
        self.web_note_id=None
        self.web_comment_step=0
        return value

    async def scroll_listing(self,label):
        async def action():
            await self.page.page.evaluate('() => window.scrollTo(0,document.documentElement.scrollHeight)')
            await self.page.page.wait_for_timeout(2000)
            return await self.snapshot()
        return await self.gate.action(label,action)

    async def raw_search(self,kw,page):
        snap=await self.visit('https://www.xiaohongshu.com/search_result?keyword='+quote(kw)+'&source=web_explore_feed','xhs_web_search')
        cards={c['note_id']:c for c in snap['cards']}
        for _ in range(max(0,int(page)-1)):
            snap=await self.scroll_listing('xhs_web_search_scroll')
            before=len(cards)
            cards.update((c['note_id'],c) for c in snap['cards'])
            if len(cards)==before:break
        if not cards and not snap.get('emptySearch'):
            raise WebMarkupChanged('xhs_web_search_markup_changed')
        return {'items':[card_payload(c) for c in cards.values()]}

    def note_url(self,item):
        raw=item.get('raw') or {}
        source=raw.get('_web_source_url') or raw.get('source_url') or item.get('url') or 'https://www.xiaohongshu.com/explore/'+item['id']
        if raw.get('xsec_token') and not urlsplit(source).query:
            source+='?xsec_token='+quote(raw['xsec_token'])+'&xsec_source='+quote(raw.get('xsec_source') or 'pc_search')
        return source

    async def open_note(self,item):
        if self.web_note_id==item['id']:
            return await self.snapshot()
        link=await self.store.state('web_link:'+item['type']+':'+item['id']) if self.store else None
        snap=await self.visit((link or {}).get('url') or self.note_url(item),'xhs_web_note')
        if snap.get('unavailable'):
            # A saved public link may be stale. A normal visible title search can
            # provide a current public link; this is never used after a barrier.
            if item['id'] in self.web_link_lookups or not item.get('title'):
                raise CommentUnavailable('xhs_web_note_not_visible_at_saved_link')
            self.web_link_lookups.add(item['id'])
            found=await self.visit('https://www.xiaohongshu.com/search_result?keyword='+quote(item['title'][:80])+'&source=web_explore_feed','xhs_web_locate_saved_note')
            if not found['cards'] and not found.get('emptySearch'):
                raise WebMarkupChanged('xhs_web_search_markup_changed')
            card=next((c for c in found['cards'] if c['note_id']==item['id']),None)
            if not card:raise CommentUnavailable('xhs_web_note_not_visible_at_saved_link')
            snap=await self.visit(card['source_url'],'xhs_web_observed_note_link')
            if snap.get('unavailable'):raise CommentUnavailable('xhs_web_note_not_visible_at_saved_link')
            if self.store:
                await self.store.state('web_link:'+item['type']+':'+item['id'],{'url':card['source_url'],'source':'observed_public_search_result'})
        if not snap.get('root') or not snap.get('note') or snap['note']['note_id']!=item['id']:
            raise WebMarkupChanged('xhs_web_note_markup_changed')
        self.web_note_id=item['id']
        return snap

    async def detail(self,item):
        cached=await self.store.cached(item['type'],item['id'])
        if cached:return cached
        snap=await self.open_note(item)
        raw=dict(snap['note'])
        raw['note_id']=item['id']
        raw['_web_media_gaps']=['video_not_exposed_as_public_https'] if raw['type']=='video' and not any(u.startswith('https://') for u in raw.get('videos',[])) else []
        result=normalized('xhs',raw)
        result['type']=item['type']  # Keep the search/detail/checkpoint identity stable.
        result['url']=raw['source_url']
        return result

    async def history(self,aid):
        snap=await self.visit('https://www.xiaohongshu.com/user/profile/'+quote(aid),'xhs_web_author')
        cards={c['note_id']:c for c in snap['cards']}
        for _ in range(3):
            if len(cards)>=10:break
            snap=await self.scroll_listing('xhs_web_author_scroll')
            before=len(cards);cards.update((c['note_id'],c) for c in snap['cards'])
            if len(cards)==before:break
        # Visible default ordering is recorded; never assert unobserved timestamps.
        return [normalized('xhs',card_payload(c)['note_card']) for c in list(cards.values())[:10]]

    async def author_phase(self):
        await super().author_phase()
        rows=await self.store.pool.fetch("SELECT task_key,state FROM acquisition_progress WHERE run_id=$1 AND platform='xhs' AND task_key LIKE 'author:%'",self.args.run_id)
        for row in rows:
            state=json.loads(row['state'])
            if state.get('successful') and not state.get('error_type'):
                state.update(status='partial_available',ordering='web_default_public_order; latest_publication_order_unverified',recency_verified=False)
                await self.store.state(row['task_key'],state)

    async def media_items(self,item):
        raw=item.get('raw') or {}
        if raw.get('source')!='public_web_dom':
            return await super().media_items(item)
        images=[v.get('url_default') for v in raw.get('image_list') or [] if v.get('url_default','').startswith('https://')]
        resources=[MediaItem(url=url,media_type=MediaType.IMAGE,content_id=item['id'],stem=f'image_{i}') for i,url in enumerate(dict.fromkeys(images))]
        resources += [MediaItem(url=url,media_type=MediaType.VIDEO,content_id=item['id'],stem=f'video_{i}') for i,url in enumerate(raw.get('videos') or []) if url.startswith('https://')]
        return resources

    async def web_comment_page(self,item,stream):
        snap=await self.open_note(item)
        target=int(stream.get('cursor') or 0)
        while self.web_comment_step<target:
            async def advance():
                current=await self.snapshot()
                if current['expandCount']:
                    # Expansion only; never click the compose/reply/like controls.
                    await self.page.page.locator('[data-mc-xhs-expand="0"]').click(timeout=10000)
                else:
                    await self.page.page.evaluate("() => {const e=document.querySelector('.note-container .note-scroller');if(e)e.scrollTop=e.scrollHeight}")
                await self.page.page.wait_for_timeout(1800)
                return await self.snapshot()
            snap=await self.gate.action('xhs_web_comments_expand_or_scroll',advance)
            self.web_comment_step+=1
        raw_rows=snap.get('comments') or []
        if any(not r.get('id') for r in raw_rows):raise CommentParseError('xhs_web_comment_id_missing')
        rows=[]
        for raw in raw_rows:
            row=normalize('xhs',raw,raw.get('parent_id'))
            row['body']=raw.get('content','')  # Already visible text; do not parse literal markup twice.
            rows.append(row)
        signature=hashlib.sha256(json.dumps(sorted(r['id'] for r in rows)).encode()).hexdigest()
        stalls=int(stream.get('stalls') or 0)+1 if signature==stream.get('signature') else 0
        complete=(snap['noComments'] and not rows) or (not snap['expandCount'] and
                    (snap['end'] or (snap['total'] is not None and len(rows)>=snap['total'])))
        if complete:return Page(rows,None)
        if stalls>=2:
            return Page(rows,None,gaps=[{'reason':'xhs_web_visible_comments_stalled','visible':len(rows),'displayed_total':snap['total']}])
        return Page(rows,{'cursor':target+1,'signature':signature,'stalls':stalls})
