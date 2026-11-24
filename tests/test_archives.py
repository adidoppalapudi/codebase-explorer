import asyncio
import json
import stat
import struct
import sys
import zipfile
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from explorer import archives
from explorer.cli import main
from explorer.repository import Repositories


def write_zip(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, text in files:
            zipped.writestr(name, text)
    return path


@pytest.mark.parametrize("wrapper", ["", "shop-main/"])
def test_zip_navigation_persistence_and_exclusions(tmp_path, wrapper):
    archive = write_zip(
        tmp_path / "shop.zip",
        [
            (wrapper + "README.md", "# Shop\nCheckout service\n"),
            (wrapper + "app.py", "def checkout():\n    return 1\n"),
            (wrapper + "src/client.ts", "export function pay() { return 1; }\n"),
            (wrapper + ".gitignore", "ignored.txt\n"),
            (wrapper + "ignored.txt", "hiddenneedle"),
            (wrapper + "omit.txt", "excludedneedle"),
            ("__MACOSX/._shop", "metadata"),
            (".DS_Store", "metadata"),
        ],
    )
    cache = tmp_path / "cache"
    repos = Repositories(cache)
    opened = repos.open(str(archive), ["omit.txt"])
    rid = opened["repo_id"]
    assert opened["source_type"] == "zip"
    assert opened["archive_sha256"]
    assert repos.search(rid, "checkout")[0]["path"] == "app.py"
    assert repos.outline(rid, "app.py")["symbols"][0]["qualified_name"] == "checkout"
    assert not repos.search(rid, "hiddenneedle")
    assert not repos.search(rid, "excludedneedle")
    assert not repos.files(rid, path="__MACOSX")["files"]
    assert repos.open(str(archive), ["omit.txt"])["repo_id"] == rid
    copied = tmp_path / "renamed.ZIP"
    copied.write_bytes(archive.read_bytes())
    assert repos.open(str(copied), ["omit.txt"])["repo_id"] == rid
    archive.unlink()
    copied.unlink()
    assert Repositories(cache).read(rid, "app.py")["citation"] == "app.py:1-2"


def test_changed_archive_is_a_separate_snapshot(tmp_path):
    archive = tmp_path / "shop.zip"
    repos = Repositories(tmp_path / "cache")
    write_zip(archive, [("app.py", "def old(): pass\n")])
    old = repos.open(str(archive))["repo_id"]
    write_zip(archive, [("app.py", "def new(): pass\n")])
    new = repos.open(str(archive))["repo_id"]
    assert old != new
    assert repos.read(old, "app.py")["lines"] == ["def old(): pass"]
    assert repos.read(new, "app.py")["lines"] == ["def new(): pass"]


@pytest.mark.parametrize(
    "name",
    [
        "../outside.py",
        "repo/../../outside.py",
        "/outside.py",
        "C:/outside.py",
        "C:outside.py",
        "\\\\server\\share\\outside.py",
        "repo\\outside.py",
        "repo/./app.py",
        "repo//app.py",
        "CON.txt",
        "repo/NUL",
        "a.py:stream",
        "repo/trailing.",
        "repo/trailing ",
        "bad?.py",
    ],
)
def test_unsafe_archive_paths_are_rejected(tmp_path, name):
    archive = write_zip(tmp_path / "bad.zip", [(name, "bad")])
    # On Windows ZipInfo normalizes backslashes when writing, so preserve the
    # original malicious spelling in both headers to exercise the importer.
    if "\\" in name:
        archive.write_bytes(
            archive.read_bytes().replace(
                name.replace("\\", "/").encode(), name.encode()
            )
        )
    repos = Repositories(tmp_path / "cache")
    with pytest.raises(ValueError, match="Unsafe ZIP"):
        repos.open(str(archive))
    assert not (tmp_path / "outside.py").exists()
    assert not list((tmp_path / "cache" / "archives").iterdir())
    assert not list((tmp_path / "cache").glob("*.db"))


@pytest.mark.parametrize(
    "names",
    [
        ["app.py", "APP.py"],
        ["Repo/a.py", "repo/b.py"],
        ["x", "x/y.py"],
        ["x/y.py", "x"],
        ["\u00e9.py", "e\u0301.py"],
    ],
)
def test_archive_path_collisions_leave_no_snapshot(tmp_path, names):
    archive = write_zip(tmp_path / "bad.zip", [(name, "bad") for name in names])
    with pytest.raises(ValueError):
        Repositories(tmp_path / "cache").open(str(archive))
    assert not list((tmp_path / "cache" / "archives").iterdir())


@pytest.mark.parametrize(
    "kind", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFSOCK]
)
def test_links_and_special_files_are_rejected(tmp_path, kind):
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (kind | 0o777) << 16
    archive = write_zip(tmp_path / "bad.zip", [(info, "../outside.py")])
    with pytest.raises(ValueError, match="links and special"):
        Repositories(tmp_path / "cache").open(str(archive))


