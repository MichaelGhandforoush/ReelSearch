"""How much disk ReelSearch uses, and leftovers that are safe to remove.

Nothing here runs on its own: the accounts page shows the report and the
person chooses what to clean up.

Leftovers are:
- profiles: browser profiles under selenium_profiles that belong to no
  account and no login in progress (removed accounts, abandoned logins).
  Legacy profiles outside selenium_profiles are never looked at.
- info_files: the .info.json files older versions wrote next to downloads.
- media: downloads left in the videos folder, usually by a sync that was
  stopped mid-video. Recent files and videos being loaded are skipped.
- sync_state: sync progress kept for accounts that no longer exist.
- url_files: saved-video lists for accounts that no longer exist.
"""

import json
import os
import re
import time

from src import account_store

CATEGORIES = ("profiles", "info_files", "media", "sync_state", "url_files")
# Downloads newer than this may still be in use by a video being processed.
MEDIA_MIN_AGE_SECONDS = 60 * 60
URLS_FILE = re.compile(r"^(instagram|tiktok)(?:_([0-9a-f]+))?_urls\.json$")


def _size(path):
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for directory, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(directory, name))
            except OSError:
                pass
    return total


def _category(items):
    """items: (path or key, bytes) pairs."""
    return {
        "items": [item for item, _ in items],
        "sizes": [size for _, size in items],
        "count": len(items),
        "bytes": sum(size for _, size in items),
    }


def _orphan_profiles(profiles_root, account_ids, active_login_ids):
    found = []
    if not os.path.isdir(profiles_root):
        return found
    for platform in sorted(os.listdir(profiles_root)):
        platform_dir = os.path.join(profiles_root, platform)
        if not os.path.isdir(platform_dir):
            continue
        for profile_id in sorted(os.listdir(platform_dir)):
            path = os.path.join(platform_dir, profile_id)
            if (
                os.path.isdir(path)
                and profile_id not in account_ids
                and profile_id not in active_login_ids
            ):
                found.append((path, _size(path)))
    return found


def _video_leftovers(videos_dir, busy_urls, now):
    info_files, media = [], []
    if not os.path.isdir(videos_dir):
        return info_files, media
    for name in sorted(os.listdir(videos_dir)):
        path = os.path.join(videos_dir, name)
        if not os.path.isfile(path):
            continue
        if name.endswith(".info.json"):
            info_files.append((path, _size(path)))
            continue
        stat = os.stat(path)
        # yt-dlp can date a download by the server's Last-Modified, so the
        # creation/change time is what says when it was written here.
        written = max(stat.st_mtime, stat.st_ctime)
        media_id = name.split(".", 1)[0]
        if now - written < MEDIA_MIN_AGE_SECONDS or any(
            media_id and media_id in url for url in busy_urls
        ):
            continue
        media.append((path, stat.st_size))
    return info_files, media


def _orphan_url_files(root, accounts):
    account_ids = {account["id"] for account in accounts}
    legacy_platforms = {
        account["platform"] for account in accounts if account.get("legacy")
    }
    found = []
    if not os.path.isdir(root):
        return found
    for name in sorted(os.listdir(root)):
        match = URLS_FILE.match(name)
        path = os.path.join(root, name)
        if not match or not os.path.isfile(path):
            continue
        platform, account_id = match.groups()
        if account_id is None:
            # The list a legacy account keeps; used while one exists.
            if platform in legacy_platforms:
                continue
        elif account_id in account_ids:
            continue
        found.append((path, _size(path)))
    return found


def storage_report(
    accounts,
    sync_states,
    active_login_ids=(),
    busy_urls=(),
    root=None,
    profiles_root=None,
    videos_dir=None,
    library_dir=None,
    now=None,
):
    """Sizes of what ReelSearch keeps on disk and of each leftover category.

    accounts: the account registry. sync_states: the sync state file's
    contents, keyed by account id. active_login_ids: logins in progress,
    whose profiles are in use. busy_urls: videos syncs are loading now.
    """
    root = root or account_store.ROOT
    profiles_root = profiles_root or account_store.PROFILES_ROOT
    videos_dir = videos_dir or os.path.join(root, "videos")
    library_dir = library_dir or os.path.join(root, "chroma_db")
    now = time.time() if now is None else now
    account_ids = {account["id"] for account in accounts}

    info_files, media = _video_leftovers(videos_dir, busy_urls, now)
    categories = {
        "profiles": _category(_orphan_profiles(
            profiles_root, account_ids, set(active_login_ids)
        )),
        "info_files": _category(info_files),
        "media": _category(media),
        "sync_state": _category([
            (key, len(json.dumps(value)))
            for key, value in sorted(sync_states.items())
            if key not in account_ids
        ]),
        "url_files": _category(_orphan_url_files(root, accounts)),
    }
    usage = {
        "library": _size(library_dir) if os.path.isdir(library_dir) else 0,
        "videos": _size(videos_dir) if os.path.isdir(videos_dir) else 0,
        "profiles": (
            _size(profiles_root) if os.path.isdir(profiles_root) else 0
        ),
    }
    return {
        "usage": usage,
        "categories": categories,
        "reclaimable": sum(item["bytes"] for item in categories.values()),
    }


def storage_cleanup(report, categories, clear_sync_state):
    """Remove the leftovers a storage_report found in the chosen categories.

    Only what the report listed is removed, so anything made after it (a
    login that has just started) is left alone. clear_sync_state removes
    one key from the sync state file. Returns what was removed per category
    and the paths or keys that could not be removed.
    """
    unknown = set(categories) - set(CATEGORIES)
    if unknown:
        raise ValueError(f"Unknown storage categories: {sorted(unknown)}")
    removed = {}
    failed = []
    for name in dict.fromkeys(categories):
        count = size = 0
        category = report["categories"][name]
        for item, item_size in zip(category["items"], category["sizes"]):
            try:
                if name == "sync_state":
                    clear_sync_state(item)
                    ok = True
                elif name == "profiles":
                    ok = account_store.remove_folder(item)
                else:
                    os.remove(item)
                    ok = True
            except FileNotFoundError:
                ok = True
            except OSError as error:
                print(f"Could not remove {item}: {error}")
                ok = False
            if ok:
                count += 1
                size += item_size
            else:
                failed.append(item)
        removed[name] = {"count": count, "bytes": size}
    return {"removed": removed, "failed": failed}
