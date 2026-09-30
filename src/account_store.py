import json
import os
import shutil
import time
import uuid


ROOT = os.path.dirname(os.path.dirname(__file__))
ACCOUNTS_PATH = os.path.join(ROOT, "accounts.json")
PROFILES_ROOT = os.path.join(ROOT, "selenium_profiles")


def _read():
    if not os.path.exists(ACCOUNTS_PATH):
        return []
    try:
        with open(ACCOUNTS_PATH, "r", encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read account registry: {error}") from error


def _write(accounts):
    with open(ACCOUNTS_PATH, "w", encoding="utf-8") as file:
        json.dump(accounts, file, indent=2)


def _legacy_username(platform):
    path = os.path.join(ROOT, f"{platform}_username.txt")
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as file:
        return file.read().strip()


def remove_legacy_username(platform):
    path = os.path.join(ROOT, f"{platform}_username.txt")
    if os.path.exists(path):
        os.remove(path)


def ensure_migrated():
    accounts = _read()
    changed = False
    for platform in ("instagram", "tiktok"):
        username = _legacy_username(platform)
        if username and not any(
            account["platform"] == platform
            and account["username"].lower() == username.lower()
            for account in accounts
        ):
            accounts.append({
                "id": uuid.uuid4().hex,
                "platform": platform,
                "username": username,
                "legacy": True,
            })
            changed = True
    if changed:
        _write(accounts)
    return accounts


def list_accounts():
    return ensure_migrated()


def get_account(account_id):
    return next(
        (account for account in list_accounts() if account["id"] == account_id),
        None,
    )


def accounts_for_platform(platform):
    return [
        account for account in list_accounts()
        if account["platform"] == platform.lower()
    ]


def find_account(platform, username):
    """The connected account for this platform and username, if any."""
    return next(
        (
            account for account in accounts_for_platform(platform)
            if account["username"].lower() == username.strip().lower()
        ),
        None,
    )


def create_account(platform, username, account_id=None):
    platform = platform.lower().strip()
    username = username.strip()
    accounts = list_accounts()
    account = {
        "id": account_id or uuid.uuid4().hex,
        "platform": platform,
        "username": username,
    }
    accounts.append(account)
    _write(accounts)
    return account


# Per-account preferences and their defaults. Accounts saved before a
# setting existed use its default.
SETTING_DEFAULTS = {
    # "all" scrolls the whole saved page on every sync, catching videos an
    # earlier sync missed; "new" stops once it reaches already-known videos.
    "sync_mode": "all",
    # How aggressively videos are downloaded, as src.processor LOAD_SPEEDS:
    # slow is least likely to be timed out by the platform, fast is quickest.
    "load_speed": "medium",
    # Set aside videos earlier syncs found but never loaded, so syncs only
    # load the videos they find themselves.
    "skip_backlog": False,
    # What goes into a video's searchable text, as src.processor FEATURES.
    # Applies to videos processed after the change.
    "use_transcript": True,
    "use_ocr": True,
    "use_comments": True,
    "use_visual_description": False,
}
SETTING_CHOICES = {
    "sync_mode": ("all", "new"),
    "load_speed": ("slow", "medium", "fast"),
    "skip_backlog": (True, False),
    "use_transcript": (True, False),
    "use_ocr": (True, False),
    "use_comments": (True, False),
    "use_visual_description": (True, False),
}


def account_settings(account):
    settings = dict(SETTING_DEFAULTS)
    for name, value in (account.get("settings") or {}).items():
        if name in SETTING_CHOICES and value in SETTING_CHOICES[name]:
            settings[name] = value
    return settings


def update_settings(account_id, **changes):
    """Save changed settings for an account and return all of its settings."""
    for name, value in changes.items():
        if name not in SETTING_CHOICES or value not in SETTING_CHOICES[name]:
            raise ValueError(f"Invalid value for {name}: {value!r}")
    accounts = list_accounts()
    account = next(
        (item for item in accounts if item["id"] == account_id), None
    )
    if account is None:
        raise ValueError("Account not found.")
    account["settings"] = {**account_settings(account), **changes}
    _write(accounts)
    return account["settings"]


def delete_account(account_id):
    accounts = list_accounts()
    account = get_account(account_id)
    if account is None:
        return None
    _write([item for item in accounts if item["id"] != account_id])
    return account


def profile_path(account):
    if account.get("legacy"):
        return {
            "instagram": r"C:\selenium\instagram_profile",
            "tiktok": r"C:\selenium\tiktok_profile",
        }[account["platform"]]
    path = os.path.join(PROFILES_ROOT, account["platform"], account["id"])
    os.makedirs(path, exist_ok=True)
    return path


def remove_profile(account):
    """Delete an account's browser profile, and with it its login.

    Returns False if the folder is still there afterwards. Legacy profiles
    live outside the app folder and are never touched.
    """
    if account.get("legacy"):
        return True
    return remove_folder(
        os.path.join(PROFILES_ROOT, account["platform"], account["id"])
    )


def remove_folder(path):
    """Delete a browser profile folder; False if it could not be removed."""
    # Chrome can take a moment to let go of its files after it closes.
    for _ in range(6):
        if not os.path.exists(path):
            return True
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return True
        time.sleep(0.5)
    return not os.path.exists(path)


def urls_path(account):
    if account.get("legacy"):
        return os.path.join(ROOT, f"{account['platform']}_urls.json")
    return os.path.join(
        ROOT, f"{account['platform']}_{account['id']}_urls.json"
    )
