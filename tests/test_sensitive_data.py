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
    clean = copy.deepcopy(raw_order)
    clean.pop("SSN")
    clean.pop("DOB")
    result = prepare_order("5", clean)
    assert result["EZ Enroll"] == expected
    assert raw_order["EZ Enroll"] == choice


@pytest.mark.parametrize("choice", ["maybe", "", True, None, 1])
def test_ez_invalid(choice, raw_order):
    raw_order["EZ Enroll"] = choice
    raw_order.pop("SSN")
    raw_order.pop("DOB")
    raw_order["Customer ID"] = "5"
    with pytest.raises(sensitive.SensitiveDataError, match="Customer 5: EZ Enroll:"):
        prepare_order("5", raw_order)


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
    return safe_order(raw_order, "1")


def safe_order(order, customer_id):
    safe = copy.deepcopy(order)
    safe.pop("SSN", None)
    safe.pop("DOB", None)
    safe.pop("Sensitive Data", None)
    safe["Customer ID"] = str(customer_id)
    return safe


@pytest.fixture
def legacy_protected(material, raw_order):
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


def test_legacy_cipher_round_trip_is_not_used_for_order_storage(material, raw_order, legacy_protected):
    original = copy.deepcopy(raw_order)
    assert raw_order == original
    assert sensitive.decrypt_sensitive(legacy_protected) == {
        "SSN": raw_order["SSN"], "DOB": raw_order["DOB"]
    }
    encoded = json.dumps(legacy_protected)
    assert raw_order["SSN"] not in encoded
    assert raw_order["DOB"] not in encoded
    assert "SSN" not in legacy_protected and "DOB" not in legacy_protected


def test_plaintext_validation_requires_explicit_import_mode(raw_order):
    with pytest.raises(sensitive.SensitiveDataError):
        new.validate_order(raw_order)
    new.validate_order(raw_order, allow_plaintext=True)


def test_tampered_ciphertext_fails(legacy_protected):
    legacy_protected["Sensitive Data"]["token"] = "x" * 200
    with pytest.raises(sensitive.SensitiveDataError):
        sensitive.decrypt_sensitive(legacy_protected)


def test_wrong_key_fails(material, legacy_protected):
    material["fernet_key"] = Fernet.generate_key().decode("ascii")
    with pytest.raises(sensitive.SensitiveDataError):
        sensitive.decrypt_sensitive(legacy_protected)


def test_record_binding_and_mixed_payload_fail(legacy_protected):
    with pytest.raises(sensitive.SensitiveDataError):
        sensitive.protect_order(legacy_protected, "2")
    legacy_protected["Customer ID"] = "2"
    with pytest.raises(sensitive.SensitiveDataError):
        sensitive.decrypt_sensitive(legacy_protected)
    legacy_protected["SSN"] = "000-00-0000"
    with pytest.raises(sensitive.SensitiveDataError):
        sensitive.assert_protected_order(legacy_protected)


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
    assert "order" not in run
    with db.connection() as con:
        payload = con.execute("SELECT payload FROM orders").fetchone()[0]
        snapshot = con.execute("SELECT order_snapshot FROM runs").fetchone()[0]
    for text in (payload, snapshot):
        assert raw_order["SSN"] not in text and raw_order["DOB"] not in text
        assert "Sensitive Data" not in text
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
    tampered["SSN"] = "999999999"
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
    db.set_submission_state(run_id, "attempting")
    db.set_submission_state(run_id, "confirmed")
    db.update_run(run_id, "installation_completed", "order submitted")
    db.update_run(run_id, "installation_completed", "order submitted")
    with pytest.raises(db.Conflict):
        db.update_run(run_id, "running", "opening simulator")


def test_submission_confirmation_is_persisted_and_blocks_duplicate_run(database, protected):
    db.import_orders({"1": protected})
    run_id = str(uuid4())
    db.start_run("1", run_id)
    db.update_run(run_id, "running", "Authorization and Submit Order")
    db.set_submission_state(run_id, "attempting")
    db.set_submission_state(run_id, "confirmed")
    db.update_run(run_id, "installation_completed", "order submitted")
    result = db.get_run(run_id)
    assert result["submission_state"] == "confirmed"
    assert result["final_order_submitted"] is True
    with pytest.raises(db.Conflict, match="confirmed submission"):
        db.start_run("1", str(uuid4()))


