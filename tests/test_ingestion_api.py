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
            database_url=f"sqlite:///{tmp_path / 'api.db'}",
            uploads_dir=str(tmp_path / "uploads"),
            ingestion_temp_dir=str(tmp_path / "ingestion"),
        )
    )
    executor = RecordingExecutor()
    app.state.ingestion_executor = executor
    return TestClient(app), executor


def test_create_ingestion_returns_accepted_job_and_queues_work(tmp_path: Path) -> None:
    client, executor = make_client(tmp_path)

    response = client.post("/ingestions", json={"url": "https://youtu.be/abcdefghijk"})

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["job_id"].startswith("ing_")
    assert len(executor.calls) == 1

    status = client.get(f"/ingestions/{payload['job_id']}")
    assert status.status_code == 200
    assert status.json()["stage"] == "queued"


def test_missing_ingestion_returns_404(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)

    assert client.get("/ingestions/ing_missing").status_code == 404
