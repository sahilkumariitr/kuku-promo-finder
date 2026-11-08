"""Visit the deployed web app in a real (headless) browser so Streamlit Community Cloud keeps it awake.

Community Cloud puts an app to sleep after 12 hours without a visit, and a plain HTTP request doesn't count:
only a browser that runs the page's JavaScript opens the app's session. If the app is already asleep, this
clicks "Yes, get this app back up!" and waits until the app has loaded. It exits with an error if the app
doesn't load, so a failed scheduled run warns the owner by email.

    python .github/scripts/keep_awake.py https://kuku-promo-finder-23119038.streamlit.app/
"""
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

WAKE_BUTTON = re.compile(r"get this app back up", re.I)
APP_READY = "Find promos"  # the app's main button; only present once the app itself has loaded
TIMEOUT_S = 300


def frames_with(page, find):
    for frame in page.frames:
        try:
            loc = find(frame)
            if loc.count():
                yield loc.first
        except Exception:  # a frame can detach while the app (re)loads
            continue


def main(url: str) -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("CHROME_PATH") or None)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        woke = False
        deadline = time.time() + TIMEOUT_S
        while time.time() < deadline:
            for button in frames_with(page, lambda f: f.get_by_role("button", name=WAKE_BUTTON)):
                try:
                    button.click(timeout=10_000)
                except Exception:  # the page changed under us; look again on the next pass
                    break
                woke = True
                print("App was asleep: clicked the wake-up button.")
                break
            if any(True for _ in frames_with(page, lambda f: f.get_by_text(APP_READY))):
                page.wait_for_timeout(15_000)  # stay briefly so the visit registers as traffic
                print("App is up" + (" (woken from sleep)." if woke else "."))
                browser.close()
                return 0
            page.wait_for_timeout(5_000)
        page.screenshot(path="keep-awake-failure.png", full_page=True)
        print(f"App did not load within {TIMEOUT_S // 60} minutes; see the keep-awake-failure screenshot.")
        browser.close()
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
