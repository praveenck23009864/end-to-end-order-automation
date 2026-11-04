# End-to-End Order Automation

A Python and Playwright project that automates customer information entry and supported order steps in an ISP order simulator.

The project uses visible Chromium so the operator can watch the automation and inspect any errors.

## Current Status

This project is under development.

Basic Information entry has worked in local runs. Qualification, provider navigation, plan selection, and checkout are being tested and refined.

The complete order journey has not yet been verified end to end.

## Features

- Select different test customer scenarios.
- Fill customer names and service addresses.
- Enter ZIP first to unlock address fields.
- Select property type and ownership status.
- Fill the moving date when Moving is Yes.
- Leave Lead ID empty unless supplied.
- Handle new and transfer customer choices.
- Select the current provider for transfer customers.
- Check provider availability.
- Match an internet plan by its full name and pricing term.
- Verify the expected base price before checkout.
- Select optional video and phone services.
- Use None when an optional service is not requested.
- Report the stage where automation stops.

## Technology

- Python
- Playwright synchronous API
- Chromium
- Windows PowerShell
- Git and GitHub

## Project Files

    end-to-end-order-automation/
    ├── README.md
    └── test_start_session.py

The Python script contains the customer scenarios, browser actions, field locators, and verification checks.

## Requirements

- Python installed and available through the py launcher
- Git
- Access to the authorized order simulator
- Approved test customer data

## Installation

Open PowerShell in the project folder:

    cd D:\allconnect-automation

Create a virtual environment:

    py -m venv .venv

Install Playwright and Chromium:

    .\.venv\Scripts\python.exe -m pip install playwright
    .\.venv\Scripts\python.exe -m playwright install chromium

## Configuration

Open the script:

    notepad .\test_start_session.py

### Simulator URL

Set the URL locally:

    URL = "YOUR_AUTHORIZED_SIMULATOR_HTTPS_URL"

Keep internal addresses and credentials out of published code.

### Customer Scenarios

Each entry in ORDERS represents one customer scenario.

Configure:

- Name and optional prefix, middle initial, and suffix
- Service ZIP, street, unit, city, and state
- Dwelling type
- Own or rent
- Moving status and moving date
- Optional Lead ID
- New or transfer customer
- Current provider for transfers
- Requested internet provider and plan
- Expected base price
- Optional video and phone products

Keep ZIP codes as strings to preserve leading zeros.

### Moving Date

When Moving is Yes, provide a valid date:

    "Moving?": "Yes",
    "Move Date": "2026-11-15",

The configured date uses YYYY-MM-DD format.

When Moving is No, the script skips date entry.

### Optional Services

Example with no additional services:

    "Video": "None",
    "Video Price": "0.00",
    "Phone": "None",
    "Phone Price": "0.00",

Example with video:

    "Video": "World Soccer Ticket",
    "Video Price": "85.00",
    "Phone": "None",
    "Phone Price": "0.00",

Product names and prices must match the simulator's current offers. Example amounts are test values from observed screens.

## Running the Automation

Run:

    .\.venv\Scripts\python.exe .\test_start_session.py

Then:

1. Choose a configured test customer.
2. Sign in inside the Chromium window.
3. Allow the script to enter customer information.
4. Watch qualification and provider selection.
5. Review the selected plan and checkout options.
6. Inspect the browser if the script reports a failure.

## Intended Workflow

1. Start Session.
2. Continue through the greeting.
3. Fill Basic Information.
4. Confirm the matched address.
5. Select New or Transfer.
6. Select the current provider when transferring.
7. Review available providers.
8. Open the requested provider's plans.
9. Match the requested plan and pricing term.
10. Open View Details.
11. Select Add to Order and Checkout.
12. Choose optional video and phone products.
13. Continue to subsequent checkout steps.
14. Complete installation and final summary verification.

Later checkout steps remain pending implementation or live validation.

## Troubleshooting

### Multiple elements matched

A locator may match hidden panels or repeated labels. Scope it to the active section, visible heading, or correct iframe.

### Move-In Date was not found

The simulator may expose the date field as a textbox rather than a native date input. Use the locator appropriate to the live page.

### Checkout controls were not found

Checkout options are inside the checkoutContent iframe. Locate its contents through Playwright's frame_locator.

### Provider remains Pending

Availability checks may still be processing. The script should proceed only after the requested provider becomes Available.

### Requested plan or price differs

Review the live offer and customer requirements. Do not substitute the first available plan.

### Script stops unexpectedly

Read the terminal's reported stage and inspect the open browser before closing it.

## Known Limitations

- Signing in currently requires manual interaction.
- Page layout changes may require locator updates.
- Offers and prices can vary by address.
- Some controls are still being validated against live simulator screens.
- Installation and final order summary automation are not complete.
- A successful intermediate step does not mean the entire order is complete.

## Data Handling

Use approved test data during development.

Do not commit passwords, real customer records, session files, or screenshots containing private information.

## Planned Improvements

- Finish the remaining checkout screens.
- Add account-holder and contact information handling.
- Support equipment choices.
- Automate self-install and professional-install scenarios.
- Verify the final order summary against customer requirements.
- Improve failure diagnostics.
- Test additional customer and provider scenarios.

## Author

Praveen CK
