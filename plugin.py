"""备忘录待办插件。

白名单用户可以用自然语言让 AI 记待办（例如"我得买牛奶"），
AI 通过工具把事情写入备忘录；插件会在合适的时机把未完成待办
交给 AI（Maisaka 主动任务），由 AI 用自然的口吻提醒用户，
直到用户表示做完为止。

提醒时机有两种：
1. 定时提醒：记录时 AI 可给出首次提醒时间（自己看着办），
   之后按递增间隔反复提醒，直到完成为止；
2. 聊天时顺带提：白名单用户来找 bot 说话时，如果有到期的
   待办，优先通过私聊把待办交给 AI 自然地提一嘴。

提醒发送位置：优先私聊（查找已有私聊流，没有则尝试建立），
失败时退回记录待办时所在的聊天流。

待办清单存放在插件配置（config.toml 的 [todos.*] 表）中，
可在 WebUI 插件配置页面直接查看、编辑、新增、删除；
提醒节流状态（上次提醒时间、昵称缓存）存放在数据文件
memo_state.json 中。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import string
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from maibot_sdk import API, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.config import Field, PluginConfigBase
from maibot_sdk.types import HookMode, ToolParameterInfo, ToolParamType

try:
    import tomlkit
except ImportError:  # pragma: no cover - 容器内已随其他插件依赖安装
    tomlkit = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("Asia/Shanghai")
_LOOP_INTERVAL_SECONDS = 30
_CAPABILITY_TIMEOUT_SECONDS = 15


class _PluginSection(PluginConfigBase):
    """插件元信息配置节。"""

    config_version: str = "1.0.0"


class TodoItemConfig(PluginConfigBase):
    """单条待办在配置页面中的结构。"""

    __ui_label__ = "待办"

    user_id: str = Field(default="", description="所属用户 QQ 号")
    content: str = Field(default="", description="待办内容，如：买牛奶")
    note: str = Field(default="", description="备注，可空")
    user_nickname: str = Field(default="", description="用户昵称，可空")
    platform: str = Field(default="qq", description="平台，一般不动")
    source_stream_id: str = Field(default="", description="记事时所在聊天流 ID（提醒兜底位置），一般不动")
    created_at: float = Field(default=0.0, description="创建时间戳（不用管）")
    done: bool = Field(default=False, description="是否已完成（完成后再也不会提醒）")
    done_at: float = Field(default=0.0, description="完成时间戳（不用管）")
    remind_count: int = Field(default=0, description="已提醒次数")
    last_reminded_at: float = Field(default=0.0, description="上次提醒时间戳（不用管）")
    next_remind_at: float = Field(default=0.0, description="下次提醒时间戳（不用管，改它可推迟/提前下次提醒）")


class MemoConfig(PluginConfigBase):
    """备忘录插件配置。"""

    plugin: _PluginSection = Field(default_factory=_PluginSection)
    enabled: bool = Field(default=True, description="插件总开关")
    whitelist: list[str] = Field(
        default_factory=list,
        description="允许使用备忘录的用户 QQ 号列表；为空时无人可用",
    )
    blacklist: list[str] = Field(
        default_factory=list,
        description="禁止使用备忘录的用户 QQ 号列表，优先于白名单",
    )
    prefer_private: bool = Field(default=True, description="提醒优先发送到与用户的私聊")
    chat_mention_enabled: bool = Field(
        default=True, description="用户来找 bot 聊天时，顺带提醒未完成的待办"
    )
    chat_mention_min_gap_minutes: int = Field(
        default=120, ge=5, description="顺带提醒的最小间隔（分钟），防止刷屏"
    )
    chat_mention_delay_seconds: int = Field(
        default=8, ge=0, description="顺带提醒前等待的秒数，让提醒显得自然"
    )
    first_remind_delay_minutes: int = Field(
        default=240, ge=1, description="记录待办时未指定提醒时间，默认多少分钟后首次提醒"
    )
    remind_backoff_minutes: list[int] = Field(
        default_factory=lambda: [240, 720, 1440],
        description="每次提醒后到下一次提醒的间隔（分钟），依次递增，超出档位后重复最后一档",
    )
    max_todos_per_user: int = Field(default=30, ge=1, le=500, description="单个用户待办数量上限")
    max_total_todos: int = Field(default=300, ge=1, le=5000, description="全部待办数量上限")
    todos: dict[str, TodoItemConfig] = Field(
        default_factory=dict,
        description="待办清单，以编号为键；可在下方表格里查看、编辑、新增、删除",
    )


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=_TZ).strftime("%m-%d %H:%M")


class MemoStore:
    """提醒节流状态（上次提醒时间、昵称）的 JSON 持久化存储。

    待办清单在插件配置中；旧版数据文件里的 todos 仅用于迁移。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self.last_mention: dict[str, float] = {}
        self.nicknames: dict[str, str] = {}
        self.legacy_todos: dict[str, dict[str, Any]] = {}

    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("备忘录数据文件读取失败，将忽略已有数据: %s", self._path)
            return
        self.last_mention = {str(k): float(v) for k, v in (data.get("last_mention") or {}).items()}
        self.nicknames = {str(k): str(v) for k, v in (data.get("nicknames") or {}).items()}
        legacy = data.get("todos")
        if isinstance(legacy, dict) and legacy:
            self.legacy_todos = {str(k): dict(v) for k, v in legacy.items()}

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "last_mention": self.last_mention,
                "nicknames": self.nicknames,
            }
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except Exception:
            logger.exception("备忘录数据保存失败: %s", self._path)


