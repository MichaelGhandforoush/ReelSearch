import chromadb
import json
import os
import re


def platform_from_url(url):
    url = url.lower()
    if "instagram.com" in url:
        return "instagram"
    if "tiktok.com" in url:
        return "tiktok"
    return None


ACCOUNT_KEY_PREFIX = "acct_"


def account_key(account_id):
    """Metadata key marking that an account has saved a video.

    Chroma metadata cannot hold lists, so each owning account gets its own
    key set to 1. A key set to 0 means the account no longer owns it.
    """
    return f"{ACCOUNT_KEY_PREFIX}{account_id}"


def account_owners(metadata):
    return [
        key[len(ACCOUNT_KEY_PREFIX):]
        for key, value in (metadata or {}).items()
        if key.startswith(ACCOUNT_KEY_PREFIX) and value == 1
    ]


def accounts_where(account_ids):
    """Filter matching videos saved by any of the given accounts."""
    clauses = [{account_key(account_id): 1} for account_id in account_ids]
    return clauses[0] if len(clauses) == 1 else {"$or": clauses}


# Straight and curly double quotes, since phone keyboards type the latter.
QUOTES = '"\u201c\u201d'
PHRASE_PATTERN = re.compile(f"[{QUOTES}]([^{QUOTES}]*)[{QUOTES}]")
REGEX_SPECIAL = set("\\.+*?()|[]{}^$#&-~")


def split_phrases(query):
    """Split a search query into (text, phrases).

    Quoted parts of the query are exact phrases; the rest is the text to
    search by meaning. Stray unmatched quotes are dropped from the text.
    """
    phrases = [
        " ".join(phrase.split())
        for phrase in PHRASE_PATTERN.findall(query)
    ]
    text = PHRASE_PATTERN.sub(" ", query)
    text = " ".join(re.sub(f"[{QUOTES}]", " ", text).split())
    return text, [phrase for phrase in phrases if phrase]


def phrase_pattern(phrase):
    """A case-insensitive Chroma regex matching the phrase literally, with
    any run of whitespace between its words (transcripts wrap lines)."""
    words = [
        "".join(
            "\\" + char if char in REGEX_SPECIAL else char for char in word
        )
        for word in phrase.split()
    ]
    return "(?i)" + r"\s+".join(words)


def phrases_where_document(phrases):
    """Filter matching documents that contain every phrase, ignoring case.

    Chroma's $contains is case-sensitive, so this uses $regex with the
    (?i) flag instead of lowercasing the stored documents.
    """
    clauses = [{"$regex": phrase_pattern(phrase)} for phrase in phrases]
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


# Text search results farther than this from the query are "weak matches",
# held back behind "Show weaker matches". Distances are Chroma's default
# metric, squared L2; all-MiniLM-L6-v2 embeddings are unit length, so this
# equals 2 - 2 * cosine similarity and 1.35 means a similarity of 0.325.
# Chosen from real queries on a ~1500 video library: gibberish and topics
# the library lacks ("xkcd blorp", "photosynthesis") had no result under
# 1.38, while on-topic ones ("pizza", "gym", "recipe", "cat") had their
# relevant results between 0.65 and 1.30. To retune (for example after
# changing the embedding model or the collection's metric), print the
# distances search returns for a few real queries and pick the point where
# results stop being on topic. Raise it to hide fewer videos.
WEAK_MATCH_DISTANCE = 1.35


