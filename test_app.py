import json
import os

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
    add_owned_video(
        app_module, "https://www.instagram.com/reel/1/", "dog", account["id"]
    )

    response = client.post("/logout", data={"account_id": account["id"]})

    assert response.status_code == 302
    assert not os.path.exists(profile)
    assert not os.path.exists(urls_path)
    assert os.path.isdir(other_profile)
    assert [item["id"] for item in store.list_accounts()] == [other["id"]]
    assert app_module.collection.count() == 0
    assert profile.startswith(str(tmp_path))


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
