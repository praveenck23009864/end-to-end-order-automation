"""Import encrypted customer records into allconnect_secure.db."""
import argparse
import json
from pathlib import Path

import database as db
from sensitive_data import SensitiveDataError
from order_workflow import read_orders, prepare_order


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, nargs="?", default=Path("orders.encrypted.json"))
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    try:
        orders = read_orders(args.file)
        if not isinstance(orders, dict) or not orders:
            raise ValueError
        protected = {}
        for customer_id, order in orders.items():
            # Require ciphertext in the source, then authenticate it locally.
            protected[customer_id] = prepare_order(customer_id, order)
        db.init_db()
        db.check_stored_protection()
        result = db.import_orders(protected, update=args.update)
    except SensitiveDataError as error:
        raise SystemExit(str(error)) from None
    except (ValueError, OSError, TypeError):
        raise SystemExit(
            "Import failed. Check encrypted JSON, local secrets, database protection "
            "and active runs. No partial customer batch was committed."
        ) from None
    print(json.dumps(result, indent=2))
    print(f"Database: {db.DB_PATH.resolve()}")
    print("SSN and DOB remain encrypted in orders and run snapshots.")


if __name__ == "__main__":
    main()
