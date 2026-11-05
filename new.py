"""Visible Chromium: Basic Information -> address match -> exact internet offer.

Paste the local simulator URL below. Sign in in the Chromium window.
The script continues past product selection, then pauses on the next screen.
It does not submit the final order.
"""

from contextlib import suppress
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from urllib.parse import urlparse
import json
from playwright.sync_api import Error, expect, sync_playwright

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
            raise ValueError(
                f"orders.json line {error.lineno}: {error.msg}"
            ) from None
    if not isinstance(data, dict) or not data:
        raise ValueError("Provide a nonempty orders.json or LOCAL_ORDERS dictionary.")
    if any(not isinstance(item, dict) for item in data.values()):
        raise ValueError("Each customer entry must be a JSON object.")
    return data


def validate_order(order):
    """Report missing/invalid data before opening the browser; never print values."""
    errors = []
    required = set(BASIC_FIELDS) | {
        "Provider", "Provider ID", "Plan", "Base Price", "Customer Type",
        "Video", "Video Price", "Phone", "Phone Price", "Equipment",
        "Existing ProviderXX Customer", "EZ Enroll", "Automatic Payments",
        "Mailing Same As Service", "Shipping Same As Service",
        "Phone Number", "Email Address", "SSN", "DOB", "Installation Type",
    }
    optional_blank = {"Prefix", "MI", "Suffix", "Unit Type", "Unit Number"}
    for key in sorted(required):
        value = order.get(key)
        if not isinstance(value, str) or (key not in optional_blank and not value.strip()):
            errors.append(f"{key}: provide a {'possibly empty ' if key in optional_blank else 'nonempty '}string.")

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

    for key in ("Broadband Labels Requested", "NOW TV Streaming"):
        if not isinstance(order.get(key, False), bool):
            errors.append(f"{key}: use JSON true or false.")
    if order.get("Account Holder Verified") is not True:
        errors.append("Account Holder Verified: confirmation is required.")
    if (order.get("Automatic Payments") == "Yes"
            and order.get("Automatic Payments Disclosure Read") is not True):
        errors.append("Automatic Payments Disclosure Read: acknowledgement is required.")

    providers = order.get("Current Providers", [])
    if (not isinstance(providers, list)
            or any(not isinstance(p, str) for p in providers)):
        errors.append("Current Providers: provide a list of provider names.")
    elif order.get("Customer Type") == "Transfer":
        if len(providers) != 1 or any(p not in CURRENT_PROVIDER_CHOICES for p in providers):
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

    date_keys = ["DOB"]
    if order.get("Moving?") == "Yes":
        date_keys.append("Move Date")
    if order.get("Installation Type") == "PRO_INSTALL":
        date_keys.append("Install Date")
        if order.get("Install Time") not in ("Morning", "Afternoon", "Evening"):
            errors.append("Install Time: choose Morning, Afternoon or Evening.")
    for key in date_keys:
        try:
            value = order[key]
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
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
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", str(order.get("Email Address", ""))):
        errors.append("Email Address: provide a valid email format.")
    # Format only: test fixtures are not validated as real SSNs.
    if not re.fullmatch(r"(?:\d{9}|\d{3}-\d{2}-\d{4})", str(order.get("SSN", ""))):
        errors.append("SSN: provide the simulator test fixture in nine-digit or dashed format.")
    if errors:
        raise ValueError("Fix the selected customer data:\n- " + "\n- ".join(dict.fromkeys(errors)))


def click_forward(scope):
    button = scope.get_by_role("button", name="Forward >", exact=True).filter(visible=True)
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


