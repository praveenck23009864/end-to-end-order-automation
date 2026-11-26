"""Visible Chromium: Basic Information -> address match -> exact internet offer.

Paste the local simulator URL below. Sign in in the Chromium window.
The script continues past product selection, then pauses on the next screen.
It does not submit the final order.
"""

import random
import time
import sys
from contextlib import suppress
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from urllib.parse import urlparse
import json
from playwright.sync_api import Error, expect, sync_playwright
from sensitive_data import (
    SensitiveDataError,
    assert_protected_order,
    decrypt_sensitive,
    reject_card_fields,
    require_private_browser_logging,
    validate_sensitive_values,
)
from urllib.parse import urlparse

URL = "https://lemon-forest-0915c3c1e.6.azurestaticapps.net/allconnect/"
BASE_DIR = Path(__file__).resolve().parent
USE_EXTERNAL_ORDERS = True
# Optional: paste your original ORDERS dictionary here to use False above.
LOCAL_ORDERS = {}
SLOW_MO_MS = 50
TIMEOUT_MS = 30_000

DROPDOWNS = {
    "Prefix",
    "Suffix",
    "State",
    "Unit Type",
    "Dwelling Type",
    "Own / Rent",
    "Moving?",
}

CURRENT_PROVIDER_CHOICES = (
    "ProviderVZ 5G",
    "ProviderXX",
    "ProviderOP ST",
    "ProviderAT IG",
    "ProviderTM IG",
    "Others",
)
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


