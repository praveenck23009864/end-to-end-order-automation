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
from sensitive_input import collect_sensitive_payload
from sensitive_data import (
    SensitiveDataError,
    assert_non_sensitive_order,
    get_api_token,
    require_private_browser_logging,
)
from jwe_crypto import encrypt_sensitive_payload, clear_sensitive_mapping
from collections.abc import Callable, Iterable
from typing import cast

Step = tuple[str, Callable[[], object]]

TIMEOUT_MS = getattr(automation, "TIMEOUT_MS", 30_000)
SLOW_MO_MS = getattr(automation, "SLOW_MO_MS", 50)

MIN_DELAY_MS = 800
MAX_DELAY_MS = 2000
def _safe_exception_detail(error, sensitive=None, order=None):
    """Retain useful diagnostics while removing known customer/payment values."""
    detail = str(error) or type(error).__name__
    values = set()
    if isinstance(sensitive, dict):
        values.update(value for value in sensitive.values() if isinstance(value, str) and value)
    def collect_order_values(value):
        if isinstance(value, dict):
            for item in value.values():
                collect_order_values(item)
        elif isinstance(value, list):
            for item in value:
                collect_order_values(item)
        elif isinstance(value, str) and len(value) >= 3:
            values.add(value)

    collect_order_values(order)
    for value in sorted(values, key=len, reverse=True):
        detail = detail.replace(value, "[redacted]")
    detail = re.sub(r"(?<!\w)(?:\d[ -]?){9,19}(?!\w)", "[redacted-number]", detail)
    detail = re.sub(r"\b\d{3,4}\b", "[redacted-number]", detail)
    detail = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "[redacted-date]", detail)
    detail = re.sub(r"\b[0-9a-fA-F]{64}\b", "[redacted-token]", detail)
    return detail[:1000]


def pause_for_review(message):
    """
    Wait for explicit Enter before allowing browser cleanup.
    Do not silently treat EOF as user confirmation.
    """
    while True:
        try:
            input(message)
            return True

        except EOFError:
            print(
                "\nERROR: Terminal input is unavailable. "
                "Chromium cannot be safely closed through this prompt.",
                flush=True,
            )
            raise RuntimeError(
                "Manual browser review could not be completed because "
                "terminal input is unavailable."
            )

        except KeyboardInterrupt:
            print(
                "\nBrowser review interrupted by the user.",
                flush=True,
            )
            raise


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
        "complete_installation_and_payment",
    )

    missing = [
        name for name in required if not callable(getattr(automation, name, None))
    ]

    if missing:
        raise ValueError(
            "These functions are missing from new.py: " + ", ".join(missing)
        )


