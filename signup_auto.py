# signup_auto.py
import json
import re
import secrets
import string
import time
from pathlib import Path

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException


PELLA_SIGNUP_URL = "https://www.pella.app/signup"
TEMPMAIL_URL = "https://temp-mail.org/en/"
COOKIE_OUTPUT = Path("pella_cookies.json")


# =====================================================================
# Helpers
# =====================================================================
def gen_password(length=20):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def human_type(element, text, per_char=0.08):
    for ch in text:
        element.send_keys(ch)
        time.sleep(per_char)


def wait_visible(driver, by, sel, timeout=30):
    return WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((by, sel))
    )


def make_driver():
    opts = uc.ChromeOptions()
    opts.add_argument("--start-maximized")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--lang=en-US")

    return uc.Chrome(
        options=opts,
        use_subprocess=True,
        version_main=147,
    )


def open_new_tab(driver):
    """Selenium 4 method — bypasses popup blocker."""
    driver.switch_to.new_window("tab")
    return driver.current_window_handle


# =====================================================================
# Temp-mail — read the email address
# =====================================================================
def read_temp_mail_email(driver, timeout=60):
    print("Waiting for temp-mail address to load...")
    mail_input = wait_visible(driver, By.ID, "mail", timeout=timeout)

    deadline = time.time() + timeout
    attempt = 0
    last_value = None

    while time.time() < deadline:
        attempt += 1
        try:
            val = mail_input.get_attribute("value")
        except Exception:
            val = None

        if val != last_value:
            print(f"  attempt #{attempt}: value={val!r}")
            last_value = val

        if val and "@" in val and "." in val.split("@")[-1] and "loading" not in val.lower():
            return val.strip()

        try:
            dval = mail_input.get_attribute("data-value")
            if dval and "@" in dval and "loading" not in dval.lower():
                return dval.strip()
        except Exception:
            pass

        time.sleep(1.5)

    raise RuntimeError(f"Could not read email from #mail. Last: {last_value!r}")


# =====================================================================
# Temp-mail — wait for OTP email
# =====================================================================
def wait_for_otp(driver, timeout=180):
    """Read the OTP from temp-mail preview text, or navigate to the email."""
    print(f"Waiting up to {timeout}s for the verification email...")
    deadline = time.time() + timeout
    attempt = 0

    patterns = [
        r"(\d{6})\s+is your verification code",
        r"verification code[^\d]{0,40}(\d{6})",
        r"\bcode[^\d]{0,40}(\d{6})",
        r"\bOTP[^\d]{0,40}(\d{6})",
    ]

    while time.time() < deadline:
        attempt += 1

        # Strategy 1: OTP visible in inbox preview
        try:
            page_text = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            page_text = ""

        for pat in patterns:
            m = re.search(pat, page_text, re.IGNORECASE)
            if m:
                print(f"  poll #{attempt}: ✓ OTP from preview: {m.group(1)}")
                return m.group(1)

        # Strategy 2: navigate to the first email
        try:
            first_link = driver.find_element(
                By.CSS_SELECTOR,
                ".inbox-dataList a.viewLink, .inbox-dataList a[href*='/view/']",
            )
            href = first_link.get_attribute("href")
            if href:
                print(f"  poll #{attempt}: opening email ...{href[-30:]}")
                driver.get(href)
                time.sleep(2.5)

                try:
                    email_text = driver.find_element(By.TAG_NAME, "body").text
                except Exception:
                    email_text = ""

                for pat in patterns:
                    m = re.search(pat, email_text, re.IGNORECASE)
                    if m:
                        print(f"  ✓ OTP from email body: {m.group(1)}")
                        return m.group(1)

                print("  (no OTP in opened email, going back)")
                driver.get(TEMPMAIL_URL)
                time.sleep(2)
        except Exception as e:
            print(f"  poll #{attempt}: no rows yet ({str(e)[:50]})")

        time.sleep(4)

    raise TimeoutError("No OTP email arrived within the time limit.")


