from __future__ import annotations

from collections.abc import Callable, Iterable
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile

from app.services.error_reporting import CNCError, ErrorCode


class UnsafeTarArchiveError(CNCError):
    def __init__(
        self,
        message: str,
        *,
        error_code: ErrorCode = ErrorCode.RESTORE_FAILED,
    ) -> None:
        super().__init__(error_code, message, status_code=400)


TarMemberPredicate = Callable[[tarfile.TarInfo], bool]


def validate_safe_tar(
    archive: tarfile.TarFile,
    *,
    members: Iterable[tarfile.TarInfo] | None = None,
    include: TarMemberPredicate | None = None,
    allow_relative_symlinks: bool = False,
    guest_symlink_roots: Iterable[str | PurePosixPath] = (),
) -> None:
    destination_root = Path("/__cnc_safe_tar_validation__")
    pending_hardlinks: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
    guest_roots = {_normalize_guest_symlink_root(root) for root in guest_symlink_roots}
    created_paths: dict[PurePosixPath, str] = {}

    for member in members or archive.getmembers():
        relative_path = _validated_member_path(
            member, allow_relative_symlinks=allow_relative_symlinks
        )
        _validate_virtual_target(relative_path, member.name, created_paths)
        if include is not None and not include(member):
            continue
        if member.isdir():
            _record_virtual_path(created_paths, relative_path, "dir", member.name)
            continue
        if member.isreg():
            handle = archive.extractfile(member)
            if handle is None:
                raise UnsafeTarArchiveError(
                    f"tar regular file has no payload: {member.name}"
                )
            handle.close()
            _record_virtual_path(created_paths, relative_path, "file", member.name)
            continue
        if member.issym() and allow_relative_symlinks:
            _validated_symlink_target(
                member,
                relative_path,
                destination_root / Path(*relative_path.parts),
                destination_root,
                guest_roots,
            )
            _record_virtual_path(created_paths, relative_path, "symlink", member.name)
            continue
        if member.islnk() and allow_relative_symlinks:
            pending_hardlinks.append((member, relative_path))
            continue
        raise UnsafeTarArchiveError(f"unsupported tar member type: {member.name}")

    for member, relative_path in pending_hardlinks:
        _validate_hardlink_target(member, created_paths)
        _record_virtual_path(created_paths, relative_path, "file", member.name)


