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
