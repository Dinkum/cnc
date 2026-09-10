import base64
import io
import json
import os
import struct
import sys
import tarfile

import pytest

from app.services.guest_metadata import (
    GuestArchiveView,
    IdMapping,
    XATTR_HEADER,
    guest_id,
    normalize_xattr,
    parse_id_map,
    guest_tar_filter,
)
from app.services.safe_tar import (
    UnsafeTarArchiveError,
    safe_extract_tar,
    validate_safe_tar,
)


def test_active_and_unmounted_views_produce_identical_guest_ids():
    mappings = parse_id_map("0 268435456 65536\n")
    for ident in (0, 1, 1000, 65534, 65535):
        assert (
            guest_id(268435456 + ident, mappings) == guest_id(ident, mappings) == ident
        )
    with pytest.raises(ValueError):
        guest_id(200000, mappings)
    with pytest.raises(ValueError):
        parse_id_map("0 1000 65536\n")


def test_capability_and_acl_ids_normalize_across_active_and_raw_views():
    view = GuestArchiveView(
        "example",
        uid_map=(IdMapping(0, 268435456, 65536),),
        gid_map=(IdMapping(0, 268435456, 65536),),
    )
    capability = struct.pack("<6I", 0x03000001, 1, 0, 0, 0, 268435456)
    assert (
        struct.unpack("<6I", normalize_xattr("security.capability", capability, view))[
            -1
        ]
        == 0
    )
    acl = struct.pack("<IHHIHHI", 2, 2, 7, 268436456, 8, 5, 268437456)
    normalized = normalize_xattr("system.posix_acl_access", acl, view)
    assert struct.unpack("<IHHIHHI", normalized) == (2, 2, 7, 1000, 8, 5, 2000)
    assert normalize_xattr("system.posix_acl_access", normalized, view) == normalized
    with pytest.raises(ValueError):
        normalize_xattr("security.capability", b"bad", view)


