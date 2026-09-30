from flask import Flask, jsonify, render_template, request, redirect, url_for

from src.collection import (
    Collection, accounts_where, page_matches,
    platform_from_url, split_phrases,
)
from src.processor import (
    DEFAULT_DOWNLOAD_WORKERS,
    FEATURES,
    Processor,
    default_cpu_workers,
    download_workers_for,
    explain_error,
)
from src.instagram import Instagram
from src.instagram_embed import render_embed, warm as warm_instagram
from src.login_sessions import LoginError, LoginSessions
from src.storage import CATEGORIES as STORAGE_CATEGORIES
from src.storage import storage_cleanup, storage_report
from src.tiktok import TikTok
from src.video import Video
from src import account_store
from sentence_transformers import SentenceTransformer

import _thread
import json
import os
import queue
import threading
import time
import tempfile
from datetime import datetime
from urllib.parse import urlencode

from werkzeug.datastructures import MultiDict


app = Flask(__name__)


# --------------------------------------------------
# General configuration
# --------------------------------------------------

embed_model = SentenceTransformer("all-MiniLM-L6-v2")

# A sync has two kinds of work with separate limits, shared by every sync
# running at once. Downloads wait on the network: this many run at once per
# platform, fewer while the platform is dropping connections. Transcribing
# and embedding keep the CPU busy: this many run at once, with a couple of
# cores left free so the scraping browser stays responsive.
DOWNLOAD_WORKERS = int(
    os.getenv("REELSEARCH_DOWNLOAD_WORKERS") or DEFAULT_DOWNLOAD_WORKERS
)
CPU_WORKERS = int(
    os.getenv("REELSEARCH_CPU_WORKERS") or default_cpu_workers()
)
# Enough threads that every download and CPU slot can be busy at once, so
# videos download while others are being transcribed.
PROCESSING_WORKERS = (
    download_workers_for("fast", DOWNLOAD_WORKERS) + CPU_WORKERS
)

collection = Collection("videos", embed_model)
processor = Processor(
    embed_model=embed_model,
    download_workers=DOWNLOAD_WORKERS,
    cpu_workers=CPU_WORKERS,
)
# Where the library and downloads live, for the storage report. Both are
# relative to where the app was started, as in Collection and Processor.
library_dir = os.path.abspath("chroma_db")
videos_dir = os.path.abspath(processor.output_dir)

num_videos = 3
sync_state_path = os.path.join(os.path.dirname(__file__), "sync_state.json")
sync_lock = threading.RLock()
sync_workers = {}
sync_platforms = {}
shutting_down = threading.Event()
SHUTDOWN_WAIT_SECONDS = 15
ACTIVE_SYNC_STATUSES = {
    "collecting_urls",
    "loading_videos",
    "running",
    "pause_requested",
    "cancel_requested",
}
account_store.ensure_migrated()
collection.backfill_platforms()
collection.backfill_account_keys()

# Videos per page when the browser does not ask for a size. The page sizes
# itself to whole rows of the grid, so this is only the fallback.
SEARCH_PAGE_SIZE = 3
SEARCH_MAX_PAGE_SIZE = 30
# Videos past the ones being sent whose Instagram posts are fetched ahead,
# about two rows of the grid beyond what the page is about to load.
WARM_AHEAD = 9
UPLOAD_WINDOWS = {"week": 7, "month": 30, "year": 365}
SORT_FIELDS = {"newest": "uploaded_at", "likes": "likes"}
SEARCH_FILTER_NAMES = ("platform", "account", "uploaded", "sort", "reverse")
# Sources: a video matches if its whole platform or its account is selected.
MULTI_VALUE_FILTERS = ("platform", "account")
PLATFORM_LABELS = {"instagram": "Instagram", "tiktok": "TikTok"}
DETAILS_REQUEST_DELAY_SECONDS = 1
DETAILS_MAX_CONSECUTIVE_FAILURES = 5
details_lock = threading.Lock()
details_job = {"running": False, "done": 0, "total": 0, "error": None}


# Map a platform name to its concrete implementation
PLATFORMS = {
    "instagram": Instagram,
    "tiktok": TikTok,
}

# Accounts whose login window is open. An account is only saved once its
# login finishes.
login_sessions = LoginSessions(PLATFORMS)


def account_display_data():
    display_accounts = []
    for account in account_store.list_accounts():
        platform_class = get_platform_class(account["platform"])
        platform = platform_class(account["username"])
        display_accounts.append({
            **account,
            "abbreviation": platform_class.abbreviation,
            "account_url": platform.get_account_url(),
            "settings": account_store.account_settings(account),
        })
    return display_accounts


def get_platform_class(platform_name):
    """
    Return the concrete platform class for a platform name.
    """
    platform_name = platform_name.lower()

    if platform_name not in PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform_name}")

    return PLATFORMS[platform_name]


def get_username(platform_name):
    """
    Load the saved username for a platform.
    """
    filename = f"{platform_name.lower()}_username.txt"

    if not os.path.exists(filename):
        return ""

    with open(filename, "r") as f:
        return f.read().strip()


def save_username(platform_name, username):
    """
    Persist the username for a platform.
    """
    filename = f"{platform_name.lower()}_username.txt"

    with open(filename, "w") as f:
        f.write(username.strip())


def is_connected(platform_name):
    """
    Determine whether a platform has a saved username.
    """
    return get_username(platform_name) != ""


