import json
import os
import sqlite3

DB_PATH = os.getenv(
    "ALLCONNECT_DB",
    r"D:\end-to-end-order-automation-run\allconnect_demo.db"
)

SENSITIVE_WORDS = (
    "ssn", "dob", "date of birth", "card number",
    "cardholder", "cvv", "cvc", "expiry", "expiration",
    "password", "secret", "private key", "api token"
)

def redact(data):
    if isinstance(data, dict):
        return {
            key: "[REDACTED]" if any(
                word in key.lower() for word in SENSITIVE_WORDS
            ) else redact(value)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact(item) for item in data]
    return data

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

try:
    rows = conn.execute("SELECT * FROM orders ORDER BY id").fetchall()
    print(f"\nTOTAL CUSTOMERS: {len(rows)}\n")

    for row in rows:
        record = dict(row)
        if isinstance(record.get("payload"), str):
            try:
                record["payload"] = json.loads(record["payload"])
            except json.JSONDecodeError:
                pass

        print("=" * 60)
        print("CUSTOMER / ORDER ID:", record.get("id"))
        print(json.dumps(redact(record), indent=4, ensure_ascii=False))
finally:
    conn.close()