# =====================================================================
# Main
# =====================================================================
def main():
    password = gen_password()
    print(f"Generated password: {password}")
    print("(save this — you may need it if the run fails partway)\n")

    driver = make_driver()
    success = False
    cookies_saved = False
    error_msg = None
    email = None

    try:
        # ------------------------------------------------------------------
        # STEP 1: Temp-mail tab — grab an email address
        # ------------------------------------------------------------------
        print("Opening temp-mail.org...")
        driver.get(TEMPMAIL_URL)
        temp_handle = driver.current_window_handle

        email = read_temp_mail_email(driver, timeout=60)
        print(f"\n  ✓ EMAIL: {email}\n")

        # ------------------------------------------------------------------
        # STEP 2: Open Pella in a NEW tab (Selenium 4 native — no popup block)
        # ------------------------------------------------------------------
        print("Opening Pella signup in a new tab...")
        pella_handle = open_new_tab(driver)
        driver.get(PELLA_SIGNUP_URL)

        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(1.5)

        print("Filling email...")
        email_input = wait_visible(driver, By.ID, "emailAddress-field")
        email_input.click()
        time.sleep(0.3)
        email_input.send_keys(Keys.CONTROL, "a")
        email_input.send_keys(Keys.DELETE)
        time.sleep(0.3)
        human_type(email_input, email)
        time.sleep(0.5)

        print("Filling password...")
        pw_input = wait_visible(driver, By.ID, "password-field")
        pw_input.click()
        time.sleep(0.3)
        pw_input.send_keys(Keys.CONTROL, "a")
        pw_input.send_keys(Keys.DELETE)
        time.sleep(0.3)
        human_type(pw_input, password)
        time.sleep(0.7)

        print("Clicking Continue...")
        driver.find_element(
            By.CSS_SELECTOR, "button.cl-formButtonPrimary"
        ).click()

        # Watch for CAPTCHA or OTP prompt
        print("Waiting for next screen...")
        deadline = time.time() + 30
        saw_captcha = False
        while time.time() < deadline:
            if "pella.app" not in driver.current_url:
                print(f"  → navigated away: {driver.current_url}")
                return

            if driver.find_elements(
                By.CSS_SELECTOR,
                "iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile']",
            ):
                saw_captcha = True
                break

            if driver.find_elements(
                By.CSS_SELECTOR,
                "input[autocomplete='one-time-code'], input[data-input-otp='true']",
            ):
                break

            time.sleep(0.5)

        if saw_captcha:
            print("\n*** CAPTCHA appeared. Solve it in the browser window. ***")
            input("Press ENTER here once solved: ")

        # ------------------------------------------------------------------
        # STEP 3: Switch to temp-mail tab (Pella stays open in its own tab)
        # ------------------------------------------------------------------
        print("\nSwitching to temp-mail tab to read OTP...")
        driver.switch_to.window(temp_handle)

        try:
            otp = wait_for_otp(driver, timeout=180)
            print(f"\n  ✓ OTP: {otp}\n")
        except TimeoutError as e:
            error_msg = str(e)
            print(f"\nOTP FETCH FAILED: {e}")
            return

        # ------------------------------------------------------------------
        # STEP 4: Switch BACK to Pella tab — form state is still there!
        # ------------------------------------------------------------------
        print("Switching back to Pella tab (form state preserved)...")
        driver.switch_to.window(pella_handle)
        time.sleep(1)

        # Verify we're still on the OTP screen
        otp_input = wait_visible(
            driver,
            By.CSS_SELECTOR,
            "input[autocomplete='one-time-code'], input[data-input-otp='true']",
            timeout=30,
        )
        otp_input.click()
        human_type(otp_input, otp, per_char=0.1)
        time.sleep(0.5)
        otp_input.send_keys(Keys.ENTER)

        # ------------------------------------------------------------------
        # STEP 5: Wait for redirect
        # ------------------------------------------------------------------
        print("Waiting for verification...")
        try:
            WebDriverWait(driver, 90).until(
                lambda d: "signup" not in d.current_url.lower()
            )
            print(f"  → redirected to: {driver.current_url}")
        except TimeoutException:
            print("  → still on signup URL, saving cookies anyway")

        cookies = driver.get_cookies()
        COOKIE_OUTPUT.write_text(json.dumps(cookies, indent=2))
        cookies_saved = True
        success = True

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)[:300]}"
        print(f"\nERROR: {error_msg}")
        try:
            print(f"  → current URL: {driver.current_url}")
            driver.save_screenshot("signup_error.png")
            print("  → screenshot: signup_error.png")
        except Exception:
            pass

    finally:
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"Email:           {email or '(not set)'}")
        print(f"Password:        {password}")
        print(f"Status:          {'SUCCESS' if success else 'FAILED'}")
        if cookies_saved:
            print(f"Cookies saved:   {COOKIE_OUTPUT.resolve()}")
        if error_msg:
            print(f"Error:           {error_msg}")
        print("=" * 60)

        print()
        input("Press ENTER to close the browser and exit...")
        try:
            driver.quit()
            print("  → browser closed")
        except Exception as e:
            print(f"  → warning: {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()