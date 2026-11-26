"""Remove legacy sensitive fields while preserving customer and run records."""

import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

import database
from sensitive_data import CARD_FIELDS, SENSITIVE_ORDER_KEYS


def _sensitive_key(key):
    normalized = str(key).strip().casefold()
    compact = re.sub(r"[^a-z0-9]", "", normalized)
    return (
        normalized in SENSITIVE_ORDER_KEYS
        or compact in CARD_FIELDS
        or compact in {
            "expirationmonth", "expirationyear",
        }
    )


def redact(value):
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items() if not _sensitive_key(key)}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _rewrite_json(path: Path):
    if not path.exists():
        return 0
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    safe = redact(value)
    parent = path.parent
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=parent,
            prefix=".redacted-", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(safe, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return len(value) if isinstance(value, dict) else 0


def scrub_database():
    path = Path(database.DB_PATH)
    if not path.exists():
        raise FileNotFoundError("Configured project database does not exist.")
    con = sqlite3.connect(path, timeout=20)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        for table, identity, column in (
            ("orders", "id", "payload"),
            ("runs", "id", "order_snapshot"),
        ):
            rows = con.execute(f"SELECT {identity},{column} FROM {table}").fetchall()
            for record_id, raw in rows:
                safe = json.dumps(redact(json.loads(raw)), separators=(",", ":"))
                con.execute(f"UPDATE {table} SET {column}=? WHERE {identity}=?", (safe, record_id))
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("VACUUM")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def scrub_project_files():
    return sum(
        _rewrite_json(Path(filename))
        for filename in ("orders.json", "orders.encrypted.json")
    )


if __name__ == "__main__":
    records = scrub_project_files()
    scrub_database()
    print(f"Redacted sensitive fields from project JSON exports; JSON records checked: {records}.")
    print("SQLite customer and run records preserved; legacy sensitive fields removed.")
