from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Iterable


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".gif",
    ".webp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".m4v",
    ".mkv",
    ".mov",
    ".avi",
    ".wmv",
    ".webm",
    ".mpg",
    ".mpeg",
    ".ts",
    ".m2ts",
    ".3gp",
}


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


@dataclass(frozen=True, slots=True)
class ImageFingerprint:
    width: int
    height: int
    content_hash: str
    dhash: int
    thumbnail: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True, slots=True)
class VideoFingerprint:
    width: int
    height: int
    duration: float
    frame_hashes: tuple[int, ...]


VideoMetadata = tuple[int, int, float]


def image_fingerprint(path: str, hash_size: int = 8, thumbnail_size: int = 32) -> ImageFingerprint | None:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None

    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            rgb_image = image.convert("RGB")
            width, height = rgb_image.size
            content_hash = hashlib.sha256(rgb_image.tobytes()).hexdigest()

            thumbnail_image = rgb_image.resize((thumbnail_size, thumbnail_size), _resampling_filter(Image))
            thumbnail = tuple(thumbnail_image.getdata())

            gray_image = rgb_image.convert("L")
            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            gray_image = gray_image.resize((hash_size + 1, hash_size), resampling)
            pixels = list(gray_image.getdata())
    except Exception:
        return None

    value = 0
    for row in range(hash_size):
        row_start = row * (hash_size + 1)
        for col in range(hash_size):
            left = pixels[row_start + col]
            right = pixels[row_start + col + 1]
            value = (value << 1) | int(left > right)
    return ImageFingerprint(
        width=width,
        height=height,
        content_hash=content_hash,
        dhash=value,
        thumbnail=thumbnail,
    )


def image_dhash(path: str, hash_size: int = 8) -> int | None:
    fingerprint = image_fingerprint(path, hash_size=hash_size)
    return fingerprint.dhash if fingerprint else None


def images_match_strictly(
    left: ImageFingerprint,
    right: ImageFingerprint,
    dhash_threshold: int,
    thumbnail_mean_threshold: float = 6.0,
) -> tuple[str, int] | None:
    if left.width != right.width or left.height != right.height:
        return None
    if left.content_hash == right.content_hash:
        return ("image-content", 100)

    distance = hamming_distance(left.dhash, right.dhash)
    if distance > dhash_threshold:
        return None
    if _thumbnail_mean_difference(left.thumbnail, right.thumbnail) > thumbnail_mean_threshold:
        return None
    return ("image", _similarity_score(distance, dhash_threshold))


def _thumbnail_mean_difference(
    left: tuple[tuple[int, int, int], ...],
    right: tuple[tuple[int, int, int], ...],
) -> float:
    if len(left) != len(right) or not left:
        return float("inf")
    difference = 0
    for left_pixel, right_pixel in zip(left, right):
        difference += abs(left_pixel[0] - right_pixel[0])
        difference += abs(left_pixel[1] - right_pixel[1])
        difference += abs(left_pixel[2] - right_pixel[2])
    return difference / (len(left) * 3)


def _resampling_filter(image_module: object) -> int:
    return getattr(getattr(image_module, "Resampling", image_module), "LANCZOS")


def image_similarity_available() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def video_fingerprint(
    path: str,
    sample_count: int = 5,
    metadata: VideoMetadata | None = None,
) -> VideoFingerprint | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None

    metadata = metadata if metadata is not None else probe_video_metadata(path)
    if metadata is None:
        return None
    width, height, duration = metadata
    if duration <= 0 or width <= 0 or height <= 0:
        return None

    if duration > 1:
        positions = [
            max(0.0, duration * ratio)
            for ratio in _sample_ratios(sample_count)
        ]
    else:
        positions = [0.0 for _index in range(sample_count)]

    hashes: list[int] = []
    hashes.extend(_frame_dhashes(ffmpeg, path, positions))

    if not hashes:
        return None

    while len(hashes) < sample_count:
        hashes.append(hashes[-1])

    return VideoFingerprint(
        width=width,
        height=height,
        duration=duration,
        frame_hashes=tuple(hashes[:sample_count]),
    )


def videos_match_strictly(
    left: VideoFingerprint,
    right: VideoFingerprint,
    total_threshold: int,
    frame_threshold: int,
    min_matching_frames: int,
) -> tuple[str, int] | None:
    if left.width != right.width or left.height != right.height:
        return None
    if not _durations_match(left.duration, right.duration):
        return None
    if len(left.frame_hashes) != len(right.frame_hashes) or not left.frame_hashes:
        return None

    distances = [
        hamming_distance(left_hash, right_hash)
        for left_hash, right_hash in zip(left.frame_hashes, right.frame_hashes)
    ]
    matching_frames = sum(distance <= frame_threshold for distance in distances)
    total_distance = sum(distances)
    if matching_frames < min_matching_frames or total_distance > total_threshold:
        return None
    return ("video", _similarity_score(total_distance, total_threshold))


def probe_video_metadata(path: str) -> VideoMetadata | None:
    return _probe_video_metadata(path)


def video_metadata_can_match(left: VideoMetadata, right: VideoMetadata) -> bool:
    left_width, left_height, left_duration = left
    right_width, right_height, right_duration = right
    return (
        left_width == right_width
        and left_height == right_height
        and _durations_match(left_duration, right_duration)
    )


