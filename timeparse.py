"""纯 Python 的日期/时间解析，不依赖 AstrBot。

从 main.py 抽出，方便脱离 AstrBot 复用和单独测试。
"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, time as dtime, timedelta, timezone


def _parse_cn_number(value: str) -> int | None:
    value = (value or "").strip()
    if value.isdigit():
        return int(value)
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    value = value.lstrip("零") or "零"
    if value in digits:
        return digits[value]
    if "十" in value:
        left, _, right = value.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def _minute_from_text(text: str) -> int:
    text = text or ""
    m = re.search(r"[点时]\s*(\d{1,2})\s*分?", text)
    if m:
        value = int(m.group(1))
        return value if 0 <= value <= 59 else 0
    m = re.search(r"[点时]\s*([零一二两三四五六七八九十]{1,3})\s*分?", text)
    if m:
        value = _parse_cn_number(m.group(1))
        return value if value is not None and 0 <= value <= 59 else 0
    if "三刻" in text:
        return 45
    if "一刻" in text:
        return 15
    if "半" in text:
        return 30
    return 0


def _hour_from_text(text: str) -> int | None:
    m = re.search(r"(?<!\d)(\d{1,2})\s*[:点时]", text or "")
    if m:
        hour = int(m.group(1))
    else:
        m = re.search(r"([一二两三四五六七八九十]{1,3})\s*[点时]", text or "")
        if not m:
            return None
        hour = _parse_cn_number(m.group(1))
    if hour is None or not (0 <= hour <= 23):
        return None
    return hour


def _weekday_date(text: str, now_local: datetime):
    mapping = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    m = re.search(r"(?:周|星期|礼拜)([一二三四五六日天])", text or "")
    if not m:
        return None
    target = mapping.get(m.group(1))
    if target is None:
        return None
    days = (target - now_local.weekday()) % 7
    return now_local.date() + timedelta(days=days), target


def parse_relative_due(text: str, now: datetime, tz) -> datetime | None:
    """规则兜底：解析“明天凌晨四点”“周五晚上六点”这类中文时间。"""
    now_local = now.astimezone(tz)
    m = re.search(
        r"(今天|明天|后天|大后天)?\s*"
        r"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)?\s*"
        r"(\d{1,2}|[一二两三四五六七八九十]{1,3})\s*[点时]"
        r"(?:\s*(\d{1,2})\s*分?)?",
        text or "",
    )
    if not m:
        return None
    day_word, period, hour_text, minute_text = m.groups()
    hour = _parse_cn_number(hour_text)
    if hour is None:
        return None
    minute = _minute_from_text(text)
    if period in {"下午", "傍晚", "晚上", "夜里"} and hour < 12:
        hour += 12
    if period == "中午" and hour < 11:
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    day = now_local.date()
    weekday = _weekday_date(text, now_local)
    if weekday:
        day = weekday[0]
    elif day_word == "明天":
        day = day + timedelta(days=1)
    elif day_word == "后天":
        day = day + timedelta(days=2)
    elif day_word == "大后天":
        day = day + timedelta(days=3)
    elif day_word == "今天":
        day = day
    due = datetime.combine(day, dtime(hour, minute), tzinfo=tz)
    if due <= now_local:
        if weekday:
            due += timedelta(days=7)
        elif day_word in {"今天", None}:
            # 没写日期时，“四点”通常指下一个四点；写了今天则已经过去。
            if day_word is None:
                due += timedelta(days=1)
            else:
                return None
        else:
            return None
    return due.astimezone(timezone.utc)


def time_text_grounded(user_text: str, time_text: str, history_text: str = "") -> bool:
    """time_text 必须能在用户原话/最近对话里找到；允许补日期前缀。"""
    user = re.sub(r"\s+", "", user_text or "")
    history = re.sub(r"\s+", "", history_text or "")
    claim = re.sub(r"\s+", "", time_text or "")
    if not claim:
        return False
    if claim in user or claim in history:
        return True
    for prefix in ("大后天", "后天", "明天", "今天"):
        rest = claim[len(prefix) :] if claim.startswith(prefix) else ""
        if rest and (rest in user or rest in history):
            return True
    period = re.search(r"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)", claim)
    if period:
        rest = claim.replace(period.group(1), "", 1)
        has_period = period.group(1) in user or period.group(1) in history
        if has_period and rest and (rest in user or rest in history):
            return True
    return False


def _extract_date_texts(text: str) -> list[str]:
    text = text or ""
    patterns = (
        r"\d{4}年\d{1,2}月\d{1,2}[日号]?",
        r"(?:\d{1,2}|[一二三四五六七八九十]{1,3})月"
        r"(?:\d{1,2}|[一二三四五六七八九十]{1,3})[日号]?",
        r"(?:下下|下|这|本)?(?:周|星期|礼拜)[一二三四五六日天]",
        r"今天|明天|后天|大后天",
        r"(?:\d{1,2}|[一二三四五六七八九十]{1,3})[日号]",
    )
    found: list[str] = []
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            value = m.group(0)
            if value not in found:
                found.append(value)
    return found


def _extract_date_text(text: str) -> str:
    found = _extract_date_texts(text)
    return found[0] if found else ""


def _extract_clock_text(text: str) -> str:
    text = text or ""
    m = re.search(r"(?<!\d)\d{1,2}:\d{2}(?!\d)", text)
    if m:
        return m.group(0)
    m = re.search(
        r"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)?\s*"
        r"(?:\d{1,2}|[一二两三四五六七八九十]{1,3})\s*[点时]"
        r"(?:\s*(?:\d{1,2}|[零一二两三四五六七八九十]{1,3}|半|一刻|三刻)\s*分?)?",
        text,
    )
    if m:
        return m.group(0)
    m = re.search(r"凌晨|早上|上午|中午|正午|下午|傍晚|晚上|夜里", text)
    return m.group(0) if m else ""


def _date_from_text(date_text: str, now_local: datetime):
    text = re.sub(r"\s+", "", date_text or "")
    if not text:
        return now_local.date()
    m = re.fullmatch(
        r"(?:(\d{4})年)?"
        r"(\d{1,2}|[一二三四五六七八九十]{1,3})月"
        r"(\d{1,2}|[一二三四五六七八九十]{1,3})[日号]?",
        text,
    )
    if m:
        year = int(m.group(1)) if m.group(1) else now_local.year
        month = _parse_cn_number(m.group(2))
        day = _parse_cn_number(m.group(3))
        if month is None or day is None:
            return None
        try:
            candidate = now_local.date().replace(year=year, month=month, day=day)
        except ValueError:
            return None
        if not m.group(1) and candidate < now_local.date():
            candidate = candidate.replace(year=year + 1)
        return candidate
    m = re.fullmatch(r"(\d{1,2}|[一二三四五六七八九十]{1,3})[日号]", text)
    if m:
        day = _parse_cn_number(m.group(1))
        if day is None:
            return None
        year, month = now_local.year, now_local.month
        try:
            candidate = now_local.date().replace(year=year, month=month, day=day)
        except ValueError:
            return None
        if candidate < now_local.date():
            month += 1
            if month > 12:
                month, year = 1, year + 1
            try:
                candidate = now_local.date().replace(year=year, month=month, day=day)
            except ValueError:
                return None
        return candidate
    m = re.fullmatch(r"(下下|下|这|本)?(?:周|星期|礼拜)([一二三四五六日天])", text)
    if m:
        mapping = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
        target = mapping[m.group(2)]
        weeks = {"下下": 2, "下": 1, "这": 0, "本": 0, None: 0}[m.group(1)]
        monday = now_local.date() - timedelta(days=now_local.weekday())
        candidate = monday + timedelta(days=weeks * 7 + target)
        if not m.group(1) and candidate < now_local.date():
            candidate += timedelta(days=7)
        return candidate
    day_words = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}
    if text in day_words:
        return now_local.date() + timedelta(days=day_words[text])
    return None


def _clock_from_text(clock_text: str):
    text = re.sub(r"\s+", "", clock_text or "")
    if not text:
        return None
    if text in {
        "凌晨", "早上", "上午", "中午", "正午", "下午", "傍晚", "晚上", "夜里",
    }:
        # 只给时段、不给点数，视为不准确时间，必须追问。
        return "ambiguous"
    period_match = re.search(r"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)", text)
    period = period_match.group(1) if period_match else ""
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(
            r"(\d{1,2}|[一二两三四五六七八九十]{1,3})[点时]",
            text,
        )
        if not m:
            return None
        hour = _parse_cn_number(m.group(1))
        minute = _minute_from_text(text)
    if hour is None or not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    if 1 <= hour <= 12 and not period:
        return "ambiguous"
    if period in {"下午", "傍晚", "晚上", "夜里"} and hour < 12:
        hour += 12
    if period == "中午" and hour < 11:
        hour += 12
    if period == "凌晨" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23):
        return None
    return hour, minute


def _add_month(day, months: int):
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return day.replace(year=year, month=month, day=min(day.day, last))


def parse_structured_time(
    date_text: str,
    clock_text: str,
    now: datetime,
    tz,
) -> tuple[datetime | None, bool]:
    """日期、时间分开解析；返回 (due, 是否歧义)。"""
    now_local = now.astimezone(tz)
    date = _date_from_text(date_text, now_local)
    if date is None:
        return None, False
    clock = _clock_from_text(clock_text)
    if clock == "ambiguous":
        return None, True
    if not clock:
        return None, False
    hour, minute = clock
    due = datetime.combine(date, dtime(hour, minute), tzinfo=tz)
    if due <= now_local:
        if not (date_text or "").strip():
            due += timedelta(days=1)
        elif re.fullmatch(r"(?:周|星期|礼拜)[一二三四五六日天]", date_text.strip()):
            due += timedelta(days=7)
        elif re.fullmatch(r"\d{1,2}[日号]", date_text.strip()):
            due = _add_month(due, 1)
        else:
            return None, False
    return due.astimezone(timezone.utc), False


def parse_time_spec(text: str, now: datetime, tz) -> tuple[datetime | None, bool]:
    """解析自然语言时间，返回 (due, 是否歧义)。"""
    text = (text or "").strip()
    now_local = now.astimezone(tz)
    period = bool(re.search(r"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)", text))
    try:
        iso = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if iso.tzinfo is None:
            iso = iso.replace(tzinfo=tz)
        local_iso = iso.astimezone(tz)
        # ISO 里的 10:00 仍可能是上午/晚上；除非用户写了时段。
        if 1 <= local_iso.hour <= 12 and not period:
            return None, True
        return iso.astimezone(timezone.utc), False
    except ValueError:
        pass

    date_text = _extract_date_text(text)
    clock_text = _extract_clock_text(text)
    if not clock_text:
        return None, False
    return parse_structured_time(date_text, clock_text, now, tz)
