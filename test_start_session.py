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

from playwright.sync_api import Error, expect, sync_playwright



URL = "*********"
OUTPUT = Path(__file__).resolve().parent

# Plan names and amounts come from your uploaded example, not a live quote.
# Change these per customer before running. Prices are monthly base prices.
ORDERS = {
    "1": {
        "Prefix": "Mr", "First Name": "Alex", "MI": "",
        "Last Name": "Taylor", "Suffix": "", "Zip": "12345",
        "Address 1": "14 Sample Road", "Unit Type": "", "Unit Number": "",
        "City": "Schenectady", "State": "NY", "Dwelling Type": "House",
        "Own / Rent": "Own", "Moving?": "No", "Lead ID": "",
        "Provider": "ProviderXX", "Plan": "300 Mbps (1-YEAR PRICING)",
        "Base Price": "40.00",
        "Customer Type": "New", "Current Providers": [],
        "Video": "World Soccer Ticket",
        "Video Price": "85.00" ,
        "Phone": "None",
        "Phone Price": "0.00",
    },
    "2": {
        "Prefix": "Ms", "First Name": "Jordan", "MI": "R",
        "Last Name": "Lee", "Suffix": "", "Zip": "02108",
        "Address 1": "25 Example Lane", "Unit Type": "Apartment",
        "Unit Number": "4B", "City": "Boston", "State": "MA",
        "Dwelling Type": "Apartment", "Own / Rent": "Rent",
        "Moving?": "Yes", "Move Date": "2026-11-15", "Lead ID": "",
        "Provider": "ProviderXX", "Plan": "300 Mbps (5-YEAR PRICE GUARANTEE)",
        "Base Price": "45.00",
        "Customer Type": "Transfer",
        "Current Providers": ["ProviderXX"],
        "Video": "None", "Video Price": "0.00",
        "Phone": "None", "Phone Price": "0.00",
    },
}

