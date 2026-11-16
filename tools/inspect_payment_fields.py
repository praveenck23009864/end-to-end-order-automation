"""Value-free Playwright diagnostics. Never read values, URLs or page HTML."""
import json
import re
from pathlib import Path

from sensitive_data import SensitiveDataError


class PaymentDiagnostics:
    def __init__(self):
        self.events = []

    def candidates(self, operation, selector, locator):
        event = {"operation": operation, "selector": selector,
                 "match_count": locator.count()}
        self.events.append(event)
        print(json.dumps(event), flush=True)
        # Enumerating candidates is diagnostic only; never choose one by index.
        for index in range(event["match_count"]):
            item = locator.nth(index)
            metadata = item.evaluate("""el => ({
                type: el.getAttribute('type'), id: el.id || null,
                name: el.getAttribute('name'), placeholder: el.getAttribute('placeholder')
            })""")
            metadata.update(visible=item.is_visible(), enabled=item.is_enabled(),
                            editable=item.evaluate("""el =>
                                (el.matches('input,textarea,select') || el.isContentEditable)
                                && !el.matches(':disabled') && !el.readOnly"""))
            self.events.append(metadata)
            print(json.dumps(metadata), flush=True)
        return event["match_count"]

    def perform(self, operation, selector, locator, action):
        try:
            self.candidates(operation, selector, locator)
            return action()
        except Exception as error:
            current = error.__traceback__
            locations = []
            while current:
                filename = Path(current.tb_frame.f_code.co_filename).name
                if filename in {"inspect_payment_fields.py", "verify_payment_live.py", "new.py"}:
                    locations.append(f"{filename}:{current.tb_lineno}")
                current = current.tb_next
            message = f"Payment blocked during {operation} ({type(error).__name__}); source: {', '.join(locations)}"
            print(message, flush=True)
            raise SensitiveDataError(message) from None


def inspect_payment_fields(page):
    diagnostics = PaymentDiagnostics()
    roots = page.locator("iframe#checkoutContent")
    diagnostics.candidates("Inspect checkout frame", "iframe#checkoutContent", roots)
    if roots.count() != 1:
        raise SensitiveDataError("Payment inspection requires exactly one checkoutContent iframe.")
    root = roots.element_handle().content_frame()
    if root is None:
        raise SensitiveDataError("checkoutContent frame is not attached.")

    def inspect_frame(frame, path):
        diagnostics.candidates("Inspect fields", path + " >> input,select,textarea", frame.locator("input,select,textarea"))
        frames = frame.locator("iframe")
        diagnostics.candidates("Inspect nested frames", path + " >> iframe", frames)
        for index in range(frames.count()):
            child = frames.nth(index).element_handle().content_frame()
            if child:
                inspect_frame(child, path + f" >> iframe[{index}]")
    inspect_frame(root, "iframe#checkoutContent")
    for label in ("Card Type", "Card Number", "Card Holder Name", "CVV Code", "Expiration Date"):
        text = root.get_by_text(re.compile(r'^\s*' + re.escape(label) + r'\s*:\s*$', re.I))
        diagnostics.candidates("Inspect field label", label, text)
        for relation in ('xpath=following-sibling::*[1][self::input or self::select]',
                         'xpath=following-sibling::*[1]//input',
                         'xpath=..'):
            candidate = text.locator(relation)
            if relation == 'xpath=..':
                candidate = candidate.locator('input,select')
            diagnostics.candidates("Inspect field association", label + ' >> ' + relation, candidate)
