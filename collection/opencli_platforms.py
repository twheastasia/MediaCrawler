"""Content collection using OpenCLI. Unsupported capabilities remain explicit gaps."""
import asyncio
import json
import re
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from collection.opencli_adapter import OpenCliAdapter, AdapterChanged
from collection.runtime import GatedTransport, AccessRestricted
from collection.comment_store import save_page
from media_downloader import MediaDownloader, MediaItem, MediaType
from collection.settings import KEYWORDS, SEARCH_PER_KEYWORD, HISTORY_PER_AUTHOR
from collection.worker import Job


def post_item(raw):
    url = raw.get('url') or ''
    u = urlsplit(url)
    if u.scheme != 'https' or u.hostname not in ('www.linkedin.com', 'linkedin.com'):
        raise AdapterChanged('linkedin_post_url_missing')
    m = re.search(r'urn:li:(?:activity|ugcPost):(\d+)|activity-(\d+)', u.path)
    content_id = next((x for x in m.groups() if x), None) if m else None
    if not content_id:
        # /pulse/... and post slugs have stable public URLs. Never fabricate a
        # numeric LinkedIn activity ID from text or an anonymous hash.
        if not u.path.startswith(('/posts/', '/pulse/')):
            raise AdapterChanged('linkedin_post_identity_missing')
        content_id = u.path.rstrip('/')
    au = raw.get('author_url') or ''
    a = urlsplit(au)
    author = a.path.strip('/') if a.hostname in ('www.linkedin.com', 'linkedin.com') and a.path.startswith(('/in/', '/company/')) else None
    return dict(type='post', id=content_id, author_id=author, title='',
                body=raw.get('raw_body') or raw.get('body') or raw.get('text') or '',
                url=url, time=None, raw=raw)


DETAIL = r'''() => {
 const card=document.querySelector('article,[role="article"],.feed-shared-update-v2,.occludable-update');
 if(!card)return {found:false};
 const body=card.querySelector('.feed-shared-update-v2__description,.update-components-text,.feed-shared-text,[class*="update-components-text"]');
 const author=card.querySelector('a[href*="/in/"],a[href*="/company/"]');
 const expand=[...card.querySelectorAll('button')].find(b=>/^(?:…?\s*(?:see more|show more)|查看更多|展开)$/i.test((b.innerText||'').trim()));
 if(expand)expand.setAttribute('data-mc-post-expand','1');
 return {found:true,raw_body:body?.innerText||'',raw_rendered_text:card.innerText,
 author_url:author?.href||'',url:location.href,expandable:!!expand,
 media_urls:[...card.querySelectorAll('img[src],video')].map(e=>e.currentSrc||e.src||'').filter(x=>/^https:\/\//.test(x))};
}'''

COMMENTS = r'''() => {
 const selector='.comments-comment-entity,.comments-comment-item';
 const nodes=[...document.querySelectorAll(selector)];
 const identity=n=>{const candidates=[n?.getAttribute('data-urn'),n?.getAttribute('data-id'),n?.id];
   return candidates.find(x=>/^urn:li:(?:fsd_)?comment:/.test(x||''))||'';};
 const rows=[];let missing=0;
 for(const n of nodes){
   const id=identity(n);if(!id){missing++;continue;}
   const content=n.querySelector('.comments-comment-item__main-content,.comments-comment-entity__main-content,[class*="main-content"]');
   if(!content){missing++;continue;}
   const author=n.querySelector('a[href*="/in/"],a[href*="/company/"]');
   const a=author?new URL(author.href,location.href):null;
   const uid=a&&/^(www\.)?linkedin\.com$/.test(a.hostname)?a.pathname.replace(/^\/+|\/+$/g,''):null;
   const parent=identity(n.parentElement?.closest(selector));
   rows.push({id,parent_id:parent||null,author_id:uid,body:content.innerText,
     raw:{comment_id:id,author_id:uid,body:content.innerText,raw_rendered_text:n.innerText,author_url:author?.href||''}});
 }
 const visible=e=>e.getClientRects().length>0;
 const controls=[...document.querySelectorAll('button')].filter(visible);
 const more=controls.find(b=>/^(?:load more comments|load more replies|view (?:\d+ )?more (?:comments|replies)|show (?:\d+ )?more (?:comments|replies)|查看更多评论|加载更多评论|查看更多回复|查看\s*\d+\s*条回复)$/i.test((b.innerText||'').trim()));
 const open=!nodes.length?document.querySelector('button.social-details-social-counts__comments'):null;
 document.querySelectorAll('[data-mc-comment-next]').forEach(e=>e.removeAttribute('data-mc-comment-next'));
 if(more||open)(more||open).setAttribute('data-mc-comment-next','1');
 return {rows,missing,visible_count:nodes.length,has_more:!!(more||open),
 explicit_empty:/no comments yet|暂无评论/i.test(document.body.innerText)};
}'''