def test_ambiguous_submission_requires_manual_resolution(database, protected):
    db.import_orders({"1": protected})
    run_id = str(uuid4())
    db.start_run("1", run_id)
    db.set_submission_state(run_id, "attempting")
    db.set_submission_state(run_id, "ambiguous")
    assert db.get_run(run_id)["status"] == "running"
    with pytest.raises(db.Conflict):
        db.start_run("1", str(uuid4()))
    recovered = db.resolve_ambiguous_submission(run_id, submitted=False)
    assert recovered["status"] == "failed"
    assert recovered["submission_state"] == "not_submitted"
    assert recovered["final_order_submitted"] is False
    assert db.start_run("1", str(uuid4()))["status"] == "running"


def _make_stale_run(protected, *, submission_state="not_attempted"):
    db.import_orders({"1": protected})
    run_id = str(uuid4())
    db.start_run("1", run_id)
    if submission_state != "not_attempted":
        db.set_submission_state(run_id, "attempting")
        db.set_submission_state(run_id, submission_state)
    with db.connection() as con:
        con.execute(
            "UPDATE runs SET updated_at=? WHERE id=?",
            ("2020-01-01T00:00:00+00:00", run_id),
        )
    return run_id


def test_no_active_run_allows_new_run(database, protected):
    db.import_orders({"1": protected})
    assert db.start_run("1", str(uuid4()))["status"] == "running"


def test_active_run_is_not_recovered_or_duplicated(database, protected):
    db.import_orders({"1": protected})
    run_id = str(uuid4())
    db.start_run("1", run_id)
    with pytest.raises(db.Conflict, match="active run"):
        db.start_run("1", str(uuid4()))
    with pytest.raises(db.Conflict, match="stale interval"):
        db.recover_stale_run(run_id, outcome="unknown", worker_stopped=True)


def test_stale_run_with_live_worker_is_not_recovered(database, protected):
    run_id = _make_stale_run(protected)
    db.register_run_worker(run_id, "worker-live", db.os.getpid())
    with pytest.raises(db.Conflict, match="active worker"):
        db.recover_stale_run(
            run_id, outcome="not_submitted", worker_stopped=True,
            local_worker_active=True,
        )


def test_recent_worker_heartbeat_blocks_recovery(database, protected):
    run_id = _make_stale_run(protected)
    db.register_run_worker(run_id, "worker-heartbeat", db.os.getpid())
    with pytest.raises(db.Conflict, match="active"):
        db.recover_stale_run(
            run_id, outcome="unknown", worker_stopped=True,
            local_worker_active=False,
        )


def test_stale_run_with_confirmed_stopped_worker_can_be_recovered(database, protected):
    run_id = _make_stale_run(protected)
    db.register_run_worker(run_id, "worker-dead", 2_000_000_000)
    with db.connection() as con:
        con.execute(
            "UPDATE run_workers SET heartbeat_at=?,active=1 WHERE run_id=?",
            ("2020-01-01T00:00:00+00:00", run_id),
        )
    recovered = db.recover_stale_run(
        run_id, outcome="not_submitted", worker_stopped=True
    )
    assert recovered["status"] == "failed"
    assert recovered["submission_state"] == "not_submitted"
    assert recovered["final_order_submitted"] is False


def test_stale_submission_outcomes_and_idempotence(database, protected):
    submitted_id = _make_stale_run(protected, submission_state="attempting")
    submitted = db.recover_stale_run(
        submitted_id, outcome="submitted", worker_stopped=True
    )
    assert submitted["status"] == "installation_completed"
    assert submitted["submission_state"] == "confirmed"
    assert submitted["final_order_submitted"] is True
    assert db.recover_stale_run(
        submitted_id, outcome="submitted", worker_stopped=True
    ) == submitted
    with pytest.raises(db.Conflict, match="confirmed submission"):
        db.start_run("1", str(uuid4()))


