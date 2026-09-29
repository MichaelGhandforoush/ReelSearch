import json
import os
import re
import shutil
from collections import OrderedDict

import cv2
import pytesseract
import torch
import yt_dlp
from faster_whisper import WhisperModel
from nltk.stem.snowball import SnowballStemmer
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS
from transformers import BitsAndBytesConfig, pipeline

from src.video import Video

STEMMER = SnowballStemmer("english")
CONTRACTIONS = [
    (re.compile(r"n't\b"), " not"),
    (re.compile(r"'re\b"), " are"),
    (re.compile(r"'m\b"), " am"),
    (re.compile(r"'ve\b"), " have"),
    (re.compile(r"'ll\b"), " will"),
    (re.compile(r"'d\b"), " would"),
    (re.compile(r"'s\b"), ""),
]
# Chat filler that neither the stop-word list nor stemming removes.
CHAT_FILLER_WORDS = {
    "dont", "gonna", "im", "just", "pls", "plz", "really", "u", "ur",
    "wanna", "yeah",
}
CHAT_FILLER_PATTERN = re.compile(r"(?:a?ha)+h?|he(?:he)+|l+o+l+|lmf?ao+|omf?g+")
COMMENT_SPAM_PATTERN = re.compile(
    r"https?://|www\.|\bdm (me|us)\b|\bcheck (out )?my\b|\bfollow (me|us)\b"
    r"|\blink in bio\b",
    re.IGNORECASE,
)


