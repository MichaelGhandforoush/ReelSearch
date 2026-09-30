import json

from src.base_platform import Platform


class FakePlatform(Platform):
    def __init__(self, urls_path):
        super().__init__(
            "test",
            "test",
            profile_path=".",
            urls_path=str(urls_path),
        )

    def get_saved_url(self):
        return ""

    def get_account_url(self):
        return ""

    def is_video_url(self, url):
        return "/video/" in url


class FakeDriver:
    """Simulates an infinite-scroll page that reveals one page per scroll."""

    def __init__(self, pages):
        self.pages = pages
        self.revealed = 1
        self.scroll_count = 0
        self.seen = set()
        self.cdp_commands = []

    def _hrefs(self):
        return [href for page in self.pages[:self.revealed] for href in page]

    def execute_script(self, script):
        if "__reelsearchSeen" in script:
            fresh = [href for href in self._hrefs() if href not in self.seen]
            self.seen.update(fresh)
            return fresh
        if "getElementsByTagName" in script:
            return [1000 * self.revealed, len(self._hrefs())]
        if "scrollTo" in script:
            self.scroll_count += 1
            self.revealed = min(self.revealed + 1, len(self.pages))
        return None

    def execute_cdp_cmd(self, command, params):
        self.cdp_commands.append((command, params))


def make_platform(tmp_path, monkeypatch, pages, known=None):
    urls_path = tmp_path / "urls.json"
    if known is not None:
        urls_path.write_text(json.dumps(known), encoding="utf-8")
    platform = FakePlatform(urls_path)
    platform.driver = FakeDriver(pages)
    platform.scroll_settle_seconds = 0.05
    monkeypatch.setattr("src.base_platform.time.sleep", lambda _: None)
    return platform, urls_path


def test_scrape_stops_and_saves_urls_when_pause_requested(
    tmp_path, monkeypatch
):
    platform, urls_path = make_platform(tmp_path, monkeypatch, [[
        "https://example.com/video/1",
        "https://example.com/video/2",
    ]])
    pause_requested = False

    def on_url_found(url):
        nonlocal pause_requested
        assert url == "https://example.com/video/1"
        pause_requested = True

    urls = platform.scrape_videos(
        on_url_found=on_url_found,
        should_stop=lambda: pause_requested,
    )

    assert urls == {"https://example.com/video/1"}
    assert json.loads(urls_path.read_text(encoding="utf-8")) == [
        "https://example.com/video/1"
    ]
    assert platform.driver.scroll_count == 0


def test_scrape_reopens_a_lost_browser_and_carries_on(tmp_path, monkeypatch):
    import urllib3

    pages = [
        [f"https://example.com/video/{page}-{item}" for item in range(3)]
        for page in range(4)
    ]
    platform, _ = make_platform(tmp_path, monkeypatch, pages)
    broken = platform.driver
    original = broken.execute_script
    calls = {"count": 0}

    def flaky(script):
        calls["count"] += 1
        if calls["count"] == 4:
            raise urllib3.exceptions.ProtocolError(
                "Connection aborted.", ConnectionResetError(10054)
            )
        return original(script)

    broken.execute_script = flaky
    broken.quit = lambda: None
    fresh = FakeDriver(pages)
    monkeypatch.setattr(
        "src.base_platform.webdriver.Chrome", lambda options: fresh
    )
    fresh.get = lambda url: None

    urls = platform.scrape_videos()

    assert platform.driver is fresh
    assert urls == {href for page in pages for href in page}


def test_scrape_gives_up_after_too_many_browser_restarts(
    tmp_path, monkeypatch
):
    import pytest
    import urllib3

    platform, _ = make_platform(tmp_path, monkeypatch, [["x"]])

    def dead_driver():
        driver = FakeDriver([["x"]])

        def fail(script):
            raise urllib3.exceptions.ProtocolError("Connection aborted.")

        driver.execute_script = fail
        driver.get = lambda url: None
        driver.quit = lambda: None
        return driver

    platform.driver = dead_driver()
    monkeypatch.setattr(
        "src.base_platform.webdriver.Chrome", lambda options: dead_driver()
    )

    with pytest.raises(urllib3.exceptions.ProtocolError):
        platform.scrape_videos()


