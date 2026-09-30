"""Logins that are still in progress.

An account is only added to the registry once its login has really
finished, so opening the login page, closing the browser or going back to the
accounts list never leaves half a connection behind. Each pending login owns
one browser window, driven from its own thread: it is finished when the
platform's session cookie appears (or the person presses Continue and it is
there), and thrown away, profile and all, when the person cancels or leaves,
the browser is closed, or too long passes.
"""

import shutil
import threading
import time
import uuid

from src import account_store
from src.base_platform import Platform

LOGIN_TIMEOUT_SECONDS = 10 * 60
# The waiting page checks in about every second and a half. Browsers slow
# timers on tabs in the background, so allow a good while without one before
# deciding the person has left.
ABANDON_AFTER_SECONDS = 90
# After the login cookie shows up, wait a moment so the browser can finish
# writing it and any last screen (such as a two-step prompt) is not cut off.
LOGGED_IN_GRACE_SECONDS = 3
POLL_SECONDS = 1.0
KEEP_FINISHED_SECONDS = 5 * 60

FINISHED = {"connected", "failed", "cancelled"}
NOT_LOGGED_IN_NOTICE = (
    "You are not logged in yet. Finish logging in in the browser window, "
    "then press Continue again."
)


class LoginError(ValueError):
    """The login cannot be started; the message is safe to show."""


def _short(error):
    """First line of an exception, without Selenium's stack trace."""
    lines = str(error).strip().splitlines()
    text = lines[0].strip() if lines else ""
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


class LoginSession:
    def __init__(self, platform_name, username):
        # Also the id the account will get, so the browser profile made for
        # the login is the one the account keeps.
        self.id = uuid.uuid4().hex
        self.platform_name = platform_name
        self.username = username
        self.platform = None
        self.status = "opening"
        self.error = None
        self.notice = None
        self.account = None
        self.created_at = time.monotonic()
        self.last_seen = self.created_at
        self.finished_at = None
        self.cancel_event = threading.Event()
        self.confirm_event = threading.Event()
        self.thread = None

    @property
    def finished(self):
        return self.status in FINISHED

    def snapshot(self):
        return {
            "status": self.status,
            "error": self.error,
            "notice": self.notice,
            "username": self.username,
        }

    def stub(self):
        return {"platform": self.platform_name, "id": self.id}


