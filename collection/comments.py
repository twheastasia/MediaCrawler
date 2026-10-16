"""Single-page adapters: no hidden retries, shared CDP transports and request gate."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit
from parsel import Selector
from collection.runtime import AccessRestricted, LoginRequired
from collection.comment_store import save_page, summary
from collection.text import html_text


class CommentParseError(ValueError):
    pass


class CommentUnavailable(ValueError):
    pass


class HistoricalCommentUnavailable(CommentUnavailable):
    """One historical XHS note's comment endpoint, explicitly skippable."""
    pass


def number(value):
    try:return int(value) if value is not None else None
    except (ValueError,TypeError):return None


def public_raw(value):
    # Comment objects only, never full API envelopes/headers or browser globals.
    if isinstance(value,dict):
        return {k:public_raw(v) for k,v in value.items() if not re.search(
            r'cookie|authorization|password|session|token|pass_ticket|credential|^tbs$|^sign(?:ature)?$',k,re.I)}
    if isinstance(value,list):return [public_raw(v) for v in value]
    return value


def text(value):
    if isinstance(value,list):return ''.join(text(v.get('text','')) for v in value if isinstance(v,dict))
    return html_text(value)


def normalize(platform, raw, parent=None):
    user=raw.get('user') or raw.get('member') or raw.get('author') or raw.get('userInfo') or {}
    if 'member' in user:user=user['member']
    cid=raw.get('cid') if platform=='dy' else raw.get('rpid') if platform=='bili' else raw.get('comment_id') if platform=='ks' else raw.get('id')
    cid=cid or raw.get('commentId') or raw.get('comment_id') or raw.get('id')
    if not cid:raise CommentParseError('comment_id_missing')
    body=raw.get('content')
    if isinstance(body,dict):body=body.get('message') or body.get('text')
    if body is None:body=raw.get('text') or raw.get('comment') or ''
    stamp=number(raw.get('create_time') or raw.get('created_time') or raw.get('ctime') or raw.get('time') or raw.get('timestamp') or raw.get('created'))
    if stamp and stamp>100_000_000_000:stamp//=1000
    # Weibo uses RFC dates; preserve the original even when it cannot be parsed.
    if not stamp and raw.get('created_at'):
        from email.utils import parsedate_to_datetime
        try:stamp=int(parsedate_to_datetime(raw['created_at']).timestamp())
        except (ValueError,TypeError):pass
    aid=user.get('sec_uid') or user.get('user_id') or user.get('id') or user.get('mid') or raw.get('author_id') or raw.get('authorId')
    return {'id':str(cid),'parent_id':str(parent) if parent else None,
        'author_id':str(aid) if aid else None,
        'author_name':user.get('nickname') or user.get('name') or user.get('uname') or user.get('screen_name') or user.get('name_show') or raw.get('author_name') or raw.get('authorName'),
        'body':text(body),'time':stamp,'likes':number(raw.get('like_count') or raw.get('like') or raw.get('liked_count') or raw.get('digg_count') or raw.get('likedCount') or raw.get('likeCount') or raw.get('voteup_count') or 0),
        'raw':public_raw(raw)}


@dataclass
class Page:
    rows:list
    next_cursor:object=None
    children:list=field(default_factory=list)
    gaps:list=field(default_factory=list)


def need_list(data,key):
    value=data.get(key)
    if not isinstance(value,list):raise CommentParseError('comment_list_missing_'+key)
    return value


def child(parent,cursor=None,**context):
    return {'parent':str(parent),'cursor':cursor,**context}


