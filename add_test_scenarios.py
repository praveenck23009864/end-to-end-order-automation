"""
Create 61 Allconnect simulator test-scenario orders.

- Preserves the five existing orders.
- Creates a timestamped SQLite backup before inserting.
- Clones complete order payloads from existing customers and overrides
  simulator trigger fields for each test case.
- Does NOT store SSN, DOB, card number, CVV, expiry, or other payment secrets.
  Those must be entered through the project's existing secure masked-input flow.
- Does NOT create runs, submit orders, or modify existing run records.

Run from the project folder:
    python add_test_scenarios.py
"""

import copy
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.getenv(
    "ALLCONNECT_DB",
    r"D:\end-to-end-order-automation-run\allconnect_demo.db"
)).resolve()

SENSITIVE_KEYS = {
    "ssn", "social security number", "date of birth", "dob",
    "card number", "cardholder name", "cvv", "cvc",
    "expiration month", "expiration year", "expiry",
}

# (case ID, category, simulator trigger field, trigger value, description,
#  template customer ID, additional safe field overrides)
CASES = [
    ("A1", "Address and availability", "Zip", "99901", "Address not found; suggestions", "1", {}),
    ("A2", "Address and availability", "Zip", "99902", "Apartment building; unit number missing", "2", {"Unit Type": "Apartment", "Unit Number": ""}),
    ("A3", "Address and availability", "Zip", "99903", "ProviderXX unavailable; ProviderVZ only", "1", {}),
    ("A4", "Address and availability", "Zip", "99904", "Only 300 Mbps and 500 Mbps available", "1", {}),
    ("A5", "Address and availability", "Zip", "99905", "P O Box address not allowed", "1", {"Address 1": "PO Box 125"}),

    ("P1", "Price and product", "Zip", "99910", "Checkout price is $5 higher than plan list", "1", {}),
    ("P2", "Price and product", "Zip", "99911", "No 1-YEAR PRICING plans", "1", {}),
    ("P3", "Price and product", "Zip", "99912", "Phone unavailable", "3", {}),
    ("P4", "Price and product", "Zip", "99913", "TV unavailable", "2", {}),
    ("P5", "Price and product", "Zip", "99914", "Leased gateway out of stock", "1", {}),
    ("P6", "Price and product", "Zip", "99915", "Checkout price is $5 lower", "1", {}),
    ("P7", "Price and product", "Zip", "99916", "Plan disappears after adding", "1", {}),
    ("P8", "Price and product", "Zip", "99917", "Next lower speed preselected at checkout", "1", {}),
    ("P9", "Price and product", "Zip", "99918", "Unexpected $10 monthly Broadband Service Fee", "1", {}),
    ("P10", "Price and product", "Zip", "99919", "Professional install fee is $150 instead of $100", "2", {"Installation Type": "PRO_INSTALL"}),
    ("P11", "Price and product", "Zip", "99930", "1 Gig plan listed twice at $60 and $65", "4", {}),
    ("P12", "Price and product", "Zip", "99931", "Price depends on autopay", "1", {"Automatic Payments": "No"}),
    ("P13", "Price and product", "Zip", "99932", "1 Gig renamed Gigabit Connection", "4", {}),
    ("P14", "Price and product", "Zip", "99933", "ProviderXX rows show Call for pricing", "1", {}),
    ("P15", "Price and product", "Zip", "99934", "Monthly total is $5 more than line items", "4", {}),

    ("I1", "Installation", "Zip", "99920", "Professional install required; no self-install", "2", {"Installation Type": "PRO_INSTALL"}),
    ("I2", "Installation", "Zip", "99921", "First installation date is 21 days away", "2", {"Installation Type": "PRO_INSTALL"}),
    ("I3", "Installation", "Zip", "99922", "Self-install only; no professional install", "1", {"Installation Type": "STANDARD_SHIPPING"}),

    ("C1", "Customer and account", "Last Name", "Existing", "Already ProviderXX customer at this address", "4", {"Customer Type": "Transfer", "Existing ProviderXX Customer": "Yes"}),
    ("C2", "Customer and account", "Last Name", "Pastdue", "Past-due balance of $245.67", "4", {"Customer Type": "Transfer"}),
    ("C3", "Customer and account", "Last Name", "Mover", "ProviderXX service at another address", "3", {"Customer Type": "New", "Moving?": "Yes"}),
    ("C4", "Customer and account", "Last Name", "Mismatch", "Name does not match account", "4", {"Customer Type": "Transfer"}),
    ("C5", "Customer and account", "Last Name", "Holder", "Caller is not the account holder", "4", {"Customer Type": "Transfer", "Account Holder Verified": False}),

    ("K1", "Credit check and payment", "Secure input", "SSN: approved test value", "Credit check approved; secure SSN input required", "1", {"EZ Enroll": "No"}),
    ("K2", "Credit check and payment", "Secure input", "SSN: declined test value", "Credit check declined; secure SSN input required", "1", {"EZ Enroll": "No"}),
    ("K3", "Credit check and payment", "Secure input", "SSN: deposit-required test value", "Approved with $100 deposit; secure SSN input required", "1", {"EZ Enroll": "No"}),
    ("K4", "Credit check and payment", "Secure input", "SSN: identity-verification test value", "Identity cannot be verified; secure SSN input required", "1", {"EZ Enroll": "No"}),
    ("K5", "Credit check and payment", "Secure input", "SSN: first-try service-down test value", "Credit check service down on first try; secure SSN input required", "1", {"EZ Enroll": "No"}),
    ("K6", "Credit check and payment", "Secure input", "DOB: under-18 test date", "Customer under 18; secure DOB input required", "1", {"EZ Enroll": "No"}),
    ("K7", "Credit check and payment", "Secure input", "Card: accepted test card", "Card accepted; secure payment input required", "5", {"EZ Enroll": "Yes"}),
    ("K8", "Credit check and payment", "Secure input", "Card: declined test card", "Card declined; secure payment input required", "5", {"EZ Enroll": "Yes"}),
    ("K9", "Credit check and payment", "Secure input", "Card: expired test card", "Card expired; secure payment input required", "5", {"EZ Enroll": "Yes"}),
    ("K10", "Credit check and payment", "Secure input", "Card: mistyped-number test card", "Card number check fails; secure payment input required", "5", {"EZ Enroll": "Yes"}),
    ("K11", "Credit check and payment", "Secure input", "Card: wrong-security-code test card", "Wrong security code; secure payment input required", "5", {"EZ Enroll": "Yes"}),
    ("K12", "Credit check and payment", "Secure input", "Card: past expiration date", "Expiration date invalid; secure payment input required", "5", {"EZ Enroll": "Yes"}),
    ("K13", "Credit check and payment", "Secure input", "Card: billing ZIP mismatch test card", "Billing ZIP mismatch; secure payment input required", "5", {"EZ Enroll": "Yes"}),
    ("K14", "Credit check and payment", "Secure input", "Card: billing street mismatch test card", "Billing street mismatch; secure payment input required", "5", {"EZ Enroll": "Yes"}),
    ("K15", "Credit check and payment", "Secure input", "Card: unsupported Amex test card", "American Express not accepted; secure payment input required", "5", {"EZ Enroll": "Yes"}),
    ("K16", "Credit check and payment", "Secure input", "Card: blank cardholder name", "Required cardholder name missing; secure payment input required", "5", {"EZ Enroll": "Yes"}),
    ("K17", "Credit check and payment", "Secure input", "Card: three failed attempts", "Payment locked after three failed tries; secure payment input required", "5", {"EZ Enroll": "Yes"}),
    ("K18", "Credit check and payment", "Secure input", "Card: first-try payment service error", "Payment service error on first try; secure payment input required", "5", {"EZ Enroll": "Yes"}),
    ("K19", "Credit check and payment", "Secure input", "Card: payment service unavailable", "Payment service down on every try; secure payment input required", "5", {"EZ Enroll": "Yes"}),
    ("K20", "Credit check and payment", "Secure input", "Card: 60-second payment delay", "Payment hangs 60 seconds then succeeds; secure payment input required", "5", {"EZ Enroll": "Yes"}),
    ("K21", "Credit check and payment", "Secure input", "Card: insufficient-funds test card", "Insufficient funds for initial payment; secure payment input required", "5", {"EZ Enroll": "Yes"}),

    ("T1", "Portal and technical problems", "First Name", "Slowframe", "Checkout frame loads after 25 seconds", "1", {}),
    ("T2", "Portal and technical problems", "First Name", "Frameerror", "Checkout frame fails first load; retry works", "1", {}),
    ("T3", "Portal and technical problems", "First Name", "Expired", "Session expires once; reload resumes order", "1", {}),
    ("T4", "Portal and technical problems", "First Name", "Popup", "Unexpected promotion pop-up", "1", {}),
    ("T5", "Portal and technical problems", "First Name", "Layoutb", "Alternate checkout layout and field labels", "1", {}),
    ("T6", "Portal and technical problems", "First Name", "Script", "Extra mandatory statement with Read box", "1", {}),
    ("T7", "Portal and technical problems", "First Name", "Maintenance", "Portal under maintenance; order cannot continue", "1", {}),

    ("S1", "After Submit Order", "First Name", "Placefail", "Place Order fails", "1", {}),
    ("S2", "After Submit Order", "First Name", "Noconfirm", "Provider order ID missing on Close Call", "1", {}),
    ("S3", "After Submit Order", "First Name", "Slowsubmit", "Submit Order hangs 90 seconds then completes", "1", {}),
    ("S4", "After Submit Order", "First Name", "Doubleclick", "Second Submit Order within 10 seconds is reported", "1", {}),
    ("S5", "After Submit Order", "First Name", "Different", "Close Call names the next lower speed", "1", {}),
]