def load_orders():
    if not USE_EXTERNAL_ORDERS:
        data = LOCAL_ORDERS
    else:
        path = BASE_DIR / "orders.json"
        try:
            with path.open(encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except OSError as error:
            raise ValueError(f"Cannot read {path}: {error.strerror}") from None
        except json.JSONDecodeError as error:
            raise ValueError(f"orders.json line {error.lineno}: {error.msg}") from None
    if not isinstance(data, dict) or not data:
        raise ValueError("Provide a nonempty orders.json or LOCAL_ORDERS dictionary.")
    if any(not isinstance(item, dict) for item in data.values()):
        raise ValueError("Each customer entry must be a JSON object.")
    return data


def validate_order(order, *, allow_plaintext=False):
    """Report missing/invalid data before opening the browser; never print values."""
    if not isinstance(order, dict):
        raise ValueError("Customer data must be an object.")
    reject_card_fields(order)
    protected = "Sensitive Data" in order
    if protected:
        assert_protected_order(order)
    elif allow_plaintext:
        validate_sensitive_values({"SSN": order.get("SSN"), "DOB": order.get("DOB")})
    else:
        raise SensitiveDataError("Encrypt the customer data before running automation.")
    errors = []
    required = set(BASIC_FIELDS) | {
        "Provider",
        "Provider ID",
        "Plan",
        "Base Price",
        "Customer Type",
        "Video",
        "Video Price",
        "Phone",
        "Phone Price",
        "Equipment",
        "Existing ProviderXX Customer",
        "EZ Enroll",
        "Automatic Payments",
        "Mailing Same As Service",
        "Shipping Same As Service",
        "Phone Number",
        "Email Address",
        "SSN",
        "DOB",
        "Installation Type",
    }
    if protected:
        required -= {"SSN", "DOB"}
    optional_blank = {"Prefix", "MI", "Suffix", "Unit Type", "Unit Number"}
    for key in sorted(required):
        value = order.get(key)
        if not isinstance(value, str) or (
            key not in optional_blank and not value.strip()
        ):
            errors.append(
                f"{key}: provide a {'possibly empty ' if key in optional_blank else 'nonempty '}string."
            )

    allowed = {
        "Customer Type": ("New", "Transfer"),
        "Moving?": ("Yes", "No"),
        "Equipment": ("Leased Gateway", "Own Equipment"),
        "Video": ("None", "Ultimate TV", "Popular TV", "World Soccer Ticket"),
        "Phone": ("None", "ProviderXX Voice Premier"),
        "Installation Type": ("STANDARD_SHIPPING", "PRO_INSTALL"),
        "Existing ProviderXX Customer": ("Yes", "No"),
        "EZ Enroll": ("Yes", "No"),
        "Automatic Payments": ("Yes", "No"),
        "Mailing Same As Service": ("Yes",),
        "Shipping Same As Service": ("Yes",),
    }
    for key, values in allowed.items():
        if order.get(key) not in values:
            errors.append(f"{key}: supported choices are {', '.join(values)}.")

    for key in ("Broadband Labels Requested", "NOW TV Streaming", "Sample Payment"):
        if not isinstance(order.get(key, False), bool):
            errors.append(f"{key}: use JSON true or false.")
    if order.get("Account Holder Verified") is not True:
        errors.append("Account Holder Verified: confirmation is required.")
    if order.get("EZ Enroll") == "Yes" and order.get("EZ Enroll Disclosure Read") is not True:
        errors.append("EZ Enroll Disclosure Read: acknowledgement must be JSON true.")
    if (
        order.get("Automatic Payments") == "Yes"
        and order.get("Automatic Payments Disclosure Read") is not True
    ):
        errors.append(
            "Automatic Payments Disclosure Read: acknowledgement is required."
        )

    providers = order.get("Current Providers", [])
    if not isinstance(providers, list) or any(
        not isinstance(p, str) for p in providers
    ):
        errors.append("Current Providers: provide a list of provider names.")
    elif order.get("Customer Type") == "Transfer":
        if len(providers) != 1 or any(
            p not in CURRENT_PROVIDER_CHOICES for p in providers
        ):
            errors.append("Transfer requires exactly one valid Current Provider.")

    for key in ("Base Price", "Video Price", "Phone Price"):
        try:
            amount = Decimal(order[key])
            if not amount.is_finite() or amount < 0:
                raise ValueError
            service = key.removesuffix(" Price")
            if order.get(service) == "None" and amount != 0:
                errors.append(f"{key}: must be 0.00 when {service} is None.")
        except (KeyError, TypeError, ValueError, InvalidOperation):
            errors.append(f"{key}: provide a finite nonnegative amount.")

    date_keys = [] if protected else ["DOB"]
    if order.get("Moving?") == "Yes":
        date_keys.append("Move Date")
    if order.get("Installation Type") == "PRO_INSTALL":
        date_keys.append("Install Date")
        if order.get("Install Time") not in ("Morning", "Afternoon", "Evening"):
            errors.append("Install Time: choose Morning, Afternoon or Evening.")
    for key in date_keys:
        try:
            value = order[key]
            if not isinstance(value, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", value
            ):
                raise ValueError
            date.fromisoformat(value)
        except (KeyError, ValueError, TypeError):
            errors.append(f"{key}: provide a valid YYYY-MM-DD date.")

    if order.get("Dwelling Type") == "Apartment":
        for key in ("Unit Type", "Unit Number"):
            if not isinstance(order.get(key), str) or not order[key].strip():
                errors.append(f"{key}: required for an apartment.")
    if not re.fullmatch(r"\d{5}", str(order.get("Zip", ""))):
        errors.append("Zip: provide five digits as a string.")
    if not re.fullmatch(r"\d{10}", str(order.get("Phone Number", ""))):
        errors.append("Phone Number: provide ten digits as a string.")
    if not re.fullmatch(
        r"[^\s@]+@[^\s@]+\.[^\s@]+", str(order.get("Email Address", ""))
    ):
        errors.append("Email Address: provide a valid email format.")
    # Format only: test fixtures are not validated as real SSNs.
    if not protected and not re.fullmatch(
        r"(?:\d{9}|\d{3}-\d{2}-\d{4})", str(order.get("SSN", ""))
    ):
        errors.append(
            "SSN: provide the simulator test fixture in nine-digit or dashed format."
        )
    if errors:
        raise ValueError(
            "Fix the selected customer data:\n- " + "\n- ".join(dict.fromkeys(errors))
        )


def click_forward(scope):
    button = scope.get_by_role("button", name="Forward >", exact=True).filter(
        visible=True
    )
    expect(button).to_be_visible(timeout=TIMEOUT_MS)
    expect(button).to_be_enabled()
    button.click()


def pause_for_review(message):
    try:
        input(message)
    except (EOFError, KeyboardInterrupt):
        pass


def exact_text(value):
    """Allow whitespace/case changes without matching another plan term."""
    words = re.split(r"\s+", value.strip())
    return re.compile(r"^\s*" + r"\s+".join(map(re.escape, words)) + r"\s*$", re.I)


def control(page, label):
    if label in DROPDOWNS:
        option = page.get_by_role("option", name=label, exact=True)
        return page.get_by_role("combobox").filter(has=option)
    if label == "Zip":
        return page.locator('#zipcode:visible, input[aria-label="Zip"]:visible')
    return page.get_by_role("textbox", name=label, exact=True)


def forward_button(page):
    return page.get_by_role("button", name=re.compile(r"^forward\s*>?$", re.I)).filter(
        visible=True
    )


def human_fill(field, value: str) -> None:
    expect(field).to_be_editable()
    if "--fast" in sys.argv:
        field.fill(value)
        return
    field.fill("")

    for index, character in enumerate(value):
        if index > 0:
            time.sleep(random.uniform(0.15, 0.30))
        field.press_sequentially(character)


def fill_basic_information(page, order):
    zip_search = page.locator("#ssZipCode:visible")
    expect(zip_search).to_be_editable(timeout=30_000)
    human_fill(zip_search, order["Zip"])
    zip_search.press("Tab")
    expect(control(page, "Address 1")).to_be_editable(timeout=30_000)

    for label in BASIC_FIELDS:
        field, value = control(page, label), order[label]
        expect(field).to_have_count(1)
        if field.input_value() == value:
            continue
        print(f"Setting {label}...")
        if label in DROPDOWNS:
            field.select_option(value=value)
        else:
            human_fill(field, value)

    if order.get("Lead ID", "").strip():
        lead = page.locator("#rvCallID")
        human_fill(lead, order["Lead ID"])
        expect(lead).to_have_value(order["Lead ID"])

    if order["Moving?"] == "Yes":
        move_date = date.fromisoformat(order["Move Date"]).isoformat()
        # The live Basic Information snapshot exposes a named textbox.
        # Keep native date inputs supported for simulator variants.
        field = (
            page.get_by_role("textbox", name="Move-In Date", exact=True)
            .or_(page.locator('input[type="date"]'))
            .filter(visible=True)
        )
        expect(field).to_have_count(1, timeout=15_000)
        expect(field).to_be_editable(timeout=15_000)
        field.click()
        field.fill(move_date)
        field.press("Tab")
        expect(field).to_have_value(move_date)

    for label in BASIC_FIELDS:
        expect(control(page, label)).to_have_value(order[label])
    forward_button(page).click()
    expect(zip_search).to_be_hidden(timeout=30_000)
    print("PASS: Basic Information completed.")


def confirm_address(page):
    ready = (
        page.locator("p")
        .filter(
            has_text=re.compile(r"Address\s*Matched\s*Proceed to the next screen", re.I)
        )
        .filter(visible=True)
    )
    expect(ready).to_have_count(1, timeout=30_000)
    expect(ready).to_be_visible()
    forward_button(page).click()
    expect(ready).to_be_hidden(timeout=30_000)
    print("PASS: Address matched; clicked Forward.")


def named_choice(scope, kind, label):
    """Support accessible labels and the legacy input + adjacent-text layout.

    Screenshot-only screens still require live validation. Ambiguous matches
    deliberately stop instead of choosing an arbitrary radio or checkbox.
    """
    accessible = scope.get_by_role(kind, name=label, exact=True).filter(visible=True)
    if accessible.count() == 1:
        return accessible
    inputs = scope.locator(f'input[type="{kind}"]:visible')
    descriptions = inputs.evaluate_all("""elements => elements.map(el => {
        const labels = Array.from(el.labels || []).map(x => x.textContent);
        for (const attr of ['aria-label', 'title']) {
            if (el.getAttribute(attr)) labels.push(el.getAttribute(attr));
        }
        let text = '';
        for (let node = el.nextSibling; node; node = node.nextSibling) {
            if (node.nodeType === 1 &&
                (node.matches('input, br') || node.querySelector('input'))) break;
            text += node.textContent || '';
            if (text.length > 200) break;
        }
        labels.push(text);
        return labels.map(x => x.replace(/\\s+/g, ' ').trim());
    })""")
    # Only a displayed price may follow the name. ProviderXX must not match
    # ProviderXX IG, and 1 Gig must not match 1 Gig (a different pricing term).
    prefix = re.compile(r"^" + re.escape(label) + r"\s+\$[\d,.]+(?:\s|$)", re.I)
    matches = [
        i
        for i, names in enumerate(descriptions)
        if any(exact_text(label).fullmatch(n) or prefix.match(n) for n in names)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Could not uniquely identify {kind} '{label}'. "
            "Keep the browser open and inspect this screen."
        )
    return inputs.nth(matches[0])


def qualify_customer(page, order):
    customer_type = order["Customer Type"]
    if customer_type not in ("New", "Transfer"):
        raise ValueError("Customer Type must be New or Transfer.")

    question = page.get_by_text("Is this a new or transfer customer?", exact=True)
    expect(question).to_be_visible(timeout=30_000)

    section = question.locator('xpath=ancestor::*[count(.//input[@type="radio"])=2][1]')
    radios = section.locator('input[type="radio"]:visible')
    expect(radios).to_have_count(2)

    selected = radios.nth(0 if customer_type == "New" else 1)
    selected.check()
    expect(selected).to_be_checked()

    if customer_type == "Transfer":
        choices = CURRENT_PROVIDER_CHOICES
        providers = order.get("Current Providers", [])

        if not providers or set(providers) - set(choices):
            raise ValueError("Transfer requires a valid current provider.")

        provider_question = page.get_by_text(
            "Who is your current service provider?", exact=True
        )
        expect(provider_question).to_be_visible()

        provider_section = provider_question.locator(
            'xpath=ancestor::*[count(.//input[@type="checkbox"])=6][1]'
        )
        boxes = provider_section.locator('input[type="checkbox"]:visible')
        expect(boxes).to_have_count(6)

        # Checkbox order matches your screenshot.
        for index, provider in enumerate(choices):
            wanted = provider in providers
            boxes.nth(index).set_checked(wanted)
            expect(boxes.nth(index)).to_be_checked(checked=wanted)

    forward = page.get_by_role("button", name="Forward >", exact=True).filter(
        visible=True
    )

    expect(forward).to_be_enabled()
    forward.click()

    expect(page.locator("#content_header .pageTitle")).to_have_text(
        "Recommendations", timeout=30_000
    )


def check_provider_availability(page, order):
    row = page.locator("#rts_widget_content tr").filter(
        has=page.get_by_role("cell", name=order["Provider"], exact=True)
    )

    expect(row).to_have_count(1, timeout=30_000)
    expect(row.locator("td").nth(1)).to_have_text(
        exact_text("Available"), timeout=60_000
    )
    print(f"PASS: {order['Provider']} is Available.")

    internet = (
        page.locator("nav a")
        .filter(has=page.locator('img[alt="Internet"]'))
        .filter(visible=True)
    )

    expect(internet).to_have_count(1)
    internet.click()

    expect(page.locator(".productTitle:visible").first).to_be_visible(timeout=30_000)

    print("Internet plans page opened.")


def select_internet_offer(page, order):
    title = page.locator(".productTitle").filter(has_text=exact_text(order["Plan"]))

    card = page.locator(".syncbdlmain").filter(has=title).filter(visible=True)

    provider_id = order.get("Provider ID")
    if not provider_id:
        raise ValueError("Add the selected provider's Provider ID to ORDERS.")

    card = card.filter(has=page.get_by_alt_text(str(provider_id), exact=True))
    expect(card).to_have_count(1, timeout=30_000)

    card.get_by_role("button", name="View Details", exact=True).click()

    expect(page.locator("#product_name:visible")).to_have_text(
        exact_text(order["Plan"]), timeout=30_000
    )

    expect(page.locator("#product_basePriceInfo:visible")).to_have_text(
        f'${Decimal(order["Base Price"]):.2f}',
        timeout=15_000,
    )

    checkout = page.get_by_role(
        "button",
        name="Add to Order and Checkout",
        exact=True,
    ).filter(visible=True)

    expect(checkout).to_be_enabled(timeout=15_000)
    checkout.click()

    expect(page.locator("#checkoutContent")).to_be_visible(timeout=30_000)


def product_section(page, heading):
    """Find the smallest VIDEO/PHONE section containing its own radios."""
    header = page.get_by_text(heading, exact=True).filter(visible=True)
    expect(header).to_have_count(1)
    # A section may use divs or table rows; don't assume unprovided HTML IDs.
    section = header.locator('xpath=ancestor::*[.//input[@type="radio"]][1]')
    expect(section).to_have_count(1)
    for other in {"VIDEO", "INTERNET", "PHONE"} - {heading}:
        if section.get_by_text(other, exact=True).filter(visible=True).count():
            raise ValueError(
                f"Cannot isolate {heading} options from other product sections."
            )
    return section


def customize_products(page, order):
    checkout = page.frame_locator("#checkoutContent")

    choices = {
        "Video": [
            "Ultimate TV",
            "Popular TV",
            "World Soccer Ticket",
            "None",
        ],
        "Phone": [
            "ProviderXX Voice Premier",
            "None",
        ],
    }

    for heading, key in (("VIDEO", "Video"), ("PHONE", "Phone")):
        requested = order.get(key) or "None"

        if requested not in choices[key]:
            raise ValueError(f"Unknown {key} option: {requested}")

        section = product_section(checkout, heading)
        section.scroll_into_view_if_needed()

        radios = section.locator('input[type="radio"]:visible')
        expect(radios).to_have_count(len(choices[key]))

        option = radios.nth(choices[key].index(requested))
        option.check()
        expect(option).to_be_checked()

        print(f"{key}: {requested}")

    click_forward(checkout)


def set_broadband_labels(page, requested=False):
    checkout = page.frame_locator("#checkoutContent")

    response = (
        "Customer requested the Broadband Fact Labels: providerxx.com/labels"
        if requested
        else "Customer did not request Broadband Fact labels"
    )

    radio = named_choice(checkout, "radio", response)
    radio.check()
    expect(radio).to_be_checked()

    section = radio.locator('xpath=ancestor-or-self::*[.//input[@type="checkbox"]][1]')
    read_box = named_choice(section, "checkbox", "Read")
    read_box.check()
    expect(read_box).to_be_checked()


def customize_equipment(page, order):
    checkout = page.frame_locator("#checkoutContent")

    equipment_choices = ["Leased Gateway", "Own Equipment"]
    requested = order["Equipment"]

    if requested not in equipment_choices:
        raise ValueError(f"Unknown equipment choice: {requested}")

    equipment_header = checkout.get_by_text(re.compile(r"^HSD_EQUIPMENT_AUTO_CHOICE"))
    equipment_section = equipment_header.locator(
        'xpath=ancestor::*[.//input[@type="radio"]][1]'
    )
    radios = equipment_section.locator('input[type="radio"]')
    expect(radios).to_have_count(2)

    selected = radios.nth(equipment_choices.index(requested))
    selected.check()
    expect(selected).to_be_checked()
    print(f"Equipment: {requested}")

    streaming_header = checkout.get_by_text(re.compile(r"^NOW TV WITH STREAMING APPS"))
    streaming_section = streaming_header.locator(
        'xpath=ancestor::*[.//input[@type="radio"]][1]'
    )
    streaming = streaming_section.locator('input[type="radio"]')
    expect(streaming).to_have_count(1)

    if order.get("NOW TV Streaming", False):
        streaming.check()
        expect(streaming).to_be_checked()
    else:
        # Leave the optional service unselected.
        expect(streaming).not_to_be_checked()

    click_forward(checkout)

    expect(
        checkout.get_by_text("Account Holder Qualification:", exact=True)
    ).to_be_visible(timeout=TIMEOUT_MS)
    print("PASS: Equipment completed; Account Holder Qualification opened.")


def qualify_account_holder(page, order):
    checkout = page.frame_locator("#checkoutContent")
    required_controls = []

    if order.get("Account Holder Verified") is not True:
        raise ValueError(
            "Speak to the Account Holder before continuing."
        )

    for key in ("Mailing Same As Service", "Shipping Same As Service"):
        if order.get(key) != "Yes":
            raise ValueError(
                f"{key}: separate address entry is required."
            )

    def answer_yes_no(question, answer, label):
        if answer not in ("Yes", "No"):
            raise ValueError("Expected Yes or No.")

        text = checkout.get_by_text(question).filter(visible=True)
        expect(text).to_have_count(1, timeout=30_000)

        section = text.locator(
            'xpath=ancestor::*'
            '[count(.//input[@type="radio"])=2][1]'
        )
        radios = section.locator('input[type="radio"]')
        expect(radios).to_have_count(2)

        option = radios.nth(0 if answer == "Yes" else 1)
        option.scroll_into_view_if_needed()

        if not option.is_checked():
            if option.is_disabled():
                raise SensitiveDataError(
                    f"{label}: UI locks a different answer than requested {answer}."
                )
            expect(option).to_be_enabled()
            option.check()

        expect(option).to_be_checked()
        required_controls.append((label, option))

    def acknowledge_disclosure(pattern, flag):
        if order.get(flag) is not True:
            raise SensitiveDataError(
                f"{flag}: read the disclosure and "
                "set this flag to JSON true."
            )

        text = checkout.get_by_text(pattern).filter(visible=True)
        expect(text).to_have_count(1, timeout=30_000)

        section = text.locator(
            'xpath=ancestor-or-self::*'
            '[.//input[@type="checkbox"]][1]'
        )
        box = section.locator(
            'input[type="checkbox"]:visible'
        )
        expect(box).to_have_count(1)

        if not box.is_checked():
            expect(box).to_be_enabled()
            box.check()

        expect(box).to_be_checked()
        required_controls.append((flag, box))

    answer_yes_no(
        re.compile(
            r"Agent\s*[–—-]?\s*Verify that you are speaking"
        ),
        "Yes",
        "Account Holder Verified",
    )

    phone = checkout.get_by_placeholder(
        "Phone Number", exact=True
    )
    email = checkout.get_by_placeholder(
        "Email Address", exact=True
    )

    expect(phone).to_be_visible(timeout=15_000)
    expect(phone).to_be_editable()
    human_fill(phone, order["Phone Number"])
    phone.press("Tab")

    expect(email).to_be_visible(timeout=15_000)
    human_fill(email, order["Email Address"])
    email.press("Tab")

    answer_yes_no(
        re.compile(
            r"are you a current ProviderXX customer\?",
            re.I,
        ),
        order["Existing ProviderXX Customer"],
        "Existing ProviderXX Customer",
    )

    answer_yes_no(
        re.compile(
            r"Does your customer want to continue with EZ-Enroll\?",
            re.I,
        ),
        order["EZ Enroll"],
        "EZ Enroll",
    )

    if order["EZ Enroll"] == "Yes":
        acknowledge_disclosure(
            re.compile(
                r"By signing up for\s+ProviderXX\s+EZ[-\s]*Enroll",
                re.I,
            ),
            "EZ Enroll Disclosure Read",
        )
    answer_yes_no(
        re.compile(
            r"Does your customer want to continue with "
            r"Automatic Payments and Paperless Billing Discount\?",
            re.I,
        ),
        order["Automatic Payments"],
        "Automatic Payments",
    )

    if order["Automatic Payments"] == "Yes":
        acknowledge_disclosure(
            re.compile(
                r"You have opted into ProviderXX['’]s\s+Automatic Payments",
                re.I,
            ),
            "Automatic Payments Disclosure Read",
        )

    mailing_question = checkout.get_by_text(
        re.compile(
            r"Is your mailing address the same as "
            r"service address\s*\?",
            re.I,
        )
    ).filter(visible=True)

    expect(mailing_question).to_have_count(
        1, timeout=30_000
    )

    address_section = mailing_question.locator(
        'xpath=ancestor::*'
        '[count(.//input[@type="radio"])=4][1]'
    )
    radios = address_section.locator(
        'input[type="radio"]'
    )
    expect(radios).to_have_count(4)

    # Mailing Yes/No, followed by Shipping Yes/No.
    for base, key in ((0, "Mailing Same As Service"), (2, "Shipping Same As Service")):
        index = base + (0 if order[key] == "Yes" else 1)
        option = radios.nth(index)

        if not option.is_checked():
            expect(option).to_be_enabled()
            option.check()

        expect(option).to_be_checked()
        required_controls.append((key, option))

    for label, option in required_controls:
        if not option.is_checked():
            raise SensitiveDataError(f"Account Holder Qualification: {label} is no longer selected.")
    click_forward(checkout)

    try:
        expect(checkout.get_by_text(
            "Installation and Payment:", exact=True,
        )).to_be_visible(timeout=TIMEOUT_MS)
    except (Error, AssertionError):
        # Never include page text, field values or raw Playwright diagnostics.
        diagnostics = "validation state unavailable"
        try:
            invalid = checkout.locator('input:invalid:visible, select:invalid:visible, [aria-invalid="true"]:visible').count()
            alerts = checkout.locator('[role="alert"]:visible').count()
            unchecked = sum(not control.is_checked() for _, control in required_controls)
            diagnostics = f"invalid controls={invalid}; visible alerts={alerts}; unchecked required controls={unchecked}"
        except Error:
            pass
        raise SensitiveDataError(
            "Account Holder Qualification navigation blocked after one Forward click; "
            + diagnostics + ". Review the simulator validation messages."
        ) from None

    print(
        "PASS: Account Holder Qualification completed; "
        "Installation and Payment opened."
    )

def fill_sensitive_field(field, value, label):
    """Blur before filling, then report a fixed error message on any failure."""
    try:
        require_private_browser_logging()
        expect(field).to_have_count(1)
        expect(field).to_be_editable()
        field.evaluate("""element => {
            element.setAttribute('data-allconnect-sensitive', 'true');
            element.style.setProperty('filter', 'blur(7px)', 'important');
            element.style.setProperty('caret-color', 'transparent', 'important');
        }""")
        field.fill(value)
        field.press("Tab")
        actual = field.input_value()
        if label in {"SSN", "Card Number"}:
            matches = re.sub(r"\D", "", actual) == re.sub(r"\D", "", value)
        else:
            matches = actual == value
        if not matches:
            raise SensitiveDataError("Sensitive field value did not match after entry.")
    except (Error, AssertionError, ValueError):
        raise SensitiveDataError(
            "Sensitive field entry failed. Check the input format and selector."
        ) from None


def _log_payment_state(page, stage):
    """Print value-free checkout state for payment-flow diagnosis."""
    parsed = urlparse(page.url)
    safe_page_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    print(f"PAYMENT DIAGNOSTIC [{stage}]: page_url={safe_page_url}", flush=True)
    heading_allowlist = re.compile(
        r"^(?:ProviderXX Checkout|Installation and Payment|Select Installation Type|"
        r"CREDIT CHECK|ORDER QUALIFICATION STATUS|PAYMENT INFORMATION|ORDER SUMMARY)$",
        re.I,
    )
    known_text = {
        "hold_disclosure": re.compile(r"To confirm the card has sufficient funds", re.I),
        "payment_heading": re.compile(r"^PAYMENT INFORMATION$", re.I),
        "add_card": re.compile(r"^Add a new credit card$", re.I),
        "card_number_label": re.compile(r"^Card Number\s*:$", re.I),
        "forward_button": re.compile(r"^Forward\s*>$", re.I),
        "confirm_installation_button": re.compile(r"^Confirm Installation\s*>$", re.I),
        "installation_confirmed": re.compile(
            r"^Installation (?:confirmed|scheduled|successfully confirmed)$", re.I
        ),
        "order_summary": re.compile(r"^ORDER SUMMARY$", re.I),
    }
    for index, frame in enumerate(page.frames):
        frame_url = urlparse(frame.url)
        safe_frame_url = f"{frame_url.scheme}://{frame_url.netloc}{frame_url.path}"
        try:
            headings = []
            for heading in frame.locator("h1,h2,h3,h4").all_inner_texts():
                normalized = re.sub(r"\s+", " ", heading).strip()
                if heading_allowlist.fullmatch(normalized):
                    headings.append(normalized)
            observations = {}
            for key, pattern in known_text.items():
                matches = frame.get_by_text(pattern).filter(visible=True)
                observations[key] = {
                    "count": matches.count(),
                    "visible": matches.count() > 0,
                }
            observations["visible_checkboxes"] = frame.locator(
                'input[type="checkbox"]:visible'
            ).count()
            observations["visible_radios"] = frame.locator(
                'input[type="radio"]:visible'
            ).count()
            observations["visible_inputs"] = frame.locator(
                "input:visible,select:visible,textarea:visible"
            ).count()
            print(
                f"PAYMENT DIAGNOSTIC [{stage}]: frame={index} "
                f"name={frame.name!r} url={safe_frame_url} "
                f"headings={headings!r} controls={observations}",
                flush=True,
            )
        except Error:
            print(
                f"PAYMENT DIAGNOSTIC [{stage}]: frame={index} "
                f"name={frame.name!r} url={safe_frame_url} state=unavailable",
                flush=True,
            )


def complete_installation(page, order, *, advance=True):
    print("INSTALLATION FLOW: confirm installation")

    installation_page_url = page.url
    checkout = page.frame_locator("#checkoutContent")
    installation_type = order["Installation Type"]

    if installation_type not in (
        "STANDARD_SHIPPING",
        "PRO_INSTALL",
    ):
        raise ValueError("Choose STANDARD_SHIPPING or PRO_INSTALL.")

    install_date = ""
    install_time = ""

    required_read_boxes = []
    if installation_type == "PRO_INSTALL":
        install_date = date.fromisoformat(order["Install Date"]).isoformat()

        install_time = order["Install Time"]

        if install_time not in (
            "Morning",
            "Afternoon",
            "Evening",
        ):
            raise ValueError("Install Time must be Morning, Afternoon or Evening.")

    if order.get("EZ Enroll") not in ("Yes", "No"):
        raise SensitiveDataError("EZ Enroll: supported choices are Yes, No.")
    if order["EZ Enroll"] == "Yes":
        if order.get("EZ Enroll Disclosure Read") is not True:
            raise SensitiveDataError("EZ Enroll Disclosure Read: acknowledgement must be JSON true.")
        print("PASS: EZ Enroll selected; credit check skipped.")
    else:
        # Credit check.
        credit_heading = checkout.get_by_text(
            "CREDIT CHECK",
            exact=True,
        )
        expect(credit_heading).to_be_visible(timeout=30_000)

        credit = credit_heading.locator(
            'xpath=ancestor::*[.//input[not(@type="hidden")]][1]'
        )

        fields = credit.locator(
            'input:visible:not([type="hidden"])'
            ':not([type="button"])'
            ':not([type="submit"])'
            ':not([type="radio"])'
            ':not([type="checkbox"])'
        )
        expect(fields).to_have_count(2)

        private = decrypt_sensitive(order)

        try:
            fill_sensitive_field(
                fields.nth(0),
                private["SSN"],
                "SSN",
            )

            dob = date.fromisoformat(private["DOB"])
            dob_field = fields.nth(1)

            dob_value = (
                dob.isoformat()
                if dob_field.get_attribute("type") == "date"
                else dob.strftime("%m/%d/%Y")
            )

            fill_sensitive_field(
                dob_field,
                dob_value,
                "DOB",
            )

            credit_button = checkout.get_by_role(
                "button",
                name="Run Credit Check",
                exact=True,
            )
            expect(credit_button).to_be_enabled()
            credit_button.click()

        except (Error, AssertionError, ValueError):
            raise SensitiveDataError(
                "Sensitive credit-check entry failed. "
                "Check field selectors and input formats."
            ) from None

        finally:
            private.clear()
            dob = None
            dob_value = None

        status_heading = checkout.get_by_text(
            "ORDER QUALIFICATION STATUS",
            exact=True,
        )
        expect(status_heading).to_be_visible(timeout=60_000)

        status = status_heading.locator(
            'xpath=ancestor::*[contains(., "Processing Status:")][1]'
        )

        expect(status).to_contain_text(
            re.compile(r"Processing Status:\s*SUCCESS"),
            timeout=60_000,
        )
        expect(status).to_contain_text(
            re.compile(r"LineItem Status:\s*SUCCESS"),
            timeout=60_000,
        )

        print("PASS: Order qualification succeeded.")

    # Select installation.
    heading = checkout.get_by_text(re.compile(r"^Select Installation Type"))

    section = heading.locator('xpath=ancestor::*[.//input[@type="radio"]][1]')
    radios = section.locator('input[type="radio"]:visible')
    expect(radios).to_have_count(2)

    selected = radios.nth(0 if installation_type == "STANDARD_SHIPPING" else 1)
    selected.check()
    expect(selected).to_be_checked()

    date_field = checkout.locator('input[type="date"]:visible')
    time_select = checkout.locator("select:visible").filter(
        has=checkout.locator("option").filter(has_text=re.compile(r"^Afternoon$"))
    )

    if installation_type == "PRO_INSTALL":

        def check_read(disclosure):
            text = checkout.get_by_text(disclosure).filter(visible=True)
            expect(text).to_have_count(1)

            box = text.locator(
                'xpath=ancestor-or-self::*[.//input[@type="checkbox"]][1]'
            ).locator('input[type="checkbox"]:visible')

            expect(box).to_have_count(1)
            expect(box).to_be_enabled()
            box.check()
            expect(box).to_be_checked()
            return box

        adult_read = check_read(
            re.compile(r"Someone over the age of 18 must be available")
        )

        expect(date_field).to_have_count(1)
        expect(date_field).to_be_editable()

        date_field.fill(install_date)
        date_field.press("Tab")
        expect(date_field).to_have_value(install_date)

        expect(time_select).to_have_count(1)
        time_select.select_option(label=install_time)
        expect(time_select.locator("option:checked")).to_have_text(install_time)

        scheduling_read = check_read(
            re.compile(r"Availability of installation dates and times")
        )
        required_read_boxes = [adult_read, scheduling_read]

    # Do not ask the simulator to confirm until the requested installation
    # option and every required acknowledgment are in the accepted UI state.
    confirm = checkout.get_by_role(
        "button", name="Confirm Installation >", exact=True
    )
    expect(confirm).to_be_enabled()

    forward = checkout.get_by_role(
        "button", name="Forward >", exact=True
    )
    # Some simulator states do not create Forward until Confirm Installation
    # succeeds. Observe it if present, but do not require it before confirming.
    forward_count_before = forward.count()
    forward_was_visible = forward_count_before == 1 and forward.is_visible()
    forward_was_enabled = forward_was_visible and forward.is_enabled()
    expect(selected).to_be_checked()
    for box in required_read_boxes:
        expect(box).to_be_checked()
        expect(box).to_be_enabled()
    if installation_type == "PRO_INSTALL":
        expect(date_field).to_have_value(install_date)
        expect(time_select.locator("option:checked")).to_have_text(install_time)

    frame_element = page.locator("iframe#checkoutContent").element_handle()
    checkout_frame = frame_element.content_frame() if frame_element else None
    if checkout_frame is None:
        raise SensitiveDataError(
            "Installation confirmation cannot be verified in checkoutContent; "
            "Forward was not clicked."
        )

    warning_text = re.compile(
        r"Read the MANDATORY statement to the customer and tick Read", re.I
    )
    warning = checkout.get_by_text(warning_text).filter(visible=True)
    parent_warning = page.get_by_text(warning_text).filter(visible=True)
    dialog_seen = []

    def stop_on_dialog(dialog):
        # Dismiss only to release the browser automation; any dialog blocks the
        # workflow, so no Forward action follows it.
        dialog_seen.append(dialog.type)
        dialog.dismiss()

    page.once("dialog", stop_on_dialog)
    confirm.click()
    if dialog_seen:
        raise SensitiveDataError(
            "Installation confirmation was blocked by a simulator dialog; "
            "Forward was not clicked."
        )

    # Wait for either a simulator validation response or an observable accepted
    # confirmation state. This waits on DOM state, not a fixed delay.
    try:
        checkout_frame.wait_for_function(
            """({forwardWasVisible, forwardWasEnabled}) => {
                const visible = el => {
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden'
                        && el.getClientRects().length > 0;
                };
                const norm = el => (el.innerText || el.textContent || '')
                    .replace(/\\s+/g, ' ').trim();
                const docs = [document];
                try { if (window.top.document !== document) docs.push(window.top.document); }
                catch (_) {}
                const content = docs.map(d => d.body?.innerText || '').join(' ');
                const validation = /Read the MANDATORY statement to the customer and tick Read/i
                    .test(content) || docs.some(d => [...d.querySelectorAll(
                        '[role="alert"], [role="dialog"], .alert, .error, :invalid'
                    )].some(visible));
                const buttons = [...document.querySelectorAll('button')];
                const confirm = buttons.find(b => /^Confirm Installation\\s*>$/.test(norm(b)));
                const forward = buttons.find(b => /^Forward\\s*>$/.test(norm(b)));
                const success = /Installation (confirmed|scheduled|successfully confirmed)/i
                    .test(content);
                const forwardChanged = forward && visible(forward) && !forward.disabled
                    && (!forwardWasVisible || !forwardWasEnabled);
                return validation || success || !confirm || confirm.disabled || forwardChanged;
            }""",
            arg={"forwardWasVisible": forward_was_visible,
                 "forwardWasEnabled": forward_was_enabled},
            timeout=TIMEOUT_MS,
        )
    except Error:
        raise SensitiveDataError(
            "Installation confirmation acceptance was not observable; "
            "Forward was not clicked."
        ) from None

    alerts = checkout.locator(
        '[role="alert"]:visible, [role="dialog"]:visible, .alert:visible, .error:visible'
    )
    parent_alerts = page.locator(
        '[role="alert"]:visible, [role="dialog"]:visible, .alert:visible, .error:visible'
    )
    invalid = checkout.locator(":visible:invalid")
    warning_count = warning.count() + parent_warning.count()
    alert_count = alerts.count() + parent_alerts.count()
    unchecked_count = sum(not box.is_checked() for box in required_read_boxes)
    invalid_count = invalid.count()
    if warning_count or alert_count or invalid_count or unchecked_count:
        raise SensitiveDataError(
            "Installation confirmation failed simulator validation; "
            f"mandatory_warning={warning_count}, visible_alerts={alert_count}, "
            f"invalid_controls={invalid_count}, unchecked_required={unchecked_count}; "
            "Forward was not clicked."
        )

    success_text = checkout.get_by_text(
        re.compile(r"Installation (?:confirmed|scheduled|successfully confirmed)", re.I)
    ).filter(visible=True)
    accepted = (
        success_text.count() > 0
        or confirm.count() == 0
        or not confirm.is_visible()
        or not confirm.is_enabled()
        or (forward.is_visible() and forward.is_enabled()
            and (not forward_was_visible or not forward_was_enabled))
    )
    if not accepted:
        raise SensitiveDataError(
            "Installation confirmation acceptance was not observable; "
            "Forward was not clicked."
        )

    # EZ Enroll = No keeps the payment section behind the installation
    # Forward transition. Sample-payment runs must take that transition before
    # looking for the hold disclosure. EZ Enroll = Yes already renders payment
    # after confirmation, so its final payment Forward stays owned below.
    sample_payment = "--sample-payment" in sys.argv or order.get("Sample Payment") is True
    if sample_payment:
        _log_payment_state(page, "after installation confirmation")
    if advance:
        click_forward(checkout)
        if sample_payment:
            _log_payment_state(page, "after installation Forward")
            if order.get("EZ Enroll") == "No":
                hold = checkout.get_by_text(
                    re.compile(r"To confirm the card has sufficient funds", re.I)
                ).filter(visible=True)
                try:
                    expect(hold).to_have_count(1, timeout=TIMEOUT_MS)
                except (Error, AssertionError):
                    raise SensitiveDataError(
                        "Installation Forward did not expose the mandatory payment "
                        "hold disclosure; payment entry and payment Forward were not attempted."
                    ) from None
    elif page.url != installation_page_url:
        raise SensitiveDataError(
            "Installation confirmation navigated away before payment was ready."
        )
    print("PASS: Installation confirmation completed; payment stage is ready.")


def complete_sample_payment(page, order):
    """Enter fixed simulator fixtures, then click payment Forward once."""
    # Card values never come from an order snapshot.
    simulator_host = "lemon-forest-0915c3c1e.6.azurestaticapps.net"
    parsed = urlparse(page.url)
    if parsed.scheme != "https" or parsed.hostname != simulator_host:
        raise SensitiveDataError(
            "Sample payment is restricted to the configured simulator host."
        )
    require_private_browser_logging()
    _log_payment_state(page, "before payment step")
    checkout = page.frame_locator("#checkoutContent")
    # The payment form is lower in the same scrollable checkout frame. Scroll
    # the disclosure into view through its own locator, keeping this page open.
    disclosure_pattern = re.compile(
        r"To confirm the card has sufficient funds", re.I
    )
    frame_disclosure = checkout.get_by_text(
        disclosure_pattern
    ).filter(visible=True)
    page_disclosure = page.get_by_text(
        disclosure_pattern
    ).filter(visible=True)
    # Installation confirmation can trigger the payment section to render
    # asynchronously. In --fast runs the next step can otherwise inspect the
    # frame before that disclosure has been added to the DOM.
    disclosure_deadline = time.monotonic() + (TIMEOUT_MS / 1000)
    frame_disclosure_count = 0
    page_disclosure_count = 0
    while True:
        frame_disclosure_count = frame_disclosure.count()
        page_disclosure_count = page_disclosure.count()
        if frame_disclosure_count + page_disclosure_count == 1:
            break
        if frame_disclosure_count + page_disclosure_count > 1:
            break
        remaining = disclosure_deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.25, remaining))
    if frame_disclosure_count + page_disclosure_count != 1:
        _log_payment_state(page, "payment disclosure not found")
        raise SensitiveDataError(
            "Expected one visible payment hold disclosure; "
            f"checkout matches={frame_disclosure_count}, "
            f"page matches={page_disclosure_count}. "
            "Card entry and Forward were not attempted."
        )
    disclosure = frame_disclosure if frame_disclosure_count == 1 else page_disclosure
    original_url = page.url
    disclosure.scroll_into_view_if_needed(timeout=TIMEOUT_MS)
    expect(disclosure).to_be_visible(timeout=TIMEOUT_MS)
    if page.url != original_url:
        raise SensitiveDataError(
            "Payment disclosure scrolling navigated away from Installation and Payment."
        )

    # Accept only the simulator's expected mandatory-disclosure alert. A
    # different alert is dismissed and causes a safe stop below.
    expected_alert = "Read the MANDATORY statement to the customer and tick Read."
    dialog_messages = []

    def handle_payment_dialog(dialog):
        dialog_messages.append(dialog.message)
        if dialog.message.strip() == expected_alert:
            dialog.accept()
        else:
            dialog.dismiss()

    page.on("dialog", handle_payment_dialog)
    read_box = disclosure.locator(
        'xpath=ancestor-or-self::*[.//input[@type="checkbox"]][1]'
    ).locator('input[type="checkbox"]:visible')
    expect(read_box).to_have_count(1)
    try:
        if not read_box.is_checked():
            expect(read_box).to_be_enabled(timeout=TIMEOUT_MS)
            read_box.check(timeout=TIMEOUT_MS)
        expect(read_box).to_be_checked(timeout=TIMEOUT_MS)
    except (Error, AssertionError):
        try:
            checkbox_state = (
                f"visible={read_box.is_visible()}, enabled={read_box.is_enabled()}, "
                f"checked={read_box.is_checked()}"
            )
        except Error:
            checkbox_state = "state=unavailable"
        raise SensitiveDataError(
            "Payment hold Read checkbox interaction failed ("
            f"{checkbox_state}); card entry and Forward were not attempted."
        ) from None
    finally:
        page.remove_listener("dialog", handle_payment_dialog)
    if any(message.strip() != expected_alert for message in dialog_messages):
        raise SensitiveDataError(
            "Unexpected simulator alert blocked payment; card entry and Forward were not attempted."
        )
    if dialog_messages and not read_box.is_checked():
        raise SensitiveDataError(
            "Payment hold Read acknowledgement was not accepted; Forward was not attempted."
        )

    add = checkout.get_by_role(
        "radio", name="Add a new credit card", exact=True
    ).filter(visible=True)
    expect(add).to_have_count(1)
    if not add.is_checked():
        expect(add).to_be_enabled()
        add.check()
    expect(add).to_be_checked()

    payment = add.locator(
        'xpath=ancestor::*[.//input[@type="radio"] and '
        'contains(., "Card Number")][1]'
    )
    expect(payment).to_have_count(1)

    def field_label(label):
        return payment.get_by_text(
            re.compile(r"^\s*" + re.escape(label) + r"\s*:\s*$", re.I)
        ).filter(visible=True)

    def visible_control_if_unique(label, selector):
        text = field_label(label)
        if text.count() != 1:
            return None
        tag = "select" if selector == "select:visible" else "input"
        adjacent = text.locator('xpath=following-sibling::*[1]')
        controls = adjacent.locator(
            f'xpath=self::{tag} | .//{tag}'
        ).filter(visible=True)
        if controls.count() != 1:
            return None
        return controls

    number_probe = visible_control_if_unique("Card Number", "input:visible")
    type_probe = visible_control_if_unique("Card Type", "select:visible")
    fields_unlocked = (
        number_probe is not None
        and type_probe is not None
        and number_probe.is_editable()
        and type_probe.is_enabled()
    )
    edit = payment.get_by_role("button", name="Edit Secure Fields", exact=True)
    if not fields_unlocked:
        try:
            expect(edit).to_have_count(1, timeout=TIMEOUT_MS)
            expect(edit).to_be_visible(timeout=TIMEOUT_MS)
            expect(edit).to_be_enabled(timeout=TIMEOUT_MS)
            edit.click()
        except (Error, AssertionError):
            raise SensitiveDataError(
                "Secure payment fields could not be unlocked; "
                "card entry and Forward were not attempted."
            ) from None

    def row_controls(label, selector, expected_count):
        text = field_label(label)
        expect(text).to_have_count(1, timeout=TIMEOUT_MS)
        tag = "select" if selector == "select:visible" else "input"
        # Live simulator: labels and controls are adjacent siblings in one
        # shared grid. The nearest ancestor contains every card field.
        # Keep the earlier table layout supported via its adjacent value cell.
        adjacent = text.locator('xpath=following-sibling::*[1]')
        controls = adjacent.locator(
            f'xpath=self::{tag} | .//{tag}'
        ).filter(visible=True)
        expect(controls).to_have_count(expected_count, timeout=TIMEOUT_MS)
        return controls

    payment_field_stage = "Card Type"
    try:
        card_type = row_controls("Card Type", "select:visible", 1)
        payment_field_stage = "Card Number"
        inputs = 'input:visible:not([type="hidden"])'
        number = row_controls("Card Number", inputs, 1)
        payment_field_stage = "Card Holder Name"
        name = row_controls("Card Holder Name", inputs, 1)
        payment_field_stage = "CVV Code"
        cvv = row_controls("CVV Code", inputs, 1)
        payment_field_stage = "Expiration Date"
        expiry = row_controls("Expiration Date", inputs, 2)
        for label, field in (
            ("Card Type", card_type), ("Card Number", number),
            ("Card Holder Name", name), ("CVV Code", cvv),
            ("Expiration Date", expiry.nth(0)),
            ("Expiration Date", expiry.nth(1)),
        ):
            payment_field_stage = label
            expect(field).to_be_editable(timeout=TIMEOUT_MS)
    except (Error, AssertionError):
        raise SensitiveDataError(
            f"Required secure payment field unavailable ({payment_field_stage}) after unlock; "
            "card entry and Forward were not attempted."
        ) from None

    # Public dummy fixtures from the supplied simulator example.
    # Actual card acceptance remains subject to the simulator's validation.
    expect(card_type).to_be_enabled()
    card_type.select_option(label="Visa")
    fill_sensitive_field(number, "1234567890123456", "Card Number")
    fill_sensitive_field(name, "DEMO CUSTOMER", "Card Holder Name")
    fill_sensitive_field(cvv, "203", "CVV")
    fill_sensitive_field(expiry.nth(0), "03", "Expiry Month")
    fill_sensitive_field(expiry.nth(1), "2030", "Expiry Year")
    billing_question = payment.get_by_text(
        re.compile(r"Is the credit card billing address the same as service address", re.I)
    ).filter(visible=True)
    expect(billing_question).to_have_count(1)
    billing = billing_question.locator('xpath=ancestor-or-self::*[count(.//input[@type="radio"])=2][1]')
    billing_answer = order.get(
        "Billing Same As Service", order.get("Mailing Same As Service")
    )
    if billing_answer not in ("Yes", "No"):
        raise SensitiveDataError(
            "Billing Same As Service must be Yes or No in the customer order."
        )
    billing_choice = named_choice(billing, "radio", billing_answer)
    if not billing_choice.is_checked():
        expect(billing_choice).to_be_enabled()
        billing_choice.check()
    expect(billing_choice).to_be_checked()

    expect(read_box).to_be_checked()
    expect(add).to_be_checked()
    expect(card_type.locator("option:checked")).to_have_text("Visa")
    if checkout.locator(":visible:invalid").count():
        raise SensitiveDataError(
            "Payment fields failed browser validation; Forward was not clicked."
        )

    click_forward(checkout)
    # Confirm the navigation destination, rather than relying on a heading
    # that differs between EZ Enroll Yes and No render states.
    expect(page).to_have_title("Order Summary", timeout=30_000)
    print("PASS: Sample payment entered with blur; Order Summary opened. Final order NOT submitted.")


def human_pause(min_seconds=0.8, max_seconds=2.0):
    if "--fast" in sys.argv:
        return
    delay = random.uniform(min_seconds, max_seconds)
    print(f"Pausing {delay:.1f} seconds...")
    time.sleep(delay)


def main():
    from run_from_api import main as run_api_main

    run_api_main()


if __name__ == "__main__":
    import sys

    sys.modules["new"] = sys.modules[__name__]

    from run_from_api import main as run_api_main

    run_api_main()
