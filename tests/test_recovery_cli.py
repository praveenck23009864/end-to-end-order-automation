from datetime import datetime, timedelta, timezone

import pytest

import api_client
import recover_stale_runs as recovery


def test_report_includes_order_test_case_and_worker_eligibility():
    run = {
        "id": "run-1", "order_id": "2", "status": "running",
        "stage": "Step 3 installation and payment", "error_code": None,
        "submission_state": "not_attempted", "final_order_submitted": False,
        "updated_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "worker_lease": {"active": False, "worker_pid": 12},
    }
    row = recovery.build_report([run], {"2": {"Test Case ID": "M2"}})[0]
    assert row["test_case_id"] == "M2"
    assert row["recovery_eligible"] is True
    assert row["worker"]["worker_pid"] == 12


@pytest.mark.parametrize("submission", ["ambiguous", "confirmed", "unknown"])
def test_report_blocks_ambiguous_and_submitted_states(submission):
    run = {
        "id": "run-1", "order_id": "2", "status": "running",
        "submission_state": submission, "updated_at": "2020-01-01T00:00:00+00:00",
        "worker": {"active": False},
    }
    assert recovery.build_report([run], {"2": {}})[0]["recovery_eligible"] is False


def test_cli_recovers_only_selected_run_after_confirmations(monkeypatch):
    records = [{
        "id": "run-m2", "order_id": "2", "test_case_id": "M2",
        "status": "running", "stage": "installation", "error_code": None,
        "submission_state": "not_attempted", "final_order_submitted": False,
        "updated_at": "2020-01-01T00:00:00+00:00", "worker_active": False,
        "worker": {"active": False}, "recovery_eligible": True,
    }]
    monkeypatch.setattr(recovery, "load_report", lambda: records)
    called = []
    monkeypatch.setattr(api_client, "recover_run", lambda *a, **kw: called.append((a, kw)) or {
        "id": "run-m2", "status": "failed", "stage": "manual recovery",
        "submission_state": "not_submitted", "final_order_submitted": False,
    })
    answers = iter(["M2", "WORKER AND CHROMIUM STOPPED", "NOT SUBMITTED", "RECOVER run-m2"])
    recovery.main(reader=lambda _: next(answers), writer=lambda *_: None)
    assert called == [(("run-m2",), {"outcome": "not_submitted", "worker_stopped": True})]


def test_cli_unknown_keeps_ambiguous_run_unresolved(monkeypatch):
    run = {
        "id": "run-m2", "order_id": "2", "test_case_id": "M2",
        "status": "running", "stage": "submission outcome ambiguous",
        "error_code": "manual_recovery", "submission_state": "ambiguous",
        "final_order_submitted": False, "worker_active": False,
        "worker": {"active": False}, "recovery_eligible": False,
    }
    monkeypatch.setattr(recovery, "load_report", lambda: [run])
    called = []
    monkeypatch.setattr(api_client, "recover_run", lambda *a, **kw: called.append(kw) or {
        "id": "run-m2", "status": "running", "stage": "submission outcome ambiguous",
        "submission_state": "ambiguous", "final_order_submitted": False,
    })
    answers = iter(["M2", "WORKER AND CHROMIUM STOPPED", "unknown", "RECOVER run-m2"])
    recovery.main(reader=lambda _: next(answers), writer=lambda *_: None)
    assert called[0]["outcome"] == "unknown"
