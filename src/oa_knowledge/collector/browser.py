from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import time
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from oa_knowledge.config import Settings
from oa_knowledge.collector.credentials import load_chrome_saved_credential


class LoginState(StrEnum):
    AUTHENTICATED = "authenticated"
    AUTH_REQUIRED = "auth_required"
    UNKNOWN = "unknown"


@dataclass
class BrowserSession:
    settings: Settings
    headed: bool = False
    _playwright: Playwright | None = None
    context: BrowserContext | None = None
    page: Page | None = None

    def __enter__(self) -> "BrowserSession":
        self._playwright = sync_playwright().start()
        profile = self.settings.browser_profile_path
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        configured_port = urlsplit(self.settings.browser.base_url).port
        browser_args = [f"--explicitly-allowed-ports={configured_port}"] if configured_port else []
        self.context = self._playwright.chromium.launch_persistent_context(
            str(profile),
            executable_path=str(self.settings.browser.executable_path),
            headless=not self.headed,
            # Chrome suppresses password-manager autofill when it detects the
            # automation switch. Playwright still controls the browser through
            # DevTools without this visible command-line marker.
            ignore_default_args=["--enable-automation"],
            args=browser_args,
            ignore_https_errors=self.settings.browser.ignore_https_errors,
            accept_downloads=True,
        )
        self.context.set_default_timeout(self.settings.browser.navigation_timeout_seconds * 1000)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self

    def __exit__(self, *_args) -> None:
        if self.context:
            self.context.close()
        if self._playwright:
            self._playwright.stop()

    @property
    def base_url(self) -> str:
        return self.settings.browser.base_url.rstrip("/")

    def goto_home(self) -> LoginState:
        assert self.page
        self.page.goto(f"{self.base_url}{self.settings.browser.context_path}/main.do?method=main", wait_until="domcontentloaded")
        return self.login_state()

    def login_state(self) -> LoginState:
        assert self.page
        url = self.page.url
        login_controls = self.page.locator("#login_username, #login_password1, #login_button")
        if "/seeyon/index.jsp" in url or login_controls.count() >= 2:
            return LoginState.AUTH_REQUIRED
        if "/seeyon/main.do" in url and self.page.get_by_text("个人空间", exact=True).count() > 0:
            return LoginState.AUTHENTICATED
        return LoginState.UNKNOWN

    def login_with_saved_credentials(self, wait_seconds: int = 300) -> LoginState:
        assert self.page
        state = self.goto_home()
        # The legacy OA redirects main.do to its login form after
        # DOMContentLoaded, so the first observation can legitimately be
        # UNKNOWN. Wait for that redirect before deciding whether to submit.
        settle_deadline = time.monotonic() + min(10, wait_seconds)
        while state == LoginState.UNKNOWN and time.monotonic() < settle_deadline:
            self.page.wait_for_timeout(250)
            state = self.login_state()
        if state == LoginState.AUTH_REQUIRED:
            self._submit_saved_credentials()
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            state = self.login_state()
            if state == LoginState.AUTHENTICATED:
                return state
            self.page.wait_for_timeout(500)
        return self.login_state()

    def _submit_saved_credentials(self, autofill_wait_seconds: int = 12) -> bool:
        """Ask Chrome to autofill and submit without exposing credential values."""
        assert self.page
        username = self.page.locator("#login_username")
        password = self.page.locator("#login_password1")
        button = self.page.locator("#login_button")
        if not username.count() or not password.count() or not button.count():
            return False

        # Autofill is asynchronous and sometimes starts only after the login
        # control receives a real focus/click sequence.
        username.click()
        username.focus()
        self.page.keyboard.press("Tab")
        deadline = time.monotonic() + autofill_wait_seconds
        while time.monotonic() < deadline:
            fields_present = self.page.evaluate(
                """() => Boolean(
                    document.querySelector('#login_username')?.value &&
                    document.querySelector('#login_password1')?.value
                )"""
            )
            if fields_present:
                button.click()
                return True
            self.page.wait_for_timeout(250)
        credential_profiles = [self.settings.browser_profile_path]
        external_profile = self.settings.browser.credential_profile_path
        if external_profile and external_profile not in credential_profiles:
            credential_profiles.append(external_profile)
        for profile in credential_profiles:
            try:
                credential = load_chrome_saved_credential(profile, self.base_url)
            except ValueError:
                continue
            if credential:
                username.fill(credential.username)
                password.fill(credential.password)
                button.click()
                return True
        return False
