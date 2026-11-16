"""New tracked simulator run with value-free payment inspection before entry.

Pause before payment actions so selectors can be reviewed. Reload only new.py
after the pause; the run's immutable order snapshot stays in the runner.
"""
import importlib
import sys
import re
import argparse
from pathlib import Path

# Support both `python -m tools.verify_payment_live` and direct script launch,
# including launch from outside the repository. Do not change the working dir.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect

import new
import run_from_api
from tools.inspect_payment_fields import inspect_payment_fields, PaymentDiagnostics


def inspect_then_pay(page, order):
    checkout = page.frame_locator("#checkoutContent")
    diagnostics = PaymentDiagnostics()
    inspect_payment_fields(page)
    while True:
        command = input("Inspection command (inspect/read/add/edit/pay/abort): ").strip()
        if command == "abort":
            raise KeyboardInterrupt
        if command == "pay":
            # Payment/Forward is attempted only once; failures exit to the
            # tracked runner and cannot be replayed in this run.
            importlib.reload(new)
            return new.complete_sample_payment(page, order)
        if command not in {"inspect", "read", "add", "edit"}:
            continue
        try:
            if command == "read":
                disclosure = checkout.get_by_text(re.compile("To confirm the card has sufficient funds", re.I)).filter(visible=True)
                diagnostics.perform("Finding hold disclosure", "text=To confirm the card has sufficient funds", disclosure,
                                    lambda: expect(disclosure).to_have_count(1))
                box = disclosure.locator('xpath=ancestor-or-self::*[.//input[@type="checkbox"]][1]').locator('input[type="checkbox"]:visible')
                diagnostics.perform("Finding hold Read checkbox", "hold container >> input[type=checkbox]:visible", box,
                                    lambda: expect(box).to_have_count(1))
                diagnostics.perform("Acknowledging hold disclosure", "hold container >> input[type=checkbox]:visible", box,
                                    lambda: box.check(timeout=5000))
                diagnostics.perform("Verifying hold acknowledgement", "hold container >> input[type=checkbox]:visible", box,
                                    lambda: expect(box).to_be_checked())
            elif command == "add":
                add = checkout.get_by_role("radio", name="Add a new credit card", exact=True)
                diagnostics.perform("Finding Add a new credit card", "role=radio[name='Add a new credit card']", add,
                                    lambda: expect(add).to_have_count(1))
                diagnostics.perform("Selecting Add a new credit card", "role=radio[name='Add a new credit card']", add,
                                    lambda: add.check(timeout=5000))
                diagnostics.perform("Verifying Add a new credit card", "role=radio[name='Add a new credit card']", add,
                                    lambda: expect(add).to_be_checked())
            elif command == "edit":
                edit = checkout.get_by_role("button", name="Edit Secure Fields", exact=True)
                diagnostics.perform("Finding Edit Secure Fields", "role=button[name='Edit Secure Fields']", edit,
                                    lambda: expect(edit).to_have_count(1))
                diagnostics.perform("Clicking Edit Secure Fields", "role=button[name='Edit Secure Fields']", edit,
                                    lambda: edit.click(timeout=5000))
                card_label = checkout.get_by_text(re.compile(r'^\s*Card Number\s*:\s*$', re.I))
                diagnostics.perform("Waiting for card controls", "text=/^Card Number:$/", card_label,
                                    lambda: expect(card_label).to_be_visible(timeout=5000))
                controls = card_label.locator('xpath=ancestor::*[.//input][1]').locator('input')
                diagnostics.perform("Locating Card Number", "Card Number nearest input container >> input", controls,
                                    lambda: None)
            print(f"INSPECTION: {command} completed", flush=True)
        except new.SensitiveDataError:
            # perform() retained diagnostics and emitted the exact operation.
            # No payment/Forward exception is caught by this inspection loop.
            pass
        from tools import inspect_payment_fields as inspection
        importlib.reload(inspection).inspect_payment_fields(page)


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    sys.argv = ["new.py", "--sample-payment", "--fast"]
    new.complete_sample_payment = inspect_then_pay
    run_from_api.main()
