import json
import os
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


def create_account(platform, username):
    platform = platform.lower().strip()
    username = username.strip()
    accounts = list_accounts()
    account = {
        "id": uuid.uuid4().hex,
        "platform": platform,
        "username": username,
    }
    accounts.append(account)
    _write(accounts)
    return account


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


def urls_path(account):
    if account.get("legacy"):
        return os.path.join(ROOT, f"{account['platform']}_urls.json")
    return os.path.join(
        ROOT, f"{account['platform']}_{account['id']}_urls.json"
    )
