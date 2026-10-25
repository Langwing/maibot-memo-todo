# 备忘录待办 | Memo Todo

> MaiBot 插件 · id: `local.memo-todo` · 版本 1.2.0 · 作者 pankai · MIT 协议（1.2.0：WebUI 专属待办页面）

## 安装

1. 把本仓库放进 MaiBot 的插件目录（git clone 或下载解压均可，目录名随意）：
   ```bash
   cd <你的 MaiBot>/plugins
   git clone https://github.com/Langwing/maibot-memo-todo.git
   ```
2. 重启 MaiBot，日志出现「备忘录插件已加载」即成功；
3. 管理网页 → 插件管理 → 备忘录待办 → 配置，把 `whitelist` 填上你的 QQ 号（**不填谁都用不了**）。

> 🇨🇳 **国内源**：本插件在 Gitee 有国内镜像：https://gitee.com/kanan01/maibot-memo-todo （同系列的「麦麦的厨房」「衣柜」插件也都有 Gitee 镜像），GitHub 访问不便时可从 Gitee 克隆。

---

## 一、给人看的说明（使用者指南）

### 这是什么

一个备忘录插件。你用大白话告诉麦麦一件要做的事（比如"我得买牛奶"），它就帮你记下来，并在合适的时候提醒你，**一直提醒到你做完为止**。不需要你指定时间。

### 怎么用

直接聊天就行，没有命令、没有格式要求：

| 你说 |麦麦会 |
|---|---|
| "帮我记一下，我要买牛奶" | 记下来，回你一句"记好啦" |
| "可能周末要买个收纳盒"（拿不准） | 先问你"要记到备忘录吗？"，你说是才记 |
| "牛奶买好了" | 把那条待办划掉，不再提醒 |
| "收纳盒那个不用买了" | 把那条待办删掉 |
| "我还有什么没做的？" / "我的备忘录" | 列出未完成的事 |
| （到提醒时间了） | 主动来找你，像朋友顺口一提："对了，牛奶还没买哦" |

### 提醒规则（默认值，都可在配置里改）

- **第一次提醒**：没说时间的话，记下后约 4 小时；说了大概时间（"这周末前"）则麦麦自己挑合适时刻
- **之后**：隔 4 小时 → 12 小时 → 之后每天一次，直到你说做完
- **聊天时顺带提**：你来找麦麦聊天时，如果有到期的事，等几秒后顺口提一句；同一件事至少隔 2 小时才再提，不会刷屏
- **发在哪**：优先发私聊；没有私聊通道才发在你们当时聊天的群里

### 谁能用：白名单制

- 只有 `whitelist`（白名单）里的 QQ 号能用：记事、收提醒
- `blacklist`（黑名单）优先级更高：进了黑名单，白名单也救不回来
- **白名单为空 = 谁都用不了**（刚装好就是这个状态，需要先填）

**填白名单的方法**：麦麦网页后台 → 插件管理 → 本插件 → 配置 → `whitelist` 里填 QQ 号，保存即生效（无需重启）；也可以直接改服务器上的 `config.toml`。

### 在网页后台管理待办（1.2.0：专属页面）

**主入口**：网页后台侧边栏 → **备忘录待办**（独立页面）。顶部圆片切换"未完成 / 已完成 / 全部"；每张卡片可直接改内容、备注、下次提醒时间（日期时间选择器，不用算时间戳），按钮"完成"（停止提醒）/"恢复"/"删"（有确认弹窗）；底部新增栏填 QQ 号、内容、可选提醒时间。改完立即生效，不用重启。

**备用入口**：插件管理 → 备忘录待办 → 配置里的 todos 表格仍可用（同一份数据，配置表格里改 next_remind_at 需要手填时间戳，页面里是日期选择器更方便）。

### 常见问题

- **为什么麦麦不记我说的事？** 十有八九是你不在白名单里，先检查白名单。
- **提醒会漏掉吗？** 待办存在服务器硬盘上，重启 bot 也不会丢；只有你说"做完了/不用做了"或达到数量上限才会消失。
- **怎么临时关掉？** 配置里 `enabled = false` 是总开关；`chat_mention_enabled = false` 只关"聊天时顺带提"。
- **我的待办数据在哪？** MaiBot 数据目录下 `data/plugins/local.memo-todo/memo_state.json`，是普通文本文件，能直接看。

---

## 二、给 AI 看的说明（维护者文档）

> 本节面向后续接手维护的 AI 助手或开发者。写作时环境：MaiBot Host 1.x、maibot-plugin-sdk 2.x、Python 3.13。

### 文件结构

