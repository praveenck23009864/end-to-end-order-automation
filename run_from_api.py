from contextlib import suppress
from urllib.parse import urlparse
from uuid import uuid4
import random
import re
import time
import sys

from playwright.sync_api import expect, sync_playwright
import api_client
import new as automation
from sensitive_data import (
    SensitiveDataError, assert_non_sensitive_order, get_api_token,
    require_private_browser_logging,
)
from jwe_crypto import encrypt_sensitive_payload, clear_sensitive_mapping
from sensitive_input import collect_sensitive_payload
from collections.abc import Callable, Iterable
from typing import cast

Step = tuple[str, Callable[[], object]]

TIMEOUT_MS = getattr(automation, "TIMEOUT_MS", 30_000)
SLOW_MO_MS = getattr(automation, "SLOW_MO_MS", 50)

MIN_DELAY_MS = 800
MAX_DELAY_MS = 2000


def pause_for_review(message):
    try:
        input(message)
    except (EOFError, KeyboardInterrupt):
        pass


def forward_button(page):
    return page.get_by_role(
        "button",
        name=re.compile(r"^forward\s*>?$", re.I),
    ).filter(visible=True)


def check_automation_functions():
    required = (
        "validate_order",
        "fill_basic_information",
        "confirm_address",
        "qualify_customer",
        "check_provider_availability",
        "select_internet_offer",
        "customize_products",
        "set_broadband_labels",
        "customize_equipment",
        "qualify_account_holder",
        "complete_installation",
        "verify_ez_enroll_payment",
        "complete_sample_payment",
    )

    missing = [
        name for name in required if not callable(getattr(automation, name, None))
    ]

    if missing:
        raise ValueError(
            "These functions are missing from new.py: " + ", ".join(missing)
        )


def build_steps(page, order, sensitive=None, payment_required=None) -> Iterable[Step]:
    existing_builder = getattr(automation, "build_steps", None)

    if callable(existing_builder):
        builder = cast(Callable[..., Iterable[Step]], existing_builder)
        return builder(page, order)

    steps: list[Step] = [
        (
            "filling Basic Information",
            lambda: automation.fill_basic_information(page, order),
        ),
        (
            "confirming matched address",
            lambda: automation.confirm_address(page),
        ),
        (
            "qualifying New / Transfer",
            lambda: automation.qualify_customer(page, order),
        ),
        (
            "checking provider availability",
            lambda: automation.check_provider_availability(page, order),
        ),
    ]

    open_plans = getattr(automation, "open_internet_plans", None)

    if callable(open_plans):
        open_plans_function = cast(Callable[..., object], open_plans)

        steps.append(
            (
                "opening internet plans",
                lambda: open_plans_function(page),
            )
        )

    steps.extend(
        [
            (
                "selecting internet offer",
                lambda: automation.select_internet_offer(page, order),
            ),
            (
                "selecting checkout products",
                lambda: automation.customize_products(page, order),
            ),
            (
                "setting Broadband Labels",
                lambda: automation.set_broadband_labels(
                    page,
                    requested=order.get(
                        "Broadband Labels Requested",
                        False,
                    ),
                ),
            ),
            (
                "selecting equipment",
                lambda: automation.customize_equipment(page, order),
            ),
            (
                "qualifying Account Holder",
                lambda: automation.qualify_account_holder(page, order),
            ),
            (
                "credit check and installation",
                lambda: automation.complete_installation(
                    page,
                    order,
                    advance=str(order.get("EZ Enroll", "")).strip().lower() == "no",
                    sensitive=sensitive,
                ),
            ),
        ]
    )

    ez_enroll = str(order.get("EZ Enroll", "")).strip().lower()
    if ez_enroll == "yes":
        steps.append((
            "verifying EZ Enroll payment review",
            lambda: automation.verify_ez_enroll_payment(page, order, sensitive),
        ))

    if payment_required is None:
        payment_required = sample_payment_requested(order)
    if payment_required and ez_enroll != "yes":
        steps.append((
            "verifying sample payment fields",
            lambda: automation.complete_sample_payment(page, order, sensitive),
        ))

    return steps


def sample_payment_requested(order):
    return "--sample-payment" in sys.argv or order.get("Sample Payment") is True


def print_error_location(error):
    """Print source locations only, with no values, exception text or source lines."""
    current = error.__traceback__
    while current is not None:
        code = current.tb_frame.f_code
        basename = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
        if basename in {"new.py", "run_from_api.py", "sensitive_data.py", "verify_payment_live.py", "inspect_payment_fields.py"}:
            print(f"ERROR LOCATION: {basename}:{current.tb_lineno} in {code.co_name}")
        current = current.tb_next


