# 小红书正常网页模式

按用户2026-10-16要求增加。使用本机已有Chrome CDP、每进程一个自有页面，读取正常网页渲染的公开搜索结果、笔记正文、作者作品列表、评论和回复。不读取浏览器隐藏状态，不主动签名或重放平台API请求；正常网页自身的加载请求由网页完成。

切换前已在真实正常网页确认搜索页30张笔记卡片、正文及16条评论/回复可见，包含稳定评论ID和公开评论用户ID。样本只是可行性检查，不等于整个任务已经完成。实际入库须另查PostgreSQL。

```bash
.venv/bin/python -m collection.launch \
  --run-id mit_ai_smallbatch_20261014 \
  --output data/mit_ai_smallbatch_20261014 \
  --allow-session-memory --resume --platform xhs --xhs-mode web
```

模式保存在本任务 `processes.json` 的 `platform_options.xhs.collection_mode`，普通异常恢复由原监控沿用，不能静默改回API。其他平台及其他run_id不受影响。每次受控导航、展开回复、滚动和附件下载均使用请求结束至少15秒的原RequestGate。

## 评论和断点

先为已保存笔记补评论，再继续原搜索/作者/附件阶段。评论读取 `.comment-item` 的稳定DOM ID、可见文字、公开作者主页链接中的用户ID和父评论ID，按原分页事务同时写 `acquisition_comment`、`xhs_note_comment` 与断点。`xhs_note_comment.content/user_id` 可直接查询。

旧保存链接跳到普通404页面时，只允许一次正常标题搜索查找同一笔记ID对应的可见公开链接；找不到则记 `xhs_web_note_not_visible_at_saved_link` 缺口，不能用相似标题的其他笔记替代，也不能据此断言笔记已删除。找到的公开链接另存web_link断点。此查找不会在验证或访问限制后触发。搜索卡片支持实测的 `/search_result/<id>` 新链接；等待同一页面完成渲染，不通过反复导航获取结果。

仅点击“展开…回复/查看更多回复”等公开阅读控件，不点击发布回复、点赞、关注等操作。恢复时重新打开原笔记并按断点重现滚动位置；原主键去重。只有页面明确无评论、已显示全部或实际读取数量达到页面显示评论数时才结束当前可见列表。列表停滞但没有完整结束证据时记录partial缺口，不报告完整成功。断点标记 `scope=public_web_rendered_comments_and_replies`、`all_backend_comments_verified=false`，不声称读到后台所有评论。

## 暂停和缺口

网页出现登录要求、验证码、访问限制，或网页自身的原生请求返回401/403/406/412/429/461/471，立即暂停。搜索/详情页面结构变化设置requires_user_action，不允许监控自动反复请求。此模式不会自动解除新发生的访问限制，不提供图片/滑块/点选验证码绕过能力；原授权的短信辅助仍仅在实际短信表单出现时按原规则运行。

作者列表只能证明网页当前公开默认排序，尚未严格证明“最新发表10条”；对应作者断点标记partial_available、recency_verified=false，最终验收要保留此缺口，不能将其算作最新10条全部成功。

附件只使用页面公开图片地址、视频poster及可直接访问的HTTPS视频地址。blob/MSE视频不破解或回放私有接口，记录video_not_exposed_as_public_https缺口。正文/评论原始公开DOM字段保存在raw；相对显示日期按原样保留，不虚构精确时间。Cookie依旧只在本机内存。

测试 `tests/test_xhs_web.py` 包含DOM字段、父子回复、字面文本保真、分页停滞、真实限制传播、监控恢复模式保持；`XHS_WEB_TEST_CDP=1` 使用自有about:blank合成页测试，避免操作用户页面或发平台请求。

2026-10-16 15:32生产验收：网页进程29311从原任务继续，实际向 `xhs_note_comment` 写入30条评论（20条回复），用户ID、正文和回复关系与原始表一致，均无缺失；这与前面的16条可行性样本分开统计。原任务已保存一条正常网页搜索观察到的旧笔记有效公开链接，受控操作未主动重放edith API。数量会随运行增长，最新验收见 `data/xhs_web_rollout_20261016.json`。作者排序与视频缺口仍按上述边界保留。
