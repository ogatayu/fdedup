from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import DuplicateGroup, FileRecord, ScanResult
from .scanner import choose_best_record


Action = Literal["KEEP", "DELETE", "DELETED"]


@dataclass(frozen=True, slots=True)
class SessionRow:
    group: DuplicateGroup
    record: FileRecord
    action: Action


class DuplicateSelectionSession:
    """UI-independent action state for a scan result."""

    def __init__(self, result: ScanResult, markers: tuple[str, ...]) -> None:
        self.result = result
        self.groups = result.groups
        self.record_actions: dict[int, Action] = {}
        self.record_groups: dict[int, DuplicateGroup] = {}
        self.records: dict[int, FileRecord] = {}
        self.deleted_record_ids: set[int] = set()

        for group in self.groups:
            for record in group.records:
                self.record_groups[record.id] = group
                self.records[record.id] = record

        self.apply_recommended_to_all(markers)

    def rows(self) -> list[SessionRow]:
        rows: list[SessionRow] = []
        for group in self.groups:
            for record in group.records:
                rows.append(SessionRow(group=group, record=record, action=self.action_for(record.id)))
        return rows

    def action_for(self, record_id: int) -> Action:
        if record_id in self.deleted_record_ids:
            return "DELETED"
        return self.record_actions.get(record_id, "KEEP")

    def set_action(self, record_id: int, action: Action) -> None:
        if record_id in self.deleted_record_ids:
            return
        if action == "DELETED":
            self.deleted_record_ids.add(record_id)
        self.record_actions[record_id] = action

    def toggle(self, record_id: int) -> None:
        if record_id in self.deleted_record_ids:
            return
        current = self.record_actions.get(record_id, "KEEP")
        self.record_actions[record_id] = "DELETE" if current == "KEEP" else "KEEP"

    def apply_recommended_to_all(self, markers: tuple[str, ...]) -> None:
        self.record_actions.clear()
        for group in self.groups:
            self.apply_recommended_to_group(group.id, markers)

    def apply_recommended_to_group(self, group_id: int, markers: tuple[str, ...]) -> None:
        group = self._group_by_id(group_id)
        if group is None or not group.records:
            return
        best = choose_best_record(group.records, markers)
        for record in group.records:
            if record.id not in self.deleted_record_ids:
                self.record_actions[record.id] = "KEEP" if record.id == best.id else "DELETE"

    def keep_root(self, root_label: str, markers: tuple[str, ...]) -> None:
        for group in self.groups:
            root_records = [record for record in group.records if root_label in record.roots]
            keep = choose_best_record(root_records or group.records, markers)
            for record in group.records:
                if record.id not in self.deleted_record_ids:
                    self.record_actions[record.id] = "KEEP" if record.id == keep.id else "DELETE"

    def delete_candidates(self) -> list[FileRecord]:
        return [
            record
            for group in self.groups
            for record in group.records
            if self.action_for(record.id) == "DELETE"
        ]

    def unsafe_groups(self) -> list[DuplicateGroup]:
        unsafe: list[DuplicateGroup] = []
        for group in self.groups:
            if group.records and all(self.action_for(record.id) in {"DELETE", "DELETED"} for record in group.records):
                unsafe.append(group)
        return unsafe

    def mark_deleted_paths(self, paths: set[str]) -> None:
        for record in self.records.values():
            if record.path in paths:
                self.deleted_record_ids.add(record.id)
                self.record_actions[record.id] = "DELETED"

    def duplicate_file_count(self) -> int:
        return sum(len(group.records) for group in self.groups)

    def duplicate_byte_count(self) -> int:
        return sum(record.size for group in self.groups for record in group.records)

    def _group_by_id(self, group_id: int) -> DuplicateGroup | None:
        for group in self.groups:
            if group.id == group_id:
                return group
        return None
