# 脉脉、LinkedIn 找人（2026-10-16）

最新用户授权：恢复全部已支持平台，统一Chrome CDP。人员任务现在用`--browser chrome_cdp`，继续原run及断点，官方OpenCLI命令经本项目自有Chrome页面桥接；不再使用ego空间7。LinkedIn实际停在`/check/china/add-phone`账号步骤，脉脉Chrome页面访问受限，本轮均已暂停，不自动重试。之前的ego验证样本与数据库人员仍保留（LinkedIn328、脉脉20）。这覆盖下文早前“仅调试/停止”和ego运行方式。

```sh
.venv/bin/python -m collection.people_launch --platform linkedin --run-id mit_people_opencli_20261016 --output data/mit_people_opencli_20261016 --browser chrome_cdp --resume
```

仅用户处理本轮暂停并明确恢复后使用上述命令；脉脉换为`--platform maimai`。监控按记录的browser原样恢复，不静默切回ego。Chrome未继承ego会话时按真实登录/验证状态暂停。LinkedIn允许正常登录一次，但补手机号/验证等账号步骤仍由用户完成；脉脉不自动登录。中文日志见`docs/COLLECTION_LOGS.md`。

最新运行范围：用户说明仅需调试流程，已停止LinkedIn worker及其专属监控，保留实际入库328人；自动跟进已暂停，不再执行下述历史批量配额。用户再次确认脉脉登录后单页调试成功，人工智能关键词一页20人已写入`maimai_person`及`maimai_person_source`，姓名及公开user_id均无缺失，不启动批量worker。报告见`data/mit_people_opencli_20261016/maimai/relogin_probe.json`。

此次修复：OpenCLI脉脉适配器向evaluate传入未调用的箭头函数字符串，ego会将其作为表达式返回空对象，产生COMMAND_EXEC；桥接层现在显式调用此形式，同时保持LinkedIn已调用的IIFE原样。该协议问题不是登录失效或平台访问限制。回归检查10通过、1个数据库集成测试未启用；真实20人入库已另行核对。

用户最新将这两平台从帖子采集改为人员搜索。沿用27关键词，每词目标100个可识别的不同人员；找到的人员进入各自独立PostgreSQL表。此范围不包含帖子、作者作品、评论或附件；原有其他平台及旧帖子任务保持用户停止状态。

## 官方命令与浏览器

- 脉脉：OpenCLI 1.8.8 `maimai/search-talents`（招聘人才接口，支持page/size）。
- LinkedIn：OpenCLI 1.8.8 `linkedin/people-search`（官方人员搜索适配器，单次最多10人）。
- 经 `collection/ego_people.mjs` 调用原生适配器函数，连接ego浏览器专用空间7的p1/p2；不直连Chrome调试端口，不启用OpenCLI默认Browser Bridge或stealth。
- 每平台一个worker和页面；SDK调用轮次串行，受控导航/API/分页结束至少15秒。Cookie仅在本地进程内存。
- LinkedIn分页使用实际页面出现的“第N页/Page N”按钮并确认人员变化后继续官方解析，不能重复第一页充数。

## 登录

脉脉默认直接使用现有会话，不自动登录或重试失效token；收到登录失效/验证码/访问限制立即暂停并通知。

LinkedIn登录失效时可按用户授权从`.env`读取`LINKEDIN_USERNAME`和`LINKEDIN_PASSWORD`，在自有页面仅提交一次，确认成功后重入当前搜索一次。密码、Cookie均不输出或写入任务文件。真实人机验证、账号/手机号步骤、重新登录失败仍暂停，不自动绕过。

首次实测：脉脉调用跳转/platform/login，AUTH_REQUIRED暂停；LinkedIn已通过真实找人，首两条已入库。后续以最新数据库与status核对，不把测试或启动当成功。

## 独立表

- `maimai_person`：脉脉人员。
- `linkedin_person`：LinkedIn人员。
- 各自`*_person_source`：run_id、关键词、页码/位置、人员关联与该次原始条目。
- `people_acquisition_run`与`people_acquisition_progress`：独立人员任务配置和断点。

人员表包含user_id、name、headline/job_title、company、location、school、profile_url、raw（原始人员条目）、opencli_data（官方命令输出）、first_seen_at/updated_at。不将同名当同人。

LinkedIn的user_id为公开profile handle，非虚构数字ID。脉脉ID保留API实际暴露字段；若源没有公开身份，user_id留空，identity_kind=unresolved_fingerprint，内部去重键有明确unresolved前缀，不冒充公开ID且不计入可识别人配额。

每页人员、来源和断点同事务；重复执行去重，SQL失败整页回滚。统一人员任务不会混入旧内容acquisition_*表或评论表。

## 运行

独立run_id `mit_people_opencli_20261016`，目录`data/mit_people_opencli_20261016`。

```sh
.venv/bin/python -m collection.people_launch --run-id mit_people_opencli_20261016 --output data/mit_people_opencli_20261016 --platform linkedin --space-id 7 --page p2
```

后续恢复加`--resume`。脉脉只有用户处理失效会话后才恢复，platform=maimai、page=p1。不得恢复原mit_ai_opencli_20261016帖子任务。

`collection.people_supervisor`仅按新目录processes.json内实际启动的平台和同一run_id检查。登录/访问限制/用户暂停不重启；不把未启动的脉脉加入自动恢复。普通DB断开至多每小时3次，5/15/30分钟等待。Ego交还用户或失去控制时停止操作，不自动抢回或新建空间。
