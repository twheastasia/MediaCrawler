# 八平台公开评论采集

最新历史评论例外：用户允许小红书仅历史评论补采阶段的单笔记HTTP461记录缺口后继续下一笔记。仅`/api/sns/web/v2/comment/page`和`/api/sns/web/v2/comment/sub/page`适用；无可见页面验证和明确业务验证时，评论断点保存`status=partial, skipped=true, done=true, reason=xhs_historical_comment_http_461`，未完成队列另存`pending_after_skip`，保留已采评论。此处done仅代表本轮已处理/跳过，不能当作完整评论成功，恢复不自动重试该笔记；不会因这类历史评论错误的累计次数暂停整个平台。搜索、正文、新内容评论、其他HTTP限制及真实验证仍暂停。初版连续3篇保护已撤回，不可据该旧原因阻止本轮授权继续。

2026-10-16新增；同日按用户要求改为落入各平台独立评论表。最初版本只写统一表，原生评论表为空；现在已增加同步写入和历史补入。

## 范围与存储

七平台（B站、抖音、知乎、小红书、快手、微博、贴吧）对本任务已保存和新增的搜索结果、作者历史作品采集公开主评论与回复，按服务端分页直到结束，不套用“每词100条/作者10条”的内容配额。接口返回的公开评论原始对象保留在`acquisition_comment.raw`；正文、原始公开作者、父评论ID、时间、点赞数另存列，唯一键为平台+作品类型+作品ID+评论ID。`collected_at`为首次抓取，更新不覆盖。

微信公众号使用`wxarticle_comment`，仅从自己的CDP文章页面采集公开渲染的留言与回复；不调用需应用身份的私有接口。页面未显示留言区或未显示评论时记`not_exposed`，不能当成零评论成功；`public_visible_complete`仅表示已遍历当前公开DOM，不代表后台所有留言。无稳定评论ID的DOM节点以公开昵称/内容/显示时间生成指纹，并标记`identity_source`。

日常查看评论请使用各平台独立表：

| 平台 | 评论表 | 评论内容 | 评论用户 ID | 所属作品 |
|---|---|---|---|---|
| 抖音 | `douyin_aweme_comment` | `content` | `user_id` | `aweme_id` |
| 小红书 | `xhs_note_comment` | `content` | `user_id` | `note_id` |
| B站 | `bilibili_video_comment` | `content` | `user_id` | `video_id` |
| 知乎 | `zhihu_comment` | `content` | `user_id` | `content_type` + `content_id` |
| 快手 | `kuaishou_video_comment` | `content` | `user_id` | `video_id` |
| 微博 | `weibo_note_comment` | `content` | `user_id` | `note_id` |
| 贴吧 | `tieba_comment` | `content` | `user_id` | `note_id` |
| 微信公众号 | `wxarticle_comment` | `body` | `author_id` | `content_id` |

七平台 `user_id` 为公开平台 ID，抖音优先原始 `user.uid`、缺少时保留公开 `sec_uid` 等可用 ID；不以匿名 `creator_hash` 代替。用户 ID 未公开时为 NULL，不从昵称虚构；微信公开 DOM 当前可能只提供昵称而不提供用户 ID。图片评论原本没有文字时，`content` 为空，不填造文字。所有主评论与回复均保存，`parent_comment_id` 保留回复关系。

统一表 `acquisition_comment` 继续保存原始对象和支撑任务验收；每页的独立表、统一表和断点在同一 PostgreSQL 事务中保存，任一步失败全部回滚。独立表按作品 ID + 评论 ID 去重（知乎另含作品类型），重放不新增重复记录。`add_ts` 为首次采集毫秒时间，不因补入或重放覆盖；`last_modify_ts` 保存最近更新时间。

七平台和微信分别关联本来的内容表；关键词通过本来的任务内容关联查询。评论图片等引用保留在统一表 raw，不新增评论媒体下载任务。

```sql
SELECT aweme_id, comment_id, user_id, content, parent_comment_id, add_ts
FROM douyin_aweme_comment
ORDER BY add_ts DESC
LIMIT 100;
```

`collection/native_comments.py` 负责原生表映射和事务内写入；`database/migrations/20261016_platform_comment_user_id.sql` 增加 user_id、快手父评论 ID 及唯一索引。启动准备会幂等应用表结构。只补入指定任务已有记录可执行：

```bash
.venv/bin/python -m collection.sync_comments \
  --run-id mit_ai_smallbatch_20261014 \
  --run-id mit_ai_additional_20261015
```

该命令只操作 PostgreSQL，不请求平台、不恢复暂停；输出数量、缺失和不一致统计，不输出评论正文或用户 ID。重复执行不新增重复记录。当前存活采集进程需加载新代码后才会持续同步；历史补入应在加载后再次运行以核对切换期间记录。

## 断点与执行

`acquisition_progress`或`wxarticle_progress`中`comments:<content_type>:<id>`保存评论队列与游标。每页评论和断点同事务提交；重放通过唯一键去重。主评论的回复进入队列，保持父子关系。游标不前进/循环、评论接口结构变化或不可用均记录具体缺口，不能算完成。`status=partial/unavailable/not_exposed`需在最终验收单独统计。

worker启动后先补已保存内容的评论，搜索与作者作品阶段后再次补齐新内容，再继续附件阶段。已有任务的正文、作者、附件断点不清空。微信保持正文优先：文章阶段结束后补采留言，再排空图片待办。

只复用Chrome CDP和本机内存Cookie；不保存Cookie或请求头。所有评论请求复用单并发限流：七平台请求结束后至少15秒，微信至少10秒，无额外随机等待。登录、验证码和访问限制照常暂停；评论功能不能解除原有暂停。已暂停的平台安装代码后，待用户处理其原有问题并授权恢复再实测。

## 验证

`tests/test_collection_comments.py`覆盖七平台主评论/回复分页、恢复、去重、真实限制传播、游标循环、敏感字段排除以及微信未暴露状态。`COMMENT_TEST_DB=1`在随机隔离的测试schema验证真实PostgreSQL幂等及事务回滚；微信原有隔离数据库测试也验证内容合并兼容。
