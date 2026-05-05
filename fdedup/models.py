# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


DEFAULT_PREFER_MARKERS = ("★",)


@dataclass(slots=True)
class ScanOptions:
    root_a: str
    root_b: str
    prefer_markers: tuple[str, ...] = DEFAULT_PREFER_MARKERS
    enable_image_similarity: bool = True
    enable_video_similarity: bool = True
    image_threshold: int = 4
    video_threshold: int = 10
    video_frame_threshold: int = 3
    video_sample_count: int = 5
    video_min_matching_frames: int = 4
    min_similar_size: int = 1024
    hash_workers: int | None = None
    image_workers: int | None = None
    video_workers: int | None = None


@dataclass(slots=True)
class FileRecord:
    id: int
    path: str
    roots: set[str]
    size: int
    mtime: float
    extension: str
    exact_hash: str | None = None

    @property
    def name(self) -> str:
        import os

        return os.path.basename(self.path)

    @property
    def root_label(self) -> str:
        return ",".join(sorted(self.roots))

    def has_preferred_marker(self, markers: tuple[str, ...]) -> bool:
        return any(marker and marker in self.path for marker in markers)


@dataclass(slots=True)
class DuplicateGroup:
    id: int
    kind: str
    score: int
    records: list[FileRecord]
    reasons: tuple[str, ...] = field(default_factory=tuple)
    group_hash: str = ""


@dataclass(slots=True)
class ScanResult:
    groups: list[DuplicateGroup]
    files_scanned: int
    bytes_scanned: int
    warnings: list[str] = field(default_factory=list)
    image_similarity_enabled: bool = False
    video_similarity_enabled: bool = False


ProgressCallback = Callable[[str], None]
