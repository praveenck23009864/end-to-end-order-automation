# Allconnect sensitive field update

## Current workspace entry points

- API: `api.py`, with `api_client.py` and `database.py`; the default live store is
  `allconnect_secure.db`. Preserve both databases and their run history.
- Automation: `new.py` delegates execution to `run_from_api.py`. Start it with
  `.\.venv\Scripts\python.exe .\new.py --sample-payment --fast` for the simulator
  sample-payment path. Login is manual, and final order submission is not automated.
- Encryption/import support: `sensitive_data.py`, `secure_setup.py`,
  `order_workflow.py`, `protect_orders.py`, `import_orders.py`, and
  `migrate_sensitive_db.py`. Existing keys must be reused.
- Active regressions: `tests/test_sensitive_data.py` and `tests/test_sensitive_browser.py`.
  Run the full suite without regenerating project caches:
  `.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests -q`.
- Payment troubleshooting: retain `tools/inspect_payment_fields.py` and
  `tools/verify_payment_live.py`. The corrected Order Summary assertion still needs a
  fresh live run; the earlier run remains failed even though its browser reached
  Order Summary.
- Retained legacy material: `legacy/test_start_session.py` has local edits and is not the
  active entry point. There are exactly two order JSON files: editable scenarios
  in `orders.json` and the current encrypted export in `orders.encrypted.json`.

## Project layout

```text
allconnect-automation/
├── .git/                         # preserved Git history
├── .venv/                        # existing environment
├── .gitignore
├── pytest.ini                    # discovers only maintained tests/
├── README.md
├── requirements-sensitive.txt
├── new.py                        # automation entry point
├── run_from_api.py               # tracked workflow runner
├── api.py
├── api_client.py
├── database.py
├── sensitive_data.py
├── order_workflow.py
├── protect_orders.py
├── import_orders.py
├── migrate_sensitive_db.py
├── secure_setup.py
├── orders.json
├── orders.encrypted.json
├── allconnect.db
├── allconnect_secure.db
├── tests/
│   ├── test_sensitive_browser.py
│   └── test_sensitive_data.py
├── tools/
│   ├── __init__.py
│   ├── inspect_payment_fields.py
│   └── verify_payment_live.py
└── legacy/
    └── test_start_session.py     # preserved local edits; not collected
```

Database WAL/SHM sidecars, `.env`, and local credentials stay in their existing
locations when present. The DPAPI-protected key and API credential remain under
`%LOCALAPPDATA%\AllconnectAutomation\secrets.dpapi`; no key setup is needed just
because files moved. `.gitignore` covers local data, sidecars, secrets, and caches;
it does not untrack files already in Git.

Run the commands below from `D:\allconnect-automation`. Runtime modules and data
stay at the root, so API, automation, encryption, and import commands are unchanged.
Do not re-import fixtures or rerun migration merely to start the existing project.

This package updates the uploaded `new.py` and `run_from_api.py` to use encrypted SSN and DOB records. It keeps the current 2–3 minute pacing target, manual login setup and existing checkout choices. Decryption happens when the credit-check fields are ready. Both fields are blurred before entry and filled in one operation. Native DOB inputs receive `YYYY-MM-DD`. An optional `--sample-payment` step fills fixed dummy card values from the supplied simulator example and clicks payment Forward once.

The API support files use the earlier Allconnect SQLite schema with `orders.payload` and `runs.order_snapshot`. They are supplied as a matched set because the latest API and database files were not included in the upload. Compare any additional local changes before replacing these four files.

## What is protected

- Fernet authenticates and encrypts SSN and DOB together. Each payload contains its customer ID, which is verified during decryption.
- JSON exports, active SQLite orders and newly created run snapshots contain encrypted sensitive payloads.
- Windows current-user DPAPI protects the local encryption key and API credential under `%LOCALAPPDATA%\AllconnectAutomation\secrets.dpapi`.
- API routes require the locally managed bearer credential. The client retrieves it automatically, follows no HTTP redirects and uses the loopback address.
- Error output contains controlled messages and source locations. It omits raw Playwright exceptions, source-code lines and customer values.
- The runner creates a fresh browser context without trace, video or HAR recording. It refuses to start with `DEBUG` or `PWDEBUG` logging enabled.
- Browser blur is a visual effect. Plaintext still exists temporarily in Python and the page. Python and Chromium do not provide a guaranteed memory wipe here. Extensions, operating-system capture and a compromised Windows account remain outside this protection.

Names, addresses, phone numbers and email addresses remain ordinary customer fields. Full database encryption, remote deployment, managed-identity key access and automated key rotation are outside this local update.

## Install in the existing project