@pytest.mark.parametrize(
    "limit,value",
    [
        ("MAX_ARCHIVE_BYTES", 10),
        ("MAX_MEMBER_BYTES", 4),
        ("MAX_EXTRACTED_BYTES", 8),
        ("MAX_ENTRIES", 1),
    ],
)
def test_archive_limits_are_enforced(tmp_path, monkeypatch, limit, value):
    archive = write_zip(
        tmp_path / "large.zip", [("a.txt", "12345"), ("b.txt", "12345")]
    )
    monkeypatch.setattr(archives, limit, value)
    with pytest.raises(ValueError, match="limit"):
        Repositories(tmp_path / "cache").open(str(archive))
    assert not list((tmp_path / "cache" / "archives").iterdir())


@pytest.mark.parametrize("fault", ["invalid", "empty", "encrypted", "nul", "crc"])
def test_bad_archives_fail_without_partial_imports(tmp_path, fault):
    archive = write_zip(tmp_path / "bad.zip", [("badX.py", "def hello(): pass")])
    content = bytearray(archive.read_bytes())
    if fault == "invalid":
        archive.write_bytes(b"this is not a zip")
    elif fault == "empty":
        write_zip(archive, [])
    elif fault == "nul":
        archive.write_bytes(bytes(content).replace(b"badX.py", b"bad\x00.py"))
    elif fault == "encrypted":
        struct.pack_into("<H", content, 6, 1)
        struct.pack_into("<H", content, content.index(b"PK\x01\x02") + 8, 1)
        archive.write_bytes(content)
    elif fault == "crc":
        # Corrupt both recorded CRCs so extraction must detect the mismatch.
        struct.pack_into("<I", content, 14, 0)
        struct.pack_into("<I", content, content.index(b"PK\x01\x02") + 16, 0)
        archive.write_bytes(content)
    with pytest.raises(ValueError):
        Repositories(tmp_path / "cache").open(str(archive))
    assert not list((tmp_path / "cache" / "archives").iterdir())


def test_zip_cli_open_and_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    archive = write_zip(
        tmp_path / "shop.zip", [("shop/app.py", "def checkout(): pass\n")]
    )
    assert main(["--json", "open", str(archive)]) == 0
    assert json.loads(capsys.readouterr().out)["source_type"] == "zip"
    assert main(["--json", "outline", str(archive), "app.py"]) == 0
    assert (
        json.loads(capsys.readouterr().out)["symbols"][0]["qualified_name"]
        == "checkout"
    )
    archive.write_bytes(b"invalid")
    assert main(["open", str(archive)]) == 1
    assert "Cannot import ZIP" in capsys.readouterr().err


def test_zip_stdio_mcp_workflow(tmp_path):
    archive = write_zip(
        tmp_path / "shop.zip", [("shop/app.py", "def checkout():\n    return 1\n")]
    )

    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "explorer.mcp_server",
                "--cache-dir",
                str(tmp_path / "cache"),
            ],
        )
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()

            async def call(name, args):
                result = await session.call_tool(name, args)
                assert not result.is_error, result
                return result.structured_content

            opened = await call("open_repository", {"folder": str(archive)})
            rid = opened["repo_id"]
            assert opened["source_type"] == "zip"
            assert (await call("repo_overview", {"repo_id": rid}))["files"] == 1
            assert (await call("list_files", {"repo_id": rid}))["files"][0][
                "path"
            ] == "app.py"
            assert (await call("search_code", {"repo_id": rid, "query": "checkout"}))[
                "matches"
            ]
            assert (await call("file_outline", {"repo_id": rid, "path": "app.py"}))[
                "symbols"
            ]
            archive.unlink()
            assert (await call("read_source", {"repo_id": rid, "path": "app.py"}))[
                "citation"
            ] == "app.py:1-2"
            bad = write_zip(tmp_path / "bad.zip", [("../escape.py", "bad")])
            result = await session.call_tool("open_repository", {"folder": str(bad)})
            assert result.is_error
            assert (await call("read_source", {"repo_id": rid, "path": "app.py"}))[
                "lines"
            ]

    asyncio.run(exercise())