def test_unknown_stale_outcome_remains_blocked(database, protected):
    run_id = _make_stale_run(protected)
    unresolved = db.recover_stale_run(
        run_id, outcome="unknown", worker_stopped=True
    )
    assert unresolved["status"] == "running"
    assert unresolved["submission_state"] == "ambiguous"
    assert unresolved["final_order_submitted"] is False
    assert db.recover_stale_run(
        run_id, outcome="unknown", worker_stopped=True
    ) == unresolved
    with pytest.raises(db.Conflict, match="active run"):
        db.start_run("1", str(uuid4()))


def test_recovered_not_submitted_order_can_start_again(database, protected):
    run_id = _make_stale_run(protected)
    db.recover_stale_run(run_id, outcome="not_submitted", worker_stopped=True)
    assert db.start_run("1", str(uuid4()))["status"] == "running"


def test_recovery_requires_operator_worker_confirmation(database, protected):
    run_id = _make_stale_run(protected)
    with pytest.raises(db.Conflict, match="Confirm the API worker"):
        db.recover_stale_run(run_id, outcome="unknown", worker_stopped=False)


def test_authenticated_recovery_route_and_worker_authorization(client, database, protected, monkeypatch):
    db.import_orders({"1": protected})
    run_id = str(uuid4())
    db.start_run("1", run_id)
    with db.connection() as con:
        con.execute(
            "UPDATE runs SET updated_at=? WHERE id=?",
            ("2020-01-01T00:00:00+00:00", run_id),
        )

    response = client.post(
        f"/runs/{run_id}/recovery",
        json={"outcome": "unknown", "worker_stopped": True},
        headers={"Authorization": "Bearer invalid"},
    )
    assert response.status_code == 401

    monkeypatch.setattr(api, "is_local_worker_active", lambda _: True)
    active_response = client.post(
        f"/runs/{run_id}/recovery",
        json={"outcome": "unknown", "worker_stopped": True},
    )
    assert active_response.status_code == 409
    monkeypatch.setattr(api, "is_local_worker_active", lambda _: False)
    response = client.post(
        f"/runs/{run_id}/recovery",
        json={"outcome": "unknown", "worker_stopped": True},
    )
    assert response.status_code == 200
    assert response.json()["submission_state"] == "ambiguous"


def test_background_worker_lease_tracks_actual_work(database, protected, monkeypatch):
    db.import_orders({"1": protected})
    run_id = str(uuid4())
    db.start_run("1", run_id)
    worker_id = "synthetic-worker"
    db.register_run_worker(run_id, worker_id, db.os.getpid())
    observed = []

    def run_worker(*args):
        observed.append(api.is_local_worker_active(run_id))
        observed.append(db.get_run_worker(run_id)["active"])

    monkeypatch.setattr(run_from_api, "run_backend_workflow", run_worker)
    api._run_sensitive_workflow(run_id, protected, {}, False, worker_id)
    assert observed == [True, True]
    assert api.is_local_worker_active(run_id) is False
    assert db.get_run_worker(run_id)["active"] == 0


def test_api_submission_failure_closes_unstarted_run_safely(
    client, database, protected, monkeypatch,
):
    db.import_orders({"1": protected})
    run_id = str(uuid4())
    db.start_run("1", run_id)
    monkeypatch.setattr(
        api, "decrypt_sensitive_submission",
        lambda *args, **kwargs: (_ for _ in ()).throw(api.JWEError("synthetic reject")),
    )
    response = client.post(f"/runs/{run_id}/sensitive", json={"jwe": "synthetic-jwe"})
    assert response.status_code == 422
    run = db.get_run(run_id)
    assert run["status"] == "failed"
    assert run["stage"] == "preparing automation steps"
    assert run["error_code"] == "startup_error"
    assert run["submission_state"] == "not_attempted"


