import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.service import Service

from webdriver_manager.chrome import ChromeDriverManager


def generate_strong_password(length=16):
    import secrets
    import string

    characters = (
        string.ascii_letters
        + string.digits
        + "!@#$%^&*"
    )

    return "".join(
        secrets.choice(characters)
        for _ in range(length)
    )


def main():

    # ---------------------------------------------------------
    # User Input
    # ---------------------------------------------------------

    user_email = input("Enter your email address: ").strip()

    if not user_email:
        print("Error: Email address cannot be empty.")
        return

    # ---------------------------------------------------------
    # Generate Password
    # ---------------------------------------------------------

    auto_password = generate_strong_password()

    print(f"Generated Password: {auto_password}")

    # ---------------------------------------------------------
    # Setup WebDriver
    # ---------------------------------------------------------

    driver = None

    try:
        service = Service(
            ChromeDriverManager().install()
        )

        driver = webdriver.Chrome(
            service=service
        )

        driver.maximize_window()

    except Exception as e:
        print(f"Error setting up WebDriver: {e}")
        return

    # ---------------------------------------------------------
    # Open Signup Page
    # ---------------------------------------------------------

    P_URL = "https://www.pella.app/signup"

    print(f"Navigating to: {P_URL}")

    try:
        driver.get(P_URL)

        wait = WebDriverWait(driver, 30)

        # -----------------------------------------------------
        # Locators
        # -----------------------------------------------------

        email_field_locator = (
            By.XPATH,
            "//input[@placeholder='Enter your email address']"
        )

        password_field_locator = (
            By.XPATH,
            "//input[@placeholder='Enter your password']"
        )

        continue_button_locator = (
            By.XPATH,
            "//button[normalize-space()='Continue']"
        )

        # Replace this with the actual CAPTCHA container
        # selector if you know it.
        captcha_locator = (
            By.ID,
            "captcha_widget"
        )

        otp_indicator_locator = (
            By.XPATH,
            "//*[contains(normalize-space(), 'OTP')]"
        )

        otp_field_locator = (
            By.ID,
            "otp_code"
        )

        verify_button_locator = (
            By.XPATH,
            "//button[contains(normalize-space(), 'Verify')]"
        )

        # -----------------------------------------------------
        # 1. Fill Email
        # -----------------------------------------------------

        print("Waiting for email field...")

        email_field = wait.until(
            EC.element_to_be_clickable(
                email_field_locator
            )
        )

        email_field.clear()
        email_field.send_keys(user_email)

        # -----------------------------------------------------
        # 2. Fill Password
        # -----------------------------------------------------

        print("Waiting for password field...")

        password_field = wait.until(
            EC.element_to_be_clickable(
                password_field_locator
            )
        )

        print("Entering auto-generated password...")

        password_field.clear()
        password_field.send_keys(auto_password)

        # -----------------------------------------------------
        # 3. Submit Initial Form
        # -----------------------------------------------------

        print("Waiting for Continue button...")

        continue_button = wait.until(
            EC.element_to_be_clickable(
                continue_button_locator
            )
        )

        print("Clicking Continue...")

        continue_button.click()

        # -----------------------------------------------------
        # 4. Check for CAPTCHA
        # -----------------------------------------------------

        captcha_detected = False

        try:
            print("Checking for CAPTCHA...")

            wait.until(
                EC.presence_of_element_located(
                    captcha_locator
                )
            )

            captcha_detected = True

        except TimeoutException:
            print("No CAPTCHA detected.")

        # -----------------------------------------------------
        # 5. Manual CAPTCHA Handling
        # -----------------------------------------------------

        if captcha_detected:

            print("\n" + "=" * 70)
            print("CAPTCHA DETECTED")
            print("=" * 70)
            print(
                "Please complete the CAPTCHA manually "
                "in the browser window."
            )
            print(
                "The script will continue after you "
                "press ENTER here."
            )
            print("=" * 70)

            input(
                "Press ENTER after completing the CAPTCHA: "
            )

            print("Continuing after manual CAPTCHA completion...")

        # -----------------------------------------------------
        # 6. Check for OTP
        # -----------------------------------------------------

        print("\nChecking for OTP verification...")

        try:

            wait.until(
                EC.presence_of_element_located(
                    otp_indicator_locator
                )
            )

            print(
                "OTP screen detected. "
                "Starting manual OTP input."
            )

            otp_field = wait.until(
                EC.element_to_be_clickable(
                    otp_field_locator
                )
            )

            print("\n" + "=" * 70)
            print("ACTION REQUIRED: OTP VERIFICATION")
            print("=" * 70)
            print(
                "Retrieve the OTP from your email."
            )
            print("=" * 70)

            otp_code = input(
                "Enter the OTP code: "
            ).strip()

            if not otp_code:
                print("No OTP entered.")
                return

            print("Entering OTP...")

            otp_field.clear()
            otp_field.send_keys(otp_code)

            verify_button = wait.until(
                EC.element_to_be_clickable(
                    verify_button_locator
                )
            )

            print("Clicking Verify...")

            verify_button.click()

            time.sleep(3)

            print("OTP submitted.")

        except TimeoutException:

            print(
                "No OTP screen detected. "
                "Continuing."
            )

        except Exception as e_otp:

            print(
                "An error occurred during OTP handling:"
            )
            print(e_otp)

        # -----------------------------------------------------
        # 7. Final Output
        # -----------------------------------------------------

        print("\n" + "=" * 70)
        print(
            "FORM SUBMISSION COMPLETE"
        )
        print("=" * 70)

        print("Login credentials:")
        print(f"EMAIL:    {user_email}")
        print(f"PASSWORD: {auto_password}")

        print("=" * 70)

        time.sleep(3)

    except Exception as e:

        print("\n" + "=" * 70)
        print("FATAL ERROR OCCURRED")
        print("=" * 70)
        print(f"Error details: {e}")
        print("=" * 70)

    finally:

        if driver is not None:

            try:
                driver.quit()
                print("Browser closed.")

            except Exception:
                pass


# -------------------------------------------------------------
# Program Entry Point
# -------------------------------------------------------------

if __name__ == "__main__":
    main()
