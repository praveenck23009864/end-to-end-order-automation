"""Local synthetic Chromium test. No real website or customer information."""
import os
import pytest

from playwright.sync_api import sync_playwright

import new
import database as db


def test_customer5_sqlite_billing_data_reaches_payment_playwright_function(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "billing-test.db")
    db.init_db()
    customer = {
        "Customer ID": "5",
        "EZ Enroll": "Yes",
        "Credit Card Billing Address Same as Service": "No",
        "Billing Address": "100 Example Test Road",
        "Billing City": "Austin",
        "Billing State": "Texas",
        "Billing ZIP": "78701",
    }
    db.import_orders({"5": customer})
    selected_order = db.get_order("5")["data"]
    received = {}

    class PageStub:
        def frame_locator(self, selector):
            assert selector == "#checkoutContent"
            return object()

    page = PageStub()
    monkeypatch.setattr(
        new, "_complete_payment_fields",
        lambda _page, order, _sensitive: received.update(order),
    )
    monkeypatch.setattr(
        new, "_complete_billing_address",
        lambda _page, _checkout, order: received.update(order),
    )
    monkeypatch.setattr(new, "_navigate_payment_forward", lambda _page: None)

    new.verify_ez_enroll_payment(page, selected_order, {})

    assert received["Credit Card Billing Address Same as Service"] == "No"
    assert received["Billing Address"] == "100 Example Test Road"
    assert received["Billing City"] == "Austin"
    assert received["Billing State"] == "Texas"
    assert received["Billing ZIP"] == "78701"


def test_playwright_locator_failure_is_not_sensitive_data_error(monkeypatch):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content("<div>synthetic fixture</div>")
            monkeypatch.setattr(new, "TIMEOUT_MS", 25)
            with pytest.raises(new.BrowserWorkflowError, match="field=Card Number") as error:
                new.fill_sensitive_field(page.locator("#missing-card"), "synthetic", "Card Number")
            assert not isinstance(error.value, new.SensitiveDataError)
            assert "locator_found=0" in str(error.value)
            assert "exception=AssertionError" in str(error.value)
            assert "synthetic" not in str(error.value)
        finally:
            browser.close()


@pytest.mark.parametrize(("button_markup", "message"), [
    ("", "expected exactly one visible button; found=0"),
    ('<button disabled>Forward &gt;</button>', "Forward button is disabled"),
])
def test_payment_forward_requires_one_visible_enabled_button(button_markup, message):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('<iframe id="checkoutContent"></iframe>')
            page.frames[1].set_content(button_markup)
            with pytest.raises(new.BrowserWorkflowError, match=message):
                new._navigate_payment_forward(page)
            assert page.frames[1].evaluate(
                "window.forwardClicks || 0"
            ) == 0
        finally:
            browser.close()


def test_payment_forward_uses_page_button_when_outside_checkout_iframe():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('''
                <iframe id="checkoutContent"></iframe>
                <button onclick="window.clicks++; document.querySelector('#authorization').hidden=false">
                    Forward &gt;
                </button>
                <h2 id="authorization" hidden>Authorization:</h2>
                <script>window.clicks=0;</script>
            ''')
            new._navigate_payment_forward(page)
            assert page.locator("#authorization").is_visible()
            assert page.evaluate("window.clicks") == 1
        finally:
            browser.close()


def test_payment_forward_finds_nested_child_frame_button():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('<iframe id="checkoutContent"></iframe>')
            page.frames[1].set_content('<iframe id="child"></iframe>')
            child = page.frames[2]
            child.set_content('''
                <button onclick="window.clicks++; document.querySelector('#authorization').hidden=false">
                    Forward &gt;
                </button>
                <h2 id="authorization" hidden>Authorization:</h2>
                <script>window.clicks=0;</script>
            ''')
            new._navigate_payment_forward(page)
            assert child.locator("#authorization").is_visible()
            assert child.evaluate("window.clicks") == 1
        finally:
            browser.close()


