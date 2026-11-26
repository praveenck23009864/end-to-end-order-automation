"""Runtime card type menu tests use synthetic input only."""

from pathlib import Path
from io import StringIO

import pytest
from uuid import uuid4

from sensitive_input import collect_sensitive_payload, select_card_type
import sensitive_input
from sensitive_schemas import SensitivePayloadError
import api_client
from jwcrypto import jwk
from jwe_crypto import encrypt_sensitive_payload


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        ("1", "Visa"),
        ("2", "MasterCard"),
        ("3", "American Express"),
        ("4", "Discover"),
    ],
)
def test_card_type_menu_returns_canonical_name(selection, expected):
    prompts = []
    result = select_card_type(reader=lambda prompt: prompts.append(prompt) or selection,
                              output=lambda _: None)
    assert result == expected
    assert len(prompts) == 1
    assert prompts == ["Enter card type: "]


def test_card_type_menu_reprompts_after_invalid_selection():
    answers = iter(("0", "visa", " 3 "))
    output = []
    result = select_card_type(reader=lambda _: next(answers), output=output.append)
    assert result == "American Express"
    assert output.count("Invalid selection. Enter 1, 2, 3, or 4.") == 2


def _reader_for(answers):
    answers = iter(answers)
    return lambda _: next(answers)


def test_collects_and_validates_full_synthetic_payload_without_echo(tmp_path):
    legacy_file = Path(__file__).resolve().parents[1] / "temporary_input.txt"
    existed_before = legacy_file.exists()
    output = []
    payload = collect_sensitive_payload(
        reader=_reader_for((
            "999999999", "1990-01-02", "1", "4242424242424242",
            "SYNTHETIC TEST", "999", "12", "2030",
        )),
        output=output.append,
    )
    assert payload == {
        "ssn": "999999999", "dob": "1990-01-02", "card_type": "Visa",
        "card_number": "4242424242424242", "cardholder_name": "SYNTHETIC TEST",
        "cvv": "999", "expiration_month": "12", "expiration_year": "2030",
    }
    rendered = "\n".join(output)
    assert "========== SENSITIVE INPUT ==========" in rendered
    assert "========== PAYMENT INFORMATION ==========" in rendered
    assert "1. Visa" in rendered and "4. Discover" in rendered
    assert all(secret not in rendered for secret in (
        "999999999", "1990-01-02", "4242424242424242", "SYNTHETIC TEST", "999"
    ))
    assert not (tmp_path / "temporary_input.txt").exists()
    assert legacy_file.exists() is existed_before


def test_identity_only_collection_does_not_prompt_for_payment():
    prompts = []
    answers = iter(("999999999", "1990-01-02"))

    def reader(prompt):
        prompts.append(prompt)
        return next(answers)

    payload = collect_sensitive_payload(
        payment_required=False, reader=reader, output=lambda _: None
    )
    assert prompts == ["SSN: ", "DOB: "]
    assert payload["ssn"] == "999999999"
    assert payload["dob"] == "1990-01-02"
    assert all(not payload[key] for key in (
        "card_type", "card_number", "cardholder_name", "cvv",
        "expiration_month", "expiration_year",
    ))


def test_production_sensitive_prompts_use_masked_reader(monkeypatch):
    calls = []
    answers = iter(("999999999", "1990-01-02"))

    def hidden_prompt(prompt):
        calls.append(prompt)
        return next(answers)

    monkeypatch.setattr(sensitive_input, "_read_masked", hidden_prompt)
    collect_sensitive_payload(payment_required=False, output=lambda _: None)
    assert calls == ["SSN: ", "DOB: "]


def test_windows_masked_reader_prints_stars_not_entered_characters(monkeypatch):
    typed = iter(("9", "9", "\b", "X", "\r"))
    terminal = StringIO()
    fake_msvcrt = type("FakeMsvcrt", (), {"getwch": staticmethod(lambda: next(typed))})
    monkeypatch.setattr(sensitive_input, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(sensitive_input.sys, "stdout", terminal)

    assert sensitive_input._read_masked("SSN: ") == "9X"
    assert terminal.getvalue() == "SSN: **\b \b*\n"
    assert "99" not in terminal.getvalue()


def test_blank_fields_are_reprompted_and_never_echoed():
    output = []
    payload = collect_sensitive_payload(
        payment_required=False,
        reader=_reader_for(("", "999999999", " ", "1990-01-02")),
        output=output.append,
    )
    assert payload["ssn"] == "999999999"
    assert payload["dob"] == "1990-01-02"
    assert output.count("A value is required. Please try again.") == 2
    assert "999999999" not in " ".join(output)


@pytest.mark.parametrize(
    "answers",
    [
        ("bad-ssn", "1990-01-02", "1", "4242424242424242", "TEST", "999", "12", "2030"),
        ("999999999", "not-a-date", "1", "4242424242424242", "TEST", "999", "12", "2030"),
        ("999999999", "1990-01-02", "1", "123", "TEST", "999", "12", "2030"),
    ],
)
def test_invalid_identity_or_payment_details_are_rejected_safely(answers):
    output = []
    with pytest.raises(SensitivePayloadError) as error:
        collect_sensitive_payload(
            reader=_reader_for(answers), output=output.append
        )
    message = str(error.value)
    assert "4242424242424242" not in message
    assert "999999999" not in message
    rendered = " ".join(output)
    assert all(secret not in rendered for secret in answers if secret not in {"1"})


def test_missing_answers_are_reprompted_without_constructing_payload():
    output = []
    with pytest.raises(StopIteration):
        collect_sensitive_payload(
            payment_required=False,
            reader=_reader_for(("",)),
            output=output.append,
        )
    assert output.count("A value is required. Please try again.") == 1


def test_encrypted_payload_is_passed_to_existing_api_client(monkeypatch):
    payload = collect_sensitive_payload(
        reader=_reader_for((
            "999999999", "1990-01-02", "1", "4242424242424242",
            "SYNTHETIC TEST", "999", "12", "2030",
        )),
        output=lambda _: None,
    )
    private_key = jwk.JWK.generate(kty="RSA", size=3072)
    public_key = private_key.export_public(as_dict=True)
    kid = "synthetic-test-key"
    public_document = {
        "alg": "RSA-OAEP-256", "enc": "A256GCM", "kid": kid,
        "jwk": {**public_key, "kid": kid},
    }
    run_id = str(uuid4())
    compact_jwe = encrypt_sensitive_payload(
        payload, order_id="synthetic-1", run_id=run_id,
        public_document=public_document,
    )
    captured = {}

    def fake_request(method, path, body):
        captured.update(method=method, path=path, body=body)
        return {"accepted": True}

    monkeypatch.setattr(api_client, "request", fake_request)
    response = api_client.submit_sensitive_payload(run_id, compact_jwe)
    assert response == {"accepted": True}
    assert captured == {
        "method": "POST",
        "path": f"/runs/{run_id}/sensitive",
        "body": {"jwe": compact_jwe},
    }
    assert "999999999" not in compact_jwe
    assert "4242424242424242" not in compact_jwe
