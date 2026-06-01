"""
主动发言与回复插件（中文QQ白名单版）

基于 juzi865/initiative-talk-plugin 改造：
- WebUI 配置字段使用中文 label。
- 新增 QQ 号白名单 qq_whitelist，可直接填写私聊对象 QQ 号。
- 保留 target_hashes，会话 hash 白名单仍可用。
- 新增活跃时段 active_hour_start / active_hour_end，默认 9:00-24:00。
- 默认不自动扫描所有会话，避免误发到群或陌生私聊。
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime
from typing import Any, Dict, List

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

try:
    from src.common.database import get_db
    from src.common.database.database_model import ChatLog, ChatStream

    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    get_db = None
    ChatLog = None
    ChatStream = None


class PluginSectionConfig(PluginConfigBase):
    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={"label": "启用插件"},
    )
    config_version: str = Field(
        default="1.1.0",
        description="配置文件版本",
        json_schema_extra={"label": "配置版本", "disabled": True},
    )
    interval_minutes: int = Field(
        default=30,
        ge=1,
        description="每隔几分钟检查一次是否主动发言",
        json_schema_extra={"label": "检查间隔（分钟）"},
    )
    probability: float = Field(
        default=0.06,
        ge=0.0,
        le=1.0,
        description="每次检查时主动发言的概率，0.06 表示 6%",
        json_schema_extra={
            "label": "主动发言概率",
            "hint": "0~1，越大越常主动找人。为省额度建议 0.03~0.10。",
            "x-widget": "slider",
            "min": 0.0,
            "max": 1.0,
            "step": 0.01,
        },
    )
    active_hour_start: int = Field(
        default=9,
        ge=0,
        le=23,
        description="允许主动发言的开始小时，24小时制",
        json_schema_extra={"label": "活跃时段开始（小时）"},
    )
    active_hour_end: int = Field(
        default=0,
        ge=0,
        le=24,
        description="允许主动发言的结束小时，24小时制；0 或 24 都表示午夜 24:00",
        json_schema_extra={
            "label": "活跃时段结束（小时）",
            "hint": "默认 9 到 0，表示 09:00-24:00。start=end 表示全天。",
        },
    )
    qq_whitelist: List[str] = Field(
        default_factory=list,
        description="QQ号白名单：插件只会主动找这些QQ私聊",
        json_schema_extra={
            "label": "QQ号白名单",
            "hint": "直接填 QQ 号即可；对方必须已经和麦麦产生过私聊会话。",
        },
    )
    target_hashes: List[str] = Field(
        default_factory=list,
        description="会话hash白名单：高级用法，可直接填写 stream_id/session_id",
        json_schema_extra={
            "label": "会话hash白名单（高级）",
            "hint": "一般不用填。若 QQ 号解析失败，可从推理记录里的 resolved_session_id 复制到这里。",
        },
    )
    allow_auto_scan: bool = Field(
        default=False,
        description="是否允许在白名单为空时自动扫描所有会话",
        json_schema_extra={
            "label": "允许自动扫描所有会话",
            "hint": "谨慎开启。开启后可能主动发到所有群聊/私聊。",
        },
    )
    context_messages: int = Field(
        default=6,
        ge=0,
        description="主动发言前参考最近几条聊天记录",
        json_schema_extra={"label": "参考最近消息数"},
    )
    use_persona: bool = Field(
        default=True,
        description="是否使用 Bot 人格设定",
        json_schema_extra={"label": "使用云宝人格"},
    )
    llm_timeout: int = Field(
        default=120,
        ge=10,
        description="LLM 生成请求超时时间，单位秒",
        json_schema_extra={"label": "模型超时（秒）"},
    )
    retry_times: int = Field(
        default=1,
        ge=0,
        description="LLM 请求失败后的重试次数",
        json_schema_extra={"label": "失败重试次数"},
    )
    retry_delay: float = Field(
        default=2.0,
        ge=0.0,
        description="重试间隔，单位秒",
        json_schema_extra={"label": "重试间隔（秒）"},
    )


class InitiativeTalkConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)


class InitiativeTalkPlugin(MaiBotPlugin):
    config_model = InitiativeTalkConfig

    def _get_bot_persona(self) -> str:
        try:
            persona = self.ctx.config.get("persona", "")
            if persona:
                return persona
        except Exception:
            pass
        if hasattr(self.ctx, "get_persona"):
            try:
                persona = self.ctx.get_persona()
                if persona:
                    return persona
            except Exception:
                pass
        return "你是一个热心、自然、会主动关心对方的聊天对象"

    def _is_active_time(self) -> bool:
        start = int(self.config.plugin.active_hour_start)
        end = int(self.config.plugin.active_hour_end)
        if end == 24:
            end = 0
        if start == end:
            return True
        hour = datetime.now().hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    async def _get_recent_messages(self, stream_id: str, limit: int = 3) -> List[Dict[str, str]]:
        if not DB_AVAILABLE or not ChatLog:
            return []
        try:
            with get_db().atomic():
                logs = (
                    ChatLog.select()
                    .where(ChatLog.stream_id == stream_id)
                    .order_by(ChatLog.created_at.desc())
                    .limit(limit)
                )
            messages = []
            for log in reversed(list(logs)):
                sender = getattr(log, "sender_name", None) or getattr(log, "sender_id", "未知")
                content = getattr(log, "content", "") or getattr(log, "text", "")
                if content:
                    messages.append({"sender": str(sender), "content": str(content)})
            return messages
        except Exception as exc:
            self.ctx.logger.debug("获取会话历史失败 stream=%s: %s", stream_id[:8], exc)
            return []

    async def _resolve_stream_by_qq(self, qq: str) -> str:
        qq = str(qq).strip()
        if not qq:
            return ""
        try:
            stream = await self.ctx.chat.get_stream_by_user_id(qq, platform="qq")
            if isinstance(stream, dict):
                return str(stream.get("session_id") or stream.get("stream_id") or "")
        except Exception:
            self.ctx.logger.debug("按 QQ 解析私聊会话失败 qq=%s", qq, exc_info=True)
        return ""

    async def _get_streams_from_qq_whitelist(self) -> List[str]:
        streams: List[str] = []
        for qq in self.config.plugin.qq_whitelist:
            stream_id = await self._resolve_stream_by_qq(str(qq))
            if stream_id:
                streams.append(stream_id)
            else:
                self.ctx.logger.warning("QQ白名单无法解析为私聊会话，请先让对方私聊一次麦麦: %s", qq)
        return streams

    def _get_streams_from_database(self) -> List[str]:
        if not DB_AVAILABLE or not ChatStream:
            return []
        try:
            with get_db().atomic():
                streams = list(ChatStream.select(ChatStream.stream_id).dicts())
            return [str(s["stream_id"]) for s in streams if s.get("stream_id")]
        except Exception as exc:
            self.ctx.logger.error("读取数据库会话流失败: %s", exc)
            return []

    async def _get_target_streams(self) -> List[str]:
        targets: List[str] = []
        targets.extend(str(x).strip() for x in self.config.plugin.target_hashes if str(x).strip())
        targets.extend(await self._get_streams_from_qq_whitelist())
        if not targets and self.config.plugin.allow_auto_scan:
            targets.extend(self._get_streams_from_database())
        return list(dict.fromkeys(targets))

    async def _initiate_chat(self, stream_id: str) -> None:
        persona = self._get_bot_persona() if self.config.plugin.use_persona else ""
        persona_part = f"你是{persona}。" if persona else ""
        context_part = ""
        if self.config.plugin.context_messages > 0:
            recent = await self._get_recent_messages(stream_id, self.config.plugin.context_messages)
            if recent:
                lines = ["近期聊天记录："]
                for msg in recent:
                    lines.append(f"{msg['sender']}: {msg['content']}")
                context_part = "\n".join(lines) + "\n"
        prompt = f"""{persona_part}
{context_part}
请像真实存在的人一样，主动给对方发起一句自然的私聊。
语气要贴合你当前人格和你们最近的相处，不要像公告，不要解释你为什么突然出现。
只说一句话。"""

        last_error: Exception | None = None
        for attempt in range(self.config.plugin.retry_times + 1):
            try:
                result = await self.ctx.llm.generate(
                    prompt=prompt,
                    model="replyer",
                    temperature=0.8,
                    max_tokens=60,
                    timeout=self.config.plugin.llm_timeout * 1000,
                )
                content = str(result.get("response", "")).strip()
                if result.get("success", False) and content:
                    await self.ctx.send.text(content, stream_id)
                    self.ctx.logger.info("主动私聊成功 stream=%s: %s", stream_id[:8], content)
                    return
                self.ctx.logger.warning("主动私聊生成失败: %s", result.get("error") or result.get("message"))
            except Exception as exc:
                last_error = exc
                self.ctx.logger.warning("主动私聊请求异常: %s", exc)
            if attempt < self.config.plugin.retry_times:
                await asyncio.sleep(self.config.plugin.retry_delay)

        self.ctx.logger.error("主动私聊最终失败 stream=%s last_error=%s", stream_id[:8], last_error)

    async def on_load(self) -> None:
        self.ctx.logger.info("主动发言插件（中文QQ白名单版）已加载")
        asyncio.create_task(self._schedule_chat())

    async def on_unload(self) -> None:
        self.ctx.logger.info("主动发言插件（中文QQ白名单版）已卸载")

    async def on_config_update(self, new_config: dict) -> None:
        self.ctx.logger.debug("主动发言配置已更新: %s", new_config)

    async def _schedule_chat(self) -> None:
        while True:
            if not self.config.plugin.enabled:
                await asyncio.sleep(60)
                continue
            if not self._is_active_time():
                await asyncio.sleep(60)
                continue

            target_streams = await self._get_target_streams()
            if not target_streams:
                self.ctx.logger.info("主动发言没有目标：请填写 QQ号白名单 或 会话hash白名单")
                await asyncio.sleep(60)
                continue

            for stream_id in target_streams:
                if random.random() < self.config.plugin.probability:
                    await self._initiate_chat(stream_id)
                    await asyncio.sleep(2)

            await asyncio.sleep(max(1, self.config.plugin.interval_minutes) * 60)

    @Command("主动发言", description="手动触发一次主动发言（在当前会话中）", pattern=r"^/chat$")
    async def manual_chat(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        if not stream_id:
            streams = await self._get_target_streams()
            if streams:
                stream_id = streams[0]
            else:
                return False, "没有可用的白名单会话，无法发言", 2
        await self._initiate_chat(stream_id)
        return True, "已触发主动发言", 2


def create_plugin():
    return InitiativeTalkPlugin()