def _extract_stream_id(stream_result: Any) -> str:
    """从 chat 能力返回结果中提取 stream_id。"""
    stream = stream_result
    if isinstance(stream_result, dict) and isinstance(stream_result.get("stream"), dict):
        stream = stream_result["stream"]
    if not isinstance(stream, dict):
        return ""
    return str(stream.get("stream_id") or stream.get("session_id") or "").strip()


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class MemoTodoPlugin(MaiBotPlugin):
    """备忘录待办插件主类。"""

    config_model = MemoConfig

    def __init__(self) -> None:
        super().__init__()
        self._store: MemoStore | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._inflight: set[str] = set()
        self._bg_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        data_dir = Path(self.ctx.paths.data_dir)
        self._store = MemoStore(data_dir / "memo_state.json")
        self._store.load()
        self._migrate_legacy_todos()
        self._store.save()  # 迁移后重写数据文件（去掉旧 todos 字段）
        self._loop_task = asyncio.create_task(self._schedule_loop())
        logger.info(
            "备忘录插件已加载：白名单 %d 人，未完成待办 %d 条",
            len(self.config.whitelist or []),
            sum(1 for t in self.config.todos.values() if not t.done),
        )

    async def on_unload(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()
        if self._store is not None:
            self._store.save()
        self._store = None

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data, version
        if scope == "self":
            logger.info(
                "备忘录插件配置已更新（当前待办 %d 条，未完成 %d 条）",
                len(self.config.todos),
                sum(1 for t in self.config.todos.values() if not t.done),
            )

    # ------------------------------------------------------------------
    # 待办清单：存放于插件配置，支持 WebUI 编辑与插件写入双通道
    # ------------------------------------------------------------------

    @property
    def _config_path(self) -> Path:
        return Path(__file__).parent / "config.toml"

    def _migrate_legacy_todos(self) -> None:
        """把旧版数据文件里的待办搬进插件配置（仅首次）。"""
        if self.config.todos or self._store is None or not self._store.legacy_todos:
            return
        for todo_id, raw in self._store.legacy_todos.items():
            self.config.todos[str(todo_id)] = TodoItemConfig(
                user_id=str(raw.get("user_id", "")),
                content=str(raw.get("content", "")),
                note=str(raw.get("note", "") or ""),
                user_nickname=str(raw.get("user_nickname", "") or ""),
                platform=str(raw.get("platform", "qq") or "qq"),
                source_stream_id=str(raw.get("source_stream_id", "") or ""),
                created_at=_coerce_float(raw.get("created_at")),
                done=bool(raw.get("done", False)),
                done_at=_coerce_float(raw.get("done_at")),
                remind_count=int(_coerce_float(raw.get("remind_count"))),
                last_reminded_at=_coerce_float(raw.get("last_reminded_at")),
                next_remind_at=_coerce_float(raw.get("next_remind_at")),
            )
        logger.info("已从旧数据文件迁移待办：%d 条", len(self.config.todos))
        self._save_todos_to_disk()

    def _save_todos_to_disk(self) -> None:
        """把当前待办清单写回 config.toml（保留文件中其它内容与注释）。"""
        if tomlkit is None:
            logger.warning("tomlkit 不可用，待办清单未写入 config.toml（重启后需重新迁移）")
            return
        config_path = self._config_path
        try:
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    doc = tomlkit.load(f)
            else:
                doc = tomlkit.document()

            todos_table = tomlkit.table()
            for todo_id, t in self.config.todos.items():
                table = tomlkit.table()
                table["user_id"] = t.user_id
                table["content"] = t.content
                table["note"] = t.note
                table["user_nickname"] = t.user_nickname
                table["platform"] = t.platform
                table["source_stream_id"] = t.source_stream_id
                table["created_at"] = float(t.created_at)
                table["done"] = bool(t.done)
                table["done_at"] = float(t.done_at)
                table["remind_count"] = int(t.remind_count)
                table["last_reminded_at"] = float(t.last_reminded_at)
                table["next_remind_at"] = float(t.next_remind_at)
                todos_table.add(todo_id, table)

            doc["todos"] = todos_table
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as f:
                tomlkit.dump(doc, f)
        except Exception:
            logger.exception("待办清单持久化到 config.toml 失败")

    def _pending_for(self, user_id: str) -> list[tuple[str, TodoItemConfig]]:
        return sorted(
            (kv for kv in self.config.todos.items() if kv[1].user_id == user_id and not kv[1].done),
            key=lambda kv: kv[1].next_remind_at,
        )

    def _new_todo_id(self) -> str:
        while True:
            tid = "m" + "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
            if tid not in self.config.todos:
                return tid

    # ------------------------------------------------------------------
    # 权限
    # ------------------------------------------------------------------

    def _user_allowed(self, user_id: str) -> bool:
        uid = str(user_id).strip()
        if not uid:
            return False
        if uid in {str(x).strip() for x in (self.config.blacklist or []) if str(x).strip()}:
            return False
        whitelist = {str(x).strip() for x in (self.config.whitelist or []) if str(x).strip()}
        if not whitelist:
            return False
        return uid in whitelist

    def _deny_result(self, user_id: str) -> dict[str, Any]:
        return {
            "success": False,
            "error": f"用户 {user_id} 不在备忘录白名单中，无法使用备忘录功能。请委婉告知对方。",
        }

    # ------------------------------------------------------------------
    # 消息监听：聊天时顺带提
    # ------------------------------------------------------------------

    @HookHandler(
        "chat.receive.before_process",
        name="memo_todo_track_inbound",
        description="监听白名单用户的消息，当其有到期的未完成待办时，择机交给 AI 顺带提醒。",
        mode=HookMode.OBSERVE,
    )
    async def on_inbound_message(self, message: dict[str, Any], **kwargs: Any) -> None:
        del kwargs
        if not self.config.enabled or not self.config.chat_mention_enabled:
            return
        if not isinstance(message, dict) or bool(message.get("is_notify")):
            return

        user_id, nickname, platform = self._extract_sender(message)
        if not user_id or not self._user_allowed(user_id):
            return

        assert self._store is not None
        if nickname:
            self._store.nicknames[user_id] = nickname

        todos = self._pending_for(user_id)
        if not todos:
            return
        now = time.time()
        if not any(t.next_remind_at <= now for _, t in todos):
            return
        min_gap = max(5, int(self.config.chat_mention_min_gap_minutes)) * 60
        if now - self._store.last_mention.get(user_id, 0.0) < min_gap:
            return
        if user_id in self._inflight:
            return

        stream_id = str(message.get("session_id") or message.get("stream_id") or "").strip()
        self._inflight.add(user_id)
        task = asyncio.create_task(
            self._delayed_chat_mention(user_id, platform=platform, fallback_stream_id=stream_id)
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    @staticmethod
    def _extract_sender(message: dict[str, Any]) -> tuple[str, str, str]:
        """从入站消息中提取 (user_id, nickname, platform)。"""
        platform = str(message.get("platform") or "qq").strip() or "qq"
        message_info = message.get("message_info")
        if not isinstance(message_info, dict):
            return "", "", platform
        user_info = message_info.get("user_info")
        if not isinstance(user_info, dict):
            return "", "", platform
        user_id = str(user_info.get("user_id") or "").strip()
        nickname = str(
            user_info.get("user_nickname") or user_info.get("user_cardname") or ""
        ).strip()
        return user_id, nickname, platform

    async def _delayed_chat_mention(
        self, user_id: str, *, platform: str, fallback_stream_id: str
    ) -> None:
        """稍等片刻再把到期待办交给 AI，显得像聊天中顺带一提。"""
        try:
            delay = max(0, int(self.config.chat_mention_delay_seconds))
            if delay:
                await asyncio.sleep(delay)
            if not self.config.enabled:
                return
            now = time.time()
            todos = [(tid, t) for tid, t in self._pending_for(user_id) if t.next_remind_at <= now]
            if not todos:
                return
            await self._fire_reminder(user_id, todos, platform=platform, fallback_stream_id=fallback_stream_id)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("顺带提醒执行失败: user_id=%s", user_id)
        finally:
            self._inflight.discard(user_id)

    # ------------------------------------------------------------------
    # 定时提醒循环
    # ------------------------------------------------------------------

    async def _schedule_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_LOOP_INTERVAL_SECONDS)
                if not self.config.enabled:
                    continue
                now = time.time()
                by_user: dict[str, list[tuple[str, TodoItemConfig]]] = {}
                for todo_id, todo in self.config.todos.items():
                    if todo.done or todo.next_remind_at > now:
                        continue
                    by_user.setdefault(todo.user_id, []).append((todo_id, todo))
                for user_id, todos in by_user.items():
                    if not self._user_allowed(user_id) or user_id in self._inflight:
                        # 不可发送时也把提醒时间往后推一点，避免每轮循环重复尝试
                        self._postpone_undeliverable(todos)
                        continue
                    self._inflight.add(user_id)
                    try:
                        await self._fire_reminder(
                            user_id,
                            todos,
                            platform=todos[0][1].platform or "qq",
                            fallback_stream_id=todos[0][1].source_stream_id,
                        )
                    finally:
                        self._inflight.discard(user_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("备忘录提醒循环异常")
                await asyncio.sleep(60)

    def _postpone_undeliverable(self, todos: list[tuple[str, TodoItemConfig]]) -> None:
        now = time.time()
        for _, todo in todos:
            todo.next_remind_at = now + 600
        self._save_todos_to_disk()

    # ------------------------------------------------------------------
    # 提醒发送
    # ------------------------------------------------------------------

    def _backoff_seconds(self, remind_count: int) -> float:
        steps = [max(1, int(x)) for x in (self.config.remind_backoff_minutes or [])] or [1440]
        idx = min(max(remind_count - 1, 0), len(steps) - 1)
        return float(steps[idx]) * 60

    async def _resolve_remind_stream(
        self, user_id: str, *, platform: str, fallback_stream_id: str
    ) -> str:
        """确定提醒使用的聊天流：优先与该用户的私聊流。"""
        if self.config.prefer_private:
            try:
                result = await asyncio.wait_for(
                    self.ctx.chat.get_stream_by_user_id(user_id, platform=platform),
                    timeout=_CAPABILITY_TIMEOUT_SECONDS,
                )
                stream_id = _extract_stream_id(result)
                if stream_id:
                    return stream_id
            except Exception as exc:
                logger.warning("查找用户私聊流失败: user_id=%s error=%s", user_id, exc)
            try:
                result = await asyncio.wait_for(
                    self.ctx.chat.open_session(
                        platform=platform, chat_type="private", user_id=user_id
                    ),
                    timeout=_CAPABILITY_TIMEOUT_SECONDS,
                )
                stream_id = _extract_stream_id(result)
                if stream_id:
                    return stream_id
            except Exception as exc:
                logger.warning("尝试建立用户私聊流失败: user_id=%s error=%s", user_id, exc)
        return fallback_stream_id or ""

    async def _fire_reminder(
        self, user_id: str, todos: list[tuple[str, TodoItemConfig]], *, platform: str, fallback_stream_id: str
    ) -> None:
        """把到期待办交给 AI 提醒，并安排下一次提醒时间。"""
        assert self._store is not None
        stream_id = await self._resolve_remind_stream(
            user_id, platform=platform, fallback_stream_id=fallback_stream_id
        )
        now = time.time()
        if not stream_id:
            logger.warning("待办提醒找不到可用聊天流，稍后重试: user_id=%s", user_id)
            self._postpone_undeliverable(todos)
            return

        for _, todo in todos:
            todo.remind_count += 1
            todo.last_reminded_at = now
            todo.next_remind_at = now + self._backoff_seconds(todo.remind_count)
        self._store.last_mention[user_id] = now
        self._store.save()
        self._save_todos_to_disk()

        nickname = self._store.nicknames.get(user_id, "")
        lines = [f"备忘录提醒任务：{nickname or '对方'}有 {len(todos)} 件未完成的待办："]
        for i, (_, todo) in enumerate(todos, 1):
            lines.append(
                f"{i}. {todo.content}（记录于 {_fmt_time(todo.created_at)}，已提醒 {todo.remind_count} 次）"
            )
        lines.append(
            "请用自然、像朋友顺口一提的口吻提醒对方（简短即可，不必逐条罗列，也不要太正式）；"
            "若对方表示已经完成，请调用 memo_complete_todo 划掉对应待办。"
        )
        intent = "\n".join(lines)

        try:
            await asyncio.wait_for(
                self.ctx.maisaka.proactive.trigger(
                    stream_id=stream_id,
                    intent=intent,
                    reason="memo.todo.remind",
                    metadata={"user_id": user_id, "todo_ids": [tid for tid, _ in todos]},
                ),
                timeout=_CAPABILITY_TIMEOUT_SECONDS,
            )
            logger.info(
                "已触发待办提醒: user_id=%s stream_id=%s todos=%s",
                user_id,
                stream_id,
                [tid for tid, _ in todos],
            )
        except Exception:
            logger.exception("待办提醒触发失败: user_id=%s stream_id=%s", user_id, stream_id)

    # ------------------------------------------------------------------
    # LLM 工具
    # ------------------------------------------------------------------

    @Tool(
        "memo_add_todo",
        description=(
            "把用户交代的事情记录到备忘录（待办清单）。"
            "适用场景：用户明确表达了需要去做/去买/要记住的事（例如“我得买牛奶”“记得周六交材料”），"
            "或明确要求“记一下”“记到备忘录”。判断准则：语气确定、明显是需要后续完成的，直接调用本工具记录；"
            "如果只是随口一提、语义模糊、拿不准用户是否想记（例如“可能要买”），请先口头向用户确认，"
            "得到肯定答复后再调用。记录后请用简短自然的方式告诉用户已经记好了。"
            "参数说明："
            "user_id（必填，string）说话用户的 QQ 号；"
            "content（必填，string）待办内容，一句话说清要做什么；"
            "remind_time（可选，string）首次提醒时间，ISO-8601 格式带时区，例如 2026-10-26T09:00:00+08:00，"
            "请根据事情性质选择合理的提醒时间（如“这周末前买牛奶”可选周六上午；拿不准就省略，系统会自动安排）；"
            "note（可选，string）补充说明；"
            "stream_id（可选，string）当前聊天流 ID；"
            "user_nickname（可选，string）用户昵称。"
            "提醒会持续到用户确认完成为止。"
        ),
        parameters=[
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="说话用户的 QQ 号",
                required=True,
            ),
            ToolParameterInfo(
                name="content",
                param_type=ToolParamType.STRING,
                description="待办内容，一句话说清要做什么",
                required=True,
            ),
            ToolParameterInfo(
                name="remind_time",
                param_type=ToolParamType.STRING,
                description="首次提醒时间，ISO-8601 带时区，例如 2026-10-26T09:00:00+08:00；拿不准就省略",
                required=False,
            ),
            ToolParameterInfo(
                name="note",
                param_type=ToolParamType.STRING,
                description="补充说明，可省略",
                required=False,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID，可省略",
                required=False,
            ),
            ToolParameterInfo(
                name="user_nickname",
                param_type=ToolParamType.STRING,
                description="用户昵称，可省略",
                required=False,
            ),
        ],
        visibility="visible",
    )
    async def memo_add_todo(self, **kwargs: Any) -> dict[str, Any]:
        """LLM 工具：记录待办。"""
        if self._store is None:
            return {"success": False, "error": "插件尚未完成初始化"}
        user_id = str(kwargs.get("user_id", "")).strip()
        content = str(kwargs.get("content", "")).strip()
        note = str(kwargs.get("note", "") or "").strip()
        stream_id = str(kwargs.get("stream_id", "") or "").strip()
        nickname = str(kwargs.get("user_nickname", "") or "").strip()

        if not user_id:
            return {"success": False, "error": "user_id 不能为空"}
        if not content:
            return {"success": False, "error": "content 不能为空"}
        if not self._user_allowed(user_id):
            return self._deny_result(user_id)
        if nickname:
            self._store.nicknames[user_id] = nickname

        pending = self._pending_for(user_id)
        if len(pending) >= int(self.config.max_todos_per_user):
            return {
                "success": False,
                "error": f"用户 {user_id} 的未完成待办已达上限 {self.config.max_todos_per_user} 条",
            }
        if len(self.config.todos) >= int(self.config.max_total_todos):
            return {"success": False, "error": "备忘录总数已达上限，请先清理已完成条目"}

        now = time.time()
        remind_time_raw = str(kwargs.get("remind_time", "") or "").strip()
        if remind_time_raw:
            try:
                remind_at = datetime.fromisoformat(remind_time_raw)
                if remind_at.tzinfo is None:
                    remind_at = remind_at.replace(tzinfo=_TZ)
                next_remind_at = remind_at.timestamp()
            except Exception:
                return {
                    "success": False,
                    "error": "remind_time 不是有效的 ISO-8601 时间，例如 2026-10-26T09:00:00+08:00",
                }
            if next_remind_at <= now:
                return {"success": False, "error": "remind_time 必须是未来时间，或直接省略"}
        else:
            next_remind_at = now + max(1, int(self.config.first_remind_delay_minutes)) * 60

        todo_id = self._new_todo_id()
        self.config.todos[todo_id] = TodoItemConfig(
            user_id=user_id,
            content=content,
            note=note,
            user_nickname=nickname,
            platform="qq",
            source_stream_id=stream_id,
            created_at=now,
            next_remind_at=next_remind_at,
        )
        self._save_todos_to_disk()
        logger.info("已记录待办: user_id=%s todo_id=%s", user_id, todo_id)
        return {
            "success": True,
            "content": (
                f"已记录待办「{content}」（编号 {todo_id}），"
                f"计划在 {_fmt_time(next_remind_at)} 前后首次提醒，"
                "之后会持续提醒直到对方确认完成。"
            ),
            "todo_id": todo_id,
            "next_remind_at": _fmt_time(next_remind_at),
        }

    @Tool(
        "memo_list_todos",
        description=(
            "查询某用户的备忘录待办。当用户问“我还有什么没做”“我的备忘录”“待办清单”，"
            "或你需要确认某用户是否有未完成事项时使用。"
            "参数说明：user_id（必填，string）用户 QQ 号；"
            "include_done（可选，boolean）是否同时显示最近已完成的条目，默认 false。"
        ),
        parameters=[
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="用户 QQ 号",
                required=True,
            ),
            ToolParameterInfo(
                name="include_done",
                param_type=ToolParamType.BOOLEAN,
                description="是否同时显示最近已完成的条目，默认 false",
                required=False,
            ),
        ],
        visibility="visible",
    )
    async def memo_list_todos(self, **kwargs: Any) -> dict[str, Any]:
        """LLM 工具：查询待办。"""
        user_id = str(kwargs.get("user_id", "")).strip()
        if not user_id:
            return {"success": False, "error": "user_id 不能为空"}
        if not self._user_allowed(user_id):
            return self._deny_result(user_id)

        include_done = bool(kwargs.get("include_done", False))
        pending = self._pending_for(user_id)
        lines = [f"{user_id} 当前有 {len(pending)} 件未完成待办："]
        for todo_id, t in pending:
            extra = f"（备注：{t.note}）" if t.note else ""
            lines.append(f"- {todo_id}｜{t.content}{extra}｜下次提醒 {_fmt_time(t.next_remind_at)}")
        done_items: list[tuple[str, TodoItemConfig]] = []
        if include_done:
            done_items = sorted(
                (kv for kv in self.config.todos.items() if kv[1].user_id == user_id and kv[1].done),
                key=lambda kv: kv[1].done_at,
                reverse=True,
            )[:10]
            if done_items:
                lines.append("最近已完成：")
                lines.extend(
                    f"- {todo_id}｜{t.content}｜完成于 {_fmt_time(t.done_at)}"
                    for todo_id, t in done_items
                )
        return {
            "success": True,
            "content": "\n".join(lines),
            "todos": [
                {"id": tid, "content": t.content, "note": t.note, "done": t.done}
                for tid, t in pending + done_items
            ],
        }

    def _find_todo(
        self, user_id: str, todo_id: str, content_keyword: str
    ) -> tuple[str, TodoItemConfig] | list[tuple[str, TodoItemConfig]] | None:
        """按编号精确匹配，或按内容关键词模糊匹配；多义时返回候选列表。"""
        pending = self._pending_for(user_id)
        if todo_id:
            for kv in pending:
                if kv[0] == todo_id:
                    return kv
            return None
        keyword = content_keyword.strip()
        if not keyword:
            return None
        matched = [kv for kv in pending if keyword in kv[1].content or kv[1].content in keyword]
        if len(matched) == 1:
            return matched[0]
        if not matched:
            return None
        return matched

    @Tool(
        "memo_complete_todo",
        description=(
            "把某条待办标记为完成，之后不再提醒。当用户表示某件事已经做完/买好/处理好了"
            "（例如“牛奶买好了”“材料交了”）时使用。"
            "参数说明：user_id（必填，string）用户 QQ 号；"
            "todo_id（可选，string）待办编号，来自 memo_list_todos 或 memo_add_todo 的返回；"
            "content（可选，string）待办内容关键词（如“牛奶”），没有编号时用它匹配。"
            "二者至少提供一个，优先使用 todo_id；如果匹配到多条，请向用户确认后重试。"
        ),
        parameters=[
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="用户 QQ 号",
                required=True,
            ),
            ToolParameterInfo(
                name="todo_id",
                param_type=ToolParamType.STRING,
                description="待办编号，优先使用",
                required=False,
            ),
            ToolParameterInfo(
                name="content",
                param_type=ToolParamType.STRING,
                description="待办内容关键词，如“牛奶”",
                required=False,
            ),
        ],
        visibility="visible",
    )
    async def memo_complete_todo(self, **kwargs: Any) -> dict[str, Any]:
        """LLM 工具：完成待办。"""
        return await self._finish_todo(kwargs, done=True)

    @Tool(
        "memo_delete_todo",
        description=(
            "彻底删除某条待办（用户表示这件事不用做了/不用记了/记错了）。"
            "参数说明：user_id（必填，string）用户 QQ 号；"
            "todo_id（可选，string）待办编号；"
            "content（可选，string）待办内容关键词。"
            "二者至少提供一个，优先使用 todo_id；匹配多条时请向用户确认后重试。"
        ),
        parameters=[
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="用户 QQ 号",
                required=True,
            ),
            ToolParameterInfo(
                name="todo_id",
                param_type=ToolParamType.STRING,
                description="待办编号，优先使用",
                required=False,
            ),
            ToolParameterInfo(
                name="content",
                param_type=ToolParamType.STRING,
                description="待办内容关键词",
                required=False,
            ),
        ],
        visibility="visible",
    )
    async def memo_delete_todo(self, **kwargs: Any) -> dict[str, Any]:
        """LLM 工具：删除待办。"""
        return await self._finish_todo(kwargs, done=False)

    async def _finish_todo(self, kwargs: dict[str, Any], *, done: bool) -> dict[str, Any]:
        """完成/删除待办的公共实现。"""
        user_id = str(kwargs.get("user_id", "")).strip()
        todo_id = str(kwargs.get("todo_id", "") or "").strip()
        content = str(kwargs.get("content", "") or "").strip()
        action = "完成" if done else "删除"

        if not user_id:
            return {"success": False, "error": "user_id 不能为空"}
        if not todo_id and not content:
            return {"success": False, "error": "todo_id 和 content 至少提供一个"}
        if not self._user_allowed(user_id):
            return self._deny_result(user_id)

        found = self._find_todo(user_id, todo_id, content)
        if found is None:
            return {
                "success": False,
                "error": "未找到匹配的待办，可先调用 memo_list_todos 查看编号后重试",
            }
        if isinstance(found, list):
            candidates = "；".join(f"{tid} {t.content}" for tid, t in found)
            return {
                "success": False,
                "error": f"匹配到多条待办：{candidates}。请向用户确认要{action}哪一条后再试。",
            }

        tid, todo = found
        if done:
            todo.done = True
            todo.done_at = time.time()
        else:
            del self.config.todos[tid]
        self._save_todos_to_disk()
        logger.info("待办已%s: user_id=%s todo_id=%s", action, user_id, tid)
        return {
            "success": True,
            "content": f"待办「{todo.content}」（{tid}）已{action}，不会再提醒。",
            "todo_id": tid,
        }

    # ------------------------------------------------------------------
    # WebUI 扩展页接口（webui/index.html 通过 maibot.query/action 调用）
    # ------------------------------------------------------------------

    @API("memo_page_state", version="1")
    async def api_page_state(self) -> dict[str, Any]:
        """WebUI 查询：全部待办（未完成在前）。"""
        todos = sorted(
            self.config.todos.items(),
            key=lambda kv: (kv[1].done, kv[1].next_remind_at if not kv[1].done else -kv[1].done_at),
        )
        return {
            "now": time.time(),
            "whitelist": [str(x) for x in (self.config.whitelist or [])],
            "todos": [
                {
                    "id": tid,
                    "user_id": t.user_id,
                    "user_nickname": t.user_nickname,
                    "content": t.content,
                    "note": t.note,
                    "created_at": t.created_at,
                    "next_remind_at": t.next_remind_at,
                    "remind_count": t.remind_count,
                    "done": bool(t.done),
                    "done_at": t.done_at,
                }
                for tid, t in todos
            ],
        }

    @API("memo_page_add_todo", version="1")
    async def api_page_add_todo(
        self, user_id: str, content: str, note: str = "", remind_at: float = 0
    ) -> dict[str, Any]:
        """WebUI 操作：手动加一条待办。remind_at 为 epoch 秒，0 表示用默认延迟。"""
        user_id = str(user_id or "").strip()
        content = str(content or "").strip()
        note = str(note or "").strip()
        if not user_id:
            raise ValueError("QQ 号不能为空")
        if not content:
            raise ValueError("待办内容不能为空")
        if not user_id.isdigit():
            raise ValueError("QQ 号应该是纯数字")
        pending = self._pending_for(user_id)
        if len(pending) >= int(self.config.max_todos_per_user):
            raise ValueError(f"用户 {user_id} 的未完成待办已达上限 {self.config.max_todos_per_user} 条")
        if len(self.config.todos) >= int(self.config.max_total_todos):
            raise ValueError("备忘录总数已达上限，请先清理已完成条目")
        now = time.time()
        try:
            remind_at = float(remind_at or 0)
        except (TypeError, ValueError):
            raise ValueError("提醒时间格式不对") from None
        next_remind_at = remind_at if remind_at > now else now + max(1, int(self.config.first_remind_delay_minutes)) * 60
        todo_id = self._new_todo_id()
        self.config.todos[todo_id] = TodoItemConfig(
            user_id=user_id,
            content=content,
            note=note,
            platform="qq",
            created_at=now,
            next_remind_at=next_remind_at,
        )
        self._save_todos_to_disk()
        logger.info("WebUI 添加待办: user_id=%s todo_id=%s", user_id, todo_id)
        return {"id": todo_id, "next_remind_at": next_remind_at}

    @API("memo_page_set_todo", version="1")
    async def api_page_set_todo(
        self,
        todo_id: str,
        content: str,
        note: str = "",
        next_remind_at: float = 0,
        done: bool = False,
    ) -> dict[str, Any]:
        """WebUI 操作：修改待办（内容/备注/下次提醒/完成状态）。"""
        todo_id = str(todo_id or "").strip()
        todo = self.config.todos.get(todo_id)
        if todo is None:
            raise ValueError(f"没有编号 {todo_id} 的待办")
        content = str(content or "").strip()
        if not content:
            raise ValueError("待办内容不能为空")
        try:
            next_remind_at = float(next_remind_at or 0)
        except (TypeError, ValueError):
            raise ValueError("提醒时间格式不对") from None
        now = time.time()
        was_done = todo.done
        todo.content = content
        todo.note = str(note or "").strip()
        if next_remind_at > 0:
            todo.next_remind_at = next_remind_at
        elif not done and todo.next_remind_at <= now:
            todo.next_remind_at = now + 600  # 过期未完成的，推 10 分钟后再提醒
        if done and not was_done:
            todo.done = True
            todo.done_at = now
        elif not done and was_done:
            todo.done = False
            todo.done_at = 0.0
            if todo.next_remind_at <= now:
                todo.next_remind_at = now + max(1, int(self.config.first_remind_delay_minutes)) * 60
        self._save_todos_to_disk()
        return {"id": todo_id, "done": todo.done, "next_remind_at": todo.next_remind_at}

    @API("memo_page_remove_todo", version="1")
    async def api_page_remove_todo(self, todo_id: str) -> dict[str, Any]:
        """WebUI 操作：彻底删除一条待办。"""
        todo_id = str(todo_id or "").strip()
        todo = self.config.todos.get(todo_id)
        if todo is None:
            raise ValueError(f"没有编号 {todo_id} 的待办")
        del self.config.todos[todo_id]
        self._save_todos_to_disk()
        logger.info("WebUI 删除待办: todo_id=%s", todo_id)
        return {"removed": todo_id}


def create_plugin() -> MemoTodoPlugin:
    """插件工厂函数。"""
    return MemoTodoPlugin()
