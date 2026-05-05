from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from .debug import debug


FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.USHORT),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def delete_files(paths: list[str], recycle: bool = True) -> tuple[list[str], list[tuple[str, str]]]:
    debug(f"Delete requested. files={len(paths)} recycle={recycle}")
    existing_paths = [path for path in paths if os.path.exists(path)]
    missing = [(path, "File does not exist.") for path in paths if not os.path.exists(path)]
    if not existing_paths:
        debug(f"Delete skipped. No existing files. missing={len(missing)}")
        return [], missing

    if recycle:
        if os.name != "nt":
            return [], missing + [
                (path, "Recycle Bin deletion is only available on Windows.")
                for path in existing_paths
            ]
        try:
            _move_to_recycle_bin(existing_paths)
            debug(f"Moved files to Recycle Bin. files={len(existing_paths)}")
            return existing_paths, missing
        except OSError as error:
            debug(f"Recycle Bin delete failed: {error}")
            return [], missing + [(path, str(error)) for path in existing_paths]

    deleted: list[str] = []
    errors = missing[:]
    for path in existing_paths:
        try:
            os.remove(path)
        except OSError as error:
            debug(f"Permanent delete failed. path={path} error={error}")
            errors.append((path, str(error)))
        else:
            debug(f"Permanent delete succeeded. path={path}")
            deleted.append(path)
    return deleted, errors


def _move_to_recycle_bin(paths: list[str]) -> None:
    encoded_paths = "\0".join(paths) + "\0\0"
    operation = SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    operation.pFrom = encoded_paths
    operation.pTo = None
    operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    operation.fAnyOperationsAborted = False
    operation.hNameMappings = None
    operation.lpszProgressTitle = None

    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise OSError(f"SHFileOperationW failed with code {result}.")
