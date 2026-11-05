from contextlib import suppress
from datetime import date
from getpass import getpass, GetPassWarning
from pathlib import Path
import re
import warnings

from playwright.sync_api import Error, expect, sync_playwright

URL = "https://lemon-forest-0915c3c1e.6.azurestaticapps.net/allconnect/"
CDP_URL = "ws://127.0.0.1:9223/devtools/browser"

OUTPUT_FOLDER = Path(__file__).resolve().parent

# Replace these examples with your team's test customer details.
ORDERS = {
    "1": {
        "Prefix": "Mr",
        "First Name": "Alex",
        "MI": "",
        "Last Name": "Taylor",
        "Suffix": "",
        "Zip": "12345",
        "Address 1": "14 Sample Road",
        "Unit Type": "",
        "Unit Number": "",
        "City": "Schenectady",
        "State": "NY",
        "Dwelling Type": "House",
        "Own / Rent": "Own",
        "Moving?": "No",
        "Lead ID": "",
    },
    "2": {
        "Prefix": "Ms",
        "First Name": "Jordan",
        "MI": "R",
        "Last Name": "Lee",
        "Suffix": "",
        "Zip": "02108",
        "Address 1": "25 Example Lane",
        "Unit Type": "Apartment",
        "Unit Number": "4B",
        "City": "Boston",
        "State": "MA",
        "Dwelling Type": "Apartment",
        "Own / Rent": "Rent",
        "Moving?": "Yes",
        "Move Date": "2026-11-15",
        "Lead ID": "",
    },
}

DROPDOWNS = {
    "Prefix",
    "Suffix",
    "State",
    "Unit Type",
    "Dwelling Type",
    "Own / Rent",
    "Moving?",
}

BASIC_FIELDS = (
    "Prefix",
    "First Name",
    "MI",
    "Last Name",
    "Suffix",
    "Address 1",
    "Dwelling Type",
    "Unit Type",
    "Unit Number",
    "City",
    "State",
    "Zip",
    "Own / Rent",
    "Moving?",
)


def form_control(page, label):
    if label in DROPDOWNS:
        option = page.get_by_role(
            "option",
            name=label,
            exact=True,
        )
        return page.get_by_role("combobox").filter(has=option)

    if label == "Zip":
        return page.locator('#zipcode, input[aria-label="Zip"]')

    return page.get_by_role(
        "textbox",
        name=label,
        exact=True,
    )


def save_screenshot(page, filename):
    path = OUTPUT_FOLDER / filename

    # Remove an earlier image so it cannot be mistaken for this run.
    path.unlink(missing_ok=True)

    try:
        page.screenshot(
            path=str(path),
            full_page=True,
            timeout=15_000,
        )
        print(f"Screenshot saved to {path}")
    except Error as error:
        print(f"Screenshot capture failed: {type(error).__name__}")


