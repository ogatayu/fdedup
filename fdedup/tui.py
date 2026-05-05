from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import sys
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from .models import DEFAULT_PREFER_MARKERS, DuplicateGroup, ScanOptions, ScanResult
from .scanner import format_bytes, scan
from .session import DuplicateSelectionSession, SessionRow
from .windows_delete import delete_files


RESET = "\x1b[0m"
DIM = "\x1b[2m"
INVERSE = "\x1b[7m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
MAX_SCAN_LOG_LINES = 500


@dataclass(frozen=True, slots=True)
class SortMode:
    label: str
    key: Callable[[SessionRow], object]


SORT_MODES = (
    SortMode("group", lambda row: (row.group.id, row.record.path.casefold())),
    SortMode("action", lambda row: (_action_rank(row.action), row.group.id, row.record.path.casefold())),
    SortMode("kind", lambda row: (row.group.kind, row.group.id, row.record.path.casefold())),
    SortMode("score", lambda row: (row.group.score, row.group.id, row.record.path.casefold())),
    SortMode("size", lambda row: (row.record.size, row.group.id, row.record.path.casefold())),
    SortMode("name", lambda row: (row.record.name.casefold(), row.group.id)),
    SortMode("root", lambda row: (row.record.root_label, row.group.id, row.record.path.casefold())),
    SortMode("mtime", lambda row: (row.record.mtime, row.group.id, row.record.path.casefold())),
    SortMode("path", lambda row: row.record.path.casefold()),
)


