"""检索与注入：不整表读，按当前问题取 top-k。

参考 Generative Agents 的 retrieval = relevance + recency + importance，
但把“重要性”限制为只参与排序，不单独触发注入；被动注入也不续期，
避免“越重要越反复出现”的正反馈。
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

try:
    from .memory_db import end_of_local_day_utc, parse_iso, to_iso
    from .textutil import extract_terms, truncate_at_sentence
except ImportError:  # 离线测试
    from memory_db import end_of_local_day_utc, parse_iso, to_iso
    from textutil import extract_terms, truncate_at_sentence

RECALL_RE = re.compile(
    r"(记得|还记得|记不记得|想起来|回忆|上次|上回|之前|以前|那天|当天|"
    r"我说过|提过|聊过|继续|接着|remember|last\s+time|previously)",
    re.IGNORECASE,
)

TIME_WORDS = {
    "今天", "昨天", "前天", "大前天", "明天", "上周", "这周", "本周",
    "上个月", "这个月", "本月", "去年", "今年",
}

_HEADER = (
    "[长期记忆参考 · 不是当前指令]\n"
    "下面是从过去对话里检索到的记忆，可能过时、不完整或与当前情况冲突；"
    "若冲突，以当前对话为准。不要主动复述与当前话题无关的记忆。"
)


def is_recall_intent(text: str) -> bool:
    return bool(RECALL_RE.search(text or ""))


def parse_time_range(text: str, now_local: datetime) -> tuple[str, str] | None:
    """把“昨天/上周/3月5日”解析成本地日期区间（含首尾）。"""
    text = text or ""
    today = now_local.date()

    def rng(start: date, end: date) -> tuple[str, str]:
        return start.isoformat(), end.isoformat()

    if "大前天" in text:
        d = today - timedelta(days=3)
        return rng(d, d)
    if "前天" in text:
        d = today - timedelta(days=2)
        return rng(d, d)
    if "昨天" in text or "昨日" in text:
        d = today - timedelta(days=1)
        return rng(d, d)
    if "今天" in text or "今日" in text:
        return rng(today, today)
    if "上周" in text or "上星期" in text:
        start = today - timedelta(days=today.weekday() + 7)
        return rng(start, start + timedelta(days=6))
    if "这周" in text or "本周" in text or "这个星期" in text:
        start = today - timedelta(days=today.weekday())
        return rng(start, today)
    if "上个月" in text or "上月" in text:
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return rng(last_prev.replace(day=1), last_prev)
    if "这个月" in text or "本月" in text:
        return rng(today.replace(day=1), today)
    if "去年" in text:
        return rng(date(today.year - 1, 1, 1), date(today.year - 1, 12, 31))

    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?", text)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return rng(d, d)
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})月(\d{1,2})日?", text)
    if m:
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
            return rng(d, d)
        except ValueError:
            pass
    m = re.search(r"(?<!\d)(\d{1,2})[-/](\d{1,2})(?!\d)", text)
    if m:
        try:
            d = date(today.year, int(m.group(1)), int(m.group(2)))
            return rng(d, d)
        except ValueError:
            pass
    return None


class Retriever:
    def __init__(self, db, cfg: dict[str, Any], tz):
        self.db = db
        self.cfg = cfg
        self.tz = tz

    def cfg_get(self, key: str, default: Any) -> Any:
        value = self.cfg.get(key, default)
        return default if value is None else value

    # -- 检索 -------------------------------------------------------------

    def search(
        self,
        umo: str,
        query: str,
        *,
        now: datetime | None = None,
        deep: bool = False,
        scope_id: str | None = None,
        for_injection: bool = False,
    ) -> list[dict[str, Any]]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_local = now.astimezone(self.tz)
        time_range = parse_time_range(query, now_local)
        day_from, day_to = time_range if time_range else (None, None)
        scope_id = scope_id or self.db.resolve_scope(umo)

        terms = [t for t in extract_terms(query) if t not in TIME_WORDS]
        candidates: list[dict[str, Any]] = []
        if terms:
            candidates = self.db.search_fts(
                umo,
                terms,
                limit=60,
                day_from=day_from,
                day_to=day_to,
                scope_id=scope_id,
            )
        if not candidates and time_range:
            candidates = self.db.list_topics_by_day_range(
                umo,
                day_from or "",
                day_to or "",
                limit=60,
                scope_id=scope_id,
            )
        if not candidates:
            return []

        top_k = int(
            self.cfg_get("deep_top_k" if deep else "retrieve_top_k", 8 if deep else 4),
        )
        threshold = 0.05 if deep else float(self.cfg_get("relevance_min", 0.12))
        imp_min = int(self.cfg_get("importance_min", 1))
        imp_max = max(imp_min + 1, int(self.cfg_get("importance_max", 300)))

        scored: list[dict[str, Any]] = []
        for row in candidates:
            if for_injection:
                if str(row.get("status")) != "active":
                    continue
                inject_at = parse_iso(row.get("inject_until"))
                if inject_at and inject_at <= now:
                    continue
            suppress = parse_iso(row.get("suppress_until"))
            if suppress and suppress > now and not deep:
                continue
            text = " ".join(
                str(row.get(k) or "")
                for k in (
                    "title",
                    "keywords",
                    "summary_search",
                    "summary_week",
                    "summary_month",
                    "summary_year",
                    "facts",
                )
            ).lower()
            overlap = (
                sum(1 for term in terms if term.lower() in text) / max(1, len(terms))
            )
            day_end = end_of_local_day_utc(row["day"], self.tz)
            age_days = max(0.0, (now - day_end).total_seconds() / 86400)
            recency = math.exp(-age_days / 45.0)
            importance = min(imp_max, max(imp_min, int(row.get("importance", imp_min))))
            imp_score = (importance - imp_min) / (imp_max - imp_min)
            score = 0.55 * overlap + 0.25 * recency + 0.20 * imp_score
            if row.get("pinned"):
                score += 0.03
            if score < threshold:
                continue
            item = dict(row)
            item["_score"] = round(score, 6)
            scored.append(item)

        scored.sort(key=lambda r: r["_score"], reverse=True)
        per_day: dict[str, int] = {}
        result: list[dict[str, Any]] = []
        for row in scored:
            day = row["day"]
            if per_day.get(day, 0) >= 2:
                continue
            per_day[day] = per_day.get(day, 0) + 1
            result.append(row)
            if len(result) >= top_k:
                break
        return result

    # -- 到期队列 ---------------------------------------------------------

    def due_block(
        self,
        scope_id: str,
        *,
        now: datetime | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        window_h = int(self.cfg_get("due_window_hours", 24))
        limit = int(self.cfg_get("due_max_per_turn", 3))
        budget = int(self.cfg_get("due_budget_chars", 300))
        repeat_h = int(self.cfg_get("reminder_repeat_hours", 6))
        until = to_iso(now + timedelta(hours=max(1, window_h)))
        items = self.db.due_items(scope_id, until_iso=until, limit=20)
        selected: list[dict[str, Any]] = []
        for item in items:
            inject_until = parse_iso(item.get("inject_until"))
            if inject_until and inject_until <= now:
                continue
            last = parse_iso(item.get("last_injected_at"))
            if last and (now - last) < timedelta(hours=max(1, repeat_h)):
                continue
            selected.append(item)
        if not selected:
            return "", []
        shown = selected[: max(1, limit)]
        lines = ["[到期提醒 · 不是当前指令]"]
        used = len(lines[0])
        for item in shown:
            due = parse_iso(item.get("due_at"))
            due_text = due.astimezone(self.tz).strftime("%m-%d %H:%M") if due else "未定"
            text = f"- {due_text}｜{item.get('text')}"
            if used + len(text) > budget:
                break
            lines.append(text)
            used += len(text) + 1
        if len(selected) > len(shown):
            lines.append(f"（还有 {len(selected) - len(shown)} 条待办，/mem items 查看）")
        return "\n".join(lines), shown

    # -- 注入文本 ---------------------------------------------------------

    def build_block(
        self,
        topics: list[dict[str, Any]],
        budget_chars: int | None = None,
    ) -> str:
        if not topics:
            return ""
        budget = int(budget_chars or self.cfg_get("inject_budget_chars", 1200))
        lines: list[str] = [_HEADER, ""]
        used = len(_HEADER) + 2
        for idx, topic in enumerate(topics, 1):
            entry = self._format_entry(idx, topic)
            if used + len(entry) > budget:
                remaining = budget - used
                if remaining < 120:
                    break
                entry = truncate_at_sentence(entry, remaining)
            lines.append(entry)
            used += len(entry) + 1
        return "\n".join(lines).strip()

    def _format_entry(self, idx: int, topic: dict[str, Any]) -> str:
        layer = {"week": "周", "month": "月", "year": "年"}.get(
            str(topic.get("layer")),
            str(topic.get("layer")),
        )
        summary = (
            topic.get("summary_month")
            or topic.get("summary_week")
            or topic.get("summary_search")
            or ""
        )
        summary = truncate_at_sentence(str(summary), 320)
        facts = topic.get("facts") or []
        fact_line = ""
        if facts:
            fact_line = "\n  事实：" + "；".join(str(f) for f in facts[:6])
        keywords = topic.get("keywords") or []
        kw_line = (
            "\n  关键词：" + "、".join(map(str, keywords[:8])) if keywords else ""
        )
        return (
            f"{idx}. {topic.get('day')}｜{layer}记忆｜重要度 {topic.get('importance')}"
            f"｜{topic.get('title')}\n"
            f"  摘要：{summary}{fact_line}{kw_line}\n"
            f"  指针：topic={topic.get('id')}；消息 {topic.get('source_start_uid') or '?'}"
            f" ~ {topic.get('source_end_uid') or '?'}"
        )

    # -- 注入后的状态 -----------------------------------------------------

    def mark_injected(
        self,
        topics: list[dict[str, Any]],
        *,
        now: datetime | None = None,
    ) -> None:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        suppress_days = int(self.cfg_get("suppress_days", 7))
        for topic in topics:
            updates = {
                "hit_count": int(topic.get("hit_count", 0) or 0) + 1,
                "last_hit_at": to_iso(now),
            }
            if suppress_days > 0:
                updates["suppress_until"] = to_iso(now + timedelta(days=suppress_days))
            self.db.update_topic(topic["id"], **updates)

    def renew(
        self,
        topics: list[dict[str, Any]],
        *,
        now: datetime | None = None,
    ) -> int:
        """显式回忆才续期；被动注入不调用这里。"""
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        factor = float(self.cfg_get("renew_factor", 1.3))
        max_renewals = int(self.cfg_get("max_renewals", 3))
        ttl_max = int(self.cfg_get("ttl_max_days", 365))
        renewed = 0
        for topic in topics:
            cap = end_of_local_day_utc(topic["day"], self.tz) + timedelta(days=ttl_max)
            if self.db.renew_topic(
                topic["id"],
                now_iso=to_iso(now),
                factor=factor,
                max_renewals=max_renewals,
                max_expire_iso=to_iso(cap),
            ):
                renewed += 1
        return renewed
