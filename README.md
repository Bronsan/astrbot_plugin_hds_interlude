# HDS Interlude / 幕间系统 · AstrBot 移植版

> 聊天在幕前发生，生活在幕间继续。

HDS Interlude 是一个**时间感知的持续叙事聊天框架**的 AstrBot 移植版本。
上游为 Koishi 插件 [`koishi-plugin-hds-interlude`](https://gitee.com/MomoiCore/HDS-Interlude)（MomoiCore，AGPL-3.0，本移植基于其
`1.0.1-rc57` 源码包）。装上以后，你的角色不再"收到一条消息就回一条"，而是**一直在过自己的日子**：
你发过去的消息只是这段时间里发生的一件事，她可能马上看见，也可能没看见、不想回、正忙着、晚点再回。

- 上游：Koishi 插件 `hds-interlude` `1.0.1-rc57`（AGPL-3.0）
- 本移植：AstrBot 插件 `astrbot_plugin_hds_interlude` `v0.1.2`，AstrBot `>=4.16,<5`
- 依赖：`httpx`（模型请求）；数据存储使用 Python 标准库 `sqlite3`（无需额外数据库服务）

---

## 版本与本次更新（v0.1.2 / 上游 rc57）

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

1. 把本目录整个放进 AstrBot 的插件目录：

   ```text
   AstrBot/data/plugins/astrbot_plugin_hds_interlude/
   ```

2. 在插件目录里安装依赖（或让 AstrBot 的插件管理器自动安装 `requirements.txt`）：

   ```bash
   pip install -r requirements.txt
   ```

3. 重启 AstrBot 或在 WebUI「插件」页重载插件。

### 方式二：从链接安装

把仓库根目录地址填进 AstrBot 后台「插件管理 → 从链接安装」。本仓库根目录**就是**插件目录，`metadata.yaml` 在根上。

## 3. 快速开始（三步）

1. **配模型**：打开插件配置的「模型中心」`model`，把 `providersJson` 填成一段 JSON 数组（见下节示例），给连接勾上 `useForMain`（主叙事）。初次部署至少配一条主叙事连接；想要后台压缩与情绪分析，再勾上 `useForCompaction` / `useForAlter`。
2. **配故事**：「故事档案」`storyDefaults` 填主角名、角色设定、世界设定、时区。
3. **说第一句话**：「QQ 接入」`onebot` 里把 `enabled` 打开、`userAccountsJson` 加上你的 QQ，然后私聊机器人；或在 `runtime` 打开 `autoCreate`，第一次私聊自动建故事。

故事跑起来后不用管。后台每 5 分钟扫一遍：补写生活、兑现到期意图、投递延迟消息、整理记忆、审查日程。

常用检查：

- `interlude.doctor` —— 启动前体检（配置、白名单、时区、主模型）
- `interlude.status` —— 故事状态 + 运行健康指标
- `interlude.context` —— 当前场景、关系、Overlay 与长期事实

## 4. 配置说明

配置页共 22 个分组，与上游 Console 的 21 组一一对应（`mainPromptOverride` 为移植版新增的顶层主提示词覆盖）。
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
| `forward-message.ts` 合并转发读取 | 预留（需 aiocqhttp 平台层调用 `get_forward_msg`） | 未迁移 |
| `share-sync.ts` 分享码同步 | `core/share_sync.py` | 简化（分区差异预览为字段级；18 个分区含 `weather`） |
| `endpoints.ts` 端点注册表 | `service.py` 端点子集 + 表 | 简化 |
| `qzone.ts` QQ 空间 | 命令保留，通道探测返回不可用 | 未迁移（依赖外部扩展） |
| `browser` 网页观察（Puppeteer） | 配置保留，行为未接入 | 未迁移 |
| `world-seeder.ts` 世界事件播种 | `service.py` `world_seeder_sweep` | 完整（简版提示词） |
| `weather.ts` 运行时天气 | `service.py` `weather_sweep`（Open-Meteo） | 完整 |
| `long-arc.ts` 长线指导 | 配置保留 | 未迁移（实验特性） |
| `urge.ts` 弹性推进 | 固定间隔调度 + `urge.enabled` 保留 | 简化（调度沿用 runtime 固定间隔） |
| `chatRhythm` 节奏反定型 | — | 未迁移（上游已弃用） |
| 实验性流式首条回复 | — | 未迁移 |
| 模型特化块（specialization） | 配置未暴露 | 未迁移 |
| 表情包 / 原生表情发送 | 配置与账本保留，投递未接入 | 简化 |

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
`mainPromptOverride` / `model.mainPrompt` 等配置可用于补充自定义指令。

### 6.3 AstrBot 原生能力利用

- 消息与事件：`AstrMessageEvent`（`get_sender_id` / `get_group_id` / `get_self_id` / `message_str` / `get_messages`）。
- 主动与延迟投递：`context.send_message(unified_msg_origin, MessageChain)`（分段投递带打字模拟与抖动）。
- 图片/语音采集：消息组件 `Image` / `Record`；`vision.enabled` 时以 data URI 交给多模态主模型（图片不落库）。
- 数据目录：`StarTools.get_data_dir()`（`data/plugin_data/astrbot_plugin_hds_interlude/`），卸载重装不丢数据。

## 7. 无法迁移 / 需要你额外提供的部分

| 项目 | 说明 | 配置方式 |
| --- | --- | --- |
| **模型 API Key** | 主叙事必须至少一条 OpenAI 兼容连接；压缩 / Alter / Embedding / 识图可选 | 「模型中心」`providersJson`（第 4.1 节） |
| **QQ / OneBot 连接** | 私聊白名单与机器人账号校验依赖平台侧配置 | 「QQ 接入」`onebot.*`；平台本身用 AstrBot 的 aiocqhttp 适配器接入 |
| **QQ 空间通道** | 上游经 SnowLuma 扩展动作发说说/评论/点赞。本移植的平台层探测恒为不可用；`interlude.qzone` 会明确提示 | 需要自行实现 `PluginTransport.qzone_available` / `qzone_execute`（对接支持空间动作的适配器） |
| **合并转发读取** | 需要 aiocqhttp 客户端调用 `get_forward_msg`；当前未接入 | 在 `main.py` 的 `PluginTransport` 中扩展（`event.bot.api.call_action`） |
| **网页观察（浏览器）** | 上游需要 Koishi Puppeteer 服务；AstrBot 无内置等价物 | 未接入；如需可对接外部浏览器服务后实现 `browser` 分支 |
| **语音转码** | 上游经 SnowLuma 把 QQ SILK 语音转码后交给模型。本移植直接读取 `Record` 组件的文件/URL，SILK 原样无法被模型理解 | 平台侧先转码（mp3 最稳），或将语音留作文字占位 |
| **Embedding** | 可选。留空 `endpoint` 时从 Chat 地址推导 `/embeddings` | 「模型中心」勾选 `useForEmbedding` 的连接 |
| **typ0-site 分享站** | `preset.*` 命令默认访问 `https://typ0.space`；自建站可用 `--base` 参数或 `HDSI_SHARE_BASE` 环境变量 | `presetSync.baseUrl` |

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
python tests/smoke_test.py           # 核心层 82 项：时间/意愿门/Alter/日程/剧本引擎/调度/投递三态/rc57 Overlay/服务端到端
python tests/plugin_harness_test.py  # 入口层 31 项：命令派发/确认流/消息中间件/失明模式/回执超时/rc57 配置面（AstrBot 桩）
```

## 11. 许可与致谢

- 本移植基于 **MomoiCore 的 HDS Interlude**（Koishi 插件，AGPL-3.0）制作：
  上游仓库 <https://gitee.com/MomoiCore/HDS-Interlude>，本移植基于其 `1.0.1-rc57` 源码包。
- 本插件同样以 **AGPL-3.0** 发布（见 `LICENSE`）。移植过程遵循上游许可：保留版权与许可声明、公开完整源码。
- 框架层映射部分参考了 AstrBot 官方插件文档与社区中同类移植项目的公开实现思路；
  命令语义、数据协议、阈值与算法参数均以上游源码为准逐项核对。
