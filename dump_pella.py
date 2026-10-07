# dump_pella.py
import asyncio
from playwright.async_api import async_playwright

URL = "https://www.pella.app/signup"

async def dump(page, label):
    print(f"\n========== {label} ==========")
    print("URL:  ", page.url)
    print("TITLE:", await page.title())

    html = await page.content()
    with open(f"pella_{label}.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML saved: pella_{label}.html ({len(html)} chars)")

    await page.screenshot(path=f"pella_{label}.png", full_page=True)
    print(f"Screenshot: pella_{label}.png")

    print("\n--- INPUTS ---")
    for i, el in enumerate(await page.locator("input").all()):
        try:
            info = await el.evaluate("""el => ({
                type: el.type, id: el.id, name: el.name,
                placeholder: el.placeholder,
                autocomplete: el.autocomplete,
                inputmode: el.inputmode,
                visible: el.offsetParent !== null,
                required: el.required,
            })""")
            print(f"[{i}] {info}")
        except Exception as e:
            print(f"[{i}] error {e}")

    print("\n--- BUTTONS ---")
    for i, el in enumerate(
        await page.locator("button, [role='button'], input[type='submit']").all()
    ):
        try:
            text = (await el.inner_text()).strip()[:100].replace("\n", " ")
            info = await el.evaluate("""el => ({
                tag: el.tagName, type: el.type, id: el.id,
                cls: (el.className||'').toString().slice(0, 120),
                visible: el.offsetParent !== null,
            })""")
            print(f"[{i}] text={text!r} {info}")
        except Exception as e:
            print(f"[{i}] error {e}")

    print("\n--- LINKS ---")
    for i, el in enumerate(await page.locator("a").all()):
        try:
            text = (await el.inner_text()).strip()[:100].replace("\n", " ")
            href = await el.get_attribute("href")
            print(f"[{i}] text={text!r} href={href}")
        except Exception as e:
            print(f"[{i}] error {e}")


async def run(headless: bool, label: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle", timeout=20000)
        await dump(page, label)
        await browser.close()


async def main():
    # Visible browser, on your IP — closest to a real human visit
    await run(headless=False, label="visible")
    # Headless, on your IP — matches what Railway does, but from a clean IP
    await run(headless=True, label="headless")


if __name__ == "__main__":
    asyncio.run(main())