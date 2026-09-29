import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

# Change this import if your Processor class lives in a different module.
# For example: from src.processor import Processor
from src.processor import Processor


@pytest.fixture
def processor(monkeypatch, tmp_path):
    """Create a Processor without loading the real Whisper/yt-dlp models."""
    mock_whisper = MagicMock()
    mock_ydl = MagicMock()

    monkeypatch.setattr(
        "src.processor.WhisperModel",
        lambda *args, **kwargs: mock_whisper,
    )
    monkeypatch.setattr(
        "src.processor.yt_dlp.YoutubeDL",
        lambda *args, **kwargs: mock_ydl,
    )

    # Patch __init__ dependencies by constructing the object normally after
    # replacing the expensive external components.
    p = Processor(embed_model=MagicMock(), output_dir=str(tmp_path))

    return p


def test_normalize_text(processor):
    assert processor._normalize_text(
        "  hello   world\nthis is   a test  "
    ) == "hello world this is a test"

    assert processor._normalize_text(None) == ""


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.instagram.com/reel/123/", "instagram"),
        ("https://instagram.com/p/123/", "instagram"),
        ("https://www.tiktok.com/@user/video/123", "tiktok"),
        ("https://example.com/video/123", "unknown"),
    ],
)
def test_get_platform(url, expected):
    assert Processor.get_platform(url) == expected


def test_get_prompt(processor):
    prompt = processor.get_prompt(
        transcript="A person is cooking.",
        caption="Easy recipe",
        user="test_user",
    )

    assert "A person is cooking." in prompt
    assert "Easy recipe" in prompt
    assert "test_user" in prompt
    assert "Do not invent details." in prompt


def test_transcribe(processor):
    processor.transcribe_model.transcribe.return_value = (
        [
            SimpleNamespace(text=" Hello   world ", end=2.0),
            SimpleNamespace(text=" This is a test ", end=5.0),
            SimpleNamespace(text=" ignored after limit ", end=200.0),
        ],
        None,
    )

    result = processor.transcribe("video.mp4")

    assert result == "Hello world This is a test"

    processor.transcribe_model.transcribe.assert_called_once()


def test_transcribe_ignores_empty_segments(processor):
    processor.transcribe_model.transcribe.return_value = (
        [
            SimpleNamespace(text="   ", end=1.0),
            SimpleNamespace(text="Hello", end=2.0),
        ],
        None,
    )

    assert processor.transcribe("video.mp4") == "Hello"


def test_download_tiktok(processor):
    processor.ydl_tiktok.extract_info.return_value = {
        "id": "abc123",
        "description": "A TikTok",
        "uploader": "user1",
    }

    result = processor.download(
        "https://www.tiktok.com/@user/video/abc123",
        output_dir="videos",
    )

    assert result == (
        "videos/abc123.mp4",
        "A TikTok",
        "user1",
        [],
    )

    processor.ydl_tiktok.extract_info.assert_called_once_with(
        "https://www.tiktok.com/@user/video/abc123",
        download=True,
    )


def test_download_instagram(processor):
    processor.ydl_instagram.extract_info.return_value = {
        "id": "xyz789",
        "description": "An Instagram reel",
        "uploader": "user2",
    }

    result = processor.download(
        "https://www.instagram.com/reel/xyz789/",
        output_dir="videos",
    )

    assert result == (
        "videos/xyz789.mp4",
        "An Instagram reel",
        "user2",
        [],
    )

    processor.ydl_instagram.extract_info.assert_called_once()


def test_download_returns_none_on_error(processor, capsys):
    processor.ydl_tiktok.extract_info.side_effect = Exception("network error")

    result = processor.download(
        "https://www.tiktok.com/@user/video/123"
    )

    assert result is None
    assert "Skipping" in capsys.readouterr().out


def test_select_comments_keeps_only_new_descriptive_comments(processor):
    comments = [
        {"text": "omg so cute 😍", "like_count": 900},
        {"text": "@friend1 @friend2", "like_count": 800},
        {"text": "Love this pasta recipe!!", "like_count": 700},
        {"text": "check out my page for more recipes", "like_count": 600},
        {"text": "That's chef Massimo Bottura in Modena", "like_count": 50},
        {"text": "Massimo Bottura in Modena, Italy!", "like_count": 10},
        {"text": "Recipe is from his cookbook Bread Is Gold",
         "author": "chefuser", "like_count": 0},
    ]

    result = processor.select_comments(
        comments,
        "chefuser",
        "Easy pasta recipe with tomato sauce",
    )

    assert result == (
        "Recipe is from his cookbook Bread Is Gold | "
        "That's chef Massimo Bottura in Modena"
    )


