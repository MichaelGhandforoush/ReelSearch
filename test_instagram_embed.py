import json
import threading
import time

import pytest

from src import instagram_embed as ie

POST = "https://www.instagram.com/p/ABC123/"


def embed_page(media, context_type="GraphVideo"):
    """Instagram's embed page shape: the post as a JSON string in JSON."""
    context = json.dumps({
        "context": {"type": context_type},
        "gql_data": {"shortcode_media": media},
    })
    return (
        '<html><head></head><body><div class="EmbeddedMedia"></div>'
        f'<script>{{"require":[{{"contextJSON":{json.dumps(context)}}}]}}'
        "</script></body></html>"
    )


@pytest.fixture(autouse=True)
def empty_cache():
    ie._cache.clear()
    ie._in_flight.clear()
    yield
    ie._cache.clear()
    ie._in_flight.clear()


def test_parse_media_reads_video_poster_and_size():
    html = embed_page({
        "is_video": True,
        "video_url": "https://cdn.example/v.mp4?a=1&b=2",
        "display_url": "https://cdn.example/p.jpg",
        "dimensions": {"width": 720, "height": 1280},
    })

    assert ie.parse_media(html) == {
        "kind": "video",
        "video": "https://cdn.example/v.mp4?a=1&b=2",
        "poster": "https://cdn.example/p.jpg",
        "width": 720,
        "height": 1280,
    }


def test_parse_media_uses_first_video_of_a_carousel():
    html = embed_page({
        "display_url": "https://cdn.example/cover.jpg",
        "edge_sidecar_to_children": {"edges": [
            {"node": {"display_url": "https://cdn.example/photo.jpg"}},
            {"node": {
                "video_url": "https://cdn.example/clip.mp4",
                "display_url": "https://cdn.example/clip.jpg",
                "dimensions": {"width": 1080, "height": 1080},
            }},
        ]},
    })

    media = ie.parse_media(html)
    assert media["video"] == "https://cdn.example/clip.mp4"
    assert media["poster"] == "https://cdn.example/clip.jpg"


def test_parse_media_falls_back_to_the_photo_without_a_video():
    # Copyright-blocked reels come without a video link, as on Instagram.
    html = embed_page({"display_url": "https://cdn.example/p.jpg"})

    assert ie.parse_media(html)["kind"] == "image"


@pytest.mark.parametrize("html", [
    "<html>no data here</html>",
    '<script>{"contextJSON":"not json"}</script>',
    '<script>{"contextJSON":"{\\"gql_data\\":null}"}</script>',
])
def test_parse_media_returns_none_when_the_page_changed(html):
    assert ie.parse_media(html) is None


def test_player_page_escapes_links_and_fits_by_shape():
    portrait = ie.player_page({
        "kind": "video", "video": 'https://cdn.example/v.mp4?a=1&b="x"',
        "poster": "https://cdn.example/p.jpg", "width": 720, "height": 1280,
    })
    wide = ie.player_page({
        "kind": "video", "video": "https://cdn.example/v.mp4",
        "poster": "https://cdn.example/p.jpg", "width": 1280, "height": 720,
    })

    assert 'src="https://cdn.example/v.mp4?a=1&amp;b=&quot;x&quot;"' in portrait
    assert "object-fit: cover" in portrait
    assert "object-fit: contain" in wide
    # No Instagram script is loaded by the player.
    assert "cdninstagram.com/rsrc" not in portrait


def test_render_embed_serves_the_player_or_the_original(monkeypatch):
    html = embed_page({
        "video_url": "https://cdn.example/v.mp4",
        "display_url": "https://cdn.example/p.jpg",
    })
    monkeypatch.setattr(ie, "_fetch", lambda url: html)

    player, status = ie.render_embed(POST)
    original, _ = ie.render_embed(POST, original=True)

    assert status == 200
    assert "<video" in player and 'data-player' not in original
    assert "EmbeddedMedia" in original and "<base href" in original


def test_render_embed_falls_back_when_the_media_cannot_be_read(monkeypatch):
    monkeypatch.setattr(
        ie, "_fetch", lambda url: '<head></head><div class="EmbeddedMedia">'
    )

    html, status = ie.render_embed(POST)

    assert status == 200
    assert "<base href" in html


def test_concurrent_requests_for_a_post_fetch_it_once(monkeypatch):
    calls = []

    def slow_fetch(url):
        calls.append(url)
        time.sleep(0.1)
        return "page"

    monkeypatch.setattr(ie, "_fetch", slow_fetch)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(ie._embed_html(POST)))
        for _ in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == ["page"] * 5
    assert calls == [POST]


def test_a_failed_fetch_is_not_cached(monkeypatch):
    attempts = []

    def flaky(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise OSError("reset")
        return "page"

    monkeypatch.setattr(ie, "_fetch", flaky)

    with pytest.raises(OSError):
        ie._embed_html(POST)
    assert ie._embed_html(POST) == "page"


def test_warm_fetches_in_the_background_and_skips_cached(monkeypatch):
    fetched = threading.Event()
    calls = []

    def fetch(url):
        calls.append(url)
        fetched.set()
        return "page"

    monkeypatch.setattr(ie, "_fetch", fetch)
    ie.warm([POST, "https://example.com/not-instagram"])
    assert fetched.wait(2)
    deadline = time.monotonic() + 2
    while POST not in ie._cache and time.monotonic() < deadline:
        time.sleep(0.01)

    ie.warm([POST])
    time.sleep(0.05)
    assert calls == [POST]
