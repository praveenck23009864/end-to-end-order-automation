"""JWE regressions use fabricated values and a temporary DPAPI test wrapper."""

import base64
import copy
import json
import time
from uuid import uuid4

from cryptography.fernet import Fernet
from jwcrypto import jwk, jwe
import pytest
from fastapi.testclient import TestClient

import api
import database as db
import jwe_crypto as crypto
import sensitive_data as secrets_store
from sensitive_schemas import SENSITIVE_FIELDS, SensitivePayloadError, validate_sensitive_payload


@pytest.fixture
def api_material(monkeypatch):
    material = {
        "version": 1, "key_id": secrets_store.KEY_ID,
        "fernet_key": Fernet.generate_key().decode(), "api_token": "b" * 64,
    }
    monkeypatch.setattr(secrets_store, "load_local_secrets", lambda: dict(material))
    return material


@pytest.fixture
def database(tmp_path, monkeypatch, api_material):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jwe-api-test.db")
    db.init_db()
    return db.DB_PATH


@pytest.fixture
def client(database, api_material):
    with TestClient(api.app) as test_client:
        test_client.headers["Authorization"] = "Bearer " + api_material["api_token"]
        yield test_client


@pytest.fixture
def key_store(tmp_path, monkeypatch):
    wrapper = Fernet(Fernet.generate_key())
    monkeypatch.setattr(crypto, "_key_path", lambda: tmp_path / "cse-rsa-test.dpapi")
    monkeypatch.setattr(
        crypto, "_dpapi",
        lambda data, *, decrypt: wrapper.decrypt(data) if decrypt else wrapper.encrypt(data),
    )
    monkeypatch.setattr(crypto, "_cached", None)
    return crypto.public_key_document()


@pytest.fixture
def replay_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jwe-replay-test.db")
    db.init_db()
    return db.DB_PATH


@pytest.fixture
def synthetic_payload():
    return {
        "ssn": "999999999", "dob": "1990-01-02", "card_type": "Visa",
        "card_number": "4242424242424242", "cardholder_name": "SYNTHETIC TEST",
        "cvv": "999", "expiration_month": "12", "expiration_year": "2030",
    }


def _claims(order_id, run_id, payload, **overrides):
    now = int(time.time())
    value = {
        "order_id": str(order_id), "run_id": str(run_id), "iat": now,
        "exp": now + crypto.VALIDITY_SECONDS, "jti": uuid4().hex,
        "payment_required": True, "data": payload,
    }
    value.update(overrides)
    return value


def _make_compact(public_document, claims, *, recipient=None, header=None):
    key = recipient or jwk.JWK.from_json(json.dumps(public_document["jwk"]))
    token = jwe.JWE(
        json.dumps(claims).encode(),
        protected=header or {
            "alg": crypto.ALG, "enc": crypto.ENC,
            "kid": public_document["kid"], "typ": "JWE",
        },
    )
    token.add_recipient(key)
    return token.serialize(compact=True)


def _replace_header(compact, **changes):
    parts = compact.split(".")
    header = json.loads(base64.urlsafe_b64decode(parts[0] + "=="))
    header.update(changes)
    parts[0] = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    return ".".join(parts)


def test_encrypts_and_decrypts_standard_compact_jwe(key_store, synthetic_payload, replay_db):
    run_id = str(uuid4())
    token = crypto.encrypt_sensitive_payload(
        synthetic_payload, order_id="5", run_id=run_id, public_document=key_store
    )
    assert len(token.split(".")) == 5
    assert json.loads(base64.urlsafe_b64decode(token.split(".")[0] + "==")) == {
        "alg": "RSA-OAEP-256", "enc": "A256GCM", "kid": key_store["kid"], "typ": "JWE"
    }
    decrypted, payment_required = crypto.decrypt_sensitive_submission(
        token, order_id="5", run_id=run_id
    )
    assert decrypted == synthetic_payload
    assert payment_required is True


def test_each_encrypt_call_uses_a_fresh_content_encryption_key(key_store, synthetic_payload, monkeypatch):
    captured = []
    original = crypto.jwe.JWE.add_recipient

    def capture_cek(message, *args, **kwargs):
        result = original(message, *args, **kwargs)
        captured.append(bytes(message.cek))
        return result

    monkeypatch.setattr(crypto.jwe.JWE, "add_recipient", capture_cek)
    for _ in range(2):
        crypto.encrypt_sensitive_payload(
            synthetic_payload, order_id="5", run_id=str(uuid4()), public_document=key_store
        )
    assert len(captured) == 2 and captured[0] != captured[1]
    assert all(len(value) == 32 for value in captured)


def test_private_key_is_reused_and_rotation_retains_prior_kids(key_store, synthetic_payload, replay_db):
    path = crypto._key_path()
    original_blob = path.read_bytes()
    original_kid = key_store["kid"]
    assert "d" not in key_store["jwk"]
    assert crypto.public_key_document()["kid"] == original_kid
    assert path.read_bytes() == original_blob

    run_id = str(uuid4())
    old_token = crypto.encrypt_sensitive_payload(
        synthetic_payload, order_id="5", run_id=run_id, public_document=key_store
    )
    rotated = crypto.rotate_rsa_key()
    assert rotated["kid"] != original_kid
    plaintext, required = crypto.decrypt_sensitive_submission(
        old_token, order_id="5", run_id=run_id
    )
    assert plaintext == synthetic_payload and required is True


