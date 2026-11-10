#!/usr/bin/env python3

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import grp
import io
import json
from itertools import combinations
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import tarfile
import zipfile

GUEST_RUNTIME_REVISION = 2
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
        f"--annotation={GUEST_ROOTFS_ANNOTATION}=true",
        f"--hooks-dir={GUEST_ROOTFS_HOOK.parent}",
        "--cap-add=SYS_ADMIN",
        "--cap-add=MKNOD",
        "--cap-drop=AUDIT_WRITE",
        f"--security-opt=apparmor={GUEST_APPARMOR_PROFILE}",
    ]


def guest_rootfs_argument(rootfs: Path | str) -> str:
    return f"{rootfs}:{GUEST_IDMAP}"


def guest_volume_arguments(volumes: list[str]) -> list[str]:
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

  mount options=(rw, rprivate) -> /,
  mount options=(rw, rslave) -> /,
  mount options=(rw, rshared) -> /,
  mount options=(rw, rslave) -> /dev/,
  pivot_root oldroot=/run/systemd/mount-rootfs/ /run/systemd/mount-rootfs/,
  mount options=(rw, move) /run/systemd/mount-rootfs/ -> /,
  mount options=(ro, remount, bind) -> /,
{read_only_rules}
  mount options=(rw, move) /run/systemd/** -> /run/systemd/**,
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
    repaired: list[str] = []
    for volume in volumes:
        source = Path(volume.split(":", 1)[0])
        try:
            validate_guest_volume_paths([volume])
            continue
        except ValueError:
            if managed_root not in source.parents:
                raise
        validate_guest_volume_paths([f"{source}:/storage:ro"])
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
        managed_root.chmod(0o700)
        repaired.append(str(managed_root))
        validate_guest_volume_paths([volume])
    return repaired


GUEST_ROOTFS_ANNOTATION = "io.cnc.private-rootfs"
GUEST_ROOTFS_HOOK = Path("/etc/containers/oci/hooks.d/90-cnc-rootfs.json")


def install_guest_rootfs_hook(helper: Path) -> None:
    content = (
        json.dumps(
            {
                "version": "1.0.0",
                "hook": {
                    "path": str(helper),
                    "args": [str(helper), "--rootfs-access-hook"],
                    "timeout": 15,
                },
                "when": {"annotations": {GUEST_ROOTFS_ANNOTATION: "^true$"}},
                "stages": ["precreate"],
            },
            indent=2,
        )
        + "\n"
    ).encode()
    if GUEST_ROOTFS_HOOK.is_symlink():
        raise ValueError("Guest rootfs hook cannot be a symlink.")
    if not GUEST_ROOTFS_HOOK.exists() or GUEST_ROOTFS_HOOK.read_bytes() != content:
        _atomic_write(GUEST_ROOTFS_HOOK, content, 0o644)


def prepare_rootfs_access(spec: dict) -> None:
    if spec.get("annotations", {}).get(GUEST_ROOTFS_ANNOTATION) != "true":
        raise ValueError("Rootfs access hook requires a CNC guest annotation.")
    rootfs = Path(spec.get("root", {}).get("path", ""))
    sandbox = rootfs.parent
    profile = sandbox / "profile.json"
    if (
        not rootfs.is_absolute()
        or rootfs.name != "rootfs"
        or rootfs.is_symlink()
        or not rootfs.is_dir()
        or profile.is_symlink()
        or not profile.is_file()
        or profile.stat().st_uid != os.geteuid()
        or profile.stat().st_mode & 0o022
    ):
        raise ValueError("Rootfs access hook requires a managed guest filesystem.")
    for parent in (sandbox, *sandbox.parents):
        observed = parent.stat()
        if (
            parent.is_symlink()
            or observed.st_uid not in {0, os.geteuid()}
            or (observed.st_mode & 0o022 and not observed.st_mode & 0o1000)
        ):
            raise ValueError("Guest rootfs must have trusted host ancestors.")
    mappings = spec.get("linux", {}).get("uidMappings", [])
    roots = [m for m in mappings if m.get("containerID") == 0]
    if (
        len(roots) != 1
        or not isinstance(roots[0].get("hostID"), int)
        or roots[0]["hostID"] < GUEST_ID_COUNT
    ):
        raise ValueError("Rootfs access hook requires an isolated guest UID map.")
    protect_sandbox_directory(sandbox)
    subprocess.run(
        [
            "setfacl",
            "--set",
            f"u::rwx,u:{roots[0]['hostID']}:--x,g::---,m::--x,o::---",
            str(sandbox),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def require_supported_podman() -> None:
    try:
        result = subprocess.run(
            ["podman", "version", "--format", "{{.Client.Version}}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError as error:
        raise ValueError(
            "podman is not installed on this host. The installer installs CNC's "
            "pinned runtime; a host without it has not had that step applied."
        ) from error
    except subprocess.TimeoutExpired as error:
        raise ValueError(
            "podman did not report its version within 15s. A runtime that hangs "
            "on `podman version` cannot run app guests."
        ) from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip().splitlines()
        reason = " / ".join(line.strip() for line in detail[-3:]) or "no output"
        raise ValueError(
            f"podman could not report its version (exit {error.returncode}): {reason}"
        ) from error
    version = result.stdout.strip()
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[+~-].*)?", version)
    if match is None or tuple(map(int, match.groups())) < (5, 2, 0):
        raise ValueError(
            "CNC app guests require Podman 5.2.0 or newer to preserve private "
            f"rootfs permissions (found {version or 'unknown'}). "
            "The installer installs the pinned runtime; a host below the floor "
            "has not had it applied yet."
        )


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
    require_supported_podman()
    if shutil.which("setfacl") is None:
        raise ValueError("CNC guest startup requires the Ubuntu acl package (setfacl).")
    install_guest_rootfs_hook(Path(sys.argv[0]).resolve())
    validate_guest_volume_paths(volumes or [])
    ensure_subid_pool()
    profile_path = Path("/etc/apparmor.d") / GUEST_APPARMOR_PROFILE
    content = guest_apparmor_profile().encode()
    if profile_path.is_symlink():
        raise ValueError("Guest AppArmor profile cannot be a symlink.")
    if not profile_path.exists() or profile_path.read_bytes() != content:
        _atomic_write(profile_path, content, 0o644)
    subprocess.run(["apparmor_parser", "-r", str(profile_path)], check=True, timeout=30)
    if rootfs is not None:
        if rootfs.is_symlink() or not rootfs.is_dir():
            raise ValueError("Guest rootfs must be an existing directory.")
        protect_sandbox_directory(rootfs.parent)


def guest_helper_bytes() -> bytes:
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
    parser.add_argument("--check-runtime-only", action="store_true")
    parser.add_argument("--rootfs-access-hook", action="store_true")
    args = parser.parse_args()
    if args.rootfs_access_hook:
        spec = json.load(sys.stdin)
        prepare_rootfs_access(spec)
        json.dump(spec, sys.stdout)
    elif args.check_runtime_only:
        try:
            require_supported_podman()
        except ValueError as error:
            print(f"[runtime] {error}", file=sys.stderr)
            raise SystemExit(1) from error
    elif args.archive is not None:
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
