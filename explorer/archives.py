"""Import ZIP files into persistent snapshots without trusting archive paths."""

import hashlib
import json
import lzma
import os
import stat
import tempfile
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_BYTES = 500 * 1024 * 1024
MAX_MEMBER_BYTES = 50 * 1024 * 1024
MAX_ENTRIES = 10_000
_BLOCK_BYTES = 64 * 1024
_RESERVED = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {
    f"{prefix}{number}"
    for prefix in ("com", "lpt")
    for number in "123456789\u00b9\u00b2\u00b3"
}


@dataclass(frozen=True)
class ImportedArchive:
    root: Path
    sha256: str


def import_zip(source: Path, snapshots: Path) -> ImportedArchive:
    """Copy, validate, and publish a content-addressed snapshot of a local ZIP.

    Limits apply before exclusions. Reimporting identical bytes reuses the same
    snapshot. Failed validation or extraction leaves no published snapshot.
    """
    snapshots = snapshots.resolve()
    snapshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".import-", dir=snapshots) as temporary:
        staging = Path(temporary)
        archive = staging / "source.zip"
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as reader, archive.open("xb") as writer:
            while block := reader.read(_BLOCK_BYTES):
                size += len(block)
                if size > MAX_ARCHIVE_BYTES:
                    raise ValueError("ZIP exceeds the 100 MiB archive size limit.")
                digest.update(block)
                writer.write(block)
        sha256 = digest.hexdigest()
        destination = snapshots / sha256
        if destination.exists():
            return _existing(destination, sha256)

        root = staging / "repository"
        root.mkdir()
        try:
            with zipfile.ZipFile(archive) as zipped:
                entries = _validated_entries(zipped)
                total = 0
                for info, relative in entries:
                    if relative.parts[0] == "__MACOSX" or relative.name == ".DS_Store":
                        continue
                    target = root.joinpath(*relative.parts)
                    if not target.resolve().is_relative_to(root):
                        raise ValueError(
                            "ZIP entry resolves outside the extraction folder."
                        )
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    written = 0
                    with zipped.open(info) as reader, target.open("xb") as writer:
                        while block := reader.read(_BLOCK_BYTES):
                            written += len(block)
                            total += len(block)
                            if (
                                written > MAX_MEMBER_BYTES
                                or total > MAX_EXTRACTED_BYTES
                            ):
                                raise ValueError(
                                    "ZIP exceeds the extracted size limits."
                                )
                            writer.write(block)
        except (
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            NotImplementedError,
            RuntimeError,
            OSError,
            EOFError,
            zlib.error,
            lzma.LZMAError,
        ) as exc:
            raise ValueError(f"Cannot import ZIP: {exc}") from exc

        children = list(root.iterdir())
        if not children:
            raise ValueError("ZIP contains no repository files or directories.")
        if len(children) == 1 and children[0].is_dir():
            root = children[0]
        relative_root = root.relative_to(staging).as_posix()
        (staging / "snapshot.json").write_text(
            json.dumps({"root": relative_root}), encoding="utf-8"
        )
        archive.unlink()
        # A same-filesystem rename publishes the complete extraction. Another
        # process may have published the same archive while we were extracting.
        try:
            os.rename(staging, destination)
        except OSError:
            if not destination.exists():
                raise
        return _existing(destination, sha256)


def _existing(destination: Path, sha256: str) -> ImportedArchive:
    try:
        relative = json.loads(
            (destination / "snapshot.json").read_text(encoding="utf-8")
        )["root"]
        root = (destination / relative).resolve()
        if not root.is_relative_to(destination) or not root.is_dir():
            raise ValueError("Invalid snapshot root.")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Cannot reuse ZIP snapshot at {destination}: {exc}") from exc
    return ImportedArchive(root, sha256)


def _validated_entries(
    zipped: zipfile.ZipFile,
) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    entries = zipped.infolist()
    if len(entries) > MAX_ENTRIES:
        raise ValueError("ZIP exceeds the 10,000 entry limit.")
    seen = set()
    spellings = {}
    total = 0
    validated = []
    for info in entries:
        name = info.orig_filename
        parts = name.removesuffix("/").split("/")
        if (
            not name
            or "\\" in name
            or any(ord(c) < 32 for c in name)
            or PurePosixPath(name).is_absolute()
            or PureWindowsPath(name).drive
            or any(
                part in ("", ".", "..")
                or part.endswith((" ", "."))
                or any(c in part for c in ':<>"|?*')
                or part.split(".")[0].rstrip(" ").casefold() in _RESERVED
                for part in parts
            )
        ):
            raise ValueError(f"Unsafe ZIP entry path: {name!r}")
        key = unicodedata.normalize("NFC", "/".join(parts)).casefold()
        for end in range(1, len(parts) + 1):
            prefix = "/".join(parts[:end])
            canonical = unicodedata.normalize("NFC", prefix).casefold()
            if canonical in spellings and spellings[canonical] != prefix:
                raise ValueError(f"Conflicting ZIP entry spelling: {name!r}")
            spellings[canonical] = prefix
        if key in seen:
            raise ValueError(f"Duplicate ZIP entry path: {name!r}")
        seen.add(key)
        mode = stat.S_IFMT(info.external_attr >> 16)
        if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or (
            mode == stat.S_IFDIR and not info.is_dir()
        ):
            raise ValueError(f"ZIP links and special files are unsupported: {name!r}")
        if info.flag_bits & 1:
            raise ValueError("Encrypted ZIP archives are unsupported.")
        if info.file_size > MAX_MEMBER_BYTES:
            raise ValueError("ZIP entry exceeds the 50 MiB extracted file limit.")
        total += info.file_size
        if total > MAX_EXTRACTED_BYTES:
            raise ValueError("ZIP exceeds the 500 MiB extracted size limit.")
        validated.append((info, PurePosixPath(*parts)))
    return validated