class Processor:
    """Download, enrich, and embed saved videos."""

    def __init__(
        self,
        embed_model,
        output_dir="videos",
        use_transcript=True,
        use_ocr=True,
        use_comments=True,
        use_visual_description=False,
    ):
        """Choose which generated descriptions go into the embedding.

        use_transcript: speech-to-text of the audio.
        use_ocr: text read from keyframes (still needs Tesseract installed).
        use_comments: filtered uploader/viewer comments.
        use_visual_description: VLM-written description of the keyframes.
        """
        self.use_transcript = use_transcript
        self.use_ocr = use_ocr
        self.use_comments = use_comments
        self.use_visual_description = use_visual_description
        self.transcribe_model = WhisperModel(
            "base",
            device="cpu",
            compute_type="int8",
        )
        self.embed_model = embed_model
        self.max_ocr_frames = 6
        self.max_transcript_seconds = 180
        self.max_comments = 5
        self.max_comment_chars = 200
        self.generator = None
        self.visual_description_model_name = (
            "HuggingFaceTB/SmolVLM-500M-Instruct"
        )
        tesseract_command = os.getenv("TESSERACT_CMD")
        if tesseract_command:
            pytesseract.pytesseract.tesseract_cmd = tesseract_command
        self.ocr_enabled = bool(
            (tesseract_command and (
                os.path.isfile(tesseract_command)
                or shutil.which(tesseract_command)
            ))
            or shutil.which("tesseract")
        )
        if not self.ocr_enabled:
            print(
                "OCR disabled: install Tesseract or set TESSERACT_CMD "
                "to its executable path."
            )

        self.cookies_tiktok = "/content/www.tiktok.com_cookies.txt"
        self.cookies_instagram = "/content/www.instagram.com_cookies.txt"
        common_options = {
            "outtmpl": f"{output_dir}/%(id)s.%(ext)s",
            "format": "mp4",
            "writeinfojson": True,
            "getcomments": use_comments,
            "quiet": True,
        }
        self.ydl_instagram = yt_dlp.YoutubeDL({
            **common_options,
            "cookiefile": self.cookies_instagram,
        })
        self.ydl_tiktok = yt_dlp.YoutubeDL({
            **common_options,
            "cookiefile": self.cookies_tiktok,
        })

    def download(self, url, output_dir="videos"):
        ydl = self.ydl_tiktok if "tiktok" in url else self.ydl_instagram
        try:
            info = ydl.extract_info(url, download=True)
            return (
                f"{output_dir}/{info['id']}.mp4",
                info.get("description", ""),
                info.get("uploader", ""),
                info.get("comments") or [],
            )
        except Exception as error:
            print(f"Skipping {url}: {error}")
            return None

    @staticmethod
    def _normalize_text(text):
        return re.sub(r"\s+", " ", text or "").strip()

    @staticmethod
    def _content_words(text):
        """Return stems of the meaningful words, so word forms compare equal."""
        text = text.lower().replace("’", "'")
        for pattern, replacement in CONTRACTIONS:
            text = pattern.sub(replacement, text)
        return {
            STEMMER.stem(word)
            for word in re.findall(r"[a-z0-9]+", text)
            if len(word) > 1
            and word not in ENGLISH_STOP_WORDS
            and word not in CHAT_FILLER_WORDS
            and not CHAT_FILLER_PATTERN.fullmatch(word)
        }

    def select_comments(self, comments, uploader, known_text):
        """Keep only comments that add descriptive information.

        Most comments are reactions, friend tags, or repeat the caption, so
        a comment is kept only if it contributes several content words not
        already present in the caption, transcript, OCR text, or previously
        kept comments. Leaving comments out is preferred over diluting the
        embedding.
        """
        known_words = self._content_words(known_text)
        uploader = (uploader or "").lower()
        candidates = []
        for comment in comments:
            text = comment.get("text") or ""
            if COMMENT_SPAM_PATTERN.search(text):
                continue
            # Mentions are almost always friends being tagged, not subjects.
            text = self._normalize_text(re.sub(r"@[\w.]+", " ", text))
            words = self._content_words(text)
            if len(words) < 2:
                continue
            by_uploader = (
                comment.get("author_is_uploader")
                or (comment.get("author") or "").lower() == uploader
            )
            likes = comment.get("like_count") or 0
            candidates.append((not by_uploader, -likes, text, words))

        # Uploader comments first, then most liked, so the best version of a
        # repeated remark is the one that claims its new words.
        candidates.sort(key=lambda item: item[:2])
        selected = []
        for _, _, text, words in candidates:
            new_words = words - known_words
            if len(new_words) < 2 or len(new_words) < len(words) / 2:
                continue
            selected.append(text[:self.max_comment_chars])
            known_words |= words
            if len(selected) >= self.max_comments:
                break
        return " | ".join(selected)

    def transcribe(self, path):
        segments, _ = self.transcribe_model.transcribe(
            path,
            beam_size=1,
            best_of=1,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            max_new_tokens=128,
            clip_timestamps=f"0,{self.max_transcript_seconds}",
        )
        parts = []
        elapsed = 0.0
        for segment in segments:
            if elapsed >= self.max_transcript_seconds:
                break
            text = self._normalize_text(segment.text)
            if text:
                parts.append(text)
            elapsed = max(elapsed, float(segment.end))
        return " ".join(parts)

    def get_prompt(self, transcript, caption, user):
        return f"""
        Create one concise factual video description using the transcript,
        caption, uploader, and images. Include topic, visible objects,
        actions, people, locations, and any readable on-screen text.
        Do not invent details. Return only the description.

        Transcript: {transcript}
        Caption: {caption}
        Uploader: {user}
        """

    def _load_visual_description_model(self):
        if self.generator is not None:
            return
        quant_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16,
        )
        self.generator = pipeline(
            "image-text-to-text",
            model=self.visual_description_model_name,
            device_map="auto",
            dtype=torch.float16,
            model_kwargs={"quantization_config": quant_config},
        )

    def generate_description(self, prompt):
        self._load_visual_description_model()
        response = self.generator(
            prompt,
            do_sample=False,
            max_new_tokens=60,
        )
        return response[0]["generated_text"]

    def extract_keyframes(self, path, max_frames=None):
        """Sample a bounded set of representative frames in one decode pass."""
        max_frames = max_frames or self.max_ocr_frames
        video = cv2.VideoCapture(path)
        frame_count = int(video.get(cv2.CAP_PROP_FRAME_COUNT))
        if frame_count <= 0:
            video.release()
            return []

        ratios = (0.03, 0.18, 0.35, 0.52, 0.70, 0.88)
        positions = sorted(set(
            min(frame_count - 1, int(frame_count * ratio))
            for ratio in ratios[:max_frames]
        ))
        frames = []
        for position in positions:
            video.set(cv2.CAP_PROP_POS_FRAMES, position)
            success, frame = video.read()
            if success:
                frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        video.release()
        return frames

    def _ocr_candidates(self, frame):
        """Run OCR on complementary preprocessing variants."""
        if not self.ocr_enabled:
            return []
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        scale = 2 if min(gray.shape) < 900 else 1
        gray = cv2.resize(
            gray,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_CUBIC,
        )
        blurred = cv2.GaussianBlur(gray, (3, 3), 0)
        primary_variants = [
            gray,
            cv2.threshold(
                gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
            )[1],
        ]
        candidates = self._run_ocr_variants(primary_variants)
        if candidates:
            return candidates

        adaptive_variant = cv2.adaptiveThreshold(
            blurred,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            31,
            11,
        )
        return self._run_ocr_variants([adaptive_variant])

    def _run_ocr_variants(self, variants):
        candidates = []
        for image in variants:
            data = pytesseract.image_to_data(
                image,
                config="--oem 3 --psm 11",
                output_type=pytesseract.Output.DICT,
            )
            words = []
            confidences = []
            for text, confidence in zip(data["text"], data["conf"]):
                text = self._normalize_text(text)
                try:
                    confidence = float(confidence)
                except (TypeError, ValueError):
                    confidence = -1
                if text and confidence >= 35:
                    words.append(text)
                    confidences.append(confidence)
            if words and sum(confidences) / len(confidences) >= 45:
                candidates.append(" ".join(words))
        return candidates

    def extract_text_from_frames(self, frames):
        """Keep text that is stable across frames and discard OCR noise."""
        text_counts = OrderedDict()
        for frame in frames:
            for candidate in self._ocr_candidates(frame):
                normalized = self._normalize_text(candidate)
                key = re.sub(
                    r"[^a-z0-9]+", " ", normalized.lower()
                ).strip()
                if len(key) < 3:
                    continue
                count, existing = text_counts.get(key, (0, normalized))
                text_counts[key] = (count + 1, existing)

        ranked = sorted(
            text_counts.values(),
            key=lambda item: (-item[0], -len(item[1])),
        )
        return " ".join(text for _, text in ranked[:12])

    def generate_description_with_frames(self, frames, prompt):
        from PIL import Image

        self._load_visual_description_model()
        images = [Image.fromarray(frame) for frame in frames[:3]]
        messages = [{
            "role": "user",
            "content": [
                *({"type": "image", "image": image} for image in images),
                {"type": "text", "text": prompt},
            ],
        }]
        response = self.generator(
            messages,
            do_sample=False,
            max_new_tokens=60,
        )
        return response[0]["generated_text"][-1]["content"]

    @staticmethod
    def get_platform(url):
        url = url.lower()
        if "instagram.com" in url:
            return "instagram"
        if "tiktok.com" in url:
            return "tiktok"
        return "unknown"

    def _build_description(self, video):
        """Combine the enabled frame-based descriptions (OCR and VLM)."""
        parts = []
        if self.use_ocr:
            parts.append(self.extract_text_from_frames(video.frames))
        if self.use_visual_description and video.frames:
            prompt = self.get_prompt(
                video.transcript,
                video.caption,
                video.user,
            )
            parts.append(
                self.generate_description_with_frames(video.frames, prompt)
            )
        return " ".join(part for part in parts if part).strip()

    def process_video(self, url):
        video = Video(url)
        try:
            result = self.download(video.url)
            if result is None:
                return None
            video.path, video.caption, video.user, comments = result
            if self.use_transcript:
                video.transcript = self.transcribe(video.path)
            if self.use_ocr or self.use_visual_description:
                video.frames = self.extract_keyframes(video.path)
            video.description = self._build_description(video)
            if self.use_comments:
                video.comments = self.select_comments(
                    comments,
                    video.user,
                    " ".join((
                        video.caption,
                        video.transcript,
                        video.description,
                    )),
                )
            video.embedding = self.embed_model.encode(video.to_document())
            return video
        except Exception as error:
            print(f"Failed processing {url}: {error}")
            return None
        finally:
            if video.path and os.path.exists(video.path):
                os.remove(video.path)


    def load_videos(
        self,
        collection,
        path="instagram_urls.json",
        start_number=0,
        stop_number=10,
    ):
        with open(path, "r") as file:
            urls = json.load(file)
        failed = 0
        for url in urls[start_number:stop_number]:
            print(url)
            video = self.process_video(url)
            if video is None:
                failed += 1
                continue
            collection.add(video)
        return failed
