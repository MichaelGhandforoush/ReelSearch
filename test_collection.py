import uuid

import chromadb
import pytest

from conftest import FakeEmbedModel
from src.collection import (
    WEAK_MATCH_DISTANCE, Collection, account_key, accounts_where,
    page_matches, split_phrases,
)
from src.video import Video


@pytest.fixture
def collection(monkeypatch):
    client = chromadb.EphemeralClient()
    monkeypatch.setattr(
        "src.collection.chromadb.PersistentClient", lambda path: client
    )
    return Collection(f"test-{uuid.uuid4().hex}", FakeEmbedModel())


def add_video(
    collection, url, word, stats=None, account_id=None, transcript="",
):
    video = Video(url)
    video.transcript = transcript
    video.embedding = FakeEmbedModel().encode(word)
    video.stats = stats or {}
    collection.add(video, account_id=account_id)


def test_search_filters_and_clamps_to_matching_videos(collection):
    add_video(collection, "https://www.tiktok.com/@a/video/1", "pasta")
    add_video(collection, "https://www.instagram.com/reel/2/", "pasta")
    add_video(collection, "https://www.instagram.com/reel/3/", "dog")

    ids, metadatas = collection.search(
        "pasta", num_results=10, where={"platform": "instagram"}
    )

    assert ids == [
        "https://www.instagram.com/reel/2/",
        "https://www.instagram.com/reel/3/",
    ]
    assert all(metadata["platform"] == "instagram" for metadata in metadatas)


def test_search_with_no_matching_videos_returns_nothing(collection):
    add_video(collection, "https://www.instagram.com/reel/1/", "dog")

    assert collection.search(
        "dog", where={"platform": "tiktok"}
    ) == ([], [])


def test_split_phrases_separates_quoted_parts():
    assert split_phrases('best "Joe\'s  Pizza" in \u201cnew york\u201d') == (
        "best in", ["Joe's Pizza", "new york"]
    )
    assert split_phrases('"dog park"') == ("", ["dog park"])
    assert split_phrases('pasta "" "unclosed') == ("pasta unclosed", [])


def test_quoted_only_search_returns_videos_containing_the_phrase(collection):
    add_video(
        collection, "https://www.instagram.com/reel/1/", "car",
        transcript="then we went to the Dog\nPark again",
    )
    add_video(
        collection, "https://www.instagram.com/reel/2/", "dog",
        transcript="the DOG PARK (downtown)",
    )
    add_video(
        collection, "https://www.instagram.com/reel/3/", "dog",
        transcript="a dog in the park",
    )
    add_video(
        collection, "https://www.tiktok.com/@a/video/4", "dog",
        transcript="dog park",
    )
    text, phrases = split_phrases('"dog park"')

    ids, _ = collection.search(
        text or " ".join(phrases), num_results=10,
        where={"platform": "instagram"}, phrases=phrases,
    )

    # Case and line breaks don't matter, the filter still applies, and
    # the matches are ranked by meaning.
    assert ids == [
        "https://www.instagram.com/reel/2/",
        "https://www.instagram.com/reel/1/",
    ]
    assert collection.search(
        "dog", num_results=1, phrases=["park (downtown)"]
    )[0] == ["https://www.instagram.com/reel/2/"]


def test_mixed_search_ranks_phrase_matches_by_the_other_words(collection):
    add_video(
        collection, "https://www.instagram.com/reel/1/", "car",
        transcript="Joe's Pizza #nyc",
    )
    add_video(
        collection, "https://www.instagram.com/reel/2/", "pasta",
        transcript="dinner at joe's pizza, #NYC style",
    )
    add_video(
        collection, "https://www.instagram.com/reel/3/", "pasta",
        transcript="pizza from joe",
    )
    text, phrases = split_phrases('pasta "joe\'s pizza"')

    ids, _ = collection.search(text, num_results=10, phrases=phrases)
    assert ids == [
        "https://www.instagram.com/reel/2/",
        "https://www.instagram.com/reel/1/",
    ]

    ids, _ = collection.search(
        "pasta", num_results=10, phrases=["joe's pizza", "#nyc style"]
    )
    assert ids == ["https://www.instagram.com/reel/2/"]
    assert collection.search(
        "pasta", num_results=10, phrases=["not said anywhere"]
    ) == ([], [])