def test_archive_can_switch_from_active_to_unmounted_ids(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.guest_metadata.capture_xattrs", lambda *_: {})
    view = GuestArchiveView(
        "example",
        uid_map=(IdMapping(0, 268435456, 65536),),
        gid_map=(IdMapping(0, 268435456, 65536),),
    )
    normalize = guest_tar_filter(source=tmp_path, archive_prefix="example", view=view)
    for index, (uid, gid) in enumerate(((268436456, 268437456), (1000, 2000))):
        info = tarfile.TarInfo(f"example/rootfs/data/{index}")
        info.uid, info.gid = uid, gid
        assert (normalize(info).uid, info.gid) == (1000, 2000)
    outside = tarfile.TarInfo("example/profile.json")
    outside.uid = 268435456
    assert normalize(outside).uid == 268435456


@pytest.mark.skipif(
    sys.platform != "linux", reason="Requires Linux setuid filesystem semantics"
)
def test_restore_preserves_special_bits_zero_modes_and_hardlinks(tmp_path):
    archive_path = tmp_path / "guest.tar"
    with tarfile.open(archive_path, "w") as archive:
        for name, mode in (("rootfs/tool", 0o4755), ("rootfs/locked", 0)):
            item = tarfile.TarInfo(name)
            item.uid, item.gid = os.getuid(), os.getgid()
            item.mode, item.size = mode, 1
            archive.addfile(item, io.BytesIO(b"x"))
        link = tarfile.TarInfo("rootfs/linked")
        link.type, link.linkname = tarfile.LNKTYPE, "rootfs/tool"
        link.uid, link.gid, link.mode = os.getuid(), os.getgid(), 0o4755
        archive.addfile(link)
    destination = tmp_path / "stage"
    with tarfile.open(archive_path) as archive:
        safe_extract_tar(
            archive,
            destination,
            allow_relative_symlinks=True,
            guest_metadata_roots={"rootfs"},
        )
    assert (destination / "rootfs/tool").stat().st_mode & 0o7777 == 0o4755
    assert (destination / "rootfs/locked").stat().st_mode & 0o7777 == 0
    assert (destination / "rootfs/tool").stat().st_ino == (
        destination / "rootfs/linked"
    ).stat().st_ino
    assert destination.stat().st_mode & 0o777 == 0o700


def test_archive_validation_rejects_host_ids_and_host_labels_before_extract(tmp_path):
    for uid, attrs in (
        (268435456, {}),
        (0, {"security.selinux": base64.b64encode(b"host-label").decode()}),
    ):
        archive_path = tmp_path / "invalid.tar"
        with tarfile.open(archive_path, "w") as archive:
            item = tarfile.TarInfo("rootfs/file")
            item.uid = uid
            item.pax_headers[XATTR_HEADER] = json.dumps(attrs)
            archive.addfile(item)
        with (
            tarfile.open(archive_path) as archive,
            pytest.raises(UnsafeTarArchiveError),
        ):
            validate_safe_tar(archive, guest_metadata_roots={"rootfs"})


def _archive(tmp_path, names):
    path = tmp_path / "boundary.tar"
    with tarfile.open(path, "w") as archive:
        for name in names:
            item = tarfile.TarInfo(name)
            item.uid, item.gid = os.getuid(), os.getgid()
            item.size = 1
            archive.addfile(item, io.BytesIO(b"x"))
    return path


def test_metadata_extraction_rejects_existing_external_hardlink(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    stage = tmp_path / "stage"
    (stage / "rootfs").mkdir(parents=True)
    os.link(outside, stage / "rootfs/file")
    with tarfile.open(_archive(tmp_path, ["rootfs/file"])) as archive:
        with pytest.raises(UnsafeTarArchiveError, match="empty extraction root"):
            safe_extract_tar(archive, stage, guest_metadata_roots={"rootfs"})
    assert outside.read_text() == "untouched"


def test_metadata_duplicates_are_rejected_by_validation_and_extraction(tmp_path):
    path = _archive(tmp_path, ["rootfs/file", "rootfs/file"])
    for extract in (False, True):
        with tarfile.open(path) as archive:
            with pytest.raises(UnsafeTarArchiveError, match="Duplicate"):
                if extract:
                    safe_extract_tar(
                        archive, tmp_path / "stage", guest_metadata_roots={"rootfs"}
                    )
                else:
                    validate_safe_tar(archive, guest_metadata_roots={"rootfs"})


def test_rootfs_name_does_not_implicitly_allow_host_symlinks(tmp_path):
    path = tmp_path / "symlink.tar"
    with tarfile.open(path, "w") as archive:
        item = tarfile.TarInfo("rootfs/escape")
        item.type, item.linkname = tarfile.SYMTYPE, "/etc/passwd"
        archive.addfile(item)
    with tarfile.open(path) as archive:
        with pytest.raises(UnsafeTarArchiveError, match="escapes"):
            validate_safe_tar(archive, allow_relative_symlinks=True)
        validate_safe_tar(
            archive, allow_relative_symlinks=True, guest_symlink_roots={"rootfs"}
        )


def test_archive_observation_errors_and_identity_loss_fail_closed():
    from subprocess import CompletedProcess

    def failed(command, **_):
        return CompletedProcess(command, 125, "", "observation failed")

    with pytest.raises(ValueError, match="inspect"):
        GuestArchiveView.capture("example", failed)

    def absent(command, **_):
        return CompletedProcess(
            command, 1 if command[1] == "container" else 125, "", ""
        )

    assert GuestArchiveView.capture("example", absent).identity == ""
    with pytest.raises(ValueError, match="replaced"):
        GuestArchiveView("example", "captured-id").verify(absent)

    def stopped(command, **_):
        return CompletedProcess(command, 0, "captured-id 0", "")

    GuestArchiveView(
        "example", "captured-id", (IdMapping(0, 268435456, 65536),)
    ).verify(stopped)


def test_symlink_xattrs_restore_without_following_target(monkeypatch, tmp_path):
    from app.services.guest_metadata import restore_guest_metadata

    outside = tmp_path / "outside"
    outside.write_text("untouched")
    link = tmp_path / "link"
    link.symlink_to(outside)
    item = tarfile.TarInfo("link")
    item.type = tarfile.SYMTYPE
    item.uid, item.gid = os.getuid(), os.getgid()
    item.pax_headers[XATTR_HEADER] = json.dumps(
        {"user.fixture": base64.b64encode(b"value").decode()}
    )
    writes = []
    monkeypatch.setattr(
        os,
        "setxattr",
        lambda *args, **kwargs: writes.append((args, kwargs)),
        raising=False,
    )
    restore_guest_metadata(item, link)
    assert writes == [((link, "user.fixture", b"value"), {"follow_symlinks": False})]
    assert outside.read_text() == "untouched"


def test_owner_only_restore_does_not_apply_privileged_metadata(monkeypatch, tmp_path):
    from app.services.guest_metadata import restore_guest_metadata

    target = tmp_path / "volume-file"
    target.write_text("data")
    target.chmod(0o640)
    item = tarfile.TarInfo("volume-file")
    item.uid, item.gid, item.mode = os.getuid(), os.getgid(), 0o4755
    item.pax_headers[XATTR_HEADER] = json.dumps(
        {
            "security.capability": base64.b64encode(
                struct.pack("<5I", 0x02000001, 1024, 0, 0, 0)
            ).decode()
        }
    )

    def unexpected(*_, **__):
        pytest.fail("ownership-only restore applied privileged metadata")

    monkeypatch.setattr(os, "setxattr", unexpected, raising=False)
    restore_guest_metadata(item, target, ownership_only=True)
    assert target.stat().st_mode & 0o7777 == 0o640