def test_select_comments_keeps_descriptive_words_and_merges_word_forms(
    processor,
):
    comments = [
        {"text": "Such a cute fluffy puppy", "like_count": 20},
        {"text": "hahaha I don't even know lol", "like_count": 15},
        {"text": "Cutest fluffy puppies ever", "like_count": 5},
    ]

    result = processor.select_comments(comments, "owner", "My dog video")

    assert result == "Such a cute fluffy puppy"


def test_extract_keyframes_no_frames(monkeypatch, processor):
    mock_video = MagicMock()
    mock_video.get.return_value = 0

    monkeypatch.setattr(
        "src.processor.cv2.VideoCapture",
        lambda path: mock_video,
    )

    assert processor.extract_keyframes("video.mp4") == []
    mock_video.release.assert_called_once()


def test_extract_keyframes(monkeypatch, processor):
    mock_video = MagicMock()

    # Video contains 100 frames.
    mock_video.get.return_value = 100
    mock_video.read.return_value = (
        True,
        np.zeros((10, 10, 3), dtype=np.uint8),
    )

    monkeypatch.setattr(
        "src.processor.cv2.VideoCapture",
        lambda path: mock_video,
    )

    frames = processor.extract_keyframes("video.mp4", max_frames=3)

    assert len(frames) == 3
    assert all(frame.shape == (10, 10, 3) for frame in frames)
    mock_video.release.assert_called_once()


def test_run_ocr_variants(processor, monkeypatch):
    processor.ocr_enabled = True

    fake_data = {
        "text": ["Hello", "world", ""],
        "conf": ["80", "70", "20"],
    }

    monkeypatch.setattr(
        "src.processor.pytesseract.image_to_data",
        lambda *args, **kwargs: fake_data,
    )

    result = processor._run_ocr_variants([np.zeros((10, 10), dtype=np.uint8)])

    assert result == ["Hello world"]


def test_ocr_candidates_disabled(processor):
    processor.ocr_enabled = False

    frame = np.zeros((100, 100, 3), dtype=np.uint8)

    assert processor._ocr_candidates(frame) == []


def test_extract_text_from_frames(monkeypatch, processor):
    processor._ocr_candidates = MagicMock(
        side_effect=[
            ["Hello World"],
            ["Hello World"],
            ["Some Other Text"],
        ]
    )

    frames = [
        np.zeros((10, 10, 3), dtype=np.uint8),
        np.zeros((10, 10, 3), dtype=np.uint8),
        np.zeros((10, 10, 3), dtype=np.uint8),
    ]

    result = processor.extract_text_from_frames(frames)

    assert "Hello World" in result
    assert "Some Other Text" in result


def test_generate_description(processor):
    processor.generator = MagicMock(
        return_value=[{"generated_text": "A person cooking."}]
    )

    result = processor.generate_description("Describe this video.")

    assert result == "A person cooking."
    processor.generator.assert_called_once()


def test_process_video(monkeypatch, processor):
    fake_video = MagicMock()
    fake_video.url = "https://www.tiktok.com/@user/video/123"
    fake_video.path = None

    monkeypatch.setattr(
        "src.processor.Video",
        lambda url: fake_video,
    )

    processor.download = MagicMock(
        return_value=("videos/123.mp4", "caption", "user", [])
    )
    processor.transcribe = MagicMock(return_value="transcript")
    processor.extract_keyframes = MagicMock(return_value=[])
    processor.extract_text_from_frames = MagicMock(return_value="OCR text")

    processor.embed_model.encode.return_value = [0.1, 0.2, 0.3]

    result = processor.process_video(fake_video.url)

    assert result is fake_video
    assert fake_video.path == "videos/123.mp4"
    assert fake_video.caption == "caption"
    assert fake_video.user == "user"
    assert fake_video.transcript == "transcript"
    assert fake_video.description == "OCR text"
    assert fake_video.embedding == [0.1, 0.2, 0.3]

    processor.download.assert_called_once_with(fake_video.url)
    processor.transcribe.assert_called_once_with("videos/123.mp4")
    processor.extract_keyframes.assert_called_once_with("videos/123.mp4")
    processor.extract_text_from_frames.assert_called_once()