def safe_extract_tar(
    archive: tarfile.TarFile,
    destination: Path,
    *,
    members: Iterable[tarfile.TarInfo] | None = None,
    include: TarMemberPredicate | None = None,
    allow_relative_symlinks: bool = False,
    guest_symlink_roots: Iterable[str | PurePosixPath] = (),
) -> list[Path]:
    if destination.is_symlink():
        raise UnsafeTarArchiveError(f"tar destination is a symlink: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    extracted: list[Path] = []
    pending_hardlinks: list[tuple[tarfile.TarInfo, PurePosixPath, Path]] = []
    guest_roots = {_normalize_guest_symlink_root(root) for root in guest_symlink_roots}

    for member in members or archive.getmembers():
        relative_path = _validated_member_path(
            member, allow_relative_symlinks=allow_relative_symlinks
        )
        target = destination_root / Path(*relative_path.parts)
        _validate_target_containment(destination_root, target, member.name)
        if include is not None and not include(member):
            continue
        if member.isdir():
            _extract_directory(member, target)
            extracted.append(target)
            continue
        if member.isreg():
            _extract_regular_file(archive, member, target)
            extracted.append(target)
            continue
        if member.issym() and allow_relative_symlinks:
            _extract_symlink(
                member, relative_path, target, destination_root, guest_roots
            )
            extracted.append(target)
            continue
        if member.islnk() and allow_relative_symlinks:
            pending_hardlinks.append((member, relative_path, target))
            continue
        raise UnsafeTarArchiveError(f"unsupported tar member type: {member.name}")

    for member, relative_path, target in pending_hardlinks:
        _extract_hardlink_as_regular_file(
            member, relative_path, target, destination_root
        )
        extracted.append(target)

    return extracted


def _validated_member_path(
    member: tarfile.TarInfo, *, allow_relative_symlinks: bool
) -> PurePosixPath:
    member_name = str(member.name or "").strip()
    path = PurePosixPath(member_name)
    if (
        not member_name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise UnsafeTarArchiveError(f"unsafe tar member path: {member.name}")
    if member.issym() and not allow_relative_symlinks:
        raise UnsafeTarArchiveError(
            f"tar links are not allowed: {member.name}",
            error_code=ErrorCode.RESTORE_TAR_SYMLINK_ESCAPES,
        )
    if member.islnk() and not allow_relative_symlinks:
        raise UnsafeTarArchiveError(
            f"tar hardlinks are not allowed: {member.name}",
            error_code=ErrorCode.RESTORE_TAR_HARDLINK_UNSAFE,
        )
    if member.isdev() or member.isfifo():
        raise UnsafeTarArchiveError(
            f"tar devices are not allowed: {member.name}",
            error_code=ErrorCode.RESTORE_TAR_DEVICE_MEMBER,
        )
    if (
        not member.isdir()
        and not member.isreg()
        and not (allow_relative_symlinks and (member.issym() or member.islnk()))
    ):
        raise UnsafeTarArchiveError(f"unsupported tar member type: {member.name}")
    return path


def _validated_symlink_target(
    member: tarfile.TarInfo,
    relative_path: PurePosixPath,
    target: Path,
    destination_root: Path,
    guest_symlink_roots: set[PurePosixPath],
) -> str:
    link_name = str(member.linkname or "").strip()
    link_path = PurePosixPath(link_name)
    if not link_name:
        raise UnsafeTarArchiveError(
            f"unsafe tar symlink target: {member.name}",
            error_code=ErrorCode.RESTORE_TAR_SYMLINK_ESCAPES,
        )
    if _is_guest_symlink_path(relative_path, guest_symlink_roots):
        # Guest filesystem archives contain normal Linux symlinks, including
        # absolute links and links that would escape when interpreted by the
        # host. Extraction still never writes through symlinks because every
        # member path is checked before it is created.
        return link_name
    try:
        if link_path.is_absolute():
            raise ValueError
        else:
            resolved_link = (target.parent / Path(*link_path.parts)).resolve(
                strict=False
            )
            resolved_link.relative_to(destination_root)
    except ValueError as exc:
        raise UnsafeTarArchiveError(
            f"tar symlink escapes destination: {member.name}",
            error_code=ErrorCode.RESTORE_TAR_SYMLINK_ESCAPES,
        ) from exc
    return link_name


def _normalize_guest_symlink_root(root: str | PurePosixPath) -> PurePosixPath:
    path = root if isinstance(root, PurePosixPath) else PurePosixPath(str(root))
    return PurePosixPath(*[part for part in path.parts if part not in {"", "."}])


def _is_guest_symlink_path(
    relative_path: PurePosixPath, guest_symlink_roots: set[PurePosixPath]
) -> bool:
    parts = relative_path.parts
    if "rootfs" in parts:
        return True
    for root in guest_symlink_roots:
        if not root.parts:
            return True
        if root.parts and (relative_path == root or relative_path.is_relative_to(root)):
            return True
    return False


def _validate_target_containment(
    destination_root: Path, target: Path, member_name: str
) -> None:
    try:
        resolved_target = target.resolve(strict=False)
        resolved_target.relative_to(destination_root)
    except ValueError as exc:
        raise UnsafeTarArchiveError(
            f"tar member escapes destination: {member_name}"
        ) from exc

    current = destination_root
    relative_parts = target.relative_to(destination_root).parts
    for part in relative_parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise UnsafeTarArchiveError(
                f"tar member crosses destination symlink: {member_name}"
            )
    if target.is_symlink():
        raise UnsafeTarArchiveError(
            f"tar member would overwrite destination symlink: {member_name}"
        )


def _validate_virtual_target(
    relative_path: PurePosixPath,
    member_name: str,
    created_paths: dict[PurePosixPath, str],
) -> None:
    current = PurePosixPath()
    for part in relative_path.parts[:-1]:
        current = current / part
        path_kind = created_paths.get(current)
        if path_kind == "symlink":
            raise UnsafeTarArchiveError(
                f"tar member crosses destination symlink: {member_name}"
            )
        if path_kind == "file":
            raise UnsafeTarArchiveError(
                f"tar member crosses destination file: {member_name}"
            )
    if created_paths.get(relative_path) == "symlink":
        raise UnsafeTarArchiveError(
            f"tar member would overwrite destination symlink: {member_name}"
        )


def _record_virtual_path(
    created_paths: dict[PurePosixPath, str],
    relative_path: PurePosixPath,
    path_kind: str,
    member_name: str,
) -> None:
    existing_kind = created_paths.get(relative_path)
    if existing_kind is None:
        created_paths[relative_path] = path_kind
        return
    if path_kind == "dir" and existing_kind == "dir":
        return
    if path_kind == "file" and existing_kind == "file":
        return
    raise UnsafeTarArchiveError(f"tar member target already exists: {member_name}")


def _validate_hardlink_target(
    member: tarfile.TarInfo,
    created_paths: dict[PurePosixPath, str],
) -> None:
    link_name = str(member.linkname or "").strip()
    link_path = PurePosixPath(link_name)
    if (
        not link_name
        or link_path.is_absolute()
        or any(part in {"", ".", ".."} for part in link_path.parts)
    ):
        raise UnsafeTarArchiveError(
            f"unsafe tar hardlink target: {member.name}",
            error_code=ErrorCode.RESTORE_TAR_HARDLINK_UNSAFE,
        )
    if created_paths.get(link_path) != "file":
        raise UnsafeTarArchiveError(
            f"tar hardlink target is unavailable: {member.name}",
            error_code=ErrorCode.RESTORE_TAR_HARDLINK_UNSAFE,
        )


def _safe_mode(member: tarfile.TarInfo, *, default: int) -> int:
    mode = int(member.mode or default) & 0o777
    return mode or default


def _extract_directory(member: tarfile.TarInfo, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, _safe_mode(member, default=0o755))


def _extract_regular_file(
    archive: tarfile.TarFile, member: tarfile.TarInfo, target: Path
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source = archive.extractfile(member)
    if source is None:
        raise UnsafeTarArchiveError(f"tar regular file has no payload: {member.name}")
    with source, target.open("wb") as output:
        shutil.copyfileobj(source, output)
    os.chmod(target, _safe_mode(member, default=0o644))


def _extract_symlink(
    member: tarfile.TarInfo,
    relative_path: PurePosixPath,
    target: Path,
    destination_root: Path,
    guest_symlink_roots: set[PurePosixPath],
) -> None:
    link_name = _validated_symlink_target(
        member, relative_path, target, destination_root, guest_symlink_roots
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise UnsafeTarArchiveError(f"tar symlink target already exists: {member.name}")
    target.symlink_to(link_name)


def _extract_hardlink_as_regular_file(
    member: tarfile.TarInfo,
    _relative_path: PurePosixPath,
    target: Path,
    destination_root: Path,
) -> None:
    link_name = str(member.linkname or "").strip()
    link_path = PurePosixPath(link_name)
    if (
        not link_name
        or link_path.is_absolute()
        or any(part in {"", ".", ".."} for part in link_path.parts)
    ):
        raise UnsafeTarArchiveError(
            f"unsafe tar hardlink target: {member.name}",
            error_code=ErrorCode.RESTORE_TAR_HARDLINK_UNSAFE,
        )
    source = destination_root / Path(*link_path.parts)
    try:
        source.resolve(strict=False).relative_to(destination_root)
    except ValueError as exc:
        raise UnsafeTarArchiveError(
            f"tar hardlink escapes destination: {member.name}",
            error_code=ErrorCode.RESTORE_TAR_HARDLINK_UNSAFE,
        ) from exc
    if not source.is_file() or source.is_symlink():
        raise UnsafeTarArchiveError(
            f"tar hardlink target is unavailable: {member.name}",
            error_code=ErrorCode.RESTORE_TAR_HARDLINK_UNSAFE,
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise UnsafeTarArchiveError(
            f"tar hardlink target already exists: {member.name}",
            error_code=ErrorCode.RESTORE_TAR_HARDLINK_UNSAFE,
        )
    shutil.copy2(source, target, follow_symlinks=False)
    os.chmod(target, _safe_mode(member, default=0o644))
