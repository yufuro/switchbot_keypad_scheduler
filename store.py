"""SQLite 永続化層（スケジュール・発行履歴・ログ）."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

# 既定は app.py と同じフォルダ。APP_DB_PATH で別の場所を指定できます
DB_PATH = Path(os.getenv("APP_DB_PATH") or Path(__file__).with_name("data.sqlite3")).expanduser()
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    label         TEXT    NOT NULL,
    passcode      TEXT    NOT NULL,
    key_type      TEXT    NOT NULL DEFAULT 'timeLimit',   -- timeLimit / permanent
    repeat_type   TEXT    NOT NULL DEFAULT 'daily',       -- daily / weekly / date
    weekdays      TEXT    NOT NULL DEFAULT '',            -- 月=0 … 日=6 のCSV
    on_date       TEXT    NOT NULL DEFAULT '',            -- YYYY-MM-DD（repeat_type=date）
    start_time    TEXT    NOT NULL,                       -- HH:MM
    end_time      TEXT    NOT NULL,                       -- HH:MM（start より小さい場合は翌日）
    lead_minutes  INTEGER NOT NULL DEFAULT 10,            -- 何分前に発行しておくか
    enabled       INTEGER NOT NULL DEFAULT 1,
    panel         INTEGER NOT NULL DEFAULT 0,              -- 操作パネルで扱う番号か
    panel_minutes INTEGER NOT NULL DEFAULT 120,            -- パネルで有効化したときの有効時間(分)
    note          TEXT    NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS issuances (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id     INTEGER,                                  -- NULL = 手動発行
    occurrence  TEXT NOT NULL DEFAULT '',                 -- YYYY-MM-DD（開始日）
    key_name    TEXT NOT NULL UNIQUE,                     -- 認証パッド上の名前
    passcode    TEXT NOT NULL,
    key_type    TEXT NOT NULL,
    valid_from  INTEGER,
    valid_to    INTEGER,
    key_id      INTEGER,                                  -- keyList から回収
    state       TEXT NOT NULL,                            -- creating/active/deleting/deleted/error
    detail      TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_issuance_occurrence
    ON issuances(rule_id, occurrence) WHERE rule_id IS NOT NULL AND occurrence <> '';

CREATE TABLE IF NOT EXISTS logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    except sqlite3.OperationalError as exc:
        folder = Path(DB_PATH).parent
        raise sqlite3.OperationalError(
            f"データベースを開けません: {DB_PATH}（{exc}）\n"
            f"  ・フォルダは存在しますか: {folder} → {'あり' if folder.is_dir() else 'ありません'}\n"
            "  ・アプリの起動中にフォルダを移動・リネームしていませんか\n"
            "  ・data.sqlite3 の所有者が今のユーザーか確認してください（sudo で起動した名残など）\n"
            "  ・別の場所に置きたい場合は .env に APP_DB_PATH=/絶対パス/data.sqlite3 を設定してください"
        ) from exc


# 後から増えた列（既存の data.sqlite3 をそのまま使えるようにする）
_ADDED_COLUMNS = (
    ("panel", "INTEGER NOT NULL DEFAULT 0"),
    ("panel_minutes", "INTEGER NOT NULL DEFAULT 120"),
)


def init() -> None:
    folder = Path(DB_PATH).parent
    folder.mkdir(parents=True, exist_ok=True)
    if not os.access(folder, os.W_OK):
        raise PermissionError(
            f"フォルダに書き込めません: {folder}\n"
            "  SQLite は本体のほかに -wal / -shm ファイルを同じ場所に作ります。"
            "書き込み権限のある場所に置くか、.env の APP_DB_PATH で別の場所を指定してください。"
        )
    with _lock, connect() as conn:
        conn.executescript(SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(rules)")}
        for name, ddl in _ADDED_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE rules ADD COLUMN {name} {ddl}")
        conn.commit()


def _now() -> int:
    return int(time.time())


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _lock, connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def execute(sql: str, params: tuple = ()) -> int:
    with _lock, connect() as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid or cur.rowcount


# ---------------------------------------------------------------------- rules
RULE_FIELDS = (
    "label", "passcode", "key_type", "repeat_type", "weekdays",
    "on_date", "start_time", "end_time", "lead_minutes", "enabled", "note",
    "panel", "panel_minutes",
)


_RULE_DEFAULTS: dict[str, Any] = {
    "key_type": "timeLimit", "repeat_type": "daily", "weekdays": "", "on_date": "",
    "lead_minutes": 10, "enabled": 1, "note": "", "panel": 0, "panel_minutes": 120,
}


def list_rules() -> list[dict[str, Any]]:
    return query("SELECT * FROM rules ORDER BY start_time, id")


def get_rule(rule_id: int) -> dict[str, Any] | None:
    rows = query("SELECT * FROM rules WHERE id=?", (rule_id,))
    return rows[0] if rows else None


def insert_rule(data: dict[str, Any]) -> int:
    cols = ", ".join(RULE_FIELDS) + ", created_at, updated_at"
    marks = ", ".join("?" * (len(RULE_FIELDS) + 2))
    vals = tuple(data.get(f, _RULE_DEFAULTS.get(f)) for f in RULE_FIELDS) + (_now(), _now())
    return execute(f"INSERT INTO rules ({cols}) VALUES ({marks})", vals)


def update_rule(rule_id: int, data: dict[str, Any]) -> None:
    sets = ", ".join(f"{f}=?" for f in RULE_FIELDS) + ", updated_at=?"
    vals = tuple(data.get(f, _RULE_DEFAULTS.get(f)) for f in RULE_FIELDS) + (_now(), rule_id)
    execute(f"UPDATE rules SET {sets} WHERE id=?", vals)


def delete_rule(rule_id: int) -> None:
    execute("DELETE FROM rules WHERE id=?", (rule_id,))


def clear_panel_except(rule_id: int) -> None:
    """操作パネルで扱う番号は1つだけにする."""
    execute("UPDATE rules SET panel=0 WHERE id<>?", (rule_id,))


def get_panel_rule() -> dict[str, Any] | None:
    rows = query("SELECT * FROM rules WHERE panel=1 ORDER BY id LIMIT 1")
    return rows[0] if rows else None


# ----------------------------------------------------------------- issuances
def list_issuances(states: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    if states:
        marks = ",".join("?" * len(states))
        return query(
            f"SELECT * FROM issuances WHERE state IN ({marks}) ORDER BY id DESC", states
        )
    return query("SELECT * FROM issuances ORDER BY id DESC LIMIT 200")


def find_issuance(rule_id: int, occurrence: str) -> dict[str, Any] | None:
    rows = query(
        "SELECT * FROM issuances WHERE rule_id=? AND occurrence=?", (rule_id, occurrence)
    )
    return rows[0] if rows else None


def insert_issuance(data: dict[str, Any]) -> int:
    fields = (
        "rule_id", "occurrence", "key_name", "passcode", "key_type",
        "valid_from", "valid_to", "key_id", "state", "detail",
    )
    cols = ", ".join(fields) + ", created_at, updated_at"
    marks = ", ".join("?" * (len(fields) + 2))
    vals = tuple(data.get(f) for f in fields) + (_now(), _now())
    return execute(f"INSERT INTO issuances ({cols}) VALUES ({marks})", vals)


def update_issuance(issuance_id: int, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
    execute(
        f"UPDATE issuances SET {sets} WHERE id=?",
        tuple(fields.values()) + (_now(), issuance_id),
    )


# ----------------------------------------------------------------------- logs
def log(level: str, message: str) -> None:
    print(f"[{level}] {message}", flush=True)      # 画面ログは DB が壊れていても残す
    try:
        execute("INSERT INTO logs (ts, level, message) VALUES (?,?,?)", (_now(), level, message))
    except sqlite3.Error as exc:
        print(f"[ERROR] ログを保存できませんでした: {exc}", flush=True)


def recent_logs(limit: int = 120) -> list[dict[str, Any]]:
    return query("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))
