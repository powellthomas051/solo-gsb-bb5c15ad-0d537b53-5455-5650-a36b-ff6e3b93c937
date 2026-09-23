"""SQLite 持久层：建表在服务启动时执行，合法设计与审计结果按 ID 存取。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join("data", "weave.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS designs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    cyclic      INTEGER NOT NULL,
    max_float   INTEGER NOT NULL,
    shafts      INTEGER NOT NULL,
    treadles    INTEGER NOT NULL,
    warp_ends   INTEGER NOT NULL,
    weft_picks  INTEGER NOT NULL,
    threading   TEXT NOT NULL,   -- JSON
    tie_up      TEXT NOT NULL,   -- JSON
    treadling   TEXT NOT NULL,   -- JSON
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS audits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    design_id    INTEGER NOT NULL UNIQUE REFERENCES designs (id) ON DELETE CASCADE,
    exceeds      INTEGER NOT NULL,
    interlacing  TEXT NOT NULL,  -- JSON
    violations   TEXT NOT NULL,  -- JSON
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS design_tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    design_id   INTEGER NOT NULL REFERENCES designs (id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (design_id, tag)
);

CREATE TABLE IF NOT EXISTS design_archives (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    digest      TEXT NOT NULL UNIQUE,   -- 规范化清单的 SHA-256 hex
    design_ids  TEXT NOT NULL,          -- 升序设计 ID 列表 JSON
    item_count  INTEGER NOT NULL,
    manifest    TEXT NOT NULL,          -- 规范化清单 JSON 文本（即 digest 输入）
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""

MAX_TAG_LENGTH = 32


class DesignNotFoundError(LookupError):
    """写入 / 查询标签时设计不存在。"""


class TagError(ValueError):
    """标签非空或长度等校验失败。"""


class DuplicateTagError(ValueError):
    """同一设计下标签已存在。"""


class ArchiveNotFoundError(LookupError):
    """核验 / 查询归档时归档 ID 不存在。"""


_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """启动时建表（幂等）。"""
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with _lock, _connect() as conn:
        conn.executescript(_SCHEMA)


def insert_design_with_audit(
    *,
    name: str | None,
    cyclic: bool,
    max_float: int,
    shafts: int,
    treadles: int,
    threading_map: list[int],
    tie_up: list[list[int]],
    treadling: list[int],
    interlacing: list[list[int]],
    violations: list[dict],
) -> int:
    """同事务写入设计与审计结果，返回设计 ID。"""
    with _lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO designs
               (name, cyclic, max_float, shafts, treadles, warp_ends, weft_picks,
                threading, tie_up, treadling)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name,
                int(cyclic),
                max_float,
                shafts,
                treadles,
                len(threading_map),
                len(treadling),
                json.dumps(threading_map),
                json.dumps(tie_up),
                json.dumps(treadling),
            ),
        )
        design_id = cur.lastrowid
        conn.execute(
            """INSERT INTO audits (design_id, exceeds, interlacing, violations)
               VALUES (?, ?, ?, ?)""",
            (
                design_id,
                int(bool(violations)),
                json.dumps(interlacing),
                json.dumps(violations),
            ),
        )
        return design_id


def _row_to_detail(design: sqlite3.Row, audit: sqlite3.Row) -> dict:
    return {
        "id": design["id"],
        "name": design["name"],
        "cyclic": bool(design["cyclic"]),
        "max_float": design["max_float"],
        "shafts": design["shafts"],
        "treadles": design["treadles"],
        "warp_ends": design["warp_ends"],
        "weft_picks": design["weft_picks"],
        "threading": json.loads(design["threading"]),
        "tie_up": json.loads(design["tie_up"]),
        "treadling": json.loads(design["treadling"]),
        "created_at": design["created_at"],
        "audit": {
            "exceeds": bool(audit["exceeds"]),
            "interlacing": json.loads(audit["interlacing"]),
            "violations": json.loads(audit["violations"]),
        },
    }


def get_design(design_id: int) -> dict | None:
    with _lock, _connect() as conn:
        design = conn.execute(
            "SELECT * FROM designs WHERE id = ?", (design_id,)
        ).fetchone()
        if design is None:
            return None
        audit = conn.execute(
            "SELECT * FROM audits WHERE design_id = ?", (design_id,)
        ).fetchone()
        return _row_to_detail(design, audit)


def list_designs() -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT d.id, d.name, d.cyclic, d.max_float, d.shafts, d.treadles,
                      d.warp_ends, d.weft_picks, d.created_at, a.exceeds
               FROM designs d JOIN audits a ON a.design_id = d.id
               ORDER BY d.id"""
        ).fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "cyclic": bool(r["cyclic"]),
                "max_float": r["max_float"],
                "shafts": r["shafts"],
                "treadles": r["treadles"],
                "warp_ends": r["warp_ends"],
                "weft_picks": r["weft_picks"],
                "exceeds": bool(r["exceeds"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]


def _tags_of(conn: sqlite3.Connection, design_id: int) -> list[dict]:
    """读取设计的全部标签，按首次添加（design_tags.id）升序。"""
    rows = conn.execute(
        """SELECT id, tag, created_at FROM design_tags
           WHERE design_id = ? ORDER BY id""",
        (design_id,),
    ).fetchall()
    return [
        {"id": r["id"], "tag": r["tag"], "created_at": r["created_at"]}
        for r in rows
    ]


def add_design_tag(design_id: int, tag: str) -> dict:
    """在同一 SQLite 事务内：确认设计存在 → 校验标签非空且长度不超过 32 →
    确认该设计下尚未有同名标签 → 写入并返回该设计的完整标签列表（按首次
    添加顺序）。设计不存在抛 DesignNotFoundError；标签非法抛 TagError；
    重复抛 DuplicateTagError，失败时事务整体回滚不写库。"""
    with _lock, _connect() as conn:
        design = conn.execute(
            "SELECT 1 FROM designs WHERE id = ?", (design_id,)
        ).fetchone()
        if design is None:
            raise DesignNotFoundError(design_id)
        if not isinstance(tag, str) or len(tag) == 0:
            raise TagError("tag must be a non-empty string")
        if len(tag) > MAX_TAG_LENGTH:
            raise TagError(
                f"tag too long: {len(tag)} chars, max is {MAX_TAG_LENGTH}"
            )
        existing = conn.execute(
            "SELECT 1 FROM design_tags WHERE design_id = ? AND tag = ?",
            (design_id, tag),
        ).fetchone()
        if existing is not None:
            raise DuplicateTagError(design_id, tag)
        conn.execute(
            "INSERT INTO design_tags (design_id, tag) VALUES (?, ?)",
            (design_id, tag),
        )
        tags = _tags_of(conn, design_id)
        return {
            "design_id": design_id,
            "tags": tags,
            "count": len(tags),
        }


def list_design_tags(design_id: int) -> dict:
    """按首次添加顺序返回设计的标签与数量；设计不存在抛
    DesignNotFoundError。只读，不改动原设计与审计结果。"""
    with _lock, _connect() as conn:
        design = conn.execute(
            "SELECT 1 FROM designs WHERE id = ?", (design_id,)
        ).fetchone()
        if design is None:
            raise DesignNotFoundError(design_id)
        tags = _tags_of(conn, design_id)
        return {
            "design_id": design_id,
            "tags": tags,
            "count": len(tags),
        }


def _archive_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "digest": row["digest"],
        "design_ids": json.loads(row["design_ids"]),
        "item_count": row["item_count"],
        "manifest": json.loads(row["manifest"]),
        "created_at": row["created_at"],
    }


