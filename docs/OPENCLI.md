# OpenCLI 采集接入（2026-10-16）

最新范围更新：LinkedIn人员和脉脉人才已作为新增平台，真实样本分别入`linkedin_person`、`maimai_person`；用户当前仅调试流程，均未继续批量采集。脉脉推荐暂不实现。下文帖子任务为历史记录，不能作为恢复授权。

小红书最新复测：MediaCrawler原生搜索、正文、评论接口均返回HTTP200，搜索20条、一篇正文342字/7张图片、一页10条主评论及1条内嵌回复（仍有后续页）。没有触发备用条件，因此本轮未调用OpenCLI或启用网页方案，也未恢复旧采集任务。此结果只验证样本链路，未验证完整评论、附件下载或长期稳定性；报告`data/xhs_recheck_20261016/native_detail_comments_result.json`。OpenCLI的小红书search/note/comments仍属于DOM读取，不是另一套独立数据API。

用户确认新增脉脉、LinkedIn仍采集27关键词帖子/文章、作者公开作品、评论和附件，不能用人才或职位记录替代。

## 安装与版本

项目内隔离安装官方 `@jackwener/opencli@1.8.8`：

```sh
npm install --prefix data/opencli_runtime --ignore-scripts --no-audit --no-fund --save-exact @jackwener/opencli@1.8.8
node data/opencli_runtime/node_modules/@jackwener/opencli/dist/src/main.js --version
```

Node须>=20.18.1。版本1.8.8已实测，无全局安装、无需新增浏览器扩展。禁用上游安装脚本，保留package-lock和完整性信息于data/opencli_runtime。

审阅源码commit `9f836ca423bee23e982c695b091b2b544b3702e8`（源码与npm功能通过本地调用测试复核）。上游Apache-2.0；项目通过动态导入使用其适配器，不复制安装包到源码目录。

## 浏览器和请求边界

`collection/opencli_bridge.mjs` 调用安装包的只读命令，IPage通过stdin/stdout匿名管道连接 `collection/opencli_adapter.py`。管道只在进程内存，不作为日志。

- 不调用上游默认CDP目标选择器，不连接用户既有Tab，不执行stealth注入，不启用Browser Bridge或上游命令重试。
- 每平台使用本项目新建Chrome CDP页面，沿用RequestGate；导航、脚本及滚动请求结束后至少15秒，固定渲染等待，不添加随机等待。
- Cookie仅由自有页面读取，传输于本机进程内存。适配器输出及异常均不记录Cookie/签名/验证码。
- 真实HTTP/页面访问限制或登录步骤立即暂停；不在限制后切换适配器、域名、会话或网页继续尝试。
- 调用白名单限制为只读命令。用户任务只使用帖子路径，不运行人才/职位搜索。

## 实际能力和未完成部分

| 平台 | 上游实际能力 | 本项目状态 |
|---|---|---|
| 小红书 | search、comments主要读取DOM | 可以作为正常网页提取组件；当前用户暂停持续有效 |
| Rednote | 复用小红书解析，访问rednote.com | 国际站候选，尚无真实数据验证；不能作为461后的自动换域路径 |
| 脉脉 | 仅search-talents，招聘人才API | 用户要帖子，不能复用来充配额；社区目前要求登录，内容适配器仍待登录后开发和实测 |
| LinkedIn | search是职位；posts是作者动态；people-search是人员 | 已接入动态解析；关键词帖子search-posts是本项目扩展，非上游现成命令；当前账号要求添加手机号，未通过真实内容验证 |

LinkedIn补充读取代码保留公开正文、原始可见文本、公开作者URL、评论及父子关系；评论仅接受页面实际暴露的原生comment URN，临时ember DOM ID不能冒充评论ID。缺失ID、未暴露、分页停滞均记缺口。原始表、原生评论表与分页断点同事务；原生表为`linkedin_comment`、`maimai_comment`，字段`content/user_id`。新增表为空不能称已采到评论。

图片与直接公开视频URL经过原限流下载器入库，失败与blob媒体单独保留缺口。作者默认排序未证明最新10条时标记recency_verified=false。该适配器尚未通过真实正文/评论/附件验收，不能报告两平台已完整支持或任务已完成。

## 独立任务

run_id：`mit_ai_opencli_20261016`；目录：`data/mit_ai_opencli_20261016`。不加入原四平台或快手微博贴吧监控。

```sh
.venv/bin/python -m collection.launch --run-id mit_ai_opencli_20261016 --output data/mit_ai_opencli_20261016 --allow-session-memory --platform maimai --platform linkedin
```

只可按实际记录的run_id/platforms进行后续检查；已存活禁止重复启动。实际登录/访问限制、用户暂停和能力未就绪均设置requires_user_action，不自动重试。监控入口仍为collection.supervisor --output data/mit_ai_opencli_20261016；仅做状态检查，不能解除上述暂停。

## 小红书优先级

原生API优先；普通功能不支持或可确认非访问限制的技术故障，才评估兼容适配器。OpenCLI小红书/Rednote属于网页读取层，不能声称是两个独立API备用渠道。HTTP461/验证码/访问限制仍立即暂停；不得通过切换OpenCLI或网页规避。上游risk-control的风控重试逻辑与本项目规则不同，因此没有接入小红书生产调用。小红书原worker和状态未恢复。
