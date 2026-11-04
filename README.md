# End-to-End Order Automation

Python and Playwright automation for an ISP order simulator using visible Chromium.

## Features
- Fill customer names, service addresses, and property details.
- Handle moving dates and optional Lead IDs.
- Support new and transfer customers.
- Check provider availability.
- Select an exact internet plan and pricing term.
- Configure optional video and phone services.
- Continue through supported checkout screens.

## Setup
Requires Python and Git.

    py -m venv .venv
    .\.venv\Scripts\python.exe -m pip install playwright
    .\.venv\Scripts\python.exe -m playwright install chromium

## Configuration
Edit test_start_session.py:
- Set URL to your authorized simulator address locally.
- Update ORDERS with approved test customer data.
- Set the provider, plan, prices, and optional services.
- Use "None" for unwanted video or phone services.

Keep credentials and real customer information out of Git.

## Run
    .\.venv\Scripts\python.exe .\test_start_session.py

Choose a test scenario and sign in inside Chromium.

## Status
Under development. Basic Information has been verified in local runs.
Provider selection and checkout automation are being tested.
Installation, final summary verification, and the complete end-to-end flow remain pending.
