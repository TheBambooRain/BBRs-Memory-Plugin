"""SQLite 存储层：消息、话题、附件、FTS、任务、视图。

设计要点：
- 一张 topics 表 + layer 字段，week/month/year 用视图暴露；不做三张物理表，
  避免迁移时三份数据对不上（参考 MemoryOS 的分层思想，但存储上只留一份真相）。
- messages 是不可变的原始档案；附件旁挂在 message_attachments，v0.2 只 UPDATE 字段。
- 所有跨设备标识用 msg_uid / topic UUID，自增 id 只在本地使用。
- FTS5 用 unicode61 + jieba token 串，避免 trigram 对 2 字中文词的匹配限制。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

try:  # 作为包加载（AstrBot 正常路径）时用相对导入，热重载才不会拿到旧模块
    from .textutil import tokens_text
except ImportError:  # 离线测试 / 单文件加载
    from textutil import tokens_text

SCHEMA_VERSION = 3


def make_scope_id(key: str) -> str:
    """稳定的 scope_id：同一个 key 在任何机器上得到同一个 UUID。"""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"bbrs-memory:scope:{key}").hex
DEFAULT_TZ = "Asia/Shanghai"


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------


def make_timezone(name: str | None) -> timezone | ZoneInfo:
    """尽量用 ZoneInfo；Windows 上缺少 tzdata 时退回固定 +08:00。"""
    if not name:
        name = DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return timezone(timedelta(hours=8), name=name)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc,
        )
    except ValueError:
        return None


def local_day(dt: datetime, tz) -> str:
    return dt.astimezone(tz).date().isoformat()


def end_of_local_day_utc(day: str, tz) -> datetime:
    d = date.fromisoformat(day)
    local_end = datetime.combine(d, time(23, 59, 59), tzinfo=tz)
    return local_end.astimezone(timezone.utc)


def compute_expire_at(day: str, ttl_days: int, tz) -> str:
    """到期时间 = 对话当天 23:59:59 + ttl_days。"""
    return to_iso(end_of_local_day_utc(day, tz) + timedelta(days=int(ttl_days)))


def compute_ttl_days(
    importance: int,
    unit_days: float,
    min_days: int,
    max_days: int,
) -> int:
    """重要度 × 单位天数，截断到上下限。参考 MemoryBank 的遗忘曲线做离散化。"""
    try:
        raw = float(importance) * float(unit_days)
    except (TypeError, ValueError):
        raw = float(min_days)
    return max(int(min_days), min(int(max_days), int(round(raw))))


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def normalize_kind(kind: str | None) -> str:
    raw = (kind or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    if "group" in raw or "guild" in raw or "channel" in raw:
        return "group"
    if "friend" in raw or "private" in raw or "user" in raw:
        return "private"
    return raw or "private"


def parse_umo(umo: str) -> tuple[str, str, str]:
    """把 AstrBot 的 platform:type:session 拆成 platform/kind/external_id。"""
    parts = str(umo or "").split(":", 2)
    if len(parts) != 3:
        return "unknown", "private", str(umo or "unknown")
    platform, msg_type, session_id = parts
    kind = normalize_kind(msg_type)
    return platform or "unknown", kind, session_id or "unknown"


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_uid TEXT NOT NULL UNIQUE,
    umo TEXT NOT NULL,
    scope_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user',
    sender_id TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    local_day TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_day_umo ON messages(local_day, umo);
CREATE INDEX IF NOT EXISTS idx_messages_umo_id ON messages(umo, id);

CREATE TABLE IF NOT EXISTS scopes (
    scope_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'private',
    external_id TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    person_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(platform, kind, external_id)
);
CREATE INDEX IF NOT EXISTS idx_scopes_person ON scopes(person_id);

CREATE TABLE IF NOT EXISTS session_bindings (
    umo TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    bound_at TEXT NOT NULL,
    bound_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bindings_scope ON session_bindings(scope_id);

CREATE TABLE IF NOT EXISTS scope_aliases (
    alias_key TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aliases_scope ON scope_aliases(scope_id);

CREATE TABLE IF NOT EXISTS message_attachments (
    id TEXT PRIMARY KEY,
    message_uid TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'file',
    platform_ref TEXT NOT NULL DEFAULT '',
    mime TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    local_path TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    transcript TEXT NOT NULL DEFAULT '',
    caption TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    model TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_att_msg ON message_attachments(message_uid);
CREATE INDEX IF NOT EXISTS idx_att_status ON message_attachments(status);
CREATE INDEX IF NOT EXISTS idx_att_sha ON message_attachments(sha256);

CREATE TABLE IF NOT EXISTS topics (
    id TEXT PRIMARY KEY,
    umo TEXT NOT NULL,
    scope_id TEXT NOT NULL DEFAULT '',
    day TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '[]',
    summary_search TEXT NOT NULL DEFAULT '',
    summary_week TEXT NOT NULL DEFAULT '',
    summary_month TEXT NOT NULL DEFAULT '',
    summary_year TEXT NOT NULL DEFAULT '',
    facts TEXT NOT NULL DEFAULT '[]',
    importance INTEGER NOT NULL DEFAULT 100,
    layer TEXT NOT NULL DEFAULT 'week' CHECK(layer IN ('week', 'month', 'year')),
    ttl_days INTEGER NOT NULL DEFAULT 100,
    source_start_uid TEXT NOT NULL DEFAULT '',
    source_end_uid TEXT NOT NULL DEFAULT '',
    source_start_id INTEGER,
    source_end_id INTEGER,
    expire_at TEXT NOT NULL,
    last_hit_at TEXT,
    hit_count INTEGER NOT NULL DEFAULT 0,
    renew_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived', 'expired', 'deleted')),
    pinned INTEGER NOT NULL DEFAULT 0,
    suppress_until TEXT,
    inject_until TEXT,
    search_until TEXT,
    forgotten_at TEXT,
    summary_model TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topics_umo_layer ON topics(umo, layer, status);
CREATE INDEX IF NOT EXISTS idx_topics_day ON topics(day);
CREATE INDEX IF NOT EXISTS idx_topics_expire ON topics(status, expire_at);

CREATE TABLE IF NOT EXISTS memory_items (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    topic_id TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT 'episode',
    text TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '[]',
    importance INTEGER NOT NULL DEFAULT 100,
    due_at TEXT,
    done_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    inject_until TEXT,
    search_until TEXT,
    last_notified_at TEXT,
    last_injected_at TEXT,
    source_start_uid TEXT NOT NULL DEFAULT '',
    source_end_uid TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_scope_status ON memory_items(scope_id, status, due_at);
CREATE INDEX IF NOT EXISTS idx_items_scope_type ON memory_items(scope_id, type, status);
CREATE INDEX IF NOT EXISTS idx_items_topic ON memory_items(topic_id);

CREATE TABLE IF NOT EXISTS reminder_outbox (
    event_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'upsert',
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    acked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON reminder_outbox(status, created_at);

CREATE TABLE IF NOT EXISTS jobs (
    day TEXT NOT NULL,
    umo TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    last_message_id INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    PRIMARY KEY (day, umo, stage)
);

CREATE VIEW IF NOT EXISTS week_memories AS
    SELECT * FROM topics WHERE layer = 'week' AND status = 'active';
CREATE VIEW IF NOT EXISTS month_memories AS
    SELECT * FROM topics WHERE layer = 'month' AND status = 'active';
CREATE VIEW IF NOT EXISTS year_memories AS
    SELECT * FROM topics WHERE layer = 'year' AND status = 'active';
"""


