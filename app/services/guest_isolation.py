#!/usr/bin/env python3
"""Host preparation and the shared Ubuntu guest isolation contract.

This module is also installed as a standalone helper on container hosts. Keep it
standard-library-only so a transferred guest can start without the CNC server.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import grp
import io
from itertools import combinations
import os
from pathlib import Path
import pwd
import re
import subprocess
import tempfile
import tarfile
import zipfile

GUEST_RUNTIME_REVISION = 1
GUEST_ID_COUNT = 65536
GUEST_APPARMOR_PROFILE = "cnc-systemd-guest-v1"
GUEST_IDMAP = "idmap=uids=@0-0-65536;gids=@0-0-65536"
GUEST_INIT_CAPABILITIES = frozenset(
    {
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "FSETID",
        "KILL",
        "MKNOD",
        "SETFCAP",
        "SETGID",
        "SETPCAP",
        "SETUID",
        "SYS_ADMIN",
        "SYS_CHROOT",
    }
)
_POOL_START = 1 << 28
_POOL_SIZE = GUEST_ID_COUNT * 1024
_POOL_END = 1 << 31


def guest_podman_args() -> list[str]:
    return [
        f"--userns=auto:size={GUEST_ID_COUNT}",
        "--cap-add=SYS_ADMIN",
        "--cap-add=MKNOD",
        # Audit writes address the host audit subsystem, not a guest resource.
        "--cap-drop=AUDIT_WRITE",
        f"--security-opt=apparmor={GUEST_APPARMOR_PROFILE}",
    ]


def guest_rootfs_argument(rootfs: Path | str) -> str:
    return f"{rootfs}:{GUEST_IDMAP}"


def guest_volume_arguments(volumes: list[str]) -> list[str]:
    """Keep the existing guest ownership view on absolute host bind mounts."""
    result = []
    for volume in volumes:
        parts = volume.split(":", 2)
        if len(parts) < 2 or not parts[0].startswith("/"):
            raise ValueError("Ubuntu guests require absolute host paths for volumes.")
        options = parts[2].split(",") if len(parts) > 2 and parts[2] else []
        if any(
            option in {"U", "O"} or option.startswith("idmap") for option in options
        ):
            raise ValueError(
                "Guest volume ownership is managed by CNC; remove U, O, or idmap options."
            )
        result.append(":".join(parts[:2]) + ":" + ",".join([*options, GUEST_IDMAP]))
    return result


def validate_guest_hardening(policy: dict) -> None:
    profile = policy.get("apparmor_profile", "")
    if profile and profile != GUEST_APPARMOR_PROFILE:
        raise ValueError(
            "Ubuntu guests require CNC's systemd AppArmor profile. Clear the "
            "container AppArmor override before applying this output."
        )
    if policy.get("cap_drop_all"):
        missing = sorted(GUEST_INIT_CAPABILITIES - set(policy.get("cap_add", [])))
        if missing:
            raise ValueError(
                "The Ubuntu init system requires these namespaced capability "
                "exceptions when dropping all capabilities: " + ", ".join(missing)
            )


def guest_apparmor_profile() -> str:
    # A remount wildcard must REQUIRE ro+remount+bind. AppArmor's `options in`
    # accepts subsets, which would also authorize a plain writable bind mount.
    optional = ("nosuid", "nodev", "noexec", "relatime", "strictatime")
    read_only_rules = "\n".join(
        "  mount options=(" + ", ".join(("ro", "remount", "bind", *extra)) + ") -> /**,"
        for size in range(len(optional) + 1)
        for extra in combinations(optional, size)
    )
    return f"""#include <tunables/global>
