import base64
import hashlib
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


class KeyframeExtractor:
    def __init__(
        self,
        *,
        frame_interval_seconds: int = 6,
        grid_size: tuple[int, int] = (2, 2),
        max_frames: int = 1000,
        unit_size: tuple[int, int] = (960, 540),
        quality: int = 80,
    ) -> None:
        if frame_interval_seconds < 1:
            raise ValueError("frame interval must be at least 1 second")
        if len(grid_size) != 2 or any(size < 1 for size in grid_size):
            raise ValueError("grid size must contain two positive values")
        if max_frames < 1:
            raise ValueError("max frames must be positive")
        self.frame_interval_seconds = frame_interval_seconds
        self.grid_size = grid_size
        self.max_frames = max_frames
        self.unit_size = unit_size
        self.quality = quality

    def extract(self, video_path: Path, output_dir: Path) -> tuple[str, ...]:
        frame_dir = output_dir / "frames"
        grid_dir = output_dir / "grids"
        frame_dir.mkdir(parents=True, exist_ok=True)
        grid_dir.mkdir(parents=True, exist_ok=True)
        duration = self._probe_duration(video_path)
        timestamps = list(range(0, max(1, int(duration) + 1), self.frame_interval_seconds))[: self.max_frames]
        frame_paths = self._extract_frames(video_path, frame_dir, timestamps)
        frame_paths = self._deduplicate(frame_paths)
        group_size = self.grid_size[0] * self.grid_size[1]
        images: list[str] = []
        for index in range(0, len(frame_paths), group_size):
            group = frame_paths[index : index + group_size]
            if len(group) < group_size:
                continue
            grid_path = self._compose_grid(group, grid_dir / f"grid_{index // group_size + 1}.jpg")
            encoded = base64.b64encode(grid_path.read_bytes()).decode("ascii")
            images.append(f"data:image/jpeg;base64,{encoded}")
        return tuple(images)

    def _probe_duration(self, video_path: Path) -> float:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return float(result.stdout.strip())

    def _extract_frames(self, video_path: Path, frame_dir: Path, timestamps: list[int]) -> list[Path]:
        paths: list[Path] = []
        for timestamp in timestamps:
            path = frame_dir / f"frame_{timestamp:08d}.jpg"
            subprocess.run(
                [
                    "ffmpeg",
                    "-ss",
                    str(timestamp),
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    "-y",
                    str(path),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                ],
                check=True,
                capture_output=True,
            )
            if path.exists():
                paths.append(path)
        return paths

    def _deduplicate(self, paths: list[Path]) -> list[Path]:
        unique: list[Path] = []
        previous_hash: str | None = None
        for path in paths:
            digest = hashlib.md5(path.read_bytes()).hexdigest()
            if digest == previous_hash:
                path.unlink(missing_ok=True)
                continue
            unique.append(path)
            previous_hash = digest
        return unique

    def _compose_grid(self, paths: list[Path], output_path: Path) -> Path:
        width, height = self.unit_size
        columns, rows = self.grid_size
        grid = Image.new("RGB", (width * columns, height * rows), "white")
        font = ImageFont.load_default()
        for index, path in enumerate(paths):
            with Image.open(path) as source:
                image = source.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
            draw = ImageDraw.Draw(image)
            match = re.search(r"frame_(\d+)\.jpg$", path.name)
            seconds = int(match.group(1)) if match else 0
            draw.text((10, 10), f"{seconds // 60:02d}:{seconds % 60:02d}", fill="yellow", font=font, stroke_width=1, stroke_fill="black")
            grid.paste(image, ((index % columns) * width, (index // columns) * height))
        grid.save(output_path, format="JPEG", quality=self.quality)
        return output_path


def extract_keyframe_images(
    video_path: Path,
    output_dir: Path,
    *,
    frame_interval_seconds: int = 6,
    grid_size: tuple[int, int] = (2, 2),
) -> tuple[str, ...]:
    return KeyframeExtractor(
        frame_interval_seconds=frame_interval_seconds,
        grid_size=grid_size,
    ).extract(video_path, output_dir)
