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
    SensitiveDataError, assert_protected_order, get_api_token,
    require_private_browser_logging,
)
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
        "complete_sample_payment",
    )

    missing = [
        name for name in required if not callable(getattr(automation, name, None))
    ]

    if missing:
        raise ValueError(
            "These functions are missing from new.py: " + ", ".join(missing)
        )


def build_steps(page, order) -> Iterable[Step]:
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
                    advance=(
                        not sample_payment_requested(order)
                        or order.get("EZ Enroll") == "No"
                    ),
                ),
            ),
        ]
    )

    if sample_payment_requested(order):
        steps.append((
            "sample payment and Forward",
            lambda: automation.complete_sample_payment(page, order),
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


def main():
    print("AUTOMATION FILE:", automation.__file__)

    # Validate configuration before creating an API run.
    try:
        check_automation_functions()
        if "--sample-payment" in sys.argv and "--inspect-payment-fields" in sys.argv:
            raise ValueError("Choose sample payment or payment-field inspection for this run.")
        require_private_browser_logging()
        get_api_token()  # Verify local DPAPI setup before creating a run.

        url = getattr(automation, "URL", "")
        if not isinstance(url, str):
            raise ValueError("URL in new.py must be a string.")

        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Set URL in new.py to your simulator HTTPS URL.")

        customer_ids = [str(customer_id) for customer_id in api_client.list_order_ids()]

        if not customer_ids:
            raise ValueError("No customers found. Run import_orders.py first.")

        choice = input(f"Choose test customer {', '.join(customer_ids)}: ").strip()

        if choice not in customer_ids:
            raise ValueError("Choose a customer ID from the list.")

        selected_order = api_client.load_order(choice)
        assert_protected_order(selected_order, choice)
        automation.validate_order(selected_order)
        if sample_payment_requested(selected_order) and "--inspect-payment-fields" in sys.argv:
            raise ValueError("Choose sample payment or payment-field inspection for this run.")

    except (
        api_client.ApiError,
        ValueError,
        AttributeError,
        KeyboardInterrupt,
        EOFError,
    ) as error:
        raise SystemExit(str(error) or "Cancelled.") from None

    run_id = str(uuid4())
    print(f"Run ID: {run_id}")

    try:
        run = api_client.begin_run(choice, run_id)
    except api_client.ApiError as error:
        raise SystemExit(
            f"{error}\n" f"Check /runs/{run_id} before retrying."
        ) from None

    if run["status"] != "running":
        raise SystemExit(
            "This run has already finished. " "Browser actions were not repeated."
        )

    stage = "validating saved order"
    browser = None
    page = None

    try:
        # Use the order snapshot saved for this run.
        order = run["order"]
        assert_protected_order(order, choice)
        automation.validate_order(order)

        with sync_playwright() as playwright:
            try:
                stage = "opening simulator"
                api_client.progress(run_id, "running", stage)

                browser = playwright.chromium.launch(
                    headless=False,
                    slow_mo=0 if "--fast" in sys.argv else SLOW_MO_MS,
                )

                page = browser.new_page(
                    viewport={
                        "width": 1600,
                        "height": 1000,
                    }
                )

                page.set_default_timeout(TIMEOUT_MS)
                expect.set_options(timeout=TIMEOUT_MS)

                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )

                stage = "waiting for login / session screen"
                wait_for_session(page, run_id)

                stage = "preparing automation steps"
                steps = list(build_steps(page, order))

                if not steps:
                    raise ValueError("No automation steps were configured.")

                # Login and final manual review are excluded.
                target_seconds =  0.0

                # Allocate a different time share to each step.
                weights = [random.uniform(0.8, 1.2) for _ in steps]

                total_weight = sum(weights)
                completed_weight = 0.0
                started_at = time.monotonic()

                print(
                    "\nTarget automation duration: "
                    f"{target_seconds / 60:.1f} minutes"
                )
                print(
                    "This includes typing, page loading, " "and pauses between steps.\n"
                )

                for index, (stage, action) in enumerate(steps):
                    api_client.progress(
                        run_id,
                        "running",
                        stage,
                    )

                    print(
                        f"STEP {index + 1}/{len(steps)}: {stage}",
                        flush=True,
                    )

                    step_started_at = time.monotonic()

                    # Execute each action exactly once.
                    action()

                    step_elapsed = time.monotonic() - step_started_at
                    print(
                        f"Step completed in {step_elapsed:.1f}s",
                        flush=True,
                    )

                    completed_weight += weights[index]

                    scheduled_finish = (
                        started_at + target_seconds * completed_weight / total_weight
                    )

                    # Page loading and human_fill() typing time
                    # reduce the amount of additional waiting.
                    remaining = scheduled_finish - time.monotonic()

                    if remaining > 0:
                        print(
                            f"Pacing pause: {remaining:.0f}s",
                            flush=True,
                        )

                        # Short chunks keep browser events processing.
                        while remaining > 0:
                            if page.is_closed():
                                raise RuntimeError("Browser page closed during pacing.")

                            page.wait_for_timeout(min(remaining, 15.0) * 1000)

                            remaining = scheduled_finish - time.monotonic()

                elapsed_seconds = time.monotonic() - started_at

                stage = (
                    "payment Forward clicked"
                    if sample_payment_requested(order)
                    else "installation Forward clicked"
                )
                api_client.progress(
                    run_id,
                    "installation_completed",
                    stage,
                )

                print("\nPASS: Installation flow completed " "and saved.")
                print(f"Automation duration: " f"{elapsed_seconds / 60:.1f} minutes")
                print("Final order has NOT been submitted.")

                if "--inspect-payment-fields" in sys.argv:
                    from tools.inspect_payment_fields import inspect_payment_fields
                    pause_for_review(
                        "Open Add a new credit card and Edit Secure Fields manually. "
                        "Leave payment fields empty. Press Enter to collect field selectors: "
                    )
                    inspect_payment_fields(page)

                pause_for_review(
                    "Review the next screen. " "Press Enter to close Chromium: "
                )

            except (Exception, KeyboardInterrupt) as error:
                record_failure(
                    run_id,
                    stage,
                    error,
                    page,
                )

            finally:
                if browser is not None:
                    with suppress(Exception):
                        browser.close()

    except (Exception, KeyboardInterrupt) as error:
        record_failure(
            run_id,
            stage,
            error,
        )


if __name__ == "__main__":
    main()
