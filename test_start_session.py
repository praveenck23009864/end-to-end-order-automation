"""Visible Chromium: Basic Information -> address match -> exact internet offer.

Paste the local simulator URL below. Sign in in the Chromium window.
The script continues past product selection, then pauses on the next screen.
It does not submit the final order.
"""

from contextlib import suppress
from datetime import date
from decimal import Decimal
from pathlib import Path
import re
from urllib.parse import urlparse
import json
from playwright.sync_api import Error, expect, sync_playwright

URL = "https://lemon-forest-0915c3c1e.6.azurestaticapps.net/allconnect/"
OUTPUT = Path(__file__).resolve().parent

# Plan names and amounts come from your uploaded example, not a live quote.
# Change these per customer before running. Prices are monthly base prices.

USE_EXTERNAL_ORDERS = True  # False uses your original ORDERS above.

if USE_EXTERNAL_ORDERS:
    orders_file = Path(__file__).resolve().parent / "orders.json"

    with orders_file.open("r", encoding="utf-8-sig") as file:
        ORDERS = json.load(file)
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
        choices = (
            "ProviderVZ 5G",
            "ProviderXX",
            "ProviderOP ST",
            "ProviderAT IG",
            "ProviderTM IG",
            "Others",
        )
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


def open_recommendations(page, order):
    check_provider_availability(page, order)
    forward = page.get_by_role("button", name="Forward >", exact=True).filter(
        visible=True
    )

    expect(forward).to_be_enabled(timeout=15_000)
    forward.click()

    expect(page.locator("#content_header .pageTitle")).to_have_text(
        "Recommendations", timeout=30_000
    )

    print("PASS: Clicked Forward; Recommendations page opened.")


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

    forward = checkout.get_by_role("button", name="Forward >", exact=True)
    expect(forward).to_be_enabled()
    forward.click()


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

    forward = checkout.get_by_role("button", name="Forward >", exact=True)
    expect(forward).to_be_enabled()
    forward.click()

    print("Equipment options selected; clicked Forward.")


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

    forward = checkout.get_by_role("button", name="Forward >", exact=True)
    expect(forward).to_be_enabled()
    forward.click()

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

    forward = checkout.get_by_role(
        "button", name="Forward >", exact=True
    )
    expect(forward).to_be_visible(timeout=30_000)
    expect(forward).to_be_enabled()
    forward.click()

    print("PASS: Installation completed; clicked Forward.")

    print(f"PASS: Confirm Installation clicked for {installation_type}.")


def main():
    parsed = urlparse(URL)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SystemExit(
            "Paste your simulator HTTPS URL into URL at the top of this file."
        )

    choice = input("Choose test customer 1, 2, 3, or 4: ").strip()
    if choice not in ORDERS:
        raise SystemExit("Choose 1, 2, 3, or 4.")

    order = ORDERS[choice]

    if order["Customer Type"] not in ("New", "Transfer"):
        raise SystemExit("Customer Type must be New or Transfer.")

    if order["Customer Type"] == "Transfer":
        providers = order.get("Current Providers", [])

        if len(providers) != 1 or set(providers) - set(CURRENT_PROVIDER_CHOICES):
            raise SystemExit("Set Current Providers to exactly one valid provider.")

    for key in ("Video", "Phone"):
        if order[key] == "None" and Decimal(order[key + " Price"]) != 0:
            raise SystemExit(f"{key} Price must be 0.00 when {key} is None.")

    if order["Moving?"] not in ("Yes", "No"):
        raise SystemExit("Moving? must be Yes or No.")

    if order["Moving?"] == "Yes":
        try:
            date.fromisoformat(order["Move Date"])
        except (KeyError, ValueError, TypeError):
            raise SystemExit("Set a valid Move Date in YYYY-MM-DD format.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=False,
            slow_mo=150,
        )
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.set_default_timeout(30_000)
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

            stage = "filling Basic Information"
            fill_basic_information(page, order)

            stage = "confirming matched address"
            confirm_address(page)

            stage = "selecting New / Transfer and current providers"
            qualify_customer(page, order)

            stage = "checking provider availability"
            check_provider_availability(page, order)

            stage = "selecting internet offer and opening checkout"
            select_internet_offer(page, order)
            stage = "selecting checkout products and clicking Forward"
            customize_products(page, order)

            stage = "setting Broadband Labels response"
            set_broadband_labels(
                page,
                requested=order.get("Broadband Labels Requested", False),
            )

            stage = "selecting equipment and clicking Forward"
            customize_equipment(page, order)

            stage = "completing Account Holder Qualification"
            qualify_account_holder(page, order)

            stage = "running simulator credit check and selecting installation"
            complete_installation(page, order)

            input("Review the next screen. Press Enter to close Chromium: ")

        except (Error, AssertionError, ValueError) as error:
            print(f"STOPPED during {stage}.")
            print(str(error).replace(URL, "[SIMULATOR URL]"))
            print("Run incomplete. Inspect the open browser " "before closing it.")
            input("Press Enter to close Chromium: ")
            raise SystemExit(1) from None

        finally:
            with suppress(Exception):
                browser.close()


if __name__ == "__main__":
    main()
