"""
Safely sync the 61 scenario definitions into the existing Allconnect SQLite DB.

This script updates scenario metadata only. It deliberately does not overwrite the
existing order payload's customer, address, product, or payment fields. The existing
trigger values in the DB are preserved. Sensitive inputs remain in the masked
secure-input workflow and are never copied from the scenario JSON into SQLite.
"""

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

EXPECTED_CASE_IDS = {
    *(f"A{i}" for i in range(1, 6)),
    *(f"P{i}" for i in range(1, 16)),
    *(f"I{i}" for i in range(1, 4)),
    *(f"C{i}" for i in range(1, 6)),
    *(f"K{i}" for i in range(1, 22)),
    *(f"T{i}" for i in range(1, 8)),
    *(f"S{i}" for i in range(1, 6)),
}
SENSITIVE_CASE_PREFIX = "K"


def parse_json_object(raw, context):
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{context} contains invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{context} must be a JSON object.")
    return value


def get_case_id(payload):
    # Existing scenario-generation script stored this exact metadata key.
    value = payload.get("Test Case ID")
    if value is None:
        # Also support a nested metadata structure without changing the payload.
        metadata = payload.get("test_scenario") or payload.get("test_metadata") or {}
        if isinstance(metadata, dict):
            value = metadata.get("test_case_id") or metadata.get("Test Case ID")
    return str(value).strip().upper() if value is not None else None