def fill_basic_information(page, order):
    zip_search = page.locator("#ssZipCode:visible")
    expect(zip_search).to_be_editable(timeout=30_000)
    zip_search.fill(order["Zip"])
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
            field.fill(value)

    if order.get("Lead ID", "").strip():
        lead = page.locator("#rvCallID")
        lead.fill(order["Lead ID"])
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

    section = radio.locator('xpath=ancestor::*[.//input[@type="checkbox"]][1]')
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

    # Set True only after the agent has spoken to the account holder.
    if order.get("Account Holder Verified") is not True:

        raise ValueError("Speak to the Account Holder before continuing.")
    # Separate address entry needs an additional implementation.
    for key in ("Mailing Same As Service", "Shipping Same As Service"):
        if order.get(key) != "Yes":
            raise ValueError(f"{key}: separate address entry is required.")

    def answer_yes_no(question, answer):
        if answer not in ("Yes", "No"):
            raise ValueError(f"Expected Yes or No, received: {answer}")

        text = checkout.get_by_text(question).filter(visible=True)
        expect(text).to_have_count(1, timeout=30_000)

        section = text.locator('xpath=ancestor::*[count(.//input[@type="radio"])=2][1]')
        radios = section.locator('input[type="radio"]')
        expect(radios).to_have_count(2)

        option = radios.nth(0 if answer == "Yes" else 1)
        option.scroll_into_view_if_needed()
        if not option.is_checked():
            expect(option).to_be_enabled()
            option.check()
        expect(option).to_be_checked()

    answer_yes_no(
        re.compile(r"Agent\s*[–—-]?\s*Verify that you are speaking"),
        "Yes",
    )

    phone = checkout.get_by_placeholder("Phone Number", exact=True)
    email = checkout.get_by_placeholder("Email Address", exact=True)

    expect(phone).to_be_visible(timeout=15_000)
    expect(phone).to_be_editable()
    phone.fill(order["Phone Number"])
    phone.press("Tab")

    expect(email).to_be_visible(timeout=15_000)
    email.fill(order["Email Address"])
    email.press("Tab")

    answer_yes_no(
        re.compile(r"are you a current ProviderXX customer\?", re.I),
        order["Existing ProviderXX Customer"],
    )

    answer_yes_no(
        re.compile(r"Does your customer want to continue with EZ-Enroll\?", re.I),
        order["EZ Enroll"],
    )

    answer_yes_no(
        re.compile(
            r"Does your customer want to continue with Automatic Payments "
            r"and Paperless Billing Discount\?",
            re.I,
        ),
        order["Automatic Payments"],
    )

    disclosure = checkout.get_by_text(
        re.compile(
            r"You have opted into ProviderXX.s Automatic Payments",
            re.I,
        )
    ).filter(visible=True)

    if order["Automatic Payments"] == "Yes":
        if order.get("Automatic Payments Disclosure Read") is not True:
            raise ValueError("Read the Automatic Payments disclosure first.")

        expect(disclosure).to_have_count(1)
        section = disclosure.locator('xpath=ancestor::*[.//input[@type="checkbox"]][1]')
        read_box = section.locator('input[type="checkbox"]')
        expect(read_box).to_have_count(1)
        read_box.check()
        expect(read_box).to_be_checked()

    mailing_question = checkout.get_by_text(
        re.compile(
            r"Is your mailing address the same as service address\s*\?",
            re.I,
        )
    ).filter(visible=True)

    expect(mailing_question).to_have_count(1)

    address_section = mailing_question.locator(
        'xpath=ancestor::*[count(.//input[@type="radio"])=4][1]'
    )
    radios = address_section.locator('input[type="radio"]')
    expect(radios).to_have_count(4)

    # Mailing Yes/No, followed by Shipping Yes/No.
    for key, index in (
        ("Mailing Same As Service", 0),
        ("Shipping Same As Service", 2),
    ):
        if order[key] != "Yes":
            raise ValueError(f"{key}: separate address entry is required.")

        option = radios.nth(index)
        option.check()
        expect(option).to_be_checked()

    click_forward(checkout)

    expect(checkout.get_by_text("Installation and Payment:", exact=True)).to_be_visible(
        timeout=30_000
    )

    print(
        "PASS: Account Holder Qualification completed; Installation and Payment opened."
    )


def complete_installation(page, order):
    checkout = page.frame_locator("#checkoutContent")
    installation_type = order["Installation Type"]

    if installation_type not in ("STANDARD_SHIPPING", "PRO_INSTALL"):
        raise ValueError("Choose STANDARD_SHIPPING or PRO_INSTALL.")

    install_date = ""
    install_time = ""
    if installation_type == "PRO_INSTALL":
        install_date = date.fromisoformat(order["Install Date"]).isoformat()
        install_time = order["Install Time"]

        if install_time not in ("Morning", "Afternoon", "Evening"):
            raise ValueError("Install Time must be Morning, Afternoon or Evening.")

    # Locate the credit-check section.
    credit_heading = checkout.get_by_text("CREDIT CHECK", exact=True)
    expect(credit_heading).to_be_visible(timeout=30_000)

    credit = credit_heading.locator(
        'xpath=ancestor::*[.//input[not(@type="hidden")]][1]'
    )
    fields = credit.locator(
        'input:visible:not([type="hidden"]):not([type="button"])'
        ':not([type="submit"]):not([type="radio"]):not([type="checkbox"])'
    )
    expect(fields).to_have_count(2)

    # Screenshot order: SSN first, DOB second.
    fields.nth(0).fill(order["SSN"])
    fields.nth(0).press("Tab")

    dob = date.fromisoformat(order["DOB"])
    dob_field = fields.nth(1)
    dob_value = (
        dob.isoformat()
        if dob_field.get_attribute("type") == "date"
        else dob.strftime("%m/%d/%Y")
    )
    dob_field.fill(dob_value)
    dob_field.press("Tab")

    checkout.get_by_role("button", name="Run Credit Check", exact=True).click()

    status_heading = checkout.get_by_text("ORDER QUALIFICATION STATUS", exact=True)
    expect(status_heading).to_be_visible(timeout=60_000)

    status = status_heading.locator(
        'xpath=ancestor::*[contains(., "Processing Status:")][1]'
    )
    expect(status).to_contain_text(
        re.compile(r"Processing Status:\s*SUCCESS"), timeout=60_000
    )
    expect(status).to_contain_text(
        re.compile(r"LineItem Status:\s*SUCCESS"), timeout=60_000
    )

    print("PASS: Order qualification succeeded.")

    heading = checkout.get_by_text(re.compile(r"^Select Installation Type"))
    section = heading.locator('xpath=ancestor::*[.//input[@type="radio"]][1]')
    radios = section.locator('input[type="radio"]:visible')
    expect(radios).to_have_count(2)

    selected = radios.nth(0 if installation_type == "STANDARD_SHIPPING" else 1)
    selected.check()
    expect(selected).to_be_checked()

    if installation_type == "PRO_INSTALL":

        def check_read(disclosure):
            text = checkout.get_by_text(disclosure).filter(visible=True)
            expect(text).to_have_count(1)
            box = text.locator(
                'xpath=ancestor::*[.//input[@type="checkbox"]][1]'
            ).locator('input[type="checkbox"]:visible')
            expect(box).to_have_count(1)
            box.check()
            expect(box).to_be_checked()

        check_read(re.compile(r"Someone over the age of 18 must be available"))

        date_field = checkout.locator('input[type="date"]:visible')
        expect(date_field).to_have_count(1)
        date_field.fill(install_date)
        date_field.press("Tab")
        expect(date_field).to_have_value(install_date)

        time_select = checkout.locator("select:visible").filter(
            has=checkout.locator("option").filter(has_text=re.compile(r"^Afternoon$"))
        )
        expect(time_select).to_have_count(1)
        time_select.select_option(label=install_time)

        check_read(re.compile(r"Availability of installation dates and times"))

    confirm = checkout.get_by_role(
        "button", name="Confirm Installation >", exact=True
    )
    expect(confirm).to_be_enabled()
    confirm.click()

    click_forward(checkout)

    print("PASS: Installation completed; clicked Forward.")




