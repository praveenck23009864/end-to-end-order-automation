"""Copy an API database while removing sensitive fields from stored payloads."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import database as db
from sensitive_data import assert_non_sensitive_order, SAFE_STAGES, unique_json_object
from scrub_sensitive_data import redact


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
            old_run_columns = {row["name"] for row in old.execute("PRAGMA table_info(runs)")}
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
                sanitized = redact(raw)
                sanitized.setdefault("Customer ID", row["id"])
                assert_non_sensitive_order(sanitized, row["id"])
                new.execute(
                    "INSERT INTO orders VALUES (?,?,?,?)",
                    (row["id"], json.dumps(sanitized), row["created_at"], row["updated_at"]),
                )
            for row in runs:
                raw = json.loads(row["order_snapshot"], object_pairs_hook=unique_json_object)
                sanitized = redact(raw)
                sanitized.setdefault("Customer ID", row["order_id"])
                assert_non_sensitive_order(sanitized, row["order_id"])
                stage = row["stage"] if row["stage"] in SAFE_STAGES else "legacy stage omitted"
                code = row["error_code"] if row["error_code"] in db.ERROR_CODES else None
                if {"final_order_submitted", "submission_state"} <= old_run_columns:
                    submission_state = row["submission_state"]
                    final_submitted = bool(row["final_order_submitted"])
                    if submission_state not in {
                        "not_attempted", "attempting", "ambiguous", "confirmed",
                        "not_submitted", "unknown",
                    }:
                        submission_state = "unknown"
                    if (submission_state == "confirmed") != final_submitted:
                        final_submitted = False
                        submission_state = "unknown"
                    if (
                        row["status"] == "installation_completed"
                        and (not final_submitted or submission_state != "confirmed")
                    ):
                        final_submitted = False
                        submission_state = "unknown"
                else:
                    # Legacy schema cannot prove submission; never infer it from
                    # status or a button stage during migration.
                    submission_state = "unknown"
                    final_submitted = False
                new.execute(
                    """INSERT INTO runs (
                        id,order_id,status,stage,error_code,started_at,updated_at,
                        finished_at,order_snapshot,final_order_submitted,submission_state
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row["id"], row["order_id"], row["status"], stage, code,
                        row["started_at"], row["updated_at"], row["finished_at"],
                        json.dumps(sanitized), int(final_submitted), submission_state,
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
    print(f"Copied {orders} orders and {runs} runs with sensitive fields removed.")
    print(f"Sanitized database: {args.output.resolve()}")
    print("The source remains unchanged; scrub it separately when retention rules permit.")


if __name__ == "__main__":
    main()
