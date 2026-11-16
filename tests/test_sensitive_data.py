"""Offline regressions. DPAPI is replaced with temporary keys in these tests."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

import api
import database as db
import migrate_sensitive_db
import new
import protect_orders
import run_from_api
import sensitive_data as sensitive
from order_workflow import prepare_order, read_orders


@pytest.mark.parametrize("choice,expected", [("yes", "Yes"), ("YES", "Yes"),
    (" Yes ", "Yes"), ("no", "No"), (" NO ", "No")])
def test_ez_normalization(choice, expected, raw_order, material):
    raw_order["EZ Enroll"] = choice
    raw_order["EZ Enroll Disclosure Read"] = True
    result = prepare_order("5", raw_order, allow_plaintext=True)
    assert result["EZ Enroll"] == expected
    assert raw_order["EZ Enroll"] == choice


@pytest.mark.parametrize("choice", ["maybe", "", True, None, 1])
def test_ez_invalid(choice, raw_order):
    raw_order["EZ Enroll"] = choice
    with pytest.raises(sensitive.SensitiveDataError, match="Customer 5: EZ Enroll:"):
        prepare_order("5", raw_order, allow_plaintext=True)


@pytest.mark.parametrize("flag", [False, None, "true", 1])
def test_ez_disclosure_requires_explicit_boolean(flag, raw_order):
    raw_order.update({"EZ Enroll": "Yes", "EZ Enroll Disclosure Read": flag})
    with pytest.raises(ValueError, match="EZ Enroll Disclosure Read"):
        new.validate_order(raw_order, allow_plaintext=True)


@pytest.mark.parametrize("ez", ["Yes", "No"])
@pytest.mark.parametrize("flag", [False, None, "true", 1, "missing"])
def test_disclosures_follow_selected_branch(raw_order, ez, flag):
    raw_order.update({"EZ Enroll": ez, "EZ Enroll Disclosure Read": True,
                      "Automatic Payments Disclosure Read": flag})
    if flag == "missing":
        del raw_order["Automatic Payments Disclosure Read"]
    with pytest.raises(ValueError, match="Automatic Payments Disclosure Read"):
        new.validate_order(raw_order, allow_plaintext=True)
    raw_order["Automatic Payments Disclosure Read"] = True
    new.validate_order(raw_order, allow_plaintext=True)
    raw_order["Automatic Payments"] = "No"
    del raw_order["Automatic Payments Disclosure Read"]
    new.validate_order(raw_order, allow_plaintext=True)


def test_json_syntax_location_is_safe(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{\n "private": invalid}', encoding="utf-8")
    with pytest.raises(sensitive.SensitiveDataError, match="line 2, column 13"):
        read_orders(path)


@pytest.fixture
def material(monkeypatch):
    keys = {
        "version": 1, "key_id": sensitive.KEY_ID,
        "fernet_key": Fernet.generate_key().decode("ascii"), "api_token": "a" * 64,
    }
    monkeypatch.setattr(sensitive, "load_local_secrets", lambda: dict(keys))
    return keys


@pytest.fixture
def raw_order():
    order = {key: "" for key in new.BASIC_FIELDS}
    order.update({
        "First Name": "Demo", "Last Name": "Customer", "Zip": "12345",
        "Address 1": "14 Sample Road", "City": "Schenectady", "State": "NY",
        "Dwelling Type": "House", "Own / Rent": "Own", "Moving?": "No",
        "Customer Type": "New", "Current Providers": [], "Provider": "ProviderXX",
        "Provider ID": "15501641", "Plan": "300 Mbps (1-YEAR PRICING)",
        "Base Price": "40.00", "Video": "None", "Video Price": "0.00",
        "Phone": "None", "Phone Price": "0.00", "Equipment": "Leased Gateway",
        "Account Holder Verified": True, "Existing ProviderXX Customer": "No",
        "EZ Enroll": "No", "Automatic Payments": "Yes",
        "Automatic Payments Disclosure Read": True,
        "Mailing Same As Service": "Yes", "Shipping Same As Service": "Yes",
        "Phone Number": "2025550141", "Email Address": "demo@example.com",
        "SSN": "000-00-0000", "DOB": "1992-03-12",
        "Installation Type": "STANDARD_SHIPPING",
    })
    return order


@pytest.fixture
def protected(material, raw_order):
    return sensitive.protect_order(raw_order, "1")


@pytest.fixture
def database(tmp_path, monkeypatch, material):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "secure.db")
    db.init_db()
    return db.DB_PATH


@pytest.fixture
def client(database, material):
    with TestClient(api.app) as client:
        client.headers["Authorization"] = "Bearer " + material["api_token"]
        yield client


def test_round_trip_and_no_in_place_plaintext_mutation(material, raw_order):
    original = copy.deepcopy(raw_order)
    protected = sensitive.protect_order(raw_order, "1")
    assert raw_order == original
    new.validate_order(protected)
    assert sensitive.decrypt_sensitive(protected) == {
        "SSN": raw_order["SSN"], "DOB": raw_order["DOB"]
    }
    encoded = json.dumps(protected)
    assert raw_order["SSN"] not in encoded
    assert raw_order["DOB"] not in encoded
    assert "SSN" not in protected and "DOB" not in protected
    assert sensitive.protect_order(protected, "1") == protected
    assert sensitive.protect_order(raw_order, "1") != protected


def test_plaintext_validation_requires_explicit_import_mode(raw_order):
    with pytest.raises(sensitive.SensitiveDataError):
        new.validate_order(raw_order)
    new.validate_order(raw_order, allow_plaintext=True)


def test_tampered_ciphertext_fails(protected):
    protected["Sensitive Data"]["token"] = "x" * 200
    with pytest.raises(sensitive.SensitiveDataError):
        sensitive.decrypt_sensitive(protected)


def test_wrong_key_fails(material, protected):
    material["fernet_key"] = Fernet.generate_key().decode("ascii")
    with pytest.raises(sensitive.SensitiveDataError):
        sensitive.decrypt_sensitive(protected)


def test_record_binding_and_mixed_payload_fail(protected):
    with pytest.raises(sensitive.SensitiveDataError):
        sensitive.protect_order(protected, "2")
    protected["Customer ID"] = "2"
    with pytest.raises(sensitive.SensitiveDataError):
        sensitive.decrypt_sensitive(protected)
    protected["SSN"] = "000-00-0000"
    with pytest.raises(sensitive.SensitiveDataError):
        sensitive.assert_protected_order(protected)


@pytest.mark.parametrize("key", ["CVV", "Card Number", "Payment Card", "CVC", "PAN"])
def test_card_data_is_rejected(raw_order, material, key):
    raw_order["Payment"] = {key: "test-only"}
    with pytest.raises(sensitive.SensitiveDataError):
        sensitive.protect_order(raw_order, "1")


def test_import_batch_rolls_back_on_plaintext(database, protected, raw_order):
    with pytest.raises(sensitive.SensitiveDataError):
        db.import_orders({"1": protected, "2": raw_order})
    assert db.list_orders() == []


def test_storage_and_snapshot_are_ciphertext(database, protected, raw_order):
    db.import_orders({"1": protected})
    run = db.start_run("1", str(uuid4()))
    assert run["order"] == protected
    with db.connection() as con:
        payload = con.execute("SELECT payload FROM orders").fetchone()[0]
        snapshot = con.execute("SELECT order_snapshot FROM runs").fetchone()[0]
    for text in (payload, snapshot):
        assert raw_order["SSN"] not in text and raw_order["DOB"] not in text
    db.check_stored_protection()


def test_api_auth_and_no_plaintext_echo(client, raw_order, protected):
    client.headers.pop("Authorization")
    assert client.get("/orders").status_code == 401
    assert client.post("/orders", json={"id": "1", "data": protected}).status_code == 401
    client.headers["Authorization"] = "Bearer " + "a" * 64
    response = client.post("/orders", json={"id": "1", "data": raw_order})
    assert response.status_code == 422
    assert raw_order["SSN"] not in response.text
    assert raw_order["DOB"] not in response.text
    assert client.post("/orders", json={"id": "1", "data": protected}).status_code == 201
    response = client.get("/orders/1")
    assert response.json()["data"] == protected
    assert response.headers["Cache-Control"] == "no-store"


def test_api_rejects_tamper_and_sensitive_stage(client, protected):
    tampered = copy.deepcopy(protected)
    tampered["Sensitive Data"]["token"] = "x" * 200
    assert client.post("/orders", json={"id": "1", "data": tampered}).status_code == 422
    client.post("/orders", json={"id": "1", "data": protected})
    run_id = str(uuid4())
    assert client.post("/orders/1/runs", json={"run_id": run_id}).status_code == 200
    response = client.patch(
        "/runs/" + run_id, json={"status": "running", "stage": "SSN=000-00-0000"}
    )
    assert response.status_code == 422
    assert "000-00-0000" not in response.text


def test_concurrent_claims_and_terminal_updates(database, protected):
    db.import_orders({"1": protected})
    def claim(_):
        try:
            return db.start_run("1", str(uuid4()))["id"]
        except db.Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, range(2)))
    assert sum(result is not None for result in results) == 1
    run_id = next(result for result in results if result is not None)
    with pytest.raises(db.Conflict):
        db.import_orders({"1": protected}, update=True)
    db.update_run(run_id, "installation_completed", "installation Forward clicked")
    db.update_run(run_id, "installation_completed", "installation Forward clicked")
    with pytest.raises(db.Conflict):
        db.update_run(run_id, "running", "opening simulator")


def test_json_protection_initial_export(tmp_path, raw_order, material):
    source = tmp_path / "orders.json"
    output = tmp_path / "orders.encrypted.json"
    source.write_text(json.dumps({"1": raw_order}))
    original = source.read_bytes()
    assert protect_orders.protect_file(source, output) == 1
    assert source.read_bytes() == original
    assert raw_order["SSN"] not in output.read_text()
    assert raw_order["DOB"] not in output.read_text()
    exported = read_orders(output)
    assert set(exported) == {"1"}
    assert sensitive.decrypt_sensitive(exported["1"]) == {key: raw_order[key] for key in ("SSN", "DOB")}
    assert set(tmp_path.iterdir()) == {source, output}


def test_json_protection_atomic_replacement(tmp_path, raw_order, material, monkeypatch):
    source = tmp_path / "orders.json"
    output = tmp_path / "orders.encrypted.json"
    source.write_text(json.dumps({"1": raw_order}))
    protect_orders.protect_file(source)
    previous = output.read_bytes()
    source.write_text(json.dumps({"1": raw_order, "2": raw_order}))
    original = source.read_bytes()
    replace = protect_orders.os.replace
    calls = []

    def observe_replace(temporary, destination):
        assert temporary.parent.resolve() == destination.parent.resolve()
        assert output.read_bytes() == previous
        batch = read_orders(temporary)
        assert set(batch) == {"1", "2"}
        for customer_id, order in batch.items():
            assert order["Customer ID"] == customer_id
            assert sensitive.decrypt_sensitive(order) == {key: raw_order[key] for key in ("SSN", "DOB")}
        calls.append(temporary)
        replace(temporary, destination)

    monkeypatch.setattr(protect_orders.os, "replace", observe_replace)
    assert protect_orders.protect_file(source, output) == 2
    assert len(calls) == 1
    assert not calls[0].exists()
    assert output.read_bytes() != previous
    assert source.read_bytes() == original
    assert set(tmp_path.iterdir()) == {source, output}


@pytest.mark.parametrize("failure", ["validation", "encryption", "fsync", "replace"])
def test_json_protection_failure_preserves_export_and_cleans_temp(tmp_path, raw_order, material, monkeypatch, failure):
    source = tmp_path / "orders.json"
    output = tmp_path / "orders.encrypted.json"
    source.write_text(json.dumps({"1": raw_order}))
    protect_orders.protect_file(source, output)
    previous = output.read_bytes()
    batch = {"1": dict(raw_order), "2": dict(raw_order)}
    if failure == "validation":
        batch["2"]["EZ Enroll"] = "invalid"
    elif failure == "encryption":
        cipher = sensitive._cipher()

        class FailingCipher:
            calls = 0

            def encrypt(self, payload):
                self.calls += 1
                if self.calls == 2:
                    raise sensitive.SensitiveDataError("Synthetic encryption failure.")
                return cipher.encrypt(payload)

        failing = FailingCipher()
        monkeypatch.setattr(sensitive, "_cipher", lambda: failing)
    else:
        def fail(*args):
            raise OSError("Synthetic filesystem failure.")
        monkeypatch.setattr(protect_orders.os, failure, fail)
    source.write_text(json.dumps(batch))
    original = source.read_bytes()
    with pytest.raises((sensitive.SensitiveDataError, OSError)):
        protect_orders.protect_file(source, output)
    assert output.read_bytes() == previous
    assert source.read_bytes() == original
    assert set(tmp_path.iterdir()) == {source, output}


@pytest.mark.parametrize("target", ["same_source", "editable_name", "hardlink", "scenario_export"])
def test_json_protection_rejects_source_overwrite_and_variant_names(tmp_path, raw_order, material, target):
    source = tmp_path / "orders.json"
    source.write_text(json.dumps({"1": raw_order}))
    if target == "same_source":
        output = source
    elif target == "editable_name":
        source = tmp_path / "input.json"
        source.write_text(json.dumps({"1": raw_order}))
        output = tmp_path / "orders.json"
    elif target == "hardlink":
        output = tmp_path / "orders.encrypted.json"
        protect_orders.os.link(source, output)
    else:
        output = tmp_path / "orders.encrypted.variant.json"
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ValueError):
        protect_orders.protect_file(source, output)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def make_legacy(path, raw_order, active=False):
    previous = db.DB_PATH
    db.DB_PATH = path
    db.init_db()
    try:
        with db.connection() as con:
            con.execute(
                "INSERT INTO orders VALUES (?,?,?,?)",
                ("1", json.dumps(raw_order), "created", "updated"),
            )
            con.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "old-run", "1", "running" if active else "failed",
                    "legacy stage containing a value", "automation_error",
                    "started", "updated", None if active else "finished",
                    json.dumps(raw_order),
                ),
            )
    finally:
        db.DB_PATH = previous


def test_legacy_migration_preserves_history_and_source(database, tmp_path, raw_order):
    legacy = tmp_path / "legacy.db"
    output = tmp_path / "migrated.db"
    make_legacy(legacy, raw_order)
    assert migrate_sensitive_db.migrate(legacy, output) == (1, 1)
    with sqlite3.connect(output) as con:
        snapshot, stage = con.execute("SELECT order_snapshot,stage FROM runs").fetchone()
    assert raw_order["SSN"] not in snapshot and raw_order["DOB"] not in snapshot
    assert stage == "legacy stage omitted"
    with sqlite3.connect(legacy) as con:
        assert raw_order["SSN"] in con.execute("SELECT payload FROM orders").fetchone()[0]
    with pytest.raises(FileExistsError):
        migrate_sensitive_db.migrate(legacy, output)


def test_legacy_protection_guard_and_active_migration(database, tmp_path, raw_order):
    legacy = tmp_path / "legacy.db"
    output = tmp_path / "migrated.db"
    make_legacy(legacy, raw_order, active=True)
    with pytest.raises(ValueError):
        migrate_sensitive_db.migrate(legacy, output)
    assert not output.exists()
    previous = db.DB_PATH
    db.DB_PATH = legacy
    try:
        with pytest.raises(sensitive.SensitiveDataError):
            db.check_stored_protection()
    finally:
        db.DB_PATH = previous


def test_debug_logging_guard(monkeypatch):
    monkeypatch.setenv("DEBUG", "pw:api")
    with pytest.raises(sensitive.SensitiveDataError):
        sensitive.require_private_browser_logging()


def test_key_setup_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "secrets.dpapi"
    wrapping = Fernet(Fernet.generate_key())
    monkeypatch.setattr(sensitive, "_private_file", lambda: path)
    monkeypatch.setattr(
        sensitive, "_dpapi",
        lambda data, *, decrypt: wrapping.decrypt(data) if decrypt else wrapping.encrypt(data),
    )
    assert sensitive.initialize_local_secrets() is True
    original = path.read_bytes()
    keys = sensitive.load_local_secrets()
    assert sensitive.initialize_local_secrets() is False
    assert path.read_bytes() == original
    assert keys == sensitive.load_local_secrets()


def test_controlled_browser_error_hides_values(monkeypatch):
    class Expectation:
        def to_have_count(self, count):
            pass
        def to_be_editable(self):
            pass
    class Field:
        def evaluate(self, code):
            pass
        def fill(self, value):
            raise ValueError("Unexpected value 000-00-0000")
    monkeypatch.setattr(new, "expect", lambda field: Expectation())
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("PWDEBUG", raising=False)
    with pytest.raises(sensitive.SensitiveDataError) as caught:
        new.fill_sensitive_field(Field(), "000-00-0000", "SSN")
    assert "000-00-0000" not in str(caught.value)


@pytest.mark.parametrize("from_order", [False, True])
def test_sample_payment_stage_defers_installation_forward(monkeypatch, from_order):
    calls = []
    monkeypatch.setattr(run_from_api.sys, "argv", ["new.py"] if from_order else ["new.py", "--sample-payment"])
    monkeypatch.setattr(
        new, "complete_installation",
        lambda page, order, *, advance=True: calls.append(("installation", advance)),
    )
    monkeypatch.setattr(new, "complete_sample_payment", lambda page, order: calls.append("payment"))
    steps = list(run_from_api.build_steps(object(), {"Sample Payment": True} if from_order else {}))
    assert len(steps) == 11
    steps[-2][1]()
    steps[-1][1]()
    assert calls == [("installation", False), "payment"]


@pytest.mark.parametrize("flag", ["true", 1, None])
def test_sample_payment_flag_requires_boolean(raw_order, flag):
    raw_order["Sample Payment"] = flag
    with pytest.raises(ValueError, match="Sample Payment"):
        new.validate_order(raw_order, allow_plaintext=True)


def test_regular_installation_keeps_its_forward(monkeypatch):
    calls = []
    monkeypatch.setattr(run_from_api.sys, "argv", ["new.py"])
    monkeypatch.setattr(
        new, "complete_installation",
        lambda page, order, *, advance=True: calls.append(advance),
    )
    steps = list(run_from_api.build_steps(object(), {}))
    assert len(steps) == 10
    steps[-1][1]()
    assert calls == [True]


def test_sample_payment_rejects_other_hosts():
    class Page:
        url = "https://example.com/checkout"
    with pytest.raises(sensitive.SensitiveDataError):
        new.complete_sample_payment(Page(), {})
