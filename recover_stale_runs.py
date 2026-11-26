"""Operator-driven stale-run report and recovery through the authenticated API."""

from datetime import datetime, timedelta, timezone

import api_client


def build_report(runs, orders, *, now=None):
    now = now or datetime.now(timezone.utc)
    report = []
    for run in runs:
        order = orders.get(str(run.get("order_id")), {})
        worker = run.get("worker") or run.get("worker_lease")
        worker_active = bool(worker and worker.get("active"))
        try:
            updated = datetime.fromisoformat(run["updated_at"])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            stale = now - updated >= timedelta(seconds=900)
        except (KeyError, TypeError, ValueError):
            stale = False
        submission = run.get("submission_state", "unknown")
        eligible = (
            run.get("status") == "running" and stale and not worker_active
            and submission not in {"confirmed", "ambiguous", "unknown", "attempting"}
        )
        report.append({
            **run,
            "test_case_id": order.get("Test Case ID", "unknown"),
            "worker": worker,
            "worker_active": worker_active,
            "recovery_eligible": eligible,
        })
    return report


def load_report():
    runs = api_client.list_runs(status="running")
    orders = {str(order_id): api_client.load_order(order_id)
              for order_id in api_client.list_order_ids()}
    return build_report(runs, orders)


def main(*, reader=input, writer=print):
    report = load_report()
    if not report:
        writer("There are no running records to review.")
        return
    writer("Running records (read only; recovery is per-run):")
    for index, run in enumerate(report, 1):
        worker = run.get("worker") or {}
        writer(
            f"{index}. Test Case ID: {run['test_case_id']} | Run ID: {run['id']} | "
            f"Order ID: {run['order_id']} | Status: {run['status']} | "
            f"Stage: {run['stage']} | Error: {run.get('error_code')} | "
            f"Submission: {run.get('submission_state')} | "
            f"Final submitted: {run.get('final_order_submitted')} | "
            f"Worker active: {run['worker_active']} | "
            f"Worker PID: {worker.get('worker_pid', 'unknown')} | "
            f"Eligible: {run['recovery_eligible']}"
        )
    selected = reader("Run ID or Test Case ID to recover (blank cancels): ").strip()
    if not selected:
        writer("Cancelled; no records changed.")
        return
    candidates = [r for r in report if r["id"] == selected or
                  str(r["test_case_id"]).casefold() == selected.casefold()]
    if len(candidates) != 1:
        raise SystemExit("Selection must identify exactly one running record.")
    run = candidates[0]
    if run["worker_active"]:
        raise SystemExit("Recovery blocked: worker lease is active.")
    if not run["recovery_eligible"] and run.get("submission_state") not in {
        "ambiguous", "unknown", "attempting"
    }:
        raise SystemExit("Recovery blocked: run is not stale and eligible.")
    if reader("Type WORKER AND CHROMIUM STOPPED after checking: ").strip() != \
            "WORKER AND CHROMIUM STOPPED":
        writer("Cancelled; no records changed.")
        return
    if run.get("submission_state") in {"ambiguous", "unknown", "attempting"}:
        writer("Verify the persisted simulator order status; age is not evidence.")
        outcome = reader("Outcome (submitted/not_submitted/unknown): ").strip()
        if outcome not in {"submitted", "not_submitted", "unknown"}:
            raise SystemExit("Choose submitted, not_submitted, or unknown.")
    else:
        if reader("Type NOT SUBMITTED after verifying simulator status: ").strip() != \
                "NOT SUBMITTED":
            writer("Cancelled; no records changed.")
            return
        outcome = "not_submitted"
    if reader(f"Type RECOVER {run['id']} to confirm: ").strip() != f"RECOVER {run['id']}":
        writer("Cancelled; no records changed.")
        return
    result = api_client.recover_run(run["id"], outcome=outcome, worker_stopped=True)
    writer(
        f"Recovery recorded: run={result['id']} status={result['status']} "
        f"stage={result['stage']} submission={result['submission_state']} "
        f"final_order_submitted={result['final_order_submitted']}"
    )
    if outcome == "unknown":
        writer("Outcome remains unresolved; this order remains blocked from new runs.")


if __name__ == "__main__":
    main()
