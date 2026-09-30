from abc import ABC, abstractmethod
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    WebDriverException,
)
import urllib3

import time
import json
import os


class Platform(ABC):
    abbreviation = ""

    def __init__(
        self, username, platform, profile_path, account_id=None,
        urls_path=None
    ):
        self.username = username
        self.platform = platform.lower()
        self.account_id = account_id
        self.driver = None
        self.urls_path = urls_path or (
            f"{self.platform}_urls.json"
            if account_id is None
            else f"{self.platform}_{account_id}_urls.json"
        )
        self.options = Options()
        self.options.add_argument(
            f"--user-data-dir={profile_path}"
        )

    def log_in(self):
        self.driver = webdriver.Chrome(options=self.options)
        self.driver.get(self.get_saved_url())
        input("Press Enter after login has completed...")

    # The site and cookie that show a browser is logged in. Set per platform.
    cookie_domain = ""
    session_cookie = "sessionid"

    def open_login(self):
        """Open the browser on the saved page and return straight away.

        Unlike log_in this does not wait for anyone; is_logged_in says when
        the login has finished.
        """
        self.driver = webdriver.Chrome(options=self.options)
        self.driver.get(self.get_saved_url())

    def is_logged_in(self):
        """True once the browser holds a login session for this platform.

        Reads every cookie in the browser, not just the current page's, so
        it still works while a sign-in pop-up or another site is showing.
        """
        cookies = self.driver.execute_cdp_cmd(
            "Network.getAllCookies", {}
        ).get("cookies", [])
        return any(
            cookie.get("name") == self.session_cookie
            and cookie.get("value")
            and cookie.get("domain", "").lstrip(".").endswith(self.cookie_domain)
            for cookie in cookies
        )


    @abstractmethod
    def get_saved_url(self):
        """Return the URL containing the user's saved content."""
        pass

    @abstractmethod
    def get_account_url(self):
        """Return the public profile URL for this account."""
        pass

    @abstractmethod
    def is_video_url(self, url):
        """Return True if the URL points to relevant video content."""
        pass

    def load_urls(self):
        if os.path.exists(self.urls_path):
            with open(self.urls_path, "r") as f:
                return set(json.load(f))

        return set()

    def save_urls(self, urls):
        with open(self.urls_path, "w") as f:
            json.dump(list(urls), f, indent=2)

    # How long to wait for the page to load more after a scroll, how often
    # to check, and how many consecutive waits without growth mean the end
    # has been reached.
    scroll_settle_seconds = 8
    scroll_poll_seconds = 0.2
    max_stalled_scrolls = 3

    # When only new links are wanted: how many already-known links in a row
    # mean the rest of the page was collected by an earlier sync. Saved
    # pages list the newest first, so new links all come before these; a few
    # rows' worth allows for pinned or reordered posts among them.
    known_links_to_stop = 12

    # Thumbnails and preview videos are the bulk of what the saved grid
    # downloads, and only the links are needed.
    blocked_media = [
        "*.jpg*", "*.jpeg*", "*.png*", "*.webp*", "*.gif*", "*.heic*",
        "*.avif*", "*.mp4*", "*.m4s*", "*.webm*",
    ]

    # How often scraping may reopen a browser that stopped responding before
    # the sync gives up on collecting links.
    max_browser_restarts = 3

    @staticmethod
    def is_browser_lost(error):
        """True if Selenium lost the browser, as opposed to a page problem.

        Selenium talks to Chrome over a local connection; when Chrome is
        starved of CPU, crashes, or closes a tab, that connection is reset
        and every later command fails the same way.
        """
        if isinstance(error, (
            urllib3.exceptions.HTTPError,
            ConnectionError,
            InvalidSessionIdException,
            NoSuchWindowException,
        )):
            return True
        text = str(error).lower()
        return isinstance(error, WebDriverException) and any(
            word in text for word in (
                "not reachable", "disconnected", "session deleted",
                "tab crashed", "target window already closed",
            )
        )

    def restart_browser(self, attempts=3):
        """Close the browser and open a fresh one on the saved page.

        The profile keeps the login, so this does not ask to log in again.
        """
        try:
            if self.driver is not None:
                self.driver.quit()
        except Exception:
            pass
        self.driver = None
        for attempt in range(1, attempts + 1):
            # Gives the old Chrome time to exit and release the profile.
            time.sleep(2 * attempt)
            try:
                self.driver = webdriver.Chrome(options=self.options)
                self.driver.get(self.get_saved_url())
                return
            except Exception:
                if self.driver is not None:
                    try:
                        self.driver.quit()
                    except Exception:
                        pass
                    self.driver = None
                if attempt == attempts:
                    raise

    def _block_media(self, blocked=True):
        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
            self.driver.execute_cdp_cmd(
                "Network.setBlockedURLs",
                {"urls": self.blocked_media if blocked else []},
            )
        except Exception as error:
            # Not fatal: scraping still works, just downloads more.
            print(f"Could not block media while scraping: {error}")

    def _new_page_hrefs(self):
        """Return links the page has not reported before, in one round-trip.

        The page remembers what it already returned, so each call only
        transfers links that appeared since the previous call.
        """
        return self.driver.execute_script(
            "const seen = window.__reelsearchSeen"
            " || (window.__reelsearchSeen = new Set());"
            "const fresh = [];"
            "for (const a of document.querySelectorAll('a[href]')) {"
            "  if (!seen.has(a.href)) { seen.add(a.href); fresh.push(a.href); }"
            "}"
            "return fresh;"
        ) or []

    def _page_size(self):
        """Cheap fingerprint of how much content is loaded."""
        return tuple(self.driver.execute_script(
            "return [document.documentElement.scrollHeight,"
            " document.getElementsByTagName('a').length];"
        ) or ())

    def _scroll_to_bottom(self, nudge=False):
        if nudge:
            # Scrolling up and back down re-triggers lazy loaders that only
            # fire when the sentinel element re-enters the viewport.
            self.driver.execute_script(
                "window.scrollBy(0, -Math.round(window.innerHeight * 1.5));"
            )
            time.sleep(self.scroll_poll_seconds)
        self.driver.execute_script(
            "window.scrollTo(0, document.documentElement.scrollHeight);"
        )

    def scrape_videos(
        self,
        max_scrolls=None,
        start=lambda: None,
        on_url_found=None,
        should_stop=None,
        new_only=False,
    ):
        """Scroll the saved page, collecting video URLs.

        By default it scrolls to the end: progress is measured by the page
        itself growing (new links or a taller page), not by finding URLs
        missing from the saved file, so re-syncs scroll past already-known
        videos and pick up any an earlier sync missed.

        new_only: stop once known_links_to_stop already-known links appear
        in a row, since everything past them was collected before.
        """
        start()
        stop = should_stop or (lambda: False)
        urls = self.load_urls()
        seen_on_page = set()

        print(f"Starting with {len(urls)} existing URLs")
        stalled = 0
        scrolls = 0
        known_in_a_row = 0
        self._block_media()

        restarts = 0

        try:
            while max_scrolls is None or scrolls < max_scrolls:
                if stop():
                    break

                try:
                    found_new = False
                    reached_known = False
                    for href in self._new_page_hrefs():
                        if href in seen_on_page or not self.is_video_url(href):
                            continue
                        seen_on_page.add(href)
                        if href in urls:
                            known_in_a_row += 1
                            if (
                                new_only
                                and known_in_a_row >= self.known_links_to_stop
                            ):
                                reached_known = True
                                break
                            continue
                        known_in_a_row = 0
                        urls.add(href)
                        found_new = True
                        if on_url_found is not None:
                            on_url_found(href)
                        # Checking only after new URLs keeps this cheap on
                        # long pages while still reacting to a pause
                        # promptly.
                        if stop():
                            break

                    if found_new:
                        self.save_urls(urls)
                        print(f"{len(urls)} URLs collected")

                    if reached_known:
                        print(
                            "Reached videos collected by an earlier sync; "
                            "stopping because only new videos are checked"
                        )
                        break

                    if stop():
                        break

                    size_before = self._page_size()
                    self._scroll_to_bottom(nudge=stalled > 0)
                    scrolls += 1

                    if self._wait_for_growth(size_before, stop):
                        stalled = 0
                    else:
                        stalled += 1
                        if stalled >= self.max_stalled_scrolls:
                            print("Reached the end of the saved videos")
                            break
                except Exception as error:
                    if (
                        not self.is_browser_lost(error)
                        or restarts >= self.max_browser_restarts
                        or stop()
                    ):
                        raise
                    restarts += 1
                    print(
                        f"Lost the connection to the browser "
                        f"({type(error).__name__}: {error}); reopening it and "
                        f"carrying on (restart {restarts} of "
                        f"{self.max_browser_restarts})."
                    )
                    # The fresh page starts at the top again; links already
                    # collected are skipped, so it just scrolls past them.
                    self.restart_browser()
                    self._block_media()
                    stalled = 0
        finally:
            self._block_media(False)

        return urls

    def _wait_for_growth(self, size_before, stop):
        """Wait until the page loads more content after a scroll."""
        deadline = time.monotonic() + self.scroll_settle_seconds
        while time.monotonic() < deadline:
            time.sleep(self.scroll_poll_seconds)
            if stop():
                return True
            if self._page_size() != size_before:
                return True
        return False