```
maibot-memo-todo/
├── _manifest.json   # 插件清单（id、版本、capabilities 声明）
├── webui.json       # WebUI 扩展页声明（页面 + queries/actions → @API 映射）
├── webui/index.html # 待办页面（自包含 HTML，maibot.query/action 桥）
├── plugin.py        # 全部逻辑 + 4 个 @API（api_page_*，单文件）
├── config.toml      # 带中文注释的配置模板（todos 表由插件/页面维护）
├── pyproject.toml   # 包元信息
├── test_import.py   # 导入自测脚本
├── test_migration.py# 旧版数据迁移自测脚本
├── LICENSE
└── README.md        # 本文件
```

### 架构与组件

单类 `MemoTodoPlugin(MaiBotPlugin)`，注册 5 个组件：

1. **4 个 LLM Tool**（`@Tool`，visibility=visible）：
   - `memo_add_todo(user_id*, content*, remind_time?, note?, stream_id?, user_nickname?)` — 记待办。`remind_time` 为 ISO-8601，缺时区时按 Asia/Shanghai 补。工具描述里写入了"确定→直接记，拿不准→先问用户"的行为约束，**修改描述会改变 AI 行为，慎重**。
   - `memo_list_todos(user_id*, include_done?)` — 查询，返回 pending + 可选最近 10 条 done。
   - `memo_complete_todo(user_id*, todo_id?, content?)` — 标记完成。`_find_todo()` 先按 id 精确匹配，再按内容双向包含匹配；匹配到多条时返回候选列表让 AI 向用户确认。
   - `memo_delete_todo(user_id*, todo_id?, content?)` — 彻底删除，参数同上。
   - user_id 由 LLM 从聊天上下文传入（与社区提醒类插件传 stream_id 的模式一致）。

2. **1 个 HookHandler**：`chat.receive.before_process`，`mode=HookMode.OBSERVE`，name=`memo_todo_track_inbound`。旁路监听全部入站消息，实现"聊天时顺带提"：提取 `message_info.user_info.user_id / user_nickname`，校验白名单 → 有到期待办 → 距上次提醒超过 `chat_mention_min_gap_minutes` → 起 asyncio task 延迟 `chat_mention_delay_seconds` 后触发提醒（延迟是为了显得自然，也避免打断正常回复流水线）。

3. **后台定时循环** `_schedule_loop`：每 30 秒扫描 `next_remind_at <= now` 的未完成待办，按 user_id 分组触发。这是"AI 自己看着办+一直提醒到做完"的兜底路径（不依赖用户先说话）。

### 提醒的发送路径（`_fire_reminder`）

不直接发消息，而是把意图交给 Maisaka（麦麦的大脑）生成自然口吻的提醒：

1. 解析目标聊天流 `_resolve_remind_stream`（`prefer_private=true` 时）：
   `ctx.chat.get_stream_by_user_id(user_id)` → 拿不到再 `ctx.chat.open_session(chat_type="private", user_id=...)`（主动建立私聊流）→ 再失败回退到待办记录时的 `source_stream_id`。
2. **先推进调度再触发**（防重入）：对每条待办 `remind_count += 1`、`next_remind_at = now + _backoff_seconds(remind_count)`，退避序列取 `remind_backoff_minutes[min(n-1, 最后一档)]`，默认 [240, 720, 1440] 分钟，即 4h→12h→每天。
3. `ctx.maisaka.proactive.trigger(stream_id, intent, reason="memo.todo.remind", metadata={user_id, todo_ids})`，intent 内含待办清单+口吻要求（自然、简短、朋友式），并提示 AI 用户说做完时调用 `memo_complete_todo`。

**防重与容错**：
- `self._inflight: set[str]`（user_id）防止同一用户并发触发（hook 路径与定时路径共用）
- 找不到可用聊天流时 `_postpone_undeliverable` 把 next_remind_at 推 10 分钟，避免 30 秒循环里反复失败
- 所有 capability 调用包了 15 秒 `asyncio.wait_for` 超时
- 白名单外的用户即使有待办数据也不会被提醒（定时循环里直接 postpone）

### 权限模型

`_user_allowed()`：blacklist 命中 → 拒绝；whitelist 为空 → 全拒；否则必须在 whitelist。Tool 和 Hook 两条入口都过这一层。

### 数据存储（1.1.0 起双通道）

