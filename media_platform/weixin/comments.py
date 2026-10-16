"""Public, rendered WeChat messages only; no app credentials or private APIs."""
from __future__ import annotations

import hashlib
from collection.comment_store import save_page,summary
from collection.comments import normalize
from media_platform.weixin.extractor import ARTICLE_SNAPSHOT,check_article_page
from media_platform.weixin.exception import ArticleUnavailable,ArticleSkipped,ParseError

SNAPSHOT = r'''() => {
 const roots=[...document.querySelectorAll('#js_cmt_area,#js_comment_area,.rich_media_comment,.js_comment_list')];
 const values=[];
 for(const root of roots) for(const node of root.querySelectorAll('[data-comment-id],[data-content-id],.js_comment_item,.discuss_item')) {
   const body=node.querySelector('.js_comment_content,.comment_content,.discuss_message_content,.js_content');
   if(!body || !body.textContent.trim()) continue;
   const author=node.querySelector('.nickname,.discuss_nickname,.js_nickname');
   values.push({id:node.getAttribute('data-comment-id')||node.getAttribute('data-content-id')||node.getAttribute('data-id')||'',
     parent_id:node.getAttribute('data-parent-id')||'',content:body.textContent.trim(),
     user:{name:author?.textContent?.trim()||''},
     publish_text:node.querySelector('.time,.discuss_time')?.textContent?.trim()||'',
     like_count:node.querySelector('.js_like_num,.praise_num')?.textContent?.trim()||''});
 }
 const controls=roots.flatMap(r=>[...r.querySelectorAll('a,button,[role="button"]')]);
 const more=controls.find(e=>/^(?:加载更多|查看更多留言|查看更多评论|展开更多回复|查看更多回复|展开全部回复)$/.test(e.textContent.trim()) && e.getClientRects().length);
 return {comments:values,area_present:roots.length>0,more:!!more};
}'''
LOAD_MORE = r'''() => {
 const roots=[...document.querySelectorAll('#js_cmt_area,#js_comment_area,.rich_media_comment,.js_comment_list')];
 const controls=roots.flatMap(r=>[...r.querySelectorAll('a,button,[role="button"]')]);
 const more=controls.find(e=>/^(?:加载更多|查看更多留言|查看更多评论|展开更多回复|查看更多回复|展开全部回复)$/.test(e.textContent.trim()) && e.getClientRects().length);
 if(more){more.click();return true;} return false;
}'''


async def collect(job,item,current_page=False):
    key='comments:article:'+item['id']
    state=await job.store.state(key) or {}
    if state.get('done'):return state
    try:
        if not current_page:
            await job.document(item['url'],ARTICLE_SNAPSHOT)
        check_article_page(await job.page.evaluate(ARTICLE_SNAPSHOT))
        async def reveal():
            await job.page.page.evaluate('() => window.scrollTo(0,document.body.scrollHeight)')
            await job.page.page.wait_for_timeout(1500)
        await job.gate.action('weixin_public_comments_scroll',reveal)
        seen=set();pages=0
        while True:
            snap=await job.page.evaluate(SNAPSHOT)
            rows=[]
            for raw in snap['comments']:
                raw=dict(raw)
                if not raw['id']:
                    raw['id']='dom_'+hashlib.sha256((raw['user']['name']+'\0'+raw['content']+'\0'+raw['publish_text']).encode()).hexdigest()
                    raw['identity_source']='public_dom_fingerprint'
                rows.append(normalize('weixin',raw,raw.get('parent_id') or None))
            ids={r['id'] for r in rows};pages+=1
            stalled=bool(pages>1 and ids<=seen and snap['more'])
            seen.update(ids)
            done=not snap['more'] or stalled or pages>=1000
            status=('not_exposed' if not rows else 'partial' if snap['more'] else 'public_visible_complete')
            state={'version':1,'done':done,'status':status,'pages':pages,'public_visible':len(seen),
                'reason':'comment_area_not_exposed' if not snap['area_present'] else
                    'no_public_visible_comments' if not rows else 'load_more_stalled' if stalled else
                    'page_budget' if pages>=1000 and snap['more'] else 'public_dom_only',
                'scope':'public_rendered_messages_and_replies','all_platform_comments_verified':False}
            await save_page(job.store,item,rows,key,state,prefix='wxarticle')
            if done:return state
            async def more():
                clicked=await job.page.page.evaluate(LOAD_MORE)
                if clicked:await job.page.page.wait_for_timeout(1500)
                return clicked
            await job.gate.action('weixin_public_comments_more',more)
    except (ArticleUnavailable,ArticleSkipped,ParseError) as error:
        state={'version':1,'done':True,'status':'unavailable','reason':str(error)}
        await save_page(job.store,item,[],key,state,prefix='wxarticle')
        return state


async def phase(job):
    rows=await job.store.pool.fetch('''SELECT DISTINCT c.content_id FROM wxarticle_content c
        JOIN wxarticle_source s ON s.content_id=c.content_id WHERE s.run_id=$1 ORDER BY c.content_id''',job.args.run_id)
    for row in rows:
        item=await job.store.cached('article',row['content_id'])
        job.progress('comments',content_id=item['id'])
        await collect(job,item)
    return await summary(job.store,prefix='wxarticle')