def load_scenarios(json_path):
    data = json.loads(json_path.read_text(encoding="utf-8"))
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list):
        raise RuntimeError("JSON must contain a top-level 'scenarios' array.")

    by_id = {}
    for item in scenarios:
        if not isinstance(item, dict):
            raise RuntimeError("Every scenario must be a JSON object.")
        case_id = str(item.get("test_case_id", "")).strip().upper()
        if not case_id:
            raise RuntimeError("A scenario is missing test_case_id.")
        if case_id in by_id:
            raise RuntimeError(f"Duplicate test_case_id in JSON: {case_id}")
        by_id[case_id] = item

    actual = set(by_id)
    if actual != EXPECTED_CASE_IDS:
        missing = sorted(EXPECTED_CASE_IDS - actual)
        unexpected = sorted(actual - EXPECTED_CASE_IDS)
        raise RuntimeError(
            f"Scenario ID validation failed. Missing={missing}; unexpected={unexpected}"
        )
    return by_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        default=r"D:\end-to-end-order-automation-run\allconnect_demo.db",
        help="Path to allconnect_demo.db",
    )
    parser.add_argument(
        "--json",
        default="provider_xx_61_test_scenarios.json",
        help="Path to the 61-scenario JSON file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report changes without writing to the database",
    )
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    json_path = Path(args.json).expanduser().resolve()

    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")
    if not json_path.is_file():
        raise FileNotFoundError(f"Scenario JSON not found: {json_path}")

    scenarios = load_scenarios(json_path)

    # SQLite backup API provides a consistent backup, even if WAL is enabled.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.stem}_pre_scenario_sync_{timestamp}{db_path.suffix}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "orders" not in tables:
            raise RuntimeError("Database does not contain the required 'orders' table.")

        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(orders)")
        }
        required = {"id", "payload", "created_at", "updated_at"}
        if not required.issubset(columns):
            raise RuntimeError(
                f"Unexpected orders schema. Required {sorted(required)}, found {sorted(columns)}"
            )

        rows = conn.execute(
            "SELECT id, payload FROM orders ORDER BY id"
        ).fetchall()

        case_rows = {}
        original_customer_count = 0
        for row in rows:
            payload = parse_json_object(row["payload"], f"orders.id={row['id']}")
            case_id = get_case_id(payload)
            if case_id is None:
                original_customer_count += 1
                continue
            if case_id in case_rows:
                raise RuntimeError(
                    f"Duplicate Test Case ID '{case_id}' in DB rows "
                    f"{case_rows[case_id]['id']} and {row['id']}. No changes made."
                )
            case_rows[case_id] = {
                "id": row["id"],
                "payload": payload,
            }

        missing_in_db = sorted(EXPECTED_CASE_IDS - set(case_rows))
        unexpected_in_db = sorted(set(case_rows) - EXPECTED_CASE_IDS)
        if missing_in_db or unexpected_in_db:
            raise RuntimeError(
                "Database scenario inventory does not match the expected 61 cases. "
                f"Missing={missing_in_db}; unexpected={unexpected_in_db}. "
                "No rows were inserted or changed. Do not create order records without "
                "the project's approved full order-payload template."
            )

        if original_customer_count != 5:
            raise RuntimeError(
                f"Expected 5 original customer records, found {original_customer_count}. "
                "No changes made."
            )

        updates = []
        for case_id in sorted(EXPECTED_CASE_IDS):
            scenario = scenarios[case_id]
            payload = dict(case_rows[case_id]["payload"])

            # Preserve existing order/trigger fields. Update descriptive metadata only.
            payload["Test Case ID"] = case_id
            payload["Test Scenario Category"] = scenario.get("category", "")
            payload["Test Scenario Description"] = scenario.get("description", "")
            payload["Test Scenario Checkpoint"] = scenario.get("checkpoint", "")
            payload["Test Scenario Expected Behavior"] = scenario.get(
                "expected_behavior", ""
            )
            payload["Test Scenario ID"] = scenario.get("scenario_id", "")

            # K-cases must use the existing masked secure-input flow.
            if case_id.startswith(SENSITIVE_CASE_PREFIX):
                payload["Secure Input Required"] = True
                payload["Secure Input Instruction"] = (
                    scenario.get("test_data", {}).get("secure_input_instruction")
                    or "Use the existing masked secure-input workflow."
                )
                # Never copy sensitive trigger values into the DB.
                payload.pop("SSN", None)
                payload.pop("DOB", None)
                payload.pop("Card Number", None)
                payload.pop("CVV", None)
                payload.pop("Card Expiry", None)

            updates.append((json.dumps(payload, ensure_ascii=False), case_rows[case_id]["id"]))

        print(f"Database: {db_path}")
        print(f"Scenario JSON: {json_path}")
        print(f"Original customer records preserved: {original_customer_count}")
        print(f"Scenario records matched: {len(updates)}")
        print("Missing scenario IDs: 0")
        print("Duplicate scenario IDs: 0")
        print(f"Total orders after sync: {len(rows)}")
        print(f"Mode: {'DRY RUN - no database changes' if args.dry_run else 'WRITE'}")

        if args.dry_run:
            print("Dry run complete. Re-run without --dry-run to apply the metadata updates.")
            return

        conn.backup(sqlite3.connect(str(backup_path)))
        print(f"Backup created: {backup_path}")

        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "UPDATE orders SET payload = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                updates,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # Verify after commit.
        verify_rows = conn.execute("SELECT id, payload FROM orders").fetchall()
        verified = {}
        customers_after = 0
        for row in verify_rows:
            payload = parse_json_object(row["payload"], f"orders.id={row['id']}")
            case_id = get_case_id(payload)
            if case_id is None:
                customers_after += 1
            elif case_id in verified:
                raise RuntimeError(f"Duplicate scenario ID found after sync: {case_id}")
            else:
                verified[case_id] = row["id"]

        missing_after = sorted(EXPECTED_CASE_IDS - set(verified))
        if missing_after or customers_after != 5 or len(verify_rows) != 66:
            raise RuntimeError(
                "Post-sync validation failed: "
                f"missing={missing_after}, original_customers={customers_after}, "
                f"total_orders={len(verify_rows)}. Backup is available at {backup_path}"
            )

        print("\nSYNC SUCCESS")
        print(f"Updated existing scenario records: {len(updates)}")
        print("Inserted records: 0")
        print("Skipped records: 0")
        print(f"Original customer records: {customers_after}")
        print(f"Unique Test Case IDs: {len(verified)}")
        print(f"Total database records: {len(verify_rows)}")
        print("All 61 scenario IDs verified.")
        print("No browser automation or order submission was executed.")

    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
