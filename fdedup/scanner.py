from __future__ import annotations

import ctypes
import hashlib
import os
import re
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TypeVar

from .debug import debug
from .models import DuplicateGroup, FileRecord, ProgressCallback, ScanOptions, ScanResult
from .similarity import (
    BKTree,
    IMAGE_EXTENSIONS,
    ImageFingerprint,
    VIDEO_EXTENSIONS,
    VideoFingerprint,
    VideoMetadata,
    ffmpeg_available,
    hamming_distance,
    image_fingerprint,
    image_similarity_available,
    images_match_strictly,
    probe_video_metadata,
    video_metadata_can_match,
    video_fingerprint,
    videos_match_strictly,
)


T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class WorkerPlan:
    hash_workers: int
    image_workers: int
    video_workers: int


class UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, item: int) -> int:
        parent = self.parent[item]
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, left: int, right: int) -> int:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return left_root

        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1
        return left_root


def scan(options: ScanOptions, progress: ProgressCallback | None = None) -> ScanResult:
    warnings: list[str] = []
    _report(progress, "Scan started.")
    records = _collect_files(options.root_a, options.root_b, warnings, progress)
    _report(progress, f"{len(records):,} files found. Checking exact hashes...")
    worker_plan = _resolve_worker_plan(options)
    _report(
        progress,
        "Parallel workers: "
        f"hash={worker_plan.hash_workers}, image={worker_plan.image_workers}, video={worker_plan.video_workers}",
    )

    _hash_same_size_files(records, warnings, progress, worker_plan.hash_workers)

    union_find = UnionFind(len(records))
    relation_reasons: dict[frozenset[int], list[tuple[str, int]]] = {}

    _union_exact_duplicates(records, union_find, relation_reasons)

    image_enabled = False
    if options.enable_image_similarity:
        if image_similarity_available():
            image_enabled = True
            _union_similar_images(records, options, union_find, relation_reasons, progress, worker_plan.image_workers)
        else:
            warnings.append("Image similarity is disabled because Pillow is not installed.")
            _report(progress, "Image similarity disabled: Pillow is not installed.")

    video_enabled = False
    if options.enable_video_similarity:
        if ffmpeg_available():
            video_enabled = True
            _union_similar_videos(records, options, union_find, relation_reasons, progress, worker_plan.video_workers)
        else:
            warnings.append("Video similarity is disabled because ffmpeg is not on PATH.")
            _report(progress, "Video similarity disabled: ffmpeg is not on PATH.")

    groups = _build_groups(records, union_find, relation_reasons)
    _report(progress, f"Scan finished. {len(groups):,} duplicate groups found.")
    return ScanResult(
        groups=groups,
        files_scanned=len(records),
        bytes_scanned=sum(record.size for record in records),
        warnings=warnings,
        image_similarity_enabled=image_enabled,
        video_similarity_enabled=video_enabled,
    )


def choose_best_record(records: list[FileRecord], markers: tuple[str, ...]) -> FileRecord:
    duplicate_avoidance_names = _duplicate_avoidance_name_record_ids(records)
    return max(
        records,
        key=lambda record: (
            record.has_preferred_marker(markers),
            record.id not in duplicate_avoidance_names,
            record.size,
            record.mtime,
            -len(record.path),
            record.path.casefold(),
        ),
    )


_DUPLICATE_AVOIDANCE_SUFFIXES = (
    re.compile(r"^(?P<base>.+?)\s*[\(\uff08]\s*\d{1,3}\s*[\)\uff09]$"),
    re.compile(r"^(?P<base>.+?)[\s._-]+\d{1,2}$"),
    re.compile(r"^(?P<base>.+?)[\s._-]+copy(?:[\s._-]*\d{1,3})?$", re.IGNORECASE),
)


def _duplicate_avoidance_name_record_ids(records: list[FileRecord]) -> set[int]:
    stems = {_normalized_stem(record.name) for record in records}
    duplicate_name_ids: set[int] = set()
    for record in records:
        stem = _normalized_stem(record.name)
        for pattern in _DUPLICATE_AVOIDANCE_SUFFIXES:
            match = pattern.match(stem)
            if match and match.group("base").strip().casefold() in stems:
                duplicate_name_ids.add(record.id)
                break
    return duplicate_name_ids