DROPDOWNS = {
    "Prefix", "Suffix", "State", "Unit Type", "Dwelling Type",
    "Own / Rent", "Moving?",
}
BASIC_FIELDS = (
    "Prefix", "First Name", "MI", "Last Name", "Suffix", "Address 1",
    "Dwelling Type", "Unit Type", "Unit Number", "City", "State", "Zip",
    "Own / Rent", "Moving?",
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
    return page.get_by_role("button", name=re.compile(r"^forward\s*>?$", re.I)).filter(visible=True)


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
        field = page.get_by_role(
            "textbox", name="Move-In Date", exact=True
        ).or_(page.locator('input[type="date"]')).filter(visible=True)
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
    ready = page.locator("p").filter(
        has_text=re.compile(r"Address\s*Matched\s*Proceed to the next screen", re.I)
    ).filter(visible=True)
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
    accessible = scope.get_by_role(kind, name=exact_text(label)).filter(visible=True)
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
    matches = [i for i, names in enumerate(descriptions)
               if any(exact_text(label).fullmatch(n) or prefix.match(n) for n in names)]
    if len(matches) != 1:
        raise ValueError(
            f"Could not uniquely identify {kind} '{label}'. "
            "Keep the browser open and inspect this screen."
        )
    return inputs.nth(matches[0])


def qualify_customer(page, order):
    named_choice(page, "radio", "Transfer").check()

    provider = named_choice(page, "checkbox", "ProviderXX")
    provider.check()
    expect(provider).to_be_checked()

    forward_button(page).click()

    expect(
        page.locator("#content_header .pageTitle").filter(
            has_text=re.compile(r"^\s*Recommendations\s*$", re.I)
        )
    ).to_be_visible(timeout=30_000)

    print("PASS: ProviderXX checked; Recommendations page opened.")


CURRENT_PROVIDER_CHOICES = (
    "ProviderVZ 5G", "ProviderXX", "ProviderOP ST",
    "ProviderAT IG", "ProviderTM IG", "Others",
)


def check_provider_availability(page, order):
    rows = page.locator("#rts_widget_content tr")
    expect(rows.first).to_be_visible(timeout=30_000)
    provider_cell = page.get_by_text(order["Provider"], exact=True)
    row = rows.filter(has=provider_cell)
    expect(row).to_have_count(1)
    # Exact Available avoids treating Not Available or Pending as a match.
    status = row.locator("td").nth(1)
    expect(status).to_have_text(exact_text("Available"), timeout=60_000)
    print(f"PASS: {order['Provider']} is Available.")
    # ProviderXX logo on the current Recommendations screen.
    provider_card = page.locator("#firstRankProviderImg:visible")
    expect(provider_card).to_be_visible(timeout=30_000)
    provider_card.click()

    expect(
        page.locator(".productTitle:visible").first
    ).to_be_visible(timeout=30_000)

    print("Provider plans page opened.")


def select_internet_offer(page, order):
    # The supplied Recommendations.html contains this Internet navigation link.
    internet = page.locator('nav a').filter(
        has=page.locator('img[alt="Internet"]')
    ).filter(visible=True)
    expect(internet).to_have_count(1, timeout=30_000)
    # A provider click can open offers directly. Otherwise use Internet.
    if not page.locator(".productTitle:visible").count():
        internet.click()

    # Scope View Details to the exact title, including the pricing term.
    title = page.locator(".productTitle").filter(has_text=exact_text(order["Plan"]))
    card = page.locator(".syncbdlmain").filter(has=title).filter(visible=True)
    expect(card).to_have_count(1, timeout=30_000)

    # Check provider identity from the card metadata, not a different offer.
    metadata = card.locator("input.singleHiddenJson").evaluate(
        "element => JSON.parse(element.value)"
    )
    if metadata.get("providerName", "").casefold() != order["Provider"].casefold():
        raise ValueError("The exact plan card belongs to a different provider.")

    expected_price = Decimal(order["Base Price"])
    actual_price = Decimal(card.locator("input.baseRecPrice").input_value())
    if actual_price != expected_price:
        raise ValueError(
            f"Plan base price changed: expected ${expected_price:.2f}, "
            f"found ${actual_price:.2f}. Review the offer."
        )

    card.get_by_role("button", name="View Details", exact=True).click()

    # Product Details selectors are present in Recommendations1.html.
    details = page.locator("#contentpdet:visible").filter(
        has=page.locator("#product_name:visible")
    )
    expect(details.locator("#product_name")).to_have_text(exact_text(order["Plan"]))
    expect(details.locator("#product_basePriceInfo")).to_have_text(
        re.compile(r"^\s*\$" + re.escape(f"{expected_price:.2f}") + r"\s*$")
    )

    add_checkout = page.locator("#addToOrderAndCheckout:visible")
    expect(add_checkout).to_have_count(1)
    expect(add_checkout).to_be_enabled()
    add_checkout.click()

    # Verify the screenshot's checkout heading and selected product.
    checkout_heading = page.get_by_text(
        exact_text(order["Provider"] + " Checkout")
    ).filter(visible=True)
    expect(checkout_heading).to_have_count(1, timeout=30_000)
    expect(page.locator("body")).to_contain_text(
        re.compile(re.escape(order["Plan"]), re.I)
    )
    print(f"PASS: Opened checkout for {order['Plan']} (${expected_price:.2f} base).")


def product_section(page, heading):
    """Find the smallest VIDEO/PHONE section containing its own radios."""
    header = page.get_by_text(heading, exact=True).filter(visible=True)
    expect(header).to_have_count(1)
    # A section may use divs or table rows; don't assume unprovided HTML IDs.
    section = header.locator('xpath=ancestor::*[.//input[@type="radio"]][1]')
    expect(section).to_have_count(1)
    for other in {"VIDEO", "INTERNET", "PHONE"} - {heading}:
        if section.get_by_text(other, exact=True).filter(visible=True).count():
            raise ValueError(f"Cannot isolate {heading} options from other product sections.")
    return section


def customize_products(page, order):
    checkout = page.frame_locator("#checkoutContent")

    for heading, key in (("VIDEO", "Video"), ("PHONE", "Phone")):
        section = product_section(checkout, heading)
        requested = order.get(key) or "None"

        option = named_choice(section, "radio", requested)
        option.check()
        expect(option).to_be_checked()

        print(f"{key}: {requested}")

    checkout.get_by_role(
        "button", name="Forward >", exact=True
    ).click()


def main():
    parsed = urlparse(URL)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SystemExit("Paste your simulator HTTPS URL into URL at the top of this file.")
    choice = input("Choose test customer 1 or 2: ").strip()
    if choice not in ORDERS:
        raise SystemExit("Choose 1 or 2.")
    order = ORDERS[choice]
    if order["Customer Type"] not in ("New", "Transfer"):
        raise SystemExit("Customer Type must be New or Transfer.")
    if order["Customer Type"] == "Transfer":
        if len(order["Current Providers"]) != 1 or set(order["Current Providers"]) - set(CURRENT_PROVIDER_CHOICES):
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
        browser = playwright.chromium.launch(headless=False, slow_mo=150)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.set_default_timeout(30_000)
        stage = "opening simulator"
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
            print("Sign in inside Chromium. Automation will continue after login.")
            stage = "waiting for login / session screen"
            zip_search = page.locator("#ssZipCode:visible")
            start = page.get_by_text(re.compile(r"^start session$", re.I)).filter(visible=True)
            forward = forward_button(page)
            start.or_(zip_search).or_(forward).first.wait_for(timeout=180_000)
            if start.is_visible():
                start.click()
                forward.or_(zip_search).first.wait_for(timeout=30_000)
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
            input("Review the next screen. Press Enter to close Chromium: ")
        except (Error, AssertionError, ValueError) as error:
            print(f"STOPPED during {stage}.")
            print(str(error).replace(URL, "[SIMULATOR URL]"))
            print("Run incomplete. Inspect the open browser before closing it.")
            input("Press Enter to close Chromium: ")
            raise SystemExit(1) from None
        finally:
            # A disconnected driver must not replace the original form error.
            with suppress(Exception):
                browser.close()


if __name__ == "__main__":
    main()