1. Stop the automation and the running API. Preserve the current Python scripts using Git or a local code backup that excludes real customer data.
2. Extract this folder. Copy its Python files into `D:\allconnect-automation`. Keep the existing customer JSON and database files in place.
3. Set `URL` at the top of the supplied `new.py` to the simulator HTTPS URL. The uploaded file contained `*******`, so the package retains that placeholder.
4. Keep the provided `.gitignore`. Ignore entries do not remove files already tracked by Git. Keep real customer files and keys out of commits.
5. Install and initialize under the same Windows account used for the API and automation.

```powershell
cd D:\allconnect-automation
.\.venv\Scripts\python.exe -m pip install -r .\requirements-sensitive.txt
.\.venv\Scripts\python.exe .\secure_setup.py
```

Setup can be repeated. It verifies and reuses existing keys. Do not delete or replace the DPAPI key file after encrypting records. It is normally usable only under the original Windows user and computer. Loss of that key can make the encrypted data unrecoverable. Plan key recovery before introducing real customer data.

The default live database is now `allconnect_secure.db`. If you previously configured `ALLCONNECT_DB`, clear it for the default commands below or deliberately point it to the new encrypted database.

```powershell
Remove-Item Env:ALLCONNECT_DB -ErrorAction SilentlyContinue
```

## Protect the existing JSON fixtures

Create or refresh the canonical encrypted export, then import it only when you
intend to update the database. Each customer keeps the same ID and selections.
SSN and DOB move into a `Sensitive Data` envelope, and a `Customer ID` property is
added. The existing local encryption key is reused.

```powershell
.\.venv\Scripts\python.exe .\protect_orders.py .\orders.json .\orders.encrypted.json
.\.venv\Scripts\python.exe .\import_orders.py .\orders.encrypted.json --update
```

After editing `orders.json`, rerun the same encryption command. The entire batch
is validated and encrypted in memory before a temporary file is written in the
destination directory. The temporary file is flushed, synced, and closed before
an atomic replacement of `orders.encrypted.json`. Validation, encryption, write,
or replacement failures leave the previous export intact; temporary files are
cleaned up. Only the canonical output name is accepted, and overwriting the
editable source (including a path alias or hard link) is rejected.

Encryption does not import anything or modify databases, run history, or existing
snapshots. The import command is a separate, explicit operation; `--update`
updates current order records and refuses to update an order with an active run.
Do not create timestamped, UUID-named, or scenario-specific exports.

The original JSON remains as supplied. If it contains plaintext sensitive values, it remains a plaintext copy. Apply the organization's retention and secure-disposal process to it, earlier exports, editor backups and any tracked history. Encryption of the new file does not erase earlier copies. Use dummy data for this first test.

## Preserve earlier run history

If you want the old order records and run history in the encrypted database, use migration instead of creating the destination with the importer first. Stop API and browser workers before migration. The destination must not exist.

```powershell
.\.venv\Scripts\python.exe .\migrate_sensitive_db.py .\allconnect.db .\allconnect_secure.db
```

Migration encrypts both current orders and historical order snapshots. It preserves record IDs, statuses and timestamps. Unrecognized legacy stage strings become `legacy stage omitted` so arbitrary old diagnostic values are not copied into the new stage metadata. Unsupported card fields cause migration to stop for review. The source database is preserved.

If a run still has `running` status, migration stops. Confirm its worker has stopped, then close that run through the earlier API before migrating. Do not mark a live run cancelled while its browser is still operating. The earlier API supports the following recovery call, with the actual run ID substituted.

```powershell
.\.venv\Scripts\python.exe -c "import api_client; api_client.progress('RUN-ID-HERE', 'cancelled', 'manual recovery', 'manual_recovery')"
```

Run `secure_setup.py` first so the updated client has its credential. The earlier local API can ignore the added Authorization header during this recovery step. Once active runs are resolved, stop it before copying the database.

The old database, its journals, backups and original customer JSON retain their prior contents. Keep them restricted and apply the approved retention process. This migration provides a clean encrypted destination rather than attempting forensic deletion from the source or storage device.

## Run the protected workflow

Start the API in the first terminal.

```powershell
cd D:\allconnect-automation
.\.venv\Scripts\python.exe -m uvicorn api:app --host 127.0.0.1 --port 8000 --no-access-log
```

Start automation in another terminal under the same Windows account.

```powershell
cd D:\allconnect-automation
.\.venv\Scripts\python.exe .\new.py
```

Choose a customer and complete the existing manual login/session prompt. The runner validates the protected record without decrypting its SSN or DOB. At the credit-check step, it decrypts them into a short-lived local dictionary, blurs the inputs, enters the values and clicks the existing simulator credit-check button. It releases its references to the private dictionary after that operation. Installation choices and the existing final review pause follow.

The API client supplies authentication automatically. An ordinary unauthenticated browser request to `/orders` now returns HTTP 401. API traffic is limited to `127.0.0.1` in this package. A remote deployment requires authenticated HTTPS and an appropriate shared key-management design.

## Payment screen follow-up