def _normalized_stem(name: str) -> str:
    return os.path.splitext(name)[0].strip().casefold()


def format_bytes(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value):,} {unit}"
            return f"{value:,.2f} {unit}"
        value /= 1024
    return f"{size:,} B"


def _resolve_worker_plan(options: ScanOptions) -> WorkerPlan:
    logical_cpus = os.cpu_count() or 1
    memory_gb = _physical_memory_gb()

    general_workers = max(1, min(8, logical_cpus // 2 or 1))
    video_workers = max(1, min(4, logical_cpus // 4 or 1))
    if memory_gb is not None and memory_gb < 8:
        general_workers = min(general_workers, 2)
        video_workers = 1

    return WorkerPlan(
        hash_workers=_sanitize_worker_override(options.hash_workers, general_workers),
        image_workers=_sanitize_worker_override(options.image_workers, general_workers),
        video_workers=_sanitize_worker_override(options.video_workers, video_workers),
    )


def _sanitize_worker_override(value: int | None, default: int) -> int:
    if value is None:
        return default
    return max(1, value)


def _physical_memory_gb() -> float | None:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullTotalPhys / (1024**3)
        except Exception:
            return None
        return None

    if hasattr(os, "sysconf"):
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return (pages * page_size) / (1024**3)
        except (OSError, ValueError):
            return None
    return None


def _report(progress: ProgressCallback | None, message: str) -> None:
    debug(message)
    if progress:
        progress(message)


def _collect_files(
    root_a: str,
    root_b: str,
    warnings: list[str],
    progress: ProgressCallback | None,
) -> list[FileRecord]:
    roots = [("A", root_a), ("B", root_b)]
    by_key: dict[str, FileRecord] = {}
    records: list[FileRecord] = []

    for root_label, root in roots:
        root = os.path.abspath(os.path.expandvars(os.path.expanduser(root)))
        if not os.path.isdir(root):
            warnings.append(f"Directory {root_label} does not exist or is not readable: {root}")
            _report(progress, f"Directory {root_label} is not readable: {root}")
            continue

        _report(progress, f"Scanning directory {root_label}: {root}")

        def on_error(error: OSError) -> None:
            warnings.append(f"Cannot read {getattr(error, 'filename', '')}: {error}")

        for current_dir, _dirnames, filenames in os.walk(root, onerror=on_error):
            for filename in filenames:
                path = os.path.join(current_dir, filename)
                try:
                    stat = os.stat(path)
                except OSError as error:
                    warnings.append(f"Cannot stat {path}: {error}")
                    continue
                if not os.path.isfile(path):
                    continue

                key = _path_key(path)
                existing = by_key.get(key)
                if existing:
                    existing.roots.add(root_label)
                    continue

                record = FileRecord(
                    id=len(records),
                    path=path,
                    roots={root_label},
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    extension=os.path.splitext(filename)[1].casefold(),
                )
                records.append(record)
                by_key[key] = record

    return records


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _hash_same_size_files(
    records: list[FileRecord],
    warnings: list[str],
    progress: ProgressCallback | None,
    workers: int,
) -> None:
    by_size: dict[int, list[FileRecord]] = defaultdict(list)
    for record in records:
        by_size[record.size].append(record)

    candidates = [record for same_size in by_size.values() if len(same_size) > 1 for record in same_size]
    total = len(candidates)
    if not candidates:
        return

    def worker(record: FileRecord) -> tuple[int, str | None, str | None]:
        try:
            return record.id, _sha256_file(record.path), None
        except OSError as error:
            return record.id, None, f"Cannot hash {record.path}: {error}"

    def report_progress(index: int) -> None:
        if index == 1 or index % 50 == 0 or index == total:
            _report(progress, f"Hashing exact-match candidates {index:,}/{total:,}")

    for record_id, exact_hash, warning in _parallel_map(
        candidates,
        worker,
        workers,
        on_completed=report_progress,
    ):
        if warning:
            warnings.append(warning)
        elif exact_hash:
            records[record_id].exact_hash = exact_hash


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parallel_map(
    items: list[T],
    worker: Callable[[T], R],
    workers: int,
    on_completed: Callable[[int], None] | None = None,
) -> list[R]:
    if not items:
        return []
    if workers <= 1 or len(items) == 1:
        results = []
        for index, item in enumerate(items, start=1):
            results.append(worker(item))
            if on_completed:
                on_completed(index)
        return results

    results: list[R | None] = [None] * len(items)
    completed = 0
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as executor:
        future_indexes = {executor.submit(worker, item): index for index, item in enumerate(items)}
        for future in as_completed(future_indexes):
            results[future_indexes[future]] = future.result()
            completed += 1
            if on_completed:
                on_completed(completed)
    return [result for result in results if result is not None]


def _union_exact_duplicates(
    records: list[FileRecord],
    union_find: UnionFind,
    relation_reasons: dict[frozenset[int], list[tuple[str, int]]],
) -> None:
    by_hash: dict[str, list[FileRecord]] = defaultdict(list)
    for record in records:
        if record.exact_hash:
            by_hash[record.exact_hash].append(record)

    for same_hash in by_hash.values():
        if len(same_hash) < 2:
            continue
        first = same_hash[0].id
        for record in same_hash[1:]:
            union_find.union(first, record.id)
            _add_relation_reason(relation_reasons, first, record.id, "exact", 100)


def _union_similar_images(
    records: list[FileRecord],
    options: ScanOptions,
    union_find: UnionFind,
    relation_reasons: dict[frozenset[int], list[tuple[str, int]]],
    progress: ProgressCallback | None,
    workers: int,
) -> None:
    candidates = [
        record
        for record in records
        if record.extension in IMAGE_EXTENSIONS and record.size >= options.min_similar_size
    ]
    _report(progress, f"Checking image similarity for {len(candidates):,} files...")

    tree = BKTree(hamming_distance)
    fingerprints: dict[int, ImageFingerprint] = {}
    total = len(candidates)

    def worker(record: FileRecord) -> tuple[FileRecord, ImageFingerprint | None]:
        return record, image_fingerprint(record.path)

    def report_progress(index: int) -> None:
        if index == 1 or index % 50 == 0 or index == total:
            _report(progress, f"Image fingerprints {index:,}/{total:,}")

    for record, fingerprint in _parallel_map(
        candidates,
        worker,
        workers,
        on_completed=report_progress,
    ):
        if fingerprint is None:
            continue
        for other_id, _distance in tree.query(fingerprint.dhash, options.image_threshold):
            other_fingerprint = fingerprints.get(other_id)
            if other_fingerprint is None:
                continue
            match = images_match_strictly(fingerprint, other_fingerprint, options.image_threshold)
            if match is None:
                continue
            reason, score = match
            union_find.union(record.id, other_id)
            _add_relation_reason(relation_reasons, record.id, other_id, reason, score)
        fingerprints[record.id] = fingerprint
        tree.add(fingerprint.dhash, record.id)


def _union_similar_videos(
    records: list[FileRecord],
    options: ScanOptions,
    union_find: UnionFind,
    relation_reasons: dict[frozenset[int], list[tuple[str, int]]],
    progress: ProgressCallback | None,
    workers: int,
) -> None:
    candidates = [
        record
        for record in records
        if record.extension in VIDEO_EXTENSIONS and record.size >= options.min_similar_size
    ]
    _report(progress, f"Checking video metadata for {len(candidates):,} files...")

    metadata_by_id: dict[int, VideoMetadata] = {}
    total = len(candidates)

    def metadata_worker(record: FileRecord) -> tuple[FileRecord, VideoMetadata | None]:
        return record, probe_video_metadata(record.path)

    def metadata_progress(index: int) -> None:
        if index == 1 or index % 50 == 0 or index == total:
            _report(progress, f"Video metadata {index:,}/{total:,}")

    metadata_workers = max(workers, min(8, os.cpu_count() or workers))
    for record, metadata in _parallel_map(
        candidates,
        metadata_worker,
        metadata_workers,
        on_completed=metadata_progress,
    ):
        if metadata is not None:
            metadata_by_id[record.id] = metadata

    candidates = _video_frame_candidates(candidates, metadata_by_id)
    if len(candidates) < 2:
        _report(progress, "Skipping video frame similarity: no metadata-compatible candidates.")
        return

    _report(progress, f"Checking preliminary video frames for {len(candidates):,} files...")

    preliminary_fingerprints: dict[int, VideoFingerprint] = {}
    total = len(candidates)

    def preliminary_worker(record: FileRecord) -> tuple[FileRecord, VideoFingerprint | None]:
        return record, video_fingerprint(
            record.path,
            sample_count=min(2, options.video_sample_count),
            metadata=metadata_by_id[record.id],
        )

    def preliminary_progress(index: int) -> None:
        if index == 1 or index % 50 == 0 or index == total:
            _report(progress, f"Preliminary video fingerprints {index:,}/{total:,}")

    for record, fingerprint in _parallel_map(
        candidates,
        preliminary_worker,
        workers,
        on_completed=preliminary_progress,
    ):
        if fingerprint is not None:
            preliminary_fingerprints[record.id] = fingerprint

    candidates = _video_full_frame_candidates(candidates, metadata_by_id, preliminary_fingerprints, options)
    if len(candidates) < 2:
        _report(progress, "Skipping full video frame similarity: no preliminary-compatible candidates.")
        return

    _report(progress, f"Checking full video frame similarity for {len(candidates):,} files...")

    fingerprints: dict[int, VideoFingerprint] = {}
    total = len(candidates)

    def fingerprint_worker(record: FileRecord) -> tuple[FileRecord, VideoFingerprint | None]:
        return record, video_fingerprint(
            record.path,
            sample_count=options.video_sample_count,
            metadata=metadata_by_id[record.id],
        )

    def fingerprint_progress(index: int) -> None:
        if index == 1 or index % 50 == 0 or index == total:
            _report(progress, f"Video fingerprints {index:,}/{total:,}")

    for record, fingerprint in _parallel_map(
        candidates,
        fingerprint_worker,
        workers,
        on_completed=fingerprint_progress,
    ):
        if fingerprint is None:
            continue
        for other_id, other_fingerprint in fingerprints.items():
            if not video_metadata_can_match(metadata_by_id[record.id], metadata_by_id[other_id]):
                continue
            match = videos_match_strictly(
                fingerprint,
                other_fingerprint,
                total_threshold=options.video_threshold,
                frame_threshold=options.video_frame_threshold,
                min_matching_frames=options.video_min_matching_frames,
            )
            if match is None:
                continue
            reason, score = match
            union_find.union(record.id, other_id)
            _add_relation_reason(relation_reasons, record.id, other_id, reason, score)
        fingerprints[record.id] = fingerprint


def _video_frame_candidates(
    records: list[FileRecord],
    metadata_by_id: dict[int, VideoMetadata],
) -> list[FileRecord]:
    by_resolution: dict[tuple[int, int], list[FileRecord]] = defaultdict(list)
    for record in records:
        metadata = metadata_by_id.get(record.id)
        if metadata is not None:
            width, height, _duration = metadata
            by_resolution[(width, height)].append(record)

    candidate_ids: set[int] = set()
    for same_resolution in by_resolution.values():
        for index, left in enumerate(same_resolution):
            left_metadata = metadata_by_id[left.id]
            for right in same_resolution[index + 1:]:
                if video_metadata_can_match(left_metadata, metadata_by_id[right.id]):
                    candidate_ids.add(left.id)
                    candidate_ids.add(right.id)

    return [record for record in records if record.id in candidate_ids]


def _video_full_frame_candidates(
    records: list[FileRecord],
    metadata_by_id: dict[int, VideoMetadata],
    preliminary_fingerprints: dict[int, VideoFingerprint],
    options: ScanOptions,
) -> list[FileRecord]:
    by_resolution: dict[tuple[int, int], list[FileRecord]] = defaultdict(list)
    for record in records:
        metadata = metadata_by_id.get(record.id)
        fingerprint = preliminary_fingerprints.get(record.id)
        if metadata is not None and fingerprint is not None:
            width, height, _duration = metadata
            by_resolution[(width, height)].append(record)

    candidate_ids: set[int] = set()
    for same_resolution in by_resolution.values():
        for index, left in enumerate(same_resolution):
            left_metadata = metadata_by_id[left.id]
            left_fingerprint = preliminary_fingerprints[left.id]
            for right in same_resolution[index + 1:]:
                if not video_metadata_can_match(left_metadata, metadata_by_id[right.id]):
                    continue
                if not _preliminary_video_match_possible(
                    left_fingerprint,
                    preliminary_fingerprints[right.id],
                    options,
                ):
                    continue
                candidate_ids.add(left.id)
                candidate_ids.add(right.id)

    return [record for record in records if record.id in candidate_ids]


def _preliminary_video_match_possible(
    left: VideoFingerprint,
    right: VideoFingerprint,
    options: ScanOptions,
) -> bool:
    distances = [
        hamming_distance(left_hash, right_hash)
        for left_hash, right_hash in zip(left.frame_hashes, right.frame_hashes)
    ]
    if sum(distances) > options.video_threshold:
        return False

    known_mismatches = sum(distance > options.video_frame_threshold for distance in distances)
    max_matching_frames = options.video_sample_count - known_mismatches
    return max_matching_frames >= options.video_min_matching_frames


def _add_relation_reason(
    relation_reasons: dict[frozenset[int], list[tuple[str, int]]],
    left: int,
    right: int,
    reason: str,
    score: int,
) -> None:
    key = frozenset((left, right))
    existing = relation_reasons.setdefault(key, [])
    if not any(name == reason for name, _score in existing):
        existing.append((reason, score))


def _build_groups(
    records: list[FileRecord],
    union_find: UnionFind,
    relation_reasons: dict[frozenset[int], list[tuple[str, int]]],
) -> list[DuplicateGroup]:
    by_root: dict[int, list[FileRecord]] = defaultdict(list)
    for record in records:
        by_root[union_find.find(record.id)].append(record)

    reason_by_component: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for pair, reasons in relation_reasons.items():
        pair_ids = tuple(pair)
        if not pair_ids:
            continue
        component = union_find.find(pair_ids[0])
        reason_by_component[component].extend(reasons)

    groups: list[DuplicateGroup] = []
    for component_records in by_root.values():
        if len(component_records) < 2:
            continue
        component = union_find.find(component_records[0].id)
        reasons = reason_by_component.get(component, [])
        reason_names = tuple(sorted({name for name, _score in reasons}))
        kind = "+".join(reason_names) if reason_names else "duplicate"
        score = min((score for _name, score in reasons), default=100)
        sorted_records = sorted(component_records, key=lambda record: record.path.casefold())
        groups.append(
            DuplicateGroup(
                id=len(groups) + 1,
                kind=kind,
                score=score,
                records=sorted_records,
                reasons=reason_names,
                group_hash=_duplicate_group_hash(kind, score, reason_names, sorted_records),
            )
        )

    groups.sort(key=lambda group: (-len(group.records), group.kind, group.records[0].path.casefold()))
    for index, group in enumerate(groups, start=1):
        group.id = index
    return groups


def _duplicate_group_hash(
    kind: str,
    score: int,
    reasons: tuple[str, ...],
    records: list[FileRecord],
) -> str:
    digest = hashlib.sha256()
    digest.update(f"kind\0{kind}\0score\0{score}\0".encode("utf-8"))
    for reason in reasons:
        digest.update(f"reason\0{reason}\0".encode("utf-8"))
    for record in records:
        digest.update(
            "\0".join(
                (
                    "record",
                    record.path.casefold(),
                    record.root_label,
                    str(record.size),
                    record.exact_hash or "",
                )
            ).encode("utf-8")
        )
        digest.update(b"\0")
    return digest.hexdigest()[:12]
