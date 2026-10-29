"""离线冒烟测试：不依赖 AstrBot，覆盖存储、提炼、检索、迁移。

运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
import unittest
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from consolidation import Consolidator, parse_topics_json  # noqa: E402
from media import render_message_text, sanitize_display_name  # noqa: E402
from memory_db import (  # noqa: E402
    MemoryDB,
    compute_expire_at,
    compute_ttl_days,
    make_scope_id,
    make_timezone,
)
from migration import export_bundle, import_bundle  # noqa: E402
from retrieval import Retriever, is_recall_intent, parse_time_range  # noqa: E402
from textutil import extract_terms  # noqa: E402

TZ = make_timezone("Asia/Shanghai")
UMO = "aiocqhttp:friend:10001"


async def _fake_llm(prompt: str, umo: str) -> tuple[str, str]:
    if "JSON 数组" in prompt:
        uids = re.findall(r"\[([^\]]+)\]", prompt)
        start, end = (uids[0], uids[-1]) if uids else ("u1", "u2")
        return (
            json.dumps(
                [
                    {
                        "title": "周末开会",
                        "keywords": ["开会", "周六", "项目"],
                        "summary_search": "周六上午十点开会，讨论项目进度。",
                        "summary_week": "用户和助手约定周六上午十点开会，讨论项目进度，地点未定。",
                        "facts": ["会议定在周六上午十点", "地点未定"],
                        "importance": 180,
                        "start_uid": start,
                        "end_uid": end,
                    },
                ],
                ensure_ascii=False,
            ),
            "fake-provider",
        )
    return (
        json.dumps(
            {
                "summary": "周六开会讨论项目进度，会议地点仍未确定。",
                "keywords": ["开会", "项目"],
            },
            ensure_ascii=False,
        ),
        "fake-provider",
    )


class MemoryCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_path = Path(__file__).parent / f".tmp_{uuid.uuid4().hex[:8]}"
        self.tmp_path.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.tmp_path, ignore_errors=True))
        self.db = MemoryDB(self.tmp_path / "memory.db")
        self.addCleanup(self.db.close)
        self.cfg = {
            "timezone": "Asia/Shanghai",
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
            "retrieve_top_k": 4,
            "deep_top_k": 8,
            "relevance_min": 0.12,
            "inject_budget_chars": 1200,
            "suppress_days": 7,
            "renew_factor": 1.3,
            "max_renewals": 3,
            "export_shard_rows": 1000,
        }

    def _add_day(self, day: str = "2026-10-20") -> None:
        created = f"{day}T10:00:00+08:00"
        self.db.add_message(
            msg_uid=f"{UMO}#m:1",
            umo=UMO,
            session_id="10001",
            platform="aiocqhttp",
            role="user",
            sender_id="10001",
            sender_name="小明",
            content="周六上午十点开会，讨论项目进度。",
            created_at=created,
            day=day,
        )
        self.db.add_message(
            msg_uid=f"{UMO}#m:2",
            umo=UMO,
            session_id="10001",
            platform="aiocqhttp",
            role="assistant",
            sender_id="bot",
            sender_name="bot",
            content="好，地点定了吗？",
            created_at=created,
            day=day,
        )

    def test_ttl_and_expire(self) -> None:
        self.assertEqual(compute_ttl_days(180, 1.0, 2, 365), 180)
        self.assertEqual(compute_ttl_days(1, 1.0, 2, 365), 2)
        self.assertEqual(compute_ttl_days(999, 1.0, 2, 365), 365)
        expire = compute_expire_at("2026-10-20", 7, TZ)
        self.assertTrue(expire.startswith("2026-10-27"))

    def test_consolidate_and_retrieve(self) -> None:
        self._add_day()
        consolidator = Consolidator(
            self.db,
            self.cfg,
            _fake_llm,
            TZ,
            now_fn=lambda: datetime(2026, 10, 21, 4, 0, tzinfo=TZ),
        )
        stats = asyncio.run(consolidator.run(day="2026-10-20", umo=UMO))
        self.assertEqual(stats["topics"], 1)
        self.assertEqual(self.db.counts()["topics"][0]["n"], 1)

        retriever = Retriever(self.db, self.cfg, TZ)
        topics = retriever.search(
            UMO,
            "上次说的开会是哪天？",
            now=datetime(2026, 10, 21, 5, 0, tzinfo=timezone.utc),
            deep=True,
        )
        self.assertEqual(len(topics), 1)
        block = retriever.build_block(topics)
        self.assertIn("长期记忆参考", block)
        self.assertIn("周六", block)

    def test_time_range_and_intent(self) -> None:
        now_local = datetime(2026, 10, 27, 12, 0, tzinfo=TZ)
        self.assertEqual(
            parse_time_range("昨天聊了什么", now_local),
            ("2026-10-26", "2026-10-26"),
        )
        self.assertEqual(
            parse_time_range("上周", now_local),
            ("2026-10-19", "2026-10-25"),
        )
        self.assertTrue(is_recall_intent("你还记得那次开会吗"))
        self.assertFalse(is_recall_intent("今天天气怎么样"))
        self.assertIn("开会", extract_terms("还记得开会的事吗"))

    def test_promotion(self) -> None:
        self._add_day()
        consolidator = Consolidator(
            self.db,
            self.cfg,
            _fake_llm,
            TZ,
            now_fn=lambda: datetime(2026, 10, 21, 4, 0, tzinfo=TZ),
        )
        asyncio.run(consolidator.run(day="2026-10-20", umo=UMO))
        topic = self.db.search_fts(UMO, ["开会"])[0]
        self.assertEqual(topic["layer"], "week")

        later = Consolidator(
            self.db,
            self.cfg,
            _fake_llm,
            TZ,
            now_fn=lambda: datetime(2026, 10, 29, 4, 0, tzinfo=TZ),
        )
        stats = asyncio.run(later.run())
        self.assertEqual(stats["promoted"], 1)
        topic = self.db.get_topic(topic["id"])
        self.assertEqual(topic["layer"], "month")
        self.assertIn("周六", topic["summary_month"])
        self.assertEqual(len(topic["facts"]), 2)

    def test_export_import(self) -> None:
        self._add_day()
        consolidator = Consolidator(
            self.db,
            self.cfg,
            _fake_llm,
            TZ,
            now_fn=lambda: datetime(2026, 10, 21, 4, 0, tzinfo=TZ),
        )
        asyncio.run(consolidator.run(day="2026-10-20", umo=UMO))
        out_dir = self.tmp_path / "exports"
        bundle = export_bundle(self.db, out_dir, self.cfg, markdown=True)
        self.assertTrue(bundle.exists())
        self.assertGreater(bundle.stat().st_size, 0)
        with zipfile.ZipFile(bundle) as zf:
            names = zf.namelist()
            self.assertIn("manifest.json", names)
            self.assertTrue(any(n.startswith("scopes/") for n in names))
            self.assertTrue(any(n.startswith("items/") for n in names))
            self.assertTrue(any(n.startswith("markdown/") for n in names))

        db2 = MemoryDB(self.tmp_path / "memory2.db")
        self.addCleanup(db2.close)
        stats = import_bundle(db2, bundle)
        self.assertEqual(stats["messages"], 2)
        self.assertEqual(stats["topics"], 1)
        self.assertEqual(stats["scopes"], 1)
        self.assertGreaterEqual(stats["items"], 1)
        self.assertGreaterEqual(len(db2.search_fts(UMO, ["开会"])), 1)

    def test_scope_binding(self) -> None:
        self._add_day()
        consolidator = Consolidator(
            self.db,
            self.cfg,
            _fake_llm,
            TZ,
            now_fn=lambda: datetime(2026, 10, 21, 4, 0, tzinfo=TZ),
        )
        asyncio.run(consolidator.run(day="2026-10-20", umo=UMO))
        scope_id = self.db.resolve_scope(UMO)
        self.assertTrue(scope_id)

        new_umo = "aiocqhttp:friend:20002"
        self.db.bind_session(
            new_umo,
            scope_id,
            bound_by="test",
            kind="private",
        )
        retriever = Retriever(self.db, self.cfg, TZ)
        topics = retriever.search(
            new_umo,
            "还记得开会吗",
            now=datetime(2026, 10, 21, 5, 0, tzinfo=timezone.utc),
            deep=True,
        )
        self.assertEqual(len(topics), 1)

        with self.assertRaises(ValueError):
            self.db.bind_session(
                "aiocqhttp:group:999",
                scope_id,
                kind="group",
            )

        self.assertTrue(self.db.unbind_session(new_umo))
        self.assertEqual(
            retriever.search(
                new_umo,
                "还记得开会吗",
                now=datetime(2026, 10, 21, 5, 0, tzinfo=timezone.utc),
                deep=True,
            ),
            [],
        )

    def test_deterministic_scope_id(self) -> None:
        key = "aiocqhttp:FriendMessage:10001"
        scope_id = self.db.resolve_scope(
            key,
            platform="aiocqhttp",
            kind="private",
            external_id="10001",
            binding_key=key,
        )
        self.assertEqual(scope_id, make_scope_id(key))

    def test_scope_alias(self) -> None:
        target = self.db.ensure_scope(
            platform="qq_official", kind="group", external_id="10001",
        )
        self.db.ensure_scope(
            platform="myapp", kind="group", external_id="20002",
        )
        self.assertTrue(
            self.db.set_scope_alias("myapp:group:20002", target, note="迁移"),
        )
        self.assertEqual(
            self.db.resolve_scope(
                "myapp:group:20002",
                platform="myapp",
                kind="group",
                external_id="20002",
                alias_key="myapp:group:20002",
            ),
            target,
        )
        self.assertTrue(self.db.delete_scope_alias("myapp:group:20002"))
        self.assertNotEqual(
            self.db.resolve_scope(
                "myapp:group:20002",
                platform="myapp",
                kind="group",
                external_id="20002",
                alias_key="myapp:group:20002",
            ),
            target,
        )

    def test_archived_and_due_queue(self) -> None:
        self._add_day()
        consolidator = Consolidator(
            self.db,
            self.cfg,
            _fake_llm,
            TZ,
            now_fn=lambda: datetime(2026, 10, 21, 4, 0, tzinfo=TZ),
        )
        asyncio.run(consolidator.run(day="2026-10-20", umo=UMO))
        scope_id = self.db.resolve_scope(UMO)
        topic = self.db.search_fts(UMO, ["开会"])[0]

        # 归档后仍可 search，但不参与注入。
        self.db.update_topic(topic["id"], status="expired")
        retriever = Retriever(self.db, self.cfg, TZ)
        now = datetime(2026, 10, 21, 5, 0, tzinfo=timezone.utc)
        self.assertEqual(
            len(retriever.search(UMO, "开会", now=now, for_injection=True)),
            0,
        )
        self.assertGreaterEqual(
            len(retriever.search(UMO, "开会", now=now, for_injection=False)),
            1,
        )

        # 到期队列不看语义相关度。
        self.db.insert_item(
            {
                "scope_id": scope_id,
                "type": "reminder",
                "text": "十点去办卡",
                "due_at": "2026-10-21T02:00:00+00:00",
                "status": "pending",
                "inject_until": "2026-10-22T02:00:00+00:00",
            },
        )
        block, items = retriever.due_block(scope_id, now=now)
        self.assertIn("办卡", block)
        self.assertEqual(len(items), 1)

    def test_incremental_consolidation(self) -> None:
        self._add_day()
        consolidator = Consolidator(
            self.db,
            self.cfg,
            _fake_llm,
            TZ,
            now_fn=lambda: datetime(2026, 10, 21, 4, 0, tzinfo=TZ),
        )
        asyncio.run(consolidator.run(day="2026-10-20", umo=UMO))
        self.db.add_message(
            msg_uid=f"{UMO}#m:3",
            umo=UMO,
            role="user",
            content="再补一句：地点定在会议室。",
            created_at="2026-10-20T11:00:00+08:00",
            day="2026-10-20",
        )
        self.assertEqual(
            self.db.pairs_for_day("2026-10-20"),
            [("2026-10-20", UMO)],
        )
        stats = asyncio.run(consolidator.run(day="2026-10-20", umo=UMO))
        self.assertEqual(stats["topics"], 1)
        self.assertEqual(self.db.max_message_id("2026-10-20", UMO), 3)
        # 没有新消息时不重复处理。
        self.assertEqual(self.db.pairs_for_day("2026-10-20"), [])

    def test_rebuild_day(self) -> None:
        self._add_day()
        consolidator = Consolidator(
            self.db,
            self.cfg,
            _fake_llm,
            TZ,
            now_fn=lambda: datetime(2026, 10, 21, 4, 0, tzinfo=TZ),
        )
        asyncio.run(consolidator.run(day="2026-10-20", umo=UMO))
        stats = asyncio.run(consolidator.rebuild_day("2026-10-20", UMO))
        self.assertEqual(stats["deleted"]["topics"], 1)
        self.assertEqual(stats["topics"], 1)
        self.assertEqual(len(self.db.search_fts(UMO, ["开会"])), 1)

    def test_cancel_all_items(self) -> None:
        scope_id = self.db.resolve_scope(UMO)
        item_id = self.db.insert_item(
            {
                "scope_id": scope_id,
                "type": "reminder",
                "text": "测试待办",
                "status": "pending",
            },
        )
        ids = self.db.cancel_all_items(scope_id, now_iso="2026-10-27T00:00:00+00:00")
        self.assertIn(item_id, ids)
        self.assertEqual(self.db.get_item(item_id)["status"], "cancelled")

    def test_media_render(self) -> None:
        self.assertEqual(
            render_message_text(
                "你好",
                [{"id": "abc123", "kind": "voice", "transcript": "今天开会"}],
            ),
            "你好 [语音#abc123] 转写：今天开会",
        )
        self.assertEqual(
            sanitize_display_name("初音未来 开心", "jpg"),
            "初音未来_开心.jpg",
        )


class ParserTest(unittest.TestCase):
    def test_parse_topics(self) -> None:
        raw = """```json
        [{"title":"A","keywords":["x"],"summary_search":"s","summary_week":"w",
          "facts":["f"],"importance":999,"start_uid":"u1","end_uid":"u2"}]
        ```"""
        topics = parse_topics_json(raw)
        self.assertEqual(topics[0]["importance"], 999)  # clamp 由 Consolidator 负责
        self.assertEqual(topics[0]["facts"], ["f"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
