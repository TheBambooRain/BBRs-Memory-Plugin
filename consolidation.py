"""每日提炼、7/30 天迁移、过期。

对应原始方案的 1.1–1.5，但做了三处修正：
1. 迁移时从原始消息重做摘要，不拿上一层摘要再压；
2. facts（关键事实）单独保存，不参与压缩；
3. 低重要度条目不会“整段陪葬”，过期按条目而不是按话题块。

参考：
- MemoryOS 的短/中/长分层；
- MemoryBank 的遗忘曲线（这里离散成 ttl_days）；
- MemGPT/Letta 的“后台巩固”思路。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

try:
    from .media import render_message_text
    from .memory_db import (
        clamp,
        compute_expire_at,
        compute_ttl_days,
        to_iso,
        utc_now,
    )
    from .textutil import truncate_at_sentence
except ImportError:  # 离线测试
    from media import render_message_text
    from memory_db import (
        clamp,
        compute_expire_at,
        compute_ttl_days,
        to_iso,
        utc_now,
    )
    from textutil import truncate_at_sentence

PROMPT_VERSION = "v0.1.0"
logger = logging.getLogger("astrbot.plugin.memory_v01")

LLMCall = Callable[[str, str], Awaitable[tuple[str, str]]]


TOPIC_PROMPT = """你是一个长期记忆整理器。下面是一天中某个会话的对话记录，每行开头是该条消息的编号。

请按“话题/事件”切分，输出一个 JSON 数组，不要输出任何解释、Markdown 代码块或前后缀。
数组每个元素包含：
- title: 话题标题，12 个字以内
- keywords: 3 到 8 个关键词，字符串数组
- summary_search: 用于检索的摘要，尽量精炼，不超过该话题原文的 30%
- summary_week: 约 70% 长度的周摘要，必须保留人物、数字、日期、决定、承诺、未完成事项
- facts: 0 到 8 条自包含的事实，每条一句话；不要压缩、不要改写数字和人名
- importance: 1 到 300 的整数，按“以后还会不会再用到”评分，不要都给高分
- items: 原子记忆数组，每个元素包含：
  - type: reminder / task / event / preference / fact / episode
  - text: 一条自包含的话，保留人名、数字、日期、地点
  - due_at: 仅 reminder 填写，ISO 8601 带时区；没有明确时间就填 null
  - importance: 1 到 300
  - keywords: 字符串数组
- start_uid: 该话题第一条消息的编号（必须来自记录）
- end_uid: 该话题最后一条消息的编号（必须来自记录）

要求：
1. 提醒、待办、承诺必须单独成 item，不能并进闲聊 item。
2. 时间敏感的提醒（例如“明天十点办卡”）必须单独拆成一个 topic 或至少一个 reminder item；
   不要和“熬夜写插件”这类 episode 合并成一个 item。
3. 一个话题尽量只覆盖一个连续事件；不要按说话人切分。
4. 寒暄、复读、无信息量的内容可以合并或省略，但不要丢掉明确的决定和承诺。
5. 如果记录里几乎没有有效信息，输出空数组 []。
6. 只输出 JSON。

日期：{day}
会话：{umo}
对话记录：
{transcript}
"""

COMPRESS_PROMPT = """你在整理长期记忆。下面是一段 {age_days} 天前的原始对话，以及它当前的摘要。

请生成更短的{layer_name}摘要，目标约 {target_chars} 个汉字：
- 保留人物、关系、数字、日期、决定、承诺、未完成事项、偏好；
- 不要新增原文里没有的事实；
- 不要写成流水账，按“这件事是什么、结论是什么、还欠什么”组织；
- 只输出 JSON：{{"summary": "...", "keywords": ["..."]}}

当前摘要：
{current}

