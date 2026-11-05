from playwright.sync_api import expect, sync_playwright

url = input("Enter the simulator URL: ").strip()

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto(url, wait_until="domcontentloaded")

    print("Sign in inside the browser and click Continue.")
    input("When Basic Information is visible, press Enter here: ")

    zip_field = page.locator('input[placeholder="Zip Code" i]:visible:enabled')
    expect(zip_field).to_be_visible(timeout=15000)
    expect(zip_field).to_be_editable(timeout=15000)

    print("PASS: Playwright found the editable Zip Code field.")
    input("Press Enter to close the browser: ")
    browser.close()
