import json
import os
import threading

from conftest import FakeEmbedModel
from src.video import Video


XHR = {"X-Requested-With": "XMLHttpRequest"}


def add_video(app_module, url, word, transcript=""):
    video = Video(url)
    video.transcript = transcript
    video.embedding = FakeEmbedModel().encode(word)
    video.stats = {}
    app_module.collection.add(video)


def card_urls(html):
    return [
        part.split('"', 1)[0]
        for part in html.split('data-video-url="')[1:]
    ]


def test_videos_count_on_an_empty_library(client):
    response = client.get("/videos/count")

    assert response.status_code == 200
    assert response.get_json() == {"total_videos": 0, "matching": 0}


def test_search_with_a_quoted_phrase_returns_only_matching_videos(
    app_module, client,
):
    add_video(
        app_module, "https://www.instagram.com/reel/1/", "car",
        transcript="then we went to the Dog\nPark again",
    )
    add_video(
        app_module, "https://www.instagram.com/reel/2/", "dog",
        transcript="a dog at the beach",
    )
    add_video(app_module, "https://www.tiktok.com/@a/video/3", "dog")

    response = client.post(
        "/search", data={"query": '"dog park"'}, headers=XHR
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert card_urls(html) == ["https://www.instagram.com/reel/1/"]
    # Phrase searches are never split into strong and weak matches.
    assert "Weak match" not in html
    assert "Show weaker matches" not in html


def test_search_holds_back_weak_matches(app_module, client):
    add_video(app_module, "https://www.instagram.com/reel/1/", "dog")
    add_video(app_module, "https://www.instagram.com/reel/2/", "pasta")

    response = client.post("/search", data={"query": "dog"}, headers=XHR)

    html = response.get_data(as_text=True)
    assert card_urls(html) == ["https://www.instagram.com/reel/1/"]
    assert "Show weaker matches" in html

    response = client.post(
        "/search", data={"query": "dog", "weak": "1", "offset": "1"},
        headers=XHR,
    )

    assert card_urls(response.get_data(as_text=True)) == [
        "https://www.instagram.com/reel/2/"
    ]
    assert response.headers["X-Search-Has-More"] == "false"
    assert response.headers["X-Search-Has-Weak"] == "false"


def test_sync_status_for_an_unknown_account_is_404(client):
    response = client.get("/sync/account/unknown/status")

    assert response.status_code == 404
    assert response.get_json() == {"error": "Account not found"}


def test_app_state_stays_out_of_the_repo(app_module, tmp_path):
    assert app_module.sync_state_path == str(tmp_path / "sync_state.json")
    assert app_module.account_store.list_accounts() == []


class RecordingCollection:
    """Wraps a Chroma collection and records the include of every get."""

    def __init__(self, inner):
        self.inner = inner
        self.includes = []

    def get(self, *args, **kwargs):
        self.includes.append(kwargs.get("include"))
        return self.inner.get(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def add_owned_video(app_module, url, word, account_id):
    video = Video(url)
    video.transcript = "a long transcript"
    video.embedding = FakeEmbedModel().encode(word)
    video.stats = {}
    app_module.collection.add(video, account_id=account_id)


def test_status_and_profile_never_load_documents(app_module, client):
    urls = [
        "https://www.instagram.com/reel/1/",
        "https://www.instagram.com/reel/2/",
        "https://www.instagram.com/reel/3/",
    ]
    add_owned_video(app_module, urls[0], "dog", "acc")
    add_owned_video(app_module, urls[1], "car", "acc")
    add_owned_video(app_module, "https://www.tiktok.com/@a/video/9", "dog",
                    "other")
    app_module.update_sync_state("acc", urls=urls)
    recording = RecordingCollection(app_module.collection.collection)
    app_module.collection.collection = recording

    payload = app_module.sync_status_payload("acc")
    response = client.get("/")

    assert payload["loaded"] == 2
    assert payload["remaining"] == 1
    assert response.status_code == 200
    assert recording.includes
    for include in recording.includes:
        assert include is not None and "documents" not in include


def test_profile_facets_describe_the_whole_library_when_filtered(
    app_module, client,
):
    add_owned_video(app_module, "https://www.instagram.com/reel/1/", "dog",
                    "acc")
    add_owned_video(app_module, "https://www.tiktok.com/@a/video/9", "dog",
                    "other")
    seen = []
    search_facets = app_module.search_facets
    app_module.search_facets = lambda metadatas, accounts: (
        seen.append(len(metadatas)) or search_facets(metadatas, accounts)
    )

    client.get("/")
    client.get("/?platform=instagram")

    assert seen == [2, 2]


def test_videos_page_lists_every_id(app_module, client):
    add_video(app_module, "https://www.instagram.com/reel/1/", "dog")

    response = client.get("/videos")

    assert response.status_code == 200
    assert "https://www.instagram.com/reel/1/" in response.get_data(
        as_text=True
    )


class AliveWorker:
    def is_alive(self):
        return True


def test_removing_an_account_removes_its_login_and_url_list(
    app_module, client, tmp_path,
):
    store = app_module.account_store
    account = store.create_account("instagram", "alice")
    other = store.create_account("tiktok", "bob")
    profile = store.profile_path(account)
    other_profile = store.profile_path(other)
    with open(os.path.join(profile, "Cookies"), "w") as file:
        file.write("session")
    urls_path = store.urls_path(account)
    with open(urls_path, "w") as file:
        json.dump(["https://www.instagram.com/reel/1/"], file)
    cookies_path = store.cookies_path(account)
    other_cookies = store.cookies_path(other)
    os.makedirs(os.path.dirname(cookies_path))
    for path in (cookies_path, other_cookies):
        with open(path, "w") as file:
            file.write("# Netscape HTTP Cookie File\n")
    add_owned_video(
        app_module, "https://www.instagram.com/reel/1/", "dog", account["id"]
    )

    response = client.post("/logout", data={"account_id": account["id"]})

    assert response.status_code == 302
    assert not os.path.exists(profile)
    assert not os.path.exists(urls_path)
    assert not os.path.exists(cookies_path)
    assert os.path.exists(other_cookies)
    assert os.path.isdir(other_profile)
    assert [item["id"] for item in store.list_accounts()] == [other["id"]]
    assert app_module.collection.count() == 0
    assert profile.startswith(str(tmp_path))


def test_turning_off_download_with_login_deletes_the_saved_login(
    app_module, client,
):
    store = app_module.account_store
    account = store.create_account("instagram", "alice")
    headers = {"X-Requested-With": "XMLHttpRequest"}

    response = client.post(
        f"/accounts/{account['id']}/settings",
        data={"download_with_login": "1"}, headers=headers,
    )
    assert response.get_json()["download_with_login"] is True
    cookies_path = store.cookies_path(account)
    os.makedirs(os.path.dirname(cookies_path))
    with open(cookies_path, "w") as file:
        file.write("# Netscape HTTP Cookie File\n")

    response = client.post(
        f"/accounts/{account['id']}/settings", data={}, headers=headers,
    )

    assert response.get_json()["download_with_login"] is False
    assert not os.path.exists(cookies_path)


def test_storage_report_route_lists_counts_not_paths(
    app_module, client, tmp_path,
):
    store = app_module.account_store
    account = store.create_account("instagram", "alice")
    store.profile_path(account)
    orphan = tmp_path / "selenium_profiles" / "tiktok" / "cc33"
    orphan.mkdir(parents=True)
    (orphan / "Cookies").write_text("abc", encoding="utf-8")
    videos = tmp_path / "videos"
    videos.mkdir()
    (videos / "C1.info.json").write_text("{}", encoding="utf-8")
    app_module.update_sync_state("instagram", status="idle")

    response = client.get("/storage")

    assert response.status_code == 200
    report = response.get_json()
    assert report["categories"]["profiles"] == {"count": 1, "bytes": 3}
    assert report["categories"]["info_files"]["count"] == 1
    assert report["categories"]["sync_state"]["count"] == 1
    assert report["busy"] is False
    assert "items" not in report["categories"]["profiles"]


def test_storage_cleanup_removes_the_chosen_leftovers(
    app_module, client, tmp_path,
):
    store = app_module.account_store
    account = store.create_account("instagram", "alice")
    live = store.profile_path(account)
    orphan = tmp_path / "selenium_profiles" / "tiktok" / "cc33"
    orphan.mkdir(parents=True)
    app_module.update_sync_state(account["id"], status="idle")
    app_module.update_sync_state("tiktok", status="idle")

    response = client.post(
        "/storage/cleanup", data={"category": ["profiles", "sync_state"]}
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["failed"] == []
    assert body["removed"]["profiles"]["count"] == 1
    assert body["storage"]["reclaimable"] == 0
    assert not orphan.exists()
    assert os.path.isdir(live)
    assert list(app_module.load_sync_states()) == [account["id"]]


def test_storage_cleanup_needs_a_category(client):
    assert client.post("/storage/cleanup").status_code == 400
    response = client.post("/storage/cleanup", data={"category": "all"})
    assert response.status_code == 400


def test_storage_cleanup_refuses_while_a_sync_or_login_runs(
    app_module, client, tmp_path, monkeypatch,
):
    orphan = tmp_path / "selenium_profiles" / "tiktok" / "cc33"
    orphan.mkdir(parents=True)
    monkeypatch.setitem(app_module.sync_workers, "someone", AliveWorker())

    response = client.post("/storage/cleanup", data={"category": "profiles"})

    assert response.status_code == 409
    assert orphan.is_dir()
    assert client.get("/storage").get_json()["busy"] is True

    app_module.sync_workers.pop("someone")
    monkeypatch.setattr(
        app_module.login_sessions, "active_ids", lambda: {"cc33"}
    )

    response = client.post("/storage/cleanup", data={"category": "profiles"})

    assert response.status_code == 409
    assert orphan.is_dir()


def test_sync_cannot_start_while_its_account_is_being_removed(
    app_module, client, monkeypatch,
):
    account = app_module.account_store.create_account("instagram", "alice")
    monkeypatch.setattr(app_module, "create_platform", lambda account_id: None)
    monkeypatch.setattr(app_module, "sync_platform", lambda *args: None)
    outcome = {}

    class RacingWorker:
        alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            # A sync request arrives while logout waits for the worker.
            self.alive = False
            try:
                outcome["action"] = app_module.start_sync(account["id"])
            except ValueError as error:
                outcome["error"] = str(error)

    monkeypatch.setitem(
        app_module.sync_workers, account["id"], RacingWorker()
    )

    response = client.post("/logout", data={"account_id": account["id"]})

    assert response.status_code == 302
    assert "action" not in outcome
    assert "removed" in outcome["error"]
    # Once the removal is over the id is free again.
    assert account["id"] not in app_module.removing_accounts


def test_sync_cannot_start_while_storage_cleanup_deletes_files(
    app_module, client, monkeypatch,
):
    account = app_module.account_store.create_account("instagram", "alice")
    monkeypatch.setattr(app_module, "create_platform", lambda account_id: None)
    monkeypatch.setattr(app_module, "sync_platform", lambda *args: None)
    started_during = []

    def cleanup_with_a_sync_request(report, categories, clear_sync_state):
        # A sync request arrives while the cleanup is deleting files.
        request = threading.Thread(
            target=lambda: app_module.start_sync(account["id"])
        )
        request.start()
        request.join(0.5)
        started_during.append(not request.is_alive())
        return {"removed": {}, "failed": []}

    monkeypatch.setattr(
        app_module, "storage_cleanup", cleanup_with_a_sync_request
    )

    response = client.post("/storage/cleanup", data={"category": "profiles"})

    assert response.status_code == 200
    assert started_during == [False]


def test_shutdown_closes_logins_while_syncs_are_stopping(
    app_module, client, monkeypatch,
):
    """Login browsers close in parallel with the sync wait, not after it."""
    logins_closing = threading.Event()
    seen = {}

    def slow_stop_syncs():
        seen["logins_closed_during_sync_wait"] = logins_closing.wait(2)
        return {"paused": 0, "forced": 0}

    monkeypatch.setattr(app_module, "stop_syncs", slow_stop_syncs)
    monkeypatch.setattr(
        app_module.login_sessions, "shutdown",
        lambda *args, **kwargs: logins_closing.set(),
    )
    monkeypatch.setattr(
        app_module.threading, "Timer",
        lambda *args, **kwargs: type("T", (), {"start": lambda self: None})(),
    )

    response = client.post("/shutdown")

    assert response.status_code == 200
    assert seen["logins_closed_during_sync_wait"] is True


def add_video_added_at(app_module, url, word, added_at, account_id=None):
    add_owned_video(app_module, url, word, account_id)
    app_module.collection._merge_metadata({url: {"added_at": added_at}})


def test_videos_filter_by_added_after_and_sort_by_added(app_module, client):
    add_video_added_at(app_module, "https://www.instagram.com/reel/1/",
                       "dog", 1000.0)
    add_video_added_at(app_module, "https://www.instagram.com/reel/2/",
                       "dog", 3000.0)
    add_video_added_at(app_module, "https://www.instagram.com/reel/3/",
                       "dog", 2000.0)
    # Saved before added_at was recorded: never counts as new.
    app_module.collection.collection.add(
        ids=["https://www.instagram.com/reel/4/"],
        embeddings=[[0.0, 1.0, 0.0]], documents=["old"],
        metadatas=[{"platform": "instagram"}],
    )

    new = client.get("/videos?added_after=2000&limit=10", headers=XHR)
    everything = client.get("/videos?sort=added&limit=10", headers=XHR)
    ignored = client.get("/videos?added_after=soon&limit=10", headers=XHR)

    assert sorted(card_urls(new.get_data(as_text=True))) == [
        "https://www.instagram.com/reel/2/",
        "https://www.instagram.com/reel/3/",
    ]
    assert card_urls(everything.get_data(as_text=True)) == [
        "https://www.instagram.com/reel/2/",
        "https://www.instagram.com/reel/3/",
        "https://www.instagram.com/reel/1/",
        "https://www.instagram.com/reel/4/",
    ]
    assert len(card_urls(ignored.get_data(as_text=True))) == 4


def test_home_shows_the_added_after_chip(app_module, client):
    response = client.get("/?added_after=1700000000")

    html = response.get_data(as_text=True)
    assert 'data-added-after="1700000000"' in html
    assert "Added since" in html


def test_last_sync_is_when_the_last_run_finished_not_updated_at(
    app_module, monkeypatch,
):
    from datetime import datetime

    finished = datetime(2024, 3, 5, 14, 2).timestamp()
    app_module.update_sync_state("acc", last_run={
        "started_at": finished - 60, "finished_at": finished,
        "status": "completed", "added": 12, "failed": 3, "skipped": 0,
    })
    # A later state write, like the auto-pause of a dead worker.
    app_module.update_sync_state("acc", current_url=None)

    data = app_module.sync_display_data("acc")

    assert data["last_sync"] == "05 Mar 2024, 14:02"
    assert data["last_run_summary"] == "12 new videos, 3 couldn't load"
    assert data["last_run"]["new_videos_url"] == (
        f"/?added_after={int(finished - 60)}&account=acc"
    )


def test_last_sync_is_never_without_a_finished_run(app_module):
    app_module.update_sync_state("acc", status="paused")

    data = app_module.sync_display_data("acc")

    assert data["last_sync"] == "Never"
    assert data["last_run"] is None