def test_page_matches_holds_back_weak_matches_until_strong_run_out():
    strong = [(f"s{i}", {"n": i}, 0.2 * i) for i in range(5)]
    weak = [
        (f"w{i}", None, WEAK_MATCH_DISTANCE + 0.1 * (i + 1))
        for i in range(3)
    ]
    matches = strong + weak

    def page(offset, limit, weak=False):
        # Like run_search, which asks for one match past the page.
        result, has_more, has_weak = page_matches(
            matches[:offset + limit + 1], offset, limit, weak=weak
        )
        return [video_id for video_id, _ in result], has_more, has_weak

    # Strong pages first; weaker ones are only offered after the last.
    assert page(0, 2) == (["s0", "s1"], True, False)
    assert page(2, 2) == (["s2", "s3"], True, False)
    assert page(4, 2) == (["s4"], False, True)
    # "Show weaker matches" continues from the strong count.
    assert page(5, 2, weak=True) == (["w0", "w1"], True, False)
    assert page(7, 2, weak=True) == (["w2"], False, False)
    # Exactly a page of strong matches still ends the strong ones.
    assert page(3, 2) == (["s3", "s4"], False, True)
    # Metadata passes through, with None as {}.
    assert page_matches(matches, 0, 1)[0] == [("s0", {"n": 0})]
    assert page_matches(matches, 5, 1, weak=True)[0] == [("w0", {})]


def test_page_matches_with_no_strong_or_no_weak_matches():
    nonsense = [("a", {}, 1.8), ("b", {}, 1.9)]
    assert page_matches(nonsense, 0, 3) == ([], False, True)
    assert page_matches(nonsense, 0, 3, weak=True) == (
        [("a", {}), ("b", {})], False, False
    )
    assert page_matches([("a", {}, 0.1)], 0, 3) == (
        [("a", {})], False, False
    )
    assert page_matches([], 0, 3) == ([], False, False)


def test_search_returns_distances_for_splitting_weak_matches(collection):
    add_video(collection, "https://www.instagram.com/reel/1/", "pasta")
    add_video(collection, "https://www.instagram.com/reel/2/", "dog")
    add_video(collection, "https://www.instagram.com/reel/3/", "pasta")

    ids, metadatas, distances = collection.search(
        "pasta", num_results=10, with_distances=True
    )
    # One-hot fake embeddings: the same word is 0 away, another is 2
    # (squared L2, Chroma's default metric).
    assert distances == pytest.approx([0, 0, 2])
    assert ids[2] == "https://www.instagram.com/reel/2/"
    assert page_matches(zip(ids, metadatas, distances), 0, 1) == (
        [(ids[0], metadatas[0])], True, False
    )
    result, has_more, has_weak = page_matches(
        zip(ids, metadatas, distances), 1, 2
    )
    assert [video_id for video_id, _ in result] == [ids[1]]
    assert (has_more, has_weak) == (False, True)

    # Without with_distances the result keeps its (ids, metadatas) shape.
    plain = collection.search("pasta", num_results=1)
    assert len(plain) == 2 and plain[0][0] in ids[:2]
    assert collection.search(
        "pasta", where={"platform": "tiktok"}, with_distances=True
    ) == ([], [], [])


def test_search_similar_leaves_out_the_video_itself(collection):
    add_video(collection, "https://www.instagram.com/reel/1/", "pasta")
    add_video(collection, "https://www.tiktok.com/@a/video/2", "pasta")
    add_video(collection, "https://www.instagram.com/reel/3/", "dog")
    add_video(collection, "https://www.instagram.com/reel/4/", "car")

    ids, _ = collection.search_similar(
        "https://www.instagram.com/reel/1/", num_results=2
    )
    assert ids[0] == "https://www.tiktok.com/@a/video/2"
    assert len(ids) == 2
    assert "https://www.instagram.com/reel/1/" not in ids

    ids, _ = collection.search_similar(
        "https://www.instagram.com/reel/1/",
        num_results=10,
        where={"platform": "instagram"},
    )
    assert sorted(ids) == [
        "https://www.instagram.com/reel/3/",
        "https://www.instagram.com/reel/4/",
    ]


def test_search_similar_to_unknown_video_returns_nothing(collection):
    add_video(collection, "https://www.instagram.com/reel/1/", "pasta")

    assert collection.search_similar("https://example.com/missing") == ([], [])