def main():
    if not URL.startswith("https://"):
        raise SystemExit("Paste your simulator HTTPS URL into the URL line.")

    choice = input("Choose 1 (house, not moving) or 2 (apartment, moving): ").strip()

    if choice not in ORDERS:
        raise SystemExit("Run again and choose 1 or 2.")

    order = ORDERS[choice]
    moving = order["Moving?"]
    move_date = None

    if moving not in ("Yes", "No"):
        raise SystemExit("Moving? must be Yes or No.")

    if moving == "Yes":
        try:
            move_date = date.fromisoformat(order.get("Move Date", "")).isoformat()
        except (ValueError, TypeError):
            raise SystemExit(
                "Moving is Yes. Enter a valid Move Date " "in YYYY-MM-DD format."
            )

    stage = "connecting to local Obscura"
    password = None

    with sync_playwright() as playwright:
        browser = None
        context = None
        page = None

        try:
            print(f"Connecting to {CDP_URL}")
            browser = playwright.chromium.connect_over_cdp(
                CDP_URL,
                timeout=15_000,
            )

            stage = "creating the browser context"
            context = browser.new_context()

            stage = "creating the page"
            page = context.new_page()
            page.set_default_timeout(30_000)

            stage = "opening the simulator"
            print("Opening simulator...")
            page.goto(
                URL,
                wait_until="domcontentloaded",
                timeout=60_000,
            )

            password_box = page.locator('input[type="password"]:visible')

            start = page.get_by_text(
                re.compile(r"^start session$", re.I)
            ).filter(visible=True)

            forward = page.locator(
                'input[type="button"][value^="Forward" i]:visible, '
                'input[type="submit"][value^="Forward" i]:visible'
            )

            zip_search = page.locator("#ssZipCode:visible")

            stage = "waiting for the login or starting screen"
            password_box.or_(start).or_(zip_search).first.wait_for(
                state="visible",
                timeout=30_000,
            )
            if password_box.is_visible():
                stage = "reading the simulator password"

                # Stop if this terminal cannot hide password entry.
                with warnings.catch_warnings():
                    warnings.simplefilter("error", GetPassWarning)
                    password = getpass("Simulator password (hidden): ")

                if not password:
                    raise ValueError("The password is empty.")

                stage = "filling the login password"
                password_box.fill(password)

                stage = "clicking Continue on login"
                page.get_by_role(
                    "button",
                    name="Continue",
                    exact=True,
                ).click()

            stage = "waiting for the page after login"
            start.or_(zip_search).first.wait_for(
                state="visible",
                timeout=30_000,
            )
            print("Login stage completed.")

            if not zip_search.is_visible():
                stage = "clicking Start Session"
                print("Clicking Start Session...")
                start.click()

                stage = "waiting for the greeting or Basic Information"
                forward.or_(zip_search).first.wait_for(
                    state="visible",
                    timeout=30_000,
                )

                if not zip_search.is_visible():
                    stage = "clicking Forward on the greeting"
                    print("Clicking Forward on greeting...")
                    forward.click()

            stage = "waiting for the Basic Information ZIP field"
            expect(zip_search).to_be_editable(timeout=30_000)

            stage = "filling the lookup ZIP"
            print("Entering ZIP...")
            zip_search.fill(order["Zip"])
            zip_search.press("Tab")

            stage = "waiting for Address 1 to unlock"
            expect(form_control(page, "Address 1")).to_be_editable(timeout=30_000)

            for label in BASIC_FIELDS:
                stage = f"filling {label}"
                field = form_control(page, label)
                value = order[label]

                # Empty optional fields can remain empty.
                if field.input_value() == value:
                    continue

                print(f"Setting {label}...")

                if label in DROPDOWNS:
                    field.select_option(value=value)
                else:
                    field.fill(value)

            if order.get("Lead ID"):
                stage = "filling optional Lead ID"
                lead = page.locator("#rvCallID")
                lead.fill(order["Lead ID"])
                expect(lead).to_have_value(order["Lead ID"])

            if moving == "Yes":
                stage = "filling the move date"
                print("Entering move date...")
                date_field = page.locator('input[type="date"]:visible')
                expect(date_field).to_be_editable(timeout=15_000)
                date_field.fill(move_date)
                date_field.press("Tab")
                expect(date_field).to_have_value(move_date)

            stage = "verifying the completed form"
            expect(zip_search).to_have_value(order["Zip"])

            for label in BASIC_FIELDS:
                stage = f"verifying {label}"
                expect(form_control(page, label)).to_have_value(order[label])

            print("Basic Information fields verified.")

            stage = "clicking Forward on Basic Information"
            forward.click()

            stage = "waiting for Basic Information to close"
            expect(zip_search).to_be_hidden(timeout=30_000)

            print(
                f"PASS: Test order {choice} filled. "
                "Basic Information was left after Forward."
            )
            save_screenshot(page, "obscura_page.png")

        except (
            Error,
            AssertionError,
            ValueError,
            GetPassWarning,
        ) as error:
            print(f"STOPPED during {stage}.")
            print(f"Error type: {type(error).__name__}")

            details = str(error)

            # Redact the password and configured URL from the error report.
            if password:
                details = details.replace(password, "[PASSWORD REDACTED]")
            details = details.replace(URL, "[SIMULATOR URL REDACTED]")

            report = (
                f"Stage: {stage}\n"
                f"Error type: {type(error).__name__}\n\n"
                f"{details}\n"
            )
            error_file = OUTPUT_FOLDER / "obscura_error.txt"

            try:
                error_file.write_text(report, encoding="utf-8")
                print(f"Error details saved to {error_file}")
            except OSError:
                print("Could not write the error report.")

            if page is not None and not page.is_closed():
                # Clear any password still present before capture.
                with suppress(Error):
                    page.locator('input[type="password"]').evaluate_all(
                        "(inputs) => inputs.forEach(input => input.value = '')"
                    )
                save_screenshot(page, "obscura_page.png")

            raise SystemExit(1) from None

        finally:
            password = None

            if context is not None:
                with suppress(Error):
                    context.close()

            if browser is not None:
                with suppress(Error):
                    browser.close()


if __name__ == "__main__":
    main()