def test_scrape_does_not_restart_for_page_errors(tmp_path, monkeypatch):
    import pytest

    platform, _ = make_platform(tmp_path, monkeypatch, [["x"]])

    def fail(script):
        raise ValueError("unexpected page")

    platform.driver.execute_script = fail
    monkeypatch.setattr(
        "src.base_platform.webdriver.Chrome",
        lambda options: pytest.fail("should not reopen the browser"),
    )

    with pytest.raises(ValueError):
        platform.scrape_videos()


def test_scrape_scrolls_past_already_known_urls(tmp_path, monkeypatch):
    pages = [
        [f"https://example.com/video/{page}-{item}" for item in range(4)]
        for page in range(10)
    ]
    # The first eight pages are known from an earlier sync; the old stop
    # rule gave up after five scrolls without anything new.
    known = [href for page in pages[:8] for href in page]
    platform, _ = make_platform(tmp_path, monkeypatch, pages, known)
    found = []

    urls = platform.scrape_videos(on_url_found=found.append)

    assert urls == {href for page in pages for href in page}
    assert found == pages[8] + pages[9]


def test_scrape_new_only_stops_at_already_known_urls(tmp_path, monkeypatch):
    pages = [
        [f"https://example.com/video/{page}-{item}" for item in range(4)]
        for page in range(10)
    ]
    # Newest first: the first two pages were saved since the last sync.
    known = [href for page in pages[2:] for href in page]
    platform, _ = make_platform(tmp_path, monkeypatch, pages, known)
    found = []

    platform.scrape_videos(on_url_found=found.append, new_only=True)

    assert found == pages[0] + pages[1]
    # Stopped after known_links_to_stop (12) known links: three more pages.
    assert platform.driver.scroll_count == 4


def test_scrape_new_only_carries_on_past_a_few_known_urls(
    tmp_path, monkeypatch
):
    pages = [
        [f"https://example.com/video/{page}-{item}" for item in range(4)]
        for page in range(6)
    ]
    # A short run of known links (fewer than known_links_to_stop) among new
    # ones, as with a re-saved or pinned post.
    known = pages[1]
    platform, _ = make_platform(tmp_path, monkeypatch, pages, known)
    found = []

    platform.scrape_videos(on_url_found=found.append, new_only=True)

    assert found == [
        href for index, page in enumerate(pages) if index != 1
        for href in page
    ]


def test_scrape_new_only_reads_everything_on_a_first_sync(
    tmp_path, monkeypatch
):
    pages = [[f"https://example.com/video/{page}"] for page in range(20)]
    platform, _ = make_platform(tmp_path, monkeypatch, pages)

    urls = platform.scrape_videos(new_only=True)

    assert len(urls) == 20


def test_account_settings_default_and_update(tmp_path, monkeypatch):
    import pytest
    from src import account_store

    monkeypatch.setattr(
        account_store, "ACCOUNTS_PATH", str(tmp_path / "accounts.json")
    )
    monkeypatch.setattr(account_store, "ROOT", str(tmp_path))
    account = account_store.create_account("instagram", "someone")

    assert account_store.account_settings(account) == (
        account_store.SETTING_DEFAULTS
    )
    account_store.update_settings(
        account["id"], sync_mode="new", use_comments=False, skip_backlog=True
    )
    saved = account_store.account_settings(
        account_store.get_account(account["id"])
    )
    assert saved["sync_mode"] == "new"
    assert saved["use_comments"] is False
    assert saved["skip_backlog"] is True
    assert saved["use_transcript"] is True
    with pytest.raises(ValueError):
        account_store.update_settings(account["id"], sync_mode="sometimes")


def test_scrape_has_no_scroll_cap_and_stops_at_the_end(tmp_path, monkeypatch):
    pages = [[f"https://example.com/video/{page}"] for page in range(150)]
    platform, _ = make_platform(tmp_path, monkeypatch, pages)

    urls = platform.scrape_videos()

    assert len(urls) == 150
    # The last page is followed by max_stalled_scrolls scrolls without growth.
    assert platform.driver.scroll_count == 149 + platform.max_stalled_scrolls


def test_scrape_blocks_media_while_scrolling_then_unblocks(
    tmp_path, monkeypatch
):
    platform, _ = make_platform(
        tmp_path, monkeypatch, [["https://example.com/video/1"]]
    )

    platform.scrape_videos()

    blocked = [
        params["urls"] for command, params in platform.driver.cdp_commands
        if command == "Network.setBlockedURLs"
    ]
    assert blocked[0] == platform.blocked_media
    assert blocked[-1] == []