def _durations_match(left: float, right: float) -> bool:
    difference = abs(left - right)
    return difference <= max(0.5, max(left, right) * 0.01)


def _sample_ratios(sample_count: int) -> tuple[float, ...]:
    if sample_count <= 1:
        return (0.5,)
    step = 0.8 / (sample_count - 1)
    return tuple(0.1 + (step * index) for index in range(sample_count))


def _probe_video_metadata(path: str) -> VideoMetadata | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=width,height:format=duration",
        "-of",
        "default=noprint_wrappers=1",
        path,
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            creationflags=_creation_flags(),
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    try:
        values: dict[str, str] = {}
        stdout = completed.stdout.decode("utf-8", errors="replace")
        for line in stdout.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values.setdefault(key.strip(), value.strip())
        width = int(values["width"])
        height = int(values["height"])
        duration = float(values["duration"])
    except (KeyError, ValueError):
        return None
    return width, height, duration


def _frame_dhash(ffmpeg: str, path: str, position: float, hash_size: int = 8) -> int | None:
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        f"{position:.3f}",
        "-i",
        path,
        "-frames:v",
        "1",
        "-vf",
        f"scale={hash_size + 1}:{hash_size},format=gray",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            creationflags=_creation_flags(),
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None

    expected = (hash_size + 1) * hash_size
    frame = completed.stdout[:expected]
    if len(frame) != expected:
        return None

    return _dhash_from_gray_frame(frame, hash_size)


def _frame_dhashes(ffmpeg: str, path: str, positions: list[float], hash_size: int = 8) -> list[int]:
    if not positions:
        return []

    expected_frame_size = (hash_size + 1) * hash_size
    expected_size = expected_frame_size * len(positions)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
    ]
    filter_parts: list[str] = []
    labels: list[str] = []
    for index, position in enumerate(positions):
        command.extend(["-ss", f"{position:.3f}", "-i", path])
        label = f"v{index}"
        labels.append(f"[{label}]")
        filter_parts.append(
            f"[{index}:v]trim=end_frame=1,setpts=PTS-STARTPTS,"
            f"scale={hash_size + 1}:{hash_size},format=gray[{label}]"
        )

    filter_complex = (
        ";".join(filter_parts)
        + ";"
        + "".join(labels)
        + f"concat=n={len(positions)}:v=1:a=0[out]"
    )
    command.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            "-vsync",
            "0",
            "-frames:v",
            str(len(positions)),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
    )
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20 * len(positions),
            creationflags=_creation_flags(),
        )
    except Exception:
        return _frame_dhashes_individually(ffmpeg, path, positions, hash_size)
    if completed.returncode != 0 or len(completed.stdout) < expected_size:
        return _frame_dhashes_individually(ffmpeg, path, positions, hash_size)

    hashes: list[int] = []
    for index in range(len(positions)):
        offset = index * expected_frame_size
        frame = completed.stdout[offset:offset + expected_frame_size]
        hashes.append(_dhash_from_gray_frame(frame, hash_size))
    return hashes


def _frame_dhashes_individually(
    ffmpeg: str,
    path: str,
    positions: list[float],
    hash_size: int,
) -> list[int]:
    hashes: list[int] = []
    for position in positions:
        frame_hash = _frame_dhash(ffmpeg, path, position, hash_size)
        if frame_hash is not None:
            hashes.append(frame_hash)
    return hashes


def _dhash_from_gray_frame(frame: bytes, hash_size: int) -> int:
    value = 0
    for row in range(hash_size):
        row_start = row * (hash_size + 1)
        for col in range(hash_size):
            left = frame[row_start + col]
            right = frame[row_start + col + 1]
            value = (value << 1) | int(left > right)
    return value


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class _BKNode:
    value: int
    item_ids: list[int] = field(default_factory=list)
    children: dict[int, "_BKNode"] = field(default_factory=dict)


class BKTree:
    def __init__(self, distance: Callable[[int, int], int] = hamming_distance) -> None:
        self._root: _BKNode | None = None
        self._distance = distance

    def add(self, value: int, item_id: int) -> None:
        if self._root is None:
            self._root = _BKNode(value=value, item_ids=[item_id])
            return

        node = self._root
        while True:
            distance = self._distance(value, node.value)
            if distance == 0:
                node.item_ids.append(item_id)
                return
            child = node.children.get(distance)
            if child is None:
                node.children[distance] = _BKNode(value=value, item_ids=[item_id])
                return
            node = child

    def query(self, value: int, threshold: int) -> Iterable[tuple[int, int]]:
        if self._root is None:
            return []

        matches: list[tuple[int, int]] = []
        pending = [self._root]
        while pending:
            node = pending.pop()
            distance = self._distance(value, node.value)
            if distance <= threshold:
                matches.extend((item_id, distance) for item_id in node.item_ids)
            low = distance - threshold
            high = distance + threshold
            for child_distance, child in node.children.items():
                if low <= child_distance <= high:
                    pending.append(child)
        return matches


def _similarity_score(distance: int, threshold: int) -> int:
    if threshold <= 0:
        return 100 if distance == 0 else 0
    return max(0, min(100, 100 - round((distance / threshold) * 20)))
