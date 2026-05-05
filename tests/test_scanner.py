from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fdedup.models import FileRecord, ScanOptions, ScanResult
from fdedup.scanner import choose_best_record, scan, _video_frame_candidates


class ScannerTests(unittest.TestCase):
    def test_same_directory_exact_duplicates_are_grouped_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.txt").write_text("same", encoding="utf-8")
            (root / "b.txt").write_text("same", encoding="utf-8")
            (root / "c.txt").write_text("different", encoding="utf-8")

            result = scan(
                ScanOptions(
                    root_a=str(root),
                    root_b=str(root),
                    enable_image_similarity=False,
                    enable_video_similarity=False,
                )
            )

            self.assertEqual(result.files_scanned, 3)
            self.assertEqual(len(result.groups), 1)
            self.assertEqual({record.name for record in result.groups[0].records}, {"a.txt", "b.txt"})
            self.assertRegex(result.groups[0].group_hash, r"^[0-9a-f]{12}$")

    def test_preferred_marker_wins_before_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            starred = FileRecord(
                id=1,
                path=str(root / "\u2605small.txt"),
                roots={"A"},
                size=1,
                mtime=1,
                extension=".txt",
            )
            larger = FileRecord(
                id=2,
                path=str(root / "large.txt"),
                roots={"B"},
                size=10,
                mtime=10,
                extension=".txt",
            )
            best = choose_best_record([larger, starred], ("\u2605",))

            self.assertEqual(best.name, "\u2605small.txt")

    def test_preferred_marker_in_parent_path_wins_before_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            preferred_dir = root / "\u2605preferred"
            plain_dir = root / "plain"
            preferred = FileRecord(
                id=1,
                path=str(preferred_dir / "small.txt"),
                roots={"A"},
                size=1,
                mtime=1,
                extension=".txt",
            )
            larger = FileRecord(
                id=2,
                path=str(plain_dir / "large.txt"),
                roots={"B"},
                size=10,
                mtime=10,
                extension=".txt",
            )

            best = choose_best_record([larger, preferred], ("\u2605",))

            self.assertEqual(best.path, str(preferred_dir / "small.txt"))

    def test_parallel_hashing_groups_exact_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index in range(8):
                (root / f"same-{index}.bin").write_bytes(b"x" * 4096)
            (root / "different.bin").write_bytes(b"y" * 4096)

            result = scan(
                ScanOptions(
                    root_a=str(root),
                    root_b=str(root),
                    enable_image_similarity=False,
                    enable_video_similarity=False,
                    hash_workers=4,
                )
            )

            self.assertEqual(len(result.groups), 1)
            self.assertEqual(len(result.groups[0].records), 8)
            self.assertRegex(result.groups[0].group_hash, r"^[0-9a-f]{12}$")

    def test_parallel_worker_counts_do_not_change_scan_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index in range(4):
                (root / f"same-{index}.txt").write_text("same", encoding="utf-8")
            (root / "other.txt").write_text("other", encoding="utf-8")

            serial = scan(
                ScanOptions(
                    root_a=str(root),
                    root_b=str(root),
                    enable_image_similarity=False,
                    enable_video_similarity=False,
                    hash_workers=1,
                )
            )
            parallel = scan(
                ScanOptions(
                    root_a=str(root),
                    root_b=str(root),
                    enable_image_similarity=False,
                    enable_video_similarity=False,
                    hash_workers=4,
                )
            )

            self.assertEqual(_group_names(serial), _group_names(parallel))
            self.assertEqual(_group_hashes(serial), _group_hashes(parallel))

    def test_different_duplicate_groups_have_different_group_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.txt").write_text("same-a", encoding="utf-8")
            (root / "b.txt").write_text("same-a", encoding="utf-8")
            (root / "c.txt").write_text("same-c", encoding="utf-8")
            (root / "d.txt").write_text("same-c", encoding="utf-8")

            result = scan(
                ScanOptions(
                    root_a=str(root),
                    root_b=str(root),
                    enable_image_similarity=False,
                    enable_video_similarity=False,
                )
            )

            self.assertEqual(len(result.groups), 2)
            self.assertEqual(len({group.group_hash for group in result.groups}), 2)

    def test_video_frame_candidates_only_include_metadata_compatible_records(self) -> None:
        records = [
            FileRecord(1, r"C:\root\a.mp4", {"A"}, 2000, 1, ".mp4"),
            FileRecord(2, r"C:\root\b.mp4", {"A"}, 2000, 1, ".mp4"),
            FileRecord(3, r"C:\root\c.mp4", {"A"}, 2000, 1, ".mp4"),
            FileRecord(4, r"C:\root\d.mp4", {"A"}, 2000, 1, ".mp4"),
        ]
        metadata_by_id = {
            1: (1920, 1080, 60.0),
            2: (1920, 1080, 60.3),
            3: (1280, 720, 60.1),
            4: (1920, 1080, 75.0),
        }

        candidates = _video_frame_candidates(records, metadata_by_id)

        self.assertEqual({record.id for record in candidates}, {1, 2})


def _group_names(result: ScanResult) -> list[set[str]]:
    return [
        {record.name for record in group.records}
        for group in result.groups
    ]


def _group_hashes(result: ScanResult) -> list[str]:
    return [group.group_hash for group in result.groups]


if __name__ == "__main__":
    unittest.main()
