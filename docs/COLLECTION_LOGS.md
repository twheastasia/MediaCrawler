# 采集日志与运行状态

本轮恢复10个平台，全部Chrome CDP。脉脉推荐与旧LinkedIn/脉脉帖子任务不启动。

- `data/collection_activity.log`：跨任务的中文进度/故障汇总。自动跟进补记最新状态。
- `data/collection_resume_20261016/latest_status.json`：十平台最近一次实际进程状态、暂停原因和日志绝对路径；自动跟进更新。
- 每个平台目录的`friendly.log`：持续记录该平台进度和异常，按北京时间显示。微信在其run根目录。
- 每个任务目录的`collection_readable.log`：同任务所有平台的可读日志；微信汇总在`data/weixin/collection_readable.log`。
- 原`worker.log`、`status.json`、`requests.jsonl`保留技术细节；人员任务Chrome限流日志为`requests_chrome.jsonl`。

示例格式：

```text
2026-10-16 17:38:23 | 需处理 | 小红书 | 采集暂停 | 异常类型=AccessRestricted | 原因=HTTP 461 | 处理=暂停此平台，保留断点；登录/验证/限制不会自动重试
```

Cookie、账号密码、短信正文及验证码不进入可读日志。公开用户ID未暴露、部分评论、不可用附件等缺口保留数据库记录；总览中的`database_audit.json`核对统一评论与平台评论表的实际一致性。尚未完成的配额不当作成功。

本轮四个任务目录：

- `data/mit_ai_smallbatch_20261014`：B站、抖音、知乎、小红书（原生API）。
- `data/mit_ai_additional_20261015`：快手、微博、贴吧。
- `data/weixin/weixin_mit_ai_20261015`：微信公众号。
- `data/mit_people_opencli_20261016`：LinkedIn人员、脉脉人才，各自人员表。

普通异常按原监控恢复；新登录失效、验证、访问限制或用户暂停保留断点等待处理。小红书不因461自动换OpenCLI/网页模式；所有平台不静默换浏览器。
