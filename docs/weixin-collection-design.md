# 微信公众号文章采集设计

状态：已按独立入口实施并完成真实 Chrome 小样本验证：1篇正文入库、6张图片下载，33项测试通过。实现和操作说明见 [使用说明](WEIXIN.md)。

实施时的兼容调整：用户要求保护正在运行的四平台采集。实际采用 media_platform.weixin 独立入口和独立监控，不修改共享 PAGES、worker、launch、supervisor。按用户后续命名要求，微信内容、作者、来源、附件、任务和断点统一存储在10张 wxarticle_* 表，旧微信数据通过事务迁移；其他平台保持原有表结构及读写。

目录按项目现有分层组织：media_platform/weixin 负责采集、解析、异常和监控；model/m_weixin.py 定义并实际承载文章、公众号和资源数据结构；store/weixin 负责全部 wxarticle_* 读写、表结构和迁移。collection 下只保留旧模块兼容转发，保护运行中的任务与旧监控调用。

## 1. 方案选择与已核实能力

采用 Python 原生适配器，复刻 OpenCLI 的页面提取原理，复用本项目 Chrome CDP、请求限流和 PostgreSQL 存储。

参考源码（2026-10-15 查阅 main，实施时记录参考提交版本）：

- [weixin/search.js](https://github.com/jackwener/OpenCLI/blob/main/clis/weixin/search.js)：访问搜狗微信搜索，读取结果卡片；支持关键词、分页，每次最多返回 10 条；识别验证与空结果。
- [weixin/download.js](https://github.com/jackwener/OpenCLI/blob/main/clis/weixin/download.js)：打开已有微信文章链接，从 DOM 提取标题、公众号、发布时间、正文 HTML、图片，导出 Markdown。
- [命令清单](https://github.com/jackwener/OpenCLI/blob/main/cli-manifest.json)：微信包含 search、download 及草稿相关命令，未提供公众号完整历史文章枚举命令。

README 的 Platform=weixin / Content Types=Articles 是能力分类，不是一条采集命令。项目可采用 platform=weixin、content_types=[articles] 作为自己的配置约定；写入通用表时统一 content_type=article。

不直接依赖 OpenCLI 的原因：本项目已经具备 Python、Playwright CDP、asyncpg；直接调用下载命令仍需补齐原始内容入库、逐请求限速、附件断点和平台互斥，原生适配更容易统一管理。若移植源码片段，保留来源和适用许可证声明。

## 2. 首版范围

支持两种入口：

1. 文章 URL 列表：采集公开可访问的 mp.weixin.qq.com 文章。
2. 关键词列表：通过搜狗微信搜索发现文章，解析真实文章链接后进入同一条采集流水线。

采集标题、正文、公众号公开身份、署名作者、发布时间、原文链接、图片及页面公开暴露的其他附件信息。正文保存 HTML、纯文本及派生 Markdown；不摘要、不做相关性判断或业务筛选。

首版图片支持下载；音视频和其他附件保留公开引用及处理结果，仅在可直接访问且下载能力受支持时下载，不承诺解析所有微信媒体播放链路。阅读量、点赞、评论等字段仅在页面公开且实际可见时记录；缺失为 null，不填 0。

公众号“最近 10 篇”不自动继承其他平台配额。搜狗搜索命中不等于公众号完整历史，也不能保证最新排序。历史列表采集作为独立后续能力，需要先验证用户可访问的实际入口。

可以复用现有关键词作为显式任务输入；每词目标数量是目标上限，不是结果保证。仅正文成功入库的去重文章计入完成量，索引不足、链接失效和访问限制分别报告。

## 3. 运行结构

关键词/URL → 待采集任务入库 → 搜索/链接解析 → 文章提取 → 内容与作者入库 → 附件任务 → 断点及结果统计。

- 微信为第五个独立进程，通过 media_platform.weixin 独立入口运行 WeixinJob。
- 连接已配置的本机 Chrome CDP 端口，仅创建和操作该进程自己的一个页面；搜索、跳转、文章读取在该页串行完成。
- 复用 RequestGate：搜索、主动跳转、文章访问、每个附件下载、重试均经过同一把锁；完成或失败后等待至少 30 秒，沿用 30–45 秒间隔。
- 一次浏览器导航自身的资源加载与自动重定向属于该次导航；额外主动导航和下载必须单独受控。导航前保存来源信息，最终目的地须验证。
- 重启仍遵守上次请求结束后的等待时间；获取平台数据库互斥锁后才连接页面并开始网络操作，避免重复进程先发请求。
- Cookie 仅在本机内存/Chrome 会话中使用，不打印、不写文件、不入库。不保存 storage_state、请求头、响应 Set-Cookie、全量浏览器存储或网络抓包。
- 不整体序列化页面脚本或全页 HTML。保存未经业务处理的正文 DOM、搜索卡片 DOM、公开元数据及附件引用；会话参数只在内存使用，持久化 URL 去除身份凭据。日志只记录任务 ID、阶段和固定错误码。

## 4. 文章提取与身份

- 标题：兼容 #activity-name、#js_text_title、.rich_media_title。
- 公众号：兼容 #js_name 等模板；署名作者单独记录，不与公众号混用。
- 正文：先读取 #js_content 原始结构，再在副本上处理懒加载图片、代码块和 Markdown 转换，原始正文不被转换逻辑覆盖。
- 发布时间：保存原始显示文本、可获取的公开时间戳和解析结果；时间不完整时记录精度，不虚构时分秒。展示时区 Asia/Shanghai。
- 先检测验证、删除、不可访问等页面状态，再判断正文是否成功；缺少正文但无明确提示视作解析失败，不能当作空文章成功。
- 长链接优先用公开的 __biz + mid + idx 标识文章；缺少这些字段时使用规范文章 URL 的 SHA-256。短链接与最终 URL 建立别名映射，后续获得完整身份时在事务中归并记录及关联。
- 不用标题、公众号昵称或正文相似度去重。不同公众号转载保持独立记录。
- 公众号优先使用公开 __biz 或页面明确给出的稳定账号标识；拿不到稳定标识时保留原始昵称，author_id 为 null，不按同名公众号合并、不伪造主页。

## 5. PostgreSQL 存储

继续使用当前配置的 PostgreSQL，微信公众号使用独立的 wxarticle_* 表：

| 表 | 微信数据 |
| --- | --- |
| wxarticle_run | 独立 run_id、实际启用平台、入口模式、关键词/URL 输入、配额、限流参数、解析版本 |
| wxarticle_content | platform=weixin、content_type=article、稳定 ID、标题、纯文本、公开来源 URL、时间、raw JSONB |
| wxarticle_author | 有稳定身份的公众号及公开资料；署名作者保留在文章 raw 中 |
| wxarticle_link | 各关键词命中关系，不因文章去重而丢关联；URL入口保存在微信来源表 |
| wxarticle_media | 图片/其他资源的来源、文件路径、大小和结果；类型与校验值保存在微信媒体扩展表 |
| wxarticle_progress | 搜索页断点、阶段进度、暂停原因和汇总状态 |

raw JSONB 保存 article_html、article_markdown、metadata_raw、media_refs、extracted_at、parser_version。搜索卡片原文、公开发现链接和页码另保存在 wxarticle_source.evidence；后续正文更新不覆盖发现证据。

任务、来源和媒体元数据也统一采用 wxarticle 前缀，不修改运行中的共享表结构：

1. wxarticle_source：run_id、source_key、candidate_key、content_id、evidence；source_key 区分 search 和 seed，保留URL输入及搜索卡片。
2. wxarticle_task：run_id、task_key、kind、payload JSONB、status、attempts、error_code、updated_at。搜索页、候选 URL、文章及附件均有可恢复任务；未能取得正文的候选也入库。
3. wxarticle_alias：alias_key、content_id，保证短链与公开身份归并到同一文章。
4. wxarticle_media_detail：content_id、stem、media_type、sha256、status、error_code。普通链接为 reference；未实现的媒体类型为 unsupported。原共享表字段和旧平台的按位置写入方式保持兼容。

事务规则：搜索页的候选任务保存成功后才能推进页码；文章内容、作者、来源关联、别名和附件待办写入同一事务后才标记文章任务完成。进程崩溃后重新领取未完成任务，唯一键和 upsert 保证重复执行不重复增加内容。

原始正文和结构化信息必须存入 PostgreSQL，不以本地 JSON/Markdown 文件代替数据库。图片等二进制附件沿用当前本地文件存储，数据库记录路径、校验值和结果；若后续要求二进制也进数据库，再单独调整存储策略。

## 6. 错误、暂停与监控

任务状态：pending → running → succeeded / failed / unavailable / blocked。文章成功与附件完整度分别统计。

- 验证、环境异常、登录要求、频率限制：保存断点，标记 blocked 和 requires_user_action；监控器不得自动重启试探。用户解决限制后显式恢复。
- 删除、链接失效：记录 unavailable，不占正文成功配额，不反复重试。
- 临时超时、网络故障：限次重试，每次仍遵守限流；耗尽后记录失败及可恢复任务。
- 页面结构变化：记录 parse_error 并暂停对应阶段，避免把解析失败误记为没有结果。
- 附件失败：保留已入库文章和附件缺口，恢复时只补未完成附件；访问限制仍触发平台暂停。
- 结束状态分 completed、completed_with_gaps、blocked；展示发现量、正文成功量、失败量、附件成功量及每词差额。

现有 supervisor 遍历 PAGES，且会启动未运行的平台。因此本次保持 PAGES 和旧监控器原样；微信通过 --monitor 启用独立监控，只管理显式启动的微信任务。微信采用新的 run_id 和输出目录，不加入既有四平台任务。

## 7. 代码接入点与实施顺序

已新增微信采集、提取、任务存储和独立监控模块，以及 store/weixin 中的记录存储、表结构和迁移模块。微信表迁移受平台锁保护：事务内复制、逐行校验，再移除旧位置的微信记录；异常整体回滚。旧平台入口、监控器、配置和共享数据库模块均未修改。

阶段一：文章 URL → CDP 页面 → 原始正文/公众号 → PostgreSQL → 图片与断点。完成正常图文、新模板、代码块、懒加载、失效文章和验证页验证。

阶段二：接入搜狗关键词搜索、搜索结果持久化、跳转链接解析、跨关键词去重、配额与不足统计。验证搜狗链接能在当前 Chrome 环境正常到达文章页；这是独立于 OpenCLI 搜索输出的待实测环节。

阶段三：监控接入及故障恢复验证：请求间隔含失败和附件均不少于 30 秒；重复启动被平台锁阻止；入库中断可恢复且不丢来源；验证后无自动重启；Cookie 不进入日志、文件和数据库；用户已有标签页不被操作。

上线验收以数据库记录与断点恢复结果为准。本机样本发现10条候选，3条已删除、1篇成功、6条达到样本目标后暂缓；成功文章6张图片均下载并通过SHA-256核对。一次媒体超时后按限流重试成功。原四平台及监控器PID保持不变，原有6个Chrome页面全部保留，样本专用页已关闭。
