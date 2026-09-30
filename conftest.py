import importlib
import sys
from unittest.mock import MagicMock

import chromadb
import numpy as np
import pytest


class FakeEmbedModel:
    """Embeds text as a one-hot vector for its first word."""

    words = ["pasta", "dog", "car"]

    def encode(self, text):
        vector = np.zeros(len(self.words))
        vector[self.words.index(text.split()[0].lower())] = 1
        return vector


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    """Import app.py without its real models, database, files or network.

    The embedding model is FakeEmbedModel, Whisper is a MagicMock, the
    "videos" collection lives in an in-memory Chroma client, accounts,
    browser profiles, sync state and the folders the storage report scans
    are under tmp_path, and
    Instagram posts are never fetched.
    """
    client = chromadb.EphemeralClient()
    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer",
        lambda *args, **kwargs: FakeEmbedModel(),
    )
    monkeypatch.setattr(
        "src.processor.WhisperModel", lambda *args, **kwargs: MagicMock()
    )
    monkeypatch.setattr(
        "chromadb.PersistentClient", lambda *args, **kwargs: client
    )
    monkeypatch.setattr("src.account_store.ROOT", str(tmp_path))
    monkeypatch.setattr(
        "src.account_store.ACCOUNTS_PATH", str(tmp_path / "accounts.json")
    )
    monkeypatch.setattr(
        "src.account_store.PROFILES_ROOT", str(tmp_path / "selenium_profiles")
    )

    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    monkeypatch.setattr(
        module, "sync_state_path", str(tmp_path / "sync_state.json")
    )
    # The storage report and cleanup must never look at the real folders.
    monkeypatch.setattr(module, "videos_dir", str(tmp_path / "videos"))
    monkeypatch.setattr(module, "library_dir", str(tmp_path / "chroma_db"))
    monkeypatch.setattr(module, "warm_embeds", lambda videos: None)
    try:
        yield module
    finally:
        sys.modules.pop("app", None)
        # In-memory clients share their data within a process, so the next
        # test would otherwise see this one's videos.
        try:
            client.delete_collection("videos")
        except Exception:
            pass


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()
