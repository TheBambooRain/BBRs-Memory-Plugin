"""AstrBot 适配层：只做“收消息、注入、调度、命令”，核心逻辑都在无 astrbot 依赖的模块里。

v0.1 的边界：
- messages 原文 + attachments 引用：v0.2 才下载/转写；
- 被动注入不续期，显式 memory_search / memory_open 才续期；
- 每日记忆整理 = 到点 + 空闲，另有强制上限和启动补跑。
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import importlib.util
import json
import re
import sys
import time
import uuid
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any

_PLUGIN_DIR = Path(__file__).resolve().parent

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.provider.entities import ProviderRequest

try:  # AstrBot 以 data.plugins.<name>.main 加载，优先相对导入
    from .consolidation import Consolidator
    from .media import make_attachment_id, render_message_text
    from .memory_db import (
        MemoryDB,
        local_day,
        make_timezone,
        normalize_kind,
        parse_iso,
        parse_umo,
        to_iso,
        utc_now,
    )
    from .migration import export_bundle, import_bundle
    from .retrieval import Retriever, is_recall_intent
    from .textutil import truncate_at_sentence
    from .timeparse import (
        _date_from_text,
        _extract_clock_text,
        _extract_date_text,
        _extract_date_texts,
        _hour_from_text,
        _minute_from_text,
        parse_relative_due,
        parse_structured_time,
        parse_time_spec,
        time_text_grounded,
    )
except ImportError:  # 离线测试 / 非包方式加载
    if str(_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_DIR))
    from consolidation import Consolidator
    from media import make_attachment_id, render_message_text
    from memory_db import (
        MemoryDB,
        local_day,
        make_timezone,
        normalize_kind,
        parse_iso,
        parse_umo,
        to_iso,
        utc_now,
    )
    from migration import export_bundle, import_bundle
    from retrieval import Retriever, is_recall_intent
    from timeparse import (
        _date_from_text,
        _extract_clock_text,
        _extract_date_text,
        _extract_date_texts,
        _hour_from_text,
        _minute_from_text,
        parse_relative_due,
        parse_structured_time,
        parse_time_spec,
        time_text_grounded,
    )

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "timezone": "Asia/Shanghai",
    "run_at": "04:00",
    "idle_minutes": 30,
    "force_after_hours": 6,
    "capture_assistant": True,
    "ignore_prefixes": ["/", "#"],
    "summary_provider_id": "",
    "chunk_chars": 12000,
    "summary_max_chars": 1200,
    "importance_min": 1,
    "importance_max": 300,
    "ttl_unit_days": 1.0,
    "ttl_min_days": 2,
    "ttl_max_days": 365,
    "promote_week_days": 7,
    "promote_month_days": 30,
    "max_promotions_per_run": 20,
    "export_shard_rows": 50000,
    "retrieve_top_k": 4,
    "deep_top_k": 8,
    "inject_budget_chars": 1200,
    "suppress_days": 7,
    "relevance_min": 0.12,
    "renew_factor": 1.3,
    "max_renewals": 3,
    "expired_retention_days": 30,
    "raw_retention_days": 0,
    "admin_only": True,
    "media_enabled": False,
    "media_dir": "",
    "stt_provider_id": "",
    "vision_provider_id": "",
    "media_max_mb": 20,
    "media_retention_days": 0,
    "wait_for_media": False,
    "reminder_backend": "auto",
    "scope_resolver": "scope_adapter.py",
    "due_window_hours": 24,
    "due_max_per_turn": 3,
    "due_budget_chars": 300,
    "reminder_repeat_hours": 6,
}

HELP_TEXT = """BBR的记忆插件 v1.0.5
/mem stats            统计
/mem whoami           当前会话的 scope
/mem scopes           列出所有 scope（管理员）
/mem bind <scope_id>  把当前会话绑定到某段记忆
/mem unbind           取消绑定
/mem alias list|set|del 管理群聊 scope 映射
/mem merge <from> <to> 合并两段记忆（管理员）
/mem items [scope]    查看原子记忆
/mem due [小时]       查看即将到期
/mem done <item_id>   标记完成
/mem cancel <item_id> 取消提醒
/mem cancel --all     取消当前会话所有待办
/mem reschedule <item_id> <时间>  修改提醒时间
/mem search <词>      搜索
/mem open <id>        看某条记忆的原文
/mem why <id>         为什么记/何时过期
/mem pin|unpin <id>   固定/取消固定
/mem mute <id> [天]   暂停主动注入
/mem forget <id>      软删除
/mem consolidate [YYYY-MM-DD] [--rebuild]  手动记忆整理/重建当天
/mem idle             空闲状态
/mem export [--md]    导出迁移包
/mem import <zip路径> 导入迁移包
/mem rebuild          重建检索索引
"""


@register(
    "astrbot_plugin_bbrsmemory_plugin",
    "dsh",
    "分层长期记忆：归档、提炼、迁移、过期、检索注入、迁移包",
    "1.0.5",
)
class MemoryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_bbrsmemory_plugin")
        self.scope_resolver = self._load_scope_resolver()
        self.db = MemoryDB(self.data_dir / "memory.db")
        self.tz = make_timezone(self._cfg("timezone"))
        self.retriever = Retriever(self.db, self._cfg_all(), self.tz)
        self.consolidator = Consolidator(
            self.db,
            self._cfg_all(),
            self._llm_json,
            self.tz,
            external_reminders=self._future_task_enabled,
        )
        self._last_activity = time.monotonic()
        self._last_attempt = 0.0
        self._closing = False
        self._scheduler_task: asyncio.Task | None = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        self._scheduler_task = loop.create_task(self._scheduler_loop())
        logger.info("memory v0.1: 数据目录 %s", self.data_dir)

    # -- 配置 -------------------------------------------------------------

    def _cfg(self, key: str) -> Any:
        value = self.config.get(key, DEFAULTS.get(key))
        return DEFAULTS.get(key) if value is None else value

    def _cfg_all(self) -> dict[str, Any]:
        return {key: self._cfg(key) for key in DEFAULTS}

    def _enabled(self) -> bool:
        return bool(self._cfg("enabled"))

    def _future_task_enabled(self, umo: str) -> bool:
        """AstrBot 主动型能力是否真的具备可用的 cron 调度器。"""
        if str(self._cfg("reminder_backend") or "auto").lower() == "memory":
            return False
        if getattr(self.context, "cron_manager", None) is None:
            return False
        try:
            config = self.context.get_config(umo=umo)
            settings = config.get("provider_settings", {}) if hasattr(config, "get") else {}
            proactive = settings.get("proactive_capability", {})
            return bool(proactive.get("add_cron_tools", True))
        except Exception:  # noqa: BLE001
            return False

    def _reminder_backend(self, event: AstrMessageEvent) -> str:
        mode = str(self._cfg("reminder_backend") or "auto").lower()
        if mode == "memory":
            return "memory"
        if self._future_task_enabled(str(event.unified_msg_origin)):
            return "future_task"
        return "memory"

    async def _schedule_future_task(
        self,
        event: AstrMessageEvent,
        text: str,
        due: datetime,
    ) -> str | None:
        cron = getattr(self.context, "cron_manager", None)
        if cron is None:
            return None
        umo = str(event.unified_msg_origin)
        note = f"用你的人设提醒用户：{text}。简短、自然，不要解释这是定时任务。"
        job = await cron.add_active_job(
            name=f"提醒：{text[:20]}",
            cron_expression=None,
            payload={
                "session": umo,
                "sender_id": str(event.get_sender_id() or ""),
                "note": note,
                "origin": "memory_plugin",
            },
            description=note,
            timezone=str(self.tz),
            run_once=True,
            run_at=due,
        )
        return str(getattr(job, "job_id", "") or "")

    async def _update_future_task(self, job_id: str, due: datetime) -> bool:
        cron = getattr(self.context, "cron_manager", None)
        if cron is None or not job_id:
            return False
        job = await cron.db.get_cron_job(job_id)
        if not job:
            return False
        payload = dict(job.payload) if isinstance(job.payload, dict) else {}
        payload["run_at"] = due.isoformat()
        updated = await cron.update_job(
            job_id,
            run_at=due,
            run_once=True,
            payload=payload,
        )
        return updated is not None

    async def _delete_future_task(self, job_id: str) -> bool:
        cron = getattr(self.context, "cron_manager", None)
        if cron is None or not job_id:
            return False
        await cron.delete_job(job_id)
        return True

    def _mark_activity(self) -> None:
        self._last_activity = time.monotonic()

    def _idle_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._last_activity)

    # -- scope ------------------------------------------------------------

    def _user_text(self, event: AstrMessageEvent) -> str:
        return (
            getattr(getattr(event, "message_obj", None), "message_str", "")
            or getattr(event, "message_str", "")
            or ""
        ).strip()

    def _message_uid(self, event: AstrMessageEvent) -> str:
        umo = str(event.unified_msg_origin)
        message_id = (
            getattr(getattr(event, "message_obj", None), "message_id", None)
            or getattr(event, "message_id", None)
        )
        if message_id:
            return f"{umo}#m:{message_id}"
        text = self._user_text(event)
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        return f"{umo}#m:{digest}" if text else ""

    def _load_scope_resolver(self):
        name = str(self._cfg("scope_resolver") or "").strip()
        if not name:
            return None
        path = Path(name)
        if not path.is_absolute():
            path = self.data_dir / path
        if not path.exists():
            return None
        try:
            spec = importlib.util.spec_from_file_location(
                "bbrs_scope_adapter", str(path),
            )
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            resolver = getattr(module, "resolve_scope", None)
            if callable(resolver):
                logger.info("memory: 已加载 scope 适配器 %s", path)
                return resolver
        except Exception:  # noqa: BLE001
            logger.exception("memory: scope 适配器加载失败 %s", path)
        return None

    def _scope_context(self, event: AstrMessageEvent) -> dict[str, Any]:
        umo = str(event.unified_msg_origin)
        obj = getattr(event, "message_obj", None)
        p_umo, k_umo, e_umo = parse_umo(umo)
        try:
            platform = str(event.get_platform_name() or p_umo)
        except Exception:  # noqa: BLE001
            platform = p_umo
        try:
            msg_type = str(event.get_message_type() or "").lower()
        except Exception:  # noqa: BLE001
            msg_type = ""
        kind = "group" if "group" in msg_type else normalize_kind(k_umo)
        if kind == "group":
            external_id = str(getattr(obj, "group_id", "") or e_umo)
        else:
            external_id = str(event.get_sender_id() or e_umo)
        return {
            "umo": umo,
            "platform": platform,
            "kind": kind,
            "external_id": external_id,
            "sender_id": str(event.get_sender_id() or ""),
            "group_id": str(getattr(obj, "group_id", "") or ""),
            "sender_name": str(event.get_sender_name() or ""),
            "raw_event": event,
        }

    def _custom_scope_ref(self, event: AstrMessageEvent) -> dict[str, Any]:
        if self.scope_resolver is None:
            return {}
        try:
            result = self.scope_resolver(self._scope_context(event))
            return dict(result) if isinstance(result, dict) else {}
        except Exception:  # noqa: BLE001
            logger.exception("memory: scope 适配器执行失败")
            return {}

    def _binding_key(self, event: AstrMessageEvent) -> str:
        ref = self._custom_scope_ref(event)
        return str(ref.get("binding_key") or event.unified_msg_origin)

    def _alias_key(self, event: AstrMessageEvent) -> str:
        ctx = self._scope_context(event)
        ref = self._custom_scope_ref(event)
        platform = str(ref.get("platform") or ctx["platform"])
        kind = normalize_kind(str(ref.get("kind") or ctx["kind"]))
        external_id = str(ref.get("external_id") or ctx["external_id"])
        return str(ref.get("alias_key") or f"{platform}:{kind}:{external_id}")

    def _event_scope(self, event: AstrMessageEvent, *, create: bool = True) -> str:
        umo = str(event.unified_msg_origin)
        ctx = self._scope_context(event)
        ref = self._custom_scope_ref(event)
        platform = str(ref.get("platform") or ctx["platform"])
        kind = normalize_kind(str(ref.get("kind") or ctx["kind"]))
        external_id = str(ref.get("external_id") or ctx["external_id"])
        binding_key = str(ref.get("binding_key") or umo)
        alias_key = str(
            ref.get("alias_key")
            or f"{platform}:{kind}:{external_id}"
        )
        label = str(
            ref.get("label")
            or (f"群 {external_id}" if kind == "group" else ctx["sender_name"])
        )
        return self.db.resolve_scope(
            umo,
            platform=platform,
            kind=kind,
            external_id=external_id,
            label=label,
            create=create,
            binding_key=binding_key,
            alias_key=alias_key,
            scope_id=str(ref.get("scope_id") or "") or None,
        )

    def _event_kind(self, event: AstrMessageEvent) -> str:
        ref = self._custom_scope_ref(event)
        if ref.get("kind"):
            return normalize_kind(str(ref["kind"]))
        try:
            msg_type = str(event.get_message_type() or "").lower()
        except Exception:  # noqa: BLE001
            msg_type = ""
        if "group" in msg_type:
            return "group"
        _, kind, _ = parse_umo(str(event.unified_msg_origin))
        return normalize_kind(kind)

    # -- LLM --------------------------------------------------------------

    async def _llm_json(self, prompt: str, umo: str) -> tuple[str, str]:
        provider_id = str(self._cfg("summary_provider_id") or "").strip()
        if not provider_id:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
        resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            system_prompt="你是严谨的长期记忆整理器，只输出被要求的 JSON，不要输出解释。",
        )
        text = getattr(resp, "completion_text", "") or ""
        if not text:
            text = str(getattr(resp, "result", "") or "")
        return text, provider_id

    # -- 消息捕获 ---------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def capture_message(self, event: AstrMessageEvent) -> None:
        if not self._enabled():
            return
        self._mark_activity()
        try:
            await self._capture_user_message(event)
        except Exception:  # noqa: BLE001
            logger.exception("memory: 捕获消息失败")

    async def _capture_user_message(self, event: AstrMessageEvent) -> None:
        obj = getattr(event, "message_obj", None)
        text = (
            getattr(obj, "message_str", "")
            or getattr(event, "message_str", "")
            or ""
        )
        if not text:
            text = "".join(
                str(getattr(c, "text", ""))
                for c in (getattr(obj, "message", None) or [])
                if type(c).__name__ == "Plain"
            )
        text = text.strip()
        prefixes = [str(p) for p in (self._cfg("ignore_prefixes") or []) if str(p)]
        if text and any(text.startswith(p) for p in prefixes):
            return
        if not text and not (getattr(obj, "message", None) or []):
            return

        now = utc_now()
        umo = str(event.unified_msg_origin)
        platform = ""
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:  # noqa: BLE001
            pass
        message_id = str(getattr(obj, "message_id", "") or "")
        if message_id:
            msg_uid = f"{umo}#m:{message_id}"
        else:
            digest = hashlib.sha256(
                f"{umo}|{text}|{now.isoformat()}".encode("utf-8", errors="ignore"),
            ).hexdigest()[:16]
            msg_uid = f"{umo}#m:{digest}"
        attachments = self._extract_attachments(event, umo, msg_uid)
        if not text and not attachments:
            return
        scope_id = self._event_scope(event)
        self.db.add_message(
            msg_uid=msg_uid,
            umo=umo,
            scope_id=scope_id,
            session_id=str(getattr(event, "session_id", "") or ""),
            platform=platform,
            role="user",
            sender_id=str(event.get_sender_id() or ""),
            sender_name=str(event.get_sender_name() or ""),
            content=text,
            created_at=to_iso(now),
            day=local_day(now, self.tz),
        )
        for att in attachments:
            self.db.add_attachment(
                attachment_id=att["id"],
                message_uid=msg_uid,
                kind=att["kind"],
                platform_ref=att.get("platform_ref", ""),
                mime=att.get("mime", ""),
                size=int(att.get("size", 0) or 0),
                status="pending",
            )
        # 提醒改为由模型调用 memory_reminder_* 工具创建；
        # 快路径默认关闭，避免和工具重复创建。

    async def _ask(self, event: AstrMessageEvent, text: str) -> None:
        try:
            from astrbot.core.message.components import Plain
            from astrbot.core.message.message_event_result import MessageChain

            await event.send(MessageChain([Plain(text)]))
        except Exception:  # noqa: BLE001
            logger.warning("memory: 发送澄清问题失败: %s", text)

    def _latest_pending(self, scope_id: str, status: str = "pending"):
        for item in self.db.list_items(scope_id, limit=50, include_done=True):
            if item.get("status") == status and item.get("type") in {"reminder", "task"}:
                return item
        return None

    async def _handle_clarification(
        self,
        event: AstrMessageEvent,
        text: str,
        scope_id: str,
        umo: str,
        now,
    ) -> bool:
        item = self._latest_pending(scope_id, "needs_clarification")
        if not item:
            return False
        if not re.search(r"(上午|下午|晚上|凌晨|早上|中午|\d{1,2}\s*[:点时])", text):
            return False
        hour = _hour_from_text(text)
        if hour is None:
            for key in item.get("keywords") or []:
                if str(key).startswith("clarify:"):
                    hour = int(str(key).split(":", 1)[1] or 0)
                    break
        if hour is None:
            return False
        period = re.search(r"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)", text)
        period_text = period.group(1) if period else ""
        if period_text in {"下午", "傍晚", "晚上", "夜里"} and hour < 12:
            hour += 12
        if period_text == "中午" and hour < 11:
            hour += 12
        now_local = now.astimezone(self.tz)
        due = datetime.combine(now_local.date(), dtime(hour, 0), tzinfo=self.tz)
        if due <= now_local:
            due += timedelta(days=1)
        self.db.update_item(
            item["id"],
            status="pending",
            type="reminder",
            due_at=to_iso(due.astimezone(timezone.utc)),
            inject_until=to_iso(due.astimezone(timezone.utc) + timedelta(days=1)),
            keywords=["提醒"],
        )
        self.db.enqueue_outbox(
            item_id=item["id"],
            action="upsert",
            payload={
                "item_id": item["id"],
                "scope_id": scope_id,
                "umo": umo,
                "text": item.get("text", ""),
                "due_at": to_iso(due.astimezone(timezone.utc)),
                "timezone": str(self.tz),
                "dedupe_key": f"{scope_id}|{to_iso(due)}|{item.get('text', '')[:60]}",
            },
        )
        await self._ask(
            event,
            f"好，已改成 {due.astimezone(self.tz).strftime('%m-%d %H:%M')}。",
        )
        return True

    async def _handle_time_update(
        self,
        event: AstrMessageEvent,
        text: str,
        scope_id: str,
        umo: str,
        now,
    ) -> bool:
        item = self._latest_pending(scope_id, "pending")
        if not item:
            return False
        has_change = bool(re.search(r"(改到|改成|推迟|提前|调整|晚点|早点|不是.*是)", text))
        has_time = bool(re.search(r"(\d{1,2}\s*[:点时]|[一二两三四五六七八九十]{1,3}\s*[点时])", text))
        has_cue = bool(re.search(r"(提醒|记得|别忘|到点|叫我|帮我记)", text))
        if not has_change and not (has_time and not has_cue):
            return False
        due, ambiguous = parse_time_spec(text, now, self.tz)
        if ambiguous:
            hour = _hour_from_text(text)
            keywords = ["提醒", f"clarify:{hour if hour is not None else 0}"]
            self.db.update_item(
                item["id"],
                status="needs_clarification",
                keywords=keywords,
            )
            await self._ask(
                event,
                f"「{text}」有歧义：是上午还是晚上？"
                f"（item {item['id'][:8]}）",
            )
            return True
        if not due:
            return False
        self.db.update_item(
            item["id"],
            status="pending",
            type="reminder",
            due_at=to_iso(due),
            inject_until=to_iso(due + timedelta(days=1)),
            keywords=["提醒"],
        )
        self.db.enqueue_outbox(
            item_id=item["id"],
            action="upsert",
            payload={
                "item_id": item["id"],
                "scope_id": scope_id,
                "umo": umo,
                "text": item.get("text", ""),
                "due_at": to_iso(due),
                "timezone": str(self.tz),
                "dedupe_key": f"{scope_id}|{to_iso(due)}|{item.get('text', '')[:60]}",
            },
        )
        await self._ask(
            event,
            f"好，已改到 {due.astimezone(self.tz).strftime('%m-%d %H:%M')}。",
        )
        return True

    async def _maybe_fast_reminder(
        self,
        event: AstrMessageEvent,
        text: str,
        umo: str,
        scope_id: str,
        msg_uid: str,
        now,
    ) -> None:
        # v1.0.5: 旧正则快路径已停用，保留代码仅作历史。
        return
        if not text or not re.search(r"(提醒|记得|别忘|到点|叫我|帮我记)", text):
            return
        if self.db.item_exists_for_message(scope_id, msg_uid):
            return
        if await self._handle_clarification(event, text, scope_id, umo, now):
            return
        if await self._handle_time_update(event, text, scope_id, umo, now):
            return
        if not text or not re.search(r"(提醒|记得|别忘|到点|叫我|帮我记)", text):
            return
        rule_due, ambiguous = parse_time_spec(text, now, self.tz)
        if ambiguous:
            hour = _hour_from_text(text)
            item = self.db.insert_item(
                {
                    "scope_id": scope_id,
                    "type": "task",
                    "text": text[:300],
                    "keywords": ["提醒", f"clarify:{hour if hour is not None else 0}"],
                    "importance": 200,
                    "status": "needs_clarification",
                    "source_start_uid": msg_uid,
                    "source_end_uid": msg_uid,
                },
            )
            await self._ask(
                event,
                f"「{text}」的时间有歧义：是上午还是晚上？"
                f"（item {item[:8]}）",
            )
            return
        now_local = now.astimezone(self.tz)
        prompt = (
            "你是提醒抽取器。根据当前时间和用户消息，判断是否包含明确的提醒/待办。\n"
            f"当前时间：{now_local.isoformat()}\n"
            f"时区：{self.tz}\n"
            f"用户消息：{text}\n"
            "只输出 JSON：\n"
            '{"is_reminder": true/false, "text": "要提醒的事", '
            '"due_at": "ISO8601 带时区或 null", "confidence": 0到1}\n'
            "规则：没有明确时间就 due_at=null；不要编造时间；不是提醒就 is_reminder=false。"
        )
        try:
            raw, _model = await self._llm_json(prompt, umo)
            data = parse_fast_reminder_json(raw)
            logger.info("memory: 快路径抽取结果 %s", data)
        except Exception:  # noqa: BLE001
            logger.warning("memory: 快路径 LLM 失败，改用规则解析", exc_info=True)
            _model = ""
            data = None
        if not data:
            data = {"is_reminder": True, "text": text, "due_at": None, "confidence": 0}
        reminder_text = str(data.get("text") or text).strip()[:300]
        if not reminder_text:
            return
        due = parse_iso(str(data.get("due_at") or ""))
        if not due or due <= now:
            due = parse_relative_due(text, now, self.tz)
        if due and due <= now:
            due = None
        logger.info("memory: 快路径 due=%s text=%s", due, reminder_text)
        msg_row = self.db.get_message_by_uid(msg_uid)
        msg_id = int(msg_row["id"]) if msg_row else None
        topic_id = self.db.insert_topic(
            {
                "umo": umo,
                "scope_id": scope_id,
                "day": local_day(now, self.tz),
                "title": truncate_at_sentence("提醒：" + reminder_text, 30),
                "keywords": ["提醒"],
                "summary_search": reminder_text,
                "summary_week": reminder_text,
                "facts": [reminder_text],
                "importance": 200,
                "layer": "week",
                "ttl_days": 30,
                "source_start_uid": msg_uid,
                "source_end_uid": msg_uid,
                "source_start_id": msg_id,
                "source_end_id": msg_id,
                "expire_at": to_iso(now + timedelta(days=30)),
                # 主题本身不注入，由到期队列负责；仍可 search。
                "inject_until": to_iso(now),
                "search_until": None,
                "summary_model": _model,
                "prompt_version": "fast-v0.1.2",
            },
        )
        item_type = "reminder" if due else "task"
        item_id = self.db.insert_item(
            {
                "scope_id": scope_id,
                "topic_id": topic_id,
                "type": item_type,
                "text": reminder_text,
                "keywords": ["提醒"],
                "importance": 200,
                "due_at": to_iso(due) if due else None,
                "status": "pending",
                "inject_until": to_iso(due + timedelta(days=1)) if due else to_iso(now + timedelta(days=14)),
                "search_until": None,
                "source_start_uid": msg_uid,
                "source_end_uid": msg_uid,
            },
        )
        if due:
            self.db.enqueue_outbox(
                item_id=item_id,
                action="upsert",
                payload={
                    "item_id": item_id,
                    "topic_id": topic_id,
                    "scope_id": scope_id,
                    "umo": umo,
                    "text": reminder_text,
                    "due_at": to_iso(due),
                    "timezone": str(self.tz),
                    "dedupe_key": f"{scope_id}|{to_iso(due)}|{reminder_text[:60]}",
                },
            )

    def _extract_attachments(
        self,
        event: AstrMessageEvent,
        umo: str,
        msg_uid: str,
    ) -> list[dict[str, Any]]:
        """v0.1 只记录引用；v0.2 的处理器会按这些记录下载/转写。"""
        result: list[dict[str, Any]] = []
        components = getattr(getattr(event, "message_obj", None), "message", None) or []
        for idx, comp in enumerate(components):
            name = type(comp).__name__.lower()
            kind = _component_kind(name, comp)
            if not kind:
                continue
            ref = (
                getattr(comp, "file", None)
                or getattr(comp, "url", None)
                or getattr(comp, "file_unique", None)
                or getattr(comp, "path", None)
                or ""
            )
            ref = str(ref or "")
            if not ref:
                continue
            mime = str(getattr(comp, "mime", "") or "")
            if not mime:
                mime = _guess_mime(ref)
            result.append(
                {
                    "id": make_attachment_id(umo, msg_uid, idx, ref),
                    "kind": kind,
                    "platform_ref": ref,
                    "mime": mime,
                    "size": int(getattr(comp, "size", 0) or 0),
                },
            )
        return result

    @filter.on_llm_response()
    async def capture_response(self, event: AstrMessageEvent, response: Any) -> None:
        if not self._enabled() or not bool(self._cfg("capture_assistant")):
            return
        self._mark_activity()
        try:
            text = (
                getattr(response, "completion_text", "")
                or getattr(response, "result", "")
                or ""
            )
            text = str(text).strip()
            if not text:
                return
            now = utc_now()
            umo = str(event.unified_msg_origin)
            msg_uid = f"{umo}#a:{uuid.uuid4().hex}"
            self.db.add_message(
                msg_uid=msg_uid,
                umo=umo,
                scope_id=self._event_scope(event),
                session_id=str(getattr(event, "session_id", "") or ""),
                platform=str(event.get_platform_name() or ""),
                role="assistant",
                sender_id=str(event.get_self_id() or "bot"),
                sender_name="bot",
                content=text,
                created_at=to_iso(now),
                day=local_day(now, self.tz),
            )
        except Exception:  # noqa: BLE001
            logger.exception("memory: 捕获回复失败")

    # -- 注入 -------------------------------------------------------------

    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self._enabled():
            return
        self._mark_activity()
        try:
            # 前置包装：不允许模型绕过 memory_reminder_* 直接排期。
            tools = getattr(req, "func_tool", None)
            if tools is not None and hasattr(tools, "remove_tool"):
                tools.remove_tool("future_task")
            query = (
                getattr(getattr(event, "message_obj", None), "message_str", "")
                or getattr(event, "message_str", "")
                or ""
            ).strip()
            if not query:
                return
            umo = str(event.unified_msg_origin)
            now = utc_now()
            scope_id = self._event_scope(event)
            deep = is_recall_intent(query)
            due_text, due_items = self.retriever.due_block(scope_id, now=now)
            topics = self.retriever.search(
                umo,
                query,
                now=now,
                deep=deep,
                scope_id=scope_id,
                for_injection=True,
            )
            memory_text = self.retriever.build_block(topics) if topics else ""
            block = "\n\n".join(part for part in (due_text, memory_text) if part).strip()
            if not block:
                return
            prefix = req.system_prompt or ""
            if "[长期记忆参考" in prefix or "[到期提醒" in prefix:
                return
            req.system_prompt = (prefix + "\n\n" + block).strip()
            if topics:
                self.retriever.mark_injected(topics, now=now)
            for item in due_items:
                self.db.mark_item_injected(item["id"], to_iso(now))
        except Exception:  # noqa: BLE001
            logger.exception("memory: 注入失败")

    # -- 定时 + 空闲 ------------------------------------------------------

    async def _scheduler_loop(self) -> None:
        await asyncio.sleep(15)
        while not self._closing:
            try:
                if self._enabled():
                    await self._maybe_run_daily()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception("memory: 调度循环异常")
            await asyncio.sleep(30)

    def _run_at(self) -> dtime:
        raw = str(self._cfg("run_at") or "04:00")
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw.strip())
        if not m:
            return dtime(4, 0)
        hour, minute = int(m.group(1)), int(m.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return dtime(4, 0)
        return dtime(hour, minute)

    async def _maybe_run_daily(self) -> None:
        now = utc_now()
        now_local = now.astimezone(self.tz)
        today = now_local.date().isoformat()
        last_run = self.db.get_meta("last_daily_run", "")
        if last_run == today:
            return

        idle_minutes = int(self._cfg("idle_minutes") or 0)
        idle = self._idle_seconds() >= idle_minutes * 60
        scheduled = datetime.combine(now_local.date(), self._run_at(), tzinfo=self.tz)
        force_hours = int(self._cfg("force_after_hours") or 0)
        force_deadline = scheduled + timedelta(hours=force_hours) if force_hours else None
        old_pending = self.db.has_pending_before(
            (now_local.date() - timedelta(days=1)).isoformat(),
        )

        should = False
        reason = ""
        if old_pending and idle:
            should, reason = True, "补跑更早的日期"
        elif now_local >= scheduled and idle:
            should, reason = True, "到点且空闲"
        elif (
            force_deadline
            and now_local >= force_deadline
            and now_local >= scheduled
        ):
            should, reason = True, "到点后长时间未空闲，强制执行"

        if not should:
            return
        if time.monotonic() - self._last_attempt < 300:
            return
        self._last_attempt = time.monotonic()
        logger.info("memory: 开始每日记忆整理（%s）", reason)
        stats = await self.consolidator.run()
        # 有失败或等待媒体的天时不标记“今天已完成”，让调度器稍后重试。
        if not stats.get("errors") and not stats.get("skipped"):
            self.db.set_meta("last_daily_run", today)
        logger.info(
            "memory: 记忆整理完成 days=%s topics=%s promoted=%s expired=%s errors=%s",
            stats.get("days"),
            stats.get("topics"),
            stats.get("promoted"),
            stats.get("expired"),
            len(stats.get("errors") or []),
        )

    # -- 命令 -------------------------------------------------------------

    @filter.command("mem")
    async def mem_command(self, event: AstrMessageEvent):
        self._mark_activity()
        raw = (getattr(event, "message_str", "") or "").strip()
        tokens = [t for t in re.split(r"\s+", raw) if t]
        idx = next(
            (i for i, t in enumerate(tokens) if t.lstrip("/").lower() == "mem"),
            None,
        )
        if idx is None:
            sub = tokens[0].lower() if tokens else "help"
            args = tokens[1:]
        else:
            sub = tokens[idx + 1].lower() if len(tokens) > idx + 1 else "help"
            args = tokens[idx + 2 :]
        if sub in {"help", ""}:
            yield event.plain_result(HELP_TEXT)
            return
        if sub == "whoami":
            yield event.plain_result(self._whoami_text(event))
            return
        if not self._is_admin(event):
            yield event.plain_result("只有管理员可以执行记忆管理命令。")
            return
        try:
            if sub == "stats":
                yield event.plain_result(self._stats_text())
            elif sub == "scopes":
                yield event.plain_result(self._scopes_text())
            elif sub == "bind":
                yield event.plain_result(
                    self._bind_text(event, args[0] if args else ""),
                )
            elif sub == "unbind":
                yield event.plain_result(self._unbind_text(event))
            elif sub == "alias":
                yield event.plain_result(self._alias_text(args))
            elif sub == "merge":
                if len(args) < 2:
                    yield event.plain_result("用法：/mem merge <from_scope> <to_scope>")
                    return
                yield event.plain_result(self._merge_text(args[0], args[1]))
            elif sub == "items":
                yield event.plain_result(
                    self._items_text(event, args[0] if args else ""),
                )
            elif sub == "due":
                hours = int(args[0]) if args and args[0].isdigit() else 24
                yield event.plain_result(self._due_text(event, hours))
            elif sub == "done":
                yield event.plain_result(self._done_text(args[0] if args else "", True))
            elif sub == "cancel":
                if args and args[0] == "--all":
                    yield event.plain_result(self._cancel_all_text(event))
                else:
                    yield event.plain_result(
                        self._done_text(args[0] if args else "", False),
                    )
            elif sub == "reschedule":
                if len(args) < 2:
                    yield event.plain_result(
                        "用法：/mem reschedule <item_id> <时间>，"
                        "例如 23:00 或 明天晚上11点",
                    )
                    return
                yield event.plain_result(
                    self._reschedule_text(args[0], " ".join(args[1:])),
                )
            elif sub == "search":
                yield event.plain_result(
                    self._search_text(str(event.unified_msg_origin), " ".join(args)),
                )
            elif sub == "open":
                yield event.plain_result(self._open_text(args[0] if args else ""))
            elif sub == "why":
                yield event.plain_result(self._why_text(args[0] if args else ""))
            elif sub in {"pin", "unpin"}:
                yield event.plain_result(
                    self._pin_text(args[0] if args else "", sub == "pin"),
                )
            elif sub == "mute":
                days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 7
                yield event.plain_result(
                    self._mute_text(args[0] if args else "", days),
                )
            elif sub == "forget":
                yield event.plain_result(self._forget_text(args[0] if args else ""))
            elif sub == "consolidate":
                rebuild = "--rebuild" in args
                clean_args = [a for a in args if a != "--rebuild"]
                day = clean_args[0] if clean_args else None
                if day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    yield event.plain_result("日期格式应为 YYYY-MM-DD。")
                    return
                if rebuild and not day:
                    yield event.plain_result(
                        "用法：/mem consolidate <YYYY-MM-DD> --rebuild",
                    )
                    return
                if rebuild:
                    backup = await asyncio.to_thread(self._backup_before_rebuild)
                    stats = await self.consolidator.rebuild_day(
                        day, str(event.unified_msg_origin),
                    )
                    deleted = stats.get("deleted") or {}
                    yield event.plain_result(
                        f"已备份到 {backup}\n"
                        f"重建 {day}：删除 topic {deleted.get('topics', 0)} 条、"
                        f"item {deleted.get('items', 0)} 条。\n"
                        + self._stats_line(stats),
                    )
                else:
                    stats = await self.consolidator.run(day=day)
                    yield event.plain_result(self._stats_line(stats))
            elif sub == "idle":
                yield event.plain_result(self._idle_text())
            elif sub == "export":
                md = "--md" in args
                path = await asyncio.to_thread(
                    export_bundle,
                    self.db,
                    self.data_dir / "exports",
                    self._cfg_all(),
                    markdown=md,
                )
                size = path.stat().st_size / 1024 / 1024
                yield event.plain_result(
                    f"迁移包已生成：{path}\n大小：{size:.2f} MB\n"
                    "把它复制到新机器后，执行 /mem import <路径> 即可。",
                )
            elif sub == "import":
                if not args:
                    yield event.plain_result("用法：/mem import <zip 路径>")
                    return
                target = Path(" ".join(args).strip().strip('"').strip("'"))
                if not target.is_absolute():
                    target = (self.data_dir / target).resolve()
                media_dir = Path(self._cfg("media_dir") or (self.data_dir / "media"))
                stats = await asyncio.to_thread(
                    import_bundle,
                    self.db,
                    target,
                    media_dir=media_dir,
                )
                yield event.plain_result(
                    "导入完成："
                    f"scope {stats.get('scopes', 0)}，绑定 {stats.get('bindings', 0)}，"
                    f"群映射 {stats.get('aliases', 0)}，"
                    f"消息 {stats['messages']}，话题 {stats['topics']}，"
                    f"items {stats.get('items', 0)}，"
                    f"附件 {stats['attachments']}，任务 {stats['jobs']}，"
                    f"媒体文件 {stats.get('media', 0)}。\n"
                    "用 /mem scopes 查看导入的 scope，再用 "
                    "/mem bind <scope_id> 把当前会话绑过去。",
                )
            elif sub == "rebuild":
                count = self.db.rebuild_fts()
                yield event.plain_result(f"检索索引已重建，共 {count} 条。")
            else:
                yield event.plain_result(HELP_TEXT)
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory: 命令失败 %s", sub)
            yield event.plain_result(f"执行失败：{exc}")

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        if not bool(self._cfg("admin_only")):
            return True
        try:
            return bool(event.is_admin())
        except Exception:  # noqa: BLE001
            return False

    def _stats_text(self) -> str:
        data = self.db.counts()
        lines = [
            "BBR的记忆插件 v1.0.5",
            f"数据库：{self.db.path}",
            f"消息：{data['messages']} 条；附件：{data['attachments']} 个；"
            f"DB：{data['db_bytes'] / 1024 / 1024:.2f} MB",
            f"上次记忆整理：{self.db.get_meta('last_daily_run', '（无）')}",
            f"scope：{data.get('scopes', 0)} 个；绑定：{data.get('bindings', 0)} 个",
            f"FTS：{'可用' if self.db.fts_enabled else '不可用'}",
        ]
        for row in data["topics"]:
            lines.append(f"  {row['layer']}/{row['status']}：{row['n']} 条")
        return "\n".join(lines)

    def _scope_line(self, scope: dict[str, Any]) -> str:
        return (
            f"{scope['scope_id'][:12]}｜{scope.get('kind')}｜"
            f"{scope.get('label') or scope.get('external_id') or '未命名'}｜"
            f"消息 {scope.get('msg_count', 0)}｜话题 {scope.get('topic_count', 0)}"
        )

    def _whoami_text(self, event: AstrMessageEvent) -> str:
        umo = str(event.unified_msg_origin)
        binding_key = self._binding_key(event)
        alias_key = self._alias_key(event)
        binding = self.db.get_session_binding(binding_key)
        alias = self.db.get_scope_alias(alias_key)
        scope_id = self._event_scope(event)
        scope = self.db.get_scope(scope_id)
        lines = [
            f"当前会话：{umo}",
            f"类型：{self._event_kind(event)}",
            f"binding key（会话键）：{binding_key}",
            "  只影响当前会话；/mem bind 使用",
            f"alias key（身份键）：{alias_key}",
            "  影响该身份下所有会话；/mem alias 使用",
            f"scope：{scope_id}",
            f"绑定：{'是' if binding else '否（使用默认 scope）'}",
        ]
        if alias:
            lines.append(f"身份映射：{alias['scope_id']}")
        if scope:
            lines.append(
                f"名称：{scope.get('label') or '（无）'}；"
                f"平台：{scope.get('platform')}；外部 ID：{scope.get('external_id')}",
            )
        lines.append("优先级：binding > alias > 默认 scope")
        lines.append("切换记忆：/mem bind <scope_id>；查看全部：/mem scopes")
        return "\n".join(lines)

    def _scopes_text(self) -> str:
        scopes = self.db.list_scopes(200)
        if not scopes:
            return "还没有 scope。先发几条消息。"
        lines = [f"共 {len(scopes)} 个 scope："]
        for scope in scopes:
            lines.append("- " + self._scope_line(scope))
        return "\n".join(lines)

    def _format_item(self, item: dict[str, Any]) -> str:
        due = parse_iso(item.get("due_at"))
        due_text = due.astimezone(self.tz).strftime("%m-%d %H:%M") if due else "—"
        return (
            f"{item['id'][:12]}｜{item.get('type')}/{item.get('status')}"
            f"｜到期 {due_text}｜{item.get('text')}"
        )

    def _items_text(self, event: AstrMessageEvent, scope_prefix: str) -> str:
        if scope_prefix:
            scope = self._find_scope(scope_prefix)
            if not scope:
                return "没有找到这个 scope。"
            scope_id = scope["scope_id"]
        else:
            scope_id = self._event_scope(event)
        items = self.db.list_items(scope_id, limit=50)
        if not items:
            return "这段记忆还没有 items。"
        lines = [f"共 {len(items)} 条："]
        lines.extend("- " + self._format_item(item) for item in items)
        return "\n".join(lines)

    def _due_text(self, event: AstrMessageEvent, hours: int) -> str:
        now = utc_now()
        until = to_iso(now + timedelta(hours=max(1, hours)))
        items = self.db.due_items(
            self._event_scope(event),
            until_iso=until,
            limit=50,
        )
        if not items:
            return f"{hours} 小时内没有到期待办。"
        lines = [f"{hours} 小时内到期 {len(items)} 条："]
        lines.extend("- " + self._format_item(item) for item in items)
        return "\n".join(lines)

    def _done_text(self, item_prefix: str, done: bool) -> str:
        if not item_prefix:
            return "用法：/mem done|cancel <item_id>"
        item = self._find_item(item_prefix)
        if not item:
            return "没有找到这个 item。"
        now = utc_now()
        if done:
            ok = self.db.finish_item(
                item["id"],
                search_until=to_iso(now + timedelta(days=30)),
                now_iso=to_iso(now),
            )
        else:
            ok = self.db.cancel_item(item["id"], now_iso=to_iso(now))
        if not ok:
            return "这条 item 不是 pending 状态。"
        self.db.enqueue_outbox(
            item_id=item["id"],
            action="done" if done else "cancel",
            payload={"item_id": item["id"]},
        )
        return f"已{'完成' if done else '取消'}：{item.get('text')}"

    def _reschedule_text(self, item_prefix: str, time_text: str) -> str:
        item = self._find_item(item_prefix)
        if not item:
            return "没有找到这个 item。"
        if item.get("status") != "pending":
            return "这条 item 不是 pending 状态，不能改时间。"
        now = utc_now()
        due, ambiguous = parse_time_spec(time_text, now, self.tz)
        if ambiguous:
            return (
                f"「{time_text}」有歧义：是上午还是晚上？\n"
                "请用 /mem reschedule <item_id> 上午11点 或 晚上11点 重新发一次。"
            )
        if not due:
            return "没解析出时间。请用 23:00、明天晚上11点 这类写法。"
        if due <= now:
            return "这个时间已经过去了，请换一个未来时间。"
        self.db.update_item(
            item["id"],
            type="reminder",
            due_at=to_iso(due),
            inject_until=to_iso(due + timedelta(days=1)),
            status="pending",
        )
        self.db.enqueue_outbox(
            item_id=item["id"],
            action="upsert",
            payload={
                "item_id": item["id"],
                "text": item.get("text", ""),
                "due_at": to_iso(due),
                "timezone": str(self.tz),
                "dedupe_key": f"{item.get('scope_id')}|{to_iso(due)}|{str(item.get('text'))[:60]}",
            },
        )
        local = due.astimezone(self.tz).strftime("%Y-%m-%d %H:%M")
        return f"已改到 {local}：{item.get('text')}"

    def _find_item(self, prefix: str) -> dict[str, Any] | None:
        prefix = (prefix or "").strip()
        if not prefix:
            return None
        with self.db._lock:
            row = self.db.conn.execute(
                "SELECT id FROM memory_items WHERE id LIKE ? LIMIT 1",
                (prefix + "%",),
            ).fetchone()
        return self.db.get_item(row["id"]) if row else None

    def _cancel_all_text(self, event: AstrMessageEvent) -> str:
        scope_id = self._event_scope(event)
        now = utc_now()
        ids = self.db.cancel_all_items(scope_id, now_iso=to_iso(now))
        for item_id in ids:
            self.db.enqueue_outbox(
                item_id=item_id,
                action="cancel",
                payload={"item_id": item_id},
            )
        if not ids:
            return "当前会话没有可取消的待办。"
        return f"已取消 {len(ids)} 条待办。"

    def _bind_text(self, event: AstrMessageEvent, prefix: str) -> str:
        if not prefix:
            return "用法：/mem bind <scope_id 前缀>"
        scope = self._find_scope(prefix)
        if not scope:
            return "没有找到这个 scope。用 /mem scopes 查看。"
        try:
            self.db.bind_session(
                self._binding_key(event),
                scope["scope_id"],
                bound_by=str(event.get_sender_id() or ""),
                kind=self._event_kind(event),
            )
        except ValueError as exc:
            return str(exc)
        return (
            f"已绑定到 {scope['scope_id']}（{scope.get('label') or scope.get('external_id')}）。\n"
            "之后这个会话的检索和注入都走这段记忆。"
        )

    def _unbind_text(self, event: AstrMessageEvent) -> str:
        removed = self.db.unbind_session(self._binding_key(event))
        return "已取消绑定，回到默认 scope。" if removed else "当前没有绑定。"

    def _alias_text(self, args: list[str]) -> str:
        action = args[0].lower() if args else "list"
        if action == "list":
            aliases = self.db.list_scope_aliases()
            if not aliases:
                return "还没有群映射。用 /mem alias set <alias_key> <scope_id> 添加。"
            lines = [f"共 {len(aliases)} 条映射："]
            for item in aliases:
                lines.append(
                    f"- {item['alias_key']} → {item['scope_id'][:12]}"
                    f"{('｜' + item['note']) if item.get('note') else ''}",
                )
            return "\n".join(lines)
        if action == "set":
            if len(args) < 3:
                return "用法：/mem alias set <alias_key> <scope_id> [note]"
            note = " ".join(args[3:]) if len(args) > 3 else ""
            if not self.db.set_scope_alias(args[1], args[2], note=note):
                return "没有找到目标 scope。用 /mem scopes 查看。"
            return f"已设置映射：{args[1]} → {args[2]}"
        if action in {"del", "delete"}:
            if len(args) < 2:
                return "用法：/mem alias del <alias_key>"
            return "已删除映射。" if self.db.delete_scope_alias(args[1]) else "没有这条映射。"
        return "用法：/mem alias list | set <alias_key> <scope_id> | del <alias_key>"

    def _merge_text(self, from_prefix: str, to_prefix: str) -> str:
        source = self._find_scope(from_prefix)
        target = self._find_scope(to_prefix)
        if not source or not target:
            return "没有找到源或目标 scope。"
        if normalize_kind(source.get("kind")) != normalize_kind(target.get("kind")):
            return "不能跨类型合并（私聊只能并私聊，群只能并群）。"
        moved = self.db.merge_scopes(source["scope_id"], target["scope_id"])
        return (
            f"已合并：{source['scope_id'][:12]} → {target['scope_id'][:12]}，"
            f"移动 {moved} 条记录。"
        )

    def _search_text(self, umo: str, query: str) -> str:
        if not query:
            return "用法：/mem search <关键词>"
        topics = self.retriever.search(umo, query, now=utc_now(), deep=True)
        if not topics:
            return "没有检索到相关记忆。"
        lines = [f"检索到 {len(topics)} 条："]
        for topic in topics:
            lines.append(
                f"- {topic['day']}｜{topic['title']}｜重要度 {topic['importance']}"
                f"｜{topic['id'][:12]}",
            )
        return "\n".join(lines)

    def _open_text(self, topic_id: str) -> str:
        if not topic_id:
            return "用法：/mem open <id 前缀>"
        topic = self._find_topic(topic_id)
        if not topic:
            return "没有找到这条记忆。"
        messages = self.db.get_messages_between_uids(
            topic["umo"],
            topic.get("source_start_uid", ""),
            topic.get("source_end_uid", ""),
        )
        if not messages:
            return "这条记忆没有可回取的原文。"
        self.retriever.renew([topic], now=utc_now())
        lines = [f"{topic['day']}｜{topic['title']}｜原文："]
        for msg in messages[:40]:
            atts = self.db.get_attachments(msg["msg_uid"])
            text = render_message_text(str(msg.get("content") or ""), atts)
            who = msg.get("sender_name") or msg.get("role")
            lines.append(f"[{msg['msg_uid']}] {who}: {text}")
        return "\n".join(lines)[:3500]

    def _why_text(self, topic_id: str) -> str:
        topic = self._find_topic(topic_id) if topic_id else None
        if not topic:
            return "用法：/mem why <id 前缀>"
        return (
            f"标题：{topic['title']}\n"
            f"层：{topic['layer']}；重要度：{topic['importance']}；TTL：{topic['ttl_days']} 天\n"
            f"创建：{topic['created_at']}；过期：{topic['expire_at']}\n"
            f"命中：{topic['hit_count']} 次；续期：{topic['renew_count']} 次；"
            f"固定：{'是' if topic['pinned'] else '否'}\n"
            f"抑制到：{topic.get('suppress_until') or '（无）'}\n"
            f"关键词：{'、'.join(topic.get('keywords') or [])}\n"
            f"事实：{'；'.join(topic.get('facts') or []) or '（无）'}"
        )

    def _pin_text(self, topic_id: str, pinned: bool) -> str:
        topic = self._find_topic(topic_id) if topic_id else None
        if not topic:
            return "用法：/mem pin|unpin <id 前缀>"
        self.db.set_pinned(topic["id"], pinned)
        return f"已{'固定' if pinned else '取消固定'}：{topic['title']}"

    def _mute_text(self, topic_id: str, days: int) -> str:
        topic = self._find_topic(topic_id) if topic_id else None
        if not topic:
            return "用法：/mem mute <id 前缀> [天]"
        until = to_iso(utc_now() + timedelta(days=max(1, days)))
        self.db.set_suppress(topic["id"], until)
        return f"已静音到 {until}：{topic['title']}"

    def _forget_text(self, topic_id: str) -> str:
        topic = self._find_topic(topic_id) if topic_id else None
        if not topic:
            return "用法：/mem forget <id 前缀>"
        self.db.delete_topic(topic["id"])
        return f"已软删除：{topic['title']}"

    def _idle_text(self) -> str:
        idle = int(self._idle_seconds())
        return (
            f"空闲：{idle // 60} 分 {idle % 60} 秒\n"
            f"判定阈值：{self._cfg('idle_minutes')} 分钟\n"
            f"记忆整理时间：{self._cfg('run_at')}；强制上限：{self._cfg('force_after_hours')} 小时\n"
            f"上次记忆整理：{self.db.get_meta('last_daily_run', '（无）')}"
        )

    def _backup_before_rebuild(self) -> Path:
        stamp = utc_now().strftime("%Y%m%d_%H%M%S")
        path = self.data_dir / "backups" / f"memory_before_rebuild_{stamp}.db"
        return self.db.backup_to(path)

    def _stats_line(self, stats: dict[str, Any]) -> str:
        return (
            f"记忆整理完成：处理 {stats.get('days', 0)} 天，新增 {stats.get('topics', 0)} 条，"
            f"迁移 {stats.get('promoted', 0)} 条，过期 {stats.get('expired', 0)} 条，"
            f"清理 {stats.get('purged', 0)} 条，跳过 {stats.get('skipped', 0)} 天。"
        )

    def _find_topic(self, prefix: str) -> dict[str, Any] | None:
        prefix = (prefix or "").strip()
        if not prefix:
            return None
        with self.db._lock:  # 只读查找，借用同一把锁
            row = self.db.conn.execute(
                "SELECT id FROM topics WHERE id LIKE ? ORDER BY created_at DESC LIMIT 1",
                (prefix + "%",),
            ).fetchone()
        return self.db.get_topic(row["id"]) if row else None

    def _find_scope(self, prefix: str) -> dict[str, Any] | None:
        prefix = (prefix or "").strip()
        if not prefix:
            return None
        with self.db._lock:
            row = self.db.conn.execute(
                "SELECT scope_id FROM scopes WHERE scope_id LIKE ? LIMIT 1",
                (prefix + "%",),
            ).fetchone()
        return self.db.get_scope(row["scope_id"]) if row else None

    # -- LLM 工具 ---------------------------------------------------------

    @filter.llm_tool(name="memory_search_v01")
    async def memory_search_tool(self, event: AstrMessageEvent, query: str) -> str:
        """搜索长期记忆。用户提到“上次/之前/你还记得”或需要旧信息时使用。

        Args:
            query(string): 要搜索的关键词或问题
        """
        topics = self.retriever.search(
            str(event.unified_msg_origin),
            query,
            now=utc_now(),
            deep=True,
            scope_id=self._event_scope(event),
        )
        if not topics:
            return "没有找到相关记忆。"
        self.retriever.renew(topics, now=utc_now())
        block = self.retriever.build_block(topics, budget_chars=1500)
        return block or "没有找到相关记忆。"

    @filter.llm_tool(name="memory_open_v01")
    async def memory_open_tool(self, event: AstrMessageEvent, topic_id: str) -> str:
        """打开某条记忆的原始对话。先用 memory_search_v01 拿到 topic id。

        Args:
            topic_id(string): 记忆 ID，可从搜索结果里的 topic= 后取得
        """
        topic = self._find_topic(topic_id)
        if not topic:
            return "没有找到这条记忆。"
        messages = self.db.get_messages_between_uids(
            topic["umo"],
            topic.get("source_start_uid", ""),
            topic.get("source_end_uid", ""),
        )
        if not messages:
            return "这条记忆没有可回取的原文。"
        self.retriever.renew([topic], now=utc_now())
        lines = []
        for msg in messages[:30]:
            atts = self.db.get_attachments(msg["msg_uid"])
            text = render_message_text(str(msg.get("content") or ""), atts)
            who = msg.get("sender_name") or msg.get("role")
            lines.append(f"[{msg['msg_uid']}] {who}: {text}")
        return "\n".join(lines)[:3000]

    # -- 提醒工具（供模型调用，不主动注入 pending items） -----------------

    async def _finalize_reminder(
        self,
        event: AstrMessageEvent,
        item_id: str,
        text: str,
        due: datetime,
        scope_id: str,
        umo: str,
    ) -> str:
        """明确时间后落库：future_task 后端只记 event；memory 后端写 outbox。"""
        if self._reminder_backend(event) == "future_task":
            try:
                job_id = await self._schedule_future_task(event, text, due)
            except Exception:  # noqa: BLE001
                logger.exception("memory: future_task 排期失败，回退到 outbox")
                job_id = None
            if job_id:
                self.db.update_item(
                    item_id,
                    type="event",
                    status="active",
                    due_at=to_iso(due),
                    inject_until=to_iso(utc_now()),
                    keywords=[
                        "提醒",
                        "backend:future_task",
                        f"job:{job_id}",
                    ],
                )
                return f"已创建提醒 item={item_id}，由 future_task 发送，时间={to_iso(due)}。"
        self.db.update_item(
            item_id,
            type="reminder",
            status="pending",
            due_at=to_iso(due),
            inject_until=to_iso(due + timedelta(days=1)),
            keywords=["提醒", "backend:memory"],
        )
        self.db.enqueue_outbox(
            item_id=item_id,
            action="upsert",
            payload={
                "item_id": item_id,
                "scope_id": scope_id,
                "umo": umo,
                "text": text,
                "due_at": to_iso(due),
                "timezone": str(self.tz),
                "dedupe_key": f"{scope_id}|{to_iso(due)}|{text[:60]}",
            },
        )
        return f"已创建提醒 item={item_id}，时间={to_iso(due)}。"

    def _job_id(self, item: dict[str, Any]) -> str:
        for key in item.get("keywords") or []:
            if str(key).startswith("job:"):
                return str(key).split(":", 1)[1]
        return ""

    def _is_future_task_item(self, item: dict[str, Any]) -> bool:
        return "backend:future_task" in (item.get("keywords") or [])

    def _clarify_hour(self, item: dict[str, Any]) -> int | None:
        for key in item.get("keywords") or []:
            if str(key).startswith("clarify:"):
                try:
                    return int(str(key).split(":", 1)[1])
                except ValueError:
                    return None
        return None

    def _clarify_period(self, item: dict[str, Any]) -> str:
        for key in item.get("keywords") or []:
            if str(key).startswith("clarify-period:"):
                return str(key).split(":", 1)[1]
        return ""

    def _clarify_date(self, item: dict[str, Any]):
        for key in item.get("keywords") or []:
            if str(key).startswith("clarify-date:"):
                try:
                    return datetime.fromisoformat(
                        str(key).split(":", 1)[1],
                    ).date()
                except ValueError:
                    return None
        return None

    def _clarify_minute(self, item: dict[str, Any]) -> int:
        for key in item.get("keywords") or []:
            if str(key).startswith("clarify-minute:"):
                try:
                    return int(str(key).split(":", 1)[1])
                except ValueError:
                    return 0
        return 0

    def _period_due(
        self,
        period: str,
        hour: int,
        now: datetime,
        *,
        minute: int = 0,
        on_date=None,
    ) -> datetime | None:
        if period in {"下午", "傍晚", "晚上", "夜里"} and hour < 12:
            hour += 12
        if period == "中午" and hour < 11:
            hour += 12
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        now_local = now.astimezone(self.tz)
        day = on_date or now_local.date()
        due = datetime.combine(day, dtime(hour, minute), tzinfo=self.tz)
        if due <= now_local and on_date is None:
            due += timedelta(days=1)
        return due.astimezone(timezone.utc)

    def _time_for_item(
        self,
        item: dict[str, Any],
        time_text: str,
        now: datetime,
    ) -> datetime | None:
        due, _ambiguous = parse_time_spec(time_text, now, self.tz)
        if due:
            return due
        period = re.search(r"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)", time_text or "")
        period_text = period.group(1) if period else self._clarify_period(item)
        hour = _hour_from_text(time_text)
        if hour is None:
            hour = self._clarify_hour(item)
        if period_text and hour is not None:
            return self._period_due(
                period_text,
                hour,
                now,
                minute=_minute_from_text(time_text) or self._clarify_minute(item),
                on_date=self._clarify_date(item),
            )
        return None

    def _draft_period_due(
        self,
        item: dict[str, Any],
        time_text: str,
        user_text: str,
        now: datetime,
    ) -> datetime | None:
        """用户只答“上午/下午”时，用草稿里保存的小时补全。"""
        period = re.search(r"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)", time_text or "")
        stored_period = self._clarify_period(item)
        if stored_period:
            # 草稿问的是“中午几点/下午几点”，以草稿时段为准，
            # 不允许模型在回答“十一点”时自己改成上午。
            period_text = stored_period
        else:
            if not period or period.group(1) not in (user_text or ""):
                return None
            period_text = period.group(1)
        hour = _hour_from_text(time_text)
        if hour is None:
            hour = self._clarify_hour(item)
        minute = _minute_from_text(time_text)
        if not minute:
            minute = self._clarify_minute(item)
        if not period_text or hour is None:
            return None
        return self._period_due(
            period_text,
            hour,
            now,
            minute=minute,
            on_date=self._clarify_date(item),
        )

    def _resolve_tool_time(
        self,
        event: AstrMessageEvent,
        time_text: str,
        date_text: str,
        clock_text: str,
        now: datetime,
    ) -> tuple[datetime | None, bool, str]:
        """把工具传入的日期/时间拆开校验，允许跨轮组合。"""
        user_text = self._user_text(event)
        history = self.db.recent_user_text(str(event.unified_msg_origin), 10)
        time_text = (time_text or "").strip()
        date_text = (date_text or "").strip() or _extract_date_text(time_text)
        clock_text = (clock_text or "").strip() or _extract_clock_text(time_text)
        if not date_text:
            # 当前只说了“下午三点”，日期在上一条“四号下午”里。
            date_text = _extract_date_text(history)
        if not clock_text:
            return None, False, "没解析出具体时间。请让用户给出几点。"
        if date_text and not time_text_grounded(user_text, date_text, history):
            now_local = now.astimezone(self.tz)
            claimed = _date_from_text(date_text, now_local)
            equivalents = set()
            for source in (user_text, history):
                for part in _extract_date_texts(source):
                    parsed = _date_from_text(part, now_local)
                    if parsed:
                        equivalents.add(parsed)
            if not claimed or claimed not in equivalents:
                return None, False, "日期必须来自用户原话或最近对话，不能自己编。"
        if not time_text_grounded(user_text, clock_text, history):
            return None, False, "时间必须来自用户原话，不能自己补时段。"
        due, ambiguous = parse_structured_time(date_text, clock_text, now, self.tz)
        if due is None and not ambiguous:
            return None, False, "这个时间已经过去或日期无效，请让用户确认。"
        return due, ambiguous, ""

    def _find_clarification_item(
        self,
        scope_id: str,
        text: str,
    ) -> dict[str, Any] | None:
        target = text.strip()
        for item in self.db.list_items(scope_id, limit=50, include_done=True):
            if item.get("status") != "needs_clarification":
                continue
            item_text = str(item.get("text") or "").strip()
            if item_text == target or target in item_text or item_text in target:
                return item
        return None

    def _resolve_item_for_tool(self, event: AstrMessageEvent, ref: str):
        scope_id = self._event_scope(event)
        ref = (ref or "").strip()
        if ref:
            item = self._find_item(ref)
            if item and item.get("scope_id") == scope_id:
                return item
            for item in self.db.list_items(scope_id, limit=50, include_done=True):
                if item.get("status") not in {"pending", "needs_clarification"} and not (
                    item.get("status") == "active" and self._is_future_task_item(item)
                ):
                    continue
                if ref in str(item.get("text") or ""):
                    return item
        for item in self.db.list_items(scope_id, limit=50, include_done=True):
            if (
                item.get("status") in {"pending", "needs_clarification"}
                or (item.get("status") == "active" and self._is_future_task_item(item))
            ) and item.get("type") in {"reminder", "task", "event"}:
                return item
        return None

    @filter.llm_tool(name="memory_reminder_list")
    async def memory_reminder_list_tool(self, event: AstrMessageEvent) -> str:
        """列出当前会话的待办/提醒。修改、取消、完成前如果不确定是哪一条，先调用本工具。

        返回每条 item 的 id、状态、到期时间和内容。
        """
        scope_id = self._event_scope(event)
        items = [
            item
            for item in self.db.list_items(scope_id, limit=50, include_done=False)
            if item.get("status") in {"pending", "needs_clarification"}
            or (item.get("status") == "active" and self._is_future_task_item(item))
        ]
        if not items:
            return "当前没有待办或提醒。"
        lines = [f"当前待办 {len(items)} 条："]
        for item in items:
            due = parse_iso(item.get("due_at"))
            due_text = (
                due.astimezone(self.tz).strftime("%m-%d %H:%M")
                if due
                else "未定"
            )
            lines.append(
                f"- item={item['id'][:8]}｜{item.get('status')}"
                f"｜到期 {due_text}｜{item.get('text')}",
            )
        return "\n".join(lines)

    @filter.llm_tool(name="memory_reminder_create")
    async def memory_reminder_create_tool(
        self,
        event: AstrMessageEvent,
        text: str,
        time_text: str = "",
        date_text: str = "",
        clock_text: str = "",
    ) -> str:
        """创建一条提醒。时间最好拆成 date_text 和 clock_text。

        不确定是哪一条时，先调用 memory_reminder_list。
        date_text 只传日期（四号/11月4日/下周三/明天），clock_text 只传钟点（下午三点/晚上八点）。
        用户只给“六点”且没写上午/下午/晚上时，本工具会返回需要确认。

        Args:
            text(string): 提醒内容，例如“买衣服”
            time_text(string): 可选，整句时间；不拆字段时使用
            date_text(string): 可选，日期部分，例如“四号”“下周三”“明天”
            clock_text(string): 可选，时间部分，例如“下午三点”“晚上八点”
        """
        now = utc_now()
        time_text = (time_text or "").strip()
        if not time_text:
            time_text = (date_text + clock_text).strip()
        scope_id = self._event_scope(event)
        user_text = self._user_text(event)
        draft = self._find_clarification_item(scope_id, text)
        if draft:
            if draft.get("source_start_uid") == self._message_uid(event):
                return (
                    "时间不明确。请先问用户具体几点，"
                    "不要在同一轮里自己补全时钟。"
                )
            due = self._draft_period_due(
                draft, clock_text or time_text, user_text, now,
            )
            if not due:
                due, _ambiguous, _error = self._resolve_tool_time(
                    event, time_text, date_text, clock_text, now,
                )
            if due and due > now:
                return await self._finalize_reminder(
                    event,
                    draft["id"],
                    str(draft.get("text") or text),
                    due,
                    scope_id,
                    str(event.unified_msg_origin),
                )
            return "时间仍不明确。请先问用户是上午还是晚上。"
        due, ambiguous, error = self._resolve_tool_time(
            event, time_text, date_text, clock_text, now,
        )
        if error:
            return error
        if ambiguous:
            clock_source = clock_text or time_text
            keywords = [
                "提醒",
                f"clarify:{_hour_from_text(clock_source) or 0}",
            ]
            period = re.search(
                r"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)", clock_source,
            )
            if period:
                keywords.append(f"clarify-period:{period.group(1)}")
            date_hint = date_text or _extract_date_text(time_text)
            if date_hint:
                parsed_date = _date_from_text(date_hint, now.astimezone(self.tz))
                if parsed_date:
                    keywords.append(f"clarify-date:{parsed_date.isoformat()}")
            minute = _minute_from_text(clock_source)
            if minute:
                keywords.append(f"clarify-minute:{minute}")
            item_id = self.db.insert_item(
                {
                    "scope_id": self._event_scope(event),
                    "type": "task",
                    "text": str(text or "")[:300],
                    "keywords": keywords,
                    "importance": 200,
                    "status": "needs_clarification",
                    "source_start_uid": self._message_uid(event),
                },
            )
            return (
                f"时间不明确（item={item_id[:8]}）。请先问用户是上午还是晚上，"
                "不要直接创建。"
            )
        if not due:
            return "没解析出时间。请让用户给出具体时间，例如“明天上午十点”。"
        if due <= now:
            return "这个时间已经过去，请向用户确认新的时间。"
        scope_id = self._event_scope(event)
        umo = str(event.unified_msg_origin)
        text = str(text or "").strip()[:300]
        if not text:
            return "提醒内容为空。"
        source_time = (date_text + clock_text).strip() or time_text
        summary_text = text + (f"（{source_time}）" if source_time else "")
        topic_id = self.db.insert_topic(
            {
                "umo": umo,
                "scope_id": scope_id,
                "day": local_day(now, self.tz),
                "title": truncate_at_sentence("提醒：" + text, 30),
                "keywords": ["提醒"],
                "summary_search": summary_text,
                "summary_week": summary_text,
                "facts": [text] + ([f"时间：{source_time}"] if source_time else []),
                "importance": 200,
                "layer": "week",
                "ttl_days": 30,
                "expire_at": to_iso(now + timedelta(days=30)),
                "inject_until": to_iso(now),
                "search_until": None,
                "prompt_version": "tool-v1.0.5",
            },
        )
        item_id = self.db.insert_item(
            {
                "scope_id": scope_id,
                "topic_id": topic_id,
                "type": "reminder",
                "text": text,
                "keywords": ["提醒"],
                "importance": 200,
                "due_at": to_iso(due),
                "status": "pending",
                "inject_until": to_iso(now),
                "search_until": None,
            },
        )
        return await self._finalize_reminder(
            event,
            item_id,
            text,
            due,
            scope_id,
            umo,
        )

    @filter.llm_tool(name="memory_reminder_update")
    async def memory_reminder_update_tool(
        self,
        event: AstrMessageEvent,
        item: str,
        time_text: str = "",
        date_text: str = "",
        clock_text: str = "",
    ) -> str:
        """修改已有提醒的时间。item 可以是 id 前缀或提醒内容中的词。

        不确定是哪一条时，先调用 memory_reminder_list 查看待办列表。
        date_text 只传日期，clock_text 只传钟点；不要自己换算成 24 小时制。

        Args:
            item(string): item id 前缀或提醒内容关键词，例如“买衣服”
            time_text(string): 可选，整句时间；不拆字段时使用
            date_text(string): 可选，日期部分，例如“四号”“下周三”
            clock_text(string): 可选，时间部分，例如“下午三点”
        """
        target = self._resolve_item_for_tool(event, item)
        if not target:
            return "没有找到待修改的提醒。"
        now = utc_now()
        if target.get("status") == "needs_clarification":
            return await self.memory_reminder_create_tool(
                event,
                str(target.get("text") or ""),
                time_text,
                date_text,
                clock_text,
            )
        due, ambiguous, error = self._resolve_tool_time(
            event, time_text, date_text, clock_text, now,
        )
        if error:
            return error
        if ambiguous and not due:
            clock_source = clock_text or time_text
            keywords = [
                "提醒",
                f"clarify:{_hour_from_text(clock_source) or 0}",
            ]
            period = re.search(
                r"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)", clock_source,
            )
            if period:
                keywords.append(f"clarify-period:{period.group(1)}")
            date_hint = date_text or _extract_date_text(time_text)
            if date_hint:
                parsed_date = _date_from_text(date_hint, now.astimezone(self.tz))
                if parsed_date:
                    keywords.append(f"clarify-date:{parsed_date.isoformat()}")
            minute = _minute_from_text(clock_source)
            if minute:
                keywords.append(f"clarify-minute:{minute}")
            self.db.update_item(
                target["id"],
                status="needs_clarification",
                keywords=keywords,
            )
            return "时间不明确。请先问用户是上午还是晚上，再调用本工具。"
        if not due:
            return "没解析出时间。请让用户给出具体时间。"
        if due <= now:
            return "这个时间已经过去，请确认新的时间。"
        job_id = self._job_id(target)
        if job_id:
            if not await self._update_future_task(job_id, due):
                return "future_task 里的任务已经不存在，请重新创建提醒。"
            self.db.update_item(
                target["id"],
                type="event",
                status="active",
                due_at=to_iso(due),
                inject_until=to_iso(now),
                keywords=["提醒", "backend:future_task", f"job:{job_id}"],
            )
            return f"已更新 item={target['id']}，时间={to_iso(due)}。"
        return await self._finalize_reminder(
            event,
            target["id"],
            str(target.get("text") or ""),
            due,
            str(target.get("scope_id") or self._event_scope(event)),
            str(event.unified_msg_origin),
        )

    @filter.llm_tool(name="memory_reminder_done")
    async def memory_reminder_done_tool(
        self,
        event: AstrMessageEvent,
        item: str,
    ) -> str:
        """把提醒标记为完成。调用成功后才能告诉用户“办好了”。

        不确定是哪一条时，先调用 memory_reminder_list。

        Args:
            item(string): item id 前缀或提醒内容关键词
        """
        target = self._resolve_item_for_tool(event, item)
        if not target:
            return "没有找到待完成的提醒。"
        now = utc_now()
        job_id = self._job_id(target)
        if job_id:
            await self._delete_future_task(job_id)
            self.db.update_item(
                target["id"],
                status="done",
                done_at=to_iso(now),
                inject_until=to_iso(now),
                search_until=to_iso(now + timedelta(days=30)),
            )
            return f"已完成 item={target['id']}。"
        if not self.db.finish_item(
            target["id"],
            search_until=to_iso(now + timedelta(days=30)),
            now_iso=to_iso(now),
        ):
            return "这条提醒不是 pending 状态。"
        self.db.enqueue_outbox(
            item_id=target["id"], action="done", payload={"item_id": target["id"]},
        )
        return f"已完成 item={target['id']}。"

    @filter.llm_tool(name="memory_reminder_cancel")
    async def memory_reminder_cancel_tool(
        self,
        event: AstrMessageEvent,
        item: str,
    ) -> str:
        """取消一条提醒。

        不确定是哪一条时，先调用 memory_reminder_list。

        Args:
            item(string): item id 前缀或提醒内容关键词
        """
        target = self._resolve_item_for_tool(event, item)
        if not target:
            return "没有找到待取消的提醒。"
        now = utc_now()
        job_id = self._job_id(target)
        if job_id:
            await self._delete_future_task(job_id)
            self.db.update_item(
                target["id"],
                status="cancelled",
                inject_until=to_iso(now),
            )
            return f"已取消 item={target['id']}。"
        if not self.db.cancel_item(target["id"], now_iso=to_iso(now)):
            return "这条提醒不是 pending 状态。"
        self.db.enqueue_outbox(
            item_id=target["id"], action="cancel", payload={"item_id": target["id"]},
        )
        return f"已取消 item={target['id']}。"

    @filter.on_llm_tool_respond()
    async def mirror_future_task(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict[str, Any] | None,
        tool_result: Any,
    ) -> None:
        """把 AstrBot 内置 future_task 的 create 同步记进 memory_items。

        模型经常优先选择内置 future_task；如果不同步，/mem items 会看不到提醒。
        """
        if getattr(tool, "name", "") != "future_task":
            return
        args = dict(tool_args or {})
        if args.get("action") != "create":
            return
        if bool(getattr(tool_result, "isError", False)):
            return
        now = utc_now()
        scope_id = self._event_scope(event)
        umo = str(event.unified_msg_origin)
        name = str(args.get("name") or "").strip()
        note = str(args.get("note") or "").strip()
        text = (name + ("：" + note if note and note != name else "")).strip()
        if not text:
            return
        due = parse_iso(str(args.get("run_at") or ""))
        due_iso = to_iso(due) if due else None
        if self.db.item_exists_by_text_due(scope_id, text, due_iso):
            return
        item_type = "event"
        topic_id = self.db.insert_topic(
            {
                "umo": umo,
                "scope_id": scope_id,
                "day": local_day(now, self.tz),
                "title": truncate_at_sentence("提醒：" + text, 30),
                "keywords": ["提醒", "future_task"],
                "summary_search": text,
                "summary_week": text,
                "facts": [text],
                "importance": 200,
                "layer": "week",
                "ttl_days": 30,
                "expire_at": to_iso(now + timedelta(days=30)),
                "inject_until": to_iso(now),
                "search_until": None,
                "prompt_version": "future_task-bridge",
            },
        )
        self.db.insert_item(
            {
                "scope_id": scope_id,
                "topic_id": topic_id,
                "type": item_type,
                "text": text,
                "keywords": ["提醒", "future_task"],
                "importance": 200,
                "due_at": due_iso,
                "status": "active",
                # future_task 自己负责发送，记忆里只留可检索记录，避免重复提醒。
                "inject_until": to_iso(now),
                "search_until": None,
            },
        )

    # -- 卸载 -------------------------------------------------------------

    async def terminate(self) -> None:
        self._closing = True
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            self.db.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 平台组件映射（v0.1 宽松处理，不硬编码某个平台）
# ---------------------------------------------------------------------------


def parse_fast_reminder_json(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "is_reminder": bool(data.get("is_reminder")),
        "text": str(data.get("text") or ""),
        "due_at": data.get("due_at"),
        "confidence": float(data.get("confidence") or 0),
    }


def _component_kind(name: str, comp: Any) -> str:
    sub_type = str(getattr(comp, "sub_type", "") or "").lower()
    if name in {"record", "voice", "audio"}:
        return "voice"
    if name in {"image", "picture"}:
        return "sticker" if sub_type in {"sticker", "mface", "emoji"} else "image"
    if name in {"mface", "face", "emoji"}:
        return "sticker" if name != "face" else "face"
    if name in {"video", "shortvideo"}:
        return "video"
    if name == "file":
        return "file"
    return ""


def _guess_mime(ref: str) -> str:
    ref = (ref or "").lower().split("?")[0]
    for ext, mime in {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".opus": "audio/opus",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".amr": "audio/amr",
        ".mp4": "video/mp4",
    }.items():
        if ref.endswith(ext):
            return mime
    return ""
