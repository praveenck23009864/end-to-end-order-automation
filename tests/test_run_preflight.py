"""Runner preflight tests use mocked API calls and synthetic input only."""

import builtins

import pytest

import api_client
import run_from_api


def _prepare_runner(monkeypatch, *, test_case_id="A1", order_id="5"):
    monkeypatch.setattr(run_from_api, "check_automation_functions", lambda: None)
    monkeypatch.setattr(run_from_api, "require_private_browser_logging", lambda: None)
    monkeypatch.setattr(run_from_api, "get_api_token", lambda: None)
    monkeypatch.setattr(run_from_api.automation, "validate_order", lambda _: None)
    monkeypatch.setattr(run_from_api, "sample_payment_requested", lambda _: False)
    monkeypatch.setattr(api_client, "list_order_ids", lambda: [str(order_id)])
    monkeypatch.setattr(api_client, "load_order", lambda _: {
        "Customer ID": str(order_id), "EZ Enroll": "No", "Test Case ID": test_case_id,
    })
    monkeypatch.setattr(builtins, "input", lambda _: test_case_id.lower())
    monkeypatch.setattr(run_from_api, "uuid4", lambda: "new-run-id")


def test_existing_active_run_is_reported_before_sensitive_collection(monkeypatch, capsys):
    _prepare_runner(monkeypatch)
    monkeypatch.setattr(api_client, "list_runs", lambda **_: [{
        "id": "existing-run", "order_id": "5", "status": "running",
        "stage": "waiting for login / session screen",
    }])
    monkeypatch.setattr(
        run_from_api, "collect_sensitive_payload",
        lambda **_: pytest.fail("sensitive input must not be requested"),
    )
    monkeypatch.setattr(
        api_client, "begin_run",
        lambda *_: pytest.fail("must not create another run"),
    )

    with pytest.raises(SystemExit):
        run_from_api.main()

    output = capsys.readouterr().out
    assert "Selected Test Case ID: A1" in output
    assert "Database Order ID: 5" in output
    assert "Run ID: existing-run" in output
    assert "Status: running" in output
    assert "Stage: waiting for login / session screen" in output
    assert "without collecting sensitive information" in output


def test_new_run_is_created_before_collection_and_jwe_submission(monkeypatch):
    _prepare_runner(monkeypatch)
    events = []
    payload = {"ssn": "synthetic", "dob": "synthetic"}
    collected = []
    monkeypatch.setattr(api_client, "list_runs", lambda **_: [])
    monkeypatch.setattr(api_client, "get_sensitive_public_key", lambda: events.append("key") or {"kid": "test"})
    monkeypatch.setattr(api_client, "begin_run", lambda *_: events.append("begin"))
    monkeypatch.setattr(
        run_from_api, "collect_sensitive_payload",
        lambda **_: events.append("collect") or collected.append(dict(payload)) or collected[-1],
    )
    monkeypatch.setattr(
        run_from_api, "encrypt_sensitive_payload",
        lambda *_, **__: events.append("encrypt") or "synthetic-jwe",
    )
    monkeypatch.setattr(
        api_client, "submit_sensitive_payload",
        lambda *_: events.append("submit") or {"accepted": True},
    )

    run_from_api.main()

    assert events == ["begin", "key", "collect", "encrypt", "submit"]
    assert collected[0] == {}  # Caller clears its payload mapping.


