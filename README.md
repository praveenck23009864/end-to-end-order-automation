# ProviderXX Order Automation

## Runtime security flow

The runner collects SSN, DOB, and payment card fields at runtime using masked
input. It obtains the authenticated API's RSA public key, encrypts exactly these
eight values as compact JWE (`RSA-OAEP-256` + `A256GCM`), and submits the JWE
once. The API validates the protected header, schema, order/run binding, expiry,
and single-use request ID, then decrypts in memory. SQLite may retain only the
SHA-256 digest and expiry of a consumed random request ID for replay prevention;
it never retains the JWE or decrypted values. The API process launches and
owns Playwright, fills the relevant simulator fields, and clears references in
`finally`. Decrypted values and JWE tokens are not put in API responses,
ordinary order dictionaries, SQLite, or application logs. Python and Chromium
cannot guarantee zeroization of every memory copy.

Billing selection and billing address fields remain ordinary customer/order
data. They are excluded from the JWE schema, input collector, encryption, and
decryption. When billing is different, Playwright chooses No, waits for the
conditional fields, reads the address, city, state, and ZIP from the existing
order, and stops with a validation error if any are missing. It never substitutes
the service address. When it is the same, Playwright chooses Yes and does not
fill separate address fields.

The payment workflow verifies the mandatory disclosure, enters the runtime card
values, verifies the billing choice, and stops for manual review. It does not
click Forward or submit an order. EZ Enroll Yes uses the payment review; EZ
Enroll No retains its existing installation navigation. No real card data
should be used in simulator testing.

## RSA keys

The RSA-3072 key ring is stored outside the repository at
`%LOCALAPPDATA%\AllconnectAutomation\cse-rsa-3072.dpapi`. DPAPI protects the
key ring for the current Windows account. The existing
`%LOCALAPPDATA%\AllconnectAutomation\secrets.dpapi` API credential store is
separate and is not overwritten by CSE setup. The RSA private key is never
served; the authenticated public-key endpoint returns only the public JWK and
`kid`.

Run the API and runner as the same Windows account that created the DPAPI
protected key ring. Preserve that protected file and its account/computer
context for recovery; losing access to it makes existing ciphertext
undecryptable. Key rotation uses a new `kid` and retains old decryption keys in
the protected ring. Do not delete the ring or regenerate keys as a recovery
shortcut. CSE key setup is lazy: the first key access creates a key only if no
usable ring exists.

## Local setup

Install the maintained JOSE dependency in the project's Python 3.11 environment:

```powershell
cd D:\end-to-end-order-automation-run
.\.venv\Scripts\python.exe -m pip install -r .\requirements-sensitive.txt
```

The API uses `allconnect_demo.db` in the project directory. Inherited
`ALLCONNECT_DB` environment values are ignored so the API cannot silently load
a different stale database.

Start the authenticated local API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn api:app --host 127.0.0.1 --port 8000 --no-access-log
```

In another terminal under the same account, run the masked-input client:

```powershell
cd D:\end-to-end-order-automation-run
.\.venv\Scripts\python.exe .\run_from_api.py
```

Non-local API communication must use HTTPS. The client rejects non-HTTPS
non-loopback URLs. Keep debug logging, screenshots, traces, video, and HAR
recording disabled during sensitive entry. Do not add request-body logging.

## Stale run recovery

Workers register a non-sensitive process ID and heartbeat in `run_workers`.
Recovery requires the run to be at least 15 minutes past its last progress
update, the API's worker check to find no active worker, and an explicit
operator confirmation that its Playwright browser is stopped. Age alone never
resolves a run. Legacy runs without a worker lease require the same manual
process check.

Before recovery, inspect the simulator's persisted order/confirmation status.
Then use the authenticated recovery command:

```powershell
.\.venv\Scripts\python.exe .\recover_stale_runs.py
```

Choose `submitted` only when the simulator confirms the order, `not submitted`
only when the simulator confirms it was not accepted, or `unknown` when the
result cannot be established. Unknown remains active and blocks another run.
Recovery never resubmits an order. `final_order_submitted` becomes true only
after a persisted simulator confirmation.

## Existing data and migration

New imports, run snapshots, and database reads reject sensitive identity/payment
fields and legacy encrypted envelopes. `scrub_sensitive_data.py` can redact
legacy sensitive keys while preserving order/run rows, but review and back up
the database and JSON exports under the organization's data-retention process
before running it. SQLite WAL/VACUUM cannot guarantee forensic erasure from
backups, snapshots, filesystem journals, or storage media. This utility does
not modify or delete the DPAPI key files.

## Validation

Run the tests with the project Python environment:

```powershell
.\.venv\Scripts\python.exe -m py_compile new.py run_from_api.py sensitive_data.py api.py api_client.py database.py jwe_crypto.py sensitive_schemas.py sensitive_input.py
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use synthetic identity and payment values and mocked/local browser pages;
they do not submit a real order.