def test_browser_crash_during_payment_persists_failure(database, protected, monkeypatch):
    db.import_orders({"1": protected})
    run_id = str(uuid4())
    db.start_run("1", run_id)
    monkeypatch.setattr(run_from_api, "assert_non_sensitive_order", lambda *_: None)
    monkeypatch.setattr(new, "validate_order", lambda *_: None)

    class Page:
        def set_default_timeout(self, _): pass
        def goto(self, *args, **kwargs): pass
        def is_closed(self): return False

    class Browser:
        def new_page(self, **_): return Page()
        def is_connected(self): return False
        def close(self): pass

    class Chromium:
        def launch(self, **_): return Browser()

    class Playwright:
        chromium = Chromium()
        def start(self): return self
        def stop(self): pass
        def __enter__(self): return self
        def __exit__(self, *_): return False

    monkeypatch.setattr(run_from_api, "sync_playwright", Playwright)
    monkeypatch.setattr(run_from_api, "wait_for_session", lambda *_: None)

    def crash(*_):
        raise RuntimeError("Synthetic browser crash with card 4242424242424242")

    monkeypatch.setattr(
        run_from_api, "build_steps",
        lambda *args: [("Step 3 installation and payment", crash)],
    )
    run_from_api.run_backend_workflow(
        run_id, protected, {"card_number": "4242424242424242"}, True
    )
    result = db.get_run(run_id)
    assert result["status"] == "failed"
    assert result["stage"] == "Step 3 installation and payment"
    assert result["submission_state"] == "not_attempted"


def test_database_failure_logs_original_and_status_write_errors(
    database, protected, monkeypatch, capsys,
):
    db.import_orders({"1": protected})
    run_id = str(uuid4())
    db.start_run("1", run_id)
    monkeypatch.setattr(run_from_api, "assert_non_sensitive_order", lambda *_: None)
    monkeypatch.setattr(new, "validate_order", lambda *_: None)

    def fail_update(*args, **kwargs):
        raise sqlite3.OperationalError(
            "database locked while processing synthetic card 4242424242424242"
        )

    monkeypatch.setattr(db, "update_run", fail_update)
    run_from_api.run_backend_workflow(run_id, protected, {}, False)
    output = capsys.readouterr().out
    assert "WORKFLOW ERROR" in output
    assert "STATUS WRITE ERROR" in output
    assert "OperationalError" in output
    assert "database locked" in output
    assert "4242424242424242" not in output
    assert db.get_run(run_id)["status"] == "running"


def test_json_protection_initial_export(tmp_path, raw_order, material):
    source = tmp_path / "orders.json"
    output = tmp_path / "orders.encrypted.json"
    source.write_text(json.dumps({"1": safe_order(raw_order, "1")}))
    original = source.read_bytes()
    assert protect_orders.protect_file(source, output) == 1
    assert source.read_bytes() == original
    assert "SSN" not in output.read_text() and "DOB" not in output.read_text()
    exported = read_orders(output)
    assert set(exported) == {"1"}
    assert "Sensitive Data" not in json.dumps(exported)
    assert set(tmp_path.iterdir()) == {source, output}


def test_json_protection_atomic_replacement(tmp_path, raw_order, material, monkeypatch):
    source = tmp_path / "orders.json"
    output = tmp_path / "orders.encrypted.json"
    source.write_text(json.dumps({"1": safe_order(raw_order, "1")}))
    protect_orders.protect_file(source)
    previous = output.read_bytes()
    source.write_text(json.dumps({"1": safe_order(raw_order, "1"), "2": safe_order(raw_order, "2")}))
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
            assert "Sensitive Data" not in json.dumps(order)
        calls.append(temporary)
        replace(temporary, destination)

    monkeypatch.setattr(protect_orders.os, "replace", observe_replace)
    assert protect_orders.protect_file(source, output) == 2
    assert len(calls) == 1
    assert not calls[0].exists()
    assert output.read_bytes() != previous
    assert source.read_bytes() == original
    assert set(tmp_path.iterdir()) == {source, output}


