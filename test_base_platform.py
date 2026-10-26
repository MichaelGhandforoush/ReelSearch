import pytest
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
)

from src.base_platform import LoginRequired, Platform, write_cookie_file
from src.tiktok import TikTok


class FakePlatform(Platform):
    def __init__(self, logged_in_after):
        super().__init__("alice", "instagram", profile_path=".")
        self.checks = 0
        self.logged_in_after = logged_in_after
        self.prepared = False

    def open_login(self):
        self.driver = object()

    def is_logged_in(self):
        self.checks += 1
        return (
            self.logged_in_after is not None
            and self.checks > self.logged_in_after
        )

    def prepare_saved_page(self):
        self.prepared = True

    def get_saved_url(self):
        return ""

    def get_account_url(self):
        return ""

    def is_video_url(self, url):
        return True


def test_open_saved_page_waits_for_the_login_then_prepares(monkeypatch):
    monkeypatch.setattr("builtins.input", _no_input)
    platform = FakePlatform(logged_in_after=2)
    platform.login_poll_seconds = 0

    platform.open_saved_page()

    assert platform.checks == 3
    assert platform.prepared


def test_open_saved_page_raises_login_required_without_a_session(
    monkeypatch
):
    monkeypatch.setattr("builtins.input", _no_input)
    platform = FakePlatform(logged_in_after=None)
    platform.login_poll_seconds = 0

    with pytest.raises(LoginRequired):
        platform.open_saved_page(wait_seconds=0.05)

    assert not platform.prepared


def _no_input(*args):
    raise AssertionError("Syncing must not wait for the console.")


class FakeElement:
    def __init__(self, on_click=None):
        self.stale = False
        self.clicks = 0
        self.on_click = on_click

    def is_displayed(self):
        return True

    def is_enabled(self):
        if self.stale:
            raise StaleElementReferenceException()
        return True

    def click(self):
        self.clicks += 1
        if self.on_click:
            self.on_click()


class FakeTikTokDriver:
    """A profile page whose Favorites tab swaps the grid's video links."""

    def __init__(self, has_tab=True, favorites=1):
        self.links = [FakeElement()]
        self.tab = FakeElement(self.show_favorites) if has_tab else None
        self.favorites = favorites

    def show_favorites(self):
        for link in self.links:
            link.stale = True
        self.links = [FakeElement() for _ in range(self.favorites)]

    def find_element(self, by, selector):
        found = self.find_elements(by, selector)
        if not found:
            raise NoSuchElementException(selector)
        return found[0]

    def find_elements(self, by, selector):
        if selector == TikTok.favorites_tab_selector:
            return [self.tab] if self.tab else []
        if selector == TikTok.video_link_selector:
            return list(self.links)
        return []


def make_tiktok(driver):
    platform = TikTok("alice", profile_path=".")
    platform.driver = driver
    platform.favorites_wait_seconds = 0.2
    return platform


def test_tiktok_opens_the_favorites_tab():
    driver = FakeTikTokDriver()
    old_link = driver.links[0]

    make_tiktok(driver).prepare_saved_page()

    assert driver.tab.clicks == 1
    assert old_link.stale
    assert driver.links and not driver.links[0].stale


def test_tiktok_empty_favorites_are_not_an_error():
    driver = FakeTikTokDriver(favorites=0)

    make_tiktok(driver).prepare_saved_page()

    assert driver.tab.clicks == 1


def test_tiktok_without_a_favorites_tab_explains_the_layout_changed():
    with pytest.raises(RuntimeError, match="Favorites tab"):
        make_tiktok(FakeTikTokDriver(has_tab=False)).prepare_saved_page()


class CookieDriver:
    def __init__(self, cookies):
        self.cookies = cookies

    def execute_cdp_cmd(self, command, params):
        assert command == "Network.getAllCookies"
        return {"cookies": self.cookies}


def test_write_cookie_file_keeps_only_the_platform_cookies(tmp_path):
    driver = CookieDriver([
        {"name": "sessionid", "value": "abc", "domain": ".instagram.com",
         "path": "/", "secure": True, "expires": 1900000000.5},
        {"name": "csrftoken", "value": "tok", "domain": "www.instagram.com",
         "path": "/", "secure": False, "expires": -1, "session": True},
        {"name": "sid", "value": "x", "domain": ".google.com", "path": "/"},
        {"name": "fake", "value": "y", "domain": "notinstagram.com"},
    ])
    path = tmp_path / "cookies" / "aa11.txt"

    assert write_cookie_file(driver, str(path), "instagram.com") == 2

    assert path.read_text(encoding="utf-8").splitlines() == [
        "# Netscape HTTP Cookie File",
        ".instagram.com\tTRUE\t/\tTRUE\t1900000000\tsessionid\tabc",
        "www.instagram.com\tFALSE\t/\tFALSE\t0\tcsrftoken\ttok",
    ]
    assert not (tmp_path / "cookies" / "aa11.txt.tmp").exists()


def test_written_cookie_file_loads_in_yt_dlp(tmp_path):
    from yt_dlp.cookies import YoutubeDLCookieJar

    driver = CookieDriver([
        {"name": "sessionid", "value": "abc", "domain": ".tiktok.com",
         "path": "/", "secure": True, "expires": -1, "session": True},
    ])
    path = tmp_path / "tiktok.txt"
    write_cookie_file(driver, str(path), "tiktok.com")

    jar = YoutubeDLCookieJar(str(path))
    jar.load()

    assert [(cookie.domain, cookie.name, cookie.value) for cookie in jar] == [
        (".tiktok.com", "sessionid", "abc"),
    ]
