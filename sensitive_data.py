"""Local Windows encryption for SSN and DOB. No keys are generated at import."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import date
import json
import os
from pathlib import Path
import re
import secrets

from cryptography.fernet import Fernet, InvalidToken


class SensitiveDataError(ValueError):
    """A controlled message that never includes customer values."""


ENVELOPE_FIELD = "Sensitive Data"
KEY_ID = "local-v1"
CUSTOMER_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
CARD_FIELDS = {
    "card", "cards", "carddetails", "paymentcard", "cardnumber",
    "creditcard", "creditcardnumber", "debitcard", "debitcardnumber",
    "pan", "cvv", "cvc", "cvv2", "cvc2", "cid", "securitycode", "pin",
    "cardholder", "cardholdername", "expiry", "expirydate", "expirationdate",
    "cardexpiry", "expirymonth", "expiryyear", "expmonth", "expyear",
}
SENSITIVE_ORDER_KEYS = {
    "ssn", "dob", "sensitive data", "card type", "card_type", "card number",
    "card_number", "cardholder name", "cardholder_name", "cvv code", "cvv",
    "cvc", "expiration month", "expiration_month", "expiration year",
    "expiration_year", "cardtype", "cvvcode", "expirymonth", "expiryyear",
}

SAFE_STAGES = {
    "opening simulator", "validating saved order",
    "waiting for login / session screen", "preparing automation steps",
    "filling Basic Information", "confirming matched address",
    "qualifying New / Transfer", "checking provider availability",
    "opening internet plans", "selecting internet offer",
    "selecting checkout products", "setting Broadband Labels",
    "selecting equipment", "qualifying Account Holder",
    "credit check and installation", "installation Forward clicked",
    "Step 3 installation and payment",
    "Authorization and Submit Order", "order submitted",
    "submission outcome ambiguous",
    "verifying EZ Enroll payment review",
    "EZ Enroll payment review completed",
    "verifying sample payment fields",
    "payment fields verified; stopped for manual review",
    "sample payment and Forward", "payment Forward clicked",
    "manual recovery", "legacy stage omitted",
}


def reject_card_fields(value):
    """Payment data must use a separately approved collection integration."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in CARD_FIELDS:
                raise SensitiveDataError(
                    "Card fields are unsupported in customer JSON and snapshots."
                )
            reject_card_fields(item)
    elif isinstance(value, list):
        for item in value:
            reject_card_fields(item)


def validate_sensitive_values(values):
    if not isinstance(values, dict) or set(values) != {"SSN", "DOB"}:
        raise SensitiveDataError("Provide exactly SSN and DOB in the private payload.")
    if not isinstance(values["SSN"], str) or not re.fullmatch(
        r"(?:\d{9}|\d{3}-\d{2}-\d{4})", values["SSN"]
    ):
        raise SensitiveDataError("SSN must have nine digits or the dashed format.")
    try:
        dob = values["DOB"]
        if not isinstance(dob, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dob):
            raise ValueError
        date.fromisoformat(dob)
    except (TypeError, ValueError):
        raise SensitiveDataError("DOB must be a valid YYYY-MM-DD date.") from None


def assert_protected_order(order, customer_id=None):
    """Check storage structure without decrypting the payload."""
    if not isinstance(order, dict):
        raise SensitiveDataError("Customer data must be an object.")
    reject_card_fields(order)
    if "SSN" in order or "DOB" in order:
        raise SensitiveDataError(
            "Plaintext SSN or DOB found. Import with the encryption tools first."
        )
    identity = order.get("Customer ID")
    if not isinstance(identity, str) or not CUSTOMER_ID_PATTERN.fullmatch(identity):
        raise SensitiveDataError("A protected customer needs a valid Customer ID.")
    if customer_id is not None and identity != str(customer_id):
        raise SensitiveDataError("Protected customer ID does not match its record.")
    envelope = order.get(ENVELOPE_FIELD)
    if not isinstance(envelope, dict) or set(envelope) != {
        "version", "algorithm", "key_id", "token"
    }:
        raise SensitiveDataError("Missing or invalid encrypted customer payload.")
    if (
        type(envelope["version"]) is not int
        or envelope["version"] != 1
        or envelope["algorithm"] != "fernet"
        or envelope["key_id"] != KEY_ID
        or not isinstance(envelope["token"], str)
        or not 80 <= len(envelope["token"]) <= 8192
    ):
        raise SensitiveDataError("Unsupported encrypted customer payload.")


def assert_non_sensitive_order(order, customer_id=None):
    """Validate an order allowed in ordinary JSON/SQLite/API responses."""
    if not isinstance(order, dict):
        raise SensitiveDataError("Customer data must be an object.")
    if not isinstance(order.get("Customer ID"), str) or not CUSTOMER_ID_PATTERN.fullmatch(order["Customer ID"]):
        raise SensitiveDataError("A customer needs a valid Customer ID.")
    if customer_id is not None and order["Customer ID"] != str(customer_id):
        raise SensitiveDataError("Customer ID does not match its record.")

    def scan(value):
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).strip().casefold()
                compact = re.sub(r"[^a-z0-9]", "", normalized)
                if normalized in SENSITIVE_ORDER_KEYS or compact in CARD_FIELDS or compact in {
                    "cardtype", "cvvcode",
                    "expirymonth", "expirationmonth", "expiryyear", "expirationyear",
                }:
                    raise SensitiveDataError("Sensitive fields are runtime-only and cannot be stored.")
                scan(item)
        elif isinstance(value, list):
            for item in value:
                scan(item)

    scan(order)
    return order