def test_find_filters_on_recorded_stats(collection):
    add_video(collection, "https://www.instagram.com/reel/1/", "dog",
              {"uploaded_at": 2000})
    add_video(collection, "https://www.instagram.com/reel/2/", "dog",
              {"uploaded_at": 500})
    add_video(collection, "https://www.instagram.com/reel/3/", "dog")

    ids, _ = collection.find({"uploaded_at": {"$gte": 1000}})

    assert ids == ["https://www.instagram.com/reel/1/"]


def test_backfill_platforms_and_details(collection):
    collection.collection.add(
        ids=["https://www.tiktok.com/@a/video/9"],
        embeddings=[[1.0, 0.0, 0.0]],
        metadatas=[{"account_id": "acc"}],
    )

    assert collection.backfill_platforms() == 1
    assert collection.ids_missing_details() == [
        "https://www.tiktok.com/@a/video/9"
    ]

    collection.set_details("https://www.tiktok.com/@a/video/9", {"views": 7})

    _, metadatas = collection.find()
    assert metadatas == [{
        "account_id": "acc",
        "platform": "tiktok",
        "views": 7,
        "details_checked": 1,
    }]
    assert collection.ids_missing_details() == []


def test_parse_document_reads_back_uploader_and_caption():
    video = Video("https://www.instagram.com/reel/1/")
    video.user = "Chef Anna"
    video.caption = "Easy pasta\nwith Caption: tomatoes"
    video.transcript = "Uploader: not me"

    assert Video.parse_document(video.to_document()) == {
        "uploader": "Chef Anna",
        "caption": "Easy pasta\nwith Caption: tomatoes",
    }
    assert Video.parse_document("") == {}


def test_get_video(collection):
    add_video(collection, "https://www.instagram.com/reel/1/", "dog", {"likes": 4})

    metadata, document = collection.get_video("https://www.instagram.com/reel/1/")

    assert metadata["likes"] == 4
    assert "Uploader:" in document
    assert collection.get_video("https://www.instagram.com/reel/404/") is None


def test_video_saved_by_two_accounts_matches_both_filters(collection):
    url = "https://www.instagram.com/reel/1/"
    add_video(collection, url, "dog", account_id="a")
    add_video(collection, url, "dog", account_id="b")

    assert collection.find(accounts_where(["a"]))[0] == [url]
    assert collection.find(accounts_where(["b"]))[0] == [url]
    assert collection.find(accounts_where(["a", "b"]))[0] == [url]
    metadata, _ = collection.get_video(url)
    assert metadata["account_id"] == "a"


def test_deleting_account_keeps_videos_another_account_saved(collection):
    shared = "https://www.instagram.com/reel/1/"
    only_a = "https://www.instagram.com/reel/2/"
    add_video(collection, shared, "dog", account_id="a")
    add_video(collection, shared, "dog", account_id="b")
    add_video(collection, only_a, "dog", account_id="a")

    assert collection.delete_account_videos("a") == 1

    ids, metadatas = collection.find()
    assert ids == [shared]
    assert metadatas[0]["account_id"] == "b"
    assert collection.find(accounts_where(["a"]))[0] == []
    assert collection.find(accounts_where(["b"]))[0] == [shared]


def test_backfill_account_keys(collection):
    collection.collection.add(
        ids=["https://www.tiktok.com/@a/video/9"],
        embeddings=[[1.0, 0.0, 0.0]],
        metadatas=[{"account_id": "acc"}],
    )

    assert collection.backfill_account_keys() == 1
    assert collection.backfill_account_keys() == 0
    assert collection.find(accounts_where(["acc"]))[0] == [
        "https://www.tiktok.com/@a/video/9"
    ]


def test_owned_ids_counts_shared_videos_and_skips_removed_owners(collection):
    shared = "https://www.instagram.com/reel/1/"
    only_b = "https://www.instagram.com/reel/2/"
    removed = "https://www.instagram.com/reel/3/"
    add_video(collection, shared, "dog", account_id="a")
    add_video(collection, shared, "dog", account_id="b")
    add_video(collection, only_b, "dog", account_id="b")
    add_video(collection, removed, "dog", account_id="a")
    add_video(collection, removed, "dog", account_id="b")
    collection._merge_metadata({removed: {account_key("a"): 0}})

    assert collection.owned_ids("a") == {shared}
    assert collection.owned_ids("b") == {shared, only_b, removed}
    assert collection.owned_ids("nobody") == set()
    assert sorted(collection.ids()) == [shared, only_b, removed]