def test_blur_precedes_input_and_dates_keep_native_format(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("PWDEBUG", raising=False)
    with sync_playwright() as playwright:
        executable = os.environ.get("TEST_CHROMIUM_EXECUTABLE")
        browser = playwright.chromium.launch(
            headless=True, executable_path=executable
        )
        try:
            page = browser.new_page()
            page.set_content('''
                <input id="ssn" type="text">
                <input id="dob" type="date">
                <input id="dob-text" type="text">
                <button>Continue</button>
                <script>
                    window.blurredOnInput = [];
                    document.querySelectorAll('input').forEach(input => {
                        input.addEventListener('input', () => {
                            window.blurredOnInput.push(
                                getComputedStyle(input).filter === 'blur(7px)'
                            );
                        });
                    });
                </script>
            ''')
            new.fill_sensitive_field(page.locator("#ssn"), "000-00-0000", "SSN")
            new.fill_sensitive_field(page.locator("#dob"), "1992-03-12", "DOB")
            new.fill_sensitive_field(page.locator("#dob-text"), "03/12/1992", "DOB")
            assert page.locator("#dob").input_value() == "1992-03-12"
            assert page.locator("#dob-text").input_value() == "03/12/1992"
            observations = page.evaluate("window.blurredOnInput")
            assert observations and all(observations)
        finally:
            browser.close()


@pytest.mark.parametrize(("layout", "disclosure_root"), [
    ("table", "checkout"),
    ("live_grid", "checkout"),
    ("table", "page"),
])
@pytest.mark.parametrize("billing_answer,missing_billing_data", [("Yes", False), ("No", False), ("No", True)])
@pytest.mark.parametrize("ez_enroll", ["Yes", "No"])
def test_sample_payment_billing_navigation(monkeypatch, layout, disclosure_root, billing_answer, missing_billing_data, ez_enroll):
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("PWDEBUG", raising=False)
    with sync_playwright() as playwright:
        executable = os.environ.get("TEST_CHROMIUM_EXECUTABLE")
        browser = playwright.chromium.launch(
            headless=True, executable_path=executable
        )
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.fulfill(body='<iframe id="checkoutContent"></iframe>'))
            page.goto("https://lemon-forest-0915c3c1e.6.azurestaticapps.net/allconnect/checkout/")
            frame = page.frames[1]
            frame.set_content('''
                <div><b>Other installation disclosure</b><input id="other-read" type="checkbox">Read</div>
                <div>
                    <b>MANDATORY To confirm the card has sufficient funds before your order is placed</b>
                    <input id="hold-read" type="checkbox"
                           onchange="window.sequence.push('read');window.holdAcknowledged=this.checked;alert('Read the MANDATORY statement to the customer and tick Read.')">Read
                </div>
                <section id="payment">
                    <h2>PAYMENT INFORMATION</h2>
                    <label><input type="radio" name="add"
                        onchange="window.sequence.push('add')">
                        Add a new credit card</label>
                    <button onclick="document.querySelector('#number').readOnly=false;
                                     document.querySelector('#type').disabled=false;
                                     window.sequence.push('edit');">
                        Edit Secure Fields
                    </button>
                    <table>
                        <tr><td>Card Type:</td><td><select id="type" disabled><option>Visa</option></select></td></tr>
                        <tr><td>Card Number:</td><td><input id="number" readonly></td></tr>
                        <tr><td>Card Holder Name:</td><td><input id="holder"></td></tr>
                        <tr><td>CVV Code:</td><td><input id="cvv"></td></tr>
                        <tr><td>Expiration Date:</td><td><input id="month"><input id="year"></td></tr>
                    </table>
                    <div>Is the credit card billing address the same as service address?
                        <input type="radio" name="billing" id="yes" onchange="document.querySelector('#billing-fields').hidden=true">Yes
                        <input type="radio" name="billing" id="no" onchange="document.querySelector('#billing-fields').hidden=false">No
                    </div>
                    <table id="billing-fields" hidden>
                        <tr><td>Billing Address:</td><td><input placeholder="Address" id="bill-address"></td></tr>
                        <tr><td>City:</td><td><input placeholder="City" id="bill-city"></td></tr>
                        <tr><td>State:</td><td><select id="bill-state"><option>TX</option></select></td></tr>
                        <tr><td>ZIP Code:</td><td><input placeholder="Zip" id="bill-zip"></td></tr>
                    </table>
                </section>
                <button onclick="window.clicks += 1;window.sequence.push('forward');
                                 window.validAtForward = window.holdAcknowledged
                                     && document.querySelector('input[name=add]').checked
                                     && document.querySelector('#number').value.length > 0
                                     && document.querySelector('#holder').value.length > 0
                                     && document.querySelector('#cvv').value.length > 0
                                         && (document.querySelector('#yes').checked
                                             || (document.querySelector('#no').checked
                                                 && document.querySelector('#bill-address').value.length > 0
                                                 && document.querySelector('#bill-city').value.length > 0
                                                 && document.querySelector('#bill-state').value.length > 0
                                                 && document.querySelector('#bill-zip').value.length > 0));
                                 document.querySelector('#payment').style.display='none';
                                 window.top.document.title='Order Summary';
                                 document.querySelector('#authorization').hidden=false;">
                    Forward &gt;
                </button>
                <h2 id="authorization" hidden>Authorization:</h2>
                <script>
                    window.clicks = 0;
                    window.sequence = [];
                    window.holdAcknowledged = false;
                    window.validAtForward = false;
                    window.blurredOnInput = [];
                    document.querySelectorAll('input').forEach(input => {
                        input.addEventListener('input', () => {
                            if (input.type === 'text' && !input.id.startsWith('bill-')) window.blurredOnInput.push(
                                getComputedStyle(input).filter === 'blur(7px)' &&
                                window.holdAcknowledged
                            );
                        });
                    });
                </script>
            ''')
            if layout == "live_grid":
                frame.evaluate('''() => {
                    const table = document.querySelector('table');
                    const grid = document.createElement('div');
                    for (const row of table.rows) {
                        const label = document.createElement('span');
                        label.textContent = row.cells[0].textContent;
                        grid.append(label);
                        if (label.textContent.startsWith('Expiration')) {
                            const expiry = document.createElement('div');
                            expiry.append(...row.cells[1].childNodes);
                            grid.append(expiry);
                        } else { grid.append(...row.cells[1].childNodes); }
                    }
                    table.replaceWith(grid);
                    grid.id = 'grid'; grid.hidden = true;
                    grid.querySelectorAll('input,select').forEach(el => el.disabled = true);
                    document.querySelector('#hold-read').onchange = () => {
                        window.sequence.push('read');
                        window.holdAcknowledged = document.querySelector('#hold-read').checked;
                        grid.hidden = false;
                    };
                    document.querySelector('#payment button').onclick = () => {
                        window.editPrerequisites = document.querySelector('#hold-read').checked
                            && document.querySelector('input[name=add]').checked;
                        window.sequence.push('edit');
                        grid.querySelectorAll('input,select').forEach(el => {
                            el.disabled = false; el.readOnly = false;
                        });
                    };
                }''')
            else:
                # EZ Enroll No may expose payment controls without rendering
                # the optional PAYMENT INFORMATION heading.
                frame.locator("#payment h2").evaluate("el => el.remove()")
            if disclosure_root == "page":
                frame.evaluate('''() => {
                    const wrapper = document.querySelector('#hold-read').parentElement;
                    wrapper.querySelector('#hold-read').removeAttribute('onchange');
                    const topDocument = window.top.document;
                    topDocument.body.appendChild(topDocument.adoptNode(wrapper));
                    const topCheckbox = topDocument.querySelector('#hold-read');
                    topCheckbox.onchange = () => {
                        const child = topDocument.querySelector('#checkoutContent').contentWindow;
                        child.sequence.push('read');
                        child.holdAcknowledged = topCheckbox.checked;
                        alert('Read the MANDATORY statement to the customer and tick Read.');
                        const grid = child.document.querySelector('#grid');
                        if (grid) grid.hidden = false;
                    };
                }''')
            sensitive_values = {
                "ssn": "999999999", "dob": "1990-01-02", "card_type": "Visa",
                "card_number": "4242424242424242", "cardholder_name": "SYNTHETIC TEST",
                "cvv": "999", "expiration_month": "12", "expiration_year": "2030",
            }
            order = {
                "Credit Card Billing Address Same as Service": billing_answer,
                "EZ Enroll": ez_enroll,
            }
            if not missing_billing_data:
                order.update({
                    "Billing Address": "2 Synthetic Street",
                    "Billing City": "Testville",
                    "Billing State": "Texas",
                    "Billing ZIP": "00000",
                })
            if billing_answer == "No" and missing_billing_data:
                with pytest.raises(new.BrowserWorkflowError, match="required customer data is missing"):
                    new.complete_sample_payment(page, order, sensitive_values)
                assert frame.locator("#no").is_checked()
                assert frame.evaluate("window.clicks") == 0
                assert page.title() != "Order Summary"
                return
            new.complete_sample_payment(page, order, sensitive_values)
            expected_clicks = 1
            assert frame.evaluate("window.clicks") == expected_clicks
            assert frame.evaluate("window.validAtForward") is True
            assert frame.evaluate("window.sequence") == ["read", "add", "edit", "forward"]
            assert frame.locator("#number").input_value() == "4242424242424242"
            assert frame.locator("#holder").input_value() == "SYNTHETIC TEST"
            assert frame.locator("#yes").is_checked() is (billing_answer == "Yes")
            assert frame.locator("#no").is_checked() is (billing_answer == "No")
            if billing_answer == "No":
                assert frame.locator("#bill-address").input_value() == "2 Synthetic Street"
                assert frame.locator("#bill-city").input_value() == "Testville"
                assert frame.locator("#bill-state").input_value() == "TX"
                assert frame.locator("#bill-zip").input_value() == "00000"
            else:
                assert frame.locator("#billing-fields").is_hidden()
            hold_checkbox = (page if disclosure_root == "page" else frame).locator("#hold-read")
            assert hold_checkbox.is_checked()
            assert not frame.locator("#other-read").is_checked()
            assert frame.locator('#authorization').is_visible()
            assert page.title() == 'Order Summary'
            assert sensitive_values["card_number"] == ""
            if layout == "live_grid":
                assert frame.evaluate('window.editPrerequisites') is True
            observations = frame.evaluate("window.blurredOnInput")
            assert len(observations) == 5 and all(observations)
        finally:
            browser.close()