class MemoryDB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            timeout=30,
        )
        self.conn.row_factory = sqlite3.Row
        self._configure()
        self._init_schema()
        self.fts_enabled = self._init_fts()
        if self.get_meta("fts_rebuild_v3", "") != "1":
            self.rebuild_fts()
            self.set_meta("fts_rebuild_v3", "1")
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    # -- 基础设施 ---------------------------------------------------------

    def _configure(self) -> None:
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=5000")

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self._ensure_column("messages", "scope_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("topics", "scope_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("topics", "inject_until", "TEXT")
            self._ensure_column("topics", "search_until", "TEXT")
            self._ensure_column("topics", "forgotten_at", "TEXT")
            self._ensure_column("jobs", "last_message_id", "INTEGER NOT NULL DEFAULT 0")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_scope ON messages(scope_id, id)",
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_topics_scope_layer"
                " ON topics(scope_id, layer, status)",
            )
            self.conn.commit()
            self._backfill_scopes()
            self._repair_scopes()
            self._repair_orphan_scopes()
            self._migrate_scope_ids()
            self._migrate_v3()
            self._migrate_jobs_cursor()

    def _migrate_v3(self) -> None:
        """v2 → v3：TTL 只停止注入，不删除历史。

        旧库的 status 允许 active/expired/deleted；这里继续用 expired 表示
        “归档、可检索但不注入”，避免 ALTER CHECK 约束。
        """
        if self.get_meta("schema_v3_migrated", "") == "1":
            return
        self.conn.execute(
            "UPDATE topics SET inject_until=COALESCE(inject_until, expire_at)"
            " WHERE status='active' AND inject_until IS NULL",
        )
        # expired 保持可检索；deleted 是显式遗忘，保持不可见。
        self.conn.commit()
        self.set_meta("schema_v3_migrated", "1")

    def _migrate_jobs_cursor(self) -> None:
        """给已完成的旧 job 补 last_message_id，避免升级后重跑历史。"""
        if self.get_meta("jobs_cursor_v4", "") == "1":
            return
        self.conn.execute(
            "UPDATE jobs SET last_message_id=COALESCE(("
            " SELECT MAX(m.id) FROM messages m"
            " WHERE m.local_day=jobs.day AND m.umo=jobs.umo"
            "), 0) WHERE status='done' AND last_message_id=0",
        )
        self.conn.commit()
        self.set_meta("jobs_cursor_v4", "1")

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            str(row[1])
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}",
            )

    def _backfill_scopes(self) -> None:
        """v1 → v2：旧数据只有 umo，按 umo 建 scope 并回填 scope_id。"""
        if self.get_meta("scope_backfill_v2", "") == "1":
            return
        rows = self.conn.execute(
            "SELECT umo, MAX(platform) AS platform FROM messages"
            " WHERE scope_id='' GROUP BY umo "
            "UNION "
            "SELECT umo, '' AS platform FROM topics WHERE scope_id='' GROUP BY umo",
        ).fetchall()
        for row in rows:
            umo = str(row["umo"] or "")
            if not umo:
                continue
            platform, kind, external_id = parse_umo(umo)
            # UMO 的前缀可能是人格名/实例名，不是真正的平台。
            # 旧消息里的 platform 字段才是 AstrBot 实际适配器名。
            platform = str(row["platform"] or platform)
            scope_id = self.ensure_scope(
                platform=platform,
                kind=kind,
                external_id=external_id,
                label="",
            )
            self.conn.execute(
                "UPDATE messages SET scope_id=? WHERE umo=? AND scope_id=''",
                (scope_id, umo),
            )
            self.conn.execute(
                "UPDATE topics SET scope_id=? WHERE umo=? AND scope_id=''",
                (scope_id, umo),
            )
        self.conn.commit()
        self.set_meta("scope_backfill_v2", "1")

    def _repair_scopes(self) -> None:
        """合并“同一真实作用域、不同 platform 写法”的重复 scope。

        v0.1.1 早期版本把 UMO 前缀（人格名/实例名）当成 platform，导致同一个人
        出现两个 scope：一个来自旧数据，一个来自新的 qq_official。这里按
        “消息里的真实 platform + 规范化 kind + external_id”归并，只保留有数据的那条。
        """
        if self.get_meta("scope_repair_v3", "") == "1":
            return
        rows = self.conn.execute(
            "SELECT s.*,"
            " (SELECT count(*) FROM messages m WHERE m.scope_id=s.scope_id) AS msg_count,"
            " (SELECT count(*) FROM topics t WHERE t.scope_id=s.scope_id) AS topic_count"
            " FROM scopes s",
        ).fetchall()
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in rows:
            scope = dict(row)
            platform_row = self.conn.execute(
                "SELECT platform FROM messages WHERE scope_id=? AND platform<>''"
                " GROUP BY platform ORDER BY count(*) DESC LIMIT 1",
                (scope["scope_id"],),
            ).fetchone()
            canonical_platform = str(platform_row["platform"]) if platform_row else str(scope["platform"])
            canonical_kind = normalize_kind(str(scope["kind"]))
            key = (canonical_platform, canonical_kind, str(scope["external_id"]))
            scope["_platform"] = canonical_platform
            scope["_kind"] = canonical_kind
            groups.setdefault(key, []).append(scope)

        for (platform, kind, _external), group in groups.items():
            if len(group) == 1:
                scope = group[0]
                self.conn.execute(
                    "UPDATE scopes SET platform=?, kind=? WHERE scope_id=?",
                    (platform, kind, scope["scope_id"]),
                )
                continue
            target = max(
                group,
                key=lambda s: (
                    int(s.get("msg_count", 0)) + int(s.get("topic_count", 0)),
                    str(s.get("created_at", "")),
                ),
            )
            for source in group:
                if source["scope_id"] == target["scope_id"]:
                    continue
                self.conn.execute(
                    "UPDATE messages SET scope_id=? WHERE scope_id=?",
                    (target["scope_id"], source["scope_id"]),
                )
                self.conn.execute(
                    "UPDATE topics SET scope_id=? WHERE scope_id=?",
                    (target["scope_id"], source["scope_id"]),
                )
                self.conn.execute(
                    "UPDATE OR IGNORE session_bindings SET scope_id=? WHERE scope_id=?",
                    (target["scope_id"], source["scope_id"]),
                )
                self.conn.execute(
                    "DELETE FROM session_bindings WHERE scope_id=?",
                    (source["scope_id"],),
                )
                self.conn.execute(
                    "DELETE FROM scopes WHERE scope_id=?",
                    (source["scope_id"],),
                )
            self.conn.execute(
                "UPDATE scopes SET platform=?, kind=?, label=CASE"
                " WHEN label='' THEN ? ELSE label END WHERE scope_id=?",
                (platform, kind, str(target.get("label") or ""), target["scope_id"]),
            )
        self.conn.commit()
        self.set_meta("scope_repair_v3", "1")

    def _repair_orphan_scopes(self) -> None:
        """把只有 topic/item、没有消息的重复 scope 并回有消息的 scope。

        v1.0.4 早期整理逻辑用 UMO 前缀（人格名）推平台，可能造出
        platform='主人格'、messages=0 的空壳 scope。这里按 external_id + kind
        找到唯一有消息的 scope，迁移 topics/items 后删掉空壳。
        """
        if self.get_meta("scope_orphan_repair_v4", "") == "1":
            return
        rows = self.conn.execute(
            "SELECT s.*,"
            " (SELECT count(*) FROM messages m WHERE m.scope_id=s.scope_id) AS msg_count,"
            " (SELECT count(*) FROM topics t WHERE t.scope_id=s.scope_id) AS topic_count,"
            " (SELECT count(*) FROM memory_items i WHERE i.scope_id=s.scope_id) AS item_count"
            " FROM scopes s",
        ).fetchall()
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            scope = dict(row)
            key = (str(scope.get("external_id") or ""), normalize_kind(str(scope.get("kind") or "")))
            groups.setdefault(key, []).append(scope)
        for _key, group in groups.items():
            if len(group) < 2:
                continue
            with_messages = [s for s in group if int(s.get("msg_count", 0)) > 0]
            if len(with_messages) != 1:
                continue
            target = with_messages[0]
            for source in group:
                if source["scope_id"] == target["scope_id"]:
                    continue
                if int(source.get("msg_count", 0)) > 0:
                    continue
                self.conn.execute(
                    "UPDATE topics SET scope_id=? WHERE scope_id=?",
                    (target["scope_id"], source["scope_id"]),
                )
                self.conn.execute(
                    "UPDATE memory_items SET scope_id=? WHERE scope_id=?",
                    (target["scope_id"], source["scope_id"]),
                )
                self.conn.execute(
                    "UPDATE OR IGNORE session_bindings SET scope_id=? WHERE scope_id=?",
                    (target["scope_id"], source["scope_id"]),
                )
                self.conn.execute(
                    "DELETE FROM session_bindings WHERE scope_id=?",
                    (source["scope_id"],),
                )
                self.conn.execute(
                    "DELETE FROM scopes WHERE scope_id=?",
                    (source["scope_id"],),
                )
        self.conn.commit()
        self.set_meta("scope_orphan_repair_v4", "1")

    def _migrate_scope_ids(self) -> None:
        """把旧随机 scope_id 改成 uuid5(UMO)。

        只处理“消息只对应一个 UMO”的 scope；绑定/多会话共享的 scope
        保留原 ID，避免破坏已有映射。
        """
        if self.get_meta("scope_uuid5_v5", "") == "1":
            return
        rows = self.conn.execute(
            "SELECT s.scope_id, COUNT(DISTINCT m.umo) AS umo_count,"
            " MIN(m.umo) AS umo FROM scopes s"
            " JOIN messages m ON m.scope_id=s.scope_id"
            " GROUP BY s.scope_id",
        ).fetchall()
        for row in rows:
            old_id = str(row["scope_id"])
            if not re.fullmatch(r"[0-9a-f]{32}", old_id):
                continue
            if int(row["umo_count"] or 0) != 1:
                continue
            umo = str(row["umo"] or "")
            if not umo:
                continue
            new_id = make_scope_id(umo)
            if new_id == old_id:
                continue
            if self.conn.execute(
                "SELECT 1 FROM scopes WHERE scope_id=?", (new_id,),
            ).fetchone():
                continue
            for table in (
                "messages",
                "topics",
                "memory_items",
                "session_bindings",
                "scope_aliases",
            ):
                self.conn.execute(
                    f"UPDATE {table} SET scope_id=? WHERE scope_id=?",
                    (new_id, old_id),
                )
            self.conn.execute(
                "UPDATE scopes SET scope_id=?, updated_at=? WHERE scope_id=?",
                (new_id, to_iso(utc_now()), old_id),
            )
        self.conn.commit()
        self.set_meta("scope_uuid5_v5", "1")

    def _init_fts(self) -> bool:
        try:
            with self._lock:
                self.conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS topic_fts USING fts5("
                    "topic_id UNINDEXED, title, keywords, summary, tokens,"
                    " tokenize='unicode61')",
                )
                self.conn.commit()
            return True
        except sqlite3.Error as exc:
            # 极少数 Python 构建没编 FTS5；退化为 LIKE 检索，不影响主流程。
            try:
                import logging

                logging.getLogger("astrbot.plugin.memory_v01").warning(
                    "FTS5 不可用，退化为 LIKE 检索: %s",
                    exc,
                )
            except Exception:  # noqa: BLE001
                pass
            return False

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.commit()
            except sqlite3.Error:
                pass
            self.conn.close()

    def backup_to(self, dest_path: str | Path) -> Path:
        """用 SQLite backup API 做一致快照，导出时不会读到半截事务。"""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            dest = sqlite3.connect(str(dest_path))
            try:
                self.conn.backup(dest)
            finally:
                dest.close()
        return dest_path

    # -- meta -------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            self.conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key=?",
                (key,),
            ).fetchone()
        return row["value"] if row else default

    # -- scopes -----------------------------------------------------------

    def ensure_scope(
        self,
        *,
        platform: str,
        kind: str,
        external_id: str,
        label: str = "",
        person_id: str | None = None,
        scope_id: str | None = None,
    ) -> str:
        platform = str(platform or "unknown")
        kind = normalize_kind(kind)
        external_id = str(external_id or "unknown")
        label = str(label or "")
        now = to_iso(utc_now())
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM scopes WHERE platform=? AND kind=? AND external_id=?",
                (platform, kind, external_id),
            ).fetchone()
            if row:
                if label and label != row["label"]:
                    self.conn.execute(
                        "UPDATE scopes SET label=?, updated_at=? WHERE scope_id=?",
                        (label, now, row["scope_id"]),
                    )
                    self.conn.commit()
                return str(row["scope_id"])

            new_id = scope_id or uuid.uuid4().hex
            self.conn.execute(
                "INSERT OR IGNORE INTO scopes"
                "(scope_id, platform, kind, external_id, label, person_id,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (new_id, platform, kind, external_id, label, person_id, now, now),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT scope_id FROM scopes WHERE platform=? AND kind=? AND external_id=?",
                (platform, kind, external_id),
            ).fetchone()
            return str(row["scope_id"]) if row else new_id

    def resolve_scope(
        self,
        umo: str,
        *,
        platform: str | None = None,
        kind: str | None = None,
        external_id: str | None = None,
        label: str = "",
        create: bool = True,
        binding_key: str | None = None,
        alias_key: str | None = None,
        scope_id: str | None = None,
    ) -> str:
        """当前会话 → scope_id：绑定 > 别名 > 指定 scope > 默认三元组。"""
        umo = str(umo or "")
        p_umo, k_umo, e_umo = parse_umo(umo)
        platform = platform or p_umo
        kind = normalize_kind(kind or k_umo)
        external_id = str(external_id or e_umo)
        key = str(binding_key or umo)
        alias_key = str(alias_key or f"{platform}:{kind}:{external_id}")

        with self._lock:
            row = self.conn.execute(
                "SELECT scope_id FROM session_bindings WHERE umo=?",
                (key,),
            ).fetchone()
        if row:
            bound = self.get_scope(str(row["scope_id"]))
            # 防止手工导入/改库把私聊绑到群；类型不一致时退回默认 scope。
            if bound and normalize_kind(bound.get("kind")) == kind:
                return str(row["scope_id"])

        with self._lock:
            row = self.conn.execute(
                "SELECT scope_id FROM scope_aliases WHERE alias_key=?",
                (alias_key,),
            ).fetchone()
        if row:
            aliased = self.get_scope(str(row["scope_id"]))
            if aliased and normalize_kind(aliased.get("kind")) == kind:
                return str(row["scope_id"])

        if scope_id:
            explicit = self.get_scope(str(scope_id))
            if explicit and normalize_kind(explicit.get("kind")) == kind:
                return str(scope_id)

        if not create:
            with self._lock:
                row = self.conn.execute(
                    "SELECT scope_id FROM scopes"
                    " WHERE platform=? AND kind=? AND external_id=?",
                    (platform, kind, external_id),
                ).fetchone()
            return str(row["scope_id"]) if row else ""
        return self.ensure_scope(
            platform=platform,
            kind=kind,
            external_id=external_id,
            label=label,
            scope_id=scope_id or make_scope_id(key),
        )

    def get_session_binding(self, umo: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM session_bindings WHERE umo=?",
                (umo,),
            ).fetchone()
        return dict(row) if row else None

    def bind_session(
        self,
        umo: str,
        scope_id: str,
        *,
        bound_by: str = "",
        kind: str | None = None,
    ) -> dict[str, Any] | None:
        scope = self.get_scope(scope_id)
        if not scope:
            return None
        if kind and normalize_kind(kind) != normalize_kind(scope.get("kind")):
            raise ValueError(
                f"类型不一致：当前会话是 {normalize_kind(kind)}，"
                f"目标 scope 是 {scope.get('kind')}",
            )
        now = to_iso(utc_now())
        with self._lock:
            self.conn.execute(
                "INSERT INTO session_bindings(umo, scope_id, bound_at, bound_by)"
                " VALUES(?,?,?,?) ON CONFLICT(umo) DO UPDATE SET"
                " scope_id=excluded.scope_id, bound_at=excluded.bound_at,"
                " bound_by=excluded.bound_by",
                (umo, scope_id, now, bound_by),
            )
            self.conn.commit()
        return scope

    def unbind_session(self, umo: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM session_bindings WHERE umo=?",
                (umo,),
            )
            self.conn.commit()
        return bool(cur.rowcount)

    def get_scope_alias(self, alias_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM scope_aliases WHERE alias_key=?",
                (alias_key,),
            ).fetchone()
        return dict(row) if row else None

    def set_scope_alias(
        self,
        alias_key: str,
        scope_id: str,
        *,
        note: str = "",
    ) -> bool:
        if not self.get_scope(scope_id):
            return False
        now = to_iso(utc_now())
        with self._lock:
            self.conn.execute(
                "INSERT INTO scope_aliases(alias_key, scope_id, note, created_at, updated_at)"
                " VALUES(?,?,?,?,?) ON CONFLICT(alias_key) DO UPDATE SET"
                " scope_id=excluded.scope_id, note=excluded.note,"
                " updated_at=excluded.updated_at",
                (alias_key, scope_id, note, now, now),
            )
            self.conn.commit()
        return True

    def delete_scope_alias(self, alias_key: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM scope_aliases WHERE alias_key=?",
                (alias_key,),
            )
            self.conn.commit()
        return bool(cur.rowcount)

    def list_scope_aliases(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM scope_aliases ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_scope(self, scope_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM scopes WHERE scope_id=?",
                (scope_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_scopes(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT s.*,"
                " (SELECT count(*) FROM messages m WHERE m.scope_id=s.scope_id) AS msg_count,"
                " (SELECT count(*) FROM topics t WHERE t.scope_id=s.scope_id"
                "   AND t.status='active') AS topic_count,"
                " (SELECT count(*) FROM session_bindings b"
                "   WHERE b.scope_id=s.scope_id) AS bind_count"
                " FROM scopes s ORDER BY s.updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def scope_counts(self) -> dict[str, int]:
        with self._lock:
            scopes = self.conn.execute("SELECT count(*) AS n FROM scopes").fetchone()["n"]
            bindings = self.conn.execute(
                "SELECT count(*) AS n FROM session_bindings",
            ).fetchone()["n"]
        return {"scopes": int(scopes), "bindings": int(bindings)}

    def merge_scopes(self, from_scope: str, to_scope: str) -> int:
        """管理员合并两段记忆；只改 scope_id，不动 umo 指针。"""
        if from_scope == to_scope:
            return 0
        if not self.get_scope(to_scope):
            raise ValueError("目标 scope 不存在")
        with self._lock:
            m = self.conn.execute(
                "UPDATE messages SET scope_id=? WHERE scope_id=?",
                (to_scope, from_scope),
            ).rowcount
            t = self.conn.execute(
                "UPDATE topics SET scope_id=? WHERE scope_id=?",
                (to_scope, from_scope),
            ).rowcount
            self.conn.execute(
                "UPDATE session_bindings SET scope_id=? WHERE scope_id=?",
                (to_scope, from_scope),
            )
            self.conn.execute(
                "DELETE FROM scopes WHERE scope_id=?",
                (from_scope,),
            )
            self.conn.commit()
        self.rebuild_fts()
        return int(m + t)

    # -- messages ---------------------------------------------------------

    def add_message(
        self,
        *,
        msg_uid: str,
        umo: str,
        session_id: str = "",
        platform: str = "",
        role: str = "user",
        sender_id: str = "",
        sender_name: str = "",
        content: str = "",
        created_at: str | None = None,
        day: str = "",
        scope_id: str = "",
    ) -> int:
        created_at = created_at or to_iso(utc_now())
        if not scope_id:
            scope_id = self.resolve_scope(umo, platform=platform, label=sender_name)
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO messages"
                "(msg_uid, umo, scope_id, session_id, platform, role, sender_id,"
                " sender_name, content, created_at, local_day)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    msg_uid,
                    umo,
                    scope_id,
                    session_id,
                    platform,
                    role,
                    sender_id,
                    sender_name,
                    content,
                    created_at,
                    day,
                ),
            )
            self.conn.commit()
            if cur.rowcount:
                return int(cur.lastrowid or 0)
            row = self.conn.execute(
                "SELECT id FROM messages WHERE msg_uid=?",
                (msg_uid,),
            ).fetchone()
            return int(row["id"]) if row else 0

    def add_attachment(
        self,
        *,
        attachment_id: str,
        message_uid: str,
        kind: str,
        platform_ref: str = "",
        mime: str = "",
        size: int = 0,
        sha256: str = "",
        status: str = "pending",
        created_at: str | None = None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO message_attachments"
                "(id, message_uid, kind, platform_ref, mime, bytes, sha256, status, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    attachment_id,
                    message_uid,
                    kind,
                    platform_ref,
                    mime,
                    int(size or 0),
                    sha256,
                    status,
                    created_at or to_iso(utc_now()),
                ),
            )
            self.conn.commit()

    def get_attachments(self, message_uid: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM message_attachments WHERE message_uid=? ORDER BY id",
                (message_uid,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_day_attachments(self, day: str, umo: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT a.* FROM message_attachments a "
                "JOIN messages m ON m.msg_uid=a.message_uid "
                "WHERE m.local_day=? AND m.umo=? ORDER BY a.id",
                (day, umo),
            ).fetchall()
        return [dict(r) for r in rows]

    def has_pending_attachments(self, day: str, umo: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM message_attachments a "
                "JOIN messages m ON m.msg_uid=a.message_uid "
                "WHERE m.local_day=? AND m.umo=? AND a.status='pending' LIMIT 1",
                (day, umo),
            ).fetchone()
        return row is not None

    def get_messages_for_day(
        self,
        day: str,
        umo: str,
        *,
        after_id: int = 0,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE local_day=? AND umo=? AND id>?"
                " ORDER BY id",
                (day, umo, int(after_id or 0)),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_user_text(self, umo: str, limit: int = 10) -> str:
        with self._lock:
            rows = self.conn.execute(
                "SELECT content FROM messages WHERE umo=? AND role='user'"
                " ORDER BY id DESC LIMIT ?",
                (umo, int(limit)),
            ).fetchall()
        return "\n".join(str(r["content"] or "") for r in rows)

    def max_message_id(self, day: str, umo: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT MAX(id) AS max_id FROM messages WHERE local_day=? AND umo=?",
                (day, umo),
            ).fetchone()
        return int(row["max_id"] or 0) if row else 0

    def get_message_by_uid(self, msg_uid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM messages WHERE msg_uid=?",
                (msg_uid,),
            ).fetchone()
        return dict(row) if row else None

    def get_messages_between_uids(
        self,
        umo: str,
        start_uid: str,
        end_uid: str,
    ) -> list[dict[str, Any]]:
        if not start_uid or not end_uid:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE umo=? AND id BETWEEN "
                "(SELECT id FROM messages WHERE msg_uid=? AND umo=?) AND "
                "(SELECT id FROM messages WHERE msg_uid=? AND umo=?) ORDER BY id",
                (umo, start_uid, umo, end_uid, umo),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- jobs -------------------------------------------------------------

    def job_start(self, day: str, umo: str, stage: str) -> None:
        now = to_iso(utc_now())
        with self._lock:
            self.conn.execute(
                "INSERT INTO jobs(day, umo, stage, status, attempts, started_at)"
                " VALUES(?,?,?,?,1,?)"
                " ON CONFLICT(day, umo, stage) DO UPDATE SET"
                " status='running', attempts=attempts+1, started_at=excluded.started_at,"
                " last_error=''",
                (day, umo, stage, "running", now),
            )
            self.conn.commit()

    def job_done(
        self,
        day: str,
        umo: str,
        stage: str,
        last_message_id: int = 0,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO jobs(day, umo, stage, status, finished_at, last_message_id)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(day, umo, stage) DO UPDATE SET"
                " status='done', finished_at=excluded.finished_at, last_error='',"
                " last_message_id=MAX(jobs.last_message_id, excluded.last_message_id)",
                (
                    day,
                    umo,
                    stage,
                    "done",
                    to_iso(utc_now()),
                    int(last_message_id or 0),
                ),
            )
            self.conn.commit()

    def job_last_message_id(self, day: str, umo: str, stage: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT last_message_id FROM jobs WHERE day=? AND umo=? AND stage=?",
                (day, umo, stage),
            ).fetchone()
        return int(row["last_message_id"] or 0) if row else 0

    def job_fail(self, day: str, umo: str, stage: str, error: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO jobs(day, umo, stage, status, last_error, finished_at)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(day, umo, stage) DO UPDATE SET"
                " status='failed', last_error=excluded.last_error,"
                " finished_at=excluded.finished_at",
                (day, umo, stage, "failed", str(error)[:2000], to_iso(utc_now())),
            )
            self.conn.commit()

    def job_is_done(self, day: str, umo: str, stage: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT status FROM jobs WHERE day=? AND umo=? AND stage=?",
                (day, umo, stage),
            ).fetchone()
        return bool(row and row["status"] == "done")

    def pending_pairs(self, before_day: str, limit: int = 50) -> list[tuple[str, str]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT m.local_day AS day, m.umo AS umo, MAX(m.id) AS max_id"
                " FROM messages m LEFT JOIN jobs j ON j.day=m.local_day"
                " AND j.umo=m.umo AND j.stage='daily'"
                " WHERE m.local_day < ?"
                " GROUP BY m.local_day, m.umo"
                " HAVING j.status IS NULL OR j.status!='done'"
                " OR MAX(m.id)>COALESCE(j.last_message_id, 0)"
                " ORDER BY m.local_day, m.umo LIMIT ?",
                (before_day, limit),
            ).fetchall()
        return [(r["day"], r["umo"]) for r in rows]

    def pairs_for_day(self, day: str) -> list[tuple[str, str]]:
        """指定日期有未处理消息的 (day, umo)。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT m.umo AS umo, MAX(m.id) AS max_id"
                " FROM messages m LEFT JOIN jobs j ON j.day=m.local_day"
                " AND j.umo=m.umo AND j.stage='daily'"
                " WHERE m.local_day=?"
                " GROUP BY m.umo"
                " HAVING j.status IS NULL OR j.status!='done'"
                " OR MAX(m.id)>COALESCE(j.last_message_id, 0)"
                " ORDER BY m.umo",
                (day,),
            ).fetchall()
        return [(day, r["umo"]) for r in rows]

    def has_pending_before(self, day: str) -> bool:
        return bool(self.pending_pairs(day, limit=1))

    # -- topics -----------------------------------------------------------

    def insert_topic(self, topic: dict[str, Any]) -> str:
        topic_id = str(topic.get("id") or uuid.uuid4())
        now = to_iso(utc_now())
        scope_id = str(topic.get("scope_id") or "")
        if not scope_id:
            scope_id = self.resolve_scope(str(topic.get("umo", "")))
        values = {
            "id": topic_id,
            "umo": topic.get("umo", ""),
            "scope_id": scope_id,
            "day": topic.get("day", ""),
            "title": topic.get("title", ""),
            "keywords": _json(topic.get("keywords", [])),
            "summary_search": topic.get("summary_search", ""),
            "summary_week": topic.get("summary_week", ""),
            "summary_month": topic.get("summary_month", ""),
            "summary_year": topic.get("summary_year", ""),
            "facts": _json(topic.get("facts", [])),
            "importance": int(topic.get("importance", 100)),
            "layer": topic.get("layer", "week"),
            "ttl_days": int(topic.get("ttl_days", 100)),
            "source_start_uid": topic.get("source_start_uid", ""),
            "source_end_uid": topic.get("source_end_uid", ""),
            "source_start_id": topic.get("source_start_id"),
            "source_end_id": topic.get("source_end_id"),
            "expire_at": topic.get("expire_at", now),
            "status": topic.get("status", "active"),
            "pinned": int(topic.get("pinned", 0)),
            "inject_until": topic.get("inject_until"),
            "search_until": topic.get("search_until"),
            "forgotten_at": topic.get("forgotten_at"),
            "summary_model": topic.get("summary_model", ""),
            "prompt_version": topic.get("prompt_version", ""),
            "created_at": topic.get("created_at", now),
            "updated_at": now,
        }
        columns = ", ".join(values.keys())
        marks = ", ".join("?" for _ in values)
        with self._lock:
            self.conn.execute(
                f"INSERT INTO topics({columns}) VALUES({marks})",
                tuple(values.values()),
            )
            self._fts_index_topic(topic_id)
            self.conn.commit()
        return topic_id

    # -- memory items -----------------------------------------------------

    def insert_item(self, item: dict[str, Any]) -> str:
        item_id = str(item.get("id") or uuid.uuid4().hex)
        now = to_iso(utc_now())
        with self._lock:
            self.conn.execute(
                "INSERT INTO memory_items"
                "(id, scope_id, topic_id, type, text, keywords, importance,"
                " due_at, done_at, status, inject_until, search_until,"
                " last_notified_at, last_injected_at, source_start_uid,"
                " source_end_uid, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    item.get("scope_id", ""),
                    item.get("topic_id", ""),
                    item.get("type", "episode"),
                    item.get("text", ""),
                    _json(item.get("keywords", [])),
                    int(item.get("importance", 100)),
                    item.get("due_at"),
                    item.get("done_at"),
                    item.get("status", "pending"),
                    item.get("inject_until"),
                    item.get("search_until"),
                    item.get("last_notified_at"),
                    item.get("last_injected_at"),
                    item.get("source_start_uid", ""),
                    item.get("source_end_uid", ""),
                    item.get("created_at", now),
                    now,
                ),
            )
            self.conn.commit()
        return item_id

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM memory_items WHERE id=?",
                (item_id,),
            ).fetchone()
        return _item_dict(row) if row else None

    def items_for_topic(self, topic_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM memory_items WHERE topic_id=? ORDER BY created_at",
                (topic_id,),
            ).fetchall()
        return [_item_dict(r) for r in rows]

    def list_items(
        self,
        scope_id: str,
        *,
        limit: int = 100,
        include_done: bool = True,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM memory_items WHERE scope_id=?"
        params: list[Any] = [scope_id]
        if not include_done:
            sql += " AND status='pending'"
        sql += " ORDER BY"
        sql += " CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at ASC, created_at DESC"
        sql += " LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [_item_dict(r) for r in rows]

    def due_items(
        self,
        scope_id: str,
        *,
        until_iso: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM memory_items WHERE scope_id=? AND status='pending'"
                " AND type IN ('reminder','task') AND due_at IS NOT NULL"
                " AND due_at<=? ORDER BY due_at ASC LIMIT ?",
                (scope_id, until_iso, int(limit)),
            ).fetchall()
        return [_item_dict(r) for r in rows]

    def update_item(self, item_id: str, **fields: Any) -> None:
        allowed = {
            "text", "keywords", "importance", "due_at", "done_at", "status",
            "inject_until", "search_until", "last_notified_at",
            "last_injected_at", "topic_id", "scope_id", "type",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if "keywords" in updates:
            updates["keywords"] = _json(updates["keywords"])
        updates["updated_at"] = to_iso(utc_now())
        assignments = ", ".join(f"{k}=?" for k in updates)
        with self._lock:
            self.conn.execute(
                f"UPDATE memory_items SET {assignments} WHERE id=?",
                tuple(updates.values()) + (item_id,),
            )
            self.conn.commit()

    def finish_item(self, item_id: str, *, search_until: str, now_iso: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE memory_items SET status='done', done_at=?, inject_until=?,"
                " search_until=?, updated_at=? WHERE id=? AND status='pending'",
                (now_iso, now_iso, search_until, now_iso, item_id),
            )
            self.conn.commit()
        return bool(cur.rowcount)

    def cancel_item(self, item_id: str, *, now_iso: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE memory_items SET status='cancelled', inject_until=?,"
                " updated_at=? WHERE id=? AND status IN ('pending','needs_clarification')",
                (now_iso, now_iso, item_id),
            )
            self.conn.commit()
        return bool(cur.rowcount)

    def cancel_all_items(self, scope_id: str, *, now_iso: str) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM memory_items WHERE scope_id=?"
                " AND status IN ('pending','needs_clarification')",
                (scope_id,),
            ).fetchall()
            ids = [str(r["id"]) for r in rows]
            if ids:
                self.conn.executemany(
                    "UPDATE memory_items SET status='cancelled', inject_until=?,"
                    " updated_at=? WHERE id=?",
                    [(now_iso, now_iso, i) for i in ids],
                )
                self.conn.commit()
        return ids

    def item_exists_for_message(self, scope_id: str, msg_uid: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM memory_items WHERE scope_id=? AND source_start_uid=?"
                " LIMIT 1",
                (scope_id, msg_uid),
            ).fetchone()
        return row is not None

    def item_exists_by_text_due(
        self,
        scope_id: str,
        text: str,
        due_at: str | None,
    ) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM memory_items WHERE scope_id=? AND text=?"
                " AND COALESCE(due_at,'')=COALESCE(?,'') LIMIT 1",
                (scope_id, text, due_at),
            ).fetchone()
        return row is not None

    def mark_item_injected(self, item_id: str, now_iso: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE memory_items SET last_injected_at=?, updated_at=? WHERE id=?",
                (now_iso, now_iso, item_id),
            )
            self.conn.commit()

    # -- reminder outbox --------------------------------------------------

    def enqueue_outbox(
        self,
        *,
        item_id: str,
        action: str,
        payload: dict[str, Any],
    ) -> str:
        event_id = uuid.uuid4().hex
        now = to_iso(utc_now())
        with self._lock:
            self.conn.execute(
                "INSERT INTO reminder_outbox"
                "(event_id, item_id, action, payload, status, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (event_id, item_id, action, _json(payload), "pending", now),
            )
            self.conn.commit()
        return event_id

    def list_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM reminder_outbox WHERE status='pending'"
                " ORDER BY created_at LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def ack_outbox(self, event_id: str, status: str = "acked") -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE reminder_outbox SET status=?, acked_at=?, sent_at=COALESCE(sent_at,?)"
                " WHERE event_id=?",
                (status, to_iso(utc_now()), to_iso(utc_now()), event_id),
            )
            self.conn.commit()

    def update_topic(self, topic_id: str, **fields: Any) -> None:
        allowed = {
            "title", "keywords", "summary_search", "summary_week", "summary_month",
            "summary_year", "facts", "importance", "layer", "ttl_days", "expire_at",
            "status", "pinned", "suppress_until", "last_hit_at", "hit_count",
            "renew_count", "summary_model", "prompt_version", "source_start_uid",
            "source_end_uid", "source_start_id", "source_end_id", "scope_id",
            "inject_until", "search_until", "forgotten_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if "keywords" in updates or "facts" in updates:
            if "keywords" in updates:
                updates["keywords"] = _json(updates["keywords"])
            if "facts" in updates:
                updates["facts"] = _json(updates["facts"])
        updates["updated_at"] = to_iso(utc_now())
        assignments = ", ".join(f"{k}=?" for k in updates)
        with self._lock:
            self.conn.execute(
                f"UPDATE topics SET {assignments} WHERE id=?",
                tuple(updates.values()) + (topic_id,),
            )
            self._fts_index_topic(topic_id)
            self.conn.commit()

    def get_topic(self, topic_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM topics WHERE id=?",
                (topic_id,),
            ).fetchone()
        return _topic_dict(row) if row else None

    def delete_topic(self, topic_id: str) -> None:
        now = to_iso(utc_now())
        with self._lock:
            self.conn.execute(
                "UPDATE topics SET status='deleted', forgotten_at=?, updated_at=?"
                " WHERE id=?",
                (now, now, topic_id),
            )
            self.conn.execute("DELETE FROM topic_fts WHERE topic_id=?", (topic_id,))
            self.conn.commit()

    def set_pinned(self, topic_id: str, pinned: bool) -> None:
        self.update_topic(topic_id, pinned=1 if pinned else 0)

    def set_suppress(self, topic_id: str, until_iso: str | None) -> None:
        self.update_topic(topic_id, suppress_until=until_iso)

    def topics_for_promotion(
        self,
        layer: str,
        cutoff_day: str,
        now_iso: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM topics WHERE layer=? AND status='active'"
                " AND day<=? AND expire_at>? ORDER BY day ASC, importance DESC LIMIT ?",
                (layer, cutoff_day, now_iso, limit),
            ).fetchall()
        return [_topic_dict(r) for r in rows]

    def expire_topics(self, now_iso: str) -> int:
        """只归档 search_until 已到的条目；expire_at 不再删除历史。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM topics WHERE status='active' AND pinned=0"
                " AND search_until IS NOT NULL AND search_until<>''"
                " AND search_until<?",
                (now_iso,),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self.conn.executemany(
                    "UPDATE topics SET status='expired', updated_at=? WHERE id=?",
                    [(now_iso, i) for i in ids],
                )
                # 归档仍可检索，索引保留。
                self.conn.commit()
        return len(ids)

    def purge_expired(self, cutoff_iso: str) -> int:
        """物理清理只针对用户显式遗忘的内容，不清理归档。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM topics WHERE status='deleted'"
                " AND forgotten_at IS NOT NULL AND forgotten_at<?",
                (cutoff_iso,),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self.conn.executemany(
                    "UPDATE topics SET status='deleted' WHERE id=?",
                    [(i,) for i in ids],
                )
                self.conn.executemany(
                    "DELETE FROM topic_fts WHERE topic_id=?",
                    [(i,) for i in ids],
                )
                self.conn.commit()
        return 0

    def renew_topic(
        self,
        topic_id: str,
        *,
        now_iso: str,
        factor: float,
        max_renewals: int,
        max_expire_iso: str,
    ) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM topics WHERE id=? AND status='active'",
                (topic_id,),
            ).fetchone()
            if not row:
                return False
            if int(row["renew_count"] or 0) >= int(max_renewals):
                return False
            expire = parse_iso(row["expire_at"])
            now = parse_iso(now_iso) or utc_now()
            if not expire:
                return False
            extension = max(
                1,
                int(round(int(row["ttl_days"] or 1) * (float(factor) - 1.0))),
            )
            new_expire = max(expire, now) + timedelta(days=extension)
            cap = parse_iso(max_expire_iso)
            if cap and new_expire > cap:
                new_expire = cap
            if new_expire <= expire:
                return False
            self.conn.execute(
                "UPDATE topics SET expire_at=?, renew_count=renew_count+1, updated_at=?"
                " WHERE id=?",
                (to_iso(new_expire), now_iso, topic_id),
            )
            self.conn.commit()
            return True

    # -- 检索 -------------------------------------------------------------

    def search_fts(
        self,
        umo: str,
        terms: Sequence[str],
        *,
        limit: int = 50,
        day_from: str | None = None,
        day_to: str | None = None,
        scope_id: str | None = None,
    ) -> list[dict[str, Any]]:
        scope_id = scope_id or self.resolve_scope(umo)
        terms = [t for t in terms if t and len(t) >= 2][:10]
        if not terms or not self.fts_enabled:
            return self.search_like(
                umo, terms, limit=limit, day_from=day_from, day_to=day_to,
                scope_id=scope_id,
            )
        query = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        sql = (
            "SELECT t.*, bm25(topic_fts) AS rank FROM topic_fts "
            "JOIN topics t ON t.id=topic_fts.topic_id "
            "WHERE topic_fts MATCH ? AND t.status IN ('active','expired')"
            " AND (t.scope_id=? OR (t.scope_id='' AND t.umo=?))"
        )
        params: list[Any] = [query, scope_id, umo]
        if day_from:
            sql += " AND t.day>=?"
            params.append(day_from)
        if day_to:
            sql += " AND t.day<=?"
            params.append(day_to)
        sql += " ORDER BY rank LIMIT ?"
        params.append(int(limit))
        try:
            with self._lock:
                rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            return self.search_like(
                umo, terms, limit=limit, day_from=day_from, day_to=day_to,
                scope_id=scope_id,
            )
        if not rows:
            return self.search_like(
                umo, terms, limit=limit, day_from=day_from, day_to=day_to,
                scope_id=scope_id,
            )
        return [_topic_dict(r) for r in rows]

    def search_like(
        self,
        umo: str,
        terms: Sequence[str],
        *,
        limit: int = 50,
        day_from: str | None = None,
        day_to: str | None = None,
        scope_id: str | None = None,
    ) -> list[dict[str, Any]]:
        scope_id = scope_id or self.resolve_scope(umo)
        terms = [t for t in terms if t][:4]
        if not terms:
            return []
        where = []
        params: list[Any] = [scope_id, umo]
        for term in terms:
            like = f"%{term}%"
            where.append(
                "(title LIKE ? OR keywords LIKE ? OR summary_search LIKE ?"
                " OR summary_week LIKE ? OR summary_month LIKE ? OR summary_year LIKE ?)",
            )
            params.extend([like] * 6)
        sql = (
            "SELECT * FROM topics WHERE status IN ('active','expired')"
            " AND (scope_id=? OR (scope_id='' AND umo=?)) AND ("
            + " OR ".join(where)
            + ")"
        )
        if day_from:
            sql += " AND day>=?"
            params.append(day_from)
        if day_to:
            sql += " AND day<=?"
            params.append(day_to)
        sql += " ORDER BY importance DESC, day DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [_topic_dict(r) for r in rows]

    def list_topics_by_day_range(
        self,
        umo: str,
        day_from: str,
        day_to: str,
        limit: int = 20,
        scope_id: str | None = None,
    ) -> list[dict[str, Any]]:
        scope_id = scope_id or self.resolve_scope(umo)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM topics WHERE status IN ('active','expired')"
                " AND (scope_id=? OR (scope_id='' AND umo=?))"
                " AND day BETWEEN ? AND ? ORDER BY importance DESC, day DESC LIMIT ?",
                (scope_id, umo, day_from, day_to, limit),
            ).fetchall()
        return [_topic_dict(r) for r in rows]

    # -- FTS --------------------------------------------------------------

    def _fts_index_topic(self, topic_id: str) -> None:
        if not self.fts_enabled:
            return
        row = self.conn.execute(
            "SELECT * FROM topics WHERE id=?",
            (topic_id,),
        ).fetchone()
        self.conn.execute("DELETE FROM topic_fts WHERE topic_id=?", (topic_id,))
        if not row or row["status"] not in {"active", "expired"}:
            return
        keywords = _loads(row["keywords"], [])
        facts = _loads(row["facts"], [])
        summary = " ".join(
            filter(
                None,
                [
                    row["summary_search"],
                    row["summary_week"],
                    row["summary_month"],
                    row["summary_year"],
                    " ".join(map(str, facts)),
                ],
            ),
        )
        tokens = tokens_text(
            " ".join([row["title"], " ".join(map(str, keywords)), summary]),
        )
        self.conn.execute(
            "INSERT INTO topic_fts(topic_id, title, keywords, summary, tokens)"
            " VALUES(?,?,?,?,?)",
            (topic_id, row["title"], " ".join(map(str, keywords)), summary, tokens),
        )

    def rebuild_fts(self) -> int:
        with self._lock:
            self.conn.execute("DELETE FROM topic_fts")
            rows = self.conn.execute(
                "SELECT id FROM topics WHERE status IN ('active','expired')",
            ).fetchall()
            for row in rows:
                self._fts_index_topic(row["id"])
            self.conn.commit()
        return len(rows)

    # -- 统计与导出辅助 ----------------------------------------------------

    def delete_day(self, day: str, umo: str) -> dict[str, int]:
        """重建专用：删除某天某会话的 topics/items/outbox/job。"""
        with self._lock:
            topic_rows = self.conn.execute(
                "SELECT id FROM topics WHERE day=? AND umo=?",
                (day, umo),
            ).fetchall()
            topic_ids = [r["id"] for r in topic_rows]
            item_rows = self.conn.execute(
                "SELECT id FROM memory_items WHERE topic_id IN ("
                " SELECT id FROM topics WHERE day=? AND umo=?"
                ")",
                (day, umo),
            ).fetchall()
            item_ids = [r["id"] for r in item_rows]
            if item_ids:
                self.conn.executemany(
                    "DELETE FROM reminder_outbox WHERE item_id=?",
                    [(i,) for i in item_ids],
                )
                self.conn.executemany(
                    "DELETE FROM memory_items WHERE id=?",
                    [(i,) for i in item_ids],
                )
            if topic_ids:
                self.conn.executemany(
                    "DELETE FROM topic_fts WHERE topic_id=?",
                    [(i,) for i in topic_ids],
                )
                self.conn.executemany(
                    "DELETE FROM topics WHERE id=?",
                    [(i,) for i in topic_ids],
                )
            self.conn.execute(
                "DELETE FROM jobs WHERE day=? AND umo=? AND stage='daily'",
                (day, umo),
            )
            self.conn.commit()
        return {
            "topics": len(topic_ids),
            "items": len(item_ids),
        }

    def counts(self) -> dict[str, Any]:
        with self._lock:
            topics = self.conn.execute(
                "SELECT layer, status, count(*) AS n FROM topics GROUP BY layer, status",
            ).fetchall()
            messages = self.conn.execute(
                "SELECT count(*) AS n FROM messages",
            ).fetchone()
            attachments = self.conn.execute(
                "SELECT count(*) AS n FROM message_attachments",
            ).fetchone()
            jobs = self.conn.execute(
                "SELECT status, count(*) AS n FROM jobs GROUP BY status",
            ).fetchall()
            scopes = self.conn.execute(
                "SELECT count(*) AS n FROM scopes",
            ).fetchone()
            bindings = self.conn.execute(
                "SELECT count(*) AS n FROM session_bindings",
            ).fetchone()
            items = self.conn.execute(
                "SELECT count(*) AS n FROM memory_items",
            ).fetchone()
        return {
            "topics": [dict(r) for r in topics],
            "messages": int(messages["n"]),
            "attachments": int(attachments["n"]),
            "items": int(items["n"]),
            "jobs": [dict(r) for r in jobs],
            "scopes": int(scopes["n"]),
            "bindings": int(bindings["n"]),
            "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "schema_version": SCHEMA_VERSION,
        }

    # -- 导入 -------------------------------------------------------------

    def import_scopes(self, rows: Iterable[dict[str, Any]]) -> dict[str, str]:
        """导入 scopes，返回 源scope_id → 本地scope_id 的映射。

        若本地已有相同 (platform, kind, external_id)，复用本地 scope_id；
        否则保留源 scope_id（没有就新建）。
        """
        mapping: dict[str, str] = {}
        now = to_iso(utc_now())
        with self._lock, self.conn:
            for row in rows:
                source = str(row.get("scope_id") or "")
                platform = str(row.get("platform") or "unknown")
                kind = normalize_kind(str(row.get("kind") or "private"))
                external_id = str(row.get("external_id") or "unknown")
                label = str(row.get("label") or "")
                person_id = row.get("person_id")

                existing = None
                if source:
                    existing = self.conn.execute(
                        "SELECT scope_id FROM scopes WHERE scope_id=?",
                        (source,),
                    ).fetchone()
                if not existing:
                    existing = self.conn.execute(
                        "SELECT scope_id FROM scopes"
                        " WHERE platform=? AND kind=? AND external_id=?",
                        (platform, kind, external_id),
                    ).fetchone()
                if existing:
                    target = str(existing["scope_id"])
                else:
                    target = source or uuid.uuid4().hex
                    self.conn.execute(
                        "INSERT OR IGNORE INTO scopes"
                        "(scope_id, platform, kind, external_id, label, person_id,"
                        " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            target,
                            platform,
                            kind,
                            external_id,
                            label,
                            person_id,
                            row.get("created_at", now),
                            now,
                        ),
                    )
                    target_row = self.conn.execute(
                        "SELECT scope_id FROM scopes"
                        " WHERE platform=? AND kind=? AND external_id=?",
                        (platform, kind, external_id),
                    ).fetchone()
                    if target_row:
                        target = str(target_row["scope_id"])
                if source:
                    mapping[source] = target
        return mapping

    def import_bindings(
        self,
        rows: Iterable[dict[str, Any]],
        scope_map: dict[str, str] | None = None,
    ) -> int:
        """导入会话绑定；默认不覆盖本地已有绑定。"""
        scope_map = scope_map or {}
        count = 0
        with self._lock, self.conn:
            for row in rows:
                scope_id = str(row.get("scope_id") or "")
                scope_id = scope_map.get(scope_id, scope_id)
                if not scope_id or not self.get_scope(scope_id):
                    continue
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO session_bindings"
                    "(umo, scope_id, bound_at, bound_by) VALUES(?,?,?,?)",
                    (
                        row.get("umo", ""),
                        scope_id,
                        row.get("bound_at", to_iso(utc_now())),
                        row.get("bound_by", ""),
                    ),
                )
                count += cur.rowcount
        return count

    def import_scope_aliases(
        self,
        rows: Iterable[dict[str, Any]],
        scope_map: dict[str, str] | None = None,
    ) -> int:
        """导入群聊 scope 映射；默认不覆盖本地已有 alias。"""
        scope_map = scope_map or {}
        count = 0
        with self._lock, self.conn:
            for row in rows:
                alias_key = str(row.get("alias_key") or "")
                scope_id = str(row.get("scope_id") or "")
                scope_id = scope_map.get(scope_id, scope_id)
                if not alias_key or not scope_id or not self.get_scope(scope_id):
                    continue
                now = to_iso(utc_now())
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO scope_aliases"
                    "(alias_key, scope_id, note, created_at, updated_at)"
                    " VALUES(?,?,?,?,?)",
                    (
                        alias_key,
                        scope_id,
                        row.get("note", ""),
                        row.get("created_at", now),
                        row.get("updated_at", now),
                    ),
                )
                count += cur.rowcount
        return count

    def import_messages(
        self,
        rows: Iterable[dict[str, Any]],
        scope_map: dict[str, str] | None = None,
    ) -> int:
        scope_map = scope_map or {}
        count = 0
        with self._lock, self.conn:
            for row in rows:
                scope_id = str(row.get("scope_id") or "")
                scope_id = scope_map.get(scope_id, scope_id)
                if scope_id and not self.get_scope(scope_id):
                    scope_id = ""
                if not scope_id:
                    scope_id = self.resolve_scope(str(row.get("umo", "")))
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO messages"
                    "(msg_uid, umo, scope_id, session_id, platform, role, sender_id,"
                    " sender_name, content, created_at, local_day)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row.get("msg_uid", ""),
                        row.get("umo", ""),
                        scope_id,
                        row.get("session_id", ""),
                        row.get("platform", ""),
                        row.get("role", "user"),
                        row.get("sender_id", ""),
                        row.get("sender_name", ""),
                        row.get("content", ""),
                        row.get("created_at", to_iso(utc_now())),
                        row.get("local_day", ""),
                    ),
                )
                count += cur.rowcount
        return count

    def import_attachments(self, rows: Iterable[dict[str, Any]]) -> int:
        count = 0
        with self._lock, self.conn:
            for row in rows:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO message_attachments"
                    "(id, message_uid, kind, platform_ref, mime, bytes, sha256,"
                    " local_path, duration_ms, transcript, caption, display_name,"
                    " status, model, error, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row.get("id", ""),
                        row.get("message_uid", ""),
                        row.get("kind", "file"),
                        row.get("platform_ref", ""),
                        row.get("mime", ""),
                        int(row.get("bytes", 0) or 0),
                        row.get("sha256", ""),
                        row.get("local_path", ""),
                        int(row.get("duration_ms", 0) or 0),
                        row.get("transcript", ""),
                        row.get("caption", ""),
                        row.get("display_name", ""),
                        row.get("status", "pending"),
                        row.get("model", ""),
                        row.get("error", ""),
                        row.get("created_at", to_iso(utc_now())),
                    ),
                )
                count += cur.rowcount
        return count

    def import_topics(
        self,
        rows: Iterable[dict[str, Any]],
        force: bool = False,
        scope_map: dict[str, str] | None = None,
    ) -> int:
        scope_map = scope_map or {}
        count = 0
        verb = "INSERT OR REPLACE" if force else "INSERT OR IGNORE"
        with self._lock, self.conn:
            for row in rows:
                start_id = self._resolve_uid(row.get("source_start_uid", ""))
                end_id = self._resolve_uid(row.get("source_end_uid", ""))
                scope_id = str(row.get("scope_id") or "")
                scope_id = scope_map.get(scope_id, scope_id)
                if scope_id and not self.get_scope(scope_id):
                    scope_id = ""
                if not scope_id:
                    scope_id = self.resolve_scope(str(row.get("umo", "")))
                cur = self.conn.execute(
                    f"{verb} INTO topics"
                    "(id, umo, scope_id, day, title, keywords, summary_search,"
                    " summary_week, summary_month, summary_year, facts, importance,"
                    " layer, ttl_days, source_start_uid, source_end_uid, source_start_id,"
                    " source_end_id, expire_at, last_hit_at, hit_count, renew_count,"
                    " status, pinned, suppress_until, summary_model, prompt_version,"
                    " created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row.get("id", ""),
                        row.get("umo", ""),
                        scope_id,
                        row.get("day", ""),
                        row.get("title", ""),
                        _json(row.get("keywords", [])),
                        row.get("summary_search", ""),
                        row.get("summary_week", ""),
                        row.get("summary_month", ""),
                        row.get("summary_year", ""),
                        _json(row.get("facts", [])),
                        int(row.get("importance", 100) or 100),
                        row.get("layer", "week"),
                        int(row.get("ttl_days", 100) or 100),
                        row.get("source_start_uid", ""),
                        row.get("source_end_uid", ""),
                        start_id,
                        end_id,
                        row.get("expire_at", to_iso(utc_now())),
                        row.get("last_hit_at"),
                        int(row.get("hit_count", 0) or 0),
                        int(row.get("renew_count", 0) or 0),
                        row.get("status", "active"),
                        int(row.get("pinned", 0) or 0),
                        row.get("suppress_until"),
                        row.get("summary_model", ""),
                        row.get("prompt_version", ""),
                        row.get("created_at", to_iso(utc_now())),
                        to_iso(utc_now()),
                    ),
                )
                count += cur.rowcount
        return count

    def import_items(
        self,
        rows: Iterable[dict[str, Any]],
        scope_map: dict[str, str] | None = None,
    ) -> int:
        scope_map = scope_map or {}
        count = 0
        with self._lock, self.conn:
            for row in rows:
                scope_id = str(row.get("scope_id") or "")
                scope_id = scope_map.get(scope_id, scope_id)
                if scope_id and not self.get_scope(scope_id):
                    scope_id = ""
                if not scope_id:
                    scope_id = self.resolve_scope(str(row.get("umo", "")))
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO memory_items"
                    "(id, scope_id, topic_id, type, text, keywords, importance,"
                    " due_at, done_at, status, inject_until, search_until,"
                    " last_notified_at, last_injected_at, source_start_uid,"
                    " source_end_uid, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row.get("id", ""),
                        scope_id,
                        row.get("topic_id", ""),
                        row.get("type", "episode"),
                        row.get("text", ""),
                        _json(row.get("keywords", [])),
                        int(row.get("importance", 100) or 100),
                        row.get("due_at"),
                        row.get("done_at"),
                        row.get("status", "pending"),
                        row.get("inject_until"),
                        row.get("search_until"),
                        row.get("last_notified_at"),
                        row.get("last_injected_at"),
                        row.get("source_start_uid", ""),
                        row.get("source_end_uid", ""),
                        row.get("created_at", to_iso(utc_now())),
                        to_iso(utc_now()),
                    ),
                )
                count += cur.rowcount
        return count

    def import_jobs(self, rows: Iterable[dict[str, Any]]) -> int:
        count = 0
        with self._lock, self.conn:
            for row in rows:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO jobs"
                    "(day, umo, stage, status, attempts, last_error, started_at, finished_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        row.get("day", ""),
                        row.get("umo", ""),
                        row.get("stage", ""),
                        row.get("status", "pending"),
                        int(row.get("attempts", 0) or 0),
                        row.get("last_error", ""),
                        row.get("started_at"),
                        row.get("finished_at"),
                    ),
                )
                count += cur.rowcount
        return count

    def _resolve_uid(self, msg_uid: str) -> int | None:
        if not msg_uid:
            return None
        row = self.conn.execute(
            "SELECT id FROM messages WHERE msg_uid=?",
            (msg_uid,),
        ).fetchone()
        return int(row["id"]) if row else None


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _json(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _topic_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["keywords"] = _loads(data.get("keywords"), [])
    data["facts"] = _loads(data.get("facts"), [])
    return data


def _item_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["keywords"] = _loads(data.get("keywords"), [])
    return data
