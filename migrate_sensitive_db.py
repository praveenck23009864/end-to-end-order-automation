"""Copy the existing API database into a new encrypted database, preserving history."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import database as db
from sensitive_data import protect_order, SAFE_STAGES, unique_json_object


def migrate(source: Path, destination: Path):
    if not source.is_file() or source.resolve() == destination.resolve():
        raise ValueError("Choose an existing source and a different new destination.")
    # Exclusive creation protects an existing destination from accidental overwrite.
    destination.touch(exist_ok=False)
    previous_path = db.DB_PATH
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as old:
            old.row_factory = sqlite3.Row
            old.execute("BEGIN")
            orders = old.execute("SELECT * FROM orders").fetchall()
            runs = old.execute("SELECT * FROM runs").fetchall()
            if any(row["status"] == "running" for row in runs):
                raise ValueError("Resolve active runs after stopping their browser workers first.")

        db.DB_PATH = destination
        db.init_db()
        with db.connection() as new:
            new.execute("BEGIN IMMEDIATE")
            for row in orders:
                raw = json.loads(row["payload"], object_pairs_hook=unique_json_object)
                protected = protect_order(raw, row["id"])
                new.execute(
                    "INSERT INTO orders VALUES (?,?,?,?)",
                    (row["id"], json.dumps(protected), row["created_at"], row["updated_at"]),
                )
            for row in runs:
                raw = json.loads(row["order_snapshot"], object_pairs_hook=unique_json_object)
                protected = protect_order(raw, row["order_id"])
                stage = row["stage"] if row["stage"] in SAFE_STAGES else "legacy stage omitted"
                code = row["error_code"] if row["error_code"] in db.ERROR_CODES else None
                new.execute(
                    "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        row["id"], row["order_id"], row["status"], stage, code,
                        row["started_at"], row["updated_at"], row["finished_at"],
                        json.dumps(protected),
                    ),
                )
        db.check_stored_protection()
        with db.connection() as new:
            new.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return len(orders), len(runs)
    except BaseException:
        # Only clean the newly created destination, never the source database.
        for path in (destination, Path(str(destination) + "-wal"), Path(str(destination) + "-shm")):
            path.unlink(missing_ok=True)
        raise
    finally:
        db.DB_PATH = previous_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        orders, runs = migrate(args.source, args.output)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        raise SystemExit(
            "Migration failed. Check local keys, the earlier API database schema, "
            "active runs, card fields and the new output path. Stop API and workers first."
        ) from None
    print(f"Protected {orders} orders and {runs} run snapshots.")
    print(f"Encrypted database: {args.output.resolve()}")
    print("The old database remains plaintext where it was plaintext before migration.")
    print("Apply your data-retention policy to the old database, source JSON and backups.")


if __name__ == "__main__":
    main()