@pytest.mark.parametrize("failure", ["validation", "fsync", "replace"])
def test_json_protection_failure_preserves_export_and_cleans_temp(tmp_path, raw_order, material, monkeypatch, failure):
    source = tmp_path / "orders.json"
    output = tmp_path / "orders.encrypted.json"
    source.write_text(json.dumps({"1": safe_order(raw_order, "1")}))
    protect_orders.protect_file(source, output)
    previous = output.read_bytes()
    batch = {"1": safe_order(raw_order, "1"), "2": safe_order(raw_order, "2")}
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
    source.write_text(json.dumps({"1": safe_order(raw_order, "1")}))
    if target == "same_source":
        output = source
    elif target == "editable_name":
        source = tmp_path / "input.json"
        source.write_text(json.dumps({"1": safe_order(raw_order, "1")}))
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
                """INSERT INTO runs (
                    id,order_id,status,stage,error_code,started_at,updated_at,
                    finished_at,order_snapshot
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
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


def test_migration_preserves_submission_tracking(database, tmp_path, raw_order):
    legacy = tmp_path / "tracked-source.db"
    output = tmp_path / "tracked-copy.db"
    make_legacy(legacy, raw_order)
    with sqlite3.connect(legacy) as con:
        con.execute(
            "UPDATE runs SET final_order_submitted=1,submission_state='confirmed' "
            "WHERE id='old-run'"
        )
    migrate_sensitive_db.migrate(legacy, output)
    with sqlite3.connect(output) as con:
        tracked = con.execute(
            "SELECT final_order_submitted,submission_state FROM runs WHERE id='old-run'"
        ).fetchone()
    assert tracked == (1, "confirmed")


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
    with pytest.raises(new.BrowserWorkflowError) as caught:
        new.fill_sensitive_field(Field(), "000-00-0000", "SSN")
    assert "000-00-0000" not in str(caught.value)


@pytest.mark.parametrize("from_order", [False, True])
def test_ez_enroll_no_keeps_installation_forward_with_sample_payment(monkeypatch, from_order):
    calls = []
    monkeypatch.setattr(run_from_api.sys, "argv", ["new.py"] if from_order else ["new.py", "--sample-payment"])
    monkeypatch.setattr(
        new, "complete_installation_and_payment",
        lambda page, order, sensitive=None, *, payment_required=True: calls.append(
            ("installation+payment", order["EZ Enroll"], payment_required)
        ),
    )
    order = {"EZ Enroll": "No", "Sample Payment": True} if from_order else {"EZ Enroll": "No"}
    steps = list(run_from_api.build_steps(object(), order))
    assert len(steps) == 11
    steps[-2][1]()
    assert calls == [("installation+payment", "No", True)]


@pytest.mark.parametrize("flag", ["true", 1, None])
def test_sample_payment_flag_requires_boolean(raw_order, flag):
    raw_order["Sample Payment"] = flag
    with pytest.raises(ValueError, match="Sample Payment"):
        new.validate_order(raw_order, allow_plaintext=True)


def test_regular_installation_keeps_its_forward(monkeypatch):
    calls = []
    monkeypatch.setattr(run_from_api.sys, "argv", ["new.py"])
    monkeypatch.setattr(
        new, "complete_installation_and_payment",
        lambda page, order, sensitive=None, *, payment_required=True: calls.append(
            (order["EZ Enroll"], payment_required)
        ),
    )
    steps = list(run_from_api.build_steps(object(), {"EZ Enroll": "No"}))
    assert len(steps) == 11
    steps[-2][1]()
    assert calls == [("No", False)]


def test_sample_payment_rejects_other_hosts():
    class Page:
        url = "https://example.com/checkout"
    with pytest.raises(sensitive.SensitiveDataError):
        new.complete_sample_payment(Page(), {})


@pytest.mark.parametrize("ez_enroll", ["Yes", "No"])
def test_api_worker_runs_payment_step_through_forward_and_completes(
    monkeypatch, database, raw_order, ez_enroll, capsys,
):
    order = safe_order(raw_order, "1")
    order.update({
        "EZ Enroll": ez_enroll,
        "EZ Enroll Disclosure Read": True,
        "Sample Payment": ez_enroll == "No",
        "Credit Card Billing Address Same as Service": "Yes",
    })
    db.import_orders({"1": order})
    run_id = str(uuid4())
    db.start_run("1", run_id)
    steps_seen = []

    class FakePage:
        url = "about:blank"

        def set_default_timeout(self, timeout):
            pass

        def goto(self, url, **kwargs):
            self.url = url

        def is_closed(self):
            return False

    class FakeBrowser:
        def __init__(self):
            self.page = FakePage()

        def new_page(self, **kwargs):
            return self.page

        def close(self):
            pass

    browser = FakeBrowser()

    class FakePlaywrightContext:
        def start(self):
            return self.__enter__()

        def stop(self):
            pass

        def __enter__(self):
            return type("Playwright", (), {
                "chromium": type("Chromium", (), {
                    "launch": staticmethod(lambda **kwargs: browser),
                })(),
            })()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(run_from_api, "sync_playwright", FakePlaywrightContext)
    monkeypatch.setattr(run_from_api, "wait_for_session", lambda page, run_id: None)
    for function_name in (
        "fill_basic_information", "confirm_address", "qualify_customer",
        "check_provider_availability", "select_internet_offer",
        "customize_products", "set_broadband_labels", "customize_equipment",
        "qualify_account_holder",
    ):
        monkeypatch.setattr(
            new, function_name,
            lambda *args, _name=function_name, **kwargs: steps_seen.append(_name),
        )
    def payment_step(page, order, sensitive=None, *, payment_required=True):
        assert order["Credit Card Billing Address Same as Service"] == "Yes"
        assert payment_required is True
        steps_seen.append("forward")
        print("PASS: Forward button clicked.")
        print("PASS: Authorization stage opened.")

    monkeypatch.setattr(new, "complete_installation_and_payment", payment_step)

    def authorization_step(page, given_order, given_run_id):
        db.set_submission_state(given_run_id, "attempting")
        db.set_submission_state(given_run_id, "confirmed")
        print("PASS: Simulator submission confirmed.")

    monkeypatch.setattr(run_from_api, "complete_authorization", authorization_step)

    run_from_api.run_backend_workflow(
        run_id, order,
        {"card_number": "synthetic-in-memory-only"},
        payment_required=True,
    )

    assert steps_seen.count("forward") == 1
    result = db.get_run(run_id)
    assert result["status"] == "installation_completed"
    assert result["stage"] == "order submitted"
    assert result["final_order_submitted"] is True
    output = capsys.readouterr().out
    assert "PASS: Forward button clicked." in output
    assert "PASS: Authorization stage opened." in output
    assert "PASS: Simulator submission confirmed." in output
    assert "Step 3 installation and payment" in output
    assert "verifying sample payment fields" not in output
    assert "credit check and installation" not in output
    assert "STOP: Review the simulator screen manually; final order was not submitted." not in output


def _customer_five_order(raw_order, material):
    value = safe_order(raw_order, "5")
    value["Customer ID"] = "5"
    value["EZ Enroll"] = "Yes"
    value["EZ Enroll Disclosure Read"] = True
    return value


def test_run_list_detail_and_status_updates(client, database, raw_order, material):
    protected = _customer_five_order(raw_order, material)
    assert client.post("/orders", json={"id": "5", "data": protected}).status_code == 201
    run_id = str(uuid4())
    assert client.post(f"/orders/5/runs", json={"run_id": run_id}).status_code == 200

    listed = next(item for item in client.get("/runs").json() if item["id"] == run_id)
    detail_response = client.get(f"/runs/{run_id}")
    assert detail_response.status_code == 200
    assert detail_response.json() == listed

    failed = client.patch(
        f"/runs/{run_id}",
        json={"status": "failed", "stage": "manual recovery", "error_code": "manual_recovery"},
    )
    assert failed.status_code == 200
    assert client.get(f"/runs/{run_id}").json()["status"] == "failed"


def test_run_can_complete_with_ez_review_stage(client, raw_order, material):
    protected = _customer_five_order(raw_order, material)
    client.post("/orders", json={"id": "5", "data": protected})
    run_id = str(uuid4())
    client.post("/orders/5/runs", json={"run_id": run_id})
    completed = client.patch(
        f"/runs/{run_id}",
        json={
            "status": "installation_completed",
            "stage": "EZ Enroll payment review completed",
        },
    )
    assert completed.status_code == 409
    assert "submission confirmation" in completed.text


def test_unknown_run_returns_404(client):
    assert client.get(f"/runs/{uuid4()}").status_code == 404
    response = client.patch(
        f"/runs/{uuid4()}",
        json={"status": "failed", "stage": "manual recovery", "error_code": "manual_recovery"},
    )
    assert response.status_code == 404


def test_completed_run_cannot_be_restarted(database, protected):
    db.import_orders({"1": protected})
    run_id = str(uuid4())
    db.start_run("1", run_id)
    db.set_submission_state(run_id, "attempting")
    db.set_submission_state(run_id, "confirmed")
    db.update_run(run_id, "installation_completed", "order submitted")
    assert db.start_run("1", run_id)["status"] == "installation_completed"
    with pytest.raises(db.Conflict):
        db.update_run(run_id, "running", "opening simulator")


def test_active_customer_five_run_blocks_second_claim(database, raw_order, material):
    protected_five = _customer_five_order(raw_order, material)
    db.import_orders({"5": protected_five})
    db.start_run("5", str(uuid4()))
    with pytest.raises(db.Conflict):
        db.start_run("5", str(uuid4()))


def test_ez_enroll_step_branching(monkeypatch):
    calls = []
    monkeypatch.setattr(run_from_api.sys, "argv", ["run_from_api.py", "--sample-payment"])
    monkeypatch.setattr(
        new, "complete_installation_and_payment",
        lambda page, order, sensitive=None, *, payment_required=True: calls.append(
            ("step3", order["EZ Enroll"], payment_required)
        ),
    )
    steps = list(run_from_api.build_steps(object(), {"EZ Enroll": " Yes ", "Sample Payment": True}))
    steps[-2][1]()
    assert calls == [("step3", " Yes ", True)]

    calls.clear()
    steps = list(run_from_api.build_steps(object(), {"EZ Enroll": " No ", "Sample Payment": True}))
    steps[-2][1]()
    assert calls == [("step3", " No ", True)]


def test_ez_enroll_review_never_clicks_forward_or_enters_card():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(new.verify_ez_enroll_payment))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "click_forward" not in names
    assert "complete_sample_payment" not in names
    assert not ({"fill", "press_sequentially", "fill_sensitive_field"} & attrs)


def test_workflow_progress_stages_are_registered():
    import ast
    import inspect

    source = inspect.getsource(run_from_api)
    tree = ast.parse(source)
    steps_function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_steps"
    )
    stages = set()
    for node in ast.walk(steps_function):
        if not isinstance(node, ast.Tuple) or len(node.elts) != 2:
            continue
        label, action = node.elts
        if isinstance(label, ast.Constant) and isinstance(label.value, str) and isinstance(action, ast.Lambda):
            stages.add(label.value)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "progress":
            continue
        if len(node.args) >= 3 and isinstance(node.args[2], ast.Constant):
            stages.add(node.args[2].value)
        if len(node.args) >= 2 and node.func.attr == "progress":
            # Positional status and stage are variable in record_failure; that
            # stage originates from the registered workflow labels or startup labels.
            pass
    stages.update({
        "opening simulator", "validating saved order",
        "waiting for login / session screen", "preparing automation steps",
        "EZ Enroll payment review completed", "installation Forward clicked",
        "payment Forward clicked",
    })
    assert stages <= sensitive.SAFE_STAGES
    assert "verifying EZ Enroll payment review" in sensitive.SAFE_STAGES
    assert "EZ Enroll payment review completed" in sensitive.SAFE_STAGES