def test_process_video_returns_none_when_download_fails(
    monkeypatch, processor
):
    fake_video = MagicMock()
    fake_video.url = "https://www.tiktok.com/@user/video/123"
    fake_video.path = None

    monkeypatch.setattr(
        "src.processor.Video",
        lambda url: fake_video,
    )

    processor.download = MagicMock(return_value=None)

    assert processor.process_video(fake_video.url) is None


def test_load_videos(monkeypatch, processor, tmp_path):
    urls = [
        "https://www.instagram.com/reel/1/",
        "https://www.tiktok.com/@user/video/2",
        "https://www.instagram.com/reel/3/",
    ]

    json_path = tmp_path / "videos.json"
    json_path.write_text(json.dumps(urls))

    collection = MagicMock()

    video1 = MagicMock()
    video2 = MagicMock()

    processor.process_video = MagicMock(
        side_effect=[video1, None, video2]
    )

    failed = processor.load_videos(
        collection=collection,
        path=str(json_path),
        start_number=0,
        stop_number=3,
    )

    assert failed == 1
    assert processor.process_video.call_count == 3
    assert collection.add.call_count == 2
    collection.add.assert_any_call(video1)
    collection.add.assert_any_call(video2)


def test_load_videos_respects_range(
    processor, tmp_path
):
    urls = ["url1", "url2", "url3", "url4"]

    json_path = tmp_path / "videos.json"
    json_path.write_text(json.dumps(urls))

    collection = MagicMock()
    processor.process_video = MagicMock(return_value=MagicMock())

    processor.load_videos(
        collection=collection,
        path=str(json_path),
        start_number=1,
        stop_number=3,
    )

    assert processor.process_video.call_count == 2
    processor.process_video.assert_any_call("url2")
    processor.process_video.assert_any_call("url3")


def _stub_pipeline(processor):
    processor.download = MagicMock(
        return_value=("videos/1.mp4", "caption", "user", [
            {"text": "Nice knife skills chef", "like_count": 1},
        ])
    )
    processor.transcribe = MagicMock(return_value="transcript")
    processor.extract_keyframes = MagicMock(return_value=["frame"])
    processor.extract_text_from_frames = MagicMock(return_value="OCR text")
    processor.generate_description_with_frames = MagicMock(
        return_value="VLM text"
    )
    processor.embed_model.encode.return_value = [0.1]


def test_defaults_enable_everything_but_visual_description(processor):
    assert processor.use_transcript
    assert processor.use_ocr
    assert processor.use_comments
    assert not processor.use_visual_description


def test_process_video_default_toggles(processor):
    _stub_pipeline(processor)

    video = processor.process_video("https://www.tiktok.com/@u/video/1")

    assert video.transcript == "transcript"
    assert video.description == "OCR text"
    assert "knife" in video.comments
    processor.generate_description_with_frames.assert_not_called()


def test_process_video_all_toggles_off_skips_work(processor):
    _stub_pipeline(processor)
    processor.use_transcript = False
    processor.use_ocr = False
    processor.use_comments = False

    video = processor.process_video("https://www.tiktok.com/@u/video/1")

    assert video.transcript == ""
    assert video.description == ""
    assert video.comments == ""
    processor.transcribe.assert_not_called()
    processor.extract_keyframes.assert_not_called()
    processor.extract_text_from_frames.assert_not_called()


def test_process_video_visual_description_only(processor):
    _stub_pipeline(processor)
    processor.use_ocr = False
    processor.use_visual_description = True

    video = processor.process_video("https://www.tiktok.com/@u/video/1")

    assert video.description == "VLM text"
    processor.extract_text_from_frames.assert_not_called()


def test_process_video_ocr_and_visual_description_combined(processor):
    _stub_pipeline(processor)
    processor.use_visual_description = True

    video = processor.process_video("https://www.tiktok.com/@u/video/1")

    assert video.description == "OCR text VLM text"