def redact_sensitive(payload):
    """Remove sensitive values inherited from a template, if any."""
    result = copy.deepcopy(payload)
    for key in list(result):
        if key.strip().lower() in SENSITIVE_KEYS:
            result.pop(key, None)
    return result

def main():
    if not DB_PATH.exists():
        raise SystemExit(f"Database not found: {DB_PATH}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = DB_PATH.with_name(f"{DB_PATH.stem}_before_61_scenarios_{stamp}{DB_PATH.suffix}")

    # Ensure no accidental overwrite and create a consistent SQLite backup.
    source = sqlite3.connect(str(DB_PATH))
    backup = sqlite3.connect(str(backup_path))
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
        required = {"id", "payload", "created_at", "updated_at"}
        if not required.issubset(columns):
            raise RuntimeError(
                f"Unexpected orders schema. Found columns: {sorted(columns)}"
            )

        existing_rows = conn.execute(
            "SELECT id, payload FROM orders ORDER BY CAST(id AS INTEGER)"
        ).fetchall()

        # Prevent accidental duplicate insertion if this script is run twice.
        existing_case_ids = set()
        for existing_row in existing_rows:
            try:
                existing_payload = json.loads(existing_row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            case_id = existing_payload.get("Test Case ID")
            if case_id:
                existing_case_ids.add(str(case_id))
        requested_case_ids = {case[0] for case in CASES}
        duplicates = sorted(existing_case_ids.intersection(requested_case_ids))
        if duplicates:
            raise RuntimeError(
                "Some scenario IDs already exist; refusing to create duplicates: "
                + ", ".join(duplicates)
            )

        if len(existing_rows) < 5:
            raise RuntimeError(
                f"Expected at least 5 existing customer orders; found {len(existing_rows)}"
            )

        templates = {}
        for row in existing_rows:
            try:
                parsed = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            templates[str(row["id"])] = parsed

        for template_id in ("1", "2", "3", "4", "5"):
            if template_id not in templates:
                raise RuntimeError(f"Could not load template customer {template_id}")

        numeric_ids = []
        for row in existing_rows:
            try:
                numeric_ids.append(int(row["id"]))
            except (TypeError, ValueError):
                pass
        next_id = max(numeric_ids, default=0) + 1

        now = datetime.now(timezone.utc).isoformat()
        inserted = []

        for index, (case_id, category, trigger_field, trigger_value,
                    description, template_id, overrides) in enumerate(CASES):
            payload = redact_sensitive(templates[template_id])

            # Keep every ordinary order field from the selected complete template.
            payload.update(copy.deepcopy(overrides))

            # Unique synthetic contact details and required 10-digit Call ID.
            scenario_num = index + 1
            payload["Customer ID"] = str(next_id)
            payload["Call ID"] = f"{7000000000 + scenario_num:010d}"
            payload["First Name"] = payload.get("First Name", "Test")
            payload["Last Name"] = payload.get("Last Name", "Customer")

            # Trigger field overrides for simulator cases.
            if trigger_field == "Zip":
                payload["Zip"] = trigger_value
            elif trigger_field == "Last Name":
                payload["Last Name"] = trigger_value
            elif trigger_field == "First Name":
                payload["First Name"] = trigger_value

            # Keep records clearly identifiable without putting sensitive values in DB.
            payload["Test Case ID"] = case_id
            payload["Test Scenario Category"] = category
            payload["Test Scenario Description"] = description
            payload["Test Trigger Field"] = trigger_field
            payload["Test Trigger Value"] = trigger_value
            if case_id.startswith("K"):
                payload["Secure Input Required"] = True
                payload["Secure Input Note"] = (
                    "Enter the simulator's documented SSN/DOB/card trigger "
                    "only through the existing secure masked-input workflow. "
                    "Do not save it in this database."
                )
            else:
                payload["Secure Input Required"] = False

            # Ensure no inherited sensitive values slipped into the JSON payload.
            payload = redact_sensitive(payload)

            row_id = str(next_id)
            conn.execute(
                "INSERT INTO orders (id, payload, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (row_id, json.dumps(payload, ensure_ascii=False), now, now)
            )
            inserted.append((row_id, case_id))
            next_id += 1

        conn.commit()

        total = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        print("SUCCESS: scenario records inserted.")
        print(f"Original records preserved: {len(existing_rows)}")
        print(f"New scenarios inserted: {len(inserted)}")
        print(f"Total order records now: {total}")
        print(f"Database: {DB_PATH}")
        print(f"Backup: {backup_path}")
        print("\nInserted scenario IDs:")
        for row_id, case_id in inserted:
            print(f"  Order {row_id}: {case_id}")
        print("\nNo runs were created or modified.")
        print("No SSN, DOB, card number, CVV, or expiry values were stored.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    main()
