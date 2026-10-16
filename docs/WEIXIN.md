# 微信公众号文章

当前单篇异常策略：账号屏蔽/注销记 skipped/account_blocked，迁移记 skipped/account_migrated（不点击“访问文章”）；文章不可用、单篇解析/加载/超时/网络异常均留原因后继续下一篇，不计正文成功。搜索入口异常、数据库故障、浏览器整体断开不视为单篇跳过；真实登录/验证码/访问限制仍暂停。

链接处理仅将显式 `&amp;` 还原为 `&`，不对浏览器已解码的 URL 再做 HTML 实体解码，防止 `&timestamp` 被误转为 `×tamp`。搜狗给出的带 timestamp/signature 链接仍可能过期；格式正确不代表永久可访问，不能仅用 biz/mid/idx 伪造长期链接。采集正文和图片保存在本地及数据库，不依赖该临时链接长期有效。

历史坏链接修复：`.venv/bin/python -m store.weixin.repair_urls` 默认事务预览，`--apply` 提交；必须在微信工作进程停止时执行。仅修复已知的时间戳实体错误，保留内容 ID、正文、关联和旧别名，补入正确链接别名；冲突则整体回滚。

使用本机已有 Chrome CDP 和当前配置的 PostgreSQL。微信入口独立于四平台 collection.launch / collection.supervisor，默认不会加入旧任务。每个微信进程只操作自己创建的页面；平台互斥锁禁止同时运行两个微信采集进程。

## 代码目录

```text
media_platform/weixin/
  core.py          采集任务与命令入口
  extractor.py     搜狗搜索及微信正文提取
  exception.py     页面解析和内容可用性异常
  monitor.py       微信独立监控
  __main__.py      python -m media_platform.weixin
model/m_weixin.py  文章、原始正文、公众号、资源、搜索结果模型
store/weixin/
  _store_impl.py   任务、来源、别名、事务与媒体结果
  records.py       文章、公众号、关联和断点读写
  schema.py        wxarticle_* 表结构
  migrate.py       旧微信表迁移
```

collection 中原有微信模块仅保留兼容转发，旧命令 `python -m collection.weixin` 和旧导入仍有效。运行中的采集与监控无需重启；新入口沿用相同任务目录、限流日志、数据库表和断点。

## 启动

先按关键词小样本采集一篇文章及其图片：

```sh
.venv/bin/python -m media_platform.weixin --run-id weixin_demo --keyword 留学规划 --limit 1 --max-pages 1 --background
```

输入已有文章链接，可以重复指定 --url：

```sh
.venv/bin/python -m media_platform.weixin --run-id weixin_urls --url 'https://mp.weixin.qq.com/s/文章短链' --background
```

使用原有27个关键词、每词目标100篇：

```sh
.venv/bin/python -m media_platform.weixin --run-id weixin_keywords --keywords-default --limit 100 --max-pages 100 --monitor --background
```

搜狗的结果范围和访问状态决定实际数量，达不到目标会记录差额。默认采图片，--no-media 只保留附件引用；音视频及其他外链记录实际支持状态。首版下载腾讯公开图片 CDN，其他资源主机标记 unsupported。

正文中的普通超链接保留为 reference 引用，不计作下载失败；带文件扩展名或 download 标记的链接归为附件。

搜索通过搜狗微信，正文从真实微信文章页提取。搜狗跳转或微信验证会暂停，绝不自动绕过。若遇验证，保留本进程的验证页面供用户手动处理；用户已有页面不会被导航或关闭。没有公众号完整历史文章/最近10篇枚举能力。

## 状态和恢复

```sh
.venv/bin/python -m media_platform.weixin --run-id weixin_demo --status
.venv/bin/python -m media_platform.weixin --run-id weixin_demo --resume --background
```

默认输出为 data/weixin/<run-id>/，含 run.json、status.json、worker.log 和媒体文件。自定义 --output 时，查询与恢复也要使用同一个目录。恢复读取原任务配置，不修改目标数量或输入；需要不同配置请使用新 run-id。

completed 表示正文与启用的附件任务完成；completed_with_gaps 含数量不足、失效链接、附件失败或不支持资源；blocked 表示验证、访问限制或页面结构变化，需要人工处理后显式 --resume。正常退出码为0，任务启动失败或暂停为2。搜索结果与正文成功数分别报告。

--monitor 为可选的微信独立监控器，最多重启3次，等待5/15/30分钟。验证、解析变化、完成（包括有缺口）不自动重启。向本微信目录 monitor_control.json 写入 {"enabled": false} 只停止自动重启，不终止采集进程。已运行的四平台监控配置完全独立。

## 数据库

微信公众号数据统一保存在以下10张 wxarticle_* 表中，旧数据和断点已通过事务迁移。

- wxarticle_run：微信任务配置和创建时间。
- wxarticle_content：platform='weixin'、content_type='article'；raw 保存原始正文 HTML、派生 Markdown、公开元数据、资源引用、提取时间与解析版本。
- wxarticle_author：有公开稳定标识的公众号；昵称与署名作者分开保存，未知身份不伪造作者 ID。
- wxarticle_link：关键词来源，role='search'。
- wxarticle_media：附件来源、路径、大小和结果。
- wxarticle_progress：阶段、搜索耗尽原因、最终数量差额。
- wxarticle_task：搜索页、文章、图片任务和恢复状态。
- wxarticle_source：原始搜索卡片和URL入口；失效文章也保留发现证据。
- wxarticle_alias：公开链接哈希到文章ID的映射。取得完整公开身份后，在事务中归并短链记录、来源、附件与待办。
- wxarticle_media_detail：附件类型、文件 SHA-256、详细结果。

