"""Serve an Instagram post as a bare video player.

Instagram's own /embed/ page is fetched here, and the post's video and
thumbnail are read out of it and served in a tiny page of our own: just a
<video> element. Instagram's page runs a large script, and because these
frames share the app's origin that script would run on the main page's own
thread, once per card, which is what made scrolling the grid stutter.

If a post cannot be read that way, Instagram's page is served instead, with
a style and script added so only the video shows.
"""

import json
import re
import threading
import time
import urllib.request
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from html import escape

# Only real post links are fetched, so this cannot be used to load other sites.
POST_URL = re.compile(
    r"^https://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)"
)
# Instagram's video links are signed and expire after a few days, so a page
# is kept for far less than that.
CACHE_SECONDS = 20 * 60
CACHE_LIMIT = 300
FETCH_TIMEOUT_SECONDS = 10
# Fetches ahead of the grid, for videos about to be shown. Few enough that
# Instagram does not start refusing them.
WARM_WORKERS = 4

_cache = OrderedDict()
_cache_lock = threading.Lock()
# One fetch per post at a time; later requests for it wait for that one.
_in_flight = {}
_warm_pool = ThreadPoolExecutor(
    max_workers=WARM_WORKERS, thread_name_prefix="instagram-warm"
)

EMBED_STYLE = """
html, body { height: 100%; margin: 0; overflow: hidden; background: #0e0d0d; }
/* Header, hover card, "view more" button, like row, and comment footer. */
.Header, .HoverCard, .PrimaryCTA, .Feedback, .SocialProof, .Footer,
.HeaderCta, .Glyph { display: none !important; }
/* No link may take the viewer to Instagram, including "watch again". */
a { pointer-events: none !important; cursor: default !important; }
a[href*="watch_again"] { display: none !important; }
/* Let the video fill the whole frame instead of a 4:5 box. */
.Content { position: fixed !important; inset: 0 !important; height: 100% !important;
           padding: 0 !important; }
.EmbeddedMedia, .EmbedVideo, .EmbeddedMediaImage { width: 100% !important;
           height: 100% !important; }
/* The video's wrapper is a padding-bottom 4:5 box; make it fill the frame. */
.EmbedVideo > * { padding-bottom: 0 !important; height: 100% !important; }
.EmbeddedMediaImage { object-fit: cover !important; }
html.wide .EmbeddedMediaImage { object-fit: contain !important; }
/* Instagram shows the thumbnail and the video's own poster at once, fitted
   differently, which looked like the video drawn on top of itself. Only the
   thumbnail shows until playback starts, then only the video. */
video:not([data-started]) { opacity: 0 !important; }
html.started .EmbeddedMediaImage { visibility: hidden !important; }
/* Dark letterboxing; only on layers behind the thumbnail, not over it. */
.Content, .EmbeddedMedia { background: #0e0d0d !important; }
video[data-started] { background: #0e0d0d !important; }
"""

# Runs before the page's own scripts. The page adds its video, and its
# "watch again on Instagram" tab, after loading, so this keeps watching.
EMBED_SCRIPT = """
(function () {
    // "play", "pause", "preview" (muted play), "stop" (pause and rewind) or
    // null; set by ?autoplay=1 or by the page embedding this one, and applied
    // to the video as soon as it exists.
    var wanted = %(autoplay)s ? "play" : null;
    function hide(element) {
        element.style.setProperty("display", "none", "important");
    }
    function aspect(video) {
        if (video && video.videoWidth) return video.videoWidth / video.videoHeight;
        var image = document.querySelector(".EmbeddedMediaImage");
        return image && image.naturalWidth ? image.naturalWidth / image.naturalHeight : 0;
    }
    function fit(video) {
        var ratio = aspect(video);
        if (!ratio) return;
        var portrait = ratio < 0.9;
        if (video) video.style.setProperty("object-fit", portrait ? "cover" : "contain", "important");
        document.documentElement.classList.toggle("wide", !portrait);
    }
    function play(video) {
        var start = video.play();
        if (start && start.catch) {
            // Browsers may refuse sound without a click; muted is allowed.
            start.catch(function () {
                video.muted = true;
                video.play().catch(function () {});
            });
        }
    }
    function apply(video) {
        if (wanted === "play") play(video);
        else if (wanted === "preview") { video.muted = true; play(video); }
        else if (wanted === "pause") video.pause();
        else if (wanted === "stop") {
            video.pause();
            try { video.currentTime = 0; } catch (error) {}
        }
    }
    function prepare(video) {
        if (video.dataset.prepared) return;
        video.dataset.prepared = "1";
        // A looping video never reaches the "watch again" screen.
        video.loop = true;
        video.preload = "metadata";
        video.setAttribute("playsinline", "");
        video.addEventListener("loadedmetadata", function () { fit(video); });
        video.addEventListener("playing", function () {
            video.dataset.started = "1";
            document.documentElement.classList.add("started");
        });
        fit(video);
        apply(video);
    }
    window.addEventListener("message", function (event) {
        var data = event.data;
        if (event.origin !== window.location.origin || !data || data["x-reelsearch"] !== true) return;
        if (["play", "pause", "preview", "stop"].indexOf(data.type) === -1) return;
        wanted = data.type;
        document.querySelectorAll("video").forEach(apply);
    });
    function sweep() {
        document.querySelectorAll("video").forEach(prepare);
        var image = document.querySelector(".EmbeddedMediaImage");
        if (image && !image.dataset.prepared) {
            image.dataset.prepared = "1";
            image.addEventListener("load", function () { fit(document.querySelector("video")); });
            fit(document.querySelector("video"));
        }
        // Any label naming Instagram (in whatever language the page uses).
        document.querySelectorAll("body a, body span, body div, body button").forEach(function (element) {
            var own = Array.prototype.filter.call(element.childNodes, function (node) {
                return node.nodeType === 3;
            }).map(function (node) { return node.textContent; }).join(" ");
            if (/instagram/i.test(own)) hide(element);
        });
    }
    // Instagram's page changes constantly while it loads and plays, so the
    // changes are gathered up and handled at most once per frame.
    var sweepQueued = false;
    function queueSweep() {
        if (sweepQueued) return;
        sweepQueued = true;
        requestAnimationFrame(function () { sweepQueued = false; sweep(); });
    }
    new MutationObserver(queueSweep).observe(document.documentElement, {
        childList: true, subtree: true, characterData: true
    });
    document.addEventListener("DOMContentLoaded", sweep);
})();
"""

