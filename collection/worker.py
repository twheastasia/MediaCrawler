from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import re
import time
import traceback
import inspect
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import config
from parsel import Selector
from var import source_keyword_var
from collection.database import Store
from collection.settings import KEYWORDS,PAGES,DOMAINS,SEARCH_PER_KEYWORD,HISTORY_PER_AUTHOR,SEARCH_PAGE_BUDGET
from collection.runtime import RequestGate, GatedTransport, AccessRestricted, BrowserTransport, ChromeSessionTransport, LoginRequired
from collection.chrome import ChromePage
from media_platform.bilibili.client import BilibiliClient
from media_platform.douyin.client import DouYinClient
from media_platform.zhihu.client import ZhiHuClient
from media_platform.xhs.client import XiaoHongShuClient
from media_platform.bilibili import media as bili_media
from media_platform.douyin import media as dy_media
from media_platform.xhs import media as xhs_media
from media_downloader import MediaDownloader, MediaItem, MediaType, is_ffmpeg_available

def text_of(value):
    return ''.join(Selector(text=str(value or '')).xpath('//text()').getall())


def stamp(value):
    try:
        return int(value) or None
    except (TypeError, ValueError):
        return None


def normalized(platform, raw, kind=None):
    if platform == 'bili':
        v = raw.get('View') or raw
        owner = v.get('owner') or {}
        uid = owner.get('mid') or v.get('mid')
        cid = str(v.get('bvid') or '')
        return dict(type='video',id=cid,author_id=str(uid) if uid else None,
            title=text_of(v.get('title')),body=v.get('desc',''),time=stamp(v.get('pubdate') or v.get('created')),
            url='https://www.bilibili.com/video/'+cid,raw=raw)
    if platform == 'dy':
        a=raw.get('author') or {};cid=str(raw.get('aweme_id') or '')
        uid=a.get('sec_uid') or a.get('uid')
        return dict(type='video',id=cid,author_id=str(uid) if uid else None,
            title=raw.get('desc',''),body=raw.get('desc',''),time=stamp(raw.get('create_time')),
            url='https://www.douyin.com/video/'+cid,raw=raw)
    if platform == 'xhs':
        user=raw.get('user') or {};cid=str(raw.get('note_id') or raw.get('id') or '')
        return dict(type=raw.get('type') or 'normal',id=cid,author_id=user.get('user_id'),
            title=raw.get('title') or raw.get('display_title') or '',body=raw.get('desc') or '',
            time=stamp(raw.get('time')),url='https://www.xiaohongshu.com/explore/'+cid,raw=raw)
    kind=kind or raw.get('type');cid=str(raw.get('id') or raw.get('content_id') or '')
    a=raw.get('author') or {};uid=a.get('url_token') or a.get('id')
    qid=str((raw.get('question') or {}).get('id') or raw.get('question_id') or '')
    url=raw.get('content_url') or (f'https://www.zhihu.com/question/{qid}/answer/{cid}' if kind=='answer'
           else 'https://zhuanlan.zhihu.com/p/'+cid if kind=='article' else 'https://www.zhihu.com/zvideo/'+cid)
    return dict(type=kind,id=cid,author_id=str(uid) if uid else None,
        title=text_of(raw.get('title') or (raw.get('question') or {}).get('title')),
        body=text_of(raw.get('content') or raw.get('content_text') or raw.get('excerpt')),
        time=stamp(raw.get('created_time') or raw.get('created') or raw.get('published_at')),
        url=url,raw=raw)


def candidates(platform, data):
    result=[]
    if platform=='bili':
        result=[normalized(platform,x) for x in (data.get('result') or []) if x.get('bvid')]
    elif platform=='dy':
        for entry in data.get('data') or []:
            values=[entry.get('aweme_info')] if entry.get('aweme_info') else (entry.get('aweme_mix_info') or {}).get('mix_items',[])
            result.extend(normalized(platform,x) for x in values if isinstance(x,dict) and x.get('aweme_id'))
    elif platform=='xhs':
        for entry in data.get('items') or data.get('notes') or []:
            card=entry.get('note_card') or entry
            cid=entry.get('id') or entry.get('note_id')
            if cid and card.get('user'):
                raw={**card,'note_id':cid,'xsec_token':entry.get('xsec_token',''),
                     'xsec_source':entry.get('xsec_source') or 'pc_search'}
                result.append(normalized('xhs',raw))
    else:
        for row in data.get('data') or []:
            obj=row.get('object') or {}
            if obj.get('type') in ('answer','article','zvideo'):
                result.append(normalized(platform,obj))
    return list({(x['type'],x['id']):x for x in result if x['id']}.values())