def get_designs_by_ids(design_ids: list[int]) -> list[dict]:
    """按给定 ID 列表读取设计详情（含审计结果），按传入顺序返回；
    调用方负责先排序。读不到的 ID 不出现在结果中，由调用方比对缺失。"""
    if not design_ids:
        return []
    placeholders = ",".join("?" for _ in design_ids)
    with _lock, _connect() as conn:
        design_rows = conn.execute(
            f"SELECT * FROM designs WHERE id IN ({placeholders})",
            tuple(design_ids),
        ).fetchall()
        by_id = {r["id"]: r for r in design_rows}
        result = []
        for design_id in design_ids:
            row = by_id.get(design_id)
            if row is None:
                continue
            audit = conn.execute(
                "SELECT * FROM audits WHERE design_id = ?", (design_id,)
            ).fetchone()
            result.append(_row_to_detail(row, audit))
        return result


def get_archive_by_digest(digest: str) -> dict | None:
    """按 digest 查找已有归档（幂等复用）；不存在返回 None。"""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM design_archives WHERE digest = ?", (digest,)
        ).fetchone()
        return _archive_row_to_dict(row) if row is not None else None


def get_archive(archive_id: int) -> dict | None:
    """按归档 ID 读取归档记录；不存在返回 None。"""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM design_archives WHERE id = ?", (archive_id,)
        ).fetchone()
        return _archive_row_to_dict(row) if row is not None else None


def insert_archive(
    *, digest: str, design_ids: list[int], manifest_text: str
) -> dict:
    """同事务持久化一条归档记录并返回其完整内容。

    digest 列有 UNIQUE 约束兜底：若并发下另一线程已先写入相同 digest，
    SQLite 抛 IntegrityError——调用方应转为按 digest 重查复用，绝不留下
    半成品（单行 INSERT 随事务整体回滚）。
    """
    with _lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO design_archives (digest, design_ids, item_count, manifest)
               VALUES (?, ?, ?, ?)""",
            (
                digest,
                json.dumps(design_ids),
                len(design_ids),
                manifest_text,
            ),
        )
        row = conn.execute(
            "SELECT * FROM design_archives WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _archive_row_to_dict(row)
