"""faster-whisper handling adapted from BiliNote's WhisperTranscriber.

Copyright (c) 2024 Jeffery Huang. Licensed under the MIT License.
"""

from collections.abc import Iterable
from collections.abc import Callable
from typing import Protocol

from app.ingestion.domain import EvidenceOrigin, Transcript, TranscriptSegment


class WhisperSegment(Protocol):
    start: float
    end: float
    text: str


ProgressCallback = Callable[[int, str], None]


def normalize_bilinote_whisper_segments(
    *, language: str | None, segments: Iterable[tuple[float, float, str]]
) -> Transcript:
    normalized = tuple(
        TranscriptSegment(start_seconds=start, end_seconds=end, text=text.strip())
        for start, end, text in segments
        if text.strip()
    )
    return Transcript(
        language=language,
        origin=EvidenceOrigin.ASR,
        full_text=" ".join(segment.text for segment in normalized),
        segments=normalized,
    )


class BiliNoteWhisperTranscriber:
    def __init__(self, *, model_size: str = "base", device: str = "cpu", compute_type: str = "int8") -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model: object | None = None

    def transcribe(self, file_path: str, *, progress_callback: ProgressCallback | None = None) -> Transcript:
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                model_size_or_path=self._model_size,
                device=self._device,
                compute_type=self._compute_type,
            )
        segments_raw, info = self._model.transcribe(file_path)

        duration = float(getattr(info, "duration", 0) or 0)

        def normalized_segments() -> Iterable[tuple[float, float, str]]:
            for segment in segments_raw:
                if progress_callback:
                    if duration > 0:
                        percent = min(85, 45 + int((float(segment.end) / duration) * 40))
                        elapsed = min(float(segment.end), duration)
                        progress_callback(percent, f"Whisper 转写 {elapsed:.0f}/{duration:.0f} 秒")
                    else:
                        progress_callback(50, "Whisper 正在转写")
                yield segment.start, segment.end, segment.text

        return normalize_bilinote_whisper_segments(
            language=getattr(info, "language", None),
            segments=normalized_segments(),
        )
