# 快手、微博、贴吧采集

任务 `mit_ai_additional_20261015`，目录 `data/mit_ai_additional_20261015`；三个独立进程，Chrome CDP已有登录会话，各自新建页面，不操作用户已有页面。每个平台单并发，请求完成后至少15秒（含失败及媒体下载），Cookie只在内存。

沿用27关键词、每词100条搜索内容、对应作者最近10条公开作品和附件。先覆盖每词5条，再轮转补100条，最后补作者作品及附件。数量以PostgreSQL实际关联为准，结果不足留缺口；不把搜索摘要当作成功详情。作者作品按接口发布顺序及返回页时间排序，平台限制或公开数量不足须记缺口。

`collection.additional` 为现有任务工作进程的适配层：快手使用原生客户端搜索/作品REST与详情GraphQL；微博使用原生客户端搜索、详情和作者列表；贴吧使用原生PC接口签名和端点，直接保留公开帖子、作者和图片原始字段，避免教学版提取器丢失作者路由。新任务的全部原始内容和公开作者在 `acquisition_content` / `acquisition_author`，关键词与作者作品在 `acquisition_link`，附件在 `acquisition_media`，断点在 `acquisition_progress`。同时兼容原生平台内容表，昵称不额外掩码。

快手附件为封面和视频；微博为图片、视频及转发原文媒体；贴吧为正文图片，未实现的视频附件不得宣称已下载。附件路径 `<任务目录>/<平台>/media/<内容ID>/`，平台ID为 `ks`、`wb`、`tieba`。

启动/恢复指定平台：

```sh
.venv/bin/python -m collection.launch --run-id mit_ai_additional_20261015 --output data/mit_ai_additional_20261015 --platform ks --platform wb --platform tieba --allow-session-memory --resume
```

监控：

```sh
.venv/bin/python -m collection.supervisor --output data/mit_ai_additional_20261015
```

监控只恢复清单配置的平台及原run_id，遇登录、验证、访问限制暂停。与原四平台监控、微信监控分别持有目录锁，平台级PostgreSQL锁防重复采集。不得为解除真实验证而自动重试。

## 贴吧采集时间

`tieba_note.add_ts` 为首次采集的Unix毫秒时间戳；刷新已有帖子保留该值，`last_modify_ts` 继续表示更新时间。数据库有默认值、非空约束和`tieba_note_add_ts_idx`索引。2026-10-15将历史缺失/非正值按当天迁移时刻回填，不能将此回填值解读为准确的历史首次采集时间。幂等迁移在`database/migrations/20261015_tieba_note_add_ts.sql`。
