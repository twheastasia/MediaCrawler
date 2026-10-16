"""Credential-free Chinese activity log, alongside the machine-readable status."""
import fcntl
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

NAMES={'bili':'B站','dy':'抖音','zhihu':'知乎','xhs':'小红书','ks':'快手',
       'wb':'微博','tieba':'贴吧','weixin':'微信公众号','maimai':'脉脉人才','linkedin':'LinkedIn人员'}
STAGES={'session_checked':'登录状态检查完成','searching':'搜索中','search':'搜索中',
        'collecting':'已保存数据','comments':'采集评论与回复','downloading':'下载附件',
        'author':'采集作者作品','authors':'采集作者作品','finished':'本轮处理结束',
        'finished_with_shortfalls':'本轮结束，存在不足','completed':'本轮处理结束',
        'completed_with_gaps':'本轮结束，存在缺口','blocked':'采集暂停',
        'paused':'采集暂停','failed':'采集异常','article_saved':'正文已保存',
        'article_skipped':'文章已跳过','sms_verified':'短信登录已确认',
        'restarted':'已恢复进程','launched':'已启动采集','running':'运行中',
        'connecting':'连接Chrome','gap':'已记录数据缺口','skipped':'已跳过当前条目',
        'comment_skipped':'本篇历史评论失败，已记缺口并跳至下一笔记'}
REASONS={'AUTH_REQUIRED':'登录已失效，需要重新登录','USER_PAUSED':'用户要求暂停',
         'ACCESS_RESTRICTED':'平台要求验证或限制访问，请人工处理',
         'weibo_api_rejected':'微博接口拒绝请求，请检查账号或访问限制',
         'kuaishou_access_restricted':'快手限制访问，请人工处理',
         'account_migrated':'公众号已迁移，本篇跳过','account_blocked':'公众号被屏蔽，本篇跳过',
         'article_parse_error':'单篇正文解析失败，本篇跳过',
         'article_load_error':'单篇页面加载失败，本篇跳过',
         'article_network_error':'单篇网络异常，本篇跳过',
         'xhs_historical_comment_http_461':'历史评论接口HTTP461，本篇评论不计成功',
         'xhs_history_comments_three_consecutive_461':'连续三篇历史评论均HTTP461，暂停避免持续请求受限接口'}


def safe(value):
    text=str(value).replace('\n',' ').replace('\r',' ')[:250]
    if re.search(r'cookie|password|authorization|csrf|xsec_token|access_token|验证码\s*[:：]',text,re.I):
        return '[敏感字段已省略]'
    return text


def activity(directory,platform,status,**fields):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    level='错误' if status=='failed' else '需处理' if status in ('blocked','paused') else '缺口' if status in ('gap','skipped','comment_skipped') else '进度'
    parts=[datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S'),
           level,NAMES.get(platform,platform),STAGES.get(status,status)]
    for key,label in [('keyword','关键词'),('stage','阶段'),('task_id','任务ID'),('content_id','内容ID'),('article_id','文章ID'),
                      ('page_no','页码/游标'),('successful','已成功'),('total_people','累计人员'),
                      ('sample_rows','本页条数'),('target','目标'),('error_type','异常类型')]:
        if fields.get(key) is not None:parts.append(label+'='+safe(fields[key]))
    reason=fields.get('reason') or fields.get('pause_reason') or fields.get('error_code')
    if reason:parts.append('原因='+REASONS.get(str(reason),safe(reason)))
    if fields.get('page_path'):parts.append('页面路径='+safe(fields['page_path']))
    if fields.get('login_attempted') is not None:
        parts.append('登录表单已提交='+('是' if fields.get('login_submitted') else '否'))
    network=fields.get('network_metadata') or []
    for r in network[-3:]:
        if r.get('status') is not None:
            parts.append('请求='+safe(r.get('host','')+r.get('path',''))+' HTTP '+str(r['status']))
    if fields.get('requires_user_action') or status in ('blocked','paused'):
        parts.append('处理=暂停此平台，保留断点；登录/验证/限制不会自动重试')
    line=' | '.join(parts)+'\n'
    # One write under a lock per log; shared task log is safe across workers.
    paths=[directory/'friendly.log',directory.parent/'collection_readable.log']
    data=Path(__file__).resolve().parents[1]/'data'
    if directory.resolve().is_relative_to(data):paths.append(data/'collection_activity.log')
    for path in paths:
        with path.open('a') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            stream.write(line);stream.flush()
            fcntl.flock(stream,fcntl.LOCK_UN)
