"""Validation for runtime-only identity and payment fields."""

from datetime import date
import re
from sensitive_data import SensitiveDataError


SENSITIVE_FIELDS = frozenset({
    "ssn", "dob", "card_type", "card_number", "cardholder_name", "cvv",
    "expiration_month", "expiration_year",
})
MAX_PLAINTEXT_BYTES = 8192


class SensitivePayloadError(SensitiveDataError):
    """Fixed-message failure that never includes a supplied value."""


def validate_sensitive_payload(payload, *, payment_required=True):
    if not isinstance(payload, dict) or set(payload) != SENSITIVE_FIELDS:
        raise SensitivePayloadError("Sensitive payload fields are invalid.")
    if any(not isinstance(payload[key], str) for key in SENSITIVE_FIELDS):
        raise SensitivePayloadError("Sensitive payload field types are invalid.")
    ssn = payload["ssn"].replace("-", "")
    if not re.fullmatch(r"\d{9}", ssn):
        raise SensitivePayloadError("Sensitive identity values are invalid.")
    try:
        date.fromisoformat(payload["dob"])
    except ValueError:
        raise SensitivePayloadError("Sensitive identity values are invalid.") from None
    if not payment_required:
        if any(payload[key] for key in (
            "card_type", "card_number", "cardholder_name", "cvv",
            "expiration_month", "expiration_year",
        )):
            raise SensitivePayloadError("Unexpected payment fields were supplied.")
        return payload
    if payload["card_type"] not in {"Visa", "MasterCard", "American Express", "Discover"}:
        raise SensitivePayloadError("Payment fields are invalid.")
    if not re.fullmatch(r"\d{12,19}", payload["card_number"]):
        raise SensitivePayloadError("Payment fields are invalid.")
    if not payload["cardholder_name"].strip() or len(payload["cardholder_name"]) > 128:
        raise SensitivePayloadError("Payment fields are invalid.")
    if not re.fullmatch(r"\d{3,4}", payload["cvv"]):
        raise SensitivePayloadError("Payment fields are invalid.")
    if not re.fullmatch(r"(?:0[1-9]|1[0-2])", payload["expiration_month"]):
        raise SensitivePayloadError("Payment fields are invalid.")
    if not re.fullmatch(r"20\d{2}", payload["expiration_year"]):
        raise SensitivePayloadError("Payment fields are invalid.")
    if len(str(payload).encode("utf-8")) > MAX_PLAINTEXT_BYTES:
        raise SensitivePayloadError("Sensitive payload is too large.")
    return payload
