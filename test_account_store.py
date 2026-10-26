import json
import threading
import time

import pytest

from src import account_store


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(
        account_store, "ACCOUNTS_PATH", str(tmp_path / "accounts.json")
    )
    monkeypatch.setattr(account_store, "ROOT", str(tmp_path))


def test_failed_write_keeps_previous_file(monkeypatch):
    account_store.create_account("instagram", "alice")

    def broken_dump(value, file, **kwargs):
        file.write('{"partial"')
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(account_store.json, "dump", broken_dump)
        with pytest.raises(OSError):
            account_store.create_account("instagram", "bob")
    with open(account_store.ACCOUNTS_PATH, encoding="utf-8") as file:
        assert [a["username"] for a in json.load(file)] == ["alice"]


def test_concurrent_creates_are_not_lost(monkeypatch):
    real_read = account_store._read

    def slow_read():
        value = real_read()
        time.sleep(0.05)
        return value

    monkeypatch.setattr(account_store, "_read", slow_read)
    threads = [
        threading.Thread(
            target=account_store.create_account, args=("tiktok", f"user{n}")
        )
        for n in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    monkeypatch.setattr(account_store, "_read", real_read)
    assert len(account_store.list_accounts()) == 5


def test_create_account_rejects_duplicate_username():
    account_store.create_account("instagram", "Alice")
    with pytest.raises(ValueError):
        account_store.create_account("instagram", " alice ")
    assert len(account_store.list_accounts()) == 1
    # The same name on another platform is a different account.
    account_store.create_account("tiktok", "alice")
    assert len(account_store.list_accounts()) == 2
