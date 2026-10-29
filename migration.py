"""迁移包：分片导出 / 流式导入 / Markdown 出口。

为什么不是单个 messages.jsonl：
- 单文件到 GB 级虽然能流式读，但随机取某天、断点续传、单点损坏都很麻烦；
- 这里按行数分片，manifest 记录每片的行数、日期范围、sha256；
- 导入按片流式处理，内存不随包增长，也支持只取某段日期。

v0.1 的媒体只留位置（attachments.jsonl + media/），v0.2 填内容。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    from .textutil import slugify
except ImportError:  # 离线测试
    from textutil import slugify

BUNDLE_FORMAT = "astrbot-memory-bundle"
PLUGIN_VERSION = "1.0.5"
SCHEMA_VERSION = 3


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_jsonl_gz(path: Path, rows: Iterator[dict[str, Any]]) -> int:
    count = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            count += 1
    return count


def _iter_jsonl_gz(path: str | Path) -> Iterator[dict[str, Any]]:
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _iter_zip_jsonl(zf: zipfile.ZipFile, member: str) -> Iterator[dict[str, Any]]:
    with zf.open(member) as raw:
        if member.endswith(".gz"):
            with gzip.open(raw, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        else:
            for line in raw:
                line = line.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def _table_rows(
    conn: sqlite3.Connection,
    table: str,
    order_by: str,
) -> Iterator[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
    for row in cur:
        yield dict(row)


def _write_shards(
    conn: sqlite3.Connection,
    table: str,
    out_dir: Path,
    rows_per: int,
    order_by: str,
    day_key: str | None,
) -> list[dict[str, Any]]:
    shards: list[dict[str, Any]] = []
    part = 0
    buffer: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal part, buffer
        part += 1
        name = f"part-{part:04d}.jsonl.gz"
        path = out_dir / name
        count = _write_jsonl_gz(path, iter(buffer))
        days = [str(r.get(day_key)) for r in buffer if day_key and r.get(day_key)]
        shards.append(
            {
                "file": f"{out_dir.name}/{name}",
                "rows": count,
                "first_day": min(days) if days else "",
                "last_day": max(days) if days else "",
                "sha256": _sha256_file(path),
                "bytes": path.stat().st_size,
            },
        )
        buffer = []

    for row in _table_rows(conn, table, order_by):
        buffer.append(row)
        if len(buffer) >= rows_per:
            flush()
    if buffer:
        flush()
    return shards


def _write_markdown(conn: sqlite3.Connection, root: Path) -> int:
    root.mkdir(parents=True, exist_ok=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM topics WHERE status='active' ORDER BY day, importance DESC",
    ).fetchall()
    count = 0
    for row in rows:
        topic = dict(row)
        try:
            keywords = json.loads(topic.get("keywords") or "[]")
        except json.JSONDecodeError:
            keywords = []
        try:
            facts = json.loads(topic.get("facts") or "[]")
        except json.JSONDecodeError:
            facts = []
        day = str(topic.get("day") or "1970-01-01")
        year, month, _ = (day.split("-") + ["01", "01"])[:3]
        summary = (
            topic.get("summary_month")
            or topic.get("summary_week")
            or topic.get("summary_search")
            or ""
        )
        filename = f"{day}-{slugify(topic.get('title', 'topic'))}-{topic.get('id', '')[:8]}.md"
        path = root / year / month / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        body = [
            "---",
            f"id: {topic.get('id')}",
            f"date: {day}",
            f"layer: {topic.get('layer')}",
            f"importance: {topic.get('importance')}",
            f"keywords: {json.dumps(keywords, ensure_ascii=False)}",
            f"status: {topic.get('status')}",
            f"expire_at: {topic.get('expire_at')}",
            f"source_start: {topic.get('source_start_uid')}",
            f"source_end: {topic.get('source_end_uid')}",
            "---",
            "",
            f"# {topic.get('title')}",
            "",
            "## 摘要",
            "",
            str(summary).strip(),
            "",
            "## 事实",
            "",
        ]
        if facts:
            body.extend(f"- {f}" for f in facts)
        else:
            body.append("（无）")
        body.extend(["", "## 关键词", "", "、".join(map(str, keywords)) or "（无）", ""])
        path.write_text("\n".join(body), encoding="utf-8")
        count += 1
    return count


def export_bundle(
    db,
    out_dir: str | Path,
    cfg: dict[str, Any],
    *,
    include_media: bool = True,
    markdown: bool = False,
    now: datetime | None = None,
) -> Path:
    """导出 zip，返回绝对路径。调用方应放在线程里跑，避免阻塞事件循环。"""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_per = max(1000, int(cfg.get("export_shard_rows", 50000) or 50000))

    # 不用 tempfile.mkdtemp：某些 Windows 沙箱下它创建的目录不可写。
    temp_dir = out_dir / f".tmp_export_{uuid.uuid4().hex[:8]}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    snapshot = temp_dir / "snapshot.db"
    db.backup_to(snapshot)
    try:
        conn = sqlite3.connect(str(snapshot))
        conn.row_factory = sqlite3.Row
        shards: dict[str, list[dict[str, Any]]] = {}
        shards["scopes"] = _write_shards(
            conn, "scopes", temp_dir / "scopes", max(1000, rows_per // 5),
            "scope_id", None,
        )
        shards["messages"] = _write_shards(
            conn, "messages", temp_dir / "messages", rows_per, "id", "local_day",
        )
        shards["topics"] = _write_shards(
            conn, "topics", temp_dir / "topics", max(1000, rows_per // 5), "day, id", "day",
        )
        shards["items"] = _write_shards(
            conn, "memory_items", temp_dir / "items",
            max(1000, rows_per // 5), "created_at, id", None,
        )
        shards["attachments"] = _write_shards(
            conn, "message_attachments", temp_dir / "attachments",
            max(1000, rows_per // 5), "id", None,
        )
        shards["jobs"] = _write_shards(
            conn, "jobs", temp_dir / "jobs", rows_per, "day, umo, stage", "day",
        )
        shards["bindings"] = _write_shards(
            conn, "session_bindings", temp_dir / "bindings",
            max(1000, rows_per // 5), "umo", None,
        )
        shards["aliases"] = _write_shards(
            conn, "scope_aliases", temp_dir / "aliases",
            max(1000, rows_per // 5), "alias_key", None,
        )

        counts = {
            "scopes": conn.execute("SELECT count(*) FROM scopes").fetchone()[0],
            "bindings": conn.execute(
                "SELECT count(*) FROM session_bindings",
            ).fetchone()[0],
            "aliases": conn.execute("SELECT count(*) FROM scope_aliases").fetchone()[0],
            "messages": conn.execute("SELECT count(*) FROM messages").fetchone()[0],
            "topics": conn.execute("SELECT count(*) FROM topics").fetchone()[0],
            "items": conn.execute("SELECT count(*) FROM memory_items").fetchone()[0],
            "attachments": conn.execute(
                "SELECT count(*) FROM message_attachments",
            ).fetchone()[0],
            "jobs": conn.execute("SELECT count(*) FROM jobs").fetchone()[0],
        }
        markdown_count = _write_markdown(conn, temp_dir / "markdown") if markdown else 0

        media_files: list[dict[str, Any]] = []
        media_raw = str(cfg.get("media_dir") or "").strip()
        media_dir = Path(media_raw) if media_raw else None
        if media_dir is not None and not media_dir.is_absolute():
            media_dir = out_dir.parent / media_dir
        if (
            include_media
            and media_dir is not None
            and media_dir.exists()
            and media_dir.is_dir()
        ):
            target = temp_dir / "media"
            shutil.copytree(media_dir, target, dirs_exist_ok=True)
            for path in target.rglob("*"):
                if path.is_file():
                    media_files.append(
                        {
                            "path": str(path.relative_to(temp_dir)).replace("\\", "/"),
                            "bytes": path.stat().st_size,
                            "sha256": _sha256_file(path),
                        },
                    )

        manifest = {
            "format": BUNDLE_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "plugin_version": PLUGIN_VERSION,
            "exported_at": now.isoformat(timespec="seconds"),
            "timezone": str(cfg.get("timezone", "")),
            "counts": counts,
            "markdown_topics": markdown_count,
            "shards": shards,
            "media": media_files,
        }
        (temp_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        (temp_dir / "meta.json").write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "plugin_version": PLUGIN_VERSION,
                    "exported_at": manifest["exported_at"],
                    "counts": counts,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (temp_dir / "README.txt").write_text(
            "BBR的记忆插件 v0.1 迁移包\n"
            "导入：/mem import <本 zip 的路径>\n"
            "导入会按 msg_uid / topic id 去重合并，并重建检索索引。\n"
            "如果 schema_version 高于当前插件，请先升级插件。\n",
            encoding="utf-8",
        )

        out = out_dir / f"memory_export_{now.strftime('%Y%m%d_%H%M%S')}.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(temp_dir.rglob("*")):
                if path.is_file() and path.name != "snapshot.db":
                    zf.write(path, path.relative_to(temp_dir).as_posix())
        conn.close()
        return out
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def import_bundle(
    db,
    zip_path: str | Path,
    *,
    force: bool = False,
    max_mb: int = 2048,
    media_dir: str | Path | None = None,
) -> dict[str, Any]:
    """流式导入，返回统计。调用方应放在线程里跑。"""
    zip_path = Path(zip_path)
    if not zip_path.exists() or not zip_path.is_file():
        raise FileNotFoundError(f"迁移包不存在: {zip_path}")
    if zip_path.suffix.lower() != ".zip":
        raise ValueError("迁移包必须是 .zip")

    stats = {
        "scopes": 0,
        "bindings": 0,
        "aliases": 0,
        "messages": 0,
        "topics": 0,
        "items": 0,
        "attachments": 0,
        "jobs": 0,
        "skipped": 0,
        "media": 0,
        "bytes": zip_path.stat().st_size,
    }
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        if "manifest.json" not in names:
            raise ValueError("迁移包缺少 manifest.json")
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        if manifest.get("format") != BUNDLE_FORMAT:
            raise ValueError("不是本插件的迁移包")
        if int(manifest.get("schema_version", 0)) > SCHEMA_VERSION:
            raise ValueError("迁移包 schema 版本高于当前插件，请先升级插件")
        total = sum(info.file_size for info in zf.infolist())
        if total > max_mb * 1024 * 1024:
            raise ValueError(f"迁移包解压后过大（>{max_mb}MB）")

        shards = manifest.get("shards") or {}

        def members(key: str, fallback: str) -> list[str]:
            entries = shards.get(key) or []
            if entries:
                return [str(e["file"]).replace("\\", "/") for e in entries]
            for name in (fallback + ".jsonl.gz", fallback + ".jsonl"):
                if name in names:
                    return [name]
            return []

        scope_rows: list[dict[str, Any]] = []
        for member in members("scopes", "scopes"):
            if member not in names:
                continue
            for row in _iter_zip_jsonl(zf, member):
                scope_rows.append(row)
        scope_map = db.import_scopes(scope_rows) if scope_rows else {}
        stats["scopes"] = len(scope_rows)

        batch: list[dict[str, Any]] = []
        for member in members("messages", "messages"):
            if member not in names:
                continue
            for row in _iter_zip_jsonl(zf, member):
                batch.append(row)
                if len(batch) >= 500:
                    stats["messages"] += db.import_messages(
                        batch, scope_map=scope_map,
                    )
                    batch = []
        if batch:
            stats["messages"] += db.import_messages(batch, scope_map=scope_map)

        batch = []
        for member in members("attachments", "attachments"):
            if member not in names:
                continue
            for row in _iter_zip_jsonl(zf, member):
                batch.append(row)
        if batch:
            stats["attachments"] += db.import_attachments(batch)

        batch = []
        for member in members("topics", "topics"):
            if member not in names:
                continue
            for row in _iter_zip_jsonl(zf, member):
                batch.append(row)
                if len(batch) >= 300:
                    stats["topics"] += db.import_topics(
                        batch, force=force, scope_map=scope_map,
                    )
                    batch = []
        if batch:
            stats["topics"] += db.import_topics(
                batch, force=force, scope_map=scope_map,
            )

        batch = []
        for member in members("items", "items"):
            if member not in names:
                continue
            for row in _iter_zip_jsonl(zf, member):
                batch.append(row)
                if len(batch) >= 300:
                    stats["items"] += db.import_items(batch, scope_map=scope_map)
                    batch = []
        if batch:
            stats["items"] += db.import_items(batch, scope_map=scope_map)

        batch = []
        for member in members("jobs", "jobs"):
            if member not in names:
                continue
            for row in _iter_zip_jsonl(zf, member):
                batch.append(row)
        if batch:
            stats["jobs"] += db.import_jobs(batch)

        batch = []
        for member in members("bindings", "bindings"):
            if member not in names:
                continue
            for row in _iter_zip_jsonl(zf, member):
                batch.append(row)
        if batch:
            stats["bindings"] += db.import_bindings(batch, scope_map=scope_map)

        batch = []
        for member in members("aliases", "aliases"):
            if member not in names:
                continue
            for row in _iter_zip_jsonl(zf, member):
                batch.append(row)
        if batch:
            stats["aliases"] += db.import_scope_aliases(batch, scope_map=scope_map)

        if media_dir is not None:
            target_root = Path(media_dir).resolve()
            target_root.mkdir(parents=True, exist_ok=True)
            for name in names:
                if not name.startswith("media/") or name.endswith("/"):
                    continue
                rel = Path(name[len("media/") :])
                if not rel.parts or ".." in rel.parts:
                    continue
                target = (target_root / rel).resolve()
                if target_root not in target.parents and target != target_root:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                stats["media"] += 1

        db.rebuild_fts()
    return stats
