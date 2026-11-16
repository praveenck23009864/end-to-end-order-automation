"""Safe fixture parsing and normalization shared by encryption and import."""
import json

from sensitive_data import SensitiveDataError, CUSTOMER_ID_PATTERN, unique_json_object


def read_orders(path):
    try:
        orders = json.loads(path.read_text(encoding="utf-8-sig"),
                            object_pairs_hook=unique_json_object)
    except json.JSONDecodeError as error:
        raise SensitiveDataError(
            f"JSON syntax error at line {error.lineno}, column {error.colno}."
        ) from None
    if not isinstance(orders, dict) or not orders:
        raise SensitiveDataError("Expected a nonempty object keyed by customer ID.")
    return orders


def prepare_order(customer_id, order, *, allow_plaintext=False):
    from new import validate_order
    from sensitive_data import protect_order

    if not isinstance(customer_id, str) or not CUSTOMER_ID_PATTERN.fullmatch(customer_id):
        raise SensitiveDataError("Customer ID: invalid record identifier.")
    try:
        if not isinstance(order, dict):
            raise SensitiveDataError("Customer data: expected an object.")
        normalized = dict(order)
        choice = normalized.get("EZ Enroll")
        if not isinstance(choice, str) or choice.strip().lower() not in {"yes", "no"}:
            raise SensitiveDataError("EZ Enroll: supported choices are Yes, No.")
        normalized["EZ Enroll"] = choice.strip().capitalize()
        validate_order(normalized, allow_plaintext=allow_plaintext)
        protected = protect_order(normalized, customer_id)
        validate_order(protected)
        return protected
    except ValueError as error:
        # Validators use fixed field names and never interpolate supplied values.
        raise SensitiveDataError(f"Customer {customer_id}: {error}") from None
