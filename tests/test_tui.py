from __future__ import annotations

import unittest

from fdedup.tui import FdupupTui, _resolve_roots


class FakeTerminal:
    def __init__(self, width: int = 80, height: int = 16) -> None:
        self.width = width
        self.height = height
        self.output = ""

    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def clear(self) -> None:
        self.output = ""

    def write(self, text: str) -> None:
        self.output += text


class FdupupTuiScanRenderTests(unittest.TestCase):
    def test_render_scanning_includes_bottom_log_pane(self) -> None:
        app = FdupupTui(markers=(), recycle=True)
        term = FakeTerminal()

        app._append_scan_log("Scanning directory A: C:\\left")
        app._append_scan_log("Hashing exact-match candidates 50/100")
        app.render_scanning(term, "Hashing exact-match candidates 50/100")

        lines = term.output.splitlines()
        self.assertEqual(len(lines), term.height)
        self.assertIn("Current: Hashing exact-match candidates 50/100", term.output)
        self.assertIn("Scan Log", term.output)
        self.assertIn("Scanning directory A: C:\\left", term.output)
        self.assertIn("Hashing exact-match candidates 50/100", term.output)

    def test_scan_log_deduplicates_consecutive_messages(self) -> None:
        app = FdupupTui(markers=(), recycle=True)

        app._append_scan_log("Scan started.")
        app._append_scan_log("Scan started.")
        app._append_scan_log("2 files found. Checking exact hashes...")

        self.assertEqual(len(app.scan_log), 2)


class RootArgumentTests(unittest.TestCase):
    def test_single_directory_argument_scans_it_against_itself(self) -> None:
        prompts: list[str] = []

        root_a, root_b = _resolve_roots("C:\\data", None, prompts.append)

        self.assertEqual(root_a, "C:\\data")
        self.assertEqual(root_b, "C:\\data")
        self.assertEqual(prompts, [])

    def test_missing_arguments_are_prompted(self) -> None:
        prompts: list[str] = []
        responses = iter(("C:\\left", "D:\\right"))

        root_a, root_b = _resolve_roots(None, None, lambda label: prompts.append(label) or next(responses))

        self.assertEqual(root_a, "C:\\left")
        self.assertEqual(root_b, "D:\\right")
        self.assertEqual(prompts, ["Directory A", "Directory B"])


if __name__ == "__main__":
    unittest.main()
