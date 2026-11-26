"""Collect simulator identity and payment values through hidden terminal prompts."""

import getpass
import sys

try:
    import msvcrt
except ImportError:  # Non-Windows fallback retains hidden input without star echo.
    msvcrt = None

from sensitive_schemas import validate_sensitive_payload

CARD_TYPES = {
    "1": "Visa",
    "2": "MasterCard",
    "3": "American Express",
    "4": "Discover",
}

BASE_REQUIRED_KEYS = {"SSN", "DOB"}
PAYMENT_REQUIRED_KEYS = {
    "CARD_TYPE",
    "CARD_NUMBER",
    "CARDHOLDER_NAME",
    "CVV",
    "EXPIRY_MONTH",
    "EXPIRY_YEAR",
}


def _read_required(prompt, *, reader, output):
    while True:
        value = reader(prompt)
        if value and value.strip():
            return value.strip()
        output("A value is required. Please try again.")


def _read_masked(prompt):
    """Read a value with asterisk feedback on Windows, never echoing its text."""
    if msvcrt is None:
        return getpass.getpass(prompt)

    sys.stdout.write(prompt)
    sys.stdout.flush()
    characters = []
    try:
        while True:
            character = msvcrt.getwch()
            if character in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "".join(characters)
            if character == "\003":
                raise KeyboardInterrupt
            if character == "\b":
                if characters:
                    characters.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if character in ("\x00", "\xe0"):
                msvcrt.getwch()  # Consume the second half of a special key.
                continue
            characters.append(character)
            sys.stdout.write("*")
            sys.stdout.flush()
    finally:
        characters.clear()


def select_card_type(*, reader=None, output=print):
    """Read a numbered card type; the non-sensitive selection is visible."""
    if reader is None:
        reader = input
    while True:
        selection = reader("Enter card type: ")
        card_type = CARD_TYPES.get(str(selection).strip())
        if card_type is not None:
            return card_type
        output("Invalid selection. Enter 1, 2, 3, or 4.")


def collect_sensitive_payload(
    *, payment_required=True, reader=None, selection_reader=None, output=print
):
    """Collect required values with masked terminal feedback and no file writes.

    ``reader``, ``selection_reader`` and ``output`` are injectable for tests.
    Production uses asterisk-masked input for sensitive values and ordinary
    terminal input for the numbered card selection.
    """
    if reader is None:
        reader = _read_masked
    if selection_reader is None:
        selection_reader = reader if _read_masked is not None and reader is not _read_masked else input

    values = {}
    payload = {}
    try:
        output("========== SENSITIVE INPUT ==========")
        values["SSN"] = _read_required("SSN: ", reader=reader, output=output)
        values["DOB"] = _read_required("DOB: ", reader=reader, output=output)

        if payment_required:
            output("")
            output("========== PAYMENT INFORMATION ==========")
            output("Card type:")
            output("1. Visa")
            output("2. MasterCard")
            output("3. American Express")
            output("4. Discover")
            values["CARD_TYPE"] = select_card_type(
                reader=selection_reader, output=output
            )
            values["CARD_NUMBER"] = _read_required(
                "Card number: ", reader=reader, output=output
            )
            values["CARDHOLDER_NAME"] = _read_required(
                "Cardholder name: ", reader=reader, output=output
            )
            values["CVV"] = _read_required("CVV: ", reader=reader, output=output)
            values["EXPIRY_MONTH"] = _read_required(
                "Expiry month: ", reader=reader, output=output
            )
            values["EXPIRY_YEAR"] = _read_required(
                "Expiry year: ", reader=reader, output=output
            )

        payload.update(
            {
                "ssn": values["SSN"],
                "dob": values["DOB"],
                "card_type": values.get("CARD_TYPE", ""),
                "card_number": values.get("CARD_NUMBER", ""),
                "cardholder_name": values.get("CARDHOLDER_NAME", ""),
                "cvv": values.get("CVV", ""),
                "expiration_month": values.get("EXPIRY_MONTH", ""),
                "expiration_year": values.get("EXPIRY_YEAR", ""),
            }
        )
        validate_sensitive_payload(payload, payment_required=payment_required)
        return payload
    except Exception:
        for key in tuple(payload):
            payload[key] = ""
        payload.clear()
        raise
    finally:
        for key in tuple(values):
            values[key] = ""
        values.clear()
