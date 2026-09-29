from flask import Flask, jsonify, render_template, request, redirect, url_for

from src.collection import Collection
from src.processor import Processor
from src.instagram import Instagram
from src.tiktok import TikTok
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


app = Flask(__name__)


# --------------------------------------------------
# General configuration
# --------------------------------------------------

embed_model = SentenceTransformer("all-MiniLM-L6-v2")

collection = Collection("videos", embed_model)
processor = Processor(embed_model=embed_model)

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


# Map a platform name to its concrete implementation
PLATFORMS = {
    "instagram": Instagram,
    "tiktok": TikTok,
}


def account_display_data():
    display_accounts = []
    for account in account_store.list_accounts():
        platform_class = get_platform_class(account["platform"])
        platform = platform_class(account["username"])
        display_accounts.append({
            **account,
            "abbreviation": platform_class.abbreviation,
            "account_url": platform.get_account_url(),
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
        data = collection.get_collection()
        for video_id, metadata in zip(
            data.get("ids", []), data.get("metadatas", []) or []
        ):
            if (
                video_id in source_urls
                and metadata
                and metadata.get("account_id") == account_id
            ):
                loaded_urls.add(video_id)
    except Exception as error:
        print(f"Could not reconcile loaded videos for {account_id}: {error}")
    return loaded_urls & source_urls


def sync_status_payload(platform_name):
    state = get_sync_state(platform_name)
    collection_count = collection.count()
    loaded_urls = loaded_urls_for_account(platform_name, state)
    loaded = len(loaded_urls)
    total = len(state["urls"])
    return {
        "platform": platform_name,
        "status": state["status"],
        "current_url": state["current_url"],
        "last_error": state["last_error"],
        "source_total": total,
        "completed": loaded,
        "loaded": loaded,
        "remaining": max(total - loaded, 0),
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

def connect_platform(platform_name, username):
    """
    Create and log in a concrete platform object.

    platform_name:
        "instagram" or "tiktok"

    Returns:
        The concrete platform object.
    """
    platform_class = get_platform_class(platform_name)

    account = account_store.create_account(platform_name, username)
    platform = platform_class(
        username,
        account_id=account["id"],
        profile_path=account_store.profile_path(account),
        urls_path=account_store.urls_path(account),
    )

    try:
        platform.log_in()
    except Exception:
        # Make sure the browser is closed if login fails
        if platform.driver is not None:
            platform.driver.quit()
        account_store.delete_account(account["id"])
        raise

    return platform


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
    second thread takes URLs off the queue and processes them, so videos
    start appearing in the library while scrolling is still going on.
    """
    state_key = account["id"]
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
            loaded = loaded_urls_for_account(
                state_key, get_sync_state(state_key)
            )
            while not stop_requested():
                try:
                    url = pending.get(timeout=0.5)
                except queue.Empty:
                    if scraping_done.is_set():
                        return
                    continue
                if url in loaded:
                    continue
                update_sync_state(state_key, current_url=url)
                video = processor.process_video(url)
                with sync_lock:
                    state = get_sync_state(state_key)
                    if (
                        video is not None
                        and state["status"] != "cancel_requested"
                    ):
                        collection.add(
                            video,
                            account_id=account["id"],
                            platform=account["platform"],
                        )
                        loaded.add(url)
                        update_sync_state(
                            state_key,
                            loaded_urls=list(
                                dict.fromkeys(state["loaded_urls"] + [url])
                            ),
                            completed_urls=list(
                                dict.fromkeys(state["completed_urls"] + [url])
                            ),
                        )
                update_sync_state(state_key, current_url=None)
        except Exception as error:
            processing_errors.append(error)

    processing_worker = threading.Thread(
        target=process_pending,
        daemon=True,
        name=f"{account['platform']}-{state_key}-process",
    )
    try:
        # URLs from earlier runs that were never processed go first.
        for url in get_sync_state(state_key)["urls"]:
            enqueue(url)
        processing_worker.start()

        platform.log_in()

        def record_url(url):
            with sync_lock:
                state = get_sync_state(state_key)
                if url not in state["urls"]:
                    update_sync_state(state_key, urls=state["urls"] + [url])
            enqueue(url)

        scraped_urls = platform.scrape_videos(
            on_url_found=record_url,
            should_stop=stop_requested,
        )
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
        processing_worker.join()

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
        if processing_worker.is_alive():
            processing_worker.join(timeout=5)
        if shutting_down.is_set():
            # The browser was closed by the app shutting down, not a failure.
            update_sync_state(state_key, status="paused", current_url=None)
            return
        print(f"{account['platform'].capitalize()} sync failed: {error}")
        update_sync_state(
            state_key, status="error", current_url=None, last_error=str(error)
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


def render_profile(search_results=None, query="", has_more=False):
    """
    Render the profile page with the connection state
    of all supported platforms.
    """
    data = collection.get_collection()

    accounts = account_display_data()
    return render_template(
        "profile.html",
        accounts=accounts,
        videos=data["ids"][:num_videos],
        total_videos=len(data["ids"]),
        last_sync="10",
        search_results=search_results,
        query=query,
        has_more=has_more,
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
    return render_profile()


# --------------------------------------------------
# Generic login flow
# --------------------------------------------------

def login_platform(platform_name):
    """
    Generic login flow used by Instagram and TikTok.
    """

    platform_class = get_platform_class(platform_name)
    platform_abbreviation = platform_class.abbreviation

    if request.method == "GET":
        return render_template(
            "login.html",
            platform=platform_name.capitalize(),
            platform_abbreviation=platform_abbreviation,
        )

    entered_username = request.form["username"].strip()

    if not entered_username:
        return render_template(
            "login.html",
            platform=platform_name.capitalize(),
            platform_abbreviation=platform_abbreviation,
            error=f"Please enter your {platform_name.capitalize()} username."
        )

    try:
        platform = connect_platform(
            platform_name,
            entered_username
        )

    except Exception as e:
        print(
            f"{platform_name.capitalize()} login error: {e}"
        )

        return render_template(
            "login.html",
            platform=platform_name.capitalize(),
            platform_abbreviation=platform_abbreviation,
            error=f"Could not open {platform_name.capitalize()}."
        )

    finally:
        # We only need the browser during login.
        if "platform" in locals() and platform.driver is not None:
            platform.driver.quit()

    return redirect(url_for("home"))


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
    )


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
        return render_template(
            "login.html",
            platform=platform.capitalize(),
            platform_abbreviation=PLATFORMS[platform].abbreviation,
            add_account=True,
        )
    return login_platform(platform)

# --------------------------------------------------
# Search
# --------------------------------------------------

@app.route("/search", methods=["POST"])
def search():

    query = request.form["query"].strip()
    offset = max(request.form.get("offset", default=0, type=int), 0)
    page_size = 2

    results = collection.search(query, num_results=offset + page_size + 1)

    # Chroma returns ids nested inside the result
    all_urls = results["ids"][0]
    urls = all_urls[offset:offset + page_size]
    has_more = len(all_urls) > offset + page_size

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        response = render_template(
            "_video_section.html",
            search_results=urls,
            query=query,
            eager=True,
            has_more=has_more,
            search_offset=offset,
            append=offset > 0,
        )
        if offset > 0:
            response = app.make_response(response)
            response.headers["X-Search-Has-More"] = str(has_more).lower()
            response.headers["X-Search-Next-Offset"] = str(
                offset + len(urls)
            )
            return response
        return response

    return render_profile(
        search_results=urls,
        query=query,
        has_more=has_more,
    )


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

    collection.delete_account_videos(
        account_id, account_store.urls_path(account)
    )
    clear_sync_state(account_id)
    account_store.delete_account(account_id)
    if account.get("legacy"):
        account_store.remove_legacy_username(account["platform"])
    return redirect(url_for("accounts_route"))

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
    # Stop the server the same way Ctrl+C does, after this response is sent.
    threading.Timer(0.5, _thread.interrupt_main).start()
    return render_template("stopped.html", **summary)


# --------------------------------------------------
# All videos
# --------------------------------------------------

@app.route("/videos/count")
def videos_count():
    return jsonify({"total_videos": collection.count()})


@app.route("/videos")
def videos():

    data = collection.get_collection()
    offset = max(request.args.get("offset", default=0, type=int), 0)
    limit = min(max(request.args.get("limit", default=num_videos, type=int), 1), 50)
    selected_videos = data["ids"][offset:offset + limit]

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "_video_cards.html",
            videos=selected_videos,
            eager=False
        )

    return render_template(
        "index.html",
        videos=data["ids"]
    )


# --------------------------------------------------
# Start Flask
# --------------------------------------------------

if __name__ == "__main__":
    app.run(debug=False)
