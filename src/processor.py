import json
import os
import random
import re
import shutil
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

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


ERROR_REASONS = (
    (("private", "login required", "log in", "sign in", "cookies"),
     "the video is private or needs a login; check your cookies file and "
     "that the account is signed in"),
    (("unavailable", "removed", "deleted", "not found", "404",
      "does not exist"),
     "the video was removed or is no longer available"),
    (("429", "too many requests", "rate limit", "rate-limit"),
     "the platform is rate limiting requests; try fewer workers or wait"),
    (("unable to download", "timed out", "timeout", "connection",
      "network", "name or service", "getaddrinfo"),
     "a network problem interrupted the download"),
    (("unsupported url",), "the link is not a supported video URL"),
    (("ffmpeg", "ffprobe"), "ffmpeg is missing or failed"),
)


# Failures worth retrying: the same request may well work a little later.
RATE_LIMIT_WORDS = ("429", "too many requests", "rate limit", "rate-limit")
TRANSIENT_WORDS = RATE_LIMIT_WORDS + (
    "timed out", "timeout", "connection", "network", "reset by peer",
    "forcibly closed", "10054", "temporarily unavailable", "502", "503",
    "504", "incomplete read", "unable to download webpage",
)
# Cores kept free for the scraping browser, the web app, and the OS. With
# every core busy transcribing, Chrome stops answering Selenium and the
# scraper loses its connection to it.
RESERVED_CORES = 2
# The optional processing steps, named as Processor attributes. Each can be
# overridden for a single video with process_video(url, features=...).
FEATURES = (
    "use_transcript", "use_ocr", "use_comments", "use_visual_description"
)
# Downloads at once per platform. Each platform is a separate host, so their
# downloads do not slow each other down.
DEFAULT_DOWNLOAD_WORKERS = 3
# How hard an account's videos are loaded: "slow" downloads one at a time
# with a pause after each, so the platform rarely times it out; "medium" is
# the standard behaviour; "fast" downloads a couple more at once and is more
# likely to be rate limited.
LOAD_SPEEDS = ("slow", "medium", "fast")
DEFAULT_LOAD_SPEED = "medium"
FAST_EXTRA_WORKERS = 2
SLOW_PAUSE_SECONDS = (2.0, 4.0)


# Failures that will never succeed, such as photo-only posts: the video is
# marked as unloadable so later syncs stop retrying it.
UNLOADABLE_WORDS = (
    "there is no video in this post",
    "no video formats found",
)


def download_workers_for(speed, medium_workers):
    """Downloads allowed at once per platform at a load speed."""
    if speed == "slow":
        return 1
    if speed == "fast":
        return medium_workers + FAST_EXTRA_WORKERS
    return medium_workers