@pytest.mark.parametrize("ez", ["Yes", "No"])
def test_ez_enroll_installation_and_payment_stop_before_final_forward(monkeypatch, ez):
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("PWDEBUG", raising=False)
    with sync_playwright() as playwright:
        executable = os.environ.get("TEST_CHROMIUM_EXECUTABLE")
        browser = playwright.chromium.launch(
            headless=True, executable_path=executable
        )
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.fulfill(body='<iframe id="checkoutContent"></iframe>'))
            page.goto("https://lemon-forest-0915c3c1e.6.azurestaticapps.net/allconnect/checkout/")
            frame = page.frames[1]
            credit_section = '''
                <section><h2>CREDIT CHECK</h2><input><input type="date">
                    <button onclick="window.creditClicks++;
                        document.querySelector('#credit-status').hidden=false">
                        Run Credit Check</button>
                    <div id="credit-status" hidden><h2>ORDER QUALIFICATION STATUS</h2>
                        Processing Status: SUCCESS LineItem Status: SUCCESS</div>
                </section>
            ''' if ez == "No" else ""
            frame.set_content('''
                __CREDIT_SECTION__
                <div id="checkout-scroll" style="height:160px;overflow:auto">
                    <section><h2>Select Installation Type</h2>
                        <label><input type="radio" name="install">STANDARD_SHIPPING</label>
                        <label><input type="radio" name="install">PRO_INSTALL</label>
                    </section>
                    <div style="height:260px"></div>
                    <p id="confirmed" role="status" hidden>Installation confirmed</p>
                    <button id="confirm" onclick="window.sequence.push('confirm');
                        document.querySelector('#confirmed').hidden=false;
                        document.querySelector('#payment').hidden=!window.ezYes;
                        if (!window.ezYes) {
                            const next=document.createElement('button');
                            next.id='installation-forward'; next.textContent='Forward >';
                            next.onclick=()=>{ window.installForwards++;
                                window.sequence.push('installation-forward');
                                next.hidden=true;
                                document.querySelector('#payment').hidden=false; };
                            document.querySelector('#install-actions').appendChild(next);
                        }
                        this.disabled=true;">Confirm Installation &gt;</button>
                    <div id="install-actions"></div>
                    <section id="payment" hidden>
                        <h2>PAYMENT INFORMATION</h2>
                        <div><p>MANDATORY To confirm the card has sufficient funds before your order is placed.</p>
                            <label><input id="hold" type="checkbox" onchange="
                                window.sequence.push('read');
                                window.readScroll=document.querySelector('#checkout-scroll').scrollTop;
                                alert('Read the MANDATORY statement to the customer and tick Read.');
                            ">Read</label>
                        </div>
                        <label><input id="add" type="radio" name="method" onchange="window.sequence.push('add')">
                            Add a new credit card</label>
                        <button id="edit" onclick="window.sequence.push('edit');
                            document.querySelector('#type').disabled=false;
                            document.querySelector('#number').readOnly=false;">Edit Secure Fields</button>
                        <table>
                            <tr><td>Card Type:</td><td><select id="type" disabled><option>Visa</option></select></td></tr>
                            <tr><td>Card Number:</td><td><input id="number" readonly></td></tr>
                            <tr><td>Card Holder Name:</td><td><input id="holder"></td></tr>
                            <tr><td>CVV Code:</td><td><input id="cvv"></td></tr>
                            <tr><td>Expiration Date:</td><td><input id="month"><input id="year"></td></tr>
                        </table>
                        <div>Is the credit card billing address the same as service address?
                            <label><input type="radio" name="billing" id="billing-yes">Yes</label>
                            <label><input type="radio" name="billing" id="billing-no">No</label>
                        </div>
                        <table id="billing-fields" hidden>
                            <tr><td>Address:</td><td><input placeholder="Address" id="bill-address"></td></tr>
                            <tr><td>City:</td><td><input placeholder="City" id="bill-city"></td></tr>
                            <tr><td>State:</td><td><select id="bill-state"><option>TX</option></select></td></tr>
                            <tr><td>ZIP:</td><td><input placeholder="Zip" id="bill-zip"></td></tr>
                        </table>
                    <button id="forward" onclick="window.forwards++;
                        window.sequence.push('forward');
                        window.validAtForward = document.querySelector('#hold').checked
                            && document.querySelector('#add').checked
                            && document.querySelector('#billing-no').checked
                            && document.querySelector('#number').value.length > 0
                            && document.querySelector('#holder').value.length > 0
                            && document.querySelector('#cvv').value.length > 0;
                        document.querySelector('#payment').hidden=true;
                        window.top.document.title='Order Summary';
                        document.querySelector('#summary').hidden=false;
                        document.querySelector('#authorization').hidden=false;">Forward &gt;</button>
                    </section>
                    <h2 id="summary" hidden>Order Summary</h2>
                    <h2 id="authorization" hidden>Authorization:</h2>
                </div>
                <script>window.sequence=[];window.forwards=0;window.installForwards=0;
                    window.creditClicks=0;window.finalSubmits=0;
                    window.ezYes=__EZ_YES__;</script>
            '''.replace("__CREDIT_SECTION__", credit_section).replace(
                "__EZ_YES__", "true" if ez == "Yes" else "false"
            ))
            initial_url = page.url
            order = {
                "EZ Enroll": ez, "EZ Enroll Disclosure Read": True,
                "Installation Type": "STANDARD_SHIPPING",
                "Mailing Same As Service": "Yes",
                "Credit Card Billing Address Same as Service": "Yes",
            }
            sensitive_values = {
                "ssn": "999999999", "dob": "1990-01-02", "card_type": "Visa",
                "card_number": "4242424242424242", "cardholder_name": "SYNTHETIC TEST",
                "cvv": "999", "expiration_month": "12", "expiration_year": "2030",
            }
            new.complete_installation(page, order, advance=(ez == "No"), sensitive=sensitive_values)
            assert page.url == initial_url
            assert frame.locator("#confirmed").is_visible()
            assert frame.locator("#forward").count() == 1
            assert frame.evaluate("window.forwards") == 0
            assert frame.evaluate("window.installForwards") == int(ez == "No")
            assert frame.evaluate("window.creditClicks") == int(ez == "No")
            assert frame.locator("#payment").is_visible()
            assert frame.evaluate("window.sequence") == (
                ["confirm", "installation-forward"] if ez == "No" else ["confirm"]
            )

            if ez == "Yes":
                new.verify_ez_enroll_payment(page, order, sensitive_values)
            else:
                new.complete_sample_payment(page, order, sensitive_values)
            assert frame.evaluate("window.sequence") == (
                (["confirm", "installation-forward"] if ez == "No" else ["confirm"])
                + ["read", "add", "edit"]
                + ["forward"]
            )
            assert frame.evaluate("window.readScroll") > 0
            assert frame.locator("#hold").is_checked()
            assert frame.locator("#add").is_checked()
            assert frame.locator("#billing-yes").is_checked()
            assert frame.evaluate("window.forwards") == 1
            assert frame.evaluate("window.finalSubmits") == 0
            assert frame.locator("#summary").is_visible()
            assert page.title() == "Order Summary"
        finally:
            browser.close()