def complete_authorization(page, order, run_id=None):
    """Verify authorization, submit once, and require explicit simulator success."""

    print("STEP 11: completing Authorization", flush=True)

    # The installation workflow ends on Order Summary. Select the plan named in
    # the saved order payload before opening Authorization. If this function is
    # called on an Authorization page (for example, after a resumed browser
    # session), the Order Summary transition has already happened.
    place_order_candidates = []
    for frame in page.frames:
        buttons = frame.get_by_role(
            "button", name="Place Order", exact=True
        ).filter(visible=True)
        count = buttons.count()
        if count:
            place_order_candidates.append((buttons, count))

    place_order_count = sum(count for _, count in place_order_candidates)
    if place_order_count:
        customer_selected_plan = order.get("Plan")
        if not isinstance(customer_selected_plan, str) or not customer_selected_plan.strip():
            raise RuntimeError(
                "Customer-selected plan is missing from the order payload field 'Plan'."
            )
        customer_selected_plan = customer_selected_plan.strip()

        matching_checkboxes = []
        for frame in page.frames:
            checkboxes = frame.locator('input[type="checkbox"]')
            for index in range(checkboxes.count()):
                checkbox = checkboxes.nth(index)
                if not checkbox.is_visible():
                    continue
                row_text = checkbox.evaluate(
                    """(el, selectedPlan) => {
                        let node = el;
                        for (let i = 0; i < 10 && node; i++, node = node.parentElement) {
                            const text = (node.innerText || node.textContent || '')
                                .replace(/\\s+/g, ' ').trim();
                            if (/ProviderXX/i.test(text)) {
                                const lowerText = text.toLocaleLowerCase();
                                const lowerPlan = selectedPlan.toLocaleLowerCase();
                                let offset = lowerText.indexOf(lowerPlan);
                                while (offset >= 0) {
                                    const before = lowerText[offset - 1] || ' ';
                                    const after = lowerText[offset + lowerPlan.length] || ' ';
                                    if (!/[a-z0-9]/i.test(before) && !/[a-z0-9]/i.test(after)) {
                                        return text;
                                    }
                                    offset = lowerText.indexOf(lowerPlan, offset + 1);
                                }
                                return '';
                            }
                        }
                        return '';
                    }""",
                    customer_selected_plan,
                )
                if row_text:
                    matching_checkboxes.append(checkbox)

        if len(matching_checkboxes) != 1:
            raise RuntimeError(
                "Could not uniquely identify the customer's selected ProviderXX plan. "
                f"Matches found: {len(matching_checkboxes)}."
            )

        plan_checkbox = matching_checkboxes[0]
        if not plan_checkbox.is_checked():
            plan_checkbox.check()
        if not plan_checkbox.is_checked():
            raise RuntimeError(
                "Customer-selected plan checkbox verification failed."
            )

        print("PASS: Customer-selected plan checkbox verified.", flush=True)
        if place_order_count != 1:
            raise RuntimeError("Place Order button was not uniquely located.")
        place_order_button = place_order_candidates[0][0]
        if not place_order_button.is_enabled():
            raise RuntimeError("Place Order button is disabled.")
        place_order_button.click()

        authorization_deadline = time.monotonic() + TIMEOUT_MS / 1000
        authorization_page_found = False
        while time.monotonic() < authorization_deadline:
            for frame in page.frames:
                if frame.get_by_role(
                    "button", name=re.compile(r"^\s*Submit Order\s*$", re.I)
                ).filter(visible=True).count():
                    authorization_page_found = True
                    break
            if authorization_page_found:
                break
            page.wait_for_timeout(100)
        if not authorization_page_found:
            raise RuntimeError(
                "Authorization stage was not detected after Place Order. "
                "Verify simulator status before retrying."
            )
        print("PASS: Place Order clicked; Authorization stage detected.", flush=True)

    customer_name = " ".join(
        part.strip() for part in (order.get("First Name", ""), order.get("Last Name", ""))
        if isinstance(part, str) and part.strip()
    )
    if not customer_name:
        raise RuntimeError("Authorization customer name is unavailable.")
    statement = (
        f"{customer_name}, do you authorize ProviderXX to process this order for the "
        "products and prices I have reviewed with you today, and agree to the "
        "ProviderXX terms and conditions of service?"
    )
    normalized_statement = " ".join(statement.split())
    read_checkbox = None
    statement_frame = None
    statement_deadline = time.monotonic() + TIMEOUT_MS / 1000
    while time.monotonic() < statement_deadline and statement_frame is None:
        for frame in page.frames:
            try:
                visible_text = " ".join(frame.locator("body").inner_text().split())
                if normalized_statement.casefold() not in visible_text.casefold():
                    continue
                statement_frame = frame
                text_locator = frame.get_by_text(
                    re.compile(re.escape(normalized_statement), re.I)
                ).filter(visible=True)
                boxes = text_locator.locator(
                    'xpath=ancestor-or-self::*[.//input[@type="checkbox"]][1]'
                ).get_by_role("checkbox", name="Read", exact=True).filter(visible=True)
                if boxes.count() == 1:
                    read_checkbox = boxes
                else:
                    direct = frame.get_by_role(
                        "checkbox", name="Read", exact=True
                    ).filter(visible=True)
                    if direct.count() == 1:
                        read_checkbox = direct
            except Exception:
                continue
            if statement_frame is not None:
                break
        if statement_frame is None:
            page.wait_for_timeout(100)

    if statement_frame is None:
        raise RuntimeError("Mandatory authorization statement was not found.")
    if read_checkbox is None:
        raise RuntimeError("Authorization Read checkbox was not uniquely associated.")

    # Check and verify.
    if not read_checkbox.is_checked():
        read_checkbox.check()

    if not read_checkbox.is_checked():
        raise RuntimeError("Authorization Read checkbox verification failed.")

    print("PASS: Authorization Read checkbox checked.", flush=True)

    submit_candidates = []
    for frame in page.frames:
        locator = frame.get_by_role(
            "button", name=re.compile(r"^\s*Submit Order\s*$", re.I)
        ).filter(visible=True)
        count = locator.count()
        if count:
            submit_candidates.append((locator, count))
    if sum(count for _, count in submit_candidates) != 1:
        raise RuntimeError("Submit Order button was not uniquely located.")
    submit_button = submit_candidates[0][0]
    if not submit_button.is_enabled():
        raise RuntimeError("Submit Order button is disabled.")
    submit_button.scroll_into_view_if_needed(timeout=TIMEOUT_MS)

    automation.validate_order(order)
    if urlparse(page.url).hostname != urlparse(automation.URL).hostname:
        raise RuntimeError("Order submission is restricted to the configured simulator.")
    import database as db
    if run_id is not None:
        db.set_submission_state(run_id, "attempting")
    try:
        submit_button.click()
        success = re.compile(
            r"order (?:was )?(?:submitted|placed) successfully|"
            r"thank you for your order|order confirmation",
            re.I,
        )
        deadline = time.monotonic() + TIMEOUT_MS / 1000
        confirmed = False
        while time.monotonic() < deadline:
            for frame in page.frames:
                if frame.get_by_text(success).filter(visible=True).count():
                    confirmed = True
                    break
            if confirmed:
                break
            page.wait_for_timeout(100)
        if not confirmed:
            raise TimeoutError("Simulator submission confirmation was not observed.")
        if run_id is not None:
            db.set_submission_state(run_id, "confirmed")
    except Exception:
        if run_id is not None:
            try:
                db.set_submission_state(run_id, "ambiguous")
            except Exception:
                pass
        raise RuntimeError(
            "Submission outcome is ambiguous; verify simulator status before retrying."
        ) from None
    print("PASS: Submit Order button clicked.", flush=True)
    print("PASS: Simulator submission confirmed.", flush=True)