class OpenCLIJob(Job):
    def progress(self, status, **fields):
        if status == 'blocked':
            fields['requires_user_action'] = True
            fields['automatic_retry'] = False
            fields['reason'] = getattr(self, 'blocked_reason', None) or fields.get('reason')
        super().progress(status, **fields)

    async def setup(self):
        if not self.args.allow_session_memory:
            from collection.runtime import LoginRequired
            raise LoginRequired('session_memory_authorization_required')
        self.adapter = OpenCliAdapter(self.platform, self.page)
        self.page.response_observer = self.adapter.response
        await self.page.connect('about:blank')
        target = 'https://maimai.cn/community' if self.platform == 'maimai' else 'https://www.linkedin.com/feed/'
        try:
            await self.adapter.navigate(target)
        except Exception:
            path = urlsplit(self.page.page.url).path
            self.blocked_reason = ('linkedin_phone_setup_required' if self.platform == 'linkedin' and path.startswith('/check/china/')
                                   else self.platform + '_login_or_access_required')
            raise
        self.progress('connected', collection_mode='opencli_cdp', content_scope='posts_articles')

    async def sms_login(self, open_login=False):
        # Existing SMS brand/identity verification covers the original eight
        # sites. New sites need a verified identity matcher before sending.
        return False

    async def run(self):
        try:
            await super().run()
        finally:
            if getattr(self, 'adapter', None):
                await self.adapter.close()

    async def detail(self, item):
        await self.adapter.navigate(item['url'])
        data = await self.page.page.evaluate(DETAIL)
        if not data.get('found'):
            raise AdapterChanged('linkedin_detail_markup_changed')
        if data.get('expandable'):
            await self.gate.action('linkedin_expand_post', lambda: self.page.page.locator('[data-mc-post-expand="1"]').click(timeout=5000))
            await asyncio.sleep(2)
            await self.adapter.guard()
            data = await self.page.page.evaluate(DETAIL)
        raw = {**item['raw'], **{k:v for k,v in data.items() if k != 'url'}, 'source_url': item['url']}
        full = post_item({**raw, 'url':item['url']})
        full['raw']['body_completeness'] = 'partial' if data.get('expandable') or not data.get('raw_body') else 'public_visible'
        return full

    async def collect_phases(self):
        if self.platform == 'maimai':
            # OpenCLI 1.8.8 only has search-talents. Do not silently substitute
            # recruiting records for the user's requested posts/articles.
            self.blocked_reason = 'maimai_opencli_post_adapter_unavailable'
            await self.store.state('capability:posts', {'status':'unavailable', 'reason':self.blocked_reason,
                'requested':['keyword_posts','author_history','comments_replies','attachments'],
                'upstream_commands':['search-talents'], 'talent_search_used':False})
            raise AdapterChanged(self.blocked_reason)
        keywords = KEYWORDS[:1] if self.args.smoke else KEYWORDS
        target = 1 if self.args.smoke else SEARCH_PER_KEYWORD
        for keyword in keywords:
            old = await self.store.links(keyword)
            if len(old) >= target: continue
            self.progress('searching', keyword=keyword, successful=len(old), target=target)
            rows = await self.adapter.invoke('search-posts', {'query':keyword, 'limit':target})
            if not rows:
                raise AdapterChanged('linkedin_no_verified_post_search_results')
            for raw in rows:
                item = post_item(raw)
                item = await self.detail(item)
                await self.store.content(item)
                if item['raw']['body_completeness'] != 'public_visible':
                    await self.store.state('detail:'+item['id'], {'status':'partial', 'keyword':keyword,
                        'reason':'visible_post_body_incomplete','counts_toward_search_quota':False})
                    continue
                await self.store.link(keyword, 'search', item)
                if item['author_id']:
                    await self.store.author(item['author_id'], raw.get('author'), raw.get('author_url'),
                        {'source_url':raw.get('author_url'), 'name':raw.get('author')})
                await self.collect_extras(item)
            count = len(await self.store.links(keyword))
            await self.store.state('search:'+keyword, {'status':'complete' if count>=target else 'partial',
                'successful':count,'target':target,'scope':'public_visible_posts'})
        for entry in await self.store.authors():
            author = entry['author_id']
            if not author.startswith('in/'):
                await self.store.state('author:'+author, {'status':'unavailable','reason':'company_history_not_supported'})
                continue
            if await self.store.state('author:'+author): continue
            rows = await self.adapter.invoke('posts', {'profile-url':'https://www.linkedin.com/'+author+'/',
                'limit':HISTORY_PER_AUTHOR})
            for raw in rows:
                item = post_item({**raw,'author_url':'https://www.linkedin.com/'+author+'/'})
                item = await self.detail(item)
                await self.store.content(item)
                await self.store.link(entry['keyword'],'history',item)
                await self.collect_extras(item)
            await self.store.state('author:'+author, {'status':'partial','count':len(rows),
                'recency_verified':False,'reason':'web_default_order_not_verified'})
        self.progress('incomplete', counts=await self.store.counts(), requires_user_action=True,
            reason='new_adapter_requires_live_comments_recency_validation', automatic_retry=False)
        await self.store.state('worker', {'status':'incomplete','requires_user_action':True})

    async def collect_extras(self, item):
        key='comments:post:'+item['id']
        prior=await self.store.state(key) or {}
        if prior.get('status') != 'public_visible_complete':
            last=None;stalls=0
            for step in range(150):
                await self.adapter.guard()
                data=await self.page.page.evaluate(COMMENTS)
                signature=tuple(r['id'] for r in data['rows'])
                stalls=stalls+1 if signature==last else 0;last=signature
                status=('partial' if data['missing'] else 'public_visible_complete') if not data['has_more'] else 'running'
                if not data['rows'] and not data['explicit_empty'] and not data['has_more']:status='not_exposed'
                if stalls>=2 or step==149:status='partial'
                state={'status':status,'visible_comments':len(data['rows']),'missing_stable_id_or_body':data['missing'],
                    'scope':'public_web_rendered_comments_and_replies','backend_all_comments_verified':False,'step':step}
                await save_page(self.store,item,data['rows'],key,state)
                if status!='running':break
                await self.gate.action('linkedin_expand_comments',lambda:self.page.page.locator('[data-mc-comment-next="1"]').click(timeout=5000))
                await asyncio.sleep(2)
                await self.adapter.guard()
        urls=item['raw'].get('media_urls') or []
        if isinstance(urls,str):urls=[u.strip() for u in urls.split('|') if u.strip()]
        resources=[MediaItem(url=u,media_type=MediaType.VIDEO if re.search(r'\.(mp4|webm)(?:\?|$)',u,re.I) else MediaType.IMAGE,content_id=hashlib.sha256(item['id'].encode()).hexdigest(),
            stem=f'{i:03}') for i,u in enumerate(dict.fromkeys(urls)) if urlsplit(u).scheme=='https']
        downloader=MediaDownloader(self.platform,base_dir=Path(self.args.output)/'media',max_retries=0,max_candidates=1)
        async with httpx.AsyncClient(transport=GatedTransport(self.gate),trust_env=False,follow_redirects=True,
                                    event_hooks={'response':[self.media_response]}) as client:
            for resource in resources:
                try:
                    path=await downloader.download(resource,client=client)
                    await self.store.media(item,resource,path=path,error=None if path else 'media_download_failed')
                except AccessRestricted:raise
                except Exception as e:await self.store.media(item,resource,error=type(e).__name__)
        await self.store.state('media:post:'+item['id'],{'status':'public_urls_processed','resources':len(resources),
            'all_attachments_verified':False,'reason':'blob_video_and_dynamic_media_not_verified'})

    async def media_response(self,response):
        if response.status_code in (401,403,406,412,429,461,471,999):
            await response.aclose()
            raise AccessRestricted('linkedin_media_access_restricted')