def page_matches(matches, offset, limit, weak=False,
                 max_distance=WEAK_MATCH_DISTANCE):
    """Return one page of (id, metadata) from (id, metadata, distance)
    matches sorted best first, whether more follow, and whether weaker
    matches were held back.

    Without weak, only matches within max_distance count, and offset is
    into those. With weak, every match counts, so a page continuing from
    the strong ones (offset = how many there are) holds the weaker ones.
    Pass at least offset + limit + 1 matches, so a following one shows.
    """
    matches = list(matches)
    has_weak = False
    if not weak:
        strong = [match for match in matches if match[2] <= max_distance]
        has_weak = len(strong) < len(matches)
        matches = strong
    end = offset + limit
    has_more = len(matches) > end
    page = [
        (video_id, metadata or {})
        for video_id, metadata, _ in matches[offset:end]
    ]
    # Weaker matches come after every strong one, so offer them only once
    # the strong ones run out.
    return page, has_more, has_weak and not has_more


class Collection:
    def __init__(self, name, embed_model):
        self.client = chromadb.PersistentClient(path="./chroma_db")
        self.embed_model = embed_model
        self.collection = self.client.get_or_create_collection(name=name)

    def add(self, video, account_id=None, platform=None):
        metadata = {**video.stats, "details_checked": 1}
        platform = platform or platform_from_url(video.url)
        existing = self.collection.get(ids=[video.url], include=["metadatas"])
        previous = (existing["metadatas"] or [None])[0] or {}
        # Keep every account that already saved this video, so a second
        # account syncing it doesn't take it away from the first.
        for key, value in previous.items():
            if key.startswith(ACCOUNT_KEY_PREFIX):
                metadata[key] = value
        if previous.get("account_id"):
            metadata["account_id"] = previous["account_id"]
        if account_id:
            metadata.setdefault("account_id", account_id)
            metadata[account_key(account_id)] = 1
        if platform:
            metadata["platform"] = platform
        self.collection.upsert(
            ids=[video.url],
            embeddings=[video.embedding.tolist()],
            documents=[video.to_document()],
            metadatas=[metadata],
        )

    def search(self, query, num_results=6, where=None, phrases=None,
               with_distances=False):
        """Return (ids, metadatas) of the closest matches, best first.

        With phrases, only videos whose document contains every one of them
        (ignoring case) are returned. With with_distances, also return each
        match's distance from the query, as (ids, metadatas, distances).
        """
        where_document = phrases_where_document(phrases or [])
        # Asking Chroma for more results than match the filter is an error
        # in some versions, so clamp to what is available.
        num_results = min(
            num_results, self._available(where, where_document)
        )
        if num_results <= 0:
            return ([], [], []) if with_distances else ([], [])
        query_embedding = self.embed_model.encode(query)
        ids, metadatas, distances = self._query(
            query_embedding.tolist(), num_results, where, where_document
        )
        if with_distances:
            return ids, metadatas, distances
        return ids, metadatas

    def search_similar(self, video_id, num_results=6, where=None):
        """Return (ids, metadatas) of the videos closest to a saved one,
        best first, leaving that video out."""
        data = self.collection.get(ids=[video_id], include=["embeddings"])
        embeddings = data.get("embeddings")
        if not data["ids"] or embeddings is None or len(embeddings) == 0:
            return [], []
        # One extra, since the video itself is usually its own best match.
        num_results = min(num_results + 1, self._available(where))
        if num_results <= 0:
            return [], []
        ids, metadatas, _ = self._query(
            [float(value) for value in embeddings[0]], num_results, where
        )
        pairs = [
            (other_id, metadata)
            for other_id, metadata in zip(ids, metadatas)
            if other_id != video_id
        ][:num_results - 1]
        return [pair[0] for pair in pairs], [pair[1] for pair in pairs]

    def _available(self, where, where_document=None):
        if where is None and where_document is None:
            return self.count()
        return len(self.collection.get(
            where=where, where_document=where_document, include=[]
        )["ids"])

    def _query(self, embedding, num_results, where, where_document=None):
        results = self.collection.query(
            query_embeddings=[embedding],
            n_results=num_results,
            where=where,
            where_document=where_document,
            include=["metadatas", "distances"],
        )
        return (
            results["ids"][0],
            results["metadatas"][0],
            results["distances"][0],
        )

    def find(self, where=None):
        """Return (ids, metadatas) of every video matching the filter."""
        data = self.collection.get(where=where, include=["metadatas"])
        return data["ids"], data["metadatas"]

    def get_video(self, video_id):
        """Return (metadata, document) for one video, or None."""
        data = self.collection.get(
            ids=[video_id], include=["metadatas", "documents"]
        )
        if not data["ids"]:
            return None
        return data["metadatas"][0] or {}, data["documents"][0] or ""

    def count(self):
        return self.collection.count()

    def owned_ids(self, account_id):
        """Ids of every video the account has saved, without loading any
        metadata or documents."""
        return set(self.collection.get(
            where=accounts_where([account_id]), include=[]
        )["ids"])

    def ids(self):
        """Every video id, without loading any metadata or documents."""
        return self.collection.get(include=[])["ids"]

    def _merge_metadata(self, updates):
        """Apply {id: changes} on top of each record's existing metadata."""
        if not updates:
            return
        ids = list(updates)
        existing = self.collection.get(ids=ids, include=["metadatas"])
        current = dict(zip(existing["ids"], existing["metadatas"]))
        ids = [video_id for video_id in ids if video_id in current]
        if ids:
            self.collection.update(
                ids=ids,
                metadatas=[
                    {**(current[video_id] or {}), **updates[video_id]}
                    for video_id in ids
                ],
            )

    def backfill_platforms(self):
        """Tag older records, saved before platform metadata existed."""
        ids, metadatas = self.find()
        updates = {}
        for video_id, metadata in zip(ids, metadatas):
            platform = platform_from_url(video_id)
            if platform and not (metadata or {}).get("platform"):
                updates[video_id] = {"platform": platform}
        self._merge_metadata(updates)
        return len(updates)

    def backfill_account_keys(self):
        """Tag older records, saved before per-account keys existed."""
        ids, metadatas = self.find()
        updates = {}
        for video_id, metadata in zip(ids, metadatas):
            account_id = (metadata or {}).get("account_id")
            if account_id and account_key(account_id) not in metadata:
                updates[video_id] = {account_key(account_id): 1}
        self._merge_metadata(updates)
        return len(updates)

    def ids_missing_details(self):
        ids, metadatas = self.find()
        return [
            video_id for video_id, metadata in zip(ids, metadatas)
            if not (metadata or {}).get("details_checked")
        ]

    def set_details(self, video_id, stats):
        self._merge_metadata({video_id: {**stats, "details_checked": 1}})

    def delete_platform_videos(self, platform_name):
        """Delete only saved video records belonging to a platform."""
        platform_name = platform_name.lower()
        data = self.collection.get()
        ids = [
            video_id for video_id in data["ids"]
            if platform_name in video_id.lower()
        ]
        if ids:
            self.collection.delete(ids=ids)
        return len(ids)

    def delete_account_videos(self, account_id, urls_path=None):
        """Remove an account's videos, keeping ones another account saved.

        Returns how many records were deleted outright.
        """
        data = self.collection.get(include=["metadatas"])
        legacy_ids = set()
        if urls_path and os.path.exists(urls_path):
            with open(urls_path, "r", encoding="utf-8") as file:
                legacy_ids = set(json.load(file))
        ids = []
        updates = {}
        for video_id, metadata in zip(
            data["ids"], data.get("metadatas") or []
        ):
            metadata = metadata or {}
            owners = account_owners(metadata)
            if not (
                account_id in owners
                or metadata.get("account_id") == account_id
                or video_id in legacy_ids
            ):
                continue
            others = [owner for owner in owners if owner != account_id]
            if not others:
                ids.append(video_id)
                continue
            changes = {account_key(account_id): 0}
            if metadata.get("account_id") == account_id:
                changes["account_id"] = others[0]
            updates[video_id] = changes
        if ids:
            self.collection.delete(ids=ids)
        self._merge_metadata(updates)
        return len(ids)