import pytest


def test_payment_diagnostics_precede_failure_and_inspect_nested_frames(capsys):
    from tools.inspect_payment_fields import PaymentDiagnostics, inspect_payment_fields
    with sync_playwright() as playwright:
        executable = os.environ.get("TEST_CHROMIUM_EXECUTABLE")
        browser = playwright.chromium.launch(
            headless=True, executable_path=executable
        )
        try:
            page = browser.new_page()
            page.set_content('<iframe id="checkoutContent"></iframe>')
            frame = page.frames[1]
            frame.set_content('''<input id="locked" disabled value="PRIVATE_VALUE">
                <input id="hidden" type="hidden" value="PRIVATE_HIDDEN">
                <iframe id="secure"></iframe><p>PRIVATE_PAGE_TEXT</p>''')
            child = frame.child_frames[0]
            child.set_content('<input id="card" name="card" placeholder="Card Number" value="PRIVATE_NESTED">')
            inspect_payment_fields(page)
            diagnostics = PaymentDiagnostics()

            def blocked():
                output = capsys.readouterr().out
                assert 'Selecting Add a new credit card' in output
                assert '"match_count": 1' in output
                assert '"enabled": false' in output
                assert 'iframe#checkoutContent >> iframe[0]' in output
                assert '"id": "card"' in output
                assert 'PRIVATE_' not in output
                raise TimeoutError('PRIVATE_EXCEPTION')

            with pytest.raises(new.SensitiveDataError, match='Selecting Add a new credit card') as error:
                diagnostics.perform('Selecting Add a new credit card', '#locked', frame.locator('#locked'), blocked)
            assert diagnostics.events[0]['match_count'] == 1
            assert len(diagnostics.events) == 2
            assert 'PRIVATE_' not in str(error.value)
            output = capsys.readouterr().out
            assert 'inspect_payment_fields.py:' in output
            assert 'PRIVATE_' not in output
        finally:
            browser.close()


