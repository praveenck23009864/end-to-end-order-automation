"""Atomically refresh orders.encrypted.json using the existing encryption key."""
import argparse
import json
import os
import tempfile
from pathlib import Path

from sensitive_data import SensitiveDataError
from order_workflow import read_orders, prepare_order


def protect_file(source: Path, destination: Path | None = None):
    source = Path(source)
    destination = Path(destination) if destination is not None else source.parent / "orders.encrypted.json"
    if (destination.name.casefold() == "orders.json"
            or source.resolve() == destination.resolve()
            or (destination.exists() and os.path.samefile(source, destination))):
        raise ValueError("The editable source must never be overwritten.")
    if destination.name != "orders.encrypted.json":
        raise ValueError("Use orders.encrypted.json as the encryption output.")
    orders = read_orders(source)
    if not isinstance(orders, dict) or not orders:
        raise ValueError("Expected a nonempty object keyed by customer ID.")
    protected = {}
    for customer_id, order in orders.items():
        protected[customer_id] = prepare_order(customer_id, order, allow_plaintext=True)
    encoded = json.dumps(protected, indent=2).encode("utf-8")
    temporary = None
    try:
        # Serialize the entire protected batch before touching the destination.
        # The same directory ensures replace is atomic on the same filesystem.
        with tempfile.NamedTemporaryFile(mode="wb", dir=destination.parent,
                                         prefix=".orders.encrypted.", suffix=".tmp",
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows requires the temporary file to be closed before replacement.
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(protected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, nargs="?", default=Path("orders.json"))
    parser.add_argument("output", type=Path, nargs="?", help="defaults to orders.encrypted.json beside the source")
    args = parser.parse_args()
    try:
        count = protect_file(args.source, args.output)
    except SensitiveDataError as error:
        raise SystemExit(str(error)) from None
    except (ValueError, OSError, TypeError):
        raise SystemExit(
            "Encryption failed. Check JSON fields, existing key setup and the output path. "
            "The previous encrypted export was preserved."
        ) from None
    print(f"Encrypted {count} customer records into orders.encrypted.json.")
    print("The source still contains its original data. Apply your data-retention policy to it.")


if __name__ == "__main__":
    main()