def _empty_sync_state():
    return {
        "status": "idle",
        "urls": [],
        "loaded_urls": [],
        "completed_urls": [],
        # Posts with no video (photos): never retried, counted as done.
        "skipped_urls": [],
        # The backlog set aside by the skip_backlog setting: not loaded.
        "ignored_urls": [],
        "current_url": None,
        "last_error": None,
        "updated_at": None,
    }


def _unique_urls(value):
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(
        url.strip() for url in value
        if isinstance(url, str) and url.strip()
    ))


def load_sync_states():
    if not os.path.exists(sync_state_path):
        return {}
    try:
        with open(sync_state_path, "r", encoding="utf-8") as file:
            states = json.load(file)
            return states if isinstance(states, dict) else {}
    except (OSError, json.JSONDecodeError) as error:
        print(f"Could not read sync state: {error}")
        return {}


def save_sync_states(states):
    directory = os.path.dirname(sync_state_path) or "."
    handle, temporary_path = tempfile.mkstemp(
        prefix="sync_state_",
        suffix=".json",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(states, file, indent=2)
        os.replace(temporary_path, sync_state_path)
    except Exception:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
        raise


def get_sync_state(platform_name):
    with sync_lock:
        states = load_sync_states()
        state = _empty_sync_state()
        state.update(states.get(platform_name, {}))
        worker = sync_workers.get(platform_name)
        if state["status"] in ACTIVE_SYNC_STATUSES and not (
            worker and worker.is_alive()
        ):
            state["status"] = "paused"
            state["current_url"] = None
            state["updated_at"] = time.time()
            states[platform_name] = state
            save_sync_states(states)

        # Older state files only tracked completed processing steps. Treat
        # those as loaded URLs when migrating to durable library progress.
        state["urls"] = _unique_urls(state.get("urls"))
        state["completed_urls"] = _unique_urls(state.get("completed_urls"))
        state["skipped_urls"] = _unique_urls(state.get("skipped_urls"))
        state["ignored_urls"] = _unique_urls(state.get("ignored_urls"))
        loaded_urls = state.get("loaded_urls") or state["completed_urls"]
        state["loaded_urls"] = _unique_urls(loaded_urls)
        return state


def update_sync_state(platform_name, **changes):
    with sync_lock:
        states = load_sync_states()
        state = _empty_sync_state()
        state.update(states.get(platform_name, {}))
        state.update(changes)
        state["updated_at"] = time.time()
        states[platform_name] = state
        save_sync_states(states)
        return state


def clear_sync_state(platform_name):
    with sync_lock:
        states = load_sync_states()
        states.pop(platform_name, None)
        if states:
            save_sync_states(states)
        elif os.path.exists(sync_state_path):
            os.remove(sync_state_path)


def loaded_urls_for_account(account_id, state):
    """Return durable URLs already loaded for this account.

    The state file is authoritative for new runs. Existing indexed records
    are also included so a state-file upgrade or an interrupted write cannot
    make previously loaded videos appear as new work.
    """
    source_urls = set(state.get("urls", []))
    loaded_urls = set(state.get("loaded_urls", []))
    try:
        loaded_urls |= collection.owned_ids(account_id) & source_urls
    except Exception as error:
        print(
            f"Could not check which videos are already loaded for "
            f"{account_id}; progress counts may be off until the next "
            f"check: {explain_error(error)}"
        )
    return loaded_urls & source_urls


def set_backlog_aside(account_id):
    """Ignore the URLs found so far that have not been loaded, so syncs
    move on to the videos they find themselves."""
    with sync_lock:
        state = get_sync_state(account_id)
        done = (
            loaded_urls_for_account(account_id, state)
            | set(state["skipped_urls"])
        )
        backlog = [url for url in state["urls"] if url not in done]
        update_sync_state(
            account_id,
            ignored_urls=list(dict.fromkeys(state["ignored_urls"] + backlog)),
        )


def restore_backlog(account_id):
    """Make ignored URLs pending again; they load from the next sync."""
    update_sync_state(account_id, ignored_urls=[])


def sync_status_payload(platform_name):
    state = get_sync_state(platform_name)
    collection_count = collection.count()
    loaded_urls = loaded_urls_for_account(platform_name, state)
    loaded = len(loaded_urls)
    total = len(state["urls"])
    source_urls = set(state["urls"]) - loaded_urls
    skipped_urls = set(state["skipped_urls"]) & source_urls
    skipped = len(skipped_urls)
    ignored = len(set(state["ignored_urls"]) & source_urls - skipped_urls)
    return {
        "platform": platform_name,
        "status": state["status"],
        "current_url": state["current_url"],
        "last_error": state["last_error"],
        "source_total": total,
        "completed": loaded,
        "loaded": loaded,
        "skipped": skipped,
        "ignored": ignored,
        "remaining": max(total - loaded - skipped - ignored, 0),
        "total_videos": collection_count,
    }


def sync_display_data(account_id):
    payload = sync_status_payload(account_id)
    updated_at = get_sync_state(account_id).get("updated_at")
    payload["last_sync"] = (
        datetime.fromtimestamp(updated_at).strftime("%d %b %Y, %H:%M")
        if isinstance(updated_at, (int, float))
        else "Never"
    )
    return payload


def accounts_with_sync_data():
    accounts = account_display_data()
    for account in accounts:
        account["sync"] = sync_display_data(account["id"])
    return accounts


# --------------------------------------------------
# Generic platform operations
# --------------------------------------------------

def _close_browser(platform):
    try:
        if platform.driver is not None:
            platform.driver.quit()
    except Exception:
        pass
    platform.driver = None


def sync_platform(platform, account):
    """Collect saved URLs and process videos at the same time.

    This thread drives the browser and queues every URL it finds, while a
    pool of worker threads takes URLs off the queue and processes several
    videos at once, so videos start appearing in the library while
    scrolling is still going on.
    """
    state_key = account["id"]
    settings = account_store.account_settings(account)
    features = {name: settings[name] for name in FEATURES}
    update_sync_state(
        state_key,
        status="collecting_urls",
        current_url=None,
        last_error=None,
    )
    pending = queue.Queue()
    queued = set()
    scraping_done = threading.Event()
    abort = threading.Event()
    processing_errors = []
    loaded = set()
    in_flight = []

    def enqueue(url):
        if url not in queued:
            queued.add(url)
            pending.put(url)

    def stop_requested():
        return abort.is_set() or get_sync_state(state_key)["status"] in {
            "pause_requested",
            "cancel_requested",
        }

    def process_pending():
        try:
            while not stop_requested():
                try:
                    url = pending.get(timeout=0.5)
                except queue.Empty:
                    if scraping_done.is_set():
                        return
                    continue
                with sync_lock:
                    # Read each time: the backlog can be set aside mid-sync.
                    if (
                        url in loaded
                        or url in get_sync_state(state_key)["ignored_urls"]
                    ):
                        continue
                    in_flight.append(url)
                    update_sync_state(state_key, current_url=in_flight[0])
                try:
                    video = processor.process_video(
                        url, features, speed=settings["load_speed"]
                    )
                    with sync_lock:
                        state = get_sync_state(state_key)
                        if video is None and url in processor.unloadable_urls:
                            loaded.add(url)
                            update_sync_state(
                                state_key,
                                skipped_urls=list(dict.fromkeys(
                                    state["skipped_urls"] + [url]
                                )),
                            )
                        elif video is None:
                            print(
                                f"Not added to the library: {url} (see the "
                                "message above for why). It will be tried "
                                "again on the next sync."
                            )
                        elif state["status"] == "cancel_requested":
                            print(
                                f"Discarded {url}: sync was cancelled "
                                "before it finished."
                            )
                        else:
                            collection.add(
                                video,
                                account_id=account["id"],
                                platform=account["platform"],
                            )
                            loaded.add(url)
                            update_sync_state(
                                state_key,
                                loaded_urls=list(dict.fromkeys(
                                    state["loaded_urls"] + [url]
                                )),
                                completed_urls=list(dict.fromkeys(
                                    state["completed_urls"] + [url]
                                )),
                            )
                finally:
                    with sync_lock:
                        in_flight.remove(url)
                        update_sync_state(
                            state_key,
                            current_url=in_flight[0] if in_flight else None,
                        )
        except Exception as error:
            print(
                f"A {account['platform']} sync worker crashed while handling "
                f"a video, so the sync is stopping: {explain_error(error)}"
            )
            processing_errors.append(error)
            abort.set()

    processing_workers = [
        threading.Thread(
            target=process_pending,
            daemon=True,
            name=f"{account['platform']}-{state_key}-process-{index}",
        )
        for index in range(PROCESSING_WORKERS)
    ]
    try:
        if settings["skip_backlog"]:
            set_backlog_aside(state_key)
        else:
            restore_backlog(state_key)
        initial_state = get_sync_state(state_key)
        loaded.update(loaded_urls_for_account(state_key, initial_state))
        loaded.update(initial_state["skipped_urls"])
        # URLs from earlier runs that were never processed go first.
        for url in get_sync_state(state_key)["urls"]:
            enqueue(url)
        for worker in processing_workers:
            worker.start()

        def record_url(url):
            with sync_lock:
                state = get_sync_state(state_key)
                if url not in state["urls"]:
                    update_sync_state(state_key, urls=state["urls"] + [url])
            enqueue(url)

        try:
            platform.log_in()
            scraped_urls = platform.scrape_videos(
                on_url_found=record_url,
                should_stop=stop_requested,
                new_only=settings["sync_mode"] == "new",
            )
        except Exception:
            if shutting_down.is_set():
                raise
            # Collecting links failed, but the videos already found are
            # still worth processing before the error is reported.
            _close_browser(platform)
            scraping_done.set()
            for worker in processing_workers:
                worker.join()
            raise
        _close_browser(platform)

        with sync_lock:
            state = get_sync_state(state_key)
            known_urls = list(state["urls"])
            known_urls.extend(
                url for url in scraped_urls if url not in known_urls
            )
            changes = {"urls": known_urls}
            if state["status"] == "collecting_urls":
                changes["status"] = "loading_videos"
            update_sync_state(state_key, **changes)
        for url in known_urls:
            enqueue(url)
        scraping_done.set()
        for worker in processing_workers:
            worker.join()

        if processing_errors:
            raise processing_errors[0]
        with sync_lock:
            status = get_sync_state(state_key)["status"]
            final_status = {
                "pause_requested": "paused",
                "cancel_requested": "cancelled",
            }.get(status, "completed")
            update_sync_state(
                state_key, status=final_status, current_url=None
            )
    except Exception as error:
        abort.set()
        deadline = time.monotonic() + 5
        for worker in processing_workers:
            if worker.is_alive():
                worker.join(timeout=max(deadline - time.monotonic(), 0))
        if shutting_down.is_set():
            # The browser was closed by the app shutting down, not a failure.
            update_sync_state(state_key, status="paused", current_url=None)
            return
        reason = explain_error(error)
        message = (
            f"{account['platform'].capitalize()} sync of "
            f"{account['username']} stopped with an error: {reason}. "
            "Progress so far is saved; start the sync again to resume."
        )
        print(message)
        update_sync_state(
            state_key, status="error", current_url=None, last_error=message
        )
    finally:
        _close_browser(platform)
        with sync_lock:
            sync_workers.pop(state_key, None)
            sync_platforms.pop(state_key, None)


def start_sync(account_id):
    with sync_lock:
        account = account_store.get_account(account_id)
        if account is None:
            raise ValueError("Account not found.")
        worker = sync_workers.get(account_id)
        if worker and worker.is_alive():
            update_sync_state(account_id, status="pause_requested")
            return "pause_requested"

        if shutting_down.is_set():
            raise ValueError("ReelSearch is shutting down.")

        platform = create_platform(account_id)
        sync_platforms[account_id] = platform
        update_sync_state(
            account_id,
            status="collecting_urls",
            current_url=None,
            last_error=None,
        )
        worker = threading.Thread(
            target=sync_platform,
            args=(platform, account),
            daemon=True,
            name=f"{account['platform']}-{account_id}-sync",
        )
        sync_workers[account_id] = worker
        worker.start()
        return "started"


def create_platform(account_id):
    """
    Create a concrete platform instance using the saved username.
    """
    account = account_store.get_account(account_id)
    if account is None:
        raise ValueError("Account not found.")
    platform_class = get_platform_class(account["platform"])
    return platform_class(
        account["username"],
        account_id=account["id"],
        profile_path=account_store.profile_path(account),
        urls_path=account_store.urls_path(account),
    )


def search_facets(metadatas, accounts):
    """Describe which filters the library has data for."""
    metadatas = [metadata or {} for metadata in metadatas]
    platforms_with_videos = {
        metadata.get("platform") for metadata in metadatas
    }
    return {
        "sources": [
            {
                "platform": platform,
                "abbreviation": PLATFORMS[platform].abbreviation,
                "label": PLATFORM_LABELS.get(platform, platform.capitalize()),
                "accounts": [
                    account for account in accounts
                    if account["platform"] == platform
                ],
            }
            for platform in PLATFORMS
            if platform in platforms_with_videos or any(
                account["platform"] == platform for account in accounts
            )
        ],
        **{
            field: any(field in metadata for metadata in metadatas)
            for field in ("uploaded_at", "likes")
        },
        "missing_details": sum(
            1 for metadata in metadatas if not metadata.get("details_checked")
        ),
    }


def render_profile(
    search_results=None, query="", has_more=False, filters=None,
    search_meta=None, similar="", has_weak=False,
):
    """
    Render the profile page with the connection state
    of all supported platforms.
    """
    filters = filters or read_search_filters(MultiDict())
    library = library_videos(filters)
    # The facets describe the whole library, not only the filtered view.
    if search_where(filters) is None:
        library_metadatas = [metadata for _, metadata in library]
    else:
        library_metadatas = collection.find()[1]
    shown = library[:num_videos]
    warm_embeds(library[:num_videos + WARM_AHEAD])
    if search_results:
        warm_embeds(search_results)

    accounts = account_display_data()
    label = similar_label(similar) if similar else None
    return render_template(
        "profile.html",
        accounts=accounts,
        videos=[video_id for video_id, _ in shown],
        video_meta={**dict(shown), **(search_meta or {})},
        total_videos=collection.count(),
        library_matching=len(library),
        last_sync="10",
        search_results=search_results,
        query=query,
        similar=similar if label else "",
        similar_label=label,
        has_more=has_more,
        has_weak=has_weak,
        filters=filters,
        search_heading=search_heading(
            query, label or ("this video" if similar else None)
        ),
        search_params=search_params(query, filters, similar),
        facets=search_facets(library_metadatas, accounts),
        details_job=dict(details_job),
        sync_states={
            account["id"]: get_sync_state(account["id"])
            for account in accounts
        },
    )


# --------------------------------------------------
# Home
# --------------------------------------------------

@app.route("/")
def home():
    query = request.args.get("q", "").strip()
    # "More like this" from a video replaces the typed query.
    similar = request.args.get("similar", "").strip()
    if similar:
        query = ""
    filters = read_search_filters(request.args)
    if not query and not similar:
        return render_profile(filters=filters)
    results, has_more, has_weak = run_search(
        query, filters, offset=0, similar=similar
    )
    return render_profile(
        search_results=[video_id for video_id, _ in results],
        search_meta=dict(results),
        query=query,
        has_more=has_more,
        filters=filters,
        similar=similar,
        has_weak=has_weak,
    )


# --------------------------------------------------
# Generic login flow
# --------------------------------------------------

def render_login(platform_name, error=None, pending=None, username=""):
    return render_template(
        "login.html",
        platform=platform_name.capitalize(),
        platform_abbreviation=get_platform_class(platform_name).abbreviation,
        error=error,
        pending=pending,
        username=username,
    )


def login_platform(platform_name):
    """
    Generic login flow used by Instagram and TikTok.

    Submitting the username opens a browser window and moves to a waiting
    page. The account is only saved once the login finishes there.
    """
    get_platform_class(platform_name)

    if request.method == "GET":
        return render_login(platform_name)

    entered_username = request.form.get("username", "")
    try:
        session = login_sessions.start(platform_name, entered_username)
    except LoginError as error:
        return render_login(
            platform_name, error=str(error), username=entered_username
        ), 400
    return redirect(url_for("login_session_route", session_id=session.id))


@app.route("/login/session/<session_id>")
def login_session_route(session_id):
    session = login_sessions.touch(session_id)
    if session is None:
        return redirect(url_for("add_account_route"))
    if session.status == "connected":
        return redirect(url_for("home"))
    return render_login(
        session.platform_name,
        username=session.username,
        pending={
            "status_url": url_for("login_status_route", session_id=session.id),
            "confirm_url": url_for("login_confirm_route", session_id=session.id),
            "cancel_url": url_for("login_cancel_route", session_id=session.id),
            "done_url": url_for("home"),
            "leave_url": url_for("accounts_route"),
            "retry_url": url_for(
                "add_account_route", platform=session.platform_name
            ),
        },
    )


def login_status_payload(session):
    payload = session.snapshot()
    payload["done_url"] = url_for("home")
    return jsonify(payload)


@app.route("/login/session/<session_id>/status")
def login_status_route(session_id):
    session = login_sessions.touch(session_id)
    if session is None:
        return jsonify({
            "status": "unknown",
            "error": "This login is no longer active.",
        }), 404
    return login_status_payload(session)


@app.route("/login/session/<session_id>/confirm", methods=["POST"])
def login_confirm_route(session_id):
    session = login_sessions.confirm(session_id)
    if session is None:
        return jsonify({
            "status": "unknown",
            "error": "This login is no longer active.",
        }), 404
    return login_status_payload(session)


@app.route("/login/session/<session_id>/cancel", methods=["POST"])
def login_cancel_route(session_id):
    session = login_sessions.cancel(session_id)
    if session is None:
        return jsonify({"status": "unknown"}), 404
    return login_status_payload(session)


# Keep your existing URLs so your current template does not break.

@app.route("/login/<platform>", methods=["GET", "POST"])
def login_platform_route(platform):
    return login_platform(platform)


@app.route("/accounts")
def accounts_route():
    return render_template(
        "accounts.html",
        accounts=accounts_with_sync_data(),
        platforms=PLATFORMS,
        ocr_available=processor.ocr_enabled,
    )


@app.route("/accounts/<account_id>/settings", methods=["POST"])
def account_settings_route(account_id):
    """Save an account's settings. They apply from its next sync."""
    from_page = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    account = account_store.get_account(account_id)
    if account is None:
        if from_page:
            return jsonify({"error": "Account not found"}), 404
        return redirect(url_for("accounts_route"))
    skipped_before = account_store.account_settings(account)["skip_backlog"]
    # The page posts every setting together; an unticked box is left out.
    settings = account_store.update_settings(
        account_id,
        sync_mode="new" if request.form.get("new_only") else "all",
        load_speed=request.form.get("load_speed", "medium"),
        skip_backlog=bool(request.form.get("skip_backlog")),
        **{name: bool(request.form.get(name)) for name in FEATURES},
    )
    # Applied straight away, so a running sync drops the backlog too.
    if settings["skip_backlog"] and not skipped_before:
        set_backlog_aside(account_id)
    elif skipped_before and not settings["skip_backlog"]:
        restore_backlog(account_id)
    if from_page:
        return jsonify(settings)
    return redirect(url_for("accounts_route"))


@app.route("/accounts/add", methods=["GET", "POST"])
def add_account_route():
    platform = request.values.get("platform", "").lower()
    if request.method == "GET" and not platform:
        return render_template(
            "add_account.html", platforms=PLATFORMS, platform=""
        )
    if platform not in PLATFORMS:
        return render_template(
            "add_account.html",
            platforms=PLATFORMS,
            platform=platform,
            error="Choose a supported platform.",
        ), 400
    if request.method == "GET":
        return render_login(platform)
    return login_platform(platform)

# --------------------------------------------------
# Search
# --------------------------------------------------

def read_search_filters(values):
    """Keep only recognised filter values from request parameters."""
    allowed = {
        "platform": PLATFORMS,
        "account": {account["id"] for account in account_store.list_accounts()},
        "uploaded": UPLOAD_WINDOWS,
        "sort": SORT_FIELDS,
        "reverse": {"1"},
    }
    filters = {}
    for name in SEARCH_FILTER_NAMES:
        if name in MULTI_VALUE_FILTERS:
            filters[name] = [
                value for value in dict.fromkeys(values.getlist(name))
                if value in allowed[name]
            ]
        else:
            value = (values.get(name) or "").strip()
            filters[name] = value if value in allowed[name] else ""
    return filters


def search_where(filters):
    clauses = []
    sources = []
    if filters["platform"]:
        sources.append({"platform": {"$in": filters["platform"]}})
    if filters["account"]:
        sources.append(accounts_where(filters["account"]))
    if len(sources) == 1:
        clauses.append(sources[0])
    elif sources:
        clauses.append({"$or": sources})
    days = UPLOAD_WINDOWS.get(filters["uploaded"])
    if days:
        clauses.append({"uploaded_at": {"$gte": time.time() - days * 86400}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def run_search(
    query, filters, offset, limit=SEARCH_PAGE_SIZE, similar="", weak=False,
):
    """Return one page of (url, metadata) matches, best first, whether more
    follow, and whether weaker matches are being held back.

    With similar set to a saved video's URL, the matches are the videos
    most like it instead of ones matching the query.

    Quoted phrases in the query must appear in a video's text (ignoring
    case). The rest of the query ranks those videos by meaning, or the
    phrases themselves if nothing else was typed.

    A plain text search leaves out matches past WEAK_MATCH_DISTANCE; with
    weak set, they are included and offset continues past the strong ones.
    "More like this" and phrase searches are never split: the first always
    has a nearest video worth showing (and video-to-video distances are on
    a different scale than a short query's), and the second already only
    returns videos containing the phrase.

    The sort order is not applied here: the page reorders only the results
    it is showing.
    """
    end = offset + limit
    where = search_where(filters)
    if similar:
        ids, metadatas = collection.search_similar(similar, end + 1, where)
        distances = [0] * len(ids)
    else:
        text, phrases = split_phrases(query)
        ids, metadatas, distances = collection.search(
            text or " ".join(phrases) or query, end + 1, where, phrases,
            with_distances=True,
        )
        weak = weak or bool(phrases)
    return page_matches(
        zip(ids, metadatas, distances), offset, limit, weak=weak
    )


def library_videos(filters):
    """Every (url, metadata) in the library that passes the filters, in the
    chosen sort order."""
    ids, metadatas = collection.find(search_where(filters))
    videos = [
        (video_id, metadata or {})
        for video_id, metadata in zip(ids, metadatas)
    ]
    sort_field = SORT_FIELDS.get(filters["sort"])
    reverse = filters["reverse"] == "1"
    if sort_field:
        # Videos without the value stay last in either direction.
        known = [video for video in videos if sort_field in video[1]]
        missing = [video for video in videos if sort_field not in video[1]]
        known.sort(key=lambda video: video[1][sort_field], reverse=not reverse)
        videos = known + missing
    elif reverse:
        videos.reverse()
    return videos


SIMILAR_CAPTION_LENGTH = 40


def similar_label(url):
    """A short name for the video a "More like this" search started from,
    or None if it is not in the library."""
    video = collection.get_video(url)
    return None if video is None else video_label(video[1])


def video_label(document):
    fields = Video.parse_document(document)
    if fields.get("uploader"):
        return f"{fields['uploader']}'s video"
    caption = " ".join(fields.get("caption", "").split())
    if caption:
        if len(caption) > SIMILAR_CAPTION_LENGTH:
            caption = caption[:SIMILAR_CAPTION_LENGTH].rstrip() + "…"
        return f'"{caption}"'
    return "this video"


def search_heading(query, similar_label=None):
    if similar_label:
        return f"More like {similar_label}"
    return f'Matches for "{query}"'


def search_params(query, filters, similar=""):
    params = {"similar": similar} if similar else {"q": query} if query else {}
    params.update({name: value for name, value in filters.items() if value})
    return urlencode(params, doseq=True)


@app.route("/search", methods=["POST"])
def search():

    query = request.form.get("query", "").strip()
    similar = request.form.get("similar", "").strip()
    if similar:
        query = ""
    filters = read_search_filters(request.form)
    offset = max(request.form.get("offset", default=0, type=int), 0)
    # "Show weaker matches" and its "Show more" pages.
    weak = request.form.get("weak") == "1"
    limit = min(
        max(request.form.get("limit", default=SEARCH_PAGE_SIZE, type=int), 1),
        SEARCH_MAX_PAGE_SIZE,
    )
    if not query and not similar:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return ""
        return redirect(url_for("home"))

    label = (similar_label(similar) or "this video") if similar else None
    results, has_more, has_weak = run_search(
        query, filters, offset, limit, similar, weak
    )
    urls = [video_id for video_id, _ in results]
    warm_embeds(urls)
    if has_more:
        # "Show more" is likely next; have its videos ready by then.
        threading.Thread(
            target=lambda: warm_embeds(
                run_search(
                    query, filters, offset + limit, limit, similar, weak
                )[0]
            ),
            daemon=True,
            name="warm-search",
        ).start()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        response = render_template(
            "_video_section.html",
            search_results=urls,
            video_meta=dict(results),
            query=query,
            filters=filters,
            search_heading=search_heading(query, label),
            search_params=search_params(query, filters, similar),
            eager=True,
            has_more=has_more,
            has_weak=has_weak,
            weak=weak,
            search_offset=offset,
            # The first weaker page can start at 0 when nothing was strong;
            # it still joins the section already on the page.
            append=offset > 0 or weak,
        )
        if offset > 0 or weak:
            response = app.make_response(response)
            response.headers["X-Search-Has-More"] = str(has_more).lower()
            response.headers["X-Search-Has-Weak"] = str(has_weak).lower()
            response.headers["X-Search-Next-Offset"] = str(
                offset + len(urls)
            )
        return response

    return render_profile(
        search_results=urls,
        search_meta=dict(results),
        query=query,
        has_more=has_more,
        filters=filters,
        similar=similar,
        has_weak=has_weak,
    )


# --------------------------------------------------
# Video details (upload date, likes)
# --------------------------------------------------

def fetch_missing_details():
    """Look up details for videos indexed before they were recorded."""
    failed = []
    error = None
    try:
        for url in collection.ids_missing_details():
            if shutting_down.is_set():
                break
            stats = processor.fetch_details(url)
            if stats is None:
                failed.append(url)
                if len(failed) >= DETAILS_MAX_CONSECUTIVE_FAILURES:
                    # Likely offline or rate limited, not missing videos,
                    # so leave these to be retried next time.
                    failed = []
                    error = (
                        f"Stopped after {DETAILS_MAX_CONSECUTIVE_FAILURES} "
                        "videos in a row could not be read. You may be "
                        "offline or rate limited; the remaining videos will "
                        "be retried next time."
                    )
                    break
            else:
                # The connection works, so earlier failures are videos that
                # were removed or made private.
                for failed_url in failed:
                    collection.set_details(failed_url, {})
                failed = []
                collection.set_details(url, stats)
            with details_lock:
                details_job["done"] += 1
            time.sleep(DETAILS_REQUEST_DELAY_SECONDS)
        for failed_url in failed:
            collection.set_details(failed_url, {})
    except Exception as exception:
        error = f"Fetching video details failed: {explain_error(exception)}"
        print(error)
    finally:
        with details_lock:
            details_job.update(running=False, error=error)


def details_status_payload():
    with details_lock:
        payload = dict(details_job)
    payload["missing"] = len(collection.ids_missing_details())
    return payload


@app.route("/videos/details", methods=["POST"])
def fetch_details_route():
    with details_lock:
        if not details_job["running"] and not shutting_down.is_set():
            details_job.update(
                running=True,
                done=0,
                total=len(collection.ids_missing_details()),
                error=None,
            )
            threading.Thread(
                target=fetch_missing_details,
                daemon=True,
                name="video-details",
            ).start()
    return jsonify(details_status_payload())


@app.route("/videos/details/status")
def details_status_route():
    return jsonify(details_status_payload())


# --------------------------------------------------
# Generic sync flow
# --------------------------------------------------

def sync(platform_name):
    """
    Generic synchronization route.

    Creates the correct concrete platform class and
    passes it to the generic sync function.
    """

    platform_name = platform_name.lower()
    accounts = account_store.accounts_for_platform(platform_name)
    if platform_name not in PLATFORMS or not accounts:
        return jsonify({"error": "Unsupported platform"}), 400
    account_id = accounts[0]["id"]
    try:
        action = start_sync(account_id)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    payload = sync_status_payload(account_id)
    payload["action"] = action
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(payload)
    return redirect(url_for("home"))


# Keep your existing routes so current HTML continues to work.

@app.route("/sync/<platform>", methods=["POST"])
def sync_platform_route(platform):
    return sync(platform)


@app.route("/sync/<platform>/status")
def sync_status_route(platform):
    platform = platform.lower()
    accounts = account_store.accounts_for_platform(platform)
    if platform not in PLATFORMS or not accounts:
        return jsonify({"error": "Unsupported platform"}), 400
    return jsonify(sync_status_payload(accounts[0]["id"]))


@app.route("/sync/account/<account_id>", methods=["POST"])
def sync_account_route(account_id):
    account = account_store.get_account(account_id)
    if account is None:
        return jsonify({"error": "Account not found"}), 404
    try:
        action = start_sync(account_id)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    payload = sync_status_payload(account_id)
    payload["action"] = action
    return jsonify(payload)


@app.route("/sync/account/<account_id>/status")
def sync_account_status_route(account_id):
    if account_store.get_account(account_id) is None:
        return jsonify({"error": "Account not found"}), 404
    return jsonify(sync_status_payload(account_id))


@app.route("/logout", methods=["GET", "POST"])
def logout_route():
    if request.method == "GET":
        return render_template(
            "logout.html",
            accounts=account_display_data(),
        )

    account_id = request.form.get("account_id", "")
    account = account_store.get_account(account_id)
    if account is None:
        return render_template(
            "logout.html",
            accounts=account_display_data(),
            error="Choose a valid connected account.",
        ), 400

    worker = sync_workers.get(account_id)
    if worker and worker.is_alive():
        update_sync_state(account_id, status="cancel_requested")
        worker.join(timeout=2)
        if worker.is_alive():
            return render_template(
                "logout.html",
                accounts=account_display_data(),
                error=(
                    "That sync is still finishing the current video. "
                    "Please try logging out again in a moment."
                ),
            ), 409

    urls_path = account_store.urls_path(account)
    collection.delete_account_videos(account_id, urls_path)
    # The browser profile holds the account's login cookies.
    if not account_store.remove_profile(account):
        print(
            f"Could not remove the browser profile of {account['username']}; "
            "it is listed under Storage on the accounts page."
        )
    if os.path.exists(urls_path):
        os.remove(urls_path)
    clear_sync_state(account_id)
    account_store.delete_account(account_id)
    if account.get("legacy"):
        account_store.remove_legacy_username(account["platform"])
    return redirect(url_for("accounts_route"))

# --------------------------------------------------
# Storage
# --------------------------------------------------

storage_lock = threading.Lock()


def running_syncs():
    with sync_lock:
        return [
            account_id for account_id, worker in sync_workers.items()
            if worker.is_alive()
        ]


def storage_args():
    """What storage_report needs to tell leftovers from files in use."""
    return {
        "accounts": account_store.list_accounts(),
        "sync_states": load_sync_states(),
        "active_login_ids": login_sessions.active_ids(),
        "busy_urls": [
            url for url in (
                get_sync_state(account_id)["current_url"]
                for account_id in running_syncs()
            ) if url
        ],
        "videos_dir": videos_dir,
        "library_dir": library_dir,
    }


def storage_payload():
    report = storage_report(**storage_args())
    for category in report["categories"].values():
        # Paths are only needed to clean up; the page shows counts.
        del category["items"], category["sizes"]
    report["busy"] = bool(running_syncs() or login_sessions.active_ids())
    return report


@app.route("/storage")
def storage_route():
    return jsonify(storage_payload())


@app.route("/storage/cleanup", methods=["POST"])
def storage_cleanup_route():
    """Remove the chosen leftovers. The page asks the person first."""
    categories = request.form.getlist("category")
    if not categories or set(categories) - set(STORAGE_CATEGORIES):
        return jsonify({"error": "Choose what to clean up."}), 400
    if not storage_lock.acquire(blocking=False):
        return jsonify({"error": "A cleanup is already running."}), 409
    try:
        with sync_lock:
            if running_syncs() or login_sessions.active_ids():
                return jsonify({
                    "error": (
                        "Wait for running syncs and logins to finish, "
                        "then clean up."
                    )
                }), 409
            report = storage_report(**storage_args())
        result = storage_cleanup(report, categories, clear_sync_state)
    finally:
        storage_lock.release()
    return jsonify({**result, "storage": storage_payload()})

# --------------------------------------------------
# Shutdown
# --------------------------------------------------

def stop_syncs(timeout=SHUTDOWN_WAIT_SECONDS):
    """Pause running syncs, closing any that do not stop in time.

    Syncs check for a pause between videos, so they normally stop cleanly
    and keep their progress. A sync still busy after the timeout has its
    browser closed and is marked paused; its current video was not saved
    yet, so it is simply loaded again on the next sync.
    """
    shutting_down.set()
    with sync_lock:
        workers = {
            account_id: worker
            for account_id, worker in sync_workers.items()
            if worker.is_alive()
        }
        for account_id in workers:
            update_sync_state(account_id, status="pause_requested")

    deadline = time.monotonic() + timeout
    for worker in workers.values():
        worker.join(timeout=max(deadline - time.monotonic(), 0))

    forced = []
    for account_id, worker in workers.items():
        if not worker.is_alive():
            continue
        forced.append(account_id)
        platform = sync_platforms.get(account_id)
        if platform is not None and platform.driver is not None:
            try:
                platform.driver.quit()
            except Exception as error:
                print(f"Could not close browser for {account_id}: {error}")
        update_sync_state(account_id, status="paused", current_url=None)
    return {"paused": len(workers) - len(forced), "forced": len(forced)}


@app.route("/shutdown", methods=["POST"])
def shutdown_route():
    summary = stop_syncs()
    login_sessions.shutdown()
    # Stop the server the same way Ctrl+C does, after this response is sent.
    threading.Timer(0.5, _thread.interrupt_main).start()
    return render_template("stopped.html", **summary)


# --------------------------------------------------
# Instagram video embeds
# --------------------------------------------------

@app.route("/embed/instagram")
def instagram_embed():
    """Instagram's embed page with only the video left in it."""
    html, status = render_embed(
        request.args.get("url", ""),
        autoplay=request.args.get("autoplay") == "1",
        original=request.args.get("original") == "1",
    )
    response = app.make_response((html, status))
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    if status == 200:
        # Reopening a video (the full-screen viewer loads its own copy of
        # each card) then needs no request at all. Kept well short of when
        # Instagram's video links expire.
        response.headers["Cache-Control"] = "private, max-age=600"
    return response


def warm_embeds(videos):
    """Start fetching Instagram posts that are about to be shown.

    videos: URLs or (url, metadata) pairs.
    """
    warm_instagram(
        video if isinstance(video, str) else video[0] for video in videos
    )


# --------------------------------------------------
# All videos
# --------------------------------------------------

@app.route("/videos/info")
def video_info():
    """Details shown beside a video in the full-screen viewer."""
    url = request.args.get("url", "")
    video = collection.get_video(url)
    if video is None:
        return jsonify({"error": "Video not found"}), 404
    metadata, document = video
    platform = metadata.get("platform") or platform_from_url(url)
    account = (
        account_store.get_account(metadata["account_id"])
        if metadata.get("account_id") else None
    )
    return jsonify({
        "url": url,
        "platform": platform,
        "platform_label": PLATFORM_LABELS.get(
            platform, (platform or "").capitalize()
        ),
        "abbreviation": (
            PLATFORMS[platform].abbreviation if platform in PLATFORMS else ""
        ),
        "uploaded_at": metadata.get("uploaded_at"),
        "likes": metadata.get("likes"),
        "saved_by": account["username"] if account else None,
        # Names the video in the search bar after "More like this".
        "similar_label": video_label(document),
        **Video.parse_document(document),
    })


@app.route("/videos/count")
def videos_count():
    return jsonify({
        "total_videos": collection.count(),
        "matching": len(library_videos(read_search_filters(request.args))),
    })


@app.route("/videos")
def videos():

    offset = max(request.args.get("offset", default=0, type=int), 0)
    limit = min(max(request.args.get("limit", default=num_videos, type=int), 1), 50)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        library = library_videos(read_search_filters(request.args))
        selected = library[offset:offset + limit]
        # These, then the next few rows, so scrolling on finds them ready.
        warm_embeds(library[offset:offset + limit + WARM_AHEAD])
        response = app.make_response(render_template(
            "_video_cards.html",
            videos=[video_id for video_id, _ in selected],
            video_meta=dict(selected),
            eager=False
        ))
        response.headers["X-Library-Total"] = str(len(library))
        return response

    return render_template(
        "index.html",
        videos=collection.ids()
    )


# --------------------------------------------------
# Start Flask
# --------------------------------------------------

if __name__ == "__main__":
    app.run(debug=False)
