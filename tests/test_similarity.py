from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fdedup.models import ScanOptions, ScanResult
from fdedup.scanner import scan
from fdedup.similarity import (
    VideoFingerprint,
    image_fingerprint,
    images_match_strictly,
    _probe_video_metadata,
    videos_match_strictly,
)


class ImageSimilarityTests(unittest.TestCase):
    def test_same_pixels_different_formats_are_grouped_as_image_content(self) -> None:
        Image = _require_pillow()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = Image.new("RGB", (64, 64), (10, 80, 160))
            image.save(root / "same.png")
            image.save(root / "same.bmp")

            result = scan(
                ScanOptions(
                    root_a=str(root),
                    root_b=str(root),
                    enable_image_similarity=True,
                    enable_video_similarity=False,
                    min_similar_size=0,
                )
            )

            self.assertEqual(len(result.groups), 1)
            self.assertEqual(result.groups[0].reasons, ("image-content",))

    def test_image_parallelism_does_not_change_scan_result(self) -> None:
        Image = _require_pillow()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index in range(4):
                image = Image.new("RGB", (64, 64), (10, 80, 160))
                image.save(root / f"same-{index}.png")
            Image.new("RGB", (64, 64), (180, 20, 40)).save(root / "different.png")

            serial = scan(
                ScanOptions(
                    root_a=str(root),
                    root_b=str(root),
                    enable_image_similarity=True,
                    enable_video_similarity=False,
                    min_similar_size=0,
                    image_workers=1,
                )
            )
            parallel = scan(
                ScanOptions(
                    root_a=str(root),
                    root_b=str(root),
                    enable_image_similarity=True,
                    enable_video_similarity=False,
                    min_similar_size=0,
                    image_workers=4,
                )
            )

            self.assertEqual(_group_names(serial), _group_names(parallel))

    def test_same_dhash_but_different_color_is_not_image_match(self) -> None:
        Image = _require_pillow()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            red_path = root / "red.png"
            blue_path = root / "blue.png"
            Image.new("RGB", (64, 64), (255, 0, 0)).save(red_path)
            Image.new("RGB", (64, 64), (0, 0, 255)).save(blue_path)

            red = image_fingerprint(str(red_path))
            blue = image_fingerprint(str(blue_path))

            self.assertIsNotNone(red)
            self.assertIsNotNone(blue)
            self.assertIsNone(images_match_strictly(red, blue, dhash_threshold=4))  # type: ignore[arg-type]


class VideoSimilarityTests(unittest.TestCase):
    def test_video_metadata_probe_reads_process_output_as_bytes(self) -> None:
        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            self.assertNotIn("text", kwargs)
            self.assertNotIn("encoding", kwargs)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=b"width=1920\nheight=1080\nduration=60.5\n",
                stderr=b"\x81",
            )

        with (
            mock.patch("fdedup.similarity.shutil.which", return_value="ffprobe"),
            mock.patch("fdedup.similarity.subprocess.run", side_effect=fake_run),
        ):
            self.assertEqual(_probe_video_metadata("video.mp4"), (1920, 1080, 60.5))

    def test_video_match_requires_metadata_and_enough_close_frames(self) -> None:
        left = VideoFingerprint(
            width=1920,
            height=1080,
            duration=60.0,
            frame_hashes=(0b0000, 0b0001, 0b0011, 0b0111, 0b1111),
        )
        right = VideoFingerprint(
            width=1920,
            height=1080,
            duration=60.2,
            frame_hashes=(0b0000, 0b0000, 0b0010, 0b0110, 0b1110),
        )

        self.assertEqual(
            videos_match_strictly(
                left,
                right,
                total_threshold=10,
                frame_threshold=3,
                min_matching_frames=4,
            ),
            ("video", 92),
        )

    def test_video_match_rejects_different_resolution(self) -> None:
        left = VideoFingerprint(width=1920, height=1080, duration=60.0, frame_hashes=(1, 2, 3, 4, 5))
        right = VideoFingerprint(width=1280, height=720, duration=60.0, frame_hashes=(1, 2, 3, 4, 5))

        self.assertIsNone(
            videos_match_strictly(
                left,
                right,
                total_threshold=10,
                frame_threshold=3,
                min_matching_frames=4,
            )
        )

    def test_video_match_rejects_too_few_close_frames(self) -> None:
        left = VideoFingerprint(width=1920, height=1080, duration=60.0, frame_hashes=(0, 0, 0, 0, 0))
        right = VideoFingerprint(width=1920, height=1080, duration=60.0, frame_hashes=(0, 0, 15, 15, 15))

        self.assertIsNone(
            videos_match_strictly(
                left,
                right,
                total_threshold=10,
                frame_threshold=3,
                min_matching_frames=4,
            )
        )


def _require_pillow() -> object:
    try:
        from PIL import Image
    except ImportError as error:
        raise unittest.SkipTest("Pillow is not installed.") from error
    return Image


def _group_names(result: ScanResult) -> list[set[str]]:
    return [
        {record.name for record in group.records}
        for group in result.groups
    ]


if __name__ == "__main__":
    unittest.main()
