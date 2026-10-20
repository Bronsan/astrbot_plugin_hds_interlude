# HDS Interlude / 幕间系统 · AstrBot 移植版

> 聊天在幕前发生，生活在幕间继续。

HDS Interlude 是一个**时间感知的持续叙事聊天框架**的 AstrBot 移植版本。
上游为 Koishi 插件 [`koishi-plugin-hds-interlude`](https://gitee.com/MomoiCore/HDS-Interlude)（MomoiCore，AGPL-3.0，本移植基于其
`1.0.1-rc57` 源码包）。装上以后，你的角色不再"收到一条消息就回一条"，而是**一直在过自己的日子**：
你发过去的消息只是这段时间里发生的一件事，她可能马上看见，也可能没看见、不想回、正忙着、晚点再回。

- 上游：Koishi 插件 `hds-interlude` `1.0.1-rc57`（AGPL-3.0）
- 本移植：AstrBot 插件 `astrbot_plugin_hds_interlude` `v0.3.1`，AstrBot `>=4.16,<5`
- **依赖插件（必需）**：`astrbot_plugin_image_caption_cache` `v1.3.1`
  —— 必须使用仓库根目录的 **`astrbot_plugin_image_caption_cache_v1.3.1.zip`** 安装，
  不能使用插件市场的上游 1.3.0。上游补丁未适配 AstrBot 4.28+ 的 `image_ref` /
  `montage_refs` 签名，会在引用图片转述时抛 `unexpected keyword argument`；
  本仓库的 1.3.1 修复版同时兼容 AstrBot 4.16–4.29，详见 [第 2 节](#2-安装)。
- 依赖库：`httpx`（模型/网页请求）；`Pillow`、`silk-python` 由 AstrBot 自带（缺失时图片缩放/语音解码自动降级）；数据存储使用标准库 `sqlite3`

---

## 版本与本次更新（v0.3.1 / 配置页清理与 WebUI 测试接口）

本版在 v0.3.0「角色工作室」基础上做两件事：**把配置页中残留的内容字段彻底移除**，并**补齐可在普通浏览器中运行的独立测试接口**，用于验证 WebUI 与后端动作层的完整调用链。

| 修改对象 | 修改内容 | 预期效果 |
| --- | --- | --- |
| `_conf_schema.json` | 移除 `storyDefaults`、`mainPromptOverride`、`model.*Prompt`、`compaction.*Prompt`、`alterSystem.prompt` 共 10 个内容字段 | 配置页不再包含或展示任何 WebUI 入口/内容配置项，只保留连接、平台与运行参数 |
| `core/studio_api.py` | 修复 `story.apply` 对同步 `store.bind_story()` 的误用 `await` | 「应用到故事」链路可用，不再返回 500 |
| `tests/studio_test_server.py` | 新增独立测试接口：真实内容库/动作层 + 静态托管 + AstrBot bridge 注入 | 浏览器直接打开即可跑通 WebUI，无需 AstrBot |
| `tests/webui_http_test.py` | 新增 63 项 HTTP 端到端测试 | 覆盖静态资源、前端 action 覆盖、全业务链路、错误信封与重启持久化 |
| Type0 导入 | 下拉选择「手动填写 / Type0 导入」；Type0 直连分享站拉取 `storyDefaults` 并自动映射 | 一键填充角色身份与世界设定，无需桌面端 |
| 酒馆格式 | 支持 V2/V1 JSON、PNG `chara` 数据块、World Info JSON 的导入与导出 | 与 SillyTavern 生态互通 |

核心叙事与投递逻辑**不变**；现有 137 + 51 项回归、WebUI 内容层与 HTTP 端到端 63 项全部通过（见 [WebUI 章节](#webui-角色工作室)）。

v0.3.0 的「角色工作室」页面结构、字段清单、下拉交互与 Type0 映射详见下文。

---

## 版本与本次更新（v0.2.1 / 依赖声明）

本版**不改变叙事与投递逻辑**，只做发布层面的调整：把图片转述缓存插件写成显式依赖，
并同步版本号。

| 修改对象 | 修改内容 | 预期效果 |
| --- | --- | --- |
| `README.md` 开头 | 新增“依赖插件（必需）”：`astrbot_plugin_image_caption_cache` `v1.3.1`，指定使用本仓库根目录的 `astrbot_plugin_image_caption_cache_v1.3.1.zip` | 安装者不会再误装插件市场的上游 1.3.0 |
| `README.md` 第 2 节 | 安装步骤新增“先装依赖插件”，并给出必须 ≥1.3.1 的原因 | 按文档顺序安装即可避开运行时崩溃 |
| `metadata.yaml` / 本文档 / 交接文档 | 版本 `v0.2.0 → v0.2.1`（zip 文件名同步） | 元数据、文档、安装包三者一致 |

依赖版本说明：图片转述缓存插件必须使用 **v1.3.1**。上游 v1.3.0 的运行时补丁只声明到
`skip_quote_image_caption`，而 AstrBot 4.28 起调用方会传 `image_ref=`（4.29 追加
`image_is_montage=`、`montage_refs=`），因此会在处理引用图片时抛：

```text
cached_process_quote_message() got an unexpected keyword argument 'image_ref'
```

v1.3.1 改为按原函数签名动态绑定参数：4.28+ 走“已准备图片”路径，≤4.27 仍从
`quote.chain` 取图；`_decorate_llm_request` 的集合返回值也原样透传。

---

## 版本与本次更新（v0.2.0 / Type0 内置 + 平台能力补齐）

本版做两件事：把 Type0 预设导入真正内置到插件设置里，并用 AstrBot/平台官方接口
补齐此前"只能标 cancelled"的能力。

### 1）Type0 预设内置（不依赖桌面端）

- `presetSync` 现在是完整的 Type0 入口：填分享码或完整链接（`shareUrl`），插件**加载时**
  直接通过 HTTP 访问 `baseUrl`，按 revision 去重后应用配置，不需要 typ-0 桌面端或任何外部平台。
- 首次导入、版本更新、手动 `interlude.preset.sync` 共用同一条路径；失败只记日志，不影响插件启动。
- `interlude.preset.status` 可查看最近应用的分享码、revision 与分区。

### 2）用 AstrBot/平台官方接口补齐能力

| 能力 | 之前 | 现在 |
| --- | --- | --- |
| 合并转发（仅 `Forward` 引用） | 只有占位文本 | 通过平台客户端 `get_forward_msg` 二次拉取，解析 OneBot 消息段并遵守节点/层数/字符预算 |
| 群消息回应 | 无 API，账本 `cancelled` | 通过 `set_msg_emoji_like` 投递；`messageRef`/`messageId` 进入群上下文，按 `allowedReactions` 过滤 |
| 语音 SILK | 直接交给模型，无法识别 | 用 AstrBot 自带 `silk-python` 解码为 24k WAV 再送模型；缺库/解码失败自动降级 |
| 图片理解 | 只统计 `imageCount` | native 模式把图片作为 `image_url` 随主请求发送；支持 `detail`；用 Pillow 按 `maxImageDimension` 缩放 |
| 网页观察 | 配置保留、无实现 | 内置 httpx 搜索/访问、正文提取、域名白/黑名单与私网拦截、缓存表、`webContext` 注入 |
| 群回合动作 | 只有文字 | 原生表情/表情包/回应在群回合也落账并投递 |

说明：`browser.mode=allow-immediate` 仍在本回合结束后执行（结果下一轮生效）；
真正的"写剧本前即时浏览"需要两次模型调用，暂不做。

---

## 版本与本次更新（v0.1.4 / typ-0 等价修复）

AstrBot 没有 typ-0 桌面桥接，所以上游 rc53/rc56 的桌面修复不能逐行照搬。
本版把其中**可迁移的协议语义**映射到 AstrBot 原生通道；桌面壳本身仍需在 Koishi 侧更新。

| 上游 typ-0 修复 | 本移植的等价实现 | 行为 |
| --- | --- | --- |
| 宿主 `unknown` 回执被误判为确定失败 | `DeliveryUnknown` → 账本 `pending`；群消息 metadata 记 `pending` + `deliveryReason` | 私聊、群聊、平台动作都不再把"可能已送达"记成失败，也不自动重发 |
| 实时回复片段数组逗号拼接 | AstrBot 用 `MessageChain` 组件而非字符串序列化 | 结构上不存在逗号拼接问题，无需移植 |
| `replyTo`（触发消息 ID）随投递透传 | 入站记 `metadata.messageId`；`chatActions.quoteReply` 开启时首条气泡带 `Reply(id=...)`；延迟/分段意图保存 `replyTo` | 角色回复会真正引用触发它的那条消息；关掉 `quoteReply` 即恢复纯文本 |
| 桌面桥接 45s 确认超时 | `TRANSPORT_TIMEOUT_SECONDS = 30` + `_is_unknown_delivery_error` | 超时/桥接关闭统一走 `unknown`，不会无限阻塞回合 |
| 表情包回源 realpath 加固 | `_sticker_files` 用 `realpath` + `commonpath` 双侧边界 | root 内的符号链接/junction 指向目录外时直接跳过并告警 |

不适用于本移植：桌面壳 `session.send` 的数组拼接、宿主侧 `settleDelivery`、typ-0 的
`intentKey` 幂等去重（AstrBot 投递链没有该宿主层）。

---

## 版本与本次更新（v0.1.3 / 修复"无法迁移"项）

v0.1.2 的盘点把 15 项列为"未接入/未迁移"。本版优先补齐**不依赖外部服务**的部分，
并把仍然受阻的条目收敛为"只差你提供一项输入"的清单（见第 7 节）。

| 修复对象 | 原受阻原因 | 本次实现 | 预期行为 |
| --- | --- | --- | --- |
| `mainPromptOverride` | 内容层字段此前未被读取 | `narrator.decide` 在 `model.mainPrompt` 为空时读取 WebUI 内容层写入的顶层覆盖 | 设置后进入系统提示词；留空回退内置默认 |
| 群回合缓冲 | 按 `storyId` 聚合，多群互相覆盖 | 键改为 `(storyId, groupId)`；每故事一把串行锁；群刷写只等私聊回合，不等其它群 | 两个群同时说话各自合并、各自回复；顺带修复了群批次互相等待导致永不刷写的死锁 |
| 端点路由 | 只写注册表，投递固定用 `participant.channelId` | 入站登记 `umo`/账号到端点表并记录 `preferredEndpointId`；投递、分段、延迟消息统一走端点 | 同一用户换账号/适配器后，消息从最近一次真实会话发出 |
| QQ 原生小表情 | 账本有 segment，无投递 | `Face` 组件投递 + 语义→face id 映射 + `allowedNativeFaces` 校验 | 允许的表情真实发出；未登记语义标记 `cancelled` 并写原因 |
| 本地表情包 | 无素材扫描、无发送 | 扫描 `stickers.directory`、把真实 `assetId` 注入提示词、`Image`/`Record` 发送 | 模型只引用存在的素材；缺失素材 `cancelled`，不再永久 `pending` |
| 消息回应（reaction） | AstrBot 无对应 API | 检测不到 `send_reaction` 时直接记 `cancelled` + `unsupported` 原因 | 账本不再留下无法解释的 `pending` |
| 合并转发读取 | `runtime.forwardMessage` 未读 | `Nodes`/`Node` 递归展开，遵守 `maxNodes`/`maxDepth`/`maxCharacters` | 适配器已展开的转发正文进入剧本；只有 `Forward` 引用时保留占位 |
| 时间导演 | `plan_timeline` 无调用方 | 自动回合（advance / intent-due）调用并写入 `request.timelinePlan`，`carry` 回写故事状态 | 自动推进带相对时间账本；失败/关闭时无账本照常推进 |
| Urge 弹性推进 | 只有配置 | 新增 `core/urge.py`：频率预设、区间、密度半衰期、上膛/确认/burst、休息与生活节奏乘数 | `urge.enabled` 后按弹性间隔推进，不再固定 40 分钟 |
| 日志配置 | `logging` 整组未应用 | `_log` 按 `logging.level` 过滤；`logScriptPreview`/`logMessageContent`/`previewLength` 控制内容预览 | 级别与隐私开关真正生效 |
| `maxStoriesPerSweep` | 只处理 canonical story | `sweep` 按最近更新遍历活跃故事，最多 N 个 | 多故事并存时后台任务不再只服务一个故事 |
| 天气两个开关 | `autoFromLocation`/`skipFantasyWorld` 未读 | 架空世界关键词跳过；按 `story.location` 调 Open-Meteo 地理编码并缓存 | 异世界可跳过真实天气；地点变化自动解析观测点 |
| 侧端识图 | `describe_visual` 未接线 | `vision.mode=sidecar` 时把图片转成事实观察，主叙事只读文字 | 无视觉主模型也能理解图片内容 |
| Embedding 语义检索 | `embed_text/_cosine` 无调用 | `liveQuery` 时按当前用户文本加权事实排序；后台按 `backfillBatchSize` 补齐向量 | 有 `useForEmbedding` 连接时事实召回更准；无连接自动降级规则排序 |

仍受平台能力限制、需要你补充输入的条目见第 7 节（QQ 空间、浏览器、SILK 转码、
仅 `Forward` 引用的转发、消息回应 API）。

---

## 上一版更新记录（v0.1.2 / 上游 rc57）

本版把移植基线从 `rc56` 提升到 `rc57`，重点是**单场景长故事的 Overlay 演化修复**、
**投递现实投影压缩**与**上下文/重试默认值下调**。rc57 中仅涉及 Koishi 部署向导、
提示词合约自动裁剪（`specializationAutoTrim`）、cache-first 指针化与发布产物的改动不进入 AstrBot 侧。

| 更新对象 | 范围 | 预期效果与行为 |
| --- | --- | --- |
| `core/service.py` 状态演化证据 | 新增 `narrative_turn_count` / `state_patch_evidence`；script 条目 metadata 增加 `sceneId` | 普通（minor）设定演化改按**独立叙事回合数**判断，不再把单个参与者误当成 1 个回合；单场景、长周期故事也能在累计 3 个回合、跨 2 天后渐进应用。重大（major）变化仍要求 2 个独立场景（rc57 Issue #8） |
| `core/script_engine.py` 投递现实投影 | 消息片段只保留 12 个码点的 `head`，附带回执时钟 `at`、`eventKind`、`index`；默认按 2000 字符预算从新到旧裁剪 | 未确认送达的动作仍会进入下一轮提示词，但不再把整段正文重复塞进上下文；模型能看到"什么时候尝试过"，上下文更小、更接近事实 |
| `core/timeutil.py` | 新增 `format_story_clock`（故事本地 HH:mm） | 回执时间按故事时区展示，跨时区不混淆 |
| `_conf_schema.json` 默认值 | `contextEntryLimit 35→17`、`contextTimeWindowMinutes 45→6`、`maxScriptCharacters 8000→1400`、`maxMessageCharacters 2000→1500`、`narrativeRetryDelaySeconds 60→15`、`forwardMessage.maxCharacters 8000→2000` | 新安装/未显式配置的实例上下文更小、失败恢复更快；已显式写入旧值的配置完全不受影响 |
| `core/narrator.py` 群回合提示词 | 群回合补充"私聊通道"说明：私聊内容经 `crossConversationActions` 发出，`groupReply` 只负责群发言 | 群回合里出现"该私下回复某人"的情节时，模型不会再把它误写成群消息 |
| `_conf_schema.json` 配置面 | `presetSync` 移至第一组并改为【预设入口】；weather/longHorizon/qzone/维护区重新编号；修正 chatActions/browser/timelineDirector/urge 的"保留项"描述 | 配置页与上游 rc57 顺序一致；用户不会再按失真的描述以为表情包/浏览器/时间导演已生效 |

不适用于本移植的 rc57 条目：`specializationAutoTrim` 合约自动裁剪（本移植提示词本就是子集，
未暴露该开关）、`recentExchange` 指针化（本移植未启用 cache-first 载荷）、
`clearDatabase` 的端点/别名清理（本移植 `clear_all_tables` 已覆盖全部 21 张表）。
另：附带的 Koishi 一键部署向导 HTML 仍把 `contextEntryLimit/contextTimeWindowMinutes`
写成 35/45，与 rc57 插件 schema 默认的 17/6 不一致；AstrBot 部署不读取该向导，请以插件配置为准。

上一版 v0.1.1 / rc56 已包含：rc54 完整默认文风、rc56 weather 分享分区、投递三态
`delivered/failed/unknown`、rc55 回合收尾加固。

---

## WebUI 角色工作室

> 打开方式：AstrBot WebUI →「插件」→ 找到 HDS Interlude → 插件详情 → 打开可视化视图「角色工作室」。
> 视图目录 `views/studio/`；后端动作统一走 `POST /astrbot_plugin_hds_interlude/studio/action`。

### 定位与边界

- **WebUI 管理**：角色身份（角色卡）、世界书、叙事提示词、故事绑定。
- **配置页保留**：模型连接 / API Key、QQ 账号与白名单、运行节奏、记忆 / 浏览器 / 天气等运行参数、Type0 自动同步（`presetSync`）。
- 内容字段已从配置页 schema 移除，配置页不再展示任何角色/世界书/提示词入口；WebUI 内容会在每次读取配置时叠加，改动即时生效。
- 数据落在 `data/plugin_data/astrbot_plugin_hds_interlude/content.json`，原子写入；删除该文件即清空 WebUI 内容，叙事回退到内置默认值。

### 独立测试接口（无需 AstrBot）

`tests/studio_test_server.py` 用真实 `ContentStore` + `StudioAPI` 托管同一套 `views/studio/` 静态资源，并向页面注入 AstrBot bridge shim（`window.AstrBotPluginView.apiPost`），因此可以在普通浏览器里直接跑通整条调用链：

```bash
python tests/studio_test_server.py --port 8787
# 打开 http://127.0.0.1:8787/
```

- `GET /`、`/app.js`、`/style.css`：静态资源（`index.html` 自动注入 bridge）；
- `POST /studio/action`（以及 `POST /astrbot_plugin_hds_interlude/studio/action`）：与插件页一致的动作信封；
- `GET /healthz`：健康检查；
- `--data-dir` 指定内容库目录（默认临时目录），`--type0-fixture` 提供离线 Type0 预览/导入数据。

自动化验证：

```bash
python tests/webui_http_test.py   # 63 项：静态资源 + 前端 action 覆盖 + 全业务链路 + 错误信封 + 重启持久化
```

### 页面结构

| 页面 | 路由 | 内容 |
| --- | --- | --- |
| 概览 | `#/overview` | 当前角色、工作区统计、最近更新、三步引导 |
| 角色身份 | `#/characters` | 角色卡网格：筛选（全部/手动/Type0/酒馆）、设为当前、复制、删除、导出 |
| 角色编辑器 | `#/characters/:id` | 标签页「身份 / 对话 / 提示词 / HDS 映射 / 提示词预览」+ 侧栏实时预览 |
| 世界书 | `#/lorebooks` | 世界书列表；编辑器含条目表、搜索、条目抽屉 |
| 叙事提示词 | `#/prompts` | 全局文风 / 主叙事 / 压缩 / Alter 提示词，一键恢复默认 |
| 故事绑定 | `#/binding` | 默认角色与世界书、按故事覆盖、「应用到故事」 |
| 导入中心 | `#/import` | Type0、手动填写、文件导入（JSON/PNG/World Info） |

### 字段清单

**角色卡**（酒馆 V2 对齐，`data` 平铺存储）：

| 分组 | 字段 |
| --- | --- |
| 身份 | `name*`、`description`、`personality`、`scenario`、`tags`、`creator`、`character_version`、`creator_notes`、`avatar` |
| 对话 | `first_mes`、`alternate_greetings`、`mes_example` |
| 提示词 | `system_prompt`、`post_history_instructions` |
| HDS 映射 | `perspective`、`perspectives`、`supplementaryFacts`、`userProfile`、`relationship`、`world`、`supportingCast`、`location`、`style`、`timezone` |
| 元信息 | `id`、`source{type,code,title,revision}`、`createdAt`、`updatedAt`、`character_book` |

**世界书条目**：`comment`、`keys`、`secondary_keys`、`content*`、`enabled`、`constant`、`selective`、`selectiveLogic`（AND ANY / NOT ALL / NOT ANY / AND ALL）、`insertion_order`、`position`（角色设定前/后）、`probability`、`depth`、`role`、`group`、`case_sensitive`、`match_whole_words`、`use_regex`。

**叙事提示词**：`stylePrompt`、`mainPrompt`、`fixedPrompt`、`formatPrompt`、`mainPromptOverride`、`compactionMainPrompt/FixedPrompt/StylePrompt`、`alterPrompt`。

### 新建角色的下拉交互流程

「新建角色」向导顶部为来源下拉框，两档：

1. **手动填写**：填写角色名（必填）、标签、描述、性格、场景 → 点击「创建并编辑」→ 进入角色编辑器继续补全。
2. **Type0 导入**：
   1. 填分享站地址（默认 `https://typ0.space`）与分享码 / 完整链接；
   2. 点击「连接并预览」→ 后端直连分享站，返回预设标题、分享码、revision、映射表与警告；
   3. 预览面板展示映射摘要（角色名、描述字数、视角数、补充事实数、世界书条目数）；
   4. 点击「导入并设为当前」→ 生成角色卡并设为当前角色，跳转到编辑器。

### Type0 数据获取与自动填充逻辑

- **获取**：复用 `core/share_sync.fetch_share()`，按 `/api/preset/<code>`、`/api/presets/<code>`、`/api/share/<code>` 依次回退；连接类敏感字段（`apiKey`/`token`/`proxyAgent` 等）在拉取后即被清洗，不会入库。
- **映射**（`storyDefaults` → 角色卡）：

| Type0 字段 | 角色卡字段 |
| --- | --- |
| `characterName` | `name` |
| `characterProfile` | `description` |
| `perspective` / `perspectives` | `hds.perspective` / `hds.perspectives` |
| `supplementaryFacts` | `hds.supplementaryFacts` |
| `world` | `scenario` + `hds.world` |
| `supportingCast` | `hds.supportingCast` |
| `location` / `relationship` / `userProfile` / `style` / `timezone` | 同名 `hds.*` |
| 预设 `title` / `code` / `revision` | `creator_notes` / `source` |

- **世界书**：若预设含 `worldBook` / `lorebook` / `worldInfo` 分区则一并归一导入；否则提示「该预设不含世界书，可手动补充」。
- **去重**：`perspective` 会并入 `perspectives`；`characterProfile` 若与 `personality` 重叠，注入时自动去重；`world` 优先于 `scenario` 回填故事设定。

### 注入规则（运行时）

- 角色卡的 `name / description / personality / scenario / perspective(s) / world / supportingCast / location / style / timezone` 映射为 `storyDefaults`，在创建故事与每次读取配置时生效。
- `system_prompt`、`post_history_instructions`、`mes_example` 与「激活的世界书条目」作为独立块注入主叙事系统提示词；`mes_example` 明确要求不得照抄。
- 世界书按关键词/常驻激活，受字符预算裁剪（默认 6000），`position` 目前统一注入（before/after 已保留字段，供后续细分）。

---

## 1. 它和普通角色扮演插件有什么不同

- **时间是真的**。故事有自己的时区、本地时间、星期和日照；每次写作都知道"现在是周四下午四点，天还亮着"。
- **你的消息只是一件事**。它进入角色的当下，参与判断，但不主宰一切。她可以看了不回、只回一个"嗯"、说"在忙，晚点说"，也可以隔半小时才想起来。
- **沉默也是剧情**。没回不是出错，是一个被记录下来的决定；剧本里写着她那会儿在干什么。
- **投递有账本**。每条真的发出去的话都带 `commitId` / `eventId`，按气泡逐段回写 `pending / delivered / partial / failed / cancelled`。"她说过"和"她想过但没说"是两件事。
- **记忆分层**。原始剧本 → 场景/弧摘要 → 长期事实 → Overlay 设定演化 → 剧情余波。事实带证据引用。
- **日程是活的**。Schedule Preplan 在后台维护近期计划，主叙事只读未来约半天，且不会把计划当成已发生的事实。
- **Alter 情绪天气**。每轮记录一个 -5..+5 的氛围偏移，累积过阈值让侧端模型写一句氛围描述注入后续提示词；同向强化、反向消退、太淡自动清掉。
- **Agency 行动窗口**。她主动找你之前先看日程负荷、隐私、设备是否顺手；忙或不方便就先不发，过一阵带着新的生活重新判断。
- **群聊意愿层**。群里普通消息先过一个纯算法的意愿分（衰减、边际递减、阈值概率），觉得值得才叫主模型；@ 她永远直接通过（睡眠态除外）。

## 2. 安装

### 方式一：手动安装（推荐）

1. **先安装依赖插件（必需）**：在 AstrBot 后台「插件管理 → 上传安装」中选择本仓库根目录的
   `astrbot_plugin_image_caption_cache_v1.3.1.zip`，装好后确认「已启用」。
   - 必须使用该 **v1.3.1** 构建：上游 1.3.0 不兼容 AstrBot 4.28+ 的图片转述签名，
     引用图片时会抛 `unexpected keyword argument 'image_ref'`。
   - 以后升级 HDS 时不要回退该依赖；`/image_caption_cache_stats` 可确认补丁已生效
     （`quoted_message` 出现在补丁列表里）。

2. 把本目录整个放进 AstrBot 的插件目录：

   ```text
   AstrBot/data/plugins/astrbot_plugin_hds_interlude/
   ```

3. 在插件目录里安装依赖（或让 AstrBot 的插件管理器自动安装 `requirements.txt`）：

   ```bash
   pip install -r requirements.txt
   ```

4. 重启 AstrBot 或在 WebUI「插件」页重载插件。

### 方式二：从链接安装

把仓库根目录地址填进 AstrBot 后台「插件管理 → 从链接安装」。本仓库根目录**就是**插件目录，`metadata.yaml` 在根上。

> 依赖插件不能通过本仓库链接安装，仍需单独上传 `astrbot_plugin_image_caption_cache_v1.3.1.zip`。

## 3. 快速开始（三步）

1. **配模型**：打开插件配置的「模型中心」`model`，把 `providersJson` 填成一段 JSON 数组（见下节示例），给连接勾上 `useForMain`（主叙事）。初次部署至少配一条主叙事连接；想要后台压缩与情绪分析，再勾上 `useForCompaction` / `useForAlter`。
2. **配角色**：打开 WebUI「角色工作室」→「角色身份」，手动填写或从 Type0 导入一张角色卡并设为当前；需要世界观细节时再建一本世界书。
3. **说第一句话**：「QQ 接入」`onebot` 里把 `enabled` 打开、`userAccountsJson` 加上你的 QQ，然后私聊机器人；或在 `runtime` 打开 `autoCreate`，第一次私聊自动建故事。

故事跑起来后不用管。后台每 5 分钟扫一遍：补写生活、兑现到期意图、投递延迟消息、整理记忆、审查日程。

常用检查：

- `interlude.doctor` —— 启动前体检（配置、白名单、时区、主模型）
- `interlude.status` —— 故事状态 + 运行健康指标
- `interlude.context` —— 当前场景、关系、Overlay 与长期事实

## 4. 配置说明

配置页共 20 个分组，只保留连接、平台账号与运行参数等非内容项（内容型分组已迁至 WebUI）。
**注意**：角色身份、世界设定与叙事提示词等「内容」只在 WebUI「角色工作室」中维护；配置页 schema 已移除这些字段，不再展示任何 WebUI 入口或同名配置项（见上文 WebUI 章节）。
**注意**：AstrBot 配置页会把"对象行列表"（白名单、连接池）退化为字符串数组控件。
本插件为此为每类对象列表提供了等价的 `*Json` 文本域，**优先级高于行列表**，推荐直接填写：

| 文本域 | 说明 |
| --- | --- |
| `model.providersJson` | 模型连接数组 |
| `onebot.botAccountsJson` | 机器人账号数组 |
| `onebot.userAccountsJson` | 私聊白名单数组 |
| `onebot.groupChatsJson` | 群聊白名单数组 |
| `runtime.restWindowsJson` | 低频推进窗口数组 |

### 4.1 模型连接示例（`model.providersJson`）

```json
[
  {
    "label": "DeepSeek",
    "mode": "deepseek-official",
    "enabled": true,
    "apiKey": "sk-你的密钥",
    "model": "deepseek-chat",
    "useForMain": true,
    "useForCompaction": true,
    "useForAlter": true
  },
  {
    "label": "备用中转",
    "mode": "openai-compatible",
    "endpoint": "https://your-gateway.example.com/v1/chat/completions",
    "apiKey": "sk-xxx",
    "model": "glm-4.7-flash",
    "useForMain": true
  }
]
```

字段说明（与上游一致）：

- `mode`：`openai-compatible`（自定义中转，需填完整 `endpoint`）/ `openai-official` / `deepseek-official` /
  `zhipu-official` / `moonshot-official` / `dashscope-official` / `siliconflow-official` / `openrouter` /
  `gemini-openai`（官方模式自动使用官方 endpoint，只需填 Key 与模型名）。
- 用途勾选：`useForMain`（主叙事）/ `useForCompaction`（后台压缩 + 日程审查 + 时间导演）/
  `useForAlter`（情绪侧端分析）/ `useForEmbedding`（词向量检索）/ `useForVision`（侧端识图）/
  `useForWorldSeeding`（世界事件播种）。同一用途可配多条连接，配合 `failover` 故障切换。
- 计费：`priceInput` / `priceOutput` / `priceCachedInput`（每百万 tokens，可留 0）。
- 代理：`proxyAgent`（如 `http://127.0.0.1:7890`，留空直连）。
- DeepSeek 思考控制：`deepseekThinking`（`inherit` / `disabled` / `enabled`）与 `deepseekReasoningEffort`。

### 4.2 白名单示例（`onebot.userAccountsJson`）

```json
[
  {
    "qq": "10001",
    "label": "小林",
    "personId": "lin",
    "profile": "主角的大学同学，在外地工作。",
    "relationship": "大学旧友，近来重新联系",
    "enabled": true
  }
]
```

同一现实人物的多个账号可复用 `personId`；`label` 是主角对该用户的称呼。

### 4.3 群聊示例（`onebot.groupChatsJson`）

```json
[
  {
    "groupId": "123456789",
    "label": "老同学群",
    "enabled": true,
    "purpose": "大学同学闲聊",
    "characterRole": "群里的老同学，话不多",
    "responseMode": "mention-only",
    "contextLimit": 20,
    "debounceSeconds": 1,
    "cooldownSeconds": 60,
    "willingnessPreset": "normal"
  }
]
```

`responseMode` 为 `mention-only`（仅 @ 触发）或 `always`（所有消息进上下文，是否调用主模型由意愿与冷却控制）。
`willingnessPreset` 可选 `off / quiet / reserved / normal / active / eager / auto / custom`。

### 4.4 管理员

`sharedStory.managerAccounts` 填可执行管理命令的账号列表；**留空表示所有已授权用户都是管理员**。

## 5. 命令清单（46 条，与上游逐一对应）

命令名保持上游点分形式，支持三种写法（等价）：

```text
interlude.status        /interlude.status        interlude status
```

| 指令 | 权限 | 用途 |
| --- | --- | --- |
| `interlude.doctor` | 白名单用户 | 检查配置档案、白名单、时区与主模型是否适合启动 |
| `interlude.story.start` | 管理员 | 确认后从配置档案启动第一份运行中故事 |
| `interlude.init [旧名称]` | 管理员 | 兼容别名；名称参数已忽略 |
| `interlude.status` | 白名单用户 | 故事状态、运行游标、主模型与健康指标 |
| `interlude.setup <JSON>` | 管理员 | 修改当前故事基础设定（Canon） |
| `interlude.qzone <内容>` | 管理员 | QQ 空间说说（需要支持空间动作的连接，见第 7 节） |
| `interlude.story.alias [remove <别名ID>]` | 管理员 | 查看/回滚剧本别名重定向 |
| `interlude.story.endpoint [add <平台> <账号> [qq\|wechat] \| disable <端点ID>]` | 管理员 | 管理剧本的角色端点 |
| `interlude.reset` | 管理员 | y/n 确认后清空数据库并重置设定为档案值 |
| `interlude.participant.link <参与者ID> <用户ID>` | 管理员 | 把同一人的另一个号链入既有参与者 |
| `interlude.participant.unlink <端点ID>` | 管理员 | 解除一个用户端点链接 |
| `interlude.participant.endpoints <参与者ID>` | 管理员 | 列出参与者名下全部用户端点 |
| `interlude.pause` / `interlude.resume` | 管理员 | 暂停 / 恢复自动处理 |
| `interlude.advance` | 管理员 | 立即把剧本补写到当前真实时间 |
| `interlude.timeline.rebase` | 管理员 | 从当前真实时间重建自动推进时间线 |
| `interlude.timeline [条数]` | 白名单用户 | 查看近期剧本条目（默认 10，最多 30） |
| `interlude.memory [条数]` | 白名单用户 | 查看场景记忆摘要 |
| `interlude.context` | 白名单用户 | 查看活动场景、关系、Overlay 与长期事实 |
| `interlude.compact` | 管理员 | 立即整理剧本场景、事实与状态提案 |
| `interlude.script [条数]` | 管理员 | 查看跨参与者原始剧本条目（默认 20，最多 50） |
| `interlude.script.note <内容>` | 管理员 | 写入带来源标记的人工剧本注记 |
| `interlude.memory.facts [条数]` | 管理员 | 列出长期事实及编号 |
| `interlude.memory.add <范围> <内容>` | 管理员 | 人工添加高置信度长期事实 |
| `interlude.memory.forget <编号>` | 管理员 | 将事实标记为失效（不物理删除） |
| `interlude.memory.intents [条数]` | 管理员 | 查看延迟回复、提醒、承诺与剧情余波 |
| `interlude.memory.cancel <编号>` | 管理员 | 取消一条等待中的意图 |
| `interlude.memory.patches [条数]` | 管理员 | 查看设定演化提案 |
| `interlude.memory.apply <编号>` | 管理员 | 手动批准提案（立即写入 overlay） |
| `interlude.memory.reject <编号>` | 管理员 | 拒绝尚未应用的提案 |
| `interlude.overlay.clear <部分>` | 管理员 | y/n 确认后清理 character/perspective/relationship/world/all overlay |
| `interlude.overlay.status` | 白名单用户 | 查看 overlay、候选提案与快照数量（只读） |
| `interlude.overlay.compact` | 管理员 | 只合并压缩已应用的 overlay |
| `interlude.schedule` | 已授权用户 | 查看日程覆盖范围与未来约半天计划 |
| `interlude.schedule.refresh` / `.rebuild` | 管理员 | 重新审查当前日程 |
| `interlude.database.clear` | 管理员 | y/n 确认后清空 HDSI 自有数据表 |
| `interlude.purge.all` | 管理员 | y/n 确认后彻底重置所有平台的剧本与记忆 |
| `interlude.purge.platform <平台>` | 管理员 | y/n 确认后清空并归档指定平台的故事 |
| `interlude.purge.range <开始> <结束>` | 管理员 | y/n 确认后删除时间范围内的剧本与关联记忆 |
| `interlude.preset.check <分享码>` | 管理员 | 拉取分享码预设并预览差异（不写入） |
| `interlude.preset.apply <分享码>` | 管理员 | 应用分享码预设（含 revision 二次校验与快照） |
| `interlude.preset.status` | 管理员 | 查看最近一次分享码应用状态 |
| `interlude.preset.sync` | 管理员 | 手动强制同步配置里的分享码 |
| `interlude.preset.rollback` | 管理员 | 回滚最近一次分享码应用 |
| `interlude` | — | 命令入口帮助 |

推荐的管理员操作顺序与上游一致：配档案 → 加管理员 → `doctor` → `story.start`（或开 `autoCreate`）→
`status` 确认 → 私聊测试 → 用 `script` / `memory.facts` / `memory.intents` / `memory.patches` 审计。

## 6. 移植对照表（Koishi → AstrBot）

本移植把上游 Koishi 框架层的每一项都映射到了 AstrBot 等价物。
"状态"列含义：**完整** = 行为一致；**简化** = 核心语义保留、实现规模收缩；**未迁移** = 当前版本不提供。

### 6.1 框架层映射

| Koishi（上游） | AstrBot（本移植） | 状态 |
| --- | --- | --- |
| 插件入口 `apply(ctx, config)` | `Star` 子类 + `@register` + `__init__` / `terminate` | 完整 |
| `ctx.command(...).action(...)` 命令 | 消息中间件内派发（支持点分 / 空格 / 唤醒前缀三种写法） | 完整 |
| `ctx.middleware(...)` 消息中间件 | `@filter.event_message_type` 事件监听 + `event.stop_event()` | 完整 |
| `session.prompt(60_000)` 确认问答 | `unified_msg_origin` 作用域 Future + 60 秒等待 | 完整 |
| Console 配置 Schema（21 组） | `_conf_schema.json`（22 组，含 `*Json` 文本域兜底） | 完整 |
| `ctx.model.extend(...)`（21 张 minato 表） | 内置 `sqlite3` 存储（表名、字段名与上游一致） | 完整 |
| `ctx.setInterval` / 定时器 | `asyncio` 后台循环 + 调度器（keep-earliest 唤醒） | 完整 |
| `ctx.scope.update(patch)` | `self.config` 写回 + `save_config()` | 完整 |
| `ctx.logger()` / 分层日志 | `astrbot.api.logger`（盲区模式静默） | 简化 |
| `inject: { database, http }` | 不需要（自带 sqlite3 与 httpx） | — |
| `ctx.server` 路由（表情包回源） | 直接使用宿主文件/URL 投递 | 简化 |
| dispose 事件 | `terminate()` | 完整 |
| 平台会话（QQ / 微信多端点） | `unified_msg_origin` + 端点表（`interlude.story.endpoint`） | 简化 |

### 6.2 子系统状态

| 上游子系统（src/） | 本移植对应 | 状态 |
| --- | --- | --- |
| `service.ts` 编排（回合 / 排队 / 投递） | `core/service.py` | 完整（核心闭环；投递三态 `delivered/failed/unknown`；rc57 状态演化按叙事回合取证） |
| `turn-engine.ts` 短时合并与请求取代 | `core/turn_engine.py` | 完整（rc55 回合收尾加固：`end_flush` 必达） |
| `scheduler.ts` 调度状态机 | `core/scheduler.py` | 完整 |
| `time.ts` 时间锚 | `core/timeutil.py` | 完整 |
| `group-willingness.ts` 群意愿门 | `core/group_willingness.py` | 完整 |
| `alter.ts` Alter 情绪 | `core/alter.py` | 完整 |
| `agency.ts` 行动窗口 | `core/agency.py` | 完整 |
| `schedule-preplan.ts` 日程预排 | `core/schedule_preplan.py` | 完整（含 granular 候选） |
| `database.ts` 21 张表 | `core/database.py` | 完整 |
| `narrator.ts` 模型请求与提示词 | `core/narrator.py` | 简化（见下方"提示词"说明） |
| `script/*` 提交 / 账本 / 标记解析 | `core/script_engine.py` | 简化（say 标记、提交、账本、投递现实完整；SceneFrame/DialogueBurst 未迁移） |
| `script/delivery-reality.ts` 投递现实投影 | `core/script_engine.py` `delivery_reality_projection` | 完整（rc57：12 码点 head、回执时钟、字符预算） |
| `script/development.ts` 状态演化证据 | `core/service.py` `state_patch_evidence` | 简化（回合/跨日/场景证据；未实现 development 候选累积） |
| `forward-message.ts` 合并转发读取 | `main._extract_forward_text` | 完整（`Nodes`/`Node` 直接展开；`Forward` 引用经平台 `get_forward_msg` 拉取） |
| `share-sync.ts` 分享码同步 | `core/share_sync.py` | 简化（分区差异预览为字段级；18 个分区含 `weather`） |
| `endpoints.ts` 端点注册表 | `service.py` 端点子集 + 表 | 简化（入站登记 + `preferredEndpointId` 投递路由；别名仍只读） |
| `qzone.ts` QQ 空间 | 命令保留，通道探测返回不可用 | 未迁移（依赖外部扩展） |
| `browser` 网页观察 | `service` 内置 httpx 搜索/访问 + 正文提取 + 域名/私网策略 + 缓存 | 完整（deferred；allow-immediate 仍在回合后执行） |
| `world-seeder.ts` 世界事件播种 | `service.py` `world_seeder_sweep` | 完整（简版提示词） |
| `weather.ts` 运行时天气 | `service.py` `weather_sweep`（Open-Meteo） | 完整 |
| `long-arc.ts` 长线指导 | 配置保留 | 未迁移（实验特性） |
| 时间导演 | `service._plan_timeline` + `narrator.plan_timeline` | 完整（自动回合；失败降级） |
| 日志分层 | `main._log` + `logging.*` | 简化（级别 + 内容预览；无独立文件/轮转） |
| 图片理解 | `narrator` native `image_url` + `describe_visual` | 完整（native 送图；sidecar 需 `useForVision`；含 detail/缩放） |
| 语音 SILK | `PluginTransport.fetch_audio_base64` | 完整（`silk-python` 解码为 WAV；缺库降级） |
| Embedding 检索 | `service.embed_text` + 后台补齐 | 简化（`liveQuery` 事实加权；`semanticHistory` 仍保留） |
| `urge.ts` 弹性推进 | `core/urge.py`（逐条移植） | 完整（默认关闭；开启后替代固定间隔） |
| `chatRhythm` 节奏反定型 | — | 未迁移（上游已弃用） |
| 实验性流式首条回复 | — | 未迁移 |
| 模型特化块（specialization） | 配置未暴露 | 未迁移 |
| 表情包 / 原生表情 / 消息回应 | `service._deliver_platform_actions` + `PluginTransport.send_face/send_media/send_reaction` | 完整（回应走平台 `set_msg_emoji_like`） |
| 群回合动作 | `service._deliver_group_platform_actions` | 完整（原生表情/表情包/回应均落账并投递） |

#### 关于提示词

上游主叙事系统提示词约 2000 行工程化条款（含家族特化、cache-first 载荷重排、
输出恢复等）。本移植忠实还原了其中决定行为的**核心合约**：

- `LIVED_WRITING_PROMPT` 生活剧本写作方针、长度与打字规范；
- `DEFAULT_STYLE_PROMPT` 完整中文写作指引（rc54 默认文风，`model.stylePrompt` 空值兜底）；
- 固定 JSON 合约（`script` 正文在前、结构化字段在后、禁止 markdown 围栏）；
- 各阶段指令（用户消息 / 对话后续 / 到期意图 / 独立生活推进）；
- transport 指令（`interaction.seen/reply.mode/sendAt`、`<sep/>` 分段语义）；
- say 标记、记忆/意图/演化字段的语义说明；
- Agency 行动窗口、Schedule Preplan、Alter 偏移的注入条款；
- `[管理员注记]` 与 `[世界事件]` 的权威性条款。

未逐字迁移的部分（家族特化块、payload 顺序重排、恢复措辞的全部变体）不影响协议与行为边界；
WebUI「角色工作室 → 叙事提示词」中的 `mainPromptOverride` / `mainPrompt` 可用于补充自定义指令。

### 6.3 AstrBot 原生能力利用

- 消息与事件：`AstrMessageEvent`（`get_sender_id` / `get_group_id` / `get_self_id` / `message_str` / `get_messages`）。
- 主动与延迟投递：`context.send_message(unified_msg_origin, MessageChain)`（分段投递带打字模拟与抖动）。
- 图片/语音采集：消息组件 `Image` / `Record`；`vision.enabled` 时以 data URI 交给多模态主模型（图片不落库）。
- 数据目录：`StarTools.get_data_dir()`（`data/plugin_data/astrbot_plugin_hds_interlude/`），卸载重装不丢数据。
- 平台扩展：`context.get_platform_inst(...).get_client()` → `get_forward_msg`（转发）、`set_msg_emoji_like`（回应）。
- 媒体处理：AstrBot 自带 `Pillow`（图片缩放）与 `silk-python`（SILK 解码），缺失时自动降级。

## 7. 仍受阻 / 需要你额外提供的部分

本版已把浏览器、合并转发、消息回应、SILK、图片理解都做进插件；剩下这些
确实需要外部输入或属于实验特性。

| 项目 | 受阻原因 | 需要你提供 / 配置 | 配置方式 |
| --- | --- | --- | --- |
| **模型 API Key** | 主叙事必须至少一条 OpenAI 兼容连接；压缩 / Alter / Embedding / 识图可选 | 连接地址、Key、模型名 | 「模型中心」`providersJson`（第 4.1 节），勾选 `useForMain` 等用途 |
| **QQ / OneBot 连接** | 私聊白名单与机器人账号校验依赖平台侧配置 | aiocqhttp 适配器 + 机器人 QQ + 用户白名单 | 「QQ 接入」`onebot.*` |
| **Type0 分享站** | 内置导入通过 HTTP 访问 `baseUrl`，需要网络可达 | 可用的分享站地址或自建站 | `presetSync.baseUrl` / `HDSI_SHARE_BASE` |
| **QQ 空间通道** | 上游经 SnowLuma 扩展动作发说说/评论/点赞；AstrBot 无内置等价 API | 一个能执行空间动作的适配器/网关 | 在 `main.py` 实现 `PluginTransport.qzone_available/qzone_execute`；当前 `interlude.qzone` 会明确提示不可用 |
| **长线叙事（longHorizon）** | 只有配置与 `interlude_long_arc_*` 表，缺上游触发分/审查规范 | 上游触发与审查提示词，或授权按近似规则实现 | `longHorizon.*`；当前不产生行为 |
| **流式首泡** | 结构化 JSON 与增量流式冲突，需要增量解析器 | 是否接受实验性开关 | 默认关闭；当前等完整 JSON |
| **模型家族特化 / `specializationAutoTrim`** | 本移植提示词本身是上游子集 | 无（属于提示词工程差异） | 不提供该开关 |
| **历史语义召回（`semanticHistory`）** | 当前只有实时事实加权与向量回填 | 需要历史片段的回填/召回策略 | `model.embedding.semanticHistory` 暂不生效 |
| **真正的即时网页浏览** | `allow-immediate` 需要"先浏览、再写剧本"的两次模型调用 | 是否接受额外延迟与成本 | 当前 immediate 请求仍在回合后执行，结果下一轮生效 |

### 7.1 已不再需要补充的能力

以下能力现在只需要在配置页打开：

- **Type0 预设**：`presetSync.enabled=true` + 分享码/`shareUrl` + `autoUpdate=true`（加载时自动导入）；
- **原生小表情**：`chatActions.enabled=true` + `nativeFaces=true`；
- **本地表情包**：`stickers.enabled=true` + 把图片放进 `stickers.directory`；
- **群消息回应**：`chatActions.enabled=true` + `messageReactions=true` + `allowedReactions`；
- **合并转发**：`runtime.forwardMessage.enabled=true`（默认开；`Forward` 引用走平台 `get_forward_msg`）；
- **语音 SILK**：无需配置，`silk-python` 可用时自动解码；
- **图片理解**：`model.vision.enabled=true`，native 直接送图，sidecar 需 `useForVision` 连接；
- **网页观察**：`browser.enabled=true`（deferred-only，自带 httpx 与域名/私网策略）；
- **弹性推进**：`urge.enabled=true`（可选 `urge.advanced` 微调）；
- **时间导演**：`timelineDirector.enabled=true`（默认开）；
- **语义检索**：`model.embedding.enabled=true` + `liveQuery=true` + `useForEmbedding` 连接。

## 8. 与 AstrBot 其它能力的边界

- 本插件**不依赖** AstrBot 自带的 LLM 配置：模型调用走插件自己的「模型中心」（与上游行为一致），
  便于按用途路由与故障切换；如需使用 AstrBot 供应商，请在模型中心填对应的 OpenAI 兼容地址。
- 建议保持 `runtime.ignoreCommandMessages=true`，防止 `interlude.*` 命令进入剧本。
- 插件在私聊会**消费**消息事件（叙事决定沉默时也不再交给其它插件/默认 Agent），
  这正是上游的行为；如果同时跑着别的拟人插件，请注意二者不要同时接管私聊。

## 9. 数据与隐私

- 数据全部在本地 SQLite：`data/plugin_data/astrbot_plugin_hds_interlude/hdsi.db`。
- 图片与语音二进制**不会**写入数据库；只保存引用与文字事实。
- Alter 侧端描述被约束为不含姓名、引用与私聊细节（上游条款保留）。
- `interlude.database.clear` 只清 HDSI 自有表；`interlude.purge.*` 是不可逆删除，执行前均要求 y/n 确认。

## 10. 本地测试

不依赖 AstrBot 本体即可运行（核心层不导入 astrbot）：

```bash
python tests/smoke_test.py           # 核心层 137 项：时间/意愿门/Alter/日程/剧本引擎/调度/投递三态/rc57 Overlay/服务端到端
python tests/plugin_harness_test.py  # 入口层 51 项：命令派发/确认流/消息中间件/失明模式/回执超时/rc57 配置面/WebUI 路由注册（AstrBot 桩）
python tests/webui_test.py           # WebUI 内容层：角色卡/世界书/Type0/PNG/内容库/动作层
python tests/webui_http_test.py      # WebUI HTTP 端到端 63 项：测试接口与页面调用链
```

## 11. 许可与致谢

- 本移植基于 **MomoiCore 的 HDS Interlude**（Koishi 插件，AGPL-3.0）制作：
  上游仓库 <https://gitee.com/MomoiCore/HDS-Interlude>，本移植基于其 `1.0.1-rc57` 源码包。
- 本插件同样以 **AGPL-3.0** 发布（见 `LICENSE`）。移植过程遵循上游许可：保留版权与许可声明、公开完整源码。
- 框架层映射部分参考了 AstrBot 官方插件文档与社区中同类移植项目的公开实现思路；
  命令语义、数据协议、阈值与算法参数均以上游源码为准逐项核对。