@pytest.mark.parametrize("ez", ["Yes", "No"])
@pytest.mark.parametrize("installation", ["STANDARD_SHIPPING", "PRO_INSTALL"])
def test_installation_credit_branches(ez, installation):
    with sync_playwright() as playwright:
        executable = os.environ.get("TEST_CHROMIUM_EXECUTABLE")
        browser = playwright.chromium.launch(
            headless=True, executable_path=executable
        )
        try:
            page = browser.new_page()
            page.set_content('<iframe id="checkoutContent"></iframe>')
            frame = page.frames[1]
            credit = "" if ez == "Yes" else '''<section><h2>CREDIT CHECK</h2>
                <input><input type="date"></section>
                <button onclick="window.creditClicks++;document.querySelector('#status').hidden=false">Run Credit Check</button>
                <section id="status" hidden><h2>ORDER QUALIFICATION STATUS</h2>
                Processing Status: SUCCESS LineItem Status: SUCCESS</section>'''
            frame.set_content(credit + '''
                <section><h2>Select Installation Type</h2>
                <input type="radio" name="install"><input type="radio" name="install"></section>
                <section>Someone over the age of 18 must be available<input type="checkbox"></section>
                <input type="date" id="installDate">
                <select><option>Morning</option><option>Afternoon</option><option>Evening</option></select>
                <section>Availability of installation dates and times<input type="checkbox"></section>
                <div id="installWarning" role="alert" hidden>Read the MANDATORY statement to the customer and tick Read.</div>
                <p id="installConfirmed" role="status" hidden>Installation confirmed</p>
                <button id="confirmInstall" onclick="window.confirms++;
                    window.acksAtConfirm = !window.needsPro ||
                        [...document.querySelectorAll('section input[type=checkbox]')].every(x => x.checked);
                    if (window.rejectInstall || !window.acksAtConfirm) {
                        document.querySelector('#installWarning').hidden=false;
                    } else {
                        window.installationConfirmed=true;
                        this.disabled=true;
                        document.querySelector('#installConfirmed').hidden=false;
                        document.querySelector('#payment').hidden=!window.ezYes;
                        const forward=document.createElement('button');
                        forward.id='forwardInstall';
                        forward.textContent='Forward >';
                        forward.onclick=()=>{
                            window.forwards++;
                            window.forwardAfterConfirm=window.installationConfirmed;
                            if (window.installationConfirmed)
                                document.querySelector('#payment').hidden=false;
                        };
                        document.querySelector('#installActions').appendChild(forward);
                    }">Confirm Installation &gt;</button>
                <div id="installActions"></div>
                <h2 id="payment" hidden>PAYMENT INFORMATION</h2>
                <script>window.creditClicks=0;window.confirms=0;window.forwards=0;
                    window.installationConfirmed=false;window.needsPro=false;window.rejectInstall=false;
                    window.finalSubmitted=0;window.forwardAfterConfirm=false;window.ezYes=false;</script>
                ''')
            frame.evaluate('(required) => window.needsPro = required', installation == "PRO_INSTALL")
            frame.evaluate('(isEzYes) => window.ezYes = isEzYes', ez == "Yes")
            # Only the relevant native date input is visible during credit entry.
            frame.locator('#installDate').evaluate("el => el.hidden = true")
            frame.get_by_role('radio').nth(1).evaluate("el => el.onclick = () => { document.querySelectorAll('section input[type=date]').forEach(x => x.hidden=true); document.querySelector('#installDate').hidden=false; }")
            assert frame.get_by_role('button', name='Forward >', exact=True).count() == 0
            install_date = "2026-11-21" if ez == "No" and installation == "PRO_INSTALL" else "2027-01-15"
            install_time = "Evening" if ez == "No" and installation == "PRO_INSTALL" else "Afternoon"
            initial_url = page.url
            order = {"EZ Enroll": ez,
                "EZ Enroll Disclosure Read": True, "Installation Type": installation,
                "Install Date": install_date, "Install Time": install_time,
                "Automatic Payments": "Yes",
                "Automatic Payments Disclosure Read": True}
            sensitive_values = {
                "ssn": "999999999", "dob": "1990-01-02", "card_type": "",
                "card_number": "", "cardholder_name": "", "cvv": "",
                "expiration_month": "", "expiration_year": "",
            }
            advance = ez == "No"
            new.complete_installation_and_payment(
                page, order, sensitive_values, payment_required=False,
            )
            assert page.url == initial_url
            assert frame.evaluate('window.creditClicks') == (0 if ez == "Yes" else 1)
            assert frame.evaluate('window.confirms') == 1
            assert frame.evaluate('window.forwards') == int(advance)
            assert frame.evaluate('window.installationConfirmed') is True
            assert frame.evaluate('window.forwardAfterConfirm') is advance
            assert frame.evaluate('window.acksAtConfirm') is True
            assert frame.locator('#installConfirmed').is_visible()
            assert frame.locator('#payment').is_visible() is (
                ez == "Yes" or advance
            )
            assert frame.evaluate('window.finalSubmitted') == 0
            assert frame.evaluate('window.creditClicks') == (0 if ez == "Yes" else 1)
            if installation == "PRO_INSTALL":
                assert frame.locator('#installDate').input_value() == install_date
                assert frame.locator('select').input_value() == install_time
        finally:
            browser.close()