def build_steps(page, order, sensitive=None, payment_required=None, run_id=None) -> Iterable[Step]:
    existing_builder = getattr(automation, "build_steps", None)

    if callable(existing_builder):
        builder = cast(Callable[..., Iterable[Step]], existing_builder)
        steps = list(builder(page, order))
    else:
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
        ]
    )
    if payment_required is None:
        payment_required = str(
            order.get("EZ Enroll", "")
        ).strip().lower() == "yes" or sample_payment_requested(order)
    steps.append(
        (
            "Step 3 installation and payment",
            lambda: automation.complete_installation_and_payment(
                page,
                order,
                sensitive,
                payment_required=payment_required,
            ),
        )
    )
    steps.append(
        (
            "Authorization and Submit Order",
            lambda: complete_authorization(page, order, run_id),
        )
    )

    return steps


def sample_payment_requested(order):
    return "--sample-payment" in sys.argv or order.get("Sample Payment") is True


def print_error_location(error):
    """Print source locations only, with no values, exception text or source lines."""
    current = error.__traceback__
    while current is not None:
        code = current.tb_frame.f_code
        basename = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
        if basename in {
            "new.py",
            "run_from_api.py",
            "sensitive_data.py",
            "verify_payment_live.py",
            "inspect_payment_fields.py",
        }:
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
    elif isinstance(error, ValueError):
        # Only expose known static validation messages. Other ValueErrors may
        # contain values copied from an order or form.
        safe_messages = {
            "No automation steps were configured.",
            "Use a predefined workflow stage without customer values.",
            "Invalid run status.",
            "Invalid error code.",
            "Invalid submission state.",
            "Submission state cannot be changed from its current state.",
            "A finished run cannot be changed or restarted.",
        }
        if str(error) in safe_messages:
            print(f"DETAIL: {error}", flush=True)

    print_error_location(error)

    if page is not None:
        with suppress(Exception):
            if not page.is_closed():
                pause_for_review(
                    "Inspect the browser. " "Press Enter to close Chromium: "
                )

    raise SystemExit(130 if cancelled else 1) from None


def _visible_action_candidates(page, pattern, *, prefer_role=True):
    """Find visible controls matching a label across the main page and frames."""
    candidates = []
    for frame in page.frames:
        try:
            if prefer_role:
                locator = frame.get_by_role("button", name=pattern).filter(visible=True)
                count = locator.count()
                if count:
                    candidates.append((frame, locator, count))
                    continue
            locator = frame.get_by_text(pattern).filter(visible=True)
            count = locator.count()
            if count:
                candidates.append((frame, locator, count))
        except Exception:
            continue
    return candidates