原始对话：
{transcript}
"""


def _strip_code_fence(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_topics_json(text: str) -> list[dict[str, Any]]:
    """尽量宽容地解析模型输出；解析失败抛 ValueError。"""
    raw = _strip_code_fence(text)
    if not raw:
        return []
    data: Any = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("[")
        end = raw.rfind("]")
        if start >= 0 and end > start:
            data = json.loads(raw[start : end + 1])
        else:
            start = raw.find("{")
            end = raw.rfind("}")
            if start >= 0 and end > start:
                data = json.loads(raw[start : end + 1])
    if isinstance(data, dict):
        data = data.get("topics") or data.get("items") or []
    if not isinstance(data, list):
        raise ValueError("模型输出不是 JSON 数组")

    topics: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:60]
        keywords = item.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [
                k.strip() for k in re.split(r"[,，、;；\s]+", keywords) if k.strip()
            ]
        facts = item.get("facts") or []
        if isinstance(facts, str):
            facts = [facts]
        summary_search = str(item.get("summary_search") or "").strip()
        summary_week = str(item.get("summary_week") or "").strip()
        if not summary_week:
            summary_week = summary_search
        if not summary_search:
            summary_search = truncate_at_sentence(
                summary_week,
                max(60, len(summary_week) // 3),
            )
        if not title and summary_week:
            title = truncate_at_sentence(summary_week, 20)
        if not title:
            continue
        try:
            importance = int(item.get("importance", 100))
        except (TypeError, ValueError):
            importance = 100
        raw_items = item.get("items") or []
        parsed_items: list[dict[str, Any]] = []
        if isinstance(raw_items, list):
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    continue
                text_value = str(raw_item.get("text") or "").strip()
                if not text_value:
                    continue
                type_value = str(raw_item.get("type") or "episode").strip().lower()
                if type_value not in {
                    "reminder", "task", "event", "preference", "fact", "episode",
                }:
                    type_value = "episode"
                try:
                    item_importance = int(raw_item.get("importance", importance))
                except (TypeError, ValueError):
                    item_importance = importance
                item_keywords = raw_item.get("keywords") or []
                if isinstance(item_keywords, str):
                    item_keywords = [
                        k.strip()
                        for k in re.split(r"[,，、;；\s]+", item_keywords)
                        if k.strip()
                    ]
                parsed_items.append(
                    {
                        "type": type_value,
                        "text": text_value[:1000],
                        "due_at": str(raw_item.get("due_at") or "").strip(),
                        "importance": item_importance,
                        "keywords": [str(k)[:40] for k in item_keywords][:12],
                    },
                )
        topics.append(
            {
                "title": title,
                "keywords": [str(k)[:40] for k in keywords][:12],
                "summary_search": summary_search,
                "summary_week": summary_week,
                "facts": [str(f)[:300] for f in facts][:10],
                "importance": importance,
                "items": parsed_items,
                "start_uid": str(item.get("start_uid") or "").strip(),
                "end_uid": str(item.get("end_uid") or "").strip(),
            },
        )
    return topics


def parse_summary_json(text: str) -> tuple[str, list[str]]:
    raw = _strip_code_fence(text)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            summary = str(data.get("summary") or "").strip()
            keywords = data.get("keywords") or []
            if isinstance(keywords, str):
                keywords = [
                    k.strip() for k in re.split(r"[,，、;；\s]+", keywords) if k.strip()
                ]
            if summary:
                return summary, [str(k)[:40] for k in keywords][:12]
    except json.JSONDecodeError:
        pass
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    return " ".join(lines)[:4000], []


def parse_due_at(value: Any, tz) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none"}:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def item_lifetimes(
    item_type: str,
    due: datetime | None,
    now: datetime,
    ttl_days: int,
) -> tuple[str, str | None]:
    """返回 (inject_until, search_until)；search_until=None 表示永久可检索。"""
    if item_type == "reminder" and due is not None:
        inject = due + timedelta(days=1)
    elif item_type in {"reminder", "task"}:
        inject = now + timedelta(days=14)
    elif item_type == "event":
        inject = now + timedelta(days=30)
    elif item_type == "preference":
        inject = now + timedelta(days=180)
    elif item_type == "fact":
        inject = now + timedelta(days=365)
    else:
        inject = now + timedelta(days=max(7, int(ttl_days or 30)))
    return to_iso(inject), None


class Consolidator:
    def __init__(
        self,
        db,
        cfg: dict[str, Any],
        llm_json: LLMCall,
        tz,
        *,
        now_fn: Callable[[], datetime] | None = None,
        external_reminders: Callable[[str], bool] | None = None,
    ):
        self.db = db
        self.cfg = cfg
        self.llm_json = llm_json
        self.tz = tz
        self.now_fn = now_fn or utc_now
        self.external_reminders = external_reminders or (lambda _umo: False)
        self._lock = asyncio.Lock()

    def cfg_get(self, key: str, default: Any) -> Any:
        value = self.cfg.get(key, default)
        return default if value is None else value

    # -- 入口 -------------------------------------------------------------

    async def rebuild_day(self, day: str, umo: str) -> dict[str, Any]:
        deleted = self.db.delete_day(day, umo)
        stats = await self.run(day=day, umo=umo)
        stats["deleted"] = deleted
        return stats

    async def run(
        self,
        *,
        day: str | None = None,
        umo: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            now = (now or self.now_fn()).astimezone(timezone.utc)
            today = now.astimezone(self.tz).date().isoformat()
            stats: dict[str, Any] = {
                "days": 0,
                "topics": 0,
                "promoted": 0,
                "expired": 0,
                "purged": 0,
                "skipped": 0,
                "errors": [],
            }

            if day:
                pairs = [(day, umo)] if umo else self.db.pairs_for_day(day)
            else:
                pairs = self.db.pending_pairs(today, limit=50)
            for pair_day, pair_umo in pairs:
                if not pair_umo:
                    continue
                if self.cfg_get("wait_for_media", False) and self.db.has_pending_attachments(
                    pair_day,
                    pair_umo,
                ):
                    stats["skipped"] += 1
                    continue
                self.db.job_start(pair_day, pair_umo, "daily")
                try:
                    after_id = self.db.job_last_message_id(pair_day, pair_umo, "daily")
                    created = await self._process_day(
                        pair_day, pair_umo, after_id=after_id,
                    )
                    self.db.job_done(
                        pair_day,
                        pair_umo,
                        "daily",
                        self.db.max_message_id(pair_day, pair_umo),
                    )
                    stats["days"] += 1
                    stats["topics"] += created
                except Exception as exc:  # noqa: BLE001
                    logger.exception("memory: 提炼失败 %s %s", pair_day, pair_umo)
                    self.db.job_fail(pair_day, pair_umo, "daily", str(exc))
                    stats["errors"].append(f"{pair_day}/{pair_umo}: {exc}")

            stats["promoted"] += await self._promote(
                "week",
                "month",
                int(self.cfg_get("promote_week_days", 7)),
                now,
            )
            stats["promoted"] += await self._promote(
                "month",
                "year",
                int(self.cfg_get("promote_month_days", 30)),
                now,
            )
            stats["expired"] = self.db.expire_topics(to_iso(now))
            retention = int(self.cfg_get("expired_retention_days", 30))
            if retention >= 0:
                stats["purged"] = self.db.purge_expired(
                    to_iso(now - timedelta(days=retention)),
                )
            return stats

    # -- 每日提炼 ---------------------------------------------------------

    async def _process_day(self, day: str, umo: str, *, after_id: int = 0) -> int:
        messages = self.db.get_messages_for_day(day, umo, after_id=after_id)
        if not messages:
            return 0
        scope_id = next(
            (str(msg.get("scope_id")) for msg in messages if msg.get("scope_id")),
            "",
        )
        if not scope_id:
            platform = next(
                (str(msg.get("platform")) for msg in messages if msg.get("platform")),
                None,
            )
            scope_id = self.db.resolve_scope(umo, platform=platform)
        now = self.now_fn().astimezone(timezone.utc)
        external_reminders = bool(self.external_reminders(umo))
        attachments: dict[str, list[dict[str, Any]]] = {}
        for msg in messages:
            attachments[msg["msg_uid"]] = self.db.get_attachments(msg["msg_uid"])

        chunk_chars = int(self.cfg_get("chunk_chars", 12000))
        summary_max = int(self.cfg_get("summary_max_chars", 1200))
        imp_min = int(self.cfg_get("importance_min", 1))
        imp_max = int(self.cfg_get("importance_max", 300))
        unit = float(self.cfg_get("ttl_unit_days", 1.0))
        ttl_min = int(self.cfg_get("ttl_min_days", 2))
        ttl_max = int(self.cfg_get("ttl_max_days", 365))

        created = 0
        for chunk in _chunk_messages(messages, attachments, chunk_chars):
            transcript = "\n".join(chunk)
            prompt = TOPIC_PROMPT.format(day=day, umo=umo, transcript=transcript)
            text, model = await self._call_with_retry(prompt, umo)
            topics = parse_topics_json(text)
            for topic in topics:
                importance = clamp(topic["importance"], imp_min, imp_max)
                ttl_days = compute_ttl_days(importance, unit, ttl_min, ttl_max)
                start_uid = topic.get("start_uid") or ""
                end_uid = topic.get("end_uid") or ""
                if not start_uid or not end_uid:
                    first = messages[0]["msg_uid"]
                    last = messages[-1]["msg_uid"]
                    start_uid = start_uid or first
                    end_uid = end_uid or last

                raw_items = list(topic.get("items") or [])
                if not raw_items:
                    raw_items.append(
                        {
                            "type": "episode",
                            "text": topic["summary_week"] or topic["summary_search"],
                            "due_at": "",
                            "importance": importance,
                            "keywords": topic["keywords"],
                        },
                    )
                # facts 单独保留，不参与摘要压缩。
                for fact in topic.get("facts") or []:
                    if not any(str(it.get("text", "")).strip() == fact for it in raw_items):
                        raw_items.append(
                            {
                                "type": "fact",
                                "text": fact,
                                "due_at": "",
                                "importance": importance,
                                "keywords": [],
                            },
                        )

                computed_items: list[dict[str, Any]] = []
                for raw_item in raw_items:
                    item_type = str(raw_item.get("type") or "episode").lower()
                    due = parse_due_at(raw_item.get("due_at"), self.tz)
                    if item_type == "reminder" and due is None:
                        item_type = "task"
                    if external_reminders and item_type in {"reminder", "task"}:
                        # future_task 负责待办；记忆层只留可检索的事件记录。
                        item_type = "event"
                    inject_until, search_until = item_lifetimes(
                        item_type, due, now, ttl_days,
                    )
                    computed_items.append(
                        {
                            "type": item_type,
                            "text": str(raw_item.get("text") or "").strip(),
                            "keywords": raw_item.get("keywords") or [],
                            "importance": clamp(
                                int(raw_item.get("importance", importance) or importance),
                                imp_min,
                                imp_max,
                            ),
                            "due_at": to_iso(due) if due else None,
                            "inject_until": inject_until,
                            "search_until": search_until,
                        },
                    )
                topic_inject = max(
                    (it["inject_until"] for it in computed_items),
                    default=compute_expire_at(day, ttl_days, self.tz),
                )

                topic_id = self.db.insert_topic(
                    {
                        "id": uuid.uuid4().hex,
                        "umo": umo,
                        "scope_id": scope_id,
                        "day": day,
                        "title": topic["title"],
                        "keywords": topic["keywords"],
                        "summary_search": truncate_at_sentence(
                            topic["summary_search"],
                            max(60, summary_max // 3),
                        ),
                        "summary_week": truncate_at_sentence(
                            topic["summary_week"],
                            summary_max,
                        ),
                        "facts": topic["facts"],
                        "importance": importance,
                        "layer": "week",
                        "ttl_days": ttl_days,
                        "source_start_uid": start_uid,
                        "source_end_uid": end_uid,
                        "source_start_id": self._uid_to_id(start_uid),
                        "source_end_id": self._uid_to_id(end_uid),
                        "expire_at": compute_expire_at(day, ttl_days, self.tz),
                        "inject_until": topic_inject,
                        "search_until": None,
                        "summary_model": model,
                        "prompt_version": PROMPT_VERSION,
                    },
                )
                if not topic_id:
                    continue
                created += 1
                for item in computed_items:
                    if not item["text"]:
                        continue
                    item_id = self.db.insert_item(
                        {
                            "scope_id": scope_id,
                            "topic_id": topic_id,
                            "type": item["type"],
                            "text": item["text"],
                            "keywords": item["keywords"],
                            "importance": item["importance"],
                            "due_at": item["due_at"],
                            "status": (
                                "pending"
                                if item["type"] in {"reminder", "task"}
                                else "active"
                            ),
                            "inject_until": item["inject_until"],
                            "search_until": item["search_until"],
                            "source_start_uid": start_uid,
                            "source_end_uid": end_uid,
                        },
                    )
                    if (
                        item["type"] == "reminder"
                        and item["due_at"]
                        and not external_reminders
                    ):
                        self.db.enqueue_outbox(
                            item_id=item_id,
                            action="upsert",
                            payload={
                                "item_id": item_id,
                                "topic_id": topic_id,
                                "scope_id": scope_id,
                                "umo": umo,
                                "text": item["text"],
                                "due_at": item["due_at"],
                                "timezone": str(self.tz),
                                "dedupe_key": f"{scope_id}|{item['due_at']}|{item['text'][:60]}",
                            },
                        )
        return created

    async def _call_with_retry(self, prompt: str, umo: str) -> tuple[str, str]:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                text, model = await self.llm_json(
                    prompt if attempt == 0 else prompt + "\n\n只输出 JSON，不要解释。",
                    umo,
                )
                if text and text.strip():
                    return text, model
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("memory: LLM 调用失败，重试一次: %s", exc)
        raise RuntimeError(f"LLM 多次调用失败: {last_error}")

    # -- 分层迁移 ---------------------------------------------------------

    async def _promote(
        self,
        from_layer: str,
        to_layer: str,
        age_days: int,
        now: datetime,
    ) -> int:
        today = now.astimezone(self.tz).date()
        cutoff = (today - timedelta(days=age_days)).isoformat()
        limit = int(self.cfg_get("max_promotions_per_run", 20))
        rows = self.db.topics_for_promotion(from_layer, cutoff, to_iso(now), limit)
        promoted = 0
        for topic in rows:
            try:
                if await self._compress_topic(topic, to_layer, age_days):
                    promoted += 1
            except Exception:  # noqa: BLE001
                logger.exception("memory: 迁移失败 %s", topic.get("id"))
        return promoted

    async def _compress_topic(
        self,
        topic: dict[str, Any],
        to_layer: str,
        age_days: int,
    ) -> bool:
        messages = self.db.get_messages_between_uids(
            topic["umo"],
            topic.get("source_start_uid", ""),
            topic.get("source_end_uid", ""),
        )
        if not messages:
            logger.warning("memory: 找不到原文，跳过迁移 %s", topic.get("id"))
            return False
        current = (
            topic.get("summary_week")
            if to_layer == "month"
            else topic.get("summary_month") or topic.get("summary_week")
        )
        current = str(current or "")
        target_chars = max(80, int(len(current) * 0.7))
        target_chars = min(target_chars, int(self.cfg_get("summary_max_chars", 1200)))
        transcript = _render_transcript(messages, self.db)
        transcript = _clip_middle(transcript, int(self.cfg_get("chunk_chars", 12000)))
        prompt = COMPRESS_PROMPT.format(
            age_days=age_days,
            layer_name="月" if to_layer == "month" else "年",
            target_chars=target_chars,
            current=current[:3000],
            transcript=transcript,
        )
        text, model = await self._call_with_retry(prompt, topic["umo"])
        summary, keywords = parse_summary_json(text)
        summary = truncate_at_sentence(summary, target_chars + 100)
        if not summary:
            return False
        fields: dict[str, Any] = {
            "layer": to_layer,
            "summary_model": model,
            "prompt_version": PROMPT_VERSION,
        }
        if to_layer == "month":
            fields["summary_month"] = summary
        else:
            fields["summary_year"] = summary
        if keywords:
            fields["keywords"] = keywords
        self.db.update_topic(topic["id"], **fields)
        return True

    # -- 小工具 -----------------------------------------------------------

    def _uid_to_id(self, msg_uid: str) -> int | None:
        row = self.db.get_message_by_uid(msg_uid)
        return int(row["id"]) if row else None


# ---------------------------------------------------------------------------
# 模块级工具（方便测试）
# ---------------------------------------------------------------------------


def _chunk_messages(
    messages: list[dict[str, Any]],
    attachments: dict[str, list[dict[str, Any]]],
    chunk_chars: int,
) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for msg in messages:
        text = render_message_text(
            str(msg.get("content") or ""),
            attachments.get(msg["msg_uid"], []),
        ).strip()
        if not text:
            continue
        who = msg.get("sender_name") or (
            "bot" if msg.get("role") == "assistant" else msg.get("role")
        )
        line = f"[{msg['msg_uid']}] {who}: {text}"
        if len(line) > chunk_chars:
            line = line[:chunk_chars] + "…"
        if current and size + len(line) + 1 > chunk_chars:
            chunks.append(current)
            current = []
            size = 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append(current)
    return chunks


def _render_transcript(messages: list[dict[str, Any]], db) -> str:
    lines: list[str] = []
    for msg in messages:
        atts = db.get_attachments(msg["msg_uid"])
        text = render_message_text(str(msg.get("content") or ""), atts)
        who = msg.get("sender_name") or (
            "bot" if msg.get("role") == "assistant" else msg.get("role")
        )
        lines.append(f"[{msg['msg_uid']}] {who}: {text}")
    return "\n".join(lines)


def _clip_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.65)
    tail = limit - head
    return text[:head] + "\n…（中间省略）…\n" + text[-tail:]