def record_failure(run_id, stage, error, page=None):
    cancelled = isinstance(error, KeyboardInterrupt)
    status = "cancelled" if cancelled else "failed"

    if cancelled:
        error_code = "interrupted"
    elif isinstance(error, api_client.ApiError):
        error_code = "api_unavailable"
    else:
        error_code = "automation_error"

    try:
        api_client.progress(
            run_id,
            status,
            stage,
            error_code,
        )
    except api_client.ApiError:
        print(
            "Could not save the final status. " f"Check /runs/{run_id} before retrying."
        )

    print(f"{status.upper()} during {stage}.")
    print(f"Error type: {type(error).__name__}")
    print(f"Run ID: {run_id}")

    if isinstance(error, SensitiveDataError):
        # Messages from this controlled exception contain no customer values.
        print(f"DETAIL: {error}", flush=True)

    print_error_location(error)

    if page is not None:
        with suppress(Exception):
            if not page.is_closed():
                pause_for_review(
                    "Inspect the browser. " "Press Enter to close Chromium: "
                )

    raise SystemExit(130 if cancelled else 1) from None


def wait_for_session(page, run_id):
    del run_id  # The session wait does not update run progress.
    pause_for_review(
        "Complete login / session setup in Chromium. "
        "Press Enter when the simulator is ready: "
    )
    if page.is_closed():
        raise RuntimeError("Browser page closed during session setup.")


def run_backend_workflow(run_id, order, sensitive, payment_required):
    """Run Playwright in the authenticated API process with decrypted RAM data."""
    import database as db

    stage = "validating saved order"
    browser = None
    try:
        assert_non_sensitive_order(order, order.get("Customer ID"))
        automation.validate_order(order)
        with sync_playwright() as playwright:
            stage = "opening simulator"
            db.update_run(run_id, "running", stage)
            browser = playwright.chromium.launch(
                headless=False,
                slow_mo=0 if "--fast" in sys.argv else SLOW_MO_MS,
            )
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            page.set_default_timeout(TIMEOUT_MS)
            expect.set_options(timeout=TIMEOUT_MS)
            page.goto(automation.URL, wait_until="domcontentloaded", timeout=60_000)

            stage = "waiting for login / session screen"
            wait_for_session(page, run_id)
            stage = "preparing automation steps"
            steps = list(build_steps(page, order, sensitive, payment_required))
            if not steps:
                raise ValueError("No automation steps were configured.")
            for index, (stage, action) in enumerate(steps, start=1):
                db.update_run(run_id, "running", stage)
                print(f"STEP {index}/{len(steps)}: {stage}", flush=True)
                action()
            final_stage = "payment Forward clicked" if payment_required else "installation Forward clicked"
            db.update_run(run_id, "installation_completed", final_stage)
            browser.close()
            browser = None
    except (Exception, KeyboardInterrupt) as error:
        status = "cancelled" if isinstance(error, KeyboardInterrupt) else "failed"
        error_code = "interrupted" if status == "cancelled" else "automation_error"
        try:
            db.update_run(run_id, status, stage, error_code)
        except Exception:
            print("Could not persist the workflow failure status.", flush=True)
        print(
            f"{status.upper()} function=run_backend_workflow stage={stage} "
            f"exception={type(error).__name__}.",
            flush=True,
        )
        if isinstance(error, (SensitiveDataError, automation.BrowserWorkflowError)):
            # Controlled automation diagnostics contain field names and state,
            # never customer values, page text, or raw Playwright exceptions.
            print(f"SAFE DETAIL: {error}", flush=True)
    finally:
        if browser is not None:
            with suppress(Exception):
                browser.close()
        clear_sensitive_mapping(sensitive)


def main():
    """Collect and encrypt sensitive values, then submit JWE to the API worker."""
    print("AUTOMATION FILE:", automation.__file__)
    try:
        check_automation_functions()
        require_private_browser_logging()
        get_api_token()
        customer_ids = [str(customer_id) for customer_id in api_client.list_order_ids()]
        if not customer_ids:
            raise ValueError("No customers found. Import non-sensitive order data first.")
        choice = input(f"Choose test customer {', '.join(customer_ids)}: ").strip()
        if choice not in customer_ids:
            raise ValueError("Choose a customer ID from the list.")
        order = api_client.load_order(choice)
        assert_non_sensitive_order(order, choice)
        automation.validate_order(order)
        payment_required = (
            str(order.get("EZ Enroll", "")).strip().lower() == "yes"
            or sample_payment_requested(order)
        )
        payload = collect_sensitive_payload(payment_required=payment_required)
        run_id = str(uuid4())
        key_document = api_client.get_sensitive_public_key()
        compact_jwe = encrypt_sensitive_payload(
            payload, order_id=choice, run_id=run_id,
            payment_required=payment_required, public_document=key_document,
        )
        api_client.begin_run(choice, run_id)
        api_client.submit_sensitive_payload(run_id, compact_jwe)
        print(f"Run {run_id} accepted by the API worker. Sensitive values were not logged.")
        print("The browser is controlled by the API process; complete login there for manual review.")
    except (api_client.ApiError, ValueError, AttributeError, KeyboardInterrupt, EOFError) as error:
        raise SystemExit(str(error) or "Cancelled.") from None
    finally:
        if "payload" in locals():
            clear_sensitive_mapping(payload)
        if "compact_jwe" in locals():
            compact_jwe = None


if __name__ == "__main__":
    main()