def _wait_for_unique_action(page, pattern, description, *, timeout_ms=600_000):
    """Wait for one visible action across frames; fail closed on ambiguity."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        candidates = _visible_action_candidates(page, pattern)
        total = sum(count for _, _, count in candidates)
        if total == 1:
            frame, locator, _ = candidates[0]
            if not locator.is_enabled():
                page.wait_for_timeout(200)
                continue
            return frame, locator
        if total > 1:
            raise RuntimeError(
                f"{description} was ambiguous: found {total} visible matches. "
                "No navigation click was performed."
            )
        if page.is_closed():
            raise RuntimeError("Browser page closed during session setup.")
        page.wait_for_timeout(250)
    raise TimeoutError(
        f"Timed out waiting for {description}. Confirm manual login and inspect the simulator."
    )


def _visible_text_exists(page, pattern):
    for frame in page.frames:
        try:
            if frame.get_by_text(pattern).filter(visible=True).count():
                return True
        except Exception:
            continue
    return False


def wait_for_session(page, run_id):
    """After manual login, start the simulator session and advance Greetings."""
    del run_id  # The session wait does not update run progress.
    if page.is_closed():
        raise RuntimeError("Browser page closed during session setup.")

    print(
        "Complete the simulator login manually in Chromium. "
        "Automation will click START SESSION and Forward > when those screens appear.",
        flush=True,
    )

    start_pattern = re.compile(r"^\s*START\s+SESSION\s*$", re.I)
    greetings_pattern = re.compile(r"^\s*Greetings\s*$", re.I)
    forward_pattern = re.compile(r"^\s*Forward\s*>\s*$", re.I)

    # If the operator has already clicked START SESSION, do not click it again.
    if not _visible_text_exists(page, greetings_pattern):
        _, start_button = _wait_for_unique_action(
            page, start_pattern, "START SESSION button"
        )
        start_button.click(timeout=TIMEOUT_MS)
        print("PASS: START SESSION clicked.", flush=True)

    # The Greetings screen must be visible before its Forward action is used.
    deadline = time.monotonic() + TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        if _visible_text_exists(page, greetings_pattern):
            break
        if page.is_closed():
            raise RuntimeError("Browser page closed before the Greetings screen loaded.")
        page.wait_for_timeout(200)
    else:
        raise TimeoutError("Greetings screen did not appear after START SESSION.")

    _, forward = _wait_for_unique_action(
        page, forward_pattern, "Greetings Forward > button", timeout_ms=TIMEOUT_MS
    )
    forward.click(timeout=TIMEOUT_MS)
    print("PASS: Greetings Forward > clicked.", flush=True)

    # Confirm that navigation reached Basic Information before running the
    # existing fill_basic_information() step. Do not click Forward a second time.
    deadline = time.monotonic() + TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        for frame in page.frames:
            try:
                if frame.locator("#ssZipCode:visible").count() == 1:
                    print("PASS: Basic Information screen is ready.", flush=True)
                    return
            except Exception:
                continue
        if page.is_closed():
            raise RuntimeError("Browser page closed before Basic Information loaded.")
        page.wait_for_timeout(200)
    raise TimeoutError(
        "Basic Information screen was not detected after Greetings Forward. "
        "The existing form automation was not started."
    )


def run_backend_workflow(run_id, order, sensitive, payment_required):
    """Run Playwright in the authenticated API process with decrypted RAM data."""
    import database as db

    stage = "validating saved order"
    browser = None
    playwright = None
    try:
        assert_non_sensitive_order(order, order.get("Customer ID"))
        automation.validate_order(order)
        playwright = sync_playwright().start()
        try:
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
            steps = list(build_steps(page, order, sensitive, payment_required, run_id))
            if not steps:
                raise ValueError("No automation steps were configured.")
            for index, (stage, action) in enumerate(steps, start=1):
                db.update_run(run_id, "running", stage)
                print(f"STEP {index}/{len(steps)}: {stage}", flush=True)
                action()
            db.update_run(run_id, "installation_completed", "order submitted")

            print(
                "\nAutomation completed successfully.",
                "\nAuthorization page is open for manual review.",
                "\nPress Enter in this API terminal when you want to close Chromium.",
                flush=True,
            )

            pause_for_review("Press Enter to close the browser: ")

            # The finally block owns browser/manager cleanup on both paths.
        except BaseException:
            # The outer handler must inspect/hold the live browser before the
            # Playwright driver is stopped.
            raise

    except BaseException as error:
        cancelled = isinstance(error, (KeyboardInterrupt, SystemExit))
        status = "cancelled" if cancelled else "failed"
        failure_code = (
            "interrupted" if cancelled
            else "api_unavailable" if isinstance(error, api_client.ApiError)
            else "automation_error"
        )
        try:
            current = db.get_run(run_id)
            submission_state = current.get("submission_state", "not_attempted")
            if submission_state in ("attempting", "ambiguous", "unknown"):
                if submission_state == "attempting":
                    db.set_submission_state(run_id, "ambiguous")
                db.update_run(
                    run_id, "running", "submission outcome ambiguous", "manual_recovery"
                )
                status = "manual verification required"
            elif submission_state == "confirmed":
                db.update_run(
                    run_id, "installation_completed", "order submitted", "manual_recovery"
                )
                status = "submitted"
            else:
                db.update_run(run_id, status, stage, failure_code)
        except Exception as persist_error:
            print(
                f"WORKFLOW ERROR run_id={run_id} type={type(error).__name__} "
                f"detail={_safe_exception_detail(error, sensitive, order)}; "
                f"STATUS WRITE ERROR type={type(persist_error).__name__} "
                f"detail={_safe_exception_detail(persist_error, sensitive, order)}. "
                f"Inspect GET /runs/{run_id} before retrying.",
                flush=True,
            )

        print(
            f"{status.upper()} function=run_backend_workflow "
            f"stage={stage} exception={type(error).__name__} "
            f"detail={_safe_exception_detail(error, sensitive, order)}.",
            flush=True,
        )

        # Print source locations without exposing sensitive values.
        print_error_location(error)

        if isinstance(error, (SensitiveDataError, automation.BrowserWorkflowError, api_client.ApiError)):
            print(
                f"SAFE DETAIL: {_safe_exception_detail(error, sensitive, order)}",
                flush=True,
            )
        elif isinstance(error, ValueError):
            safe_messages = {
                "No automation steps were configured.",
                "Use a predefined workflow stage without customer values.",
                "Invalid run status.",
                "Invalid error code.",
                "Invalid submission state.",
                "Submission state cannot be changed from its current state.",
                "A finished run cannot be changed or restarted.",
            }
            if str(error) in safe_messages:
                print(f"SAFE DETAIL: {_safe_exception_detail(error, sensitive, order)}", flush=True)

        # Keep Chromium open for inspection after a failure.
        if browser is not None:
            try:
                if browser.is_connected():
                    print(
                        "Browser remains open. Inspect the failed page.",
                        flush=True,
                    )
                    pause_for_review("Press Enter in this terminal to close Chromium: ")
            except Exception:
                print("Could not keep the browser open for inspection.", flush=True)

            if isinstance(error, (SensitiveDataError, automation.BrowserWorkflowError)):
                print(
                    f"SAFE DETAIL: {_safe_exception_detail(error, sensitive, order)}",
                    flush=True,
                )
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception as close_error:
                print(
                    f"BROWSER CLEANUP ERROR run_id={run_id} "
                    f"type={type(close_error).__name__} "
                    f"detail={_safe_exception_detail(close_error, sensitive, order)}",
                    flush=True,
                )
            browser = None
        if playwright is not None:
            try:
                playwright.stop()
            except Exception as stop_error:
                print(
                    f"PLAYWRIGHT CLEANUP ERROR run_id={run_id} "
                    f"type={type(stop_error).__name__}",
                    flush=True,
                )
            playwright = None
        try:
            clear_sensitive_mapping(sensitive)
        except Exception as clear_error:
            print(
                f"SENSITIVE MEMORY CLEANUP ERROR run_id={run_id} "
                f"type={type(clear_error).__name__}",
                flush=True,
            )


def main():
    """Collect and encrypt sensitive values, then submit JWE to the API worker."""
    print("AUTOMATION FILE:", automation.__file__)
    payload = None
    compact_jwe = None
    try:
        check_automation_functions()
        require_private_browser_logging()
        get_api_token()
        test_case_id, choice = select_test_case(load_test_case_choices())
        print(f"Selected Test Case ID: {test_case_id}")
        print(f"Database Order ID: {choice}")
        order = api_client.load_order(choice)
        assert_non_sensitive_order(order, choice)
        automation.validate_order(order)

        active_runs = api_client.list_runs(order_id=choice, status="running")
        if active_runs:
            active = active_runs[0]
            print(
                f"An active run already exists for Database Order ID {choice} "
                f"(Test Case ID {test_case_id}):"
            )
            print(f"Run ID: {active.get('id', 'unknown')}")
            print(f"Status: {active.get('status', 'unknown')}")
            print(f"Stage: {active.get('stage', 'unknown')}")
            print("Stopping without collecting sensitive information.")
            raise SystemExit(1)

        payment_required = str(
            order.get("EZ Enroll", "")
        ).strip().lower() == "yes" or sample_payment_requested(order)
        run_id = str(uuid4())
        # The API remains the final authority on duplicate and completed runs.
        # Collection begins only after this insert succeeds.
        api_client.begin_run(choice, run_id)

        try:
            key_document = api_client.get_sensitive_public_key()
            payload = collect_sensitive_payload(payment_required=payment_required)
            compact_jwe = encrypt_sensitive_payload(
                payload,
                order_id=choice,
                run_id=run_id,
                payment_required=payment_required,
                public_document=key_document,
            )
        except (KeyboardInterrupt, EOFError) as error:
            _finish_input_run(run_id, cancelled=True)
            raise SystemExit("Sensitive input cancelled; the new run was closed safely.") from None
        except Exception as error:
            _finish_input_run(run_id, cancelled=False)
            if isinstance(error, SensitiveDataError):
                detail = f" {error}"
            else:
                detail = f" ({type(error).__name__})"
            raise SystemExit(
                f"Sensitive input failed; the new run was closed safely.{detail}"
            ) from None

        api_client.submit_sensitive_payload(run_id, compact_jwe)
        print(
            f"Run {run_id} accepted by the API worker. Sensitive values were not logged."
        )

        print(
            "The browser is controlled by the API process; complete login there for manual review."
        )
    except (
        api_client.ApiError,
        FileNotFoundError,
        ValueError,
        AttributeError,
        KeyboardInterrupt,
        EOFError,
    ) as error:
        raise SystemExit(str(error) or "Cancelled.") from None
    finally:
        if "payload" in locals():
            clear_sensitive_mapping(payload)
        if "compact_jwe" in locals():
            compact_jwe = None


def _finish_input_run(run_id, *, cancelled):
    """Close only the run created by this invocation after collection fails."""
    status = "cancelled" if cancelled else "failed"
    error_code = "interrupted" if cancelled else "startup_error"
    try:
        api_client.progress(run_id, status, "manual recovery", error_code)
    except api_client.ApiError:
        print(
            f"Could not close run {run_id}. Inspect GET /runs/{run_id} before retrying."
        )


TEST_CASE_CATEGORY_ORDER = ("A", "P", "I", "C", "K", "T", "S")


def load_test_case_choices():
    """Read Test Case IDs from every order payload through the authenticated API."""
    choices = []
    for order_id in api_client.list_order_ids():
        order = api_client.load_order(order_id)
        test_case_id = order.get("Test Case ID")
        if isinstance(test_case_id, str) and test_case_id.strip():
            choices.append((test_case_id.strip().upper(), str(order_id)))

    by_case = {}
    for test_case_id, order_id in choices:
        normalized = test_case_id.casefold()
        if normalized in by_case:
            raise ValueError(
                f"Duplicate Test Case ID {test_case_id} exists in the orders table. "
                "Resolve the duplicate before running a scenario."
            )
        by_case[normalized] = (test_case_id, order_id)
    return list(by_case.values())


def select_test_case(choices, *, reader=None, output=None):
    """Display scenarios by category and resolve a case-insensitive exact ID."""
    if reader is None:
        reader = input
    if output is None:
        output = print
    if not choices:
        raise ValueError(
            "No orders with a Test Case ID were found in the orders table."
        )

    category_rank = {category: index for index, category in enumerate(TEST_CASE_CATEGORY_ORDER)}
    ordered = sorted(
        choices,
        key=lambda item: (
            category_rank.get(item[0][:1].upper(), len(category_rank)),
            item[0][:1].upper(),
            (0, int(item[0][1:])) if item[0][1:].isdigit() else (1, item[0][1:]),
        ),
    )
    output("Available test cases:")
    grouped = {}
    for test_case_id, order_id in ordered:
        grouped.setdefault(test_case_id[:1].upper(), []).append((test_case_id, order_id))
    for category in sorted(
        grouped,
        key=lambda value: (category_rank.get(value, len(category_rank)), value),
    ):
        output(f"Category {category}:")
        for test_case_id, order_id in grouped[category]:
            output(f"  {test_case_id} (Database Order ID: {order_id})")

    selected = reader("Choose Test Case ID: ").strip().casefold()
    if not selected:
        raise ValueError("Test Case ID is required.")
    matches = [item for item in choices if item[0].casefold() == selected]
    if len(matches) != 1:
        raise ValueError("Invalid Test Case ID. Choose one of the listed scenarios.")
    return matches[0]


if __name__ == "__main__":
    if "--recover" in sys.argv:
        from recover_stale_runs import main as recovery_main
        recovery_main()
    else:
        main()
