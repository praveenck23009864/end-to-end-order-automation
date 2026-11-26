import sqlite3
import json

DB = "allconnect_demo.db"
conn = sqlite3.connect(DB)

try:
    conn.execute("BEGIN")

    columns = conn.execute("PRAGMA table_info(orders)").fetchall()
    column_names = [c[1] for c in columns]

    json_column = None

    for column in column_names:
        if column == "id":
            continue

        for row in conn.execute(
            f'SELECT "{column}" FROM orders WHERE id = ?',
            ("1",)
        ):
            try:
                data = json.loads(row[0])
                if isinstance(data, dict) and "First Name" in data:
                    json_column = column
                    break
            except (TypeError, json.JSONDecodeError):
                pass

        if json_column:
            break

    if not json_column:
        raise RuntimeError("Could not identify the order JSON column.")

    for record_id in range(1, 6):
        row = conn.execute(
            f'SELECT "{json_column}" FROM orders WHERE id = ?',
            (str(record_id),)
        ).fetchone()

        if not row:
            raise RuntimeError(f"Database record {record_id} not found.")

        order = json.loads(row[0])
        order.update({
            "Test Case ID": f"M{record_id}",
            "Test Scenario Category": "Manual",
            "Test Scenario Description": "Original manually created order"
        })

        conn.execute(
            f'UPDATE orders SET "{json_column}" = ? WHERE id = ?',
            (json.dumps(order), str(record_id))
        )

    conn.commit()
    print("SUCCESS: Metadata added to records M1-M5.")

    for record_id in range(1, 6):
        row = conn.execute(
            f'SELECT "{json_column}" FROM orders WHERE id = ?',
            (str(record_id),)
        ).fetchone()
        order = json.loads(row[0])
        print(record_id, order["Test Case ID"], order["First Name"],
              order["Test Scenario Category"])

except Exception:
    conn.rollback()
    raise
finally:
    conn.close()