class Terminal:
    def __enter__(self) -> Terminal:
        _enable_virtual_terminal_processing()
        self._raw_mode = _RawMode()
        self._raw_mode.__enter__()
        self.write("\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.write(f"{RESET}\x1b[?25h\x1b[?1049l")
        self._raw_mode.__exit__(exc_type, exc, traceback)

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def clear(self) -> None:
        self.write("\x1b[2J\x1b[H")

    def size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size((120, 32))
        return size.columns, size.lines

    def read_key(self) -> str:
        if os.name == "nt":
            return _read_windows_key()
        return _read_posix_key()


class _RawMode:
    def __enter__(self) -> _RawMode:
        self._old_settings = None
        if os.name != "nt":
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._old_settings = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._old_settings is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)


class FdupupTui:
    def __init__(self, markers: tuple[str, ...], recycle: bool) -> None:
        self.markers = markers
        self.recycle = recycle
        self.result: ScanResult | None = None
        self.session: DuplicateSelectionSession | None = None
        self.cursor = 0
        self.scroll = 0
        self.sort_index = 0
        self.sort_descending = False
        self.status = "Ready."
        self._last_scan_render = 0.0
        self.scan_log: list[str] = []
        self._last_scan_log_message: str | None = None

    def run_scan(self, term: Terminal, options: ScanOptions) -> None:
        def progress(message: str) -> None:
            self._append_scan_log(message)
            now = time.monotonic()
            if now - self._last_scan_render >= 0.05:
                self.render_scanning(term, message)
                self._last_scan_render = now

        self.scan_log = []
        self._last_scan_log_message = None
        self._last_scan_render = 0.0
        self._append_scan_log("Scan started.")
        self.render_scanning(term, "Scan started.")
        self._last_scan_render = time.monotonic()
        result = scan(options, progress=progress)
        self.result = result
        self.session = DuplicateSelectionSession(result, self.markers)
        self.cursor = 0
        self.scroll = 0
        self.status = "Scan complete."

    def loop(self, term: Terminal) -> None:
        while True:
            self.render(term)
            key = term.read_key()
            if key in {"q", "Q", "ESC"}:
                return
            if key in {"UP", "k"}:
                self._move(-1)
            elif key in {"DOWN", "j"}:
                self._move(1)
            elif key == "PAGE_UP":
                self._move(-self._page_size(term))
            elif key == "PAGE_DOWN":
                self._move(self._page_size(term))
            elif key == "HOME":
                self.cursor = 0
            elif key == "END":
                self.cursor = max(0, len(self._sorted_rows()) - 1)
            elif key in {" ", "ENTER"}:
                self._toggle_current()
            elif key in {"r", "R"}:
                self._recommend_all()
            elif key in {"g", "G"}:
                self._recommend_current_group()
            elif key == "a":
                self._keep_root("A")
            elif key == "b":
                self._keep_root("B")
            elif key == "o":
                self._cycle_sort()
            elif key == "O":
                self.sort_descending = not self.sort_descending
                self.status = f"Sort order: {'descending' if self.sort_descending else 'ascending'}."
            elif key in {"d", "D"}:
                self._delete(term)
            elif key in {"?", "h", "H"}:
                self._show_help(term)

    def render_scanning(self, term: Terminal, message: str) -> None:
        width, height = term.size()
        log_height = _scan_log_height(height)
        main_height = max(0, height - log_height)
        lines = [
            "Fdedup TUI",
            "",
            "Scanning duplicate candidates...",
            f"Current: {message}",
            "",
            "Press Ctrl+C to cancel.",
        ]
        lines = [_fit(line, width) for line in lines[:main_height]]
        while len(lines) < main_height:
            lines.append("")
        lines.extend(self._scan_log_pane_lines(width, log_height))
        _draw_lines(term, lines, width, height)

    def _append_scan_log(self, message: str) -> None:
        message = " ".join(message.splitlines()).strip()
        if not message or message == self._last_scan_log_message:
            return

        self._last_scan_log_message = message
        self.scan_log.append(f"[{time.strftime('%H:%M:%S')}] {message}")
        overflow = len(self.scan_log) - MAX_SCAN_LOG_LINES
        if overflow > 0:
            del self.scan_log[:overflow]

    def _scan_log_pane_lines(self, width: int, height: int) -> list[str]:
        if height <= 0:
            return []
        separator = "-" * max(0, width)
        if height == 1:
            return [_fit(separator, width)]

        lines = [
            _fit(separator, width),
            _fit("Scan Log", width),
        ]
        visible_log_count = height - len(lines)
        log_lines = self.scan_log[-visible_log_count:] if visible_log_count > 0 else []
        if visible_log_count > 0 and not log_lines:
            log_lines = ["(no log messages yet)"]
        lines.extend(_fit(log_line, width) for log_line in log_lines)
        while len(lines) < height:
            lines.append("")
        return lines[:height]

    def render(self, term: Terminal) -> None:
        width, height = term.size()
        session = self._session()
        rows = self._sorted_rows()
        self._clamp_cursor(rows)

        header = self._header_lines(width)
        footer_height = 4
        list_top = len(header) + 1
        list_height = max(1, height - list_top - footer_height)
        self._clamp_scroll(list_height, len(rows))

        lines = header
        lines.append(_fit(self._table_header(width), width))

        if rows:
            for index in range(self.scroll, min(len(rows), self.scroll + list_height)):
                current = index == self.cursor
                lines.append(self._row_line(rows[index], width, current))
        else:
            lines.append(_fit("No duplicate groups found. Press q to quit.", width))

        while len(lines) < height - footer_height:
            lines.append("")

        current = rows[self.cursor] if rows else None
        lines.extend(self._footer_lines(session, current, width))
        _draw_lines(term, lines, width, height)

    def _header_lines(self, width: int) -> list[str]:
        result = self.result
        session = self._session()
        if result is None:
            return ["Fdedup TUI", ""]

        delete_count = len(session.delete_candidates())
        delete_bytes = sum(record.size for record in session.delete_candidates())
        sort = SORT_MODES[self.sort_index].label
        direction = "desc" if self.sort_descending else "asc"
        warning_text = f" warnings={len(result.warnings):,}" if result.warnings else ""
        summary = (
            f"Fdedup TUI | scanned={result.files_scanned:,} "
            f"groups={len(result.groups):,} candidates={session.duplicate_file_count():,} "
            f"bytes={format_bytes(session.duplicate_byte_count())}{warning_text}"
        )
        actions = (
            f"delete={delete_count:,} ({format_bytes(delete_bytes)}) | "
            f"sort={sort}/{direction} | recycle={'on' if self.recycle else 'off'}"
        )
        keys = "Keys: Up/Down/Pg move  Space toggle  r recommend  g group  a keep-A  b keep-B  o sort  O reverse  d delete  ? help  q quit"
        return [_fit(summary, width), _fit(actions, width), _fit(keys, width)]

    def _table_header(self, width: int) -> str:
        name_width = self._name_width(width)
        return (
            "  "
            + _fit("Action", 8)
            + " "
            + _fit("Grp", 5)
            + " "
            + _fit("GroupKey", 12)
            + " "
            + _fit("Kind", 12)
            + " "
            + _fit("Score", 5)
            + " "
            + _fit("Root", 5)
            + " "
            + _fit("Size", 11)
            + " "
            + _fit("Modified", 16)
            + " "
            + _fit("Name", name_width)
        )

    def _row_line(self, row: SessionRow, width: int, current: bool) -> str:
        record = row.record
        name_width = self._name_width(width)
        modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(record.mtime))
        prefix = "> " if current else "  "
        line = (
            prefix
            + _fit(row.action, 8)
            + " "
            + _fit(str(row.group.id), 5)
            + " "
            + _fit(row.group.group_hash or "-", 12)
            + " "
            + _fit(row.group.kind, 12)
            + " "
            + _fit(str(row.group.score), 5)
            + " "
            + _fit(record.root_label, 5)
            + " "
            + _fit(format_bytes(record.size), 11)
            + " "
            + _fit(modified, 16)
            + " "
            + _fit(record.name, name_width)
        )
        line = _fit(line, width)
        if current:
            return f"{INVERSE}{line}{RESET}"
        if row.action == "DELETE":
            return f"{RED}{line}{RESET}"
        if row.action == "KEEP":
            return f"{GREEN}{line}{RESET}"
        return f"{DIM}{line}{RESET}"

    def _footer_lines(
        self,
        session: DuplicateSelectionSession,
        current: SessionRow | None,
        width: int,
    ) -> list[str]:
        separator = "-" * width
        if current is None:
            detail = "Path: -"
            group = "Group: -"
        else:
            keep_count, delete_count, deleted_count = self._group_action_counts(session, current.group)
            detail = f"Path: {current.record.path}"
            group = (
                f"Group {current.group.id}: files={len(current.group.records)} "
                f"key={current.group.group_hash or '-'} kind={current.group.kind} score={current.group.score} "
                f"keep={keep_count} delete={delete_count} deleted={deleted_count}"
            )
        warning = ""
        if self.result and self.result.warnings:
            warning = f"Warnings: {len(self.result.warnings):,}. Press ? for details."
        return [
            _fit(separator, width),
            _fit(detail, width),
            _fit(group if not warning else f"{group} | {warning}", width),
            _fit(self.status, width),
        ]

    def _move(self, delta: int) -> None:
        rows = self._sorted_rows()
        if not rows:
            self.cursor = 0
            return
        self.cursor = max(0, min(len(rows) - 1, self.cursor + delta))

    def _page_size(self, term: Terminal) -> int:
        _width, height = term.size()
        return max(1, height - 8)

    def _toggle_current(self) -> None:
        row = self._current_row()
        if row is None:
            return
        record_id = row.record.id
        self._session().toggle(row.record.id)
        self._restore_cursor(record_id)
        self.status = f"{row.record.name}: {self._session().action_for(record_id)}."

    def _recommend_all(self) -> None:
        current_id = self._current_record_id()
        self._session().apply_recommended_to_all(self.markers)
        self._restore_cursor(current_id)
        self.status = "Applied recommended selection to all groups."

    def _recommend_current_group(self) -> None:
        row = self._current_row()
        if row is None:
            return
        record_id = row.record.id
        self._session().apply_recommended_to_group(row.group.id, self.markers)
        self._restore_cursor(record_id)
        self.status = f"Applied recommended selection to group {row.group.id}."

    def _keep_root(self, root_label: str) -> None:
        current_id = self._current_record_id()
        self._session().keep_root(root_label, self.markers)
        self._restore_cursor(current_id)
        self.status = f"Directory {root_label} is preferred for all groups."

    def _cycle_sort(self) -> None:
        current_id = self._current_record_id()
        self.sort_index = (self.sort_index + 1) % len(SORT_MODES)
        self._restore_cursor(current_id)
        self.status = f"Sort mode: {SORT_MODES[self.sort_index].label}."

    def _delete(self, term: Terminal) -> None:
        session = self._session()
        delete_records = session.delete_candidates()
        if not delete_records:
            self.status = "No files are marked DELETE."
            return

        unsafe_groups = session.unsafe_groups()
        if unsafe_groups:
            group_ids = ", ".join(str(group.id) for group in unsafe_groups[:8])
            self.status = f"Delete blocked. At least one file must remain in each group. Unsafe groups: {group_ids}"
            return

        paths = sorted({record.path for record in delete_records})
        total_size = sum(record.size for record in delete_records)
        if not self._confirm_delete(term, paths, total_size):
            self.status = "Delete canceled."
            return

        self.status = "Deleting files..."
        self.render(term)
        deleted, errors = delete_files(paths, recycle=self.recycle)
        current_id = self._current_record_id()
        session.mark_deleted_paths(set(deleted))
        self._restore_cursor(current_id)

        if errors:
            first_path, first_error = errors[0]
            self.status = (
                f"Deleted {len(deleted):,}; {len(errors):,} failed. "
                f"First error: {first_path}: {first_error}"
            )
        else:
            self.status = f"Deleted {len(deleted):,} files."

    def _confirm_delete(self, term: Terminal, paths: list[str], total_size: int) -> bool:
        width, height = term.size()
        destination = "Recycle Bin" if self.recycle else "permanent deletion"
        lines = [
            "Confirm Delete",
            "",
            f"Files: {len(paths):,}",
            f"Bytes: {format_bytes(total_size)}",
            f"Destination: {destination}",
            "",
            "Press y to execute, or any other key to cancel.",
            "",
        ]
        preview_count = max(0, height - len(lines) - 1)
        for path in paths[:preview_count]:
            lines.append(path)
        if len(paths) > preview_count:
            lines.append(f"... {len(paths) - preview_count:,} more")
        _draw_lines(term, lines, width, height)
        return term.read_key() in {"y", "Y"}

    def _show_help(self, term: Terminal) -> None:
        width, height = term.size()
        warning_lines = []
        if self.result and self.result.warnings:
            warning_lines = ["", "Warnings:"] + [f"- {warning}" for warning in self.result.warnings[:8]]
            if len(self.result.warnings) > 8:
                warning_lines.append(f"- ... {len(self.result.warnings) - 8:,} more")

        lines = [
            "Fdedup TUI Help",
            "",
            "Navigation: Up/Down, PgUp/PgDn, Home/End",
            "Selection: Space or Enter toggles the current file between KEEP and DELETE",
            "Bulk actions: r recommend all, g recommend current group, a keep Directory A, b keep Directory B",
            "Sorting: o cycles sort column, O reverses sort order",
            "Delete: d deletes files marked DELETE after confirmation",
            "Quit: q or Esc",
            "",
            "Press any key to return.",
            *warning_lines,
        ]
        _draw_lines(term, lines, width, height)
        term.read_key()

    def _sorted_rows(self) -> list[SessionRow]:
        session = self._session()
        rows = session.rows()
        mode = SORT_MODES[self.sort_index]
        rows.sort(key=mode.key, reverse=self.sort_descending)
        return rows

    def _current_row(self) -> SessionRow | None:
        rows = self._sorted_rows()
        self._clamp_cursor(rows)
        if not rows:
            return None
        return rows[self.cursor]

    def _current_record_id(self) -> int | None:
        row = self._current_row()
        return row.record.id if row else None

    def _restore_cursor(self, record_id: int | None) -> None:
        if record_id is None:
            return
        for index, row in enumerate(self._sorted_rows()):
            if row.record.id == record_id:
                self.cursor = index
                return

    def _clamp_cursor(self, rows: list[SessionRow]) -> None:
        if not rows:
            self.cursor = 0
            self.scroll = 0
            return
        self.cursor = max(0, min(len(rows) - 1, self.cursor))

    def _clamp_scroll(self, list_height: int, row_count: int) -> None:
        if row_count <= 0:
            self.scroll = 0
            return
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        if self.cursor >= self.scroll + list_height:
            self.scroll = self.cursor - list_height + 1
        self.scroll = max(0, min(self.scroll, max(0, row_count - list_height)))

    def _name_width(self, width: int) -> int:
        fixed_width = 2 + 8 + 5 + 12 + 12 + 5 + 5 + 11 + 16 + 9
        return max(12, width - fixed_width)

    def _session(self) -> DuplicateSelectionSession:
        if self.session is None:
            raise RuntimeError("Scan result has not been loaded.")
        return self.session

    def _group_action_counts(
        self,
        session: DuplicateSelectionSession,
        group: DuplicateGroup,
    ) -> tuple[int, int, int]:
        keep = delete = deleted = 0
        for record in group.records:
            action = session.action_for(record.id)
            if action == "KEEP":
                keep += 1
            elif action == "DELETE":
                delete += 1
            else:
                deleted += 1
        return keep, delete, deleted


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    root_a, root_b = _resolve_roots(args.root_a, args.root_b, _prompt)
    if not root_a or not root_b:
        parser.error("Directory A and Directory B are required.")

    markers = tuple(args.marker) if args.marker else DEFAULT_PREFER_MARKERS
    options = ScanOptions(
        root_a=root_a,
        root_b=root_b,
        prefer_markers=markers,
        enable_image_similarity=args.image_similarity,
        enable_video_similarity=args.video_similarity,
    )
    app = FdupupTui(markers=markers, recycle=args.recycle)

    try:
        with Terminal() as term:
            app.run_scan(term, options)
            app.loop(term)
    except KeyboardInterrupt:
        print("Canceled.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fdedup",
        description="Find duplicate files and review deletion candidates in a terminal UI.",
    )
    parser.add_argument("root_a", nargs="?", help="Directory A")
    parser.add_argument("root_b", nargs="?", help="Directory B")
    parser.add_argument(
        "--marker",
        action="append",
        help="Preferred full-path marker to keep. Can be specified multiple times. Default: star marker.",
    )
    parser.add_argument(
        "--no-image-similarity",
        action="store_false",
        dest="image_similarity",
        default=True,
        help="Disable strict image content and similarity checks.",
    )
    parser.add_argument(
        "--no-video-similarity",
        action="store_false",
        dest="video_similarity",
        default=True,
        help="Disable ffmpeg-based strict video sample checks.",
    )
    parser.add_argument(
        "--permanent",
        action="store_false",
        dest="recycle",
        default=True,
        help="Delete files permanently instead of moving them to the Recycle Bin.",
    )
    return parser


def _resolve_roots(
    root_a: str | None,
    root_b: str | None,
    prompt: Callable[[str], str],
) -> tuple[str, str]:
    if root_a and root_b is None:
        return root_a, root_a
    return root_a or prompt("Directory A"), root_b or prompt("Directory B")


def _prompt(label: str) -> str:
    try:
        return input(f"{label}: ").strip()
    except EOFError:
        return ""


def _action_rank(action: str) -> int:
    return {"DELETE": 0, "KEEP": 1, "DELETED": 2}.get(action, 3)


def _enable_virtual_terminal_processing() -> None:
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        return


def _read_windows_key() -> str:
    import msvcrt

    char = msvcrt.getwch()
    if char in {"\x00", "\xe0"}:
        code = msvcrt.getwch()
        return {
            "H": "UP",
            "P": "DOWN",
            "K": "LEFT",
            "M": "RIGHT",
            "I": "PAGE_UP",
            "Q": "PAGE_DOWN",
            "G": "HOME",
            "O": "END",
        }.get(code, code)
    if char == "\x03":
        raise KeyboardInterrupt
    if char == "\r":
        return "ENTER"
    if char == "\x1b":
        return "ESC"
    return char


def _read_posix_key() -> str:
    import select

    char = sys.stdin.read(1)
    if char == "\x03":
        raise KeyboardInterrupt
    if char in {"\r", "\n"}:
        return "ENTER"
    if char != "\x1b":
        return char

    if not select.select([sys.stdin], [], [], 0.01)[0]:
        return "ESC"
    next_char = sys.stdin.read(1)
    if next_char != "[":
        return "ESC"
    code = sys.stdin.read(1)
    if code in {"1", "4", "5", "6", "7", "8"}:
        suffix = sys.stdin.read(1)
        if suffix != "~":
            return "ESC"
        return {
            "1": "HOME",
            "4": "END",
            "5": "PAGE_UP",
            "6": "PAGE_DOWN",
            "7": "HOME",
            "8": "END",
        }.get(code, "ESC")
    return {
        "A": "UP",
        "B": "DOWN",
        "C": "RIGHT",
        "D": "LEFT",
        "H": "HOME",
        "F": "END",
    }.get(code, "ESC")


def _draw_lines(term: Terminal, lines: list[str], width: int, height: int) -> None:
    rendered = []
    for line in lines[:height]:
        rendered.append(_fit_ansi(line, width))
    while len(rendered) < height:
        rendered.append("")
    term.clear()
    term.write("\n".join(rendered[:height]))


def _scan_log_height(height: int) -> int:
    if height <= 0:
        return 0
    if height < 8:
        return max(1, height // 3)
    return min(10, max(5, height // 3), height - 4)


def _fit_ansi(text: str, width: int) -> str:
    if "\x1b[" not in text:
        return _fit(text, width)
    return text


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    current_width = _display_width(text)
    if current_width <= width:
        return text + (" " * (width - current_width))

    marker = "..." if width > 3 else ""
    target = width - len(marker)
    output = []
    used = 0
    for char in text:
        char_width = _char_width(char)
        if used + char_width > target:
            break
        output.append(char)
        used += char_width
    return "".join(output) + marker


def _display_width(text: str) -> int:
    return sum(_char_width(char) for char in text)


def _char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
