"""用最小桩验证 main.py 的 AstrBot 接口导入/装饰器形状。

不依赖真实 AstrBot，只保证：模块能导入、类能定义、组件映射不炸。
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))


def _decorator(*args, **kwargs):
    def wrap(fn):
        return fn

    return wrap


def _install_stubs() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")

    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    api.AstrBotConfig = dict
    astrbot.api = api

    event = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:  # noqa: N801
        pass

    event.AstrMessageEvent = AstrMessageEvent
    event.filter = types.SimpleNamespace(
        EventMessageType=types.SimpleNamespace(ALL="ALL"),
        event_message_type=_decorator,
        command=_decorator,
        on_llm_request=_decorator,
        on_llm_response=_decorator,
        on_llm_tool_respond=_decorator,
        llm_tool=_decorator,
    )
    sys.modules["astrbot.api.event"] = event

    star = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context

    class StarTools:
        @classmethod
        def get_data_dir(cls, name=None):
            return Path(__file__).parent

    star.Context = Context
    star.Star = Star
    star.StarTools = StarTools
    star.register = _decorator
    sys.modules["astrbot.api.star"] = star

    core = types.ModuleType("astrbot.core")
    provider = types.ModuleType("astrbot.core.provider")
    entities = types.ModuleType("astrbot.core.provider.entities")

    class ProviderRequest:
        def __init__(self):
            self.system_prompt = ""

    entities.ProviderRequest = ProviderRequest
    sys.modules["astrbot.core"] = core
    sys.modules["astrbot.core.provider"] = provider
    sys.modules["astrbot.core.provider.entities"] = entities
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api


class MainImportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _install_stubs()
        import main  # noqa: F401

        cls.main = main

    def test_component_kind(self) -> None:
        class Record:
            pass

        class Image:
            sub_type = "sticker"

        class File:
            pass

        self.assertEqual(self.main._component_kind("record", Record()), "voice")
        self.assertEqual(self.main._component_kind("image", Image()), "sticker")
        self.assertEqual(self.main._component_kind("file", File()), "file")
        self.assertEqual(self.main._component_kind("plain", object()), "")

    def test_help_text(self) -> None:
        self.assertIn("/mem export", self.main.HELP_TEXT)
        self.assertIn("/mem import", self.main.HELP_TEXT)

    def test_relative_due(self) -> None:
        tz = timezone(timedelta(hours=8))
        now = datetime(2026, 10, 27, 9, 46, tzinfo=tz)
        due = self.main.parse_relative_due("明天凌晨四点提醒我更新系统", now, tz)
        self.assertIsNotNone(due)
        local = due.astimezone(tz)
        self.assertEqual(local.date().isoformat(), "2026-10-28")
        self.assertEqual((local.hour, local.minute), (4, 0))

    def test_time_ambiguity(self) -> None:
        tz = timezone(timedelta(hours=8))
        now = datetime(2026, 10, 27, 9, 46, tzinfo=tz)
        due, ambiguous = self.main.parse_time_spec("11点", now, tz)
        self.assertIsNone(due)
        self.assertTrue(ambiguous)
        due, ambiguous = self.main.parse_time_spec("晚上11点", now, tz)
        self.assertFalse(ambiguous)
        self.assertEqual(due.astimezone(tz).hour, 23)
        due, ambiguous = self.main.parse_time_spec("23:00", now, tz)
        self.assertFalse(ambiguous)
        self.assertEqual(due.astimezone(tz).hour, 23)

    def test_weekday_due(self) -> None:
        tz = timezone(timedelta(hours=8))
        now = datetime(2026, 10, 28, 4, 21, tzinfo=tz)  # 周三
        due, ambiguous = self.main.parse_time_spec("周五晚上六点", now, tz)
        self.assertFalse(ambiguous)
        local = due.astimezone(tz)
        self.assertEqual(local.date().isoformat(), "2026-10-30")
        self.assertEqual((local.hour, local.minute), (18, 0))

    def test_absolute_and_relative_date(self) -> None:
        tz = timezone(timedelta(hours=8))
        now = datetime(2026, 10, 28, 4, 23, tzinfo=tz)

        due, ambiguous = self.main.parse_time_spec("30号晚上8点", now, tz)
        self.assertFalse(ambiguous)
        local = due.astimezone(tz)
        self.assertEqual(local.date().isoformat(), "2026-10-30")
        self.assertEqual((local.hour, local.minute), (20, 0))

        due, ambiguous = self.main.parse_time_spec("下周三晚上6点", now, tz)
        self.assertFalse(ambiguous)
        local = due.astimezone(tz)
        self.assertEqual(local.date().isoformat(), "2026-11-04")
        self.assertEqual((local.hour, local.minute), (18, 0))

        due, ambiguous = self.main.parse_time_spec("30号晚上8点", datetime(
            2026, 10, 31, 9, 0, tzinfo=tz,
        ), tz)
        self.assertEqual(due.astimezone(tz).date().isoformat(), "2026-11-30")

        due, ambiguous = self.main.parse_time_spec("三号下午五点", now, tz)
        self.assertFalse(ambiguous)
        local = due.astimezone(tz)
        self.assertEqual(local.date().isoformat(), "2026-11-03")
        self.assertEqual((local.hour, local.minute), (17, 0))

        due, ambiguous = self.main.parse_time_spec("十月三号下午五点", now, tz)
        self.assertFalse(ambiguous)
        self.assertEqual(due.astimezone(tz).date().isoformat(), "2027-10-03")

        due, ambiguous = self.main.parse_time_spec("四号下午三点", now, tz)
        self.assertFalse(ambiguous)
        local = due.astimezone(tz)
        self.assertEqual(local.date().isoformat(), "2026-11-04")
        self.assertEqual((local.hour, local.minute), (15, 0))

        due, ambiguous = self.main.parse_time_spec("七号上午十一点四十五", now, tz)
        self.assertFalse(ambiguous)
        local = due.astimezone(tz)
        self.assertEqual(local.date().isoformat(), "2026-11-07")
        self.assertEqual((local.hour, local.minute), (11, 45))

        due, ambiguous = self.main.parse_time_spec("七号上午十一点零五", now, tz)
        self.assertFalse(ambiguous)
        self.assertEqual(due.astimezone(tz).minute, 5)

        due, ambiguous = self.main.parse_time_spec("二号中午", now, tz)
        self.assertIsNone(due)
        self.assertTrue(ambiguous)

        due, ambiguous = self.main.parse_time_spec("二号中午十二点", now, tz)
        self.assertFalse(ambiguous)
        local = due.astimezone(tz)
        self.assertEqual(local.date().isoformat(), "2026-11-02")
        self.assertEqual((local.hour, local.minute), (12, 0))

        due, ambiguous = self.main.parse_time_spec("二号下午", now, tz)
        self.assertIsNone(due)
        self.assertTrue(ambiguous)

    def test_stored_period_beats_model_guess(self) -> None:
        tz = timezone(timedelta(hours=8))
        now = datetime(2026, 10, 28, 5, 38, tzinfo=tz)

        class Dummy:
            pass

        dummy = Dummy()
        dummy.tz = tz
        dummy._clarify_period = lambda item: "中午"
        dummy._clarify_hour = lambda item: 0
        dummy._clarify_date = lambda item: date(2026, 11, 4)
        dummy._clarify_minute = lambda item: 30
        dummy._period_due = lambda period, hour, at, **kwargs: self.main.MemoryPlugin._period_due(
            dummy, period, hour, at, **kwargs,
        )
        due = self.main.MemoryPlugin._draft_period_due(
            dummy,
            {
                "keywords": [
                    "clarify:0",
                    "clarify-period:中午",
                    "clarify-date:2026-11-04",
                    "clarify-minute:30",
                ],
            },
            "上午十一点半",
            "十一点半",
            now,
        )
        self.assertIsNotNone(due)
        local = due.astimezone(tz)
        self.assertEqual(local.date().isoformat(), "2026-11-04")
        self.assertEqual((local.hour, local.minute), (11, 30))

    def test_time_text_grounding(self) -> None:
        self.assertFalse(
            self.main.time_text_grounded("提醒我九点洗头", "今天早上九点"),
        )
        self.assertTrue(self.main.time_text_grounded("提醒我九点洗头", "九点"))
        self.assertTrue(
            self.main.time_text_grounded("明天十点提醒我吃药", "明天十点"),
        )
        self.assertTrue(
            self.main.time_text_grounded(
                "晚上",
                "周五晚上六点",
                "提醒我周五六点要出去吃饭",
            ),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