**待办清单 → 插件配置** `config.toml` 的 `[todos.mXXXX]` 表（TodoItemConfig：user_id/content/note/user_nickname/platform/source_stream_id/created_at/done/done_at/remind_count/last_reminded_at/next_remind_at，dict 键=编号）。这是**单一事实来源**，两个写入通道：
- **WebUI 编辑**：配置页 todos 表格增删改 → Runner 注入新配置 + 调 `on_config_update`（插件只打日志，`self.config.todos` 已是最新）
- **插件写入**（记事/完成/删除/提醒触发推进 remind_count 与 next_remind_at/迁移）：改内存 `self.config.todos` 后调 `_save_todos_to_disk()`——tomlkit 读改写 config.toml，**保留注释与其它字段**（社区提醒类插件通用的持久化模式）。⚠️ `_fire_reminder` 推进调度后必须同时 `_store.save()` + `_save_todos_to_disk()` 两份都写，漏掉任一都会在重启后错乱。

首载迁移：config.todos 为空且数据文件有 `legacy_todos` 时搬入并写盘；之后数据文件不再含 todos。

**提醒节流状态 → 数据文件** `<data_dir>/memo_state.json`（即 MaiBot 数据目录下 `data/plugins/local.memo-todo/`）：

```json
{
  "last_mention": { "<user_id>": <epoch秒> },
  "nicknames":    { "<user_id>": "<昵称>" }
}
```

写入用 tmp + replace 原子替换。时间全部存 epoch 秒，展示时统一 Asia/Shanghai。todo_id 形如 `m` + 4 位随机小写字母数字。

### 配置模型（PluginConfigBase，全部字段会在 WebUI 生成编辑界面）

见上文"提醒规则"。注意**必须保留 `plugin` 配置节**（`config_version = "1.0.0"`），否则 Runner 报"仅加载配置管理"拒绝加载插件（踩过的坑，见下）。改配置字段后需要同步更新 config.toml 或删掉让它重新生成。

### 部署 / 更新 / 验证

```bash
# 同步到运行环境的 plugins/ 目录后重启 MaiBot（或在管理页面重载插件）
# 验证：日志应出现「备忘录插件已加载：白名单 N 人…」且「失败=0」

# 改动较大时，先做导入测试再重启（比看运行日志更快定位问题）：
python -m py_compile plugin.py
python -c "import sys; sys.path.insert(0,'.'); import plugin; plugin.create_plugin()"
```

### 踩坑记录（2026-10-25 首次部署 / 1.1.0 升级）

1. `_manifest.json` 的 `author.url` 与 `urls.repository` 是**必填**，本地插件没有仓库时可填占位网址（如文档站链接），留空会校验失败、插件注册被拒。
2. 配置模型必须含 `plugin: _PluginSection`（带 `config_version`），否则报"插件配置文件缺少 [plugin] 配置节"。
3. SDK 能力调用可直接 await；社区插件里的 `_await_target_capability` 只是超时包装，非必须。
4. `chat.get_stream_by_user_id` 返回结构可能是 `{stream: {...}}` 或流 dict 本身，用 `_extract_stream_id()` 兼容两种（`stream_id` 或 `session_id` 字段）。
5. **WebUI 编辑配置会保留注释**：Runner 的 `_save_plugin_config` 用 tomlkit 合并而非重写；但**新增配置字段**会触发整文件重写、丢注释——升级插件加字段时，把带注释的 config.toml 一起部署（模板里保留空的 `[todos]` 表头让键预先存在）。
6. 测试时 `MaiBotPlugin.config` 是只读 property，无法直接赋值；手动接线用底层字段 `p._plugin_config_instance = SomeConfig()`。
7. 单元测试要点（`test_migration.py`）：迁移后 config.toml 注释保留、`[todos.*]` 表生成、数据文件去掉 todos 且保留 last_mention、remind_count 等数值完整搬运。
8. **WebUI 扩展页（1.2.0，参考同系列厨房插件）**：根目录 `webui.json`（schema_version 2）声明页面/queries/actions，页面文件 `webui/index.html` 自包含（`await maibot.ready` 后 `maibot.query("state")` / `maibot.action("set_todo", {...})`），数据由 `@API("memo_page_state", version="1")` 等四个方法提供（ValueError 消息会显示在页面 toast 上）。页面地址 `/extensions/local.memo-todo/memo`，`placement: "sidebar"` 挂侧边栏。若声明数组参数必须带 `items` 且 `required_capabilities` 加 `"structured_arguments"`（本插件全标量参数，未用到）。

### 可扩展方向（未实现，按需取用）

- 到期未完成的"逾期"状态与更强的催促语气（现在只有 remind_count 次数提示）
- `memo_update_todo`（改内容/改时间）；目前用户想改只能删了重记
- WebUI HomeCard 展示待办列表
- 按聊天流而非按人维度的共享待办（当前是按 QQ 号一人一份）