The current simulator uses adjacent labels and controls in a shared grid inside
`checkoutContent`; the Card Number ancestor contains all five text inputs.
Payment selectors now use each label's immediate sibling (or adjacent table cell
for the earlier fixture). The hold disclosure is acknowledged before card entry,
and Edit Secure Fields enables the disabled controls. Payment Forward navigates
to the top-level `Order Summary` page; the runner verifies its title and stops
before final submission.

An order may opt into the fixed simulator-only dummy profile with
`"Sample Payment": true`. Customer 5 has this flag enabled. Card values remain
fixed in code; existing encrypted fields and historical run snapshots are not
rewritten. `--sample-payment` continues to enable the same profile explicitly.

`tools/verify_payment_live.py` starts a new tracked diagnostic run and pauses at payment.
Its `inspect`, `read`, `add`, and `edit` commands report value-free candidate
metadata before acting. `pay` attempts sample entry and payment Forward once;
a failure remains failed and cannot replay payment in that run. Diagnostics
include operation names and source locations, never raw exceptions or page HTML.

```powershell
.\.venv\Scripts\python.exe -B .\tools\verify_payment_live.py
# Equivalent module launch from the project root:
.\.venv\Scripts\python.exe -B -m tools.verify_payment_live
```

The tool inserts the project root into its import path for direct script launch;
it does not relocate data or change the working directory. The inspection module
is imported as `tools.inspect_payment_fields` by the runner and tests.

The supplied screenshots show `Add a new credit card`, `Edit Secure Fields`, Card Type, Card Number, Card Holder Name, CVV Code, separate expiration month/year fields and billing-address Yes/No. The added sample-payment function uses these visible labels and the screenshot's radio order, with strict field-count checks. Screenshots do not establish the input IDs or secure-iframe behavior. These selectors need a live simulator test. The screen states that card entry can place a hold.

Run the sample-card path with the following command after completing the encryption setup.

```powershell
.\.venv\Scripts\python.exe .\new.py --sample-payment
```

This mode defers the installation Forward click, selects Add a new credit card, uses Edit Secure Fields if the controls are locked, chooses Visa, and fills the fixed dummy number/CVV/expiry values from the example. It uses `DEMO CUSTOMER` as the sample holder name. The sensitive card inputs are blurred before filling. It selects billing address Yes, checks the payment disclosure as a simulated acknowledgment, and clicks Forward once. It verifies that the payment section disappears before reporting completion. If the simulator rejects the sample number or the selectors do not match, it stops for inspection.

The sample mode accepts only the exact simulator host shown in the screenshots. It uses public dummy fixtures in the Python code, without reading card values from the customer JSON or writing them into SQLite/run history. The existing terminal run status is `installation_completed`, with the stage `payment Forward clicked` for this path. It pauses for review on the next screen. Final authorization and order submission remain a later step.

Customer JSON, database imports and snapshots reject known card-data keys. Real card storage and CVV persistence are unsupported. For a real payment integration, first establish the provider's approved hosted/tokenized or runtime-only card collection process and use its test-card environment for integration testing.

To collect selectors for the next implementation, use the optional inspection mode. After the installation flow reaches review, manually open the payment fields and leave them empty. The helper reports input IDs, names, types, placeholders, read-only state and nested iframe count. It does not read field values, page HTML or screenshots. This helps identify what `Edit Secure Fields` changes.

```powershell
.\.venv\Scripts\python.exe .\new.py --inspect-payment-fields
```

Share the `PAYMENT FIELD SELECTORS AND TYPES` output after opening the fields if the sample step cannot locate a control. Use inspection mode separately from sample-payment mode. Do not share actual SSN, DOB, card numbers or CVV.

## Verification

The maintained suite contains 80 tests covering encryption, atomic export refresh,
API authentication, immutable snapshots, migration, concurrency, workflow choices,
and synthetic Chromium field entry/navigation. Tests use disposable keys and
temporary databases; they do not modify the live orders or run history.
`pytest.ini` limits default discovery to `tests/` and provides root-module imports.
The legacy manual script is intentionally excluded.

The suite passed after organization. Live payment reached Order Summary in the
earlier diagnostic run, but that run remains failed because its destination
assertion was incorrect at the time. The corrected assertion still needs a fresh
live verification; this organization task did not launch a customer workflow.

```powershell
.\.venv\Scripts\python.exe -m pip install pytest httpx
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests -q
```

Run the local DPAPI setup and a dummy-customer browser test before using real data. Keep trace/video/HAR capture disabled through sensitive entry and any later screen retaining the values. Visual masking alone does not encrypt browser memory or diagnostic artifacts.

## References

- Fernet authenticated encryption and key handling — https://cryptography.io/en/stable/fernet/
- Windows DPAPI current-user behavior — https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata
- Playwright tracing captures operations and network activity — https://playwright.dev/python/docs/api/class-tracing
- PCI SSC guidance on CVV retention after authorization — https://www.pcisecuritystandards.org/faqs/1319/