def test_pro_install_confirmation_validation_stops_before_forward(monkeypatch):
    monkeypatch.setattr(new, "TIMEOUT_MS", 500)
    with sync_playwright() as playwright:
        executable = os.environ.get("TEST_CHROMIUM_EXECUTABLE")
        browser = playwright.chromium.launch(
            headless=True, executable_path=executable
        )
        try:
            page = browser.new_page()
            page.set_content('<iframe id="checkoutContent"></iframe>')
            frame = page.frames[1]
            frame.set_content('''
                <section><h2>Select Installation Type</h2>
                    <input type="radio" name="install"><input type="radio" name="install">
                </section>
                <section><p>Someone over the age of 18 must be available</p>
                    <input id="adult-read" type="checkbox"></section>
                <input type="date" id="install-date">
                <select id="install-time"><option>Morning</option><option>Afternoon</option></select>
                <section><p>Availability of installation dates and times</p>
                    <input id="schedule-read" type="checkbox"></section>
                <div id="warning" role="alert" hidden>
                    Read the MANDATORY statement to the customer and tick Read.
                </div>
                <button id="confirm" onclick="window.confirms++;
                    document.querySelector('#warning').hidden=false;">
                    Confirm Installation &gt;</button>
                <h2 id="payment" hidden>PAYMENT INFORMATION</h2>
                <script>window.confirms=0;window.forwards=0;</script>
            ''')
            order = {
                "EZ Enroll": "Yes", "EZ Enroll Disclosure Read": True,
                "Installation Type": "PRO_INSTALL", "Install Date": "2027-01-15",
                "Install Time": "Morning",
            }
            with pytest.raises(new.SensitiveDataError, match="failed simulator validation"):
                new.complete_installation(page, order, advance=False)
            assert frame.evaluate("window.confirms") == 1
            assert frame.evaluate("window.forwards") == 0
            assert frame.locator("#adult-read").is_checked()
            assert frame.locator("#schedule-read").is_checked()
            assert frame.locator("#payment").is_hidden()
        finally:
            browser.close()


@pytest.mark.parametrize("installation", ["PRO_INSTALL", "STANDARD_SHIPPING"])
@pytest.mark.parametrize("billing", ["Yes", "No"])
@pytest.mark.parametrize("ez_enroll", ["Yes", "No"])
def test_installation_and_payment_orchestrator_is_one_ordered_step(
    monkeypatch, installation, billing, ez_enroll,
):
    sequence = []
    order = {
        "Installation Type": installation,
        "Install Date": "2027-02-12",
        "Install Time": "Afternoon",
        "EZ Enroll": ez_enroll,
        "Credit Card Billing Address Same as Service": billing,
    }
    if billing == "No":
        order.update({
            "Billing Address": "8 Test Street", "Billing City": "Testville",
            "Billing State": "Texas", "Billing ZIP": "12345",
        })

    monkeypatch.setattr(
        new, "complete_installation",
        lambda page, given_order, *, advance, sensitive: sequence.append(
            ("installation", given_order["Installation Type"], advance)
        ),
    )
    monkeypatch.setattr(
        new, "_complete_payment_billing_and_forward",
        lambda page, given_order, sensitive: sequence.append(
            ("payment+billing+forward", given_order["Credit Card Billing Address Same as Service"])
        ),
    )
    sensitive = {
        "card_number": "synthetic", "cvv": "synthetic",
        "expiration_month": "synthetic", "expiration_year": "synthetic",
    }

    new.complete_installation_and_payment(
        object(), order, sensitive, payment_required=True,
    )

    assert sequence == [
        ("installation", installation, ez_enroll == "No"),
        ("payment+billing+forward", billing),
    ]
    assert sensitive["card_number"] == ""


def test_installation_failure_prevents_payment_and_payment_forward(monkeypatch):
    payment_calls = []

    def fail_installation(*args, **kwargs):
        raise new.SensitiveDataError("Installation confirmation failed safely.")

    monkeypatch.setattr(new, "complete_installation", fail_installation)
    monkeypatch.setattr(
        new, "_complete_payment_billing_and_forward",
        lambda *args, **kwargs: payment_calls.append("payment forward"),
    )
    with pytest.raises(new.SensitiveDataError, match="Installation confirmation failed"):
        new.complete_installation_and_payment(
            object(), {"EZ Enroll": "No"}, {}, payment_required=True,
        )
    assert payment_calls == []


