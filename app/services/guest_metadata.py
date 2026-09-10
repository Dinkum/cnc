"""Portable guest filesystem metadata, independent of a host's user-ID map."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import subprocess
import tarfile
from typing import Callable

ID_COUNT = 65536
XATTR_HEADER = "CNC.guest.xattrs"
MAX_XATTR_BYTES = 256 * 1024


@dataclass(frozen=True)
class IdMapping:
    guest: int
    host: int
    count: int


def parse_id_map(text: str) -> tuple[IdMapping, ...]:
    maps = tuple(
        IdMapping(*map(int, line.split())) for line in text.splitlines() if line.strip()
    )
    if all(item.guest == item.host for item in maps):
        return ()  # Legacy host-root guests already expose canonical ownership.
    for item in maps:
        if (
            item.guest < 0
            or item.count < 1
            or item.guest + item.count > ID_COUNT
            or item.host < ID_COUNT
        ):
            raise ValueError("Guest ID map cannot be normalized safely.")
    for previous, following in zip(
        sorted(maps, key=lambda item: item.host),
        sorted(maps, key=lambda item: item.host)[1:],
    ):
        if previous.host + previous.count > following.host:
            raise ValueError("Guest ID map has overlapping host ranges.")
    return maps


def guest_id(value: int, mappings: tuple[IdMapping, ...]) -> int:
    if 0 <= value < ID_COUNT:
        return value
    for mapping in mappings:
        if mapping.host <= value < mapping.host + mapping.count:
            return mapping.guest + value - mapping.host
    raise ValueError(f"Filesystem owner {value} is outside the guest ID map.")


@dataclass(frozen=True)
class GuestArchiveView:
    container: str
    identity: str = ""
    uid_map: tuple[IdMapping, ...] = ()
    gid_map: tuple[IdMapping, ...] = ()

    @classmethod
    def capture(
        cls, container: str, runner: Callable | None = None
    ) -> GuestArchiveView:
        command = ["podman", "inspect", "--format", "{{.Id}} {{.State.Pid}}", container]
        try:
            result = (
                runner(command, timeout_sec=15)
                if runner is not None
                else subprocess.run(command, capture_output=True, text=True, timeout=15)
            )
        except FileNotFoundError as exc:
            raise ValueError(
                "Cannot inspect the guest archive ownership view."
            ) from exc
        if result.returncode:
            # Podman uses 125 for both absence and observation errors. Only an
            # explicit existence query's documented 'absent' result permits raw IDs.
            exists_command = ["podman", "container", "exists", container]
            exists = (
                runner(exists_command, timeout_sec=15)
                if runner is not None
                else subprocess.run(
                    exists_command, capture_output=True, text=True, timeout=15
                )
            )
            if exists.returncode == 1:
                return cls(container)
            raise ValueError("Cannot inspect the guest archive ownership view.")
        fields = result.stdout.strip().split()
        if len(fields) != 2 or not fields[1].isdigit():
            raise ValueError("Cannot determine the guest archive ownership view.")
        identity, pid = fields
        if int(pid) == 0:
            return cls(container, identity)
        try:
            uid_map = parse_id_map(Path(f"/proc/{pid}/uid_map").read_text())
            gid_map = parse_id_map(Path(f"/proc/{pid}/gid_map").read_text())
        except FileNotFoundError as exc:
            raise ValueError(
                "Guest exited while observing its ownership view; retry the archive."
            ) from exc
        return cls(container, identity, uid_map, gid_map)

    def verify(self, runner: Callable | None = None) -> None:
        current = self.capture(self.container, runner)
        if current.identity != self.identity:
            raise ValueError("Guest was replaced while its archive was being captured.")
        if current.uid_map and (
            current.uid_map != self.uid_map or current.gid_map != self.gid_map
        ):
            raise ValueError(
                "Guest ID mapping changed while its archive was being captured."
            )


def normalize_xattr(name: str, value: bytes, view: GuestArchiveView) -> bytes:
    if name == "security.capability":
        if len(value) < 4:
            raise ValueError("Invalid guest file capability metadata.")
        revision = struct.unpack_from("<I", value)[0] & 0xFF000000
        if revision == 0x02000000 and len(value) == 20:
            return value
        if revision == 0x03000000 and len(value) == 24:
            root_id = guest_id(struct.unpack_from("<I", value, 20)[0], view.uid_map)
            return value[:20] + struct.pack("<I", root_id)
        raise ValueError("Unsupported guest file capability metadata.")
    if name in {"system.posix_acl_access", "system.posix_acl_default"}:
        if (
            len(value) < 4
            or (len(value) - 4) % 8
            or struct.unpack_from("<I", value)[0] != 2
        ):
            raise ValueError("Invalid guest POSIX ACL metadata.")
        normalized = bytearray(value)
        for offset in range(4, len(value), 8):
            tag, _permissions, ident = struct.unpack_from("<HHI", value, offset)
            if tag in {2, 8}:  # ACL_USER / ACL_GROUP carry numeric IDs.
                mapping = view.uid_map if tag == 2 else view.gid_map
                struct.pack_into("<I", normalized, offset + 4, guest_id(ident, mapping))
            elif tag not in {1, 4, 16, 32}:
                raise ValueError("Unsupported guest POSIX ACL entry.")
        return bytes(normalized)
    if name.startswith("user."):
        return value
    raise ValueError(f"Unsupported guest extended attribute: {name}")


def capture_xattrs(path: Path, view: GuestArchiveView) -> dict[str, str]:
    if not hasattr(os, "listxattr"):
        return {}
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as exc:
        if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            return {}
        raise
    result = {}
    size = 0
    for name in names:
        # Host LSM labels are not guest state and must not cross hosts.
        if name in {"security.selinux", "security.apparmor"}:
            continue
        value = os.getxattr(path, name, follow_symlinks=False)
        value = normalize_xattr(name, value, view)
        size += len(name) + len(value)
        if size > MAX_XATTR_BYTES or len(result) >= 64:
            raise ValueError("Guest file extended attributes exceed the archive limit.")
        result[name] = base64.b64encode(value).decode("ascii")
    return result


def decode_xattrs(member: tarfile.TarInfo) -> dict[str, bytes]:
    text = member.pax_headers.get(XATTR_HEADER, "{}")
    if len(text) > MAX_XATTR_BYTES * 2:
        raise ValueError("Guest extended attribute metadata exceeds the archive limit.")
    encoded = json.loads(text)
    if not isinstance(encoded, dict) or len(encoded) > 64:
        raise ValueError("Invalid guest extended attribute metadata.")
    result = {}
    for name, value in encoded.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("Invalid guest extended attribute metadata.")
        raw = base64.b64decode(value, validate=True)
        result[name] = normalize_xattr(name, raw, GuestArchiveView(""))
    if sum(len(name) + len(value) for name, value in result.items()) > MAX_XATTR_BYTES:
        raise ValueError("Guest extended attribute metadata exceeds the archive limit.")
    return result


def validate_guest_metadata(member: tarfile.TarInfo) -> None:
    guest_id(member.uid, ())
    guest_id(member.gid, ())
    decode_xattrs(member)


def guest_tar_filter(
    *,
    source: Path,
    archive_prefix: str,
    view: GuestArchiveView,
    rootfs_prefix: str = "rootfs",
    include_xattrs: bool = True,
) -> Callable[[tarfile.TarInfo], tarfile.TarInfo | None]:
    prefix = PurePosixPath(archive_prefix)
    rootfs = prefix / rootfs_prefix if rootfs_prefix else prefix

    def normalize(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if (
            member.isdev()
            or member.isfifo()
            or not (
                member.isdir() or member.isreg() or member.issym() or member.islnk()
            )
        ):
            return None
        path = PurePosixPath(member.name)
        if not (path == rootfs or path.is_relative_to(rootfs)):
            return member
        relative = path.relative_to(prefix)
        host_path = source.joinpath(*relative.parts)
        member.uid = guest_id(member.uid, view.uid_map)
        member.gid = guest_id(member.gid, view.gid_map)
        member.uname = member.gname = ""
        xattrs = capture_xattrs(host_path, view) if include_xattrs else {}
        if xattrs:
            member.pax_headers[XATTR_HEADER] = json.dumps(
                xattrs, sort_keys=True, separators=(",", ":")
            )
        return member

    return normalize


def restore_guest_metadata(
    member: tarfile.TarInfo, target: Path, *, ownership_only: bool = False
) -> None:
    validate_guest_metadata(member)
    if member.issym():
        observed = target.lstat()
        if not stat.S_ISLNK(observed.st_mode):
            raise ValueError("Unexpected guest archive symlink target.")
        if (observed.st_uid, observed.st_gid) != (member.uid, member.gid):
            os.chown(target, member.uid, member.gid, follow_symlinks=False)
        for name, value in ({} if ownership_only else decode_xattrs(member)).items():
            os.setxattr(target, name, value, follow_symlinks=False)
        return
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if member.isdir():
        flags |= os.O_DIRECTORY
    fd = os.open(target, flags)
    try:
        observed = os.fstat(fd)
        if not (stat.S_ISREG(observed.st_mode) or stat.S_ISDIR(observed.st_mode)):
            raise ValueError("Unexpected guest archive extraction target.")
        if (observed.st_uid, observed.st_gid) != (member.uid, member.gid):
            os.fchown(fd, member.uid, member.gid)
        if ownership_only:
            return
        os.utime(fd, (member.mtime, member.mtime))
        # chown (and timestamp changes on some filesystems) clears special bits
        # and file capabilities; restore those only after ordinary metadata.
        os.fchmod(fd, member.mode & 0o7777)
        for name, value in decode_xattrs(member).items():
            os.setxattr(fd, name, value)
    finally:
        os.close(fd)