UNAVAILABLE_PAGE = """<!doctype html>
<meta charset="utf-8">
<style>
html, body { height: 100%%; margin: 0; }
body { display: grid; place-items: center; background: #0e0d0d; color: #a29d96;
       font: 14px/1.4 system-ui, sans-serif; text-align: center; padding: 16px; }
</style>
<p>%s</p>
"""


def unavailable_page(message="Video unavailable"):
    return UNAVAILABLE_PAGE % escape(message)


def is_post_url(url):
    return bool(POST_URL.match(url or ""))


def _fetch(url):
    match = POST_URL.match(url)
    embed_url = f"{match.group(0)}/embed/"
    request = urllib.request.Request(embed_url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(
        request, timeout=FETCH_TIMEOUT_SECONDS
    ) as response:
        return response.read().decode("utf-8", "replace")


def _cached(url):
    """The cached page for a post if it is still fresh (lock held)."""
    cached = _cache.get(url)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        _cache.move_to_end(url)
        return cached[1]
    return None


def _embed_html(url):
    with _cache_lock:
        html = _cached(url)
        if html is not None:
            return html
        waiting = _in_flight.get(url)
        if waiting is None:
            waiting = _in_flight[url] = Future()
            owner = True
        else:
            owner = False
    if not owner:
        # A warm-up or another frame is already fetching this post.
        return waiting.result(timeout=FETCH_TIMEOUT_SECONDS * 2)
    try:
        html = _fetch(url)
    except Exception as error:
        with _cache_lock:
            _in_flight.pop(url, None)
        waiting.set_exception(error)
        raise
    with _cache_lock:
        _cache[url] = (time.monotonic(), html)
        _cache.move_to_end(url)
        while len(_cache) > CACHE_LIMIT:
            _cache.popitem(last=False)
        _in_flight.pop(url, None)
    waiting.set_result(html)
    return html


def warm(urls):
    """Fetch posts in the background so their frames load straight away."""
    for url in urls:
        if not is_post_url(url):
            continue
        with _cache_lock:
            if _cached(url) is not None or url in _in_flight:
                continue
        _warm_pool.submit(_warm_one, url)


def _warm_one(url):
    try:
        _embed_html(url)
    except Exception:
        # The frame will try again itself and report the problem.
        pass


_CONTEXT_KEY = '"contextJSON":'


def parse_media(html):
    """Read a post's video (or photo) out of Instagram's embed page.

    The page carries the post as JSON inside a string ("contextJSON").
    Returns None when that is missing or has changed shape, so the caller
    can fall back to Instagram's own page.
    """
    start = html.find(_CONTEXT_KEY)
    if start == -1:
        return None
    try:
        text, _ = json.JSONDecoder().raw_decode(html, start + len(_CONTEXT_KEY))
        media = json.loads(text)["gql_data"]["shortcode_media"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(media, dict):
        return None
    # A carousel shows its first video, or else its first photo.
    children = [
        edge.get("node") or {}
        for edge in (media.get("edge_sidecar_to_children") or {}).get(
            "edges", []
        )
    ]
    item = next(
        (child for child in children if child.get("video_url")),
        children[0] if children else media,
    )
    dimensions = item.get("dimensions") or media.get("dimensions") or {}
    poster = item.get("display_url") or media.get("display_url")
    video = item.get("video_url")
    if not poster and not video:
        return None
    return {
        "kind": "video" if video else "image",
        "video": video,
        "poster": poster,
        "width": dimensions.get("width"),
        "height": dimensions.get("height"),
    }


PLAYER_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer">
<style>
/* See-through until the thumbnail has arrived, so the card's own placeholder
   shows instead of an empty box, then the thumbnail fades in. */
html, body { height: 100%%; margin: 0; overflow: hidden; background: transparent; }
video, img { display: block; width: 100%%; height: 100%%; object-fit: %(fit)s;
             background: #0e0d0d; opacity: 0; transition: opacity 180ms ease; }
html.ready video, html.ready img { opacity: 1; }
</style></head><body>%(media)s
<script>
(function () {
    var root = document.documentElement;
    root.dataset.player = "1";
    function ready() {
        if (root.classList.contains("ready")) return;
        root.classList.add("ready");
        // Tells the grid it can stop showing this card's placeholder.
        try {
            window.parent.postMessage({ "x-reelsearch": true, type: "ready" }, location.origin);
        } catch (error) {}
    }
    var video = document.querySelector("video");
    if (!video) {
        var image = document.querySelector("img");
        if (image.complete) ready();
        else image.onload = image.onerror = ready;
        return;
    }
    // "play", "pause", "preview" (muted play), "stop" (pause and rewind) or
    // null; set by ?autoplay=1 or by the page embedding this one.
    var wanted = %(autoplay)s ? "play" : null;
    function play() {
        var start = video.play();
        if (start && start.catch) {
            // Browsers may refuse sound without a click; muted is allowed.
            start.catch(function () {
                video.muted = true;
                video.play().catch(function () {});
            });
        }
    }
    function apply() {
        if (wanted === "play") play();
        else if (wanted === "preview") { video.muted = true; play(); }
        else if (wanted === "pause") video.pause();
        else if (wanted === "stop") {
            video.pause();
            try { video.currentTime = 0; } catch (error) {}
        }
    }
    // Controls only in the full-screen viewer; in the grid the card itself
    // is the button that opens it.
    var frame = null;
    try { frame = window.frameElement; } catch (error) {}
    if (frame && frame.closest && frame.closest(".video-viewer-card")) {
        video.controls = true;
    }
    // The thumbnail comes first; only then does the video start fetching
    // its first bytes, so a preview can start without a wait but new cards
    // don't compete with it for bandwidth.
    var poster = new Image();
    poster.onload = poster.onerror = function () {
        video.dataset.posterReady = "1";
        ready();
        if (video.preload === "none") video.preload = "metadata";
    };
    // Playing before the thumbnail arrived shows the video straight away.
    video.addEventListener("playing", ready);
    poster.src = video.poster;
    video.addEventListener("error", function () {
        // Link expired or refused: use Instagram's own player instead.
        if (!/[?&]original=1/.test(location.search)) {
            location.replace(location.href + "&original=1");
        }
    });
    window.addEventListener("message", function (event) {
        var data = event.data;
        if (event.origin !== window.location.origin || !data || data["x-reelsearch"] !== true) return;
        if (["play", "pause", "preview", "stop"].indexOf(data.type) === -1) return;
        wanted = data.type;
        apply();
    });
    apply();
})();
</script>
</body></html>
"""


def player_page(media, autoplay=False):
    width, height = media.get("width"), media.get("height")
    # Portrait videos fill the card; wide ones are letterboxed.
    portrait = not (width and height) or width / height < 0.9
    poster = escape(media["poster"] or "", quote=True)
    if media["kind"] == "video":
        element = (
            f'<video src="{escape(media["video"], quote=True)}" '
            f'poster="{poster}" preload="none" loop playsinline '
            'disablepictureinpicture controlslist="nodownload noplaybackrate">'
            "</video>"
        )
    else:
        element = f'<img src="{poster}" alt="">'
    return PLAYER_PAGE % {
        "fit": "cover" if portrait else "contain",
        "media": element,
        "autoplay": "true" if autoplay else "false",
    }


def render_embed(url, autoplay=False, original=False):
    """Return (html, status) for a post's player page.

    original: serve Instagram's own page (cleaned up) instead of the bare
    player, used when the player could not play the video.
    """
    if not is_post_url(url):
        return unavailable_page("Not an Instagram video link"), 400
    try:
        html = _embed_html(url)
    except Exception as error:
        print(f"Could not load the Instagram embed for {url}: {error}")
        return unavailable_page("Could not load this video"), 502
    # Removed, private, and age-restricted posts come back without any media.
    if "EmbeddedMedia" not in html:
        return unavailable_page("This video is no longer available"), 200

    media = None if original else parse_media(html)
    if media is not None:
        return player_page(media, autoplay), 200

    injected = (
        '<base href="https://www.instagram.com/">'
        f"<style>{EMBED_STYLE}</style>"
        f"<script>{EMBED_SCRIPT % {'autoplay': 'true' if autoplay else 'false'}}"
        "</script>"
    )
    html, count = re.subn(
        r"<head[^>]*>", lambda match: match.group(0) + injected, html, count=1
    )
    if not count:
        html = injected + html
    return html, 200
