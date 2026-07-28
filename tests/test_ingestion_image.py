from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple[object, ...]]] = []

    def submit(self, function: object, *args: object) -> None:
        self.calls.append((function, args))


def make_client(tmp_path: Path) -> tuple[TestClient, RecordingExecutor]:
    app = create_app(
        settings=Settings(
            database_url=f"sqlite:///{tmp_path / 'image.db'}",
            uploads_dir=str(tmp_path / "uploads"),
            ingestion_temp_dir=str(tmp_path / "ingestion"),
            ingestion_max_upload_bytes=16,
        )
    )
    executor = RecordingExecutor()
    app.state.ingestion_executor = executor
    return TestClient(app), executor


def test_image_ingestion_queues_supported_upload(tmp_path: Path) -> None:
    client, executor = make_client(tmp_path)

    response = client.post("/ingestions/image", files={"file": ("note.png", b"image-bytes", "image/png")})

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert len(executor.calls) == 1


def test_image_ingestion_rejects_non_image_or_large_upload(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)

    wrong_type = client.post("/ingestions/image", files={"file": ("note.txt", b"x", "text/plain")})
    too_large = client.post("/ingestions/image", files={"file": ("big.png", b"x" * 17, "image/png")})

    assert wrong_type.status_code == 415
    assert too_large.status_code == 413
