"""Standards-based compact JWE helpers and DPAPI-protected RSA key storage."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
from uuid import uuid4

from jwcrypto import jwk, jwe

from sensitive_data import SensitiveDataError, _dpapi
from sensitive_schemas import validate_sensitive_payload

ALG = "RSA-OAEP-256"
ENC = "A256GCM"
KEY_BITS = 3072
MAX_JWE_BYTES = 32 * 1024
VALIDITY_SECONDS = 120
_KEY_NAME = "cse-rsa-3072.dpapi"
_key_lock = threading.RLock()
_cached = None


class JWEError(SensitiveDataError):
    """Safe JWE error with no cryptographic or customer values."""


def _key_path() -> Path:
    if os.name != "nt" or not os.environ.get("LOCALAPPDATA"):
        raise JWEError("Protected encryption keys are unavailable on this account.")
    return Path(os.environ["LOCALAPPDATA"]) / "AllconnectAutomation" / _KEY_NAME


def _load_or_create_key():
    global _cached
    with _key_lock:
        if _cached is not None:
            return _cached
        path = _key_path()
        if path.exists():
            try:
                material = json.loads(_dpapi(path.read_bytes(), decrypt=True))
                if set(material) != {"current_kid", "keys"} or not isinstance(material["keys"], dict):
                    raise ValueError
                private_keys = {}
                for key_id, encoded in material["keys"].items():
                    private_key = jwk.JWK.from_json(encoded)
                    public_numbers = private_key.export(as_dict=True)
                    modulus = public_numbers.get("n", "")
                    modulus_bytes = base64.urlsafe_b64decode(
                        modulus + "=" * (-len(modulus) % 4)
                    ) if isinstance(modulus, str) else b""
                    if public_numbers.get("kty") != "RSA" or int.from_bytes(modulus_bytes, "big").bit_length() < KEY_BITS:
                        raise ValueError
                    expected_id = "rsa-" + private_key.thumbprint()[:24]
                    if key_id != expected_id:
                        raise ValueError
                    private_keys[key_id] = private_key
                current_kid = material["current_kid"]
                private = private_keys[current_kid]
                public = private.export_public(as_dict=True)
                _cached = (private, current_kid, public, private_keys)
                return _cached
            except Exception:
                raise JWEError("Protected encryption keys are invalid; no replacement was created.") from None

        path.parent.mkdir(parents=True, exist_ok=True)
        private = jwk.JWK.generate(kty="RSA", size=KEY_BITS)
        kid = "rsa-" + private.thumbprint()[:24]
        protected = _dpapi(json.dumps({
            "current_kid": kid,
            "keys": {kid: private.export(private_key=True)},
        }).encode("utf-8"), decrypt=False)
        try:
            with path.open("xb") as handle:
                handle.write(protected)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            # Another process completed first. Load its usable key; never replace it.
            return _load_or_create_key()
        public = private.export_public(as_dict=True)
        _cached = (private, kid, public, {kid: private})
        return _cached


def public_key_document():
    _, kid, public, _ = _load_or_create_key()
    return {"kid": kid, "jwk": {**public, "kid": kid}, "alg": ALG, "enc": ENC}


def rotate_rsa_key():
    """Add a new active key while retaining old kids for short-lived JWE decrypts."""
    global _cached
    with _key_lock:
        _load_or_create_key()
        path = _key_path()
        material = json.loads(_dpapi(path.read_bytes(), decrypt=True))
        private = jwk.JWK.generate(kty="RSA", size=KEY_BITS)
        kid = "rsa-" + private.thumbprint()[:24]
        material["keys"][kid] = private.export(private_key=True)
        material["current_kid"] = kid
        encrypted = _dpapi(json.dumps(material).encode("utf-8"), decrypt=False)
        fd, temp_name = tempfile.mkstemp(prefix=".cse-key-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            Path(temp_name).unlink(missing_ok=True)
        _cached = None
        return public_key_document()


def encrypt_sensitive_payload(payload, *, order_id: str, run_id: str, payment_required=True, public_document=None):
    validate_sensitive_payload(payload, payment_required=payment_required)
    if public_document is None:
        public_document = public_key_document()
    if public_document.get("alg") != ALG or public_document.get("enc") != ENC:
        raise JWEError("The encryption endpoint returned unsupported algorithms.")
    kid = public_document.get("kid")
    public = public_document.get("jwk")
    if not isinstance(kid, str) or not isinstance(public, dict) or public.get("kid") != kid:
        raise JWEError("The encryption endpoint returned an invalid key identifier.")
    try:
        recipient = jwk.JWK.from_json(json.dumps(public))
        now = int(time.time())
        claims = {
            "order_id": str(order_id), "run_id": str(run_id), "iat": now,
            "exp": now + VALIDITY_SECONDS, "jti": uuid4().hex,
            "payment_required": bool(payment_required), "data": payload,
        }
        message = jwe.JWE(
            json.dumps(claims, separators=(",", ":")).encode("utf-8"),
            protected={"alg": ALG, "enc": ENC, "kid": kid, "typ": "JWE"},
        )
        message.add_recipient(recipient)
        token = message.serialize(compact=True)
        if len(token.encode("ascii")) > MAX_JWE_BYTES:
            raise JWEError("Encrypted sensitive payload is too large.")
        return token
    except JWEError:
        raise
    except Exception:
        raise JWEError("Sensitive payload encryption failed.") from None


def _consume_jti(jti: str, expires: int):
    now = int(time.time())
    # Persist only a one-way digest of the random request ID, never the JWE or
    # decrypted payload. SQLite makes single-use enforcement survive restarts
    # and coordinate concurrent API workers.
    import sqlite3
    import database as db

    digest = hashlib.sha256(jti.encode("ascii")).hexdigest()
    try:
        with db.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM sensitive_replay WHERE expires_at < ?", (now,))
            con.execute(
                "INSERT INTO sensitive_replay(jti_hash,expires_at) VALUES (?,?)",
                (digest, expires),
            )
    except sqlite3.IntegrityError:
        raise JWEError("Sensitive submission was already used.") from None
    except Exception:
        raise JWEError("Sensitive submission could not be verified.") from None


def decrypt_sensitive_submission(token: str, *, order_id: str, run_id: str, payment_required=None):
    if not isinstance(token, str) or not token or len(token.encode("utf-8")) > MAX_JWE_BYTES:
        raise JWEError("Encrypted sensitive payload is invalid.")
    _, _, _, private_keys = _load_or_create_key()
    try:
        parts = token.split(".")
        if len(parts) != 5:
            raise ValueError
        message = jwe.JWE()
        message.deserialize(token)
        header = message.jose_header
        if not isinstance(header, dict) or set(header) != {"alg", "enc", "kid", "typ"}:
            raise ValueError
        if (
            set(header) != {"alg", "enc", "kid", "typ"}
            or header.get("alg") != ALG or header.get("enc") != ENC
            or header.get("typ") != "JWE" or header.get("kid") not in private_keys
        ):
            raise ValueError
        message.decrypt(private_keys[header["kid"]])
        raw = message.payload
        if len(raw) > 8192:
            raise ValueError
        claims = json.loads(raw)
        if not isinstance(claims, dict) or set(claims) != {"order_id", "run_id", "iat", "exp", "jti", "payment_required", "data"}:
            raise ValueError
        now = int(time.time())
        required_payment = claims.get("payment_required")
        if (
            claims["order_id"] != str(order_id)
            or claims["run_id"] != str(run_id)
            or type(required_payment) is not bool
            or (payment_required is not None and required_payment is not payment_required)
            or type(claims["iat"]) is not int
            or type(claims["exp"]) is not int
            or claims["iat"] > now + 5
            or claims["exp"] < now
            or claims["exp"] - claims["iat"] > VALIDITY_SECONDS
            or now - claims["iat"] > VALIDITY_SECONDS
            or not isinstance(claims["jti"], str)
            or len(claims["jti"]) != 32
        ):
            raise ValueError
        payload = validate_sensitive_payload(claims["data"], payment_required=required_payment)
        _consume_jti(claims["jti"], claims["exp"])
        return payload, required_payment
    except JWEError:
        raise
    except Exception:
        raise JWEError("Encrypted sensitive payload could not be verified.") from None


def clear_sensitive_mapping(payload):
    """Best-effort removal of references; Python cannot guarantee zeroization."""
    if isinstance(payload, dict):
        for key in tuple(payload):
            payload[key] = "" if isinstance(payload[key], str) else None
        payload.clear()
