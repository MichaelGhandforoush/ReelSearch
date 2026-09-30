import os
import time

import pytest
from selenium.common.exceptions import NoSuchWindowException

from src import account_store, login_sessions
from src.login_sessions import LoginError, LoginSessions


class FakeDriver:
    def __init__(self):
        self.quit_called = False

    def quit(self):
        self.quit_called = True


class FakePlatform:
    """A platform whose 'browser' logs in, closes or fails on command."""

    instances = []
    fail_open = False

    def __init__(self, username, account_id, profile_path, urls_path):
        self.username = username
        self.account_id = account_id
        self.profile_path = profile_path
        self.driver = None
        self.logged_in = False
        self.closed = False
        FakePlatform.instances.append(self)

    def open_login(self):
        if FakePlatform.fail_open:
            raise RuntimeError("chromedriver not found\n  stacktrace line")
        self.driver = FakeDriver()
        self.first_driver = self.driver

    def is_logged_in(self):
        if self.closed:
            raise NoSuchWindowException("target window already closed")
        return self.logged_in


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(account_store, "ROOT", str(tmp_path))
    monkeypatch.setattr(
        account_store, "ACCOUNTS_PATH", str(tmp_path / "accounts.json")
    )
    monkeypatch.setattr(
        account_store, "PROFILES_ROOT", str(tmp_path / "profiles")
    )
    monkeypatch.setattr(login_sessions, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(login_sessions, "LOGGED_IN_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(login_sessions, "ABANDON_AFTER_SECONDS", 5)
    FakePlatform.instances = []
    FakePlatform.fail_open = False


@pytest.fixture
def sessions():
    manager = LoginSessions({"instagram": FakePlatform})
    yield manager
    manager.shutdown(timeout=2)


def wait_for(session, statuses, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.status in statuses:
            return session.status
        time.sleep(0.01)
    return session.status


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def profile_dir(session):
    return os.path.join(
        account_store.PROFILES_ROOT, session.platform_name, session.id
    )


def test_account_is_not_saved_while_the_login_is_open(sessions):
    session = sessions.start("instagram", "alice")

    assert wait_until(lambda: session.status == "waiting")
    time.sleep(0.2)

    assert session.status == "waiting"
    assert account_store.list_accounts() == []


def test_account_is_saved_when_the_login_finishes_by_itself(sessions):
    session = sessions.start("instagram", "alice")
    assert wait_until(lambda: session.status == "waiting")
    platform = FakePlatform.instances[0]

    platform.logged_in = True

    assert wait_for(session, {"connected"}) == "connected"
    accounts = account_store.list_accounts()
    assert [(a["platform"], a["username"]) for a in accounts] == [
        ("instagram", "alice")
    ]
    # The account keeps the id (and so the browser profile) made for the login.
    assert accounts[0]["id"] == session.id
    assert platform.first_driver.quit_called


def test_continue_connects_as_soon_as_the_login_is_there(sessions, monkeypatch):
    monkeypatch.setattr(login_sessions, "LOGGED_IN_GRACE_SECONDS", 60)
    session = sessions.start("instagram", "alice")
    assert wait_until(lambda: session.status == "waiting")
    FakePlatform.instances[0].logged_in = True
    time.sleep(0.1)
    assert session.status == "waiting"

    sessions.confirm(session.id)

    assert wait_for(session, {"connected"}) == "connected"


def test_continue_before_logging_in_explains_and_keeps_waiting(sessions):
    session = sessions.start("instagram", "alice")
    assert wait_until(lambda: session.status == "waiting")

    sessions.confirm(session.id)

    assert wait_until(lambda: session.notice is not None)
    assert "not logged in" in session.notice
    assert session.status == "waiting"
    assert account_store.list_accounts() == []

    FakePlatform.instances[0].logged_in = True
    assert wait_for(session, {"connected"}) == "connected"
    assert session.notice is None


def test_cancel_saves_nothing_and_removes_the_profile(sessions):
    session = sessions.start("instagram", "alice")
    assert wait_until(lambda: session.status == "waiting")
    assert os.path.isdir(profile_dir(session))

    sessions.cancel(session.id)

    assert wait_for(session, {"cancelled"}) == "cancelled"
    assert account_store.list_accounts() == []
    assert not os.path.exists(profile_dir(session))
    assert FakePlatform.instances[0].first_driver.quit_called


def test_leaving_the_page_abandons_the_login(sessions, monkeypatch):
    monkeypatch.setattr(login_sessions, "ABANDON_AFTER_SECONDS", 0.15)
    session = sessions.start("instagram", "alice")

    # Nothing polls the session, as when the page is closed.
    assert wait_for(session, {"cancelled"}) == "cancelled"
    assert account_store.list_accounts() == []
    assert not os.path.exists(profile_dir(session))


def test_a_page_that_keeps_checking_in_is_not_abandoned(sessions, monkeypatch):
    monkeypatch.setattr(login_sessions, "ABANDON_AFTER_SECONDS", 0.3)
    session = sessions.start("instagram", "alice")

    for _ in range(25):
        sessions.touch(session.id)
        time.sleep(0.02)

    assert session.status == "waiting"


def test_closing_the_browser_fails_the_login(sessions):
    session = sessions.start("instagram", "alice")
    assert wait_until(lambda: session.status == "waiting")

    FakePlatform.instances[0].closed = True

    assert wait_for(session, {"failed"}) == "failed"
    assert "browser window was closed" in session.error
    assert account_store.list_accounts() == []
    assert not os.path.exists(profile_dir(session))


def test_a_browser_that_will_not_open_fails_the_login(sessions):
    FakePlatform.fail_open = True

    session = sessions.start("instagram", "alice")

    assert wait_for(session, {"failed"}) == "failed"
    assert "Could not open Instagram" in session.error
    assert "chromedriver not found" in session.error
    assert "stacktrace" not in session.error
    assert account_store.list_accounts() == []
    assert not os.path.exists(profile_dir(session))


def test_login_times_out(sessions, monkeypatch):
    monkeypatch.setattr(login_sessions, "LOGIN_TIMEOUT_SECONDS", 0.15)
    session = sessions.start("instagram", "alice")
    session.last_seen = time.monotonic() + 100

    assert wait_for(session, {"failed"}) == "failed"
    assert "not finished" in session.error
    assert account_store.list_accounts() == []


def test_already_connected_account_is_refused(sessions):
    account_store.create_account("instagram", "Alice")

    with pytest.raises(LoginError, match="already connected"):
        sessions.start("instagram", "@alice")

    assert FakePlatform.instances == []


@pytest.mark.parametrize("username", ["", "   ", "@", "two words"])
def test_bad_usernames_are_refused(sessions, username):
    with pytest.raises(LoginError):
        sessions.start("instagram", username)


def test_leading_at_sign_is_dropped(sessions):
    session = sessions.start("instagram", "  @alice ")

    assert session.username == "alice"


def test_pressing_continue_twice_reuses_the_open_login(sessions):
    first = sessions.start("instagram", "alice")
    second = sessions.start("instagram", "Alice")

    assert second is first
    assert wait_until(lambda: first.status == "waiting")
    assert len(FakePlatform.instances) == 1


def test_unknown_platform_is_refused(sessions):
    with pytest.raises(LoginError):
        sessions.start("myspace", "alice")


def test_shutdown_closes_open_browsers_without_saving(sessions):
    session = sessions.start("instagram", "alice")
    assert wait_until(lambda: session.status == "waiting")

    sessions.shutdown(timeout=2)

    assert session.status == "cancelled"
    assert account_store.list_accounts() == []
    assert FakePlatform.instances[0].first_driver.quit_called


def test_active_ids_lists_only_logins_in_progress(sessions):
    session = sessions.start("instagram", "alice")
    assert wait_until(lambda: session.status == "waiting")

    assert sessions.active_ids() == {session.id}

    sessions.cancel(session.id)
    assert wait_for(session, {"cancelled"}) == "cancelled"
    assert sessions.active_ids() == set()