class CommentCollector:
    def __init__(self,job):self.job=job;self.platform=job.platform

    async def page(self,item,stream):
        self.job.current_operation='comments'
        if self.platform=='xhs' and hasattr(self.job,'web_comment_page'):
            return await self.job.web_comment_page(item,stream)
        p=self.platform;c=self.job.client;cid=item['id'];parent=stream.get('parent');cursor=stream.get('cursor')
        children=[];gaps=[];rows=[]
        if p=='dy':
            data=await (c.get_sub_comments(cid,parent,int(cursor or 0)) if parent else c.get_aweme_comments(cid,int(cursor or 0)))
            if data.get('status_code',0)!=0:raise CommentParseError('douyin_comment_status')
            values=data.get('comments')
            if values is None and data.get('total')==0:values=[]
            if not isinstance(values,list) or 'has_more' not in data:raise CommentParseError('douyin_comment_shape')
            nxt=data.get('cursor') if data['has_more'] else None
            for r in values:
                row=normalize(p,r,parent);rows.append(row)
                if not parent and (number(r.get('reply_comment_total')) or 0)>0:children.append(child(row['id'],0))
        elif p=='xhs':
            token=item['raw'].get('xsec_token','')
            data=await (c.get_note_sub_comments(cid,parent,token,cursor=cursor or '') if parent else c.get_note_comments(cid,token,cursor or ''))
            values=need_list(data,'comments')
            if 'has_more' not in data:raise CommentParseError('xhs_comment_cursor_missing')
            nxt=data.get('cursor') if data['has_more'] else None
            for r in values:
                row=normalize(p,r,parent);rows.append(row)
                if not parent:
                    subs=r.get('sub_comments') or []
                    rows.extend(normalize(p,s,row['id']) for s in subs)
                    if r.get('sub_comment_has_more'):children.append(child(row['id'],r.get('sub_comment_cursor') or ''))
        elif p=='bili':
            from media_platform.bilibili.field import CommentOrderType
            aid=str((item['raw'].get('View') or item['raw']).get('aid') or '')
            if not aid:raise CommentUnavailable('bilibili_numeric_aid_missing')
            data=await (c.get_video_level_two_comments(aid,int(parent),int(cursor or 1),20,CommentOrderType.DEFAULT) if parent else c.get_video_comments(aid,next=int(cursor or 0)))
            values=data.get('replies')
            if values is None and (data.get('cursor') or data.get('page')):values=[]
            if not isinstance(values,list):raise CommentParseError('bilibili_comment_shape')
            if parent:
                page=data.get('page') or {};total=number(page.get('count'))
                if total is None:raise CommentParseError('bilibili_reply_total_missing')
                nxt=int(cursor or 1)+1 if int(cursor or 1)*20<total else None
            else:
                state=data.get('cursor') or {}
                if 'is_end' not in state:raise CommentParseError('bilibili_comment_cursor_missing')
                nxt=state.get('next') if not state['is_end'] else None
                from media_platform.bilibili.client import _extract_pinned_comments
                values=_extract_pinned_comments((data.get('top'),data.get('top_replies')))+values
            for r in values:
                row=normalize(p,r,parent);rows.append(row)
                if not parent and (number(r.get('rcount') or r.get('count')) or 0)>0:children.append(child(row['id'],1))
        elif p=='zhihu':
            data=await (c.get_child_comments(parent,str(cursor or '')) if parent else c.get_root_comments(cid,item['type'],str(cursor or '')))
            if not data:raise CommentUnavailable('zhihu_comments_unavailable_or_404')
            values=need_list(data,'data');paging=data.get('paging') or {}
            if 'is_end' not in paging:raise CommentParseError('zhihu_comment_paging_missing')
            nxt=None
            if not paging['is_end']:
                nxt=(parse_qs(urlsplit(paging.get('next','')).query).get('offset') or [None])[0]
                if nxt is None:raise CommentParseError('zhihu_next_offset_missing')
            for r in values:
                row=normalize(p,r,parent);rows.append(row)
                if not parent and (number(r.get('child_comment_count')) or 0)>0:children.append(child(row['id'],''))
        elif p=='ks':
            data=await (c.get_video_sub_comments(cid,int(parent),str(cursor or '')) if parent else c.get_video_comments(cid,str(cursor or '')))
            values=need_list(data,'subCommentsV2' if parent else 'rootCommentsV2')
            if 'pcursorV2' not in data:raise CommentParseError('kuaishou_comment_cursor_missing')
            nxt=data['pcursorV2'];nxt=None if nxt=='no_more' else nxt
            for r in values:
                row=normalize(p,r,parent);rows.append(row)
                if not parent and r.get('hasSubComments'):children.append(child(row['id'],''))
        elif p=='wb':
            if parent:
                data=await c.get('/comments/hotFlowChild',params={'cid':parent,'max_id':cursor or 0,'max_id_type':stream.get('max_id_type',0)})
            else:data=await c.get_note_comments(cid,int(cursor or 0),int(stream.get('max_id_type',0)))
            if data.get('comments_unavailable'):raise CommentUnavailable('weibo_comments_unavailable')
            values=need_list(data,'data');nxt=data.get('max_id') or None
            if 'max_id' not in data:raise CommentParseError('weibo_comment_cursor_missing')
            for r in values:
                row=normalize(p,r,parent);rows.append(row)
                if not parent:
                    subs=r.get('comments') or []
                    if isinstance(subs,list):rows.extend(normalize(p,s,row['id']) for s in subs if s.get('id'))
                    if (number(r.get('total_number')) or 0)>len(subs):children.append(child(row['id'],0,max_id_type=0))
            if nxt is not None:nxt={'cursor':nxt,'max_id_type':data.get('max_id_type',0)}
        else:
            return await self.tieba_page(item,stream)
        if nxt is not None and not isinstance(nxt,dict):nxt={'cursor':nxt}
        if nxt is not None and nxt.get('cursor') is None:raise CommentParseError('next_comment_cursor_missing')
        return Page(rows,nxt,children,gaps)

    async def tieba_page(self,item,stream):
        c=self.job.client;parent=stream.get('parent');pn=int(stream.get('cursor') or 1)
        if parent:
            from urllib.parse import urlencode
            url='https://tieba.baidu.com/p/comment?'+urlencode({'tid':item['id'],'pid':parent,'fid':stream.get('fid',''),'pn':pn})
            async def visit():
                response=await self.job.page.page.goto(url,wait_until='domcontentloaded',timeout=30000)
                if response and response.status in (401,403,412,429,461):raise AccessRestricted('tieba_comment_access_restricted')
                return await self.job.page.page.content()
            markup=await self.job.gate.action('tieba_comment_replies',visit)
            selector=Selector(text=markup)
            if selector.css('input[name="vcode"],#verify-form,.verify-wrap'):raise AccessRestricted('tieba_comment_verification')
            rows=[]
            for node in selector.css('li.lzl_single_post'):
                try:r=__import__('json').loads(node.attrib.get('data-field','{}'))
                except ValueError:raise CommentParseError('tieba_reply_field_invalid') from None
                if not r.get('spid'):raise CommentParseError('tieba_reply_id_missing')
                raw={'id':r['spid'],'content':node.css('.lzl_content_main').get() or '',
                     'user':{'id':r.get('user_id'),'name':r.get('showname')},'publish_time':node.css('.lzl_time::text').get()}
                rows.append(normalize('tieba',raw,parent))
            total=int(stream.get('total') or 0)
            if not rows and total>0:raise CommentUnavailable('tieba_replies_not_exposed')
            nxt={'cursor':pn+1} if pn*10<total else None
            return Page(rows,nxt)
        data=await c._get_pc_page_data(item['id'],pn)
        values=need_list(data,'post_list');page=data.get('page') or {}
        total=number(page.get('total_page'))
        if total is None:raise CommentParseError('tieba_comment_page_missing')
        users={str(u.get('id')):u for u in data.get('user_list') or []};rows=[];children=[]
        forum=data.get('forum') or data.get('display_forum') or {}
        for r in values:
            row=normalize('tieba',{**r,'user':users.get(str(r.get('author_id')),{})})
            if number(r.get('floor'))!=1:rows.append(row)
            count=number(r.get('sub_post_number')) or 0
            if count:children.append(child(row['id'],1,total=count,fid=str(forum.get('id') or '')))
        return Page(rows,{'cursor':pn+1} if pn<total else None,children)

    async def collect(self,item,max_pages=None):
        store=self.job.store;key='comments:'+item['type']+':'+item['id']
        state=await store.state(key) or {'version':1,'queue':[child('',None)],'parents':[], 'pages':0,'observed':0,'gaps':[]}
        if state.get('done'):return state
        state=copy.deepcopy(state)
        if hasattr(self.job,'web_comment_page'):
            state['scope']='public_web_rendered_comments_and_replies'
            state['all_backend_comments_verified']=False
        state.setdefault('visited',{})
        processed=0
        while state['queue'] and (max_pages is None or processed<max_pages):
            stream=state['queue'][0]
            self.job.progress('comments',content_id=item['id'],content_type=item['type'],pages=state['pages'])
            try:page=await self.page(item,stream)
            except HistoricalCommentUnavailable as error:
                if self.platform!='xhs' or not getattr(self.job,'historical_comment_phase',False):
                    raise AccessRestricted('historical_comment_skip_outside_allowed_scope') from error
                state['gaps'].append({'parent':stream.get('parent') or None,'reason':str(error),'http_status':461})
                state.update(done=True,status='partial',skipped=True,reason=str(error),
                             pending_after_skip=state['queue'],queue=[])
                await save_page(store,item,[],key,state)
                self.job.progress('comment_skipped',content_id=item['id'],content_type=item['type'],
                                  reason=str(error),http_status=461)
                self.job.historical_comment_failures=getattr(self.job,'historical_comment_failures',0)+1
                # This user-authorized exception is per historical note. Never
                # replay the failing note, or turn its gap into platform success.
                # Real page/business verification is still raised by response().
                return state
            except (CommentParseError,CommentUnavailable) as error:
                # No automatic retry for an unavailable/malformed comment stream.
                state['gaps'].append({'parent':stream.get('parent') or None,'reason':str(error)})
                state['queue'].pop(0)
                state.update(done=not state['queue'],status='partial')
                await save_page(store,item,[],key,state)
                continue
            if self.platform=='xhs':self.job.historical_comment_failures=0
            next_queue=state['queue'][1:]
            continuation=[]
            if page.next_cursor is not None:
                nxt={**stream,**page.next_cursor}
                import json
                stream_key=stream.get('parent') or ''
                visited=state['visited'].setdefault(stream_key,[])
                current=json.dumps(stream,sort_keys=True)
                visited.append(current)
                if json.dumps(nxt,sort_keys=True) in visited:
                    page.gaps.append({'parent':stream.get('parent') or None,'reason':'comment_cursor_not_advancing'})
                else:continuation=[nxt]
            new_children=[]
            for sub in page.children:
                if sub['parent'] not in state['parents']:
                    state['parents'].append(sub['parent']);new_children.append(sub)
            state['queue']=new_children+continuation+next_queue
            state['pages']+=1;state['observed']+=len(page.rows);state['gaps'].extend(page.gaps)
            state['done']=not state['queue'];state['status']='partial' if state['gaps'] else 'complete' if state['done'] else 'running'
            await save_page(store,item,page.rows,key,state)
            processed+=1
        return state


async def phase(job):
    rows=await job.store.pool.fetch('''SELECT DISTINCT c.content_type,c.content_id FROM acquisition_content c
        JOIN acquisition_link l USING(platform,content_type,content_id) WHERE l.run_id=$1 AND l.platform=$2
        ORDER BY c.content_type,c.content_id''',job.args.run_id,job.platform)
    collector=CommentCollector(job)
    for row in rows:
        item=await job.store.cached(row['content_type'],row['content_id'])
        await collector.collect(item)
    job.current_operation=''
    return await summary(job.store)
