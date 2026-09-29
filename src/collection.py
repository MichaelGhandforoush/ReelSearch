import chromadb
import json
import os


class Collection:
    def __init__(self, name, embed_model):
        self.client = chromadb.PersistentClient(path="./chroma_db")
        self.embed_model = embed_model
        self.collection = self.client.get_or_create_collection(name=name)

    def add(self, video, account_id=None, platform=None):
        metadata = {}
        if account_id:
            metadata["account_id"] = account_id
        if platform:
            metadata["platform"] = platform
        self.collection.upsert(
            ids=[video.url],
            embeddings=[video.embedding.tolist()],
            documents=[video.to_document()],
            metadatas=[metadata] if metadata else None,
        )

    def search(self, query, num_results=6):
        query_embedding = self.embed_model.encode(query)
        return self.collection.query(
            query_embeddings=[query_embedding.tolist()],
            n_results=num_results,
        )

    def count(self):
        return self.collection.count()

    def get_collection(self):
        return self.collection.get()

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
        data = self.collection.get(include=["metadatas"])
        legacy_ids = set()
        if urls_path and os.path.exists(urls_path):
            with open(urls_path, "r", encoding="utf-8") as file:
                legacy_ids = set(json.load(file))
        ids = [
            video_id for video_id, metadata in zip(
                data["ids"], data.get("metadatas") or []
            )
            if (
                (metadata and metadata.get("account_id") == account_id)
                or video_id in legacy_ids
            )
        ]
        if ids:
            self.collection.delete(ids=ids)
        return len(ids)