profile {GUEST_APPARMOR_PROFILE} flags=(attach_disconnected,mediate_deleted) {{
  #include <abstractions/base>
  network,
  capability,
  file,
  umount,
  signal (receive) peer=unconfined,
  signal (send,receive) peer={GUEST_APPARMOR_PROFILE},
  signal (receive) peer={{/usr/bin/,/usr/sbin/,}}crun*,
  signal (receive) peer={{/usr/bin/,/usr/sbin/,}}runc,
  signal (receive) peer={{/usr/bin/,/usr/sbin/,}}podman,
  ptrace (trace,read) peer={GUEST_APPARMOR_PROFILE},

  # systemd builds service mount namespaces below its private staging tree.
  mount options=(rw, rprivate) -> /,
  mount options=(rw, rslave) -> /,
  mount options=(rw, rshared) -> /,
  mount options=(rw, rslave) -> /dev/,
  pivot_root oldroot=/run/systemd/mount-rootfs/ /run/systemd/mount-rootfs/,
  mount options=(rw, move) /run/systemd/mount-rootfs/ -> /,
  mount options=(ro, remount, bind) -> /,
{read_only_rules}
  mount options=(rw, move) /run/systemd/** -> /run/systemd/**,
  # This construction-zone exception allows service-specific bind/remount flags.
  # It cannot mount a new filesystem outside systemd's staging tree.
  mount options in (rw, ro, bind, rbind, remount, nosuid, nodev, noexec, relatime, strictatime, silent, slave, rslave, private, rprivate, shared, rshared, unbindable, runbindable) -> /run/systemd/**,
  mount fstype=tmpfs -> /run/systemd/**,
  mount fstype=tmpfs -> /tmp/,
  mount fstype=tmpfs -> /var/tmp/,
  mount fstype=tmpfs -> /run/user/*/,
  mount fstype=ramfs -> /dev/shm/,
  mount fstype=devpts -> /run/systemd/**,
  mount fstype=proc -> /run/systemd/**,

  deny /proc/* w,
  deny /proc/sysrq-trigger rwklx,
  deny /proc/kcore rwklx,
  deny /proc/sys/** w,
  # Keep the cgroup subtree available to guest systemd, while denying writes
  # through the remaining host sysfs interfaces.
  deny /sys/[^f]*/** wklx,
  deny /sys/f[^s]*/** wklx,
  deny /sys/fs/[^c]*/** wklx,
  deny /sys/fs/c[^g]*/** wklx,
  deny /sys/fs/cg[^r]*/** wklx,
  deny /sys/fs/cgr[^o]*/** wklx,
  deny /sys/fs/cgro[^u]*/** wklx,
  deny /sys/fs/cgrou[^p]*/** wklx,
  deny /sys/firmware/** rwklx,
  deny /sys/kernel/security/** rwklx,
}}
"""


@dataclass(frozen=True)
class SubidRange:
    name: str
    start: int
    count: int

    @property
    def end(self) -> int:
        return self.start + self.count


def _subid_ranges(text: str) -> list[SubidRange]:
    result = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split(":")
        if (
            len(fields) != 3
            or not fields[0]
            or not all(re.fullmatch(r"[0-9]+", item) for item in fields[1:])
        ):
            raise ValueError(
                "Invalid subordinate-ID configuration; repair subuid/subgid first."
            )
        entry = SubidRange(fields[0], int(fields[1]), int(fields[2]))
        if entry.count < 1 or entry.start < 1 or entry.end > (1 << 32) - 1:
            raise ValueError("Invalid subordinate-ID range.")
        result.append(entry)
    return result


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_subid_pool(
    *,
    subuid: Path = Path("/etc/subuid"),
    subgid: Path = Path("/etc/subgid"),
    lock: Path = Path("/run/lock/cnc-guest-subids.lock"),
    host_ids: set[int] | None = None,
) -> SubidRange:
    """Reserve one pool; Podman owns concurrent allocation and range release.

    UID/GID files cannot be replaced atomically together. A partially written
    reservation is retained and completed on retry, never allocated a new base.
    """
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        paths = (subuid, subgid)
        for path in paths:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError(
                    f"Subordinate-ID configuration must be a regular file: {path}"
                )
            if path.with_name(path.name + ".lock").exists():
                raise ValueError(
                    "A host account update is in progress; retry guest preparation."
                )
        texts = {path: path.read_text() if path.exists() else "" for path in paths}
        ranges = {path: _subid_ranges(text) for path, text in texts.items()}
        pools = [
            entry
            for entries in ranges.values()
            for entry in entries
            if entry.name == "containers"
        ]
        if any(
            sum(entry.name == "containers" for entry in entries) > 1
            for entries in ranges.values()
        ):
            raise ValueError(
                "The containers subordinate-ID pool must be a single range."
            )
        if pools and any(entry != pools[0] for entry in pools):
            raise ValueError("The containers subuid and subgid pools must match.")
        occupied = [
            entry
            for entries in ranges.values()
            for entry in entries
            if entry.name != "containers"
        ]
        ids = (
            host_ids
            if host_ids is not None
            else {
                *(entry.pw_uid for entry in pwd.getpwall()),
                *(entry.gr_gid for entry in grp.getgrall()),
            }
        )
        occupied.extend(SubidRange("host", ident, 1) for ident in ids)
        if pools:
            pool = pools[0]
        else:
            start = _POOL_START
            for entry in sorted(occupied, key=lambda item: item.start):
                if start < entry.end and start + _POOL_SIZE > entry.start:
                    start = (
                        (entry.end + GUEST_ID_COUNT - 1) // GUEST_ID_COUNT
                    ) * GUEST_ID_COUNT
            if start + _POOL_SIZE > _POOL_END:
                raise ValueError("No free subordinate-ID pool for Ubuntu guests.")
            pool = SubidRange("containers", start, _POOL_SIZE)
        if pool.start < GUEST_ID_COUNT or pool.count < GUEST_ID_COUNT:
            raise ValueError(
                "The containers pool must exclude host system IDs and hold at least 65536 IDs."
            )
        if any(pool.start < entry.end and pool.end > entry.start for entry in occupied):
            raise ValueError(
                "The containers subordinate-ID pool overlaps another host identity or reservation."
            )
        for path in paths:
            if any(entry.name == "containers" for entry in ranges[path]):
                continue
            current = path.read_text() if path.exists() else ""
            if current != texts[path]:
                raise ValueError(
                    "Subordinate-ID configuration changed during preparation; retry."
                )
            text = (
                current.rstrip("\n")
                + ("\n" if current else "")
                + f"containers:{pool.start}:{pool.count}\n"
            )
            _atomic_write(path, text.encode(), 0o644)
        return pool


def protect_sandbox_directory(sandbox: Path) -> None:
    """Guest-owned setuid files must never be executable through a host path."""
    if sandbox.is_symlink():
        raise ValueError("Sandbox directory cannot be a symlink.")
    sandbox.mkdir(parents=True, mode=0o700, exist_ok=True)
    if sandbox.stat().st_uid != os.geteuid():
        raise ValueError("Sandbox directory must belong to the CNC host administrator.")
    sandbox.chmod(0o700)


def validate_guest_volume_paths(
    volumes: list[str], *, allow_missing: bool = False
) -> None:
    for volume in volumes:
        parts = volume.split(":", 2)
        source = Path(parts[0])
        if not source.is_absolute() or ".." in source.parts:
            raise ValueError("Guest bind sources must use absolute canonical paths.")
        components = [source, *source.parents]
        if any(path.is_symlink() for path in components) or (
            not allow_missing and not source.exists()
        ):
            raise ValueError("Guest bind sources must exist and cannot be symlinks.")
        options = parts[2].split(",") if len(parts) == 3 else []
        if "ro" in options and "rw" not in options:
            continue
        # Writable guest files retain canonical host IDs, including setuid root.
        # Require an existing private boundary; never chmod a user's shared data.
        # The boundary must sit outside the mounted subtree, where guest root
        # cannot chmod it to expose canonical setuid files to host users.
        parents = [parent for parent in source.parents if parent.exists()]
        boundary = next(
            (
                parent
                for parent in parents
                if parent.stat().st_uid == 0 and parent.stat().st_mode & 0o077 == 0
            ),
            None,
        )
        if boundary is None:
            raise ValueError(
                "Writable guest bind mounts require a root-owned private directory "
                "(mode 0700) around their host storage."
            )
        for parent in boundary.parents:
            observed = parent.stat()
            if observed.st_uid != 0 or (
                observed.st_mode & 0o022 and not observed.st_mode & 0o1000
            ):
                raise ValueError(
                    "Guest storage's private boundary must have trusted host ancestors."
                )


def preflight_guest_storage(
    volumes: list[str],
    *,
    mounted_sources: list[Path],
    managed_root: Path = Path("/var/lib/cnc/apps"),
) -> list[str]:
    """Repair only CNC's private storage boundary, never guest-owned contents."""
    repaired: list[str] = []
    for volume in volumes:
        source = Path(volume.split(":", 1)[0])
        try:
            validate_guest_volume_paths([volume])
            continue
        except ValueError:
            if managed_root not in source.parents:
                raise
        # Validate existence and canonical paths before considering a repair.
        validate_guest_volume_paths([f"{source}:/storage:ro"])
        # Legacy mount sources may use an alias even though new mounts reject
        # symlinks. Such a mount must not expose the boundary inside a guest.
        canonical_sources = [path.resolve() for path in mounted_sources]
        if any(
            managed_root == path or path in managed_root.parents
            for path in canonical_sources
        ):
            raise ValueError(
                "CNC storage parent is itself exposed inside a guest; automatic repair is unsafe."
            )
        for parent in [managed_root, *managed_root.parents]:
            observed = parent.stat()
            if (
                parent.is_symlink()
                or not parent.is_dir()
                or observed.st_uid != 0
                or observed.st_mode & 0o022
            ):
                raise ValueError(
                    "CNC storage parent has untrusted ownership or permissions; automatic repair is unsafe."
                )
        observed = managed_root.stat()
        if observed.st_gid != 0 or observed.st_mode & 0o7000:
            raise ValueError(
                "CNC storage parent has a custom group or special permissions; automatic repair is unsafe."
            )
        if hasattr(os, "listxattr") and any(
            "acl" in name.lower() for name in os.listxattr(managed_root)
        ):
            raise ValueError(
                "CNC storage parent has an access-control list; automatic repair is unsafe."
            )
        # Only the host-owned outer boundary changes. Guest data and ownership
        # remain untouched, and the boundary cannot be chmodded from the guest.
        managed_root.chmod(0o700)
        repaired.append(str(managed_root))
        validate_guest_volume_paths([volume])
    return repaired


def prepare_guest_host(
    rootfs: Path | None = None, volumes: list[str] | None = None
) -> None:
    if os.geteuid() != 0:
        raise ValueError("Guest host preparation requires root.")
    if (
        not Path("/sys/module/apparmor/parameters/enabled")
        .read_text()
        .strip()
        .startswith("Y")
    ):
        raise ValueError(
            "Ubuntu guest isolation requires AppArmor enabled on the host."
        )
    validate_guest_volume_paths(volumes or [])
    ensure_subid_pool()
    profile_path = Path("/etc/apparmor.d") / GUEST_APPARMOR_PROFILE
    content = guest_apparmor_profile().encode()
    if profile_path.is_symlink():
        raise ValueError("Guest AppArmor profile cannot be a symlink.")
    if not profile_path.exists() or profile_path.read_bytes() != content:
        _atomic_write(profile_path, content, 0o644)
    # Load on every start: the file surviving reboot does not prove the kernel
    # profile is loaded, and Quadlet must never fall back to an unconfined guest.
    subprocess.run(["apparmor_parser", "-r", str(profile_path)], check=True, timeout=30)
    if rootfs is not None:
        if rootfs.is_symlink() or not rootfs.is_dir():
            raise ValueError("Guest rootfs must be an existing directory.")
        protect_sandbox_directory(rootfs.parent)


def guest_helper_bytes() -> bytes:
    """Build a deterministic standard-library zip application for every host."""
    buffer = io.BytesIO()
    buffer.write(b"#!/usr/bin/env python3\n")
    sources = {
        "__main__.py": b"from app.services.guest_isolation import main\nmain()\n",
        "app/__init__.py": b"",
        "app/services/__init__.py": b"",
    }
    for name in ("guest_isolation.py", "guest_metadata.py", "tar_safety.py"):
        sources[f"app/services/{name}"] = Path(__file__).with_name(name).read_bytes()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in sources.items():
            archive.writestr(zipfile.ZipInfo(name), content)
    return buffer.getvalue()


def archive_guest(sandbox: Path, destination: Path, container: str) -> None:
    from app.services.guest_metadata import GuestArchiveView, guest_tar_filter

    view = GuestArchiveView.capture(container)
    with tarfile.open(destination, "w:gz") as archive:
        archive.add(
            sandbox,
            arcname=sandbox.name,
            filter=guest_tar_filter(
                source=sandbox, archive_prefix=sandbox.name, view=view
            ),
        )
    view.verify()


def extract_guest(archive_path: Path, destination: Path, name: str) -> None:
    from app.services.tar_safety import safe_extract_tar, validate_safe_tar

    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
        raise ValueError("Invalid guest archive name.")
    with tarfile.open(archive_path, "r:*") as archive:
        if any(
            member.name != name and not member.name.startswith(name + "/")
            for member in archive.getmembers()
        ):
            raise ValueError("Guest archive contains paths outside its sandbox.")
        options = {
            "allow_relative_symlinks": True,
            "guest_symlink_roots": {name},
            "guest_metadata_roots": {name + "/rootfs"},
        }
        validate_safe_tar(archive, **options)
        safe_extract_tar(archive, destination, **options)
    protect_sandbox_directory(destination / name)


def extract_guest_rootfs(archive_path: Path, destination: Path) -> None:
    from app.services.tar_safety import safe_extract_tar, validate_safe_tar

    options = {
        "allow_relative_symlinks": True,
        "guest_symlink_roots": {"."},
        "guest_metadata_roots": {"."},
    }
    with tarfile.open(archive_path, "r:*") as archive:
        validate_safe_tar(archive, **options)
        safe_extract_tar(archive, destination, **options)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare CNC Ubuntu guest isolation")
    parser.add_argument("--rootfs", type=Path)
    parser.add_argument("--volume", action="append", default=[])
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--extract", type=Path)
    parser.add_argument("--extract-rootfs", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--name")
    args = parser.parse_args()
    if args.archive is not None:
        if args.destination is None or args.name is None:
            parser.error("--archive requires --destination and --name")
        archive_guest(args.archive, args.destination, "cnc-app-" + args.name)
    elif args.extract_rootfs is not None:
        if args.destination is None:
            parser.error("--extract-rootfs requires --destination")
        extract_guest_rootfs(args.extract_rootfs, args.destination)
    elif args.extract is not None:
        if args.destination is None or args.name is None:
            parser.error("--extract requires --destination and --name")
        extract_guest(args.extract, args.destination, args.name)
    else:
        prepare_guest_host(args.rootfs, args.volume)


if __name__ == "__main__":
    main()
