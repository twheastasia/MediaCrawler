"""Chrome CDP adapters for Kuaishou, Weibo and Tieba collection jobs.

Native clients supply endpoints/signatures; all requests use the same gate.
No login material is serialized and no native automatic retries are used.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import MethodType
from urllib.parse import urlencode, quote

import httpx
from collection.runtime import AccessRestricted, LoginRequired, BrowserTransport, GatedTransport
from collection.settings import DOMAINS, HISTORY_PER_AUTHOR
from collection.worker import Job, stamp, text_of
from media_downloader import MediaDownloader, MediaItem, MediaType


def timestamp(value):
    number = stamp(value)
    if number:
        return number // 1000 if number > 100_000_000_000 else number
    if value:
        try: return int(parsedate_to_datetime(str(value)).timestamp())
        except (ValueError, TypeError): pass
        try: return int(datetime.fromisoformat(str(value)).timestamp())
        except ValueError: pass
    return None


def normalize(platform, raw):
    if platform == 'ks':
        photo = raw.get('photo') or {}; author = raw.get('author') or raw.get('user') or {}
        cid = str(photo.get('id') or '')
        return dict(type='video',id=cid,author_id=str(author['id']) if author.get('id') else None,
                    title=photo.get('caption') or '',body=photo.get('caption') or '',time=timestamp(photo.get('timestamp')),
                    url='https://www.kuaishou.com/short-video/'+cid,raw=raw)
    if platform == 'wb':
        post = raw.get('mblog') or raw; author = post.get('user') or {}
        cid = str(post.get('id') or post.get('mid') or '')
        return dict(type='note',id=cid,author_id=str(author['id']) if author.get('id') else None,
                    title=text_of(post.get('text'))[:100],body=text_of(post.get('text')),time=timestamp(post.get('created_at')),
                    url='https://m.weibo.cn/detail/'+cid,raw=post)
    record = raw.get('thread') or raw
    floor = raw.get('first_floor') or {}
    author = record.get('user') or raw.get('_author') or {}
    cid = str(record.get('tid') or record.get('id') or '')
    content = floor.get('content') or record.get('content') or ''
    if isinstance(content,list):
        body = ''.join(str(part.get('text') or '') for part in content if isinstance(part,dict))
    else: body = text_of(content)
    aid = author.get('portrait') or author.get('id')
    return dict(type='thread',id=cid,author_id=str(aid) if aid else None,
                title=text_of(record.get('title')),body=body,time=timestamp(floor.get('time') or record.get('time') or record.get('create_time')),
                url='https://tieba.baidu.com/p/'+cid,raw=raw)


def extract_candidates(platform,data):
    if platform == 'ks':
        rows = data.get('feeds') or (data.get('visionSearchPhoto') or {}).get('feeds') or []
    elif platform == 'wb':
        rows = []
        def walk(cards):
            for card in cards:
                if card.get('mblog'): rows.append(card['mblog'])
                walk(card.get('card_group') or [])
        walk(data.get('cards') or [])
    else:
        rows = [c.get('data') or {} for c in (data.get('data') or {}).get('card_list') or []
                if c.get('cardInfo') == 'thread' or c.get('cardStyle') == 'thread']
    items = [normalize(platform,row) for row in rows]
    return list({(i['type'],i['id']):i for i in items if i['id']}.values())


def check_payload(platform, data, allow_empty_comments=False):
    # Read only API status fields, never classify ordinary post text as a barrier.
    message = str(data.get('msg') or data.get('error_msg') or data.get('error') or '')
    if any(s in message.lower() for s in ('captcha','verify','验证','频繁','风控','访问受限')):
        raise AccessRestricted('platform_verification_required')
    if any(s in message.lower() for s in ('未登录','请登录','登录失效','login required')):
        raise LoginRequired('platform_login_required')
    if platform == 'ks':
        if data.get('result') in (2,50,51,52): raise AccessRestricted('kuaishou_access_restricted')
        if data.get('errors'): raise AccessRestricted('kuaishou_api_error')
        if data.get('result') not in (None,1): raise RuntimeError('kuaishou_result_error')
    elif platform == 'wb' and data.get('ok') != 1:
        if allow_empty_comments and message in ('暂无数据','暂无评论','还没有评论','评论已关闭'):
            return
        if any(s in message for s in ('暂无内容','这里还没有内容','没有找到')): return
        raise AccessRestricted('weibo_api_rejected')
    elif platform == 'tieba' and str(data.get('error_code', data.get('no',0))) not in ('0','None'):
        raise AccessRestricted('tieba_api_rejected')


class AdditionalJob(Job):
    async def run(self):
        if not getattr(self.args,'smoke',False):
            return await super().run()
        import logging, os
        from collection.database import Store
        from var import source_keyword_var
        logging.disable(logging.CRITICAL)
        try:
            self.store=await Store.open(self.args.run_id,self.platform)
            self.lock_connection=await self.store.pool.acquire()
            if not await self.lock_connection.fetchval('SELECT pg_try_advisory_lock(hashtext($1))','acquisition:'+self.platform):
                raise RuntimeError('platform_worker_already_running')
            await self.setup()
            source_keyword_var.set('AI课程')
            data=await self.raw_search('AI课程',1)
            items=extract_candidates(self.platform,data)
            if not items:raise ValueError('smoke_search_empty')
            item=await self.detail(items[0]);await self.persist(item)
            await self.store.link('AI课程','search',item)
            history_count=0
            if item.get('author_id'):
                history=await self.history(item['author_id'])
                if history:
                    latest=await self.detail(history[0]);await self.persist(latest)
                    await self.store.link('AI课程','history',latest)
                history_count=len(history)
            resources=await self.media_items(item)
            image=next((r for r in resources if r.media_type==MediaType.IMAGE),None)
            downloaded=False
            if image:
                image.backup_urls=();image.audio_backup_urls=()
                path=await self.downloader.download(image)
                await self.store.media(item,image,path,error=None if path else 'download_failed')
                downloaded=bool(path)
            result=dict(search_candidates=len(items),saved_search=1,author_available=bool(item.get('author_id')),
                        author_history_available=history_count,media_candidates=len(resources),attachment_downloaded=downloaded)
            await self.store.state('smoke',result)
            self.progress('smoke_passed',**result)
        except Exception as error:
            import traceback
            self.progress('blocked',error_type=type(error).__name__,requires_user_action=isinstance(error,(AccessRestricted,LoginRequired)),
                          reason='smoke_failed',locations=[dict(file=Path(f.filename).name,line=f.lineno,function=f.name)
                            for f in traceback.extract_tb(error.__traceback__)[-5:]])
        finally:
            self.cookies.clear()
            await self.page.close()
            if self.store:
                if self.lock_connection:
                    await self.lock_connection.execute('SELECT pg_advisory_unlock(hashtext($1))','acquisition:'+self.platform)
                    await self.store.pool.release(self.lock_connection)
                await self.store.close()

    async def setup(self):
        if not self.args.allow_session_memory: raise RuntimeError('session_memory_authorization_required')
        await self.page.connect('about:blank')
        if self.platform == 'ks':
            from media_platform.kuaishou.help import KS_SIGN_CAPTURE_SCRIPT
            await self.page.page.add_init_script(KS_SIGN_CAPTURE_SCRIPT)
        await self.gate.action('chrome_initial_page',lambda:self.page.navigate(DOMAINS[self.platform]))
        self.cookies=await self.page.credentials([DOMAINS[self.platform]])
        # Current Kuaishou web login may expose only userId to this origin;
        # endpoint responses remain authoritative and stop on login rejection.
        markers={'ks':('passToken','kuaishou.server.web_st','userId'), 'wb':('SUB',), 'tieba':('BDUSS','STOKEN')}
        available=any(self.cookies.get(k) for k in markers[self.platform])
        self.progress('session_checked',session_available=available)
        if not available:
            if not await self.sms_login(open_login=True):raise LoginRequired('platform_login_required')
            self.cookies.clear()
            self.cookies.update(await self.page.credentials([DOMAINS[self.platform]]))
            if not any(self.cookies.get(k) for k in markers[self.platform]):raise LoginRequired('sms_login_not_confirmed')
        ua=await self.page.evaluate('() => navigator.userAgent')
        self.headers={'User-Agent':ua,'Referer':DOMAINS[self.platform]+'/',
                      'Content-Type':'application/json', 'Cookie':'; '.join(f'{k}={v}' for k,v in self.cookies.items())}
        # New platform native stores still use numeric API values for text columns.
        from sqlalchemy import event, String
        from sqlalchemy.orm import Session
        from database.models import Base
        def legacy_text(session,*_):
            for obj in session.new | session.dirty:
                if isinstance(obj,Base):
                    for col in obj.__table__.columns:
                        value=getattr(obj,col.key,None)
                        if isinstance(col.type,String) and isinstance(value,(int,float,bool)):setattr(obj,col.key,str(value))
        event.listen(Session,'before_flush',legacy_text)
        def api_factory(**kwargs):
            kwargs.pop('proxy',None)
            kwargs.update(transport=BrowserTransport(self.page,self.gate,self.blocked_paths),trust_env=False,
                          event_hooks={'response':[self.response]})
            return httpx.AsyncClient(**kwargs)
        self.api_factory=api_factory
        import media_downloader.downloader as downloader
        def media_factory(**kwargs):
            kwargs.pop('proxy',None)
            kwargs.update(transport=GatedTransport(self.gate,blocked_paths=self.blocked_paths),trust_env=False)
            return httpx.AsyncClient(**kwargs)
        downloader.make_async_client=media_factory
        self.downloader=MediaDownloader(self.platform,extra_headers={'User-Agent':ua,'Referer':DOMAINS[self.platform]+'/'},
                                        base_dir=Path(self.args.output).resolve(),max_retries=0,max_candidates=1,timeout=120)
        if self.platform=='ks':
            import media_platform.kuaishou.client as native
            native.make_async_client=api_factory
            self.client=native.KuaiShouClient(headers=self.headers,playwright_page=self.page.page,cookie_dict=self.cookies,timeout=30)
            async def signed(client,uri,data):
                # No automatic reload or rate-limit retry. Only this owned page.
                await self.page.page.wait_for_function('() => !!window.__ks_realm',timeout=15000)
                signature=await self.page.evaluate("""([u,b]) => new Promise((resolve,reject) => {
                    window.__ks_realm.call('$encode',[{url:u,query:{caver:2},form:{},requestBody:b},
                    {suc:resolve,err:()=>reject(new Error('signing_failed'))}]);})""",[uri,data])
                async with api_factory() as client_http:
                    response=await client_http.post(DOMAINS['ks']+uri,params={'__NS_hxfalcon':signature,'caver':2},
                                                    content=json.dumps(data,separators=(',',':')),headers=self.headers)
                payload=response.json();check_payload('ks',payload);return payload
            self.client.request_rest_v2_signed=MethodType(signed,self.client)
        elif self.platform=='wb':
            import media_platform.weibo.client as native
            native.make_async_client=api_factory
            self.client=native.WeiboClient(headers=self.headers,playwright_page=self.page.page,cookie_dict=self.cookies,timeout=30)
            async def request(client,method,url,**kwargs):
                raw=kwargs.pop('return_response',False)
                async with api_factory() as http:
                    response=await http.request(method,url,timeout=30,**kwargs)
                if raw:return response
                try:payload=response.json()
                except ValueError:raise AccessRestricted('weibo_non_json_response') from None
                empty_comments=self.current_operation=='comments'
                check_payload('wb',payload,allow_empty_comments=empty_comments)
                if empty_comments and payload.get('ok')!=1:
                    return {'comments_unavailable':True}
                return payload.get('data') or {}
            self.client.request=MethodType(request,self.client)
        else:
            from media_platform.tieba.client import BaiduTieBaClient
            self.client=BaiduTieBaClient(headers=self.headers,playwright_page=self.page.page,timeout=30)
            async def fetch(client,uri,method='GET',params=None,data=None,use_sign=False):
                params=dict(params or {});data=dict(data or {})
                if use_sign:
                    fields=data if method.upper()=='POST' else params
                    fields.setdefault('subapp_type','pc');fields.setdefault('_client_type','20')
                    fields['sign']=client._sign_pc_params(fields)
                async with api_factory() as http:
                    response=await http.request(method,DOMAINS['tieba']+uri,params=params,
                        content=urlencode(data) if data else None,
                        headers={'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'})
                try:payload=response.json()
                except ValueError:raise AccessRestricted('tieba_non_json_response') from None
                check_payload('tieba',payload);return payload
            self.client._fetch_json_by_browser=MethodType(fetch,self.client)

    async def response(self,response):
        await super().response(response)
        if isinstance(self.last_response,dict):check_payload(self.platform,self.last_response,
            allow_empty_comments=self.current_operation=='comments')

    async def raw_search(self,kw,page):
        if self.platform=='ks':
            key='ks_search_session:'+kw
            state=await self.store.state(key) or {}
            data=await self.client.search_info_by_keyword_v2(kw,str(page),state.get('search_session_id',''))
            # SearchSessionId is a public search cursor, not an authentication cookie.
            await self.store.state(key,{'search_session_id':data.get('searchSessionId','')})
            return data
        if self.platform=='wb':return await self.client.get_note_by_keyword(kw,page=page)
        from media_platform.tieba.field import SearchSortType
        return await self.client._fetch_json_by_browser('/mo/q/search/multsearch',params={
            'rn':20,'st':SearchSortType.TIME_DESC.value,'word':kw,'needbrand':1,'sug_type':2,'pn':page,
            'come_from':'search'},use_sign=True)

    async def detail(self,item):
        old=await self.store.cached(item['type'],item['id'])
        if old:return old
        if self.platform=='ks':
            data=await self.client.get_video_info(item['id'])
            raw=data.get('visionVideoDetail') or {}
            if not isinstance(raw.get('photo'),dict):raise ValueError('video_unavailable')
            return normalize('ks',raw)
        if self.platform=='wb':
            raw=await self.client.get('/statuses/show',params={'id':item['id']})
            if not raw.get('id'):raise ValueError('note_unavailable')
            return normalize('wb',raw)
        raw=await self.client._get_pc_page_data(item['id'])
        users={str(u.get('id')):u for u in raw.get('user_list') or []}
        floor=raw.get('first_floor') or {}
        # Persist public article fields, not the PC API's anti/tbs/session envelope.
        raw={k:raw[k] for k in ('thread','first_floor','forum','display_forum','page','user_list') if k in raw}
        raw['_author']=users.get(str(floor.get('author_id'))) or item['raw'].get('user') or {}
        result=normalize('tieba',raw)
        if not result['id']:raise ValueError('thread_unavailable')
        return result

    async def persist(self,item):
        await self.store.content(item)
        name=item['type']+'_'+item['id']
        if not all(c.isalnum() or c in '_-' for c in name):raise ValueError('invalid_content_id')
        (self.directory/(name+'.json')).write_text(json.dumps(item,ensure_ascii=False,indent=2,default=str))
        # Raw acquisition tables are authoritative; native tables remain compatible.
        if self.platform in ('ks','wb'):
            import importlib
            native=importlib.import_module('store.kuaishou' if self.platform=='ks' else 'store.weibo')
            native.mask_nickname=lambda value:value or ''
            if self.platform=='ks':await native.update_kuaishou_video(item['raw'])
            else:await native.update_weibo_note({'mblog':item['raw']})
        else:
            from model.m_baidu_tieba import TiebaNote
            from store.tieba import update_tieba_note
            a=item['raw'].get('_author') or {}
            forum=item['raw'].get('forum') or item['raw'].get('display_forum') or {}
            await update_tieba_note(TiebaNote(note_id=item['id'],title=item['title'],desc=item['body'],note_url=item['url'],
                publish_time=str(item['time'] or ''),user_nickname=a.get('name_show') or a.get('name') or '',
                tieba_name=forum.get('name') or '',tieba_link='https://tieba.baidu.com/f?'+urlencode({'kw':forum.get('name') or ''})))
        if item.get('author_id'):await self.save_author_from_content(item)

    async def save_author_from_content(self,item):
        raw=item['raw'];aid=item['author_id']
        if self.platform=='ks':a=raw.get('author') or {};home=DOMAINS['ks']+'/profile/'+quote(aid)
        elif self.platform=='wb':a=raw.get('user') or {};home=DOMAINS['wb']+'/u/'+quote(aid)
        else:a=raw.get('_author') or raw.get('user') or {};home=DOMAINS['tieba']+'/home/main?'+urlencode({'id':aid})
        name=a.get('name') or a.get('screen_name') or a.get('name_show') or a.get('show_nickname') or a.get('user_name')
        await self.store.author(aid,name,home,a)

    async def history(self,aid):
        if self.platform=='ks':
            data=await self.client.get_creator_profile(aid)
            profile=data.get('visionProfile') or {}
            author=profile.get('userProfile') or profile.get('user') or {}
            if author:await self.store.author(aid,author.get('user_name') or author.get('name'),DOMAINS['ks']+'/profile/'+aid,profile)
            data=await self.client.get_video_by_creater_v2(aid)
            items=extract_candidates('ks',data)
        elif self.platform=='wb':
            profile=await self.client.get_creator_info_by_id(aid)
            a=profile.get('userInfo') or {}
            if a:await self.store.author(aid,a.get('screen_name'),DOMAINS['wb']+'/u/'+aid,a)
            items=[];cursor='0'
            for _ in range(3):
                data=await self.client.get_notes_by_creator(aid,'107603'+aid,since_id=cursor)
                items.extend(extract_candidates('wb',data))
                next_cursor=str((data.get('cardlistInfo') or {}).get('since_id') or '')
                if len({x['id'] for x in items})>=HISTORY_PER_AUTHOR or not next_cursor or next_cursor==cursor:break
                cursor=next_cursor
        else:
            profile=await self.client._fetch_json_by_browser('/c/u/pc/homeSidebarRight',params={'portrait':aid,'un':''},use_sign=True)
            a=(profile.get('data') or {}).get('user') or profile.get('user') or {}
            if a:await self.store.author(aid,a.get('name_show') or a.get('name'),DOMAINS['tieba']+'/home/main?'+urlencode({'id':aid}),a)
            data=await self.client.get_notes_by_creator_portrait(aid,1,20)
            ids=self.client._page_extractor.extract_creator_thread_id_list_from_api(data)
            items=[dict(type='thread',id=cid,raw={}) for cid in ids]
        return sorted({x['id']:x for x in items}.values(),key=lambda x:x.get('time') or 0,reverse=True)[:HISTORY_PER_AUTHOR]

    async def media_items(self,item):
        if self.platform=='ks':
            from media_platform.kuaishou.media import build_media_items
            return build_media_items(item['raw'])
        if self.platform=='wb':
            from media_platform.weibo.media import build_media_items
            return build_media_items(item['raw'])
        urls=[]
        for part in (item['raw'].get('first_floor') or {}).get('content') or []:
            if not isinstance(part,dict):continue
            if str(part.get('type'))=='3':
                url=part.get('origin_src') or part.get('big_cdn_src') or part.get('src')
                if url and url not in urls:urls.append(url)
        return [MediaItem(u,MediaType.IMAGE,item['id'],stem=f'image_{i+1:03}') for i,u in enumerate(urls)]