def test_authorization_requires_matching_statement_and_confirms_submission(monkeypatch):
    import run_from_api

    monkeypatch.setattr(run_from_api.automation, "validate_order", lambda order: None)
    monkeypatch.setattr(run_from_api.automation, "URL", "about:blank")
    statement = (
        "AVERY BROOKS, do you authorize ProviderXX to process this order for the "
        "products and prices I have reviewed with you today, and agree to the "
        "ProviderXX terms and conditions of service?"
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(f'''<p>{statement}</p>
                <label><input type="checkbox" id="auth-read">Read</label>
                <button id="submit">Submit Order</button>
                <script>window.submits=0;</script>''')
            page.locator("#submit").evaluate(\
                """el => { el.onclick = () => {
                    window.submits++;
                    document.body.insertAdjacentHTML(
                        'beforeend', \"<div role='status'>Thank you for your order</div>\"
                    );
                }; }"""
            )
            run_from_api.complete_authorization(
                page, {"First Name": "AVERY", "Last Name": "BROOKS"},
            )
            assert page.locator("#auth-read").is_checked()
            assert page.evaluate("window.submits") == 1
        finally:
            browser.close()


def test_order_summary_selects_payload_plan_before_place_order(monkeypatch):
    import run_from_api

    monkeypatch.setattr(run_from_api.automation, "validate_order", lambda order: None)
    monkeypatch.setattr(run_from_api.automation, "URL", "about:blank")
    statement = (
        "AVERY BROOKS, do you authorize ProviderXX to process this order for the "
        "products and prices I have reviewed with you today, and agree to the "
        "ProviderXX terms and conditions of service?"
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('''
                <section id="wanted">ProviderXX 500 Mbps (1-YEAR PRICING)
                    <label><input id="wanted-check" type="checkbox"> Select</label>
                </section>
                <section id="other">ProviderXX 300 Mbps (1-YEAR PRICING)
                    <label><input id="other-check" type="checkbox"> Select</label>
                </section>
                <button id="place">Place Order</button>
                <script>
                    window.orderChecks = [];
                    document.querySelector('#place').onclick = () => {
                        window.orderChecks.push([
                            document.querySelector('#wanted-check').checked,
                            document.querySelector('#other-check').checked
                        ]);
                        document.body.innerHTML = `
                            <p>'''+statement+'''</p>
                            <label><input type="checkbox">Read</label>
                            <button id="submit">Submit Order</button>`;
                        document.querySelector('#submit').onclick = () => {
                            document.body.insertAdjacentHTML(
                                'beforeend', '<div role="status">Thank you for your order</div>'
                            );
                        };
                    };
                </script>
            ''')

            run_from_api.complete_authorization(
                page,
                {
                    "First Name": "AVERY",
                    "Last Name": "BROOKS",
                    "Plan": "500 Mbps (1-YEAR PRICING)",
                },
            )

            assert page.evaluate("window.orderChecks") == [[True, False]]
            assert page.get_by_text("Thank you for your order").is_visible()
        finally:
            browser.close()


def test_order_summary_missing_plan_stops_before_place_order(monkeypatch):
    import run_from_api

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('''
                <section>ProviderXX 500 Mbps
                    <label><input type="checkbox"> Select</label>
                </section>
                <button id="place">Place Order</button>
                <script>window.placeClicks = 0;
                    document.querySelector('#place').onclick = () => window.placeClicks++;
                </script>
            ''')
            with pytest.raises(RuntimeError, match="payload field 'Plan'"):
                run_from_api.complete_authorization(
                    page, {"First Name": "AVERY", "Last Name": "BROOKS"}
                )
            assert page.evaluate("window.placeClicks") == 0
        finally:
            browser.close()


def test_authorization_ambiguous_result_does_not_claim_success(monkeypatch):
    import run_from_api

    monkeypatch.setattr(run_from_api.automation, "validate_order", lambda order: None)
    monkeypatch.setattr(run_from_api.automation, "URL", "about:blank")
    monkeypatch.setattr(run_from_api, "TIMEOUT_MS", 500)
    statement = (
        "AVERY BROOKS, do you authorize ProviderXX to process this order for the "
        "products and prices I have reviewed with you today, and agree to the "
        "ProviderXX terms and conditions of service?"
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(f'''<p>{statement}</p><label><input type="checkbox">Read</label>
                <button>Submit Order</button>''')
            with pytest.raises(RuntimeError, match="outcome is ambiguous"):
                run_from_api.complete_authorization(
                    page, {"First Name": "AVERY", "Last Name": "BROOKS"},
                )
        finally:
            browser.close()


@pytest.mark.parametrize("ez,automatic,locked,missing,blocked", [
    ("Yes", "Yes", "Yes", None, False),
    ("Yes", "No", None, None, False),
    ("No", "Yes", None, None, False),
    ("No", "No", None, None, False),
    ("Yes", "Yes", "Yes", "EZ Enroll Disclosure Read", False),
    ("Yes", "Yes", "Yes", "Automatic Payments Disclosure Read", False),
    ("No", "Yes", None, "Automatic Payments Disclosure Read", False),
    ("Yes", "No", "Yes", None, False),
    ("Yes", "Yes", "No", None, False),
    ("Yes", "Yes", "Yes", None, True),
])
def test_account_holder_disclosure_branch_and_single_forward(monkeypatch, ez, automatic, locked, missing, blocked):
    monkeypatch.setattr(new, "human_fill", lambda field, value: field.fill(value))
    monkeypatch.setattr(new, "TIMEOUT_MS", 500)
    with sync_playwright() as playwright:
        executable = os.environ.get("TEST_CHROMIUM_EXECUTABLE")
        browser = playwright.chromium.launch(
            headless=True, executable_path=executable
        )
        try:
            page = browser.new_page()
            page.set_content('<iframe id="checkoutContent"></iframe>')
            frame = page.frames[1]
            def question(text, name):
                return f'<section><p>{text}</p><input type="radio" name="{name}"><input type="radio" name="{name}"></section>'
            html = question('Agent - Verify that you are speaking', 'verify')
            html += '<input placeholder="Phone Number"><input placeholder="Email Address">'
            html += question('are you a current ProviderXX customer?', 'existing')
            html += question('Does your customer want to continue with EZ-Enroll?', 'ez')
            if ez == "Yes":
                html += '<section><p>By signing up for ProviderXX EZ-Enroll no credit check is performed.</p><input id="ez-read" type="checkbox" checked></section>'
            html += question('Does your customer want to continue with Automatic Payments and Paperless Billing Discount?', 'auto')
            if automatic == "Yes" or locked == "Yes":
                html += "<section><p>You have opted into ProviderXX's Automatic Payments</p><input id='auto-read' type='checkbox'></section>"
            html += '''<section><p>Is your mailing address the same as service address?</p>
                <input type="radio" name="mail"><input type="radio" name="mail">
                <input type="radio" name="ship"><input type="radio" name="ship"></section>
                <button onclick="window.forwards++;
                    window.checkedAtForward = [...document.querySelectorAll('input[type=checkbox]')].every(x => x.checked)
                        && ['verify','existing','ez','auto','mail','ship'].every(n => document.querySelector('input[name='+n+']:checked'));
                    if (window.checkedAtForward && !window.blocked) document.querySelector('#next').hidden=false;
                    ">Forward &gt;</button>
                <h2 id="next" hidden>Installation and Payment:</h2>
                <script>window.forwards=0;</script>'''
            frame.set_content(html)
            frame.evaluate('(blocked) => window.blocked=blocked', blocked)
            if locked:
                frame.locator('input[name=auto]').nth(0 if locked == "Yes" else 1).check()
                frame.locator('input[name=auto]').evaluate_all('(els) => els.forEach(el => el.disabled=true)')
            order = {"Account Holder Verified": True,
                "Mailing Same As Service": "Yes", "Shipping Same As Service": "Yes",
                "Phone Number": "2025550141", "Email Address": "demo@example.com",
                "Existing ProviderXX Customer": "No", "EZ Enroll": ez,
                "Automatic Payments": automatic, "EZ Enroll Disclosure Read": True,
                "Automatic Payments Disclosure Read": True}
            if missing:
                del order[missing]
            if missing or (locked and locked != automatic):
                expected = missing or "Automatic Payments: UI locks a different answer"
                with pytest.raises(new.SensitiveDataError, match=expected):
                    new.qualify_account_holder(page, order)
                assert frame.evaluate('window.forwards') == 0
                return
            if blocked:
                with pytest.raises(new.SensitiveDataError, match='navigation blocked after one Forward click; invalid controls=0; visible alerts=0; unchecked required controls=0') as error:
                    new.qualify_account_holder(page, order)
                assert order['Email Address'] not in str(error.value)
                assert order['Phone Number'] not in str(error.value)
            else:
                new.qualify_account_holder(page, order)
                assert frame.locator('#next').is_visible()
            assert frame.evaluate('window.forwards') == 1
            assert frame.evaluate('window.checkedAtForward') is True
            if ez == "Yes":
                assert frame.locator('#ez-read').is_checked()
            if automatic == "Yes":
                assert frame.locator('#auto-read').is_checked()
            for name, index in [('ez', 0 if ez == 'Yes' else 1), ('auto', 0 if automatic == 'Yes' else 1), ('mail', 0), ('ship', 0)]:
                assert frame.locator(f'input[name={name}]').nth(index).is_checked()
        finally:
            browser.close()

@pytest.mark.parametrize("button_text", ["Forward", "Forward >"])
def test_payment_forward_accepts_both_button_labels(button_text):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        try:
            page = browser.new_page()
            page.set_content("""
                <iframe id="checkoutContent"></iframe>
            """)

            frame = page.frames[1]

            frame.set_content("""
                <button id="forward">BUTTON_TEXT</button>

                <script>
                    window.clicks = 0;

                    document.querySelector("#forward").onclick = () => {
                        window.clicks++;

                        document.body.insertAdjacentHTML(
                            "beforeend",
                            "<h2>Authorization:</h2>"
                        );
                    };
                </script>
            """.replace("BUTTON_TEXT", button_text))

            new._navigate_payment_forward(page)

            assert frame.evaluate("window.clicks") == 1

            assert frame.get_by_text(
                "Authorization:"
            ).is_visible()

        finally:
            browser.close()


def test_payment_forward_waits_for_delayed_button():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        try:
            page = browser.new_page()
            page.set_content("""
                <iframe id="checkoutContent"></iframe>
            """)

            frame = page.frames[1]

            frame.set_content("""
                <div id="root"></div>

                <script>
                    window.clicks = 0;

                    setTimeout(() => {
                        const button = document.createElement("button");

                        button.textContent = "Forward";

                        button.onclick = () => {
                            window.clicks++;

                            document.body.insertAdjacentHTML(
                                "beforeend",
                                "<h2>Authorization:</h2>"
                            );
                        };

                        document.querySelector("#root").appendChild(button);
                    }, 500);
                </script>
            """)

            new._navigate_payment_forward(page)

            assert frame.evaluate("window.clicks") == 1

            assert frame.get_by_text(
                "Authorization:"
            ).is_visible()

        finally:
            browser.close()
