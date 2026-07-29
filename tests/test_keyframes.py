import base64
from pathlib import Path

from PIL import Image

from app.ingestion.keyframes import KeyframeExtractor


def test_keyframe_extractor_composes_complete_grids_and_returns_data_urls(monkeypatch, tmp_path: Path) -> None:
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    frame_paths: list[Path] = []

    def fake_run(command, **_kwargs):
        if command[0] == "ffprobe":
            return type("Completed", (), {"stdout": "5\n"})()
        output = Path(command[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        index = len(frame_paths)
        Image.new("RGB", (16, 9), (index, 0, 0)).save(output, format="JPEG")
        frame_paths.append(output)
        return type("Completed", (), {"stdout": ""})()

    monkeypatch.setattr("app.ingestion.keyframes.subprocess.run", fake_run)

    images = KeyframeExtractor(frame_interval_seconds=1, grid_size=(2, 2), max_frames=5).extract(video_path, tmp_path)

    assert len(images) == 1
    assert images[0].startswith("data:image/jpeg;base64,")
    encoded = images[0].split(",", 1)[1]
    assert base64.b64decode(encoded).startswith(b"\xff\xd8")


def test_keyframe_extractor_rejects_invalid_configuration() -> None:
    try:
        KeyframeExtractor(frame_interval_seconds=0, grid_size=(2, 2))
    except ValueError as exc:
        assert "interval" in str(exc)
    else:
        raise AssertionError("invalid interval must be rejected")