def _private_file():
    if os.name != "nt":
        raise SensitiveDataError("This key store requires Windows DPAPI.")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise SensitiveDataError("The Windows user profile is unavailable.")
    return Path(local_app_data) / "AllconnectAutomation" / "secrets.dpapi"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("data", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    """Use current-user DPAPI with no UI and no machine-wide flag."""
    if os.name != "nt":
        raise SensitiveDataError("This key store requires Windows DPAPI.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(
        len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    target = _DataBlob()
    blob_pointer = ctypes.POINTER(_DataBlob)
    if decrypt:
        function = crypt32.CryptUnprotectData
        second_argument_type = ctypes.c_void_p
    else:
        function = crypt32.CryptProtectData
        second_argument_type = wintypes.LPCWSTR
    function.argtypes = [
        blob_pointer, second_argument_type, blob_pointer,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer,
    ]
    function.restype = wintypes.BOOL
    try:
        # CRYPTPROTECT_UI_FORBIDDEN is 1. Current-user scope is the default.
        if not function(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)
        ):
            raise SensitiveDataError(
                "Windows could not unlock the local secrets. Use the original user and computer."
            )
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.memset(buffer, 0, ctypes.sizeof(buffer))
        if target.data:
            ctypes.memset(target.data, 0, target.size)
            kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def load_local_secrets():
    """Read existing DPAPI-protected material. Never silently replace a key."""
    try:
        protected = _private_file().read_bytes()
        material = json.loads(_dpapi(protected, decrypt=True))
        if set(material) != {"version", "key_id", "fernet_key", "api_token"}:
            raise ValueError
        if material["version"] != 1 or material["key_id"] != KEY_ID:
            raise ValueError
        if not isinstance(material["fernet_key"], str):
            raise ValueError
        Fernet(material["fernet_key"].encode("ascii"))
        if not isinstance(material["api_token"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", material["api_token"]
        ):
            raise ValueError
        return material
    except SensitiveDataError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise SensitiveDataError(
            "Local secrets are missing or invalid. Run python secure_setup.py. "
            "Keep an existing key file if encrypted records already exist."
        ) from None


def initialize_local_secrets():
    path = _private_file()
    if path.exists():
        load_local_secrets()
        return False
    material = {
        "version": 1,
        "key_id": KEY_ID,
        "fernet_key": Fernet.generate_key().decode("ascii"),
        "api_token": secrets.token_hex(32),
    }
    protected = _dpapi(json.dumps(material).encode("utf-8"), decrypt=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Exclusive creation preserves keys if two setup processes overlap.
        with path.open("xb") as handle:
            handle.write(protected)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        load_local_secrets()
        return False
    return True


def _cipher():
    material = load_local_secrets()
    return Fernet(material["fernet_key"].encode("ascii"))


def get_api_token():
    return load_local_secrets()["api_token"]


def decrypt_sensitive(order):
    """Authenticate and decrypt only for the immediate sensitive-entry scope."""
    assert_protected_order(order)
    try:
        payload = json.loads(
            _cipher().decrypt(order[ENVELOPE_FIELD]["token"].encode("ascii"))
        )
        if not isinstance(payload, dict) or set(payload) != {"customer_id", "SSN", "DOB"}:
            raise ValueError
        if payload.pop("customer_id") != order["Customer ID"]:
            raise ValueError
        validate_sensitive_values(payload)
        return payload
    except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError):
        raise SensitiveDataError(
            "Sensitive data could not be authenticated. Check the key and customer record."
        ) from None


def protect_order(order, customer_id):
    """Encrypt a raw fixture or authenticate an already protected record."""
    if not isinstance(order, dict):
        raise SensitiveDataError("Customer data must be an object.")
    if not isinstance(customer_id, str) or not CUSTOMER_ID_PATTERN.fullmatch(customer_id):
        raise SensitiveDataError("Invalid customer ID.")
    reject_card_fields(order)
    if ENVELOPE_FIELD in order:
        assert_protected_order(order, customer_id)
        private = decrypt_sensitive(order)
        private.clear()
        return dict(order)
    if "Customer ID" in order and order["Customer ID"] != customer_id:
        raise SensitiveDataError("Customer ID does not match its record.")
    private = {"SSN": order.get("SSN"), "DOB": order.get("DOB")}
    validate_sensitive_values(private)
    payload = {"customer_id": customer_id, **private}
    token = _cipher().encrypt(json.dumps(payload).encode("utf-8")).decode("ascii")
    protected = {key: value for key, value in order.items() if key not in {"SSN", "DOB"}}
    protected["Customer ID"] = customer_id
    protected[ENVELOPE_FIELD] = {
        "version": 1,
        "algorithm": "fernet",
        "key_id": KEY_ID,
        "token": token,
    }
    assert_protected_order(protected, customer_id)
    return protected


def require_private_browser_logging():
    """This runner must not launch with Playwright debug output enabled."""
    debug = os.environ.get("DEBUG", "")
    inspector = os.environ.get("PWDEBUG", "")
    if debug or inspector not in {"", "0"}:
        raise SensitiveDataError(
            "Clear DEBUG and PWDEBUG before running sensitive form entry."
        )


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SensitiveDataError("Duplicate JSON keys are not allowed.")
        result[key] = value
    return result
