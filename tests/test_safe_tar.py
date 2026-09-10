from __future__ import annotations

import io
from pathlib import Path
import tarfile

import pytest

from app.services.error_reporting import ErrorCode
from app.services.safe_tar import (
    UnsafeTarArchiveError,
    safe_extract_tar,
    validate_safe_tar,
)


def _write_tar(path: Path, members: list[tarfile.TarInfo | tuple[str, bytes]]) -> None:
    with tarfile.open(path, "w") as archive:
        for member in members:
            if isinstance(member, tuple):
                name, payload = member
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(payload))
                continue
            archive.addfile(member)


def _directory(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    return info


def _special_member(
    name: str, member_type: bytes, *, linkname: str = ""
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = member_type
    info.linkname = linkname
    return info


def test_safe_extract_tar_extracts_regular_files_and_directories(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "bundle.tar"
    destination = tmp_path / "dest"
    _write_tar(
        archive_path,
        [
            _directory("mounts"),
            _directory("mounts/0"),
            ("mounts/0/app.txt", b"hello"),
            ("metadata.json", b"{}"),
        ],
    )

    with tarfile.open(archive_path, "r:*") as archive:
        extracted = safe_extract_tar(
            archive,
            destination,
            include=lambda member: member.name.startswith("mounts/"),
        )

    assert (destination / "mounts/0/app.txt").read_text(encoding="utf-8") == "hello"
    assert not (destination / "metadata.json").exists()
    assert destination / "mounts/0/app.txt" in extracted


@pytest.mark.parametrize(
    "member_name",
    [
        "/absolute.txt",
        "mounts/0/../../escape.txt",
        "../escape.txt",
        "",
    ],
)
def test_safe_extract_tar_rejects_hostile_member_paths(
    tmp_path: Path, member_name: str
) -> None:
    archive_path = tmp_path / "bundle.tar"
    destination = tmp_path / "dest"
    _write_tar(archive_path, [(member_name, b"owned")])

    with tarfile.open(archive_path, "r:*") as archive:
        with pytest.raises(UnsafeTarArchiveError):
            safe_extract_tar(archive, destination)

    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize(
    "member",
    [
        _special_member("mounts/0/link", tarfile.SYMTYPE, linkname="/etc/passwd"),
        _special_member(
            "mounts/0/hardlink", tarfile.LNKTYPE, linkname="mounts/0/app.txt"
        ),
        _special_member("mounts/0/device", tarfile.CHRTYPE),
        _special_member("mounts/0/block", tarfile.BLKTYPE),
        _special_member("mounts/0/fifo", tarfile.FIFOTYPE),
    ],
)
def test_safe_extract_tar_rejects_links_devices_and_fifos(
    tmp_path: Path, member: tarfile.TarInfo
) -> None:
    archive_path = tmp_path / "bundle.tar"
    destination = tmp_path / "dest"
    _write_tar(archive_path, [member])

    with tarfile.open(archive_path, "r:*") as archive:
        with pytest.raises(UnsafeTarArchiveError):
            safe_extract_tar(archive, destination)


def test_safe_extract_tar_rejects_existing_destination_symlink_escape(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "bundle.tar"
    destination = tmp_path / "dest"
    outside = tmp_path / "outside"
    destination.mkdir()
    outside.mkdir()
    (destination / "mounts").symlink_to(outside, target_is_directory=True)
    _write_tar(archive_path, [("mounts/owned.txt", b"owned")])

    with tarfile.open(archive_path, "r:*") as archive:
        with pytest.raises(UnsafeTarArchiveError):
            safe_extract_tar(archive, destination)

    assert not (outside / "owned.txt").exists()


def test_safe_extract_tar_allows_opted_in_relative_symlinks(tmp_path: Path) -> None:
    archive_path = tmp_path / "bundle.tar"
    destination = tmp_path / "dest"
    symlink = _special_member(
        "mounts/0/rootfs/bin", tarfile.SYMTYPE, linkname="usr/bin"
    )
    _write_tar(
        archive_path,
        [
            _directory("mounts"),
            _directory("mounts/0"),
            _directory("mounts/0/rootfs"),
            _directory("mounts/0/rootfs/usr"),
            _directory("mounts/0/rootfs/usr/bin"),
            symlink,
        ],
    )

    with tarfile.open(archive_path, "r:*") as archive:
        safe_extract_tar(archive, destination, allow_relative_symlinks=True)

    link_path = destination / "mounts/0/rootfs/bin"
    assert link_path.is_symlink()
    assert link_path.readlink() == Path("usr/bin")


@pytest.mark.parametrize("linkname", ["../../../outside", ""])
def test_safe_extract_tar_rejects_unsafe_opted_in_symlink_targets(
    tmp_path: Path, linkname: str
) -> None:
    archive_path = tmp_path / "bundle.tar"
    destination = tmp_path / "dest"
    symlink = _special_member("mounts/0/link", tarfile.SYMTYPE, linkname=linkname)
    _write_tar(archive_path, [symlink])

    with tarfile.open(archive_path, "r:*") as archive:
        with pytest.raises(UnsafeTarArchiveError):
            safe_extract_tar(archive, destination, allow_relative_symlinks=True)


def test_safe_extract_tar_rejects_non_rootfs_absolute_symlink_target(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "bundle.tar"
    destination = tmp_path / "dest"
    symlink = _special_member("mounts/0/link", tarfile.SYMTYPE, linkname="/etc/passwd")
    _write_tar(archive_path, [symlink])

    with tarfile.open(archive_path, "r:*") as archive:
        with pytest.raises(UnsafeTarArchiveError) as caught:
            safe_extract_tar(archive, destination, allow_relative_symlinks=True)

    assert caught.value.error_code is ErrorCode.RESTORE_TAR_SYMLINK_ESCAPES


def test_safe_extract_tar_allows_absolute_symlink_under_guest_root(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "bundle.tar"
    destination = tmp_path / "dest"
    symlink = _special_member(
        "mounts/1/.venv/bin/python3", tarfile.SYMTYPE, linkname="/usr/bin/python3"
    )
    _write_tar(
        archive_path,
        [
            _directory("mounts"),
            _directory("mounts/1"),
            _directory("mounts/1/.venv"),
            _directory("mounts/1/.venv/bin"),
            symlink,
        ],
    )

    with tarfile.open(archive_path, "r:*") as archive:
        safe_extract_tar(
            archive,
            destination,
            allow_relative_symlinks=True,
            guest_symlink_roots={"mounts/1"},
        )

    link_path = destination / "mounts/1/.venv/bin/python3"
    assert link_path.is_symlink()
    assert link_path.readlink() == Path("/usr/bin/python3")


def test_safe_extract_tar_allows_opted_in_absolute_rootfs_symlink_targets(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "bundle.tar"
    destination = tmp_path / "dest"
    symlink = _special_member(
        "mounts/0/rootfs/etc/alternatives/awk",
        tarfile.SYMTYPE,
        linkname="/usr/bin/mawk",
    )
    _write_tar(
        archive_path,
        [
            _directory("mounts"),
            _directory("mounts/0"),
            _directory("mounts/0/rootfs"),
            _directory("mounts/0/rootfs/etc"),
            _directory("mounts/0/rootfs/etc/alternatives"),
            _directory("mounts/0/rootfs/usr"),
            _directory("mounts/0/rootfs/usr/bin"),
            symlink,
        ],
    )

    with tarfile.open(archive_path, "r:*") as archive:
        safe_extract_tar(
            archive,
            destination,
            allow_relative_symlinks=True,
            guest_symlink_roots={"mounts/0/rootfs"},
        )

    link_path = destination / "mounts/0/rootfs/etc/alternatives/awk"
    assert link_path.is_symlink()
    assert link_path.readlink() == Path("/usr/bin/mawk")


def test_safe_extract_tar_allows_direct_rootfs_guest_symlinks(tmp_path: Path) -> None:
    archive_path = tmp_path / "rootfs.tar"
    destination = tmp_path / "rootfs"
    symlink = _special_member("bin", tarfile.SYMTYPE, linkname="/usr/bin")
    _write_tar(
        archive_path,
        [
            _directory("usr"),
            _directory("usr/bin"),
            symlink,
        ],
    )

    with tarfile.open(archive_path, "r:*") as archive:
        safe_extract_tar(
            archive,
            destination,
            allow_relative_symlinks=True,
            guest_symlink_roots={Path(".")},
        )

    link_path = destination / "bin"
    assert link_path.is_symlink()
    assert link_path.readlink() == Path("/usr/bin")


def test_safe_extract_tar_preserves_rootfs_symlink_graph_without_following_it(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "bundle.tar"
    destination = tmp_path / "dest"
    symlink = _special_member(
        "mounts/0/rootfs/etc/ssl/certs/a3418fda.0",
        tarfile.SYMTYPE,
        linkname="../../../../usr/share/ca-certificates/mozilla/GTS_Root_R4.crt",
    )
    _write_tar(
        archive_path,
        [
            _directory("mounts"),
            _directory("mounts/0"),
            _directory("mounts/0/rootfs"),
            _directory("mounts/0/rootfs/etc"),
            _directory("mounts/0/rootfs/etc/ssl"),
            _directory("mounts/0/rootfs/etc/ssl/certs"),
            symlink,
        ],
    )

    with tarfile.open(archive_path, "r:*") as archive:
        safe_extract_tar(archive, destination, allow_relative_symlinks=True)

    link_path = destination / "mounts/0/rootfs/etc/ssl/certs/a3418fda.0"
    assert link_path.is_symlink()
    assert link_path.readlink() == Path(
        "../../../../usr/share/ca-certificates/mozilla/GTS_Root_R4.crt"
    )


def test_safe_extract_tar_materializes_opted_in_hardlinks_as_files(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "bundle.tar"
    destination = tmp_path / "dest"
    hardlink = _special_member(
        "mounts/0/rootfs/usr/bin/perl5.38.2",
        tarfile.LNKTYPE,
        linkname="mounts/0/rootfs/usr/bin/perl",
    )
    _write_tar(
        archive_path,
        [
            _directory("mounts"),
            _directory("mounts/0"),
            _directory("mounts/0/rootfs"),
            _directory("mounts/0/rootfs/usr"),
            _directory("mounts/0/rootfs/usr/bin"),
            ("mounts/0/rootfs/usr/bin/perl", b"#!/usr/bin/perl\n"),
            hardlink,
        ],
    )

    with tarfile.open(archive_path, "r:*") as archive:
        safe_extract_tar(archive, destination, allow_relative_symlinks=True)

    hardlink_path = destination / "mounts/0/rootfs/usr/bin/perl5.38.2"
    assert hardlink_path.is_file()
    assert not hardlink_path.is_symlink()
    assert hardlink_path.read_bytes() == b"#!/usr/bin/perl\n"


def test_validate_safe_tar_rejects_restore_unsafe_hardlink(tmp_path: Path) -> None:
    archive_path = tmp_path / "bundle.tar"
    hardlink = _special_member(
        "mounts/0/rootfs/usr/bin/perl5.38.2",
        tarfile.LNKTYPE,
        linkname="/usr/bin/perl",
    )
    _write_tar(
        archive_path,
        [
            _directory("mounts"),
            _directory("mounts/0"),
            _directory("mounts/0/rootfs"),
            hardlink,
        ],
    )

    with tarfile.open(archive_path, "r:*") as archive:
        with pytest.raises(UnsafeTarArchiveError, match="unsafe tar hardlink target"):
            validate_safe_tar(archive, allow_relative_symlinks=True)


def test_safe_extract_tar_rejects_symlink_destination(tmp_path: Path) -> None:
    archive_path = tmp_path / "bundle.tar"
    destination = tmp_path / "dest"
    outside = tmp_path / "outside"
    outside.mkdir()
    destination.symlink_to(outside, target_is_directory=True)
    _write_tar(archive_path, [("owned.txt", b"owned")])

    with tarfile.open(archive_path, "r:*") as archive:
        with pytest.raises(UnsafeTarArchiveError):
            safe_extract_tar(archive, destination)

    assert not (outside / "owned.txt").exists()