def default_cpu_workers():
    """Videos transcribed at once, with a few cores for each Whisper run."""
    cores = os.cpu_count() or 4
    return max(1, min(6, (cores - RESERVED_CORES) // 3))


def is_transient_error(error):
    text = f"{type(error).__name__} {error}".lower()
    return any(word in text for word in TRANSIENT_WORDS)


def is_unloadable_error(error):
    text = str(error).lower()
    return any(word in text for word in UNLOADABLE_WORDS)


def is_rate_limit_error(error):
    text = str(error).lower()
    return any(word in text for word in RATE_LIMIT_WORDS)


class AdaptiveLimit:
    """A concurrency limit that backs off when a platform pushes back.

    Up to `maximum` callers hold a slot at once. A network failure halves
    the limit so fewer requests hit the platform together, and every
    `recover_after` successes in a row raise it by one, so it climbs back
    to the maximum once the connection is healthy again.
    """

    def __init__(self, maximum, recover_after=5):
        self.maximum = max(1, maximum)
        self.limit = self.maximum
        self.recover_after = recover_after
        self.active = 0
        self._successes = 0
        self._condition = threading.Condition()

    def __enter__(self):
        with self._condition:
            while self.active >= self.limit:
                self._condition.wait()
            self.active += 1
        return self

    def __exit__(self, *exc_info):
        with self._condition:
            self.active -= 1
            self._condition.notify_all()

    def succeeded(self):
        with self._condition:
            self._successes += 1
            if (
                self._successes >= self.recover_after
                and self.limit < self.maximum
            ):
                self._successes = 0
                self.limit += 1
                self._condition.notify_all()

    def failed(self):
        with self._condition:
            self._successes = 0
            self.limit = max(1, self.limit // 2)


def explain_error(error):
    """Return 'reason (ExceptionType: details)' for an exception."""
    detail = re.sub(r"\s+", " ", str(error)).strip() or "no details given"
    lowered = detail.lower()
    for keywords, reason in ERROR_REASONS:
        if any(keyword in lowered for keyword in keywords):
            return f"{reason} ({type(error).__name__}: {detail})"
    return f"{type(error).__name__}: {detail}"


class QuietLogger:
    """yt-dlp logger that hides warnings which are expected without cookies."""

    IGNORED_WARNINGS = ("No CSRF token set by Instagram API",)

    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        if not any(text in message for text in self.IGNORED_WARNINGS):
            print(message)

    def error(self, message):
        print(message)


class ProcessingError(Exception):
    """A video failed at a named step, with the reason attached."""

    def __init__(self, step, error):
        super().__init__(f"{step} failed - {explain_error(error)}")
        self.step = step


class Processor:
    """Download, enrich, and embed saved videos."""

    # yt-dlp's own retries for a connection dropped mid-request; failures
    # that get past these are retried by _extract() after a longer pause.
    network_options = {
        "socket_timeout": 30,
        "retries": 5,
        "extractor_retries": 3,
    }

    def __init__(
        self,
        embed_model,
        output_dir="videos",
        use_transcript=True,
        use_ocr=True,
        use_comments=True,
        use_visual_description=False,
        download_workers=1,
        cpu_workers=1,
    ):
        """Choose which generated descriptions go into the embedding.

        use_transcript: speech-to-text of the audio.
        use_ocr: text read from keyframes (still needs Tesseract installed).
        use_comments: filtered uploader/viewer comments.
        use_visual_description: VLM-written description of the keyframes.
        download_workers: how many videos may download at once from each
            platform; lowered automatically while the platform is failing.
        cpu_workers: how many videos may be transcribed, read, and embedded
            at once, across every thread using this processor.
        """
        self.use_transcript = use_transcript
        self.use_ocr = use_ocr
        self.use_comments = use_comments
        self.use_visual_description = use_visual_description
        self.output_dir = output_dir
        self.download_workers = max(1, download_workers)
        self.cpu_workers = max(1, cpu_workers)
        # The limits are shared by every sync, so syncing several accounts
        # at once queues their work instead of multiplying it.
        self.download_limits = {
            (platform, speed): AdaptiveLimit(
                download_workers_for(speed, self.download_workers)
            )
            for platform in ("instagram", "tiktok", "unknown")
            for speed in LOAD_SPEEDS
        }
        self.cpu_slots = threading.BoundedSemaphore(self.cpu_workers)
        self.download_attempts = 4
        self.retry_delay_seconds = 3
        self.rate_limit_delay_seconds = 30
        # One Whisper worker per CPU slot lets transcriptions run side by
        # side; splitting the spare cores between them avoids oversubscribing
        # the CPU.
        spare_cores = max(1, (os.cpu_count() or 4) - RESERVED_CORES)
        self.transcribe_model = WhisperModel(
            "base",
            device="cpu",
            compute_type="int8",
            num_workers=self.cpu_workers,
            cpu_threads=max(1, spare_cores // self.cpu_workers),
        )
        self._generator_lock = threading.Lock()
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

        self.cookie_files = {
            "tiktok": "/content/www.tiktok.com_cookies.txt",
            "instagram": "/content/www.instagram.com_cookies.txt",
        }
        self.download_options = {
            "outtmpl": f"{output_dir}/%(id)s.%(ext)s",
            "format": "mp4",
            # Comments and stats come from what extract_info returns, so no
            # .info.json is written next to each download.
            "writeinfojson": False,
            "getcomments": use_comments,
            "quiet": True,
            **self.network_options,
        }
        self.details_options = {
            "quiet": True, "skip_download": True, **self.network_options
        }
        # YoutubeDL is not thread-safe, so each worker thread gets its own.
        self._local = threading.local()
        # URLs that failed in a way retrying cannot fix (no video in them).
        self.unloadable_urls = set()

    def _ydl(self, url, details=False, comments=None):
        platform = "tiktok" if "tiktok" in url else "instagram"
        comments = self.use_comments if comments is None else comments
        clients = getattr(self._local, "ydl_clients", None)
        if clients is None:
            clients = self._local.ydl_clients = {}
        key = (platform, details, not details and comments)
        if key not in clients:
            options = (
                self.details_options if details
                else {**self.download_options, "getcomments": comments}
            )
            options = {**options, "logger": QuietLogger()}
            # Cookies are optional: public videos download without them.
            cookie_file = self.cookie_files[platform]
            if os.path.isfile(cookie_file):
                options["cookiefile"] = cookie_file
            clients[key] = yt_dlp.YoutubeDL(options)
        return clients[key]

    def _extract(self, url, details=False, comments=None, speed=None):
        """Run yt-dlp for a URL, retrying network failures with backoff.

        Each attempt holds one of the platform's download slots, and a
        network failure lowers how many there are, so parallel downloads
        thin out on their own when the platform starts dropping connections
        instead of every worker failing at once. The load speed sets how
        many slots there are; slow also pauses before releasing each one.
        """
        speed = speed if speed in LOAD_SPEEDS else DEFAULT_LOAD_SPEED
        limit = self.download_limits[(self.get_platform(url), speed)]
        ydl = self._ydl(url, details=details, comments=comments)
        for attempt in range(1, self.download_attempts + 1):
            try:
                with limit:
                    info = ydl.extract_info(url, download=not details)
                    if speed == "slow":
                        time.sleep(random.uniform(*SLOW_PAUSE_SECONDS))
            except Exception as error:
                if not is_transient_error(error):
                    raise
                limit.failed()
                if attempt == self.download_attempts:
                    raise
                base = (
                    self.rate_limit_delay_seconds
                    if is_rate_limit_error(error)
                    else self.retry_delay_seconds
                )
                delay = base * 2 ** (attempt - 1) * random.uniform(0.8, 1.2)
                print(
                    f"Retrying {url} in {delay:.0f}s (attempt {attempt + 1} "
                    f"of {self.download_attempts}): {explain_error(error)}"
                )
                time.sleep(delay)
            else:
                limit.succeeded()
                return info

    def download(self, url, output_dir="videos", comments=None, speed=None):
        """comments: fetch the video's comments too (default use_comments).
        speed: one of LOAD_SPEEDS (default medium)."""
        try:
            info = self._extract(url, comments=comments, speed=speed)
            entry = self._first_entry(info)
            return (
                self._downloaded_path(entry, output_dir),
                info.get("description") or entry.get("description", ""),
                info.get("uploader") or entry.get("uploader", ""),
                info.get("comments") or entry.get("comments") or [],
                self.video_stats({**entry, **info}),
            )
        except Exception as error:
            if is_unloadable_error(error):
                self.unloadable_urls.add(url)
                print(f"Skipping {url} for good: it has no video to load.")
            else:
                print(
                    f"Skipping {url}: download failed - {explain_error(error)}"
                )
            return None

    @staticmethod
    def _first_entry(info):
        """A carousel post comes back as a playlist; use its first video and
        delete the files of any others, since only one is indexed."""
        if info.get("_type") != "playlist":
            return info
        entries = [entry for entry in info.get("entries") or [] if entry]
        for extra in entries[1:]:
            for download in extra.get("requested_downloads") or []:
                path = download.get("filepath")
                if path and os.path.exists(path):
                    os.remove(path)
        return entries[0] if entries else info

    @staticmethod
    def _downloaded_path(info, output_dir):
        """Where yt-dlp actually saved the video. The file is named after
        the media id, which is not always the id in the post URL."""
        for download in info.get("requested_downloads") or []:
            if download.get("filepath"):
                return download["filepath"]
        return f"{output_dir}/{info['id']}.mp4"

    def fetch_details(self, url):
        """Read upload date, length and counts without downloading the video."""
        try:
            return self.video_stats(self._extract(url, details=True))
        except Exception as error:
            print(
                f"Could not read details for {url} (upload date, length and "
                f"counts left blank): {explain_error(error)}"
            )
            return None

    @staticmethod
    def video_stats(info):
        uploaded_at = info.get("timestamp")
        upload_date = info.get("upload_date")
        if uploaded_at is None and upload_date:
            try:
                uploaded_at = datetime.strptime(upload_date, "%Y%m%d").replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except ValueError:
                pass
        stats = {
            "uploaded_at": uploaded_at,
            "likes": info.get("like_count"),
        }
        # Chroma metadata cannot hold None, and bools are ints in Python.
        return {
            key: value for key, value in stats.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }

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
        # The VLM is one shared model, so worker threads take turns with it.
        with self._generator_lock:
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

        images = [Image.fromarray(frame) for frame in frames[:3]]
        messages = [{
            "role": "user",
            "content": [
                *({"type": "image", "image": image} for image in images),
                {"type": "text", "text": prompt},
            ],
        }]
        with self._generator_lock:
            self._load_visual_description_model()
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

    def features(self, overrides=None):
        """The processing steps to run: this processor's own settings, with
        any of FEATURES in overrides replacing them."""
        chosen = {name: getattr(self, name) for name in FEATURES}
        chosen.update(
            (name, bool(value)) for name, value in (overrides or {}).items()
            if name in FEATURES
        )
        return chosen

    def _build_description(self, video, features=None):
        """Combine the enabled frame-based descriptions (OCR and VLM)."""
        features = features or self.features()
        parts = []
        if features["use_ocr"]:
            parts.append(self.extract_text_from_frames(video.frames))
        if features["use_visual_description"] and video.frames:
            prompt = self.get_prompt(
                video.transcript,
                video.caption,
                video.user,
            )
            parts.append(
                self.generate_description_with_frames(video.frames, prompt)
            )
        return " ".join(part for part in parts if part).strip()

    def process_video(self, url, features=None, speed=None):
        """Download a video and build its searchable text and embedding.

        features: overrides for the optional steps in FEATURES, such as
            {"use_comments": False}; others use this processor's settings.
        speed: how aggressively to download, one of LOAD_SPEEDS.
        """
        features = self.features(features)
        video = Video(url)
        step = "download"
        try:
            result = self.download(
                video.url, comments=features["use_comments"], speed=speed
            )
            if result is None:
                # download() already printed why it was skipped.
                return None
            (
                video.path, video.caption, video.user, comments, video.stats
            ) = result
            # Downloads wait on the network and this part on the CPU, so it
            # has its own limit: other videos keep downloading meanwhile.
            with self.cpu_slots:
                if features["use_transcript"]:
                    step = "transcription"
                    video.transcript = self.transcribe(video.path)
                if features["use_ocr"] or features["use_visual_description"]:
                    step = "keyframe extraction"
                    video.frames = self.extract_keyframes(video.path)
                step = "frame description (OCR/visual)"
                video.description = self._build_description(video, features)
                if features["use_comments"]:
                    step = "comment selection"
                    video.comments = self.select_comments(
                        comments,
                        video.user,
                        " ".join((
                            video.caption,
                            video.transcript,
                            video.description,
                        )),
                    )
                step = "embedding"
                video.embedding = self.embed_model.encode(video.to_document())
            print(f"Processed {url}")
            return video
        except Exception as error:
            print(
                f"Skipping {url}: {ProcessingError(step, error)}. "
                "It was downloaded but not added to the library."
            )
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
        with ThreadPoolExecutor(
            max_workers=self.download_workers + self.cpu_workers
        ) as executor:
            for video in executor.map(
                self.process_video, urls[start_number:stop_number]
            ):
                if video is None:
                    failed += 1
                    continue
                collection.add(video)
        return failed