class Job:
    def __init__(self,args):
        self.args=args;self.platform=args.platform
        self.directory=Path(args.output).resolve()/self.platform
        self.directory.mkdir(parents=True,exist_ok=True)
        self.gate=RequestGate(self.directory/'requests.jsonl')
        self.page=ChromePage(self.gate)
        self.store=None;self.client=None;self.last_response=None;self.current_operation=''
        self.lock_connection=None
        self.cookies={}
        self.headers={}
        self.downloader=None
        self.blocked_paths=set()

    def progress(self,status,**fields):
        value=dict(platform=self.platform,pid=os.getpid(),browser='chrome_cdp',status=status,updated_at=time.time(),**fields)
        tmp=self.directory/'status.tmp';tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2));tmp.replace(self.directory/'status.json')
        print(json.dumps(value,ensure_ascii=False),flush=True)

    async def response(self,response):
        # Client API responses are read in memory; never persist request headers or session data.
        await response.aread()
        try:self.last_response=response.json()
        except ValueError:self.last_response=None
        if response.status_code in (401,403,406,412,429,461,471):
            self.blocked_paths.add((response.url.host,response.url.path))
            raise AccessRestricted(f'HTTP {response.status_code}')
        if isinstance(self.last_response,dict) and str(self.last_response.get('code')) in ('-352','-412','300011','300012'):
            self.blocked_paths.add((response.url.host,response.url.path))
            raise AccessRestricted('business_code_'+str(self.last_response['code']))
        payload=self.last_response.get('data') if isinstance(self.last_response,dict) else None
        if isinstance(payload,dict) and payload.get('v_voucher'):
            self.blocked_paths.add((response.url.host,response.url.path))
            raise AccessRestricted('verification_required')

    async def setup(self):
        if not self.args.allow_session_memory:
            raise RuntimeError('explicit in-memory session authorization is required')
        await self.page.connect(DOMAINS[self.platform])
        self.cookies=await self.page.credentials([DOMAINS[self.platform]])
        ua=await self.page.evaluate('() => navigator.userAgent')
        cookie='; '.join(f'{k}={v}' for k,v in self.cookies.items())
        self.headers={'User-Agent':ua,'Referer':DOMAINS[self.platform]+'/','Cookie':cookie}
        if self.platform=='dy':self.headers['Origin']=DOMAINS[self.platform]
        if self.platform=='xhs':self.headers['Content-Type']='application/json;charset=UTF-8'
        # The original stores use numeric API values for legacy text columns.
        # Coerce only those ORM fields in this collection process; raw JSON stays intact.
        from sqlalchemy import event, String
        from sqlalchemy.orm import Session
        from database.models import Base
        def legacy_text_fields(session, flush_context, instances):
            for obj in session.new | session.dirty:
                if isinstance(obj,Base):
                    for column in obj.__table__.columns:
                        value=getattr(obj,column.key,None)
                        if isinstance(column.type,String) and isinstance(value,(int,float,bool)):
                            setattr(obj,column.key,str(value))
        event.listen(Session,'before_flush',legacy_text_fields)
        self.client={
            'bili':lambda:BilibiliClient(headers=self.headers,playwright_page=self.page,cookie_dict=self.cookies,timeout=30),
            'dy':lambda:DouYinClient(headers=self.headers,playwright_page=self.page,cookie_dict=self.cookies,timeout=30),
            'xhs':lambda:XiaoHongShuClient(headers=self.headers,playwright_page=self.page,cookie_dict=self.cookies,timeout=30),
            'zhihu':lambda:ZhiHuClient(headers={**{k.lower():v for k,v in self.headers.items()},'cookie':cookie,'user-agent':ua,
                'x-api-version':'3.0.91','x-app-za':'OS=Web','x-requested-with':'fetch','x-zse-93':'101_3_3.0'},
                playwright_page=self.page,cookie_dict=self.cookies,timeout=30),
        }[self.platform]()
        if self.platform in ('zhihu','xhs'):
            # Let task recovery own retries; remove the client's automatic retry decorator.
            from types import MethodType
            self.client.request=MethodType(inspect.unwrap(type(self.client).request),self.client)
        # Patch factories only inside this worker process, leaving existing crawler flows untouched.
        def factory(**kwargs):
            kwargs.pop('proxy',None)
            kwargs['transport']=GatedTransport(self.gate,blocked_paths=self.blocked_paths)
            kwargs['trust_env']=False
            return httpx.AsyncClient(**kwargs)
        import media_platform.bilibili.client as bc
        import media_platform.douyin.client as dc
        import media_platform.zhihu.client as zc
        import media_platform.xhs.client as xc
        import media_downloader.downloader as md
        for module in (bc,dc,zc,xc):
            def api_factory(**kwargs):
                kwargs['event_hooks']={'response':[self.response]}
                if self.platform in PAGES:
                    kwargs.pop('proxy',None)
                    transport=ChromeSessionTransport if self.platform=='dy' else BrowserTransport
                    kwargs['transport']=transport(self.page,self.gate,self.blocked_paths)
                    kwargs['trust_env']=False
                    return httpx.AsyncClient(**kwargs)
                return factory(**kwargs)
            module.make_async_client=api_factory
        md.make_async_client=factory
        self.downloader=MediaDownloader(self.platform,extra_headers={'User-Agent':ua,'Referer':DOMAINS[self.platform]+'/'},
            base_dir=Path(self.args.output).resolve(),max_retries=0,max_candidates=1,timeout=120)
        login_markers={'bili':['SESSDATA'],'dy':['sessionid','sessionid_ss','sid_guard'],'zhihu':['z_c0'],'xhs':['web_session']}
        logged_in=any(self.cookies.get(x) for x in login_markers[self.platform])
        self.progress('session_checked',session_available=logged_in)
        if not logged_in:raise LoginRequired('login session missing; user login required')

    async def raw_search(self,kw,page):
        if self.platform=='bili':return await self.client.search_video_by_keyword(kw,page=page,page_size=20)
        if self.platform=='dy':return await self.client.search_info_by_keyword(kw,offset=(page-1)*15)
        if self.platform=='xhs':return await self.client.get_note_by_keyword(kw,page=page,page_size=20)
        return await self.client.get('/api/v4/search_v3',dict(gk_version='gz-gaokao',t='general',q=kw,
            correction=1,offset=(page-1)*20,limit=20,filter_fields='',lc_idx=(page-1)*20,
            show_all_topics=0,search_source='Filter',time_interval='',sort='',vertical=''))

    async def detail(self,item):
        old=await self.store.cached(item['type'],item['id'])
        if old:return old
        if self.platform=='bili':
            raw=await self.client.get_video_info(bvid=item['id'])
            if not raw.get('View'):raise ValueError('video detail missing')
            return normalized(self.platform,raw)
        if self.platform=='dy':
            raw=await self.client.get_video_by_id(item['id'])
            if not raw.get('aweme_id'):raise ValueError('video detail missing')
            return normalized(self.platform,raw)
        if self.platform=='xhs':
            context=item['raw']
            raw=await self.client.get_note_by_id(item['id'],context.get('xsec_source','pc_search'),context.get('xsec_token',''))
            if not raw:raise ValueError('note detail missing')
            raw['note_id']=item['id']
            return normalized('xhs',raw)
        # Reuse MediaCrawler's HTML detail extraction, retaining raw author identity from search.
        raw=item['raw'];kind=item['type']
        if kind=='answer':
            qid=str((raw.get('question') or {}).get('id') or raw.get('question_id') or '')
            detail=await self.client.get_answer_info(qid,item['id'])
        elif kind=='article':detail=await self.client.get_article_info(item['id'])
        else:detail=await self.client.get_video_info(item['id'])
        if not detail or not detail.content_id:raise ValueError('content detail missing')
        data=detail.model_dump();data['author']=raw.get('author') or {}
        value=normalized('zhihu',data,kind)
        for key in ('title','body','time'):
            if not value.get(key):value[key]=item.get(key)
        value['raw']={'search_or_list_object':raw,'detail':detail.model_dump()}
        return value

    async def persist(self,item):
        await self.store.content(item)
        name=re.sub(r'[^A-Za-z0-9_-]','_',item['type']+'_'+item['id'])
        (self.directory/(name+'.json')).write_text(json.dumps(item,ensure_ascii=False,indent=2,default=str))
        # Preserve compatibility with existing platform content tables, alongside complete raw records.
        if self.platform=='bili':
            from store.bilibili import update_bilibili_video
            await update_bilibili_video(item['raw'])
        elif self.platform=='dy':
            from store.douyin import update_douyin_aweme
            await update_douyin_aweme(item['raw'])
        elif self.platform=='xhs':
            from store.xhs import update_xhs_note
            await update_xhs_note(item['raw'])
        else:
            from model.m_zhihu import ZhihuContent
            from store.zhihu import batch_update_zhihu_contents
            d=(item['raw'].get('detail') or {})
            if d:
                compatible=dict(d)
                original_author=(item['raw'].get('search_or_list_object') or {}).get('author') or {}
                if original_author.get('name') is not None:
                    compatible['user_nickname']=original_author['name']
                for key in ('created_time','updated_time','voteup_count','comment_count'):
                    if compatible.get(key) is None:compatible[key]=0
                await batch_update_zhihu_contents([ZhihuContent(**compatible)])
        if item.get('author_id'):
            await self.save_author_from_content(item)

    async def save_author_from_content(self,item):
        aid=item['author_id'];raw=item['raw']
        if self.platform=='bili':
            a=(raw.get('Card') or {}).get('card') or (raw.get('View') or {}).get('owner') or {}
            home='https://space.bilibili.com/'+aid
        elif self.platform=='dy':
            a=raw.get('author') or {};home='https://www.douyin.com/user/'+aid
        elif self.platform=='xhs':
            a=raw.get('user') or {};home='https://www.xiaohongshu.com/user/profile/'+aid
        else:
            a=(raw.get('search_or_list_object') or raw).get('author') or {};home='https://www.zhihu.com/people/'+aid
        await self.store.author(aid,a.get('name') or a.get('nickname'),home,a)

    async def search_phase(self):
        # Cover all keywords first, then rotate one page per keyword until quota.
        for round_target in (min(5,SEARCH_PER_KEYWORD),SEARCH_PER_KEYWORD):
            while True:
                pending=False
                for kw in KEYWORDS:
                    source_keyword_var.set(kw)
                    links=await self.store.links(kw)
                    seen={(r['content_type'],r['content_id']) for r in links}
                    state=await self.store.state('search:'+kw) or {}
                    if state.get('target')!=SEARCH_PER_KEYWORD:
                        state={'page':1,'target':SEARCH_PER_KEYWORD}
                    if len(seen)>=round_target or state.get('done'):continue
                    pending=True
                    page=state.get('page',1)
                    self.progress('searching',keyword=kw,successful=len(seen),target=SEARCH_PER_KEYWORD,page=page)
                    data=await self.raw_search(kw,page)
                    items=candidates(self.platform,data)
                    before=len(seen)
                    partial_page=False
                    for candidate in items:
                        if len(seen)>=round_target:
                            partial_page=True;break
                        key=(candidate['type'],candidate['id'])
                        if key in seen:continue
                        try:item=await self.detail(candidate)
                        except ValueError as e:
                            await self.store.state('failed_detail:'+candidate['id'],{'error':str(e)});continue
                        await self.persist(item)
                        await self.store.link(kw,'search',item)
                        seen.add(key)
                        self.progress('searching',keyword=kw,successful=len(seen),target=SEARCH_PER_KEYWORD)
                    stalled=state.get('stalled_pages',0)+1 if len(seen)==before else 0
                    exhausted=not items or stalled>=3 or page>=SEARCH_PAGE_BUDGET
                    reached=len(seen)>=SEARCH_PER_KEYWORD
                    await self.store.state('search:'+kw,{
                        'page':page if partial_page else page+1,'successful':len(seen),
                        'target':SEARCH_PER_KEYWORD,'stalled_pages':stalled,'done':reached or exhausted,
                        'status':'complete' if reached else 'incomplete' if exhausted else 'running',
                        'reason':'quota_reached' if reached else 'results_exhausted_or_page_budget' if exhausted else 'in_progress'})
                if not pending:break

    async def history(self,aid):
        if self.platform=='xhs':
            profile=await self.client.get_creator_info(aid)
            if profile:await self.store.author(aid,(profile.get('basic_info') or {}).get('nickname'),
                'https://www.xiaohongshu.com/user/profile/'+aid,profile)
            data=await self.client.get_notes_by_creator(aid,cursor='',page_size=30)
            return candidates('xhs',data)[:HISTORY_PER_AUTHOR]
        if self.platform=='bili':
            data=await self.client.get_creator_videos(aid,pn=1,ps=30,order_mode='pubdate')
            return [normalized('bili',x) for x in ((data.get('list') or {}).get('vlist') or []) if x.get('bvid')][:HISTORY_PER_AUTHOR]
        if self.platform=='dy':
            data=await self.client.get_user_info(aid)
            user=data.get('user') or {}
            if user:await self.store.author(aid,user.get('nickname'),'https://www.douyin.com/user/'+aid,user)
            data=await self.client.get_user_aweme_posts(aid)
            items=[normalized('dy',x) for x in data.get('aweme_list') or []]
            # User lists may include pinned older posts.
            return sorted(items,key=lambda x:x['time'] or 0,reverse=True)[:HISTORY_PER_AUTHOR]
        profile=await self.page.visible('https://www.zhihu.com/people/'+aid)
        await self.store.author(aid,profile['title'],'https://www.zhihu.com/people/'+aid,profile)
        items=[]
        for fn,kind in [(self.client.get_creator_answers,'answer'),(self.client.get_creator_articles,'article'),(self.client.get_creator_videos,'zvideo')]:
            data=await fn(aid,limit=10)
            items.extend(normalized('zhihu',x,kind) for x in data.get('data') or [])
        return sorted(items,key=lambda x:x['time'] or 0,reverse=True)[:HISTORY_PER_AUTHOR]

    async def author_phase(self):
        groups=defaultdict(set)
        for row in await self.store.authors():groups[row['author_id']].add(row['keyword'])
        for aid,kws in groups.items():
            state=await self.store.state('author:'+aid) or {}
            if state.get('done') and (state.get('target')==HISTORY_PER_AUTHOR or state.get('error_type')=='AccessRestricted'):continue
            self.progress('author_history',author=aid,target_notes=HISTORY_PER_AUTHOR)
            try:items=await self.history(aid)
            except Exception as e:
                # Never repeatedly call a restricted author endpoint; record the gap.
                await self.store.state('author:'+aid,{'done':True,'status':'incomplete','error_type':type(e).__name__})
                self.progress('author_history_gap',author=aid,error_type=type(e).__name__)
                continue
            successful=0
            for candidate in items:
                item=await self.detail(candidate);await self.persist(item)
                for kw in kws:await self.store.link(kw,'history',item)
                successful+=1
            await self.store.state('author:'+aid,{'done':True,'successful':successful,
                 'target':HISTORY_PER_AUTHOR,'status':'complete' if successful==HISTORY_PER_AUTHOR else 'partial_available',
                 'ordering':'API_publish_order; latest timestamps from returned page'})

    async def media_items(self,item):
        if self.platform=='xhs':return xhs_media.build_media_items(item['raw'])
        if self.platform=='dy':return dy_media.build_media_items(item['raw'])
        if self.platform=='bili':
            v=item['raw'].get('View') or {};resources=[]
            cover=bili_media.build_cover_item(v,item['id'])
            if cover:resources.append(cover)
            if v.get('aid') and v.get('cid'):
                use_dash=is_ffmpeg_available()
                info=await self.client.get_video_play_url(v['aid'],v['cid'],fnval=bili_media.DASH_FNVAL if use_dash else bili_media.MP4_FNVAL)
                video=bili_media.build_dash_item(info,item['id']) if use_dash else bili_media.build_durl_item(info,item['id'])
                if video:resources.append(video)
            return resources
        raw=item['raw'].get('search_or_list_object') or item['raw']
        content=raw.get('content','')
        urls=list(dict.fromkeys(Selector(text=content).css('img::attr(data-original)').getall()+Selector(text=content).css('img::attr(src)').getall()))
        if not urls:
            page=await self.page.visible(item['url']);urls=page['images']
            # Only article content images; avatar/navigation resources are not attachments.
            urls=[u for u in urls if 'pic' in urlsplit(u).netloc and not any(x in u for x in ('_xs','_s.jpg','_l.jpg'))]
        resources=[MediaItem(u,MediaType.IMAGE,item['id'],stem=f'image_{i+1:03}') for i,u in enumerate(urls) if u.startswith('https://')]
        if item['type']=='zvideo':
            video=raw.get('video') or {}
            url=(video.get('playlist') or {}).get('hd',{}).get('play_url')
            if url:resources.append(MediaItem(url,MediaType.VIDEO,item['id'],stem='video'))
        return resources

    async def media_phase(self):
        rows=await self.store.pool.fetch('''SELECT DISTINCT c.content_type,c.content_id
          FROM acquisition_content c JOIN acquisition_link l USING(platform,content_type,content_id)
          WHERE l.run_id=$1 AND l.platform=$2''',self.args.run_id,self.platform)
        for row in rows:
            key='media:'+row['content_type']+':'+row['content_id']
            state=await self.store.state(key) or {}
            if state.get('done'):continue
            item=await self.store.cached(row['content_type'],row['content_id'])
            self.progress('downloading',content_id=item['id'])
            try:resources=await self.media_items(item)
            except Exception as e:
                await self.store.state(key,{'done':True,'status':'failed','error_type':type(e).__name__});continue
            downloaded=0
            for resource in resources:
                if self.platform=='zhihu':resource.content_id=item['type']+'_'+item['id']
                resource.backup_urls=();resource.audio_backup_urls=()
                path=await self.downloader.download(resource)
                await self.store.media(item,resource,path,error=None if path else 'download_failed')
                if path:downloaded+=1
            await self.store.state(key,{'done':True,'status':'complete' if downloaded==len(resources) and resources else 'partial_or_unavailable',
                                       'downloaded':downloaded,'requested':len(resources)})

    async def run(self):
        logging.disable(logging.CRITICAL)
        try:
            await self.setup()
            if self.args.preflight:return
            self.store=await Store.open(self.args.run_id,self.platform)
            self.lock_connection=await self.store.pool.acquire()
            locked=await self.lock_connection.fetchval('SELECT pg_try_advisory_lock(hashtext($1))','acquisition:'+self.platform)
            if not locked:raise RuntimeError('another worker already owns this platform')
            await self.store.state('worker',{'pid':os.getpid(),'status':'running'})
            await self.search_phase();await self.author_phase();await self.media_phase()
            counts=await self.store.counts()
            self.progress('finished',counts=counts)
            await self.store.state('worker',{'pid':os.getpid(),'status':'finished','counts':counts})
        except Exception as e:
            # Messages are deliberately restricted to error classes; credentials never appear in logs.
            self.progress('blocked',error_type=type(e).__name__,
                reason=str(e) if type(e) is AccessRestricted else 'collection_operation_failed',
                network_metadata=self.page.last_network,
                locations=[dict(file=Path(f.filename).name,line=f.lineno,function=f.name)
                           for f in traceback.extract_tb(e.__traceback__)[-5:]])
            if self.store:await self.store.state('worker',{'pid':os.getpid(),'status':'blocked','error_type':type(e).__name__})
            if self.args.preflight:raise SystemExit(2)
        finally:
            self.cookies.clear()
            await self.page.close()
            if self.store:
                if self.lock_connection:
                    await self.lock_connection.execute('SELECT pg_advisory_unlock(hashtext($1))','acquisition:'+self.platform)
                    await self.store.pool.release(self.lock_connection)
                await self.store.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('--platform',choices=PAGES,required=True)
    p.add_argument('--run-id',required=True)
    p.add_argument('--output',required=True);p.add_argument('--preflight',action='store_true')
    p.add_argument('--allow-session-memory',action='store_true')
    asyncio.run(Job(p.parse_args()).run())

if __name__=='__main__':main()