@pytest.mark.parametrize("segment", [3, 4])
def test_tampered_ciphertext_or_tag_is_rejected(key_store, synthetic_payload, segment):
    token = crypto.encrypt_sensitive_payload(
        synthetic_payload, order_id="5", run_id="run-1", public_document=key_store
    )
    parts = token.split(".")
    parts[segment] = ("A" if parts[segment][:1] != "A" else "B") + parts[segment][1:]
    with pytest.raises(crypto.JWEError):
        crypto.decrypt_sensitive_submission(".".join(parts), order_id="5", run_id="run-1")


def test_wrong_private_key_is_rejected(key_store, synthetic_payload):
    wrong = jwk.JWK.generate(kty="RSA", size=3072)
    token = _make_compact(
        key_store, _claims("5", "run-1", synthetic_payload), recipient=wrong,
    )
    with pytest.raises(crypto.JWEError):
        crypto.decrypt_sensitive_submission(token, order_id="5", run_id="run-1")


@pytest.mark.parametrize("header_change", [{"alg": "RSA1_5"}, {"enc": "A128GCM"}])
def test_unsupported_algorithms_are_rejected(key_store, synthetic_payload, header_change):
    token = crypto.encrypt_sensitive_payload(
        synthetic_payload, order_id="5", run_id="run-1", public_document=key_store
    )
    with pytest.raises(crypto.JWEError):
        crypto.decrypt_sensitive_submission(
            _replace_header(token, **header_change), order_id="5", run_id="run-1"
        )


def test_invalid_payload_schema_and_unexpected_billing_fields_are_rejected(key_store, synthetic_payload):
    invalid = copy.deepcopy(synthetic_payload)
    invalid.pop("cvv")
    token = _make_compact(key_store, _claims("5", "run-1", invalid))
    with pytest.raises(crypto.JWEError):
        crypto.decrypt_sensitive_submission(token, order_id="5", run_id="run-1")
    for billing_key in (
        "billing_same_as_service", "billing_address", "billing_city",
        "billing_state", "billing_zip",
    ):
        extra = {**synthetic_payload, billing_key: "fabricated"}
        with pytest.raises(SensitivePayloadError):
            validate_sensitive_payload(extra)
        with pytest.raises(SensitivePayloadError):
            crypto.encrypt_sensitive_payload(extra, order_id="5", run_id="run-1", public_document=key_store)


def test_expired_wrong_order_and_replayed_submissions_are_rejected(key_store, synthetic_payload, replay_db):
    now = int(time.time())
    expired = _make_compact(
        key_store,
        _claims("5", "run-1", synthetic_payload, iat=now - 180, exp=now - 60),
    )
    with pytest.raises(crypto.JWEError):
        crypto.decrypt_sensitive_submission(expired, order_id="5", run_id="run-1")
    token = crypto.encrypt_sensitive_payload(
        synthetic_payload, order_id="5", run_id="run-1", public_document=key_store
    )
    with pytest.raises(crypto.JWEError):
        crypto.decrypt_sensitive_submission(token, order_id="6", run_id="run-1")
    crypto.decrypt_sensitive_submission(token, order_id="5", run_id="run-1")
    with db.connection() as con:
        stored = con.execute("SELECT jti_hash FROM sensitive_replay").fetchall()
    assert len(stored) == 1 and len(stored[0][0]) == 64
    assert token not in stored[0][0]
    with pytest.raises(crypto.JWEError):
        crypto.decrypt_sensitive_submission(token, order_id="5", run_id="run-1")


def test_api_accepts_jwe_without_returning_or_persisting_plaintext_or_jwe(
    client, database, key_store, synthetic_payload, monkeypatch,
):
    captured = []
    monkeypatch.setattr(
        api, "public_key_document", lambda: key_store,
    )
    def capture_worker(run_id, order, payload, payment_required, worker_id):
        captured.append((run_id, payload, payment_required))
        db.unregister_run_worker(run_id, worker_id)

    monkeypatch.setattr(api, "_run_sensitive_workflow", capture_worker)
    order = {"Customer ID": "1", "EZ Enroll": "No", "Sample Payment": False}
    db.import_orders({"1": order})
    run_id = str(uuid4())
    assert client.post(f"/orders/1/runs", json={"run_id": run_id}).status_code == 200
    token = crypto.encrypt_sensitive_payload(
        synthetic_payload, order_id="1", run_id=run_id, public_document=key_store
    )
    response = client.post(f"/runs/{run_id}/sensitive", json={"jwe": token})
    assert response.status_code == 202
    assert response.json() == {"accepted": True}
    assert synthetic_payload["card_number"] not in response.text
    assert token not in response.text
    assert captured == [(run_id, synthetic_payload, True)]
    with db.connection() as con:
        persisted = " ".join(row[0] for row in con.execute("SELECT payload FROM orders"))
        persisted += " " + " ".join(row[0] for row in con.execute("SELECT order_snapshot FROM runs"))
    assert token not in persisted
    assert synthetic_payload["card_number"] not in persisted