微信后续读写全部使用 wxarticle_* 表。文章、作者、来源、别名、附件待办在事务中一起提交；搜索页的候选任务入库后才推进分页。恢复检查已下载文件的存在性和校验值。其他平台继续使用原有表。

从旧版本升级时，先运行迁移；可用 --dry-run 演练，最后自动回滚：

```sh
.venv/bin/python -m store.weixin.migrate --dry-run
.venv/bin/python -m store.weixin.migrate
```

迁移使用微信平台互斥锁，微信进程运行时会拒绝执行。迁移在一个事务中复制并逐行核对正文、公众号、附件、配置和断点，核对成功后移除旧位置的微信记录及旧微信专用表。失败整体回滚。共享表结构及其他平台记录保持原样；媒体文件路径不变。

本机已完成前缀迁移：1篇文章、1个公众号、10条资源记录（6张已下载图片及4条引用）、17个任务及断点完整保留。迁移后的恢复检查与图片校验通过，相关测试共36项通过。

所有主动请求和附件下载串行，按用户最新设置，结束/失败后下次请求至少等待10秒，重启沿用 data/weixin/requests.jsonl 的等待记录。仅保存公开内容；Cookie、storage_state、请求头、整页脚本不输出、不写文件或数据库。数据库保存正文；附件二进制保存本地，数据库保存路径和校验结果。

## 验证

```sh
.venv/bin/python -m pytest tests/test_weixin_collection.py tests/test_weixin_layout.py tests/test_collection_runtime.py -q
WEIXIN_TEST_DB=1 .venv/bin/python -m pytest tests/test_weixin_collection.py tests/test_weixin_layout.py tests/test_collection_runtime.py -q
```

数据库测试在独立 schema 内运行并最终回滚，不使用生产任务数据。

三层目录整理验收：48项测试通过；包括新旧模块同一实现、新旧命令状态读取、根目录和限流日志路径、监控新子进程入口、模型内容保留和数据库回归。数据库迁移测试使用专用测试锁，不争抢正式微信任务的锁。整理过程中五个平台及两个监控进程均保持原PID，微信继续处理媒体任务。

2026-10-15 验收：33项测试通过；真实任务 weixin_smoke_20261015 完成1篇文章入库及6张图片下载，图片大小和SHA-256均已核对。发现10条候选，其中3条已删除、6条达到样本目标后暂缓，删除证据及来源保留。文章、公开公众号、HTML、纯文本、Markdown、媒体结果和断点均在数据库。实测最短请求间隔30.518秒，包含下载超时后的重试。原四平台/监控器PID未变化，原有6个页面全部保留，微信专用页已结束。

2026-10-15 搜狗跳转修复：结果卡片的 `/link` 含临时路由 `token`，浏览器打开时必须保留，不能把持久化链接清理直接用于导航。实现将原始路由参数及来源页仅保存在本进程内存，数据库、日志和来源HTML继续去除token。断点恢复先刷新对应关键词/页码的搜索结果，旧桥接无法与新结果对应时保留旧证据并标记deferred，由新候选继续采集；不会凭标题合并文章。真实验证/访问限制仍暂停，不能自动绕过。已实测原生点击和修复后程序导航均可读取AI内训文章。

进一步点击修复：搜狗链接的原生点击会额外生成h/k路由信息，因此仅把原href（即使保留token）交给goto仍不等价。采集现保留一个自建搜索页，原生点击结果后在自建临时文章页中串行读取；成功/不可用后仅关闭该临时页，搜索页复用。真实验证/解析异常保留出错文章页供用户处理，并关闭自建搜索页；从不导航或关闭用户已有页面。平台仍只有一个并行受控请求，点击和附件均由同一RequestGate限制。账号迁移的官方导航参数（如source和fragment）只在内存保留完整值，持久化别名继续规范化。

2026-10-15 调度提速：微信外部受控请求仍单并发、结束后至少30秒，额外随机等待从0–15秒取消。每个搜索页正文处理后只穿插最多2个图片任务，其余待办完整保留，在正文搜索结束后继续排空，避免多图文章长时间挡住后续关键词。新建空白页不发网络请求，不再占用30秒请求等待位；第一次实际网络请求仍继承上次结束时间。scheduling断点记录实际限流和图片批次策略。其他平台的限流不变。

用户随后明确将微信公众号最小间隔从30秒改为10秒。运行时读取run.json的request_interval_minimum，数据库wxarticle_run与本机run.json已同步为10；其他四平台仍为30秒。此设置变更不解除访问验证暂停，恢复后才使用新间隔。

2026-10-15 用户再次调整：原30秒的七个平台（B站、抖音、知乎、小红书、快手、微博、贴吧）统一为15秒；微信公众号继续10秒。均从上次请求结束计算，错误及下载同样限流，调整间隔不解除验证暂停。
