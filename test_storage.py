import os
import time

import pytest

from src import account_store, storage
from src.storage import storage_cleanup, storage_report


LIVE = {"id": "aa11", "platform": "instagram", "username": "alice"}
LEGACY = {
    "id": "ff66", "platform": "instagram", "username": "old", "legacy": True,
}
LOGIN_ID = "bb22"


def write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path):
    """A data folder with one live account, a legacy account, a login in
    progress, and a leftover in every category."""
    profiles = tmp_path / "selenium_profiles"
    videos = tmp_path / "videos"
    write(profiles / "instagram" / "aa11" / "Default" / "Cookies")
    write(profiles / "tiktok" / LOGIN_ID / "Default" / "Cookies")
    write(profiles / "instagram" / "cc33" / "Default" / "Cookies", "abcd")
    write(profiles / "tiktok" / "dd44" / "Local State", "ab")
    write(videos / "C1.info.json", "{}")
    write(videos / "C1.mp4", "video")
    write(videos / "BUSY.mp4", "video")
    write(tmp_path / "instagram_aa11_urls.json", "[]")
    write(tmp_path / "tiktok_ee55_urls.json", "[]")
    write(tmp_path / "instagram_urls.json", "[]")
    write(tmp_path / "tiktok_urls.json", "[]")
    write(tmp_path / "accounts.json", "[]")
    return {
        "accounts": [LIVE, LEGACY],
        "sync_states": {
            "aa11": {"status": "idle"},
            "instagram": {"urls": ["u"]},
            "tiktok": {"urls": []},
        },
        "active_login_ids": {LOGIN_ID},
        "busy_urls": ["https://www.instagram.com/reel/BUSY/"],
        "root": str(tmp_path),
        "profiles_root": str(profiles),
        "videos_dir": str(videos),
        "library_dir": str(tmp_path / "chroma_db"),
        # Everything written above counts as more than an hour old.
        "now": time.time() + 2 * storage.MEDIA_MIN_AGE_SECONDS,
    }


def names(category):
    return sorted(os.path.basename(item) for item in category["items"])


def test_report_finds_exactly_the_seeded_leftovers(tree, tmp_path):
    report = storage_report(**tree)
    categories = report["categories"]

    # Not the live account's profile or the one a login is using.
    assert names(categories["profiles"]) == ["cc33", "dd44"]
    assert categories["profiles"]["bytes"] == 6
    assert names(categories["info_files"]) == ["C1.info.json"]
    # Not the video a sync is loading.
    assert names(categories["media"]) == ["C1.mp4"]
    assert categories["sync_state"]["items"] == ["instagram", "tiktok"]
    # The legacy list stays while a legacy Instagram account exists.
    assert names(categories["url_files"]) == [
        "tiktok_ee55_urls.json", "tiktok_urls.json",
    ]
    for item in categories["profiles"]["items"]:
        assert item.startswith(tree["profiles_root"])
    assert report["reclaimable"] == sum(
        category["bytes"] for category in categories.values()
    )
    assert report["usage"]["profiles"] == 1 + 1 + 4 + 2
    assert report["usage"]["library"] == 0


def test_report_skips_recent_downloads(tree):
    tree["now"] = time.time()

    report = storage_report(**tree)

    assert report["categories"]["media"]["count"] == 0
    # Info files are never read, however new they are.
    assert report["categories"]["info_files"]["count"] == 1


def test_legacy_url_list_is_a_leftover_once_no_legacy_account_exists(tree):
    tree["accounts"] = [LIVE]

    report = storage_report(**tree)

    assert "instagram_urls.json" in names(report["categories"]["url_files"])


def test_report_on_a_missing_folder_is_empty(tmp_path):
    report = storage_report(
        accounts=[], sync_states={}, root=str(tmp_path / "none"),
        profiles_root=str(tmp_path / "none" / "profiles"),
    )

    assert report["reclaimable"] == 0
    assert all(
        category["count"] == 0 for category in report["categories"].values()
    )


def test_cleanup_removes_only_the_chosen_leftovers(tree, tmp_path):
    report = storage_report(**tree)
    cleared = []

    result = storage_cleanup(
        report, ["profiles", "info_files", "sync_state", "url_files"],
        cleared.append,
    )

    assert result["failed"] == []
    assert result["removed"]["profiles"] == {"count": 2, "bytes": 6}
    assert cleared == ["instagram", "tiktok"]
    profiles = tmp_path / "selenium_profiles"
    assert (profiles / "instagram" / "aa11").is_dir()
    assert (profiles / "tiktok" / LOGIN_ID).is_dir()
    assert not (profiles / "instagram" / "cc33").exists()
    assert not (profiles / "tiktok" / "dd44").exists()
    assert not (tmp_path / "videos" / "C1.info.json").exists()
    # Media was not chosen.
    assert (tmp_path / "videos" / "C1.mp4").exists()
    assert (tmp_path / "videos" / "BUSY.mp4").exists()
    assert (tmp_path / "instagram_aa11_urls.json").exists()
    assert (tmp_path / "instagram_urls.json").exists()
    assert not (tmp_path / "tiktok_urls.json").exists()
    assert (tmp_path / "accounts.json").exists()

    remaining = storage_report(**tree)["categories"]
    assert remaining["profiles"]["count"] == 0
    assert remaining["media"]["count"] == 1


def test_cleanup_leaves_what_appeared_after_the_report(tree, tmp_path):
    report = storage_report(**tree)
    late = tmp_path / "selenium_profiles" / "tiktok" / "ab12"
    write(late / "Local State")

    storage_cleanup(report, ["profiles"], lambda key: None)

    assert late.is_dir()


def test_cleanup_reports_profiles_it_could_not_remove(tree, monkeypatch):
    report = storage_report(**tree)
    monkeypatch.setattr(
        account_store, "remove_folder",
        lambda path: not path.endswith("cc33"),
    )

    result = storage_cleanup(report, ["profiles"], lambda key: None)

    assert [os.path.basename(path) for path in result["failed"]] == ["cc33"]
    assert result["removed"]["profiles"]["count"] == 1


def test_cleanup_refuses_unknown_categories(tree):
    with pytest.raises(ValueError):
        storage_cleanup(storage_report(**tree), ["everything"], print)


def test_remove_profile_never_touches_legacy_profiles(monkeypatch):
    calls = []
    monkeypatch.setattr(account_store, "remove_folder", calls.append)

    assert account_store.remove_profile(LEGACY) is True
    assert calls == []
