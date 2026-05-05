from __future__ import annotations

import unittest

from fdedup.models import DuplicateGroup, FileRecord, ScanResult
from fdedup.session import DuplicateSelectionSession


def _record(record_id: int, path: str, roots: set[str], size: int = 1, mtime: float = 1) -> FileRecord:
    return FileRecord(
        id=record_id,
        path=path,
        roots=roots,
        size=size,
        mtime=mtime,
        extension=".txt",
    )


class DuplicateSelectionSessionTests(unittest.TestCase):
    def test_recommended_selection_keeps_preferred_marker(self) -> None:
        result = _result(
            [
                _record(1, r"C:\root\plain.txt", {"A"}, size=10, mtime=10),
                _record(2, "C:\\root\\starred-\u2605.txt", {"B"}, size=1, mtime=1),
            ]
        )

        session = DuplicateSelectionSession(result, ("\u2605",))

        self.assertEqual(session.action_for(1), "DELETE")
        self.assertEqual(session.action_for(2), "KEEP")

    def test_keep_root_prefers_requested_root(self) -> None:
        result = _result(
            [
                _record(1, r"C:\a\one.txt", {"A"}),
                _record(2, r"C:\b\one.txt", {"B"}, size=100),
            ]
        )
        session = DuplicateSelectionSession(result, ())

        session.keep_root("A", ())

        self.assertEqual(session.action_for(1), "KEEP")
        self.assertEqual(session.action_for(2), "DELETE")

    def test_recommended_selection_deletes_parenthesized_copy_name(self) -> None:
        result = _result(
            [
                _record(1, r"C:\root\photo.jpg", {"A"}, size=1, mtime=1),
                _record(2, r"C:\root\photo (1).jpg", {"A"}, size=100, mtime=100),
            ]
        )

        session = DuplicateSelectionSession(result, ())

        self.assertEqual(session.action_for(1), "KEEP")
        self.assertEqual(session.action_for(2), "DELETE")

    def test_recommended_selection_deletes_numbered_dash_copy_name(self) -> None:
        result = _result(
            [
                _record(1, r"C:\root\photo.jpg", {"A"}, size=1, mtime=1),
                _record(2, r"C:\root\photo-02.jpg", {"A"}, size=100, mtime=100),
            ]
        )

        session = DuplicateSelectionSession(result, ())

        self.assertEqual(session.action_for(1), "KEEP")
        self.assertEqual(session.action_for(2), "DELETE")

    def test_numbered_suffix_is_not_penalized_without_base_name(self) -> None:
        result = _result(
            [
                _record(1, r"C:\root\episode-02.txt", {"A"}, size=10, mtime=10),
                _record(2, r"C:\root\other.txt", {"A"}, size=1, mtime=1),
            ]
        )

        session = DuplicateSelectionSession(result, ())

        self.assertEqual(session.action_for(1), "KEEP")
        self.assertEqual(session.action_for(2), "DELETE")

    def test_unsafe_groups_detects_all_delete_group(self) -> None:
        result = _result(
            [
                _record(1, r"C:\a\one.txt", {"A"}),
                _record(2, r"C:\b\one.txt", {"B"}),
            ]
        )
        session = DuplicateSelectionSession(result, ())

        session.set_action(1, "DELETE")
        session.set_action(2, "DELETE")

        self.assertEqual([group.id for group in session.unsafe_groups()], [1])

    def test_mark_deleted_paths_overrides_action(self) -> None:
        result = _result(
            [
                _record(1, r"C:\a\one.txt", {"A"}),
                _record(2, r"C:\b\one.txt", {"B"}),
            ]
        )
        session = DuplicateSelectionSession(result, ())

        session.mark_deleted_paths({r"C:\b\one.txt"})

        self.assertEqual(session.action_for(2), "DELETED")


def _result(records: list[FileRecord]) -> ScanResult:
    group = DuplicateGroup(id=1, kind="exact", score=100, records=records, reasons=("exact",))
    return ScanResult(groups=[group], files_scanned=len(records), bytes_scanned=sum(record.size for record in records))


if __name__ == "__main__":
    unittest.main()