def test_cancelled_collection_closes_only_the_new_run(monkeypatch):
    _prepare_runner(monkeypatch)
    monkeypatch.setattr(api_client, "list_runs", lambda **_: [])
    monkeypatch.setattr(api_client, "get_sensitive_public_key", lambda: {"kid": "test"})
    monkeypatch.setattr(api_client, "begin_run", lambda *_: {"id": "new-run-id"})
    monkeypatch.setattr(
        run_from_api, "collect_sensitive_payload",
        lambda **_: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    progress = []
    monkeypatch.setattr(api_client, "progress", lambda *args: progress.append(args))

    with pytest.raises(SystemExit, match="Sensitive input cancelled"):
        run_from_api.main()

    assert progress == [(
        "new-run-id", "cancelled", "manual recovery", "interrupted",
    )]


def test_begin_run_conflict_stops_before_sensitive_collection(monkeypatch):
    _prepare_runner(monkeypatch)
    monkeypatch.setattr(api_client, "list_runs", lambda **_: [])
    monkeypatch.setattr(api_client, "get_sensitive_public_key", lambda: {"kid": "test"})

    def conflict(*_):
        raise api_client.ApiError("API conflict: An active run already exists.")

    monkeypatch.setattr(api_client, "begin_run", conflict)
    monkeypatch.setattr(
        run_from_api, "collect_sensitive_payload",
        lambda **_: pytest.fail("must not collect after a run creation conflict"),
    )

    with pytest.raises(SystemExit, match="An active run already exists"):
        run_from_api.main()


def test_api_client_lists_runs_using_existing_paginated_route(monkeypatch):
    requests = []
    runs = [{"id": "a", "order_id": "5", "status": "running", "stage": "x"}]

    def fake_request(method, path):
        requests.append((method, path))
        return runs

    monkeypatch.setattr(api_client, "request", fake_request)
    assert api_client.list_runs(order_id="5", status="running") == runs
    assert requests == [("GET", "/runs?limit=100&offset=0")]


def test_payload_test_case_ids_resolve_to_expected_database_order_ids(monkeypatch):
    payload_cases = {
        "6": "A1", "20": "P10", "26": "I1", "29": "C1",
        "54": "K21", "57": "T3", "66": "S5",
    }
    monkeypatch.setattr(api_client, "list_order_ids", lambda: list(payload_cases))
    monkeypatch.setattr(
        api_client, "load_order",
        lambda order_id: {"Test Case ID": payload_cases[str(order_id)]},
    )
    choices = run_from_api.load_test_case_choices()
    assert choices == [
        ("A1", "6"), ("P10", "20"), ("I1", "26"), ("C1", "29"),
        ("K21", "54"), ("T3", "57"), ("S5", "66"),
    ]

    printed = []
    for expected_order_id, test_case_id in payload_cases.items():
        selected = run_from_api.select_test_case(
            choices, reader=lambda _, value=test_case_id: value.lower(),
            output=printed.append,
        )
        assert selected == (test_case_id, expected_order_id)
    assert all(
        f"Category {category}:" in printed
        for category in "APICKTS"
    )


@pytest.mark.parametrize(
    ("test_case_id", "order_id"),
    [("A1", "6"), ("P10", "20"), ("K21", "54"), ("T3", "57"), ("S5", "66")],
)
def test_runner_passes_selected_case_numeric_order_id_to_api(monkeypatch, test_case_id, order_id):
    _prepare_runner(monkeypatch, test_case_id=test_case_id, order_id=order_id)
    monkeypatch.setattr(api_client, "list_runs", lambda **_: [])
    monkeypatch.setattr(api_client, "get_sensitive_public_key", lambda: {"kid": "test"})
    begun = []
    checked = []
    submitted = []
    monkeypatch.setattr(api_client, "begin_run", lambda key, _: begun.append(str(key)))
    monkeypatch.setattr(
        api_client, "list_runs",
        lambda *, order_id, status: checked.append(str(order_id)) or [],
    )
    monkeypatch.setattr(run_from_api, "collect_sensitive_payload", lambda **_: {"synthetic": "value"})
    monkeypatch.setattr(run_from_api, "encrypt_sensitive_payload", lambda *_, **__: "synthetic-jwe")
    monkeypatch.setattr(api_client, "submit_sensitive_payload", lambda *_: submitted.append(True))

    run_from_api.main()

    assert begun == [order_id]
    assert checked == [order_id]
    assert submitted == [True]


@pytest.mark.parametrize("selection", ["", "Z99"])
def test_missing_or_invalid_test_case_id_is_clear(selection):
    with pytest.raises(ValueError, match="Test Case ID"):
        run_from_api.select_test_case(
            [("A1", "6")], reader=lambda _: selection, output=lambda _: None
        )
