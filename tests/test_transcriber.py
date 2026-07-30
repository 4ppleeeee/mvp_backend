from types import SimpleNamespace


def test_whisper_transcriber_reports_progress_from_segment_timestamps(monkeypatch) -> None:
    class FakeModel:
        def transcribe(self, _: str):
            segments = iter(
                [
                    SimpleNamespace(start=0, end=10, text="第一段"),
                    SimpleNamespace(start=50, end=60, text="第二段"),
                ]
            )
            return segments, SimpleNamespace(language="zh", duration=100)

    class FakeWhisperModule:
        WhisperModel = lambda *_args, **_kwargs: FakeModel()

    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", FakeWhisperModule())
    from app.ingestion.transcriber import BiliNoteWhisperTranscriber

    progress: list[tuple[int, str]] = []
    result = BiliNoteWhisperTranscriber().transcribe(
        "audio.m4a", progress_callback=lambda percent, message: progress.append((percent, message))
    )

    assert result.full_text == "第一段 第二段"
    assert progress == [(49, "Whisper 转写 10/100 秒"), (69, "Whisper 转写 60/100 秒")]
