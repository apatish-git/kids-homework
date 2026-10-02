"""Ministry of Education SSO (lgn.edu.gov.il) login helper.

The form has #userName / #password inside #idCardForm. The page is protected by reCAPTCHA
and sometimes asks for a one-time code; when that happens we don't try to get around it —
the sync reports `needs_user` and the parent completes the login in a visible browser window.
"""
import time
from typing import Callable

from playwright.sync_api import Page

MOE_HOST = "lgn.edu.gov.il"
ERROR_HINTS = ("שגוי", "אינם תקינים", "אינו תקין", "נחסם", "נעול")
last_error = ""   # the site's own error line from the last rejected login (single worker thread)
typed_credentials = False   # whether the last complete_login typed this child's details (vs. reusing a session)


class LoginResult:
    OK = "ok"
    NEEDS_USER = "needs_user"
    BAD_CREDENTIALS = "bad_credentials"


class StopLogin(Exception):
    """Raised by a source's is_logged_in to abort the login immediately (not swallowed below)."""


def _safe(fn, default=False):
    try:
        return fn()
    except StopLogin:
        raise
    except Exception:
        return default


def type_into(locator, value: str):
    """Click and type like a person. Both login forms need this: MoE's fields are readonly until
    focused, and Webtop's Angular inputs only update their form model on real keystrokes."""
    locator.focus()   # focus, not click: Webtop's floating label sits on top of the input
    locator.evaluate("e => e.removeAttribute('readonly')")
    locator.press("Control+a")
    locator.press("Delete")
    locator.press_sequentially(value, delay=35)


def _wait_for(page: Page, cond: Callable[[], bool], seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if _safe(cond):
            return True
        page.wait_for_timeout(700)
    return False


def complete_login(page: Page, username: str, password: str, is_logged_in: Callable[[Page], bool],
                   interactive: bool, auto_timeout: int = 45, user_timeout: int = 300) -> str:
    """Call after the source site has been sent to the MoE login (or may already be logged in)."""
    global typed_credentials
    typed_credentials = False
    on_moe = lambda: MOE_HOST in page.url
    done = lambda: is_logged_in(page)

    if not _wait_for(page, lambda: done() or on_moe(), 40):
        return LoginResult.NEEDS_USER
    if done():
        return LoginResult.OK

    # MoE login is single sign-on: if this profile already logged in (e.g. to another site a
    # moment ago), the MoE page just bounces back with no form. Give that redirect time to
    # happen before deciding we need to type anything.
    user_box = page.locator("#userName")
    pw_tab = page.get_by_text("קוד משתמש וסיסמה").first
    _wait_for(page, lambda: done() or user_box.is_visible() or pw_tab.is_visible(), 20)
    if done():
        return LoginResult.OK

    # Make sure we're on the "user code + password" tab, not the one-time-code tab.
    if not _safe(lambda: user_box.is_visible()):
        _safe(lambda: page.get_by_text("קוד משתמש וסיסמה").first.click())
        _safe(lambda: user_box.wait_for(state="visible", timeout=8000))

    if username and password and _safe(lambda: user_box.is_visible()):
        type_into(user_box, username)
        type_into(page.locator("#password"), password)
        page.locator("#idCardForm button[type=submit]").first.click()   # submit exactly once
        typed_credentials = True
        if _wait_for(page, done, auto_timeout):
            return LoginResult.OK
        if on_moe():
            text = _safe(lambda: page.inner_text("body"), "")
            if any(h in text for h in ERROR_HINTS) and _safe(lambda: page.locator("#password").is_visible()):
                global last_error
                last_error = next((ln.strip() for ln in text.splitlines() if any(h in ln for h in ERROR_HINTS)), "")
                if not interactive:
                    return LoginResult.BAD_CREDENTIALS

    if interactive:
        # Visible window: let the parent finish (SMS code / captcha / wrong password).
        if _wait_for(page, done, user_timeout):
            return LoginResult.OK
    return LoginResult.NEEDS_USER