class LoginSessions:
    def __init__(self, platforms):
        self.platforms = platforms
        self._sessions = {}
        self._lock = threading.Lock()

    # ---- Starting and looking up ----

    def start(self, platform_name, username):
        platform_name = platform_name.lower()
        if platform_name not in self.platforms:
            raise LoginError("Choose a supported platform.")
        label = platform_name.capitalize()
        username = (username or "").strip().lstrip("@").strip()
        if not username:
            raise LoginError(f"Please enter your {label} username.")
        if any(char.isspace() for char in username):
            raise LoginError(f"{label} usernames cannot contain spaces.")

        with self._lock:
            self._purge()
            if account_store.find_account(platform_name, username):
                raise LoginError(
                    f"@{username} is already connected on {label}."
                )
            # A second press of Continue returns to the login already open.
            for session in self._sessions.values():
                if (
                    not session.finished
                    and session.platform_name == platform_name
                    and session.username.lower() == username.lower()
                ):
                    session.last_seen = time.monotonic()
                    return session
            session = LoginSession(platform_name, username)
            self._sessions[session.id] = session
        session.thread = threading.Thread(
            target=self._run,
            args=(session,),
            daemon=True,
            name=f"{platform_name}-login-{session.id[:8]}",
        )
        session.thread.start()
        return session

    def get(self, session_id):
        with self._lock:
            return self._sessions.get(session_id)

    def active_ids(self):
        """Ids of logins still in progress; their browser profiles are in use."""
        with self._lock:
            return {
                session_id for session_id, session in self._sessions.items()
                if not session.finished
            }

    def touch(self, session_id):
        """Look a session up and record that its page is still open."""
        session = self.get(session_id)
        if session is not None:
            session.last_seen = time.monotonic()
        return session

    def confirm(self, session_id):
        session = self.touch(session_id)
        if session is not None and not session.finished:
            session.confirm_event.set()
        return session

    def cancel(self, session_id):
        session = self.get(session_id)
        if session is not None and not session.finished:
            session.cancel_event.set()
        return session

    def shutdown(self, timeout=10):
        """Close every browser still open for a login."""
        with self._lock:
            sessions = [s for s in self._sessions.values() if not s.finished]
        for session in sessions:
            session.cancel_event.set()
        deadline = time.monotonic() + timeout
        for session in sessions:
            if session.thread is not None:
                session.thread.join(max(deadline - time.monotonic(), 0))

    def _purge(self):
        now = time.monotonic()
        for session_id in [
            session_id for session_id, session in self._sessions.items()
            if session.finished
            and now - (session.finished_at or now) > KEEP_FINISHED_SECONDS
        ]:
            del self._sessions[session_id]

    # ---- The login itself ----

    def _run(self, session):
        label = session.platform_name.capitalize()
        try:
            platform_class = self.platforms[session.platform_name]
            session.platform = platform_class(
                session.username,
                account_id=session.id,
                profile_path=account_store.profile_path(session.stub()),
                urls_path=account_store.urls_path(session.stub()),
            )
            session.platform.open_login()
        except Exception as error:
            print(f"Could not open {label} for {session.username}: {error}")
            self._finish(
                session, "failed",
                f"Could not open {label} in a browser window "
                f"({_short(error)}).",
            )
            return

        session.status = "waiting"
        logged_in_since = None
        while True:
            if session.cancel_event.wait(POLL_SECONDS):
                self._finish(session, "cancelled")
                return
            now = time.monotonic()
            if now - session.last_seen > ABANDON_AFTER_SECONDS:
                print(f"{label} login for {session.username}: page closed.")
                self._finish(session, "cancelled")
                return
            if now - session.created_at > LOGIN_TIMEOUT_SECONDS:
                self._finish(
                    session, "failed",
                    f"The {label} login was not finished within "
                    f"{LOGIN_TIMEOUT_SECONDS // 60} minutes.",
                )
                return

            try:
                logged_in = bool(session.platform.is_logged_in())
            except Exception as error:
                if Platform.is_browser_lost(error):
                    self._finish(
                        session, "failed",
                        "The browser window was closed before the login "
                        "finished.",
                    )
                    return
                # A page that is mid-load says nothing about the login.
                logged_in = False

            confirmed = session.confirm_event.is_set()
            if logged_in:
                session.notice = None
                logged_in_since = logged_in_since or now
                if confirmed or now - logged_in_since >= LOGGED_IN_GRACE_SECONDS:
                    self._connect(session)
                    return
            else:
                logged_in_since = None
                if confirmed:
                    session.confirm_event.clear()
                    session.notice = NOT_LOGGED_IN_NOTICE

    def _connect(self, session):
        label = session.platform_name.capitalize()
        # Closing the browser lets it write the login into the profile.
        self._quit(session)
        try:
            session.account = account_store.create_account(
                session.platform_name,
                session.username,
                account_id=session.id,
            )
        except Exception as error:
            self._finish(
                session, "failed",
                f"The {label} login worked but the account could not be "
                f"saved ({_short(error)}).",
            )
            return
        session.error = None
        session.notice = None
        session.finished_at = time.monotonic()
        session.status = "connected"

    def _finish(self, session, status, error=None):
        """End a login that did not produce an account."""
        self._quit(session)
        account_store.remove_profile(session.stub())
        session.error = error
        session.notice = None
        session.finished_at = time.monotonic()
        session.status = status

    @staticmethod
    def _quit(session):
        platform = session.platform
        if platform is None or platform.driver is None:
            return
        try:
            platform.driver.quit()
        except Exception:
            pass
        platform.driver = None