def main():
    parsed = urlparse(URL)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SystemExit(
            "Paste your simulator HTTPS URL into URL at the top of this file."
        )

    try:
        orders = load_orders()
        available = ", ".join(orders)
        choice = input(f"Choose test customer {available}: ").strip()
        if choice not in orders:
            raise ValueError(f"Choose one of: {available}.")
        order = orders[choice]
        validate_order(order)
    except (ValueError, KeyboardInterrupt, EOFError) as error:
        raise SystemExit(str(error) or "Cancelled.") from None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=False,
            slow_mo=SLOW_MO_MS,
        )
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.set_default_timeout(TIMEOUT_MS)
        stage = "opening simulator"

        try:
            page.goto(
                URL,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            print("Sign in inside Chromium. " "Automation will continue after login.")

            stage = "waiting for login / session screen"
            zip_search = page.locator("#ssZipCode:visible")

            start = page.get_by_text(re.compile(r"^start session$", re.I)).filter(
                visible=True
            )

            forward = forward_button(page)

            start.or_(zip_search).or_(forward).first.wait_for(
                state="visible",
                timeout=180_000,
            )

            if start.is_visible():
                start.click()
                forward.or_(zip_search).first.wait_for(
                    state="visible",
                    timeout=30_000,
                )

            if not zip_search.is_visible():
                forward.click()

            steps = (
                ("filling Basic Information", lambda: fill_basic_information(page, order)),
                ("confirming matched address", lambda: confirm_address(page)),
                ("qualifying New / Transfer", lambda: qualify_customer(page, order)),
                ("checking provider availability", lambda: check_provider_availability(page, order)),
                ("selecting internet offer", lambda: select_internet_offer(page, order)),
                ("selecting checkout products", lambda: customize_products(page, order)),
                ("setting Broadband Labels", lambda: set_broadband_labels(
                    page, requested=order.get("Broadband Labels Requested", False))),
                ("selecting equipment", lambda: customize_equipment(page, order)),
                ("qualifying Account Holder", lambda: qualify_account_holder(page, order)),
                ("credit check and installation", lambda: complete_installation(page, order)),
            )
            for stage, action in steps:
                print(f"STEP: {stage}")
                action()

            pause_for_review("Review the next screen. Press Enter to close Chromium: ")

        except (Error, AssertionError, ValueError, KeyError) as error:
            print(f"STOPPED during {stage}.")
            message = str(error).replace(URL, "[SIMULATOR URL]")
            for key in ("SSN", "DOB", "Phone Number", "Email Address"):
                value = order.get(key)
                if isinstance(value, str) and value:
                    message = message.replace(value, f"[{key}]")
            print(message)
            print("Run incomplete. Inspect the open browser before closing it.")
            if not page.is_closed():
                pause_for_review("Press Enter to close Chromium: ")
            raise SystemExit(1) from None
        except KeyboardInterrupt:
            print(f"Cancelled during {stage}.")
            raise SystemExit(130) from None

        finally:
            with suppress(Exception):
                browser.close()


if __name__ == "__main__":
    main()
