from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import re
import shutil
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.database import create_configured_async_engine
from app.logger import get_logger
from app.models.entities import Backend, BackendHardeningRun
from app.schemas.backends import BackendCloneIn
from app.services import backend_commands
from app.services.app_healthchecks import probe_backend_health
from app.services.app_runtime import build_app_container_create_command
from app.services.commands import CommandError, CommandResult, run_command
from app.services.container_runtime import container_exists, inspect_container
from app.services.control_events import emit_control_event
from app.services.renderers import container_name, network_name, safe_slug
from app.services.resource_profile import build_resource_profile


PHASE1_RATINGS = ("certain_unsafe", "likely_unsafe", "likely_safe", "uncertain")
PHASE2_RATINGS = ("certain_safe", "certain_unsafe", "uncertain")
ACTIVE_HARDENING_RUN_STATUSES = ("queued", "running")
HARDENING_RESTART_ERROR = "hardening run interrupted by CNC restart"
HARDENING_SETTINGS = (
    "privileged_false",
    "cap_drop_all",
    "cap_add_exact_exceptions",
    "no_new_privileges",
    "seccomp_custom_profile",
    "seccomp_not_unconfined",
    "apparmor_confined",
    "selinux_label_separation",
    "selinux_nested_disabled",
    "default_masked_paths",
    "unmask_all_disabled",
    "read_only_rootfs",
    "read_only_tmpfs_false",
    "explicit_tmpfs_paths",
    "tmpfs_noexec",
    "tmpfs_nosuid",
    "tmpfs_nodev",
    "existing_mounts_ro",
    "existing_mounts_noexec",
    "existing_mounts_nosuid",
    "existing_mounts_nodev",
    "mount_idmap_or_U_only_if_needed",
    "no_extra_host_devices",
    "exact_device_permissions_if_needed",
    "no_device_cgroup_rules_unless_hit",
    "network_none",
    "isolated_network_not_host",
    "no_publish_all",
    "no_hosts_file_if_safe",
    "no_add_host_unless_hit",
    "pid_private",
    "ipc_none",
    "ipc_private",
    "cgroupns_private",
    "uts_private",
    "nonroot_user",
    "userns_auto_or_nomap",
    "no_keep_groups_unless_hit",
    "no_custom_sysctls",
    "pids_limit",
    "memory_limit",
    "nofile_ulimit",
    "shm_size",
    "env_host_false",
)
HARDENING_SETTING_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "privilege",
        "Privilege",
        (
            "privileged_false",
            "cap_drop_all",
            "cap_add_exact_exceptions",
            "no_new_privileges",
        ),
    ),
    (
        "syscall_lsm",
        "Syscall and LSM",
        (
            "seccomp_custom_profile",
            "seccomp_not_unconfined",
            "apparmor_confined",
            "selinux_label_separation",
            "selinux_nested_disabled",
            "default_masked_paths",
            "unmask_all_disabled",
        ),
    ),
    (
        "filesystem",
        "Filesystem",
        (
            "read_only_rootfs",
            "read_only_tmpfs_false",
            "explicit_tmpfs_paths",
            "tmpfs_noexec",
            "tmpfs_nosuid",
            "tmpfs_nodev",
            "existing_mounts_ro",
            "existing_mounts_noexec",
            "existing_mounts_nosuid",
            "existing_mounts_nodev",
            "mount_idmap_or_U_only_if_needed",
        ),
    ),
    (
        "devices",
        "Devices",
        (
            "no_extra_host_devices",
            "exact_device_permissions_if_needed",
            "no_device_cgroup_rules_unless_hit",
        ),
    ),
    (
        "network",
        "Network",
        (
            "network_none",
            "isolated_network_not_host",
            "no_publish_all",
            "no_hosts_file_if_safe",
            "no_add_host_unless_hit",
        ),
    ),
    (
        "namespaces",
        "Namespaces",
        ("pid_private", "ipc_none", "ipc_private", "cgroupns_private", "uts_private"),
    ),
    (
        "user",
        "User",
        ("nonroot_user", "userns_auto_or_nomap", "no_keep_groups_unless_hit"),
    ),
    ("kernel_sysctl", "Kernel Sysctl", ("no_custom_sysctls",)),
    (
        "resource_limits",
        "Resource Limits",
        ("pids_limit", "memory_limit", "nofile_ulimit", "shm_size"),
    ),
    ("environment", "Environment", ("env_host_false",)),
)
_SETTING_GROUPS = {
    setting: {"group": group, "group_label": label}
    for group, label, settings in HARDENING_SETTING_GROUPS
    for setting in settings
}
HARDENING_SETTING_DESCRIPTIONS: dict[str, str] = {
    "privileged_false": "Runs the container without Podman's broad privileged mode, preserving normal isolation boundaries.",
    "cap_drop_all": "Drops all Linux capabilities by default so the container starts from the smallest privilege set.",
    "cap_add_exact_exceptions": "Adds back only a specific Linux capability when runtime evidence proves that exact exception is needed.",
    "no_new_privileges": "Blocks privilege gain during exec, including setuid, setgid, and file-capability escalation paths.",
    "seccomp_custom_profile": "Applies a learned seccomp syscall allowlist generated from observed container syscall use.",
    "seccomp_not_unconfined": "Keeps seccomp filtering enabled instead of allowing the container to run with seccomp disabled.",
    "apparmor_confined": "Runs the container under an AppArmor profile instead of leaving AppArmor confinement off.",
    "selinux_label_separation": "Keeps SELinux container labels active so the container stays separated from unrelated host files.",
    "selinux_nested_disabled": "Prevents the container from managing nested SELinux labels unless that behavior is proven necessary.",
    "default_masked_paths": "Keeps Podman's default masks for sensitive /proc and /sys kernel paths in place.",
    "unmask_all_disabled": "Avoids the broad unmask=ALL escape hatch for sensitive kernel paths.",
    "read_only_rootfs": "Mounts the container root filesystem read-only so writes must go to explicit writable locations.",
    "read_only_tmpfs_false": "Disables Podman's automatic writable temp tmpfs mounts under a read-only root filesystem.",
    "explicit_tmpfs_paths": "Provides only the temp tmpfs paths the app actually needs, with explicit size and mount options.",
    "tmpfs_noexec": "Prevents executing programs or scripts from temporary filesystems.",
    "tmpfs_nosuid": "Ignores setuid and setgid bits on temporary filesystems.",
    "tmpfs_nodev": "Prevents device nodes on temporary filesystems from being used.",
    "existing_mounts_ro": "Makes existing bind or volume mounts read-only when the app does not need to write there.",
    "existing_mounts_noexec": "Prevents executing programs or scripts from existing bind or volume mounts.",
    "existing_mounts_nosuid": "Ignores setuid and setgid bits on existing bind or volume mounts.",
    "existing_mounts_nodev": "Prevents device nodes on existing bind or volume mounts from being used.",
    "mount_idmap_or_U_only_if_needed": "Uses idmapped mounts or :U ownership remapping only when mount ownership would otherwise break access.",
    "no_extra_host_devices": "Runs without extra host devices passed into the container.",
    "exact_device_permissions_if_needed": "Allows a needed device with the least working permission mode: read, read/write, or read/write/mknod.",
    "no_device_cgroup_rules_unless_hit": "Avoids broad device cgroup rules unless an exact device access denial proves they are needed.",
    "network_none": "Runs the container with all networking removed.",
    "isolated_network_not_host": "Uses isolated container networking instead of sharing the host network namespace.",
    "no_publish_all": "Avoids automatically publishing every exposed container port.",
    "no_hosts_file_if_safe": "Prevents Podman from managing /etc/hosts entries when the app does not rely on them.",
    "no_add_host_unless_hit": "Avoids extra host aliases unless a specific hostname lookup proves one is needed.",
    "pid_private": "Uses a private PID namespace so the container cannot see or manage host processes.",
    "ipc_none": "Removes IPC facilities when the app does not need shared memory, semaphores, or message queues.",
    "ipc_private": "Uses private IPC instead of sharing IPC objects with the host or other containers.",
    "cgroupns_private": "Uses a private cgroup namespace instead of exposing the host cgroup view.",
    "uts_private": "Uses a private UTS namespace so the container has its own hostname identity.",
    "nonroot_user": "Runs the container process as a non-root UID and GID.",
    "userns_auto_or_nomap": "Uses user namespace isolation so container root is not the same identity as host root.",
    "no_keep_groups_unless_hit": "Avoids passing host supplementary groups into the container unless group access is proven necessary.",
    "no_custom_sysctls": "Runs without custom container sysctls unless the app proves it depends on one.",
    "pids_limit": "Limits the number of processes and threads the container can create.",
    "memory_limit": "Limits the maximum memory the container can use.",
    "nofile_ulimit": "Limits the number of file descriptors the container can keep open.",
    "shm_size": "Sets the size of /dev/shm for shared-memory workloads.",
    "env_host_false": "Prevents wholesale inheritance of the host environment into the container.",
}

logger = get_logger("app_hardening")


@dataclass(frozen=True)
class StrictSetting:
    name: str
    flags: tuple[str, ...]


STRICT_SETTINGS: tuple[StrictSetting, ...] = (
    StrictSetting("privileged_false", ()),
    StrictSetting("cap_drop_all", ("--cap-drop=ALL",)),
    StrictSetting("cap_add_exact_exceptions", ("--cap-drop=ALL",)),
    StrictSetting("no_new_privileges", ("--security-opt=no-new-privileges",)),
    StrictSetting("seccomp_custom_profile", ()),
    StrictSetting("seccomp_not_unconfined", ()),
    StrictSetting("apparmor_confined", ("--security-opt=apparmor=container-default",)),
    StrictSetting("selinux_label_separation", ()),
    StrictSetting("selinux_nested_disabled", ()),
    StrictSetting("default_masked_paths", ()),
    StrictSetting("unmask_all_disabled", ()),
    StrictSetting("read_only_rootfs", ("--read-only",)),
    StrictSetting("read_only_tmpfs_false", ("--read-only", "--read-only-tmpfs=false")),
    StrictSetting(
        "explicit_tmpfs_paths",
        (
            "--read-only",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=128m",
            "--tmpfs=/run:rw,noexec,nosuid,nodev,size=32m",
        ),
    ),
    StrictSetting("tmpfs_noexec", ("--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=128m",)),
    StrictSetting("tmpfs_nosuid", ("--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=128m",)),
    StrictSetting("tmpfs_nodev", ("--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=128m",)),
    StrictSetting("existing_mounts_ro", ("--read-only",)),
    StrictSetting("existing_mounts_noexec", ("--security-opt=no-new-privileges",)),
    StrictSetting("existing_mounts_nosuid", ("--security-opt=no-new-privileges",)),
    StrictSetting("existing_mounts_nodev", ()),
    StrictSetting("mount_idmap_or_U_only_if_needed", ()),
    StrictSetting("no_extra_host_devices", ()),
    StrictSetting("exact_device_permissions_if_needed", ()),
    StrictSetting("no_device_cgroup_rules_unless_hit", ()),
    StrictSetting("network_none", ("--network=none",)),
    StrictSetting("isolated_network_not_host", ()),
    StrictSetting("no_publish_all", ()),
    StrictSetting("no_hosts_file_if_safe", ("--no-hosts",)),
    StrictSetting("no_add_host_unless_hit", ()),
    StrictSetting("pid_private", ("--pid=private",)),
    StrictSetting("ipc_none", ("--ipc=none",)),
    StrictSetting("ipc_private", ("--ipc=private",)),
    StrictSetting("cgroupns_private", ("--cgroupns=private",)),
    StrictSetting("uts_private", ("--uts=private",)),
    StrictSetting("nonroot_user", ("--user=65532:65532",)),
    StrictSetting("userns_auto_or_nomap", ("--userns=auto",)),
    StrictSetting("no_keep_groups_unless_hit", ()),
    StrictSetting("no_custom_sysctls", ()),
    StrictSetting("pids_limit", ()),
    StrictSetting("memory_limit", ()),
    StrictSetting("nofile_ulimit", ()),
    StrictSetting("shm_size", ()),
    StrictSetting("env_host_false", ()),
)


PHASE1_EVIDENCE_BLURBS: dict[str, str] = {
    "privileged_false": (
        "Phase 1 cannot prove privileged mode directly, so it reads the lower-level signals privileged mode would bypass: "
        "capability checks, host device access, LSM denials, seccomp evidence, and mount writes."
    ),
    "cap_drop_all": (
        "Phase 1 checks whether the runtime recorded Linux capability checks. If no capability tracer is available, this signal "
        "stays incomplete instead of pretending all capabilities are removable."
    ),
    "cap_add_exact_exceptions": (
        "Phase 1 looks for exact capability names from capability tracing. Exact exceptions are only meaningful when the runtime "
        "shows a specific CAP_* dependency."
    ),
    "no_new_privileges": (
        "Phase 1 looks for setuid, setgid, file-capability, and privilege-helper execution patterns that would rely on gaining "
        "privilege during exec."
    ),
    "seccomp_custom_profile": (
        "Phase 1 checks whether syscall tracing produced a learned seccomp profile. Without a generated seccomp.json profile, "
        "there is no syscall allowlist to test yet."
    ),
    "seccomp_not_unconfined": (
        "Phase 1 treats seccomp confinement as the default because monitoring cannot prove a need for the unconfined escape hatch."
    ),
    "apparmor_confined": (
        "Phase 1 can only see AppArmor trouble when confinement is already active and emitting denials. Otherwise this needs "
        "Phase 2 enforcement."
    ),
    "selinux_label_separation": (
        "Phase 1 checks existing SELinux AVCs and label-related permission errors when SELinux is enforcing for the container."
    ),
    "selinux_nested_disabled": (
        "Phase 1 watches for SELinux label mutation behavior such as setxattr or chcon-style operations from inside the container."
    ),
    "default_masked_paths": (
        "Phase 1 watches for access to sensitive masked kernel paths such as /proc/kcore, /proc/keys, and /sys/firmware."
    ),
    "unmask_all_disabled": (
        "Phase 1 does not accept broad unmasking as evidence-based. It only looks for exact masked paths that the app attempted "
        "to access."
    ),
    "read_only_rootfs": (
        "Phase 1 compares Podman diff output against known mounts and temp paths to find writes that would land on the root "
        "filesystem."
    ),
    "read_only_tmpfs_false": (
        "Phase 1 checks whether the app writes to default temp locations such as /tmp, /run, /var/tmp, or /dev/shm."
    ),
    "explicit_tmpfs_paths": (
        "Phase 1 identifies which ephemeral paths are actually written and estimates sizing signals before recommending explicit "
        "tmpfs mounts."
    ),
    "tmpfs_noexec": (
        "Phase 1 looks for execution from temp paths. Any binary or script launched from /tmp, /run, /var/tmp, or /dev/shm would "
        "conflict with noexec."
    ),
    "tmpfs_nosuid": (
        "Phase 1 looks for setuid, setgid, or file-capability execution from temp paths before treating nosuid as risky."
    ),
    "tmpfs_nodev": (
        "Phase 1 checks whether non-default device nodes are opened or used under temp paths before treating nodev as risky."
    ),
    "existing_mounts_ro": (
        "Phase 1 checks mounted paths for writes using Podman diff, file-access traces, and permission-related app logs."
    ),
    "existing_mounts_noexec": (
        "Phase 1 checks whether executables are launched from existing bind or volume mounts before recommending noexec."
    ),
    "existing_mounts_nosuid": (
        "Phase 1 checks mounted paths for setuid, setgid, and file-capability execution before recommending nosuid."
    ),
    "existing_mounts_nodev": (
        "Phase 1 checks mounted paths for device-node access and ioctl-style device use before recommending nodev."
    ),
    "mount_idmap_or_U_only_if_needed": (
        "Phase 1 looks for UID, GID, ownership, chmod, chown, and EACCES signals that would justify an idmap or :U mount fix."
    ),
    "no_extra_host_devices": (
        "Phase 1 watches for opens and ioctls against non-default /dev paths. Default console, tty, random, null, and shm devices "
        "do not count as extra host-device needs."
    ),
    "exact_device_permissions_if_needed": (
        "Phase 1 looks at device access patterns so any exception can be narrowed to the least required mode: read, read/write, "
        "or read/write/mknod."
    ),
    "no_device_cgroup_rules_unless_hit": (
        "Phase 1 records device opens and device-cgroup denial signals. Broad device cgroup rules are not recommended without a "
        "specific hit."
    ),
    "network_none": (
        "Phase 1 checks for DNS lookups, outbound connects, listening sockets, published-port dependency, and network-related app "
        "logs that would break under network none."
    ),
    "isolated_network_not_host": (
        "Phase 1 checks for localhost, 127.0.0.1, ::1, or host-socket assumptions that would require host networking."
    ),
    "no_publish_all": (
        "Phase 1 distinguishes explicit listeners from the policy choice to publish every exposed port. It cannot infer a need for "
        "random auto-published ports from image metadata alone."
    ),
    "no_hosts_file_if_safe": (
        "Phase 1 weakly watches /etc/hosts reads and special hostname lookups. Host-file management usually needs enforcement to "
        "classify."
    ),
    "no_add_host_unless_hit": (
        "Phase 1 watches failed or special hostname lookups so extra host aliases are recommended only when the app actually uses "
        "one."
    ),
    "pid_private": (
        "Phase 1 watches for host PID and /proc process inspection behavior that would break inside a private PID namespace."
    ),
    "ipc_none": (
        "Phase 1 checks /dev/shm usage and SysV/POSIX shared-memory, semaphore, and message-queue signals before removing IPC."
    ),
    "ipc_private": (
        "Phase 1 looks for shared IPC assumptions between the app and host or other containers before treating private IPC as risky."
    ),
    "cgroupns_private": (
        "Phase 1 watches /sys/fs/cgroup reads, writes, and cgroup-control behavior that would require the host cgroup namespace."
    ),
    "uts_private": (
        "Phase 1 watches hostname reads, hostname writes, and sethostname behavior that would depend on the host UTS namespace."
    ),
    "nonroot_user": (
        "Phase 1 looks for root-only behavior such as privileged binds, ownership changes, capability checks, and permission errors."
    ),
    "userns_auto_or_nomap": (
        "Phase 1 checks mounted volumes and devices for UID/GID mismatch, ownership errors, and permission failures under user "
        "namespace isolation."
    ),
    "no_keep_groups_unless_hit": (
        "Phase 1 looks for group-only access to mounted volumes or devices before allowing host supplementary groups through."
    ),
    "no_custom_sysctls": (
        "Phase 1 watches /proc/sys access and sysctl-related startup or runtime messages before preserving custom sysctls."
    ),
    "pids_limit": (
        "Phase 1 samples process and thread count from Podman stats to choose a peak-plus-margin process limit."
    ),
    "memory_limit": (
        "Phase 1 samples memory usage and OOM-related signals to choose a peak-plus-margin memory limit."
    ),
    "nofile_ulimit": (
        "Phase 1 samples open file-descriptor counts from host process fd tables to choose a peak-plus-margin nofile limit."
    ),
    "shm_size": (
        "Phase 1 samples /dev/shm usage and shared-memory signals to choose a peak-plus-margin shm size."
    ),
    "env_host_false": (
        "Phase 1 cannot infer host environment intent from runtime observation, so this setting usually needs Phase 2."
    ),
}


PHASE2_EVIDENCE_BLURBS: dict[str, str] = {
    "privileged_false": (
        "Phase 2 ran the app clone without Podman's privileged escape hatch and watched whether any narrower isolation failure "
        "explained the result first."
    ),
    "cap_drop_all": (
        "Phase 2 created the clone with --cap-drop=ALL and watched create, start, health, and logs for EPERM, operation-not-permitted, "
        "permission-denied, or capability-specific failures."
    ),
    "cap_add_exact_exceptions": (
        "Phase 2 kept the clone on the cap-drop path and only treats a capability exception as useful when the evidence points to "
        "an exact CAP_* requirement."
    ),
    "no_new_privileges": (
        "Phase 2 enabled no-new-privileges and watched for blocked setuid, setgid, file-capability, and privilege-helper execution."
    ),
    "seccomp_custom_profile": (
        "Phase 2 can enforce a learned seccomp profile only when Phase 1 produced seccomp.json. Without that profile, the test has "
        "no syscall allowlist to apply."
    ),
    "seccomp_not_unconfined": (
        "Phase 2 kept seccomp confinement enabled and watched for seccomp audit records, SIGSYS, bad-system-call errors, EPERM, and "
        "startup or health failures."
    ),
    "apparmor_confined": (
        "Phase 2 applied the confined AppArmor profile and watched for AppArmor DENIED records, audit denials, permission failures, "
        "and healthcheck regression."
    ),
    "selinux_label_separation": (
        "Phase 2 kept SELinux label separation enabled and watched for AVC denials, label-related permission errors, and healthcheck "
        "regression."
    ),
    "selinux_nested_disabled": (
        "Phase 2 ran without label=nested and watched for failed label mutation, AVC denials, and label-related permission failures."
    ),
    "default_masked_paths": (
        "Phase 2 kept Podman's default sensitive /proc and /sys masks in place and watched for ENOENT, EACCES, EPERM, or app logs "
        "naming masked paths."
    ),
    "unmask_all_disabled": (
        "Phase 2 kept unmask=ALL disabled and watched whether the app required broad access to masked kernel paths."
    ),
    "read_only_rootfs": (
        "Phase 2 created the clone with --read-only and watched for EROFS, read-only filesystem messages, write failures, and "
        "healthcheck regression."
    ),
    "read_only_tmpfs_false": (
        "Phase 2 combined --read-only with --read-only-tmpfs=false and watched for missing writable temp paths, EROFS, EACCES, and "
        "startup or health failures."
    ),
    "explicit_tmpfs_paths": (
        "Phase 2 supplied explicit tmpfs mounts for observed temp paths and watched for ENOSPC, EROFS, EACCES, missing runtime files, "
        "and healthcheck regression."
    ),
    "tmpfs_noexec": (
        "Phase 2 mounted temp storage with noexec and watched for EACCES or execution failures from /tmp, /run, /var/tmp, or /dev/shm."
    ),
    "tmpfs_nosuid": (
        "Phase 2 mounted temp storage with nosuid and watched for failed setuid, setgid, or file-capability behavior from temp paths."
    ),
    "tmpfs_nodev": (
        "Phase 2 mounted temp storage with nodev and watched for device-node open, ioctl, or permission failures under temp paths."
    ),
    "existing_mounts_ro": (
        "Phase 2 tightened existing mounts toward read-only behavior and watched for EROFS, EACCES, write failures, and path-specific "
        "app errors."
    ),
    "existing_mounts_noexec": (
        "Phase 2 tested noexec behavior on mounted content and watched for EACCES or command execution failures from those paths."
    ),
    "existing_mounts_nosuid": (
        "Phase 2 tested nosuid behavior on mounted content and watched for failed setuid, setgid, or file-capability behavior."
    ),
    "existing_mounts_nodev": (
        "Phase 2 tested nodev behavior on mounted content and watched for device-node open, ioctl, or permission failures."
    ),
    "mount_idmap_or_U_only_if_needed": (
        "Phase 2 tested the clone without broad ownership remapping first and watched for UID/GID, chmod, chown, ownership, and "
        "EACCES failures that would justify idmap or :U."
    ),
    "no_extra_host_devices": (
        "Phase 2 ran without extra host devices and watched for missing /dev paths, EACCES, EPERM, ioctl failures, and healthcheck "
        "regression."
    ),
    "exact_device_permissions_if_needed": (
        "Phase 2 treats device exceptions as least-privilege candidates and watches whether read-only, read/write, or mknod permission "
        "is the first mode that clears the device hit."
    ),
    "no_device_cgroup_rules_unless_hit": (
        "Phase 2 ran without broad device cgroup rules and watched for device-cgroup denials, missing devices, and permission failures."
    ),
    "network_none": (
        "Phase 2 created the clone with --network=none and watched for DNS failures, ENETUNREACH, connection errors, listener or "
        "reachability breakage, and healthcheck failure."
    ),
    "isolated_network_not_host": (
        "Phase 2 kept the clone on isolated networking instead of host networking and watched for localhost, host-socket, DNS, and "
        "connectivity assumptions."
    ),
    "no_publish_all": (
        "Phase 2 ran without --publish-all and watched whether app reachability depended on automatically published exposed ports."
    ),
    "no_hosts_file_if_safe": (
        "Phase 2 enabled --no-hosts and watched for hostname resolution failures or app logs tied to missing Podman-managed hosts "
        "entries."
    ),
    "no_add_host_unless_hit": (
        "Phase 2 ran without extra --add-host aliases and watched for lookup failures tied to a specific host alias."
    ),
    "pid_private": (
        "Phase 2 enforced --pid=private and watched for missing host PIDs, /proc process inspection failures, process-control errors, "
        "and healthcheck regression."
    ),
    "ipc_none": (
        "Phase 2 enforced --ipc=none and watched for /dev/shm, shared-memory, semaphore, message-queue, SIGBUS, and allocation failures."
    ),
    "ipc_private": (
        "Phase 2 enforced --ipc=private and watched for shared IPC dependencies that only work with host or shared IPC."
    ),
    "cgroupns_private": (
        "Phase 2 enforced --cgroupns=private and watched for missing cgroup paths, cgroup-control failures, read-only filesystem "
        "errors, and permission denials."
    ),
    "uts_private": (
        "Phase 2 enforced --uts=private and watched for hostname identity assumptions, sethostname failures, and UTS permission errors."
    ),
    "nonroot_user": (
        "Phase 2 ran the clone as UID/GID 65532 and watched for bind permission errors, chmod/chown failures, ownership problems, "
        "EACCES, and startup failure."
    ),
    "userns_auto_or_nomap": (
        "Phase 2 enabled user namespace isolation and watched for mounted volume ownership problems, device access failures, UID/GID "
        "mismatches, and permission denials."
    ),
    "no_keep_groups_unless_hit": (
        "Phase 2 omitted host supplementary groups and watched for group-only volume or device access failures."
    ),
    "no_custom_sysctls": (
        "Phase 2 ran without custom sysctls and watched for /proc/sys access failures, sysctl errors, startup failures, and healthcheck "
        "regression."
    ),
    "pids_limit": (
        "Phase 2 enforced the Phase 1-derived process limit and watched for fork/thread failures, pids.max events, EAGAIN, and "
        "pthread_create errors."
    ),
    "memory_limit": (
        "Phase 2 enforced the Phase 1-derived memory limit and watched for OOM kills, exit 137, memory cgroup events, ENOMEM, and "
        "healthcheck regression."
    ),
    "nofile_ulimit": (
        "Phase 2 enforced the Phase 1-derived nofile limit and watched for EMFILE or too-many-open-files errors."
    ),
    "shm_size": (
        "Phase 2 enforced the Phase 1-derived /dev/shm size and watched for ENOSPC, SIGBUS, shm allocation failures, and app crashes "
        "tied to shared memory."
    ),
    "env_host_false": (
        "Phase 2 ran without host environment inheritance and watched for missing environment variables, required config failures, "
        "and startup or healthcheck regression."
    ),
}


async def create_hardening_run(
    session: AsyncSession,
    backend: Backend,
    settings: Settings,
    *,
    phase: str,
    details: dict[str, Any] | None = None,
    ratings: dict[str, Any] | None = None,
) -> BackendHardeningRun:
    run = BackendHardeningRun(
        backend_id=backend.id,
        phase=phase,
        status="queued",
        ratings_json=_json_dumps(ratings or {}),
        details_json=_json_dumps(details or {}),
    )
    session.add(run)
    await session.flush()
    run.evidence_dir = str(_run_evidence_dir(settings, backend.name, run.id, phase))
    await session.commit()
    await session.refresh(run)
    logger.info(
        "app_hardening.run.queued",
        backend_id=backend.id,
        backend_name=backend.name,
        phase=phase,
        run_id=run.id,
        evidence_dir=run.evidence_dir,
    )
    return run


async def active_hardening_run(
    session: AsyncSession, backend_id: int, phase: str
) -> BackendHardeningRun | None:
    return (
        await session.execute(
            select(BackendHardeningRun)
            .where(
                BackendHardeningRun.backend_id == backend_id,
                BackendHardeningRun.phase == phase,
                BackendHardeningRun.status.in_(ACTIVE_HARDENING_RUN_STATUSES),
            )
            .order_by(
                BackendHardeningRun.started_at.desc(), BackendHardeningRun.id.desc()
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def latest_resumable_phase2_run(
    session: AsyncSession, backend_id: int
) -> BackendHardeningRun | None:
    run = (
        await session.execute(
            select(BackendHardeningRun)
            .where(
                BackendHardeningRun.backend_id == backend_id,
                BackendHardeningRun.phase == "phase2",
            )
            .order_by(
                BackendHardeningRun.started_at.desc(), BackendHardeningRun.id.desc()
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is None or not _phase2_run_resumable(run):
        return None
    return run


async def create_resumed_phase2_run(
    session: AsyncSession,
    backend: Backend,
    settings: Settings,
    source_run: BackendHardeningRun,
) -> BackendHardeningRun:
    if not _phase2_run_resumable(source_run):
        raise ValueError("no interrupted Phase 2 run to resume")
    ratings = _phase2_seed_ratings(source_run)
    details = _phase2_seed_details(source_run, ratings)
    return await create_hardening_run(
        session,
        backend,
        settings,
        phase="phase2",
        details=details,
        ratings=ratings,
    )


async def latest_hardening_summary(
    session: AsyncSession, backend_id: int
) -> dict[str, Any]:
    runs = (
        (
            await session.execute(
                select(BackendHardeningRun)
                .where(BackendHardeningRun.backend_id == backend_id)
                .order_by(
                    BackendHardeningRun.started_at.desc(), BackendHardeningRun.id.desc()
                )
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    latest_phase1 = next((run for run in runs if run.phase == "phase1"), None)
    latest_phase2 = next((run for run in runs if run.phase == "phase2"), None)
    return {
        "phase1": _run_payload(latest_phase1),
        "phase2": _run_payload(latest_phase2),
        "features": _feature_rows(latest_phase1, latest_phase2),
    }


async def cancel_latest_hardening_run(
    session: AsyncSession, backend_id: int, phase: str
) -> BackendHardeningRun | None:
    run = (
        await session.execute(
            select(BackendHardeningRun)
            .where(
                BackendHardeningRun.backend_id == backend_id,
                BackendHardeningRun.phase == phase,
                BackendHardeningRun.status.in_(("queued", "running")),
            )
            .order_by(
                BackendHardeningRun.started_at.desc(), BackendHardeningRun.id.desc()
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is None:
        return None
    run.status = "cancelled"
    run.finished_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(run)
    logger.info(
        "app_hardening.run.cancelled", backend_id=backend_id, phase=phase, run_id=run.id
    )
    return run


async def fail_interrupted_hardening_runs(settings: Settings) -> int:
    async with _session(settings) as session:
        runs = (
            (
                await session.execute(
                    select(BackendHardeningRun)
                    .where(
                        BackendHardeningRun.status.in_(ACTIVE_HARDENING_RUN_STATUSES)
                    )
                    .order_by(BackendHardeningRun.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if not runs:
            return 0

        finished_at = datetime.now(UTC)
        clone_backends: dict[int, Backend] = {}
        for run in runs:
            run.status = "failed"
            run.error = HARDENING_RESTART_ERROR
            run.finished_at = finished_at
            details = _interrupted_hardening_details(run)
            run.details_json = _json_dumps(details)
            if run.phase == "phase2" and run.backend_id is not None:
                source = await session.get(Backend, run.backend_id)
                if source is not None:
                    for clone in await _phase2_clone_backends_for_run(
                        session, source.name, run.id
                    ):
                        clone_backends[clone.id] = clone

        await session.commit()

    cleaned_clone_ids: set[int] = set()
    for clone in clone_backends.values():
        cleanup = await asyncio.to_thread(_cleanup_phase2_clone, clone, settings)
        if bool(cleanup.get("ok")):
            cleaned_clone_ids.add(clone.id)
        else:
            logger.warning(
                "app_hardening.interrupted_clone_cleanup_incomplete",
                clone_id=clone.id,
                clone_name=clone.name,
                cleanup=cleanup,
            )

    if cleaned_clone_ids:
        async with _session(settings) as session:
            for clone_id in cleaned_clone_ids:
                db_clone = await session.get(Backend, clone_id)
                if db_clone is not None:
                    await session.delete(db_clone)
            await session.commit()

    logger.warning(
        "app_hardening.interrupted_marked_failed",
        count=len(runs),
        clone_count=len(clone_backends),
    )
    return len(runs)


async def request_phase1_monitor_stop(
    session: AsyncSession, backend_id: int
) -> BackendHardeningRun | None:
    run = (
        await session.execute(
            select(BackendHardeningRun)
            .where(
                BackendHardeningRun.backend_id == backend_id,
                BackendHardeningRun.phase == "phase1",
                BackendHardeningRun.status.in_(("queued", "running")),
            )
            .order_by(
                BackendHardeningRun.started_at.desc(), BackendHardeningRun.id.desc()
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is None:
        return None
    if run.status == "queued":
        run.status = "cancelled"
        run.finished_at = datetime.now(UTC)
    else:
        details = _json_loads_dict(run.details_json)
        details["stop_requested"] = True
        details["message"] = "Phase 1 monitor stopping; rating collected evidence."
        run.details_json = _json_dumps(details)
    await session.commit()
    await session.refresh(run)
    logger.info(
        "app_hardening.phase1.stop_requested",
        backend_id=backend_id,
        run_id=run.id,
        status=run.status,
    )
    return run


async def run_phase1_monitor(
    settings: Settings, run_id: int, backend_id: int, duration_sec: int
) -> None:
    await asyncio.to_thread(
        _run_phase1_monitor_sync, settings, run_id, backend_id, duration_sec
    )


async def run_phase2_test(settings: Settings, run_id: int, backend_id: int) -> None:
    await asyncio.to_thread(_run_phase2_test_sync, settings, run_id, backend_id)


def _run_phase1_monitor_sync(
    settings: Settings, run_id: int, backend_id: int, duration_sec: int
) -> None:
    asyncio.run(_run_phase1_monitor_async(settings, run_id, backend_id, duration_sec))


def _run_phase2_test_sync(settings: Settings, run_id: int, backend_id: int) -> None:
    asyncio.run(_run_phase2_test_async(settings, run_id, backend_id))


async def _run_phase1_monitor_async(
    settings: Settings, run_id: int, backend_id: int, duration_sec: int
) -> None:
    async with _session(settings) as session:
        run = await session.get(BackendHardeningRun, run_id)
        backend = await _load_backend(session, backend_id)
        if run is None or backend is None:
            logger.warning(
                "app_hardening.phase1.missing_run_or_backend",
                backend_id=backend_id,
                run_id=run_id,
            )
            return
        if run.status == "cancelled":
            logger.info(
                "app_hardening.phase1.start_skipped_cancelled",
                backend_id=backend_id,
                run_id=run_id,
            )
            return
        if backend.kind != "app":
            logger.warning(
                "app_hardening.phase1.unsupported_backend",
                backend_id=backend_id,
                run_id=run_id,
                kind=backend.kind,
            )
            await _finish_run(
                session,
                run,
                "failed",
                error="hardening is only available for app outputs",
            )
            return
        evidence_dir = _ensure_evidence_dir(run)
        await _update_run(
            session,
            run,
            "running",
            details={
                "message": "Phase 1 monitor running.",
                "duration_sec": duration_sec,
            },
        )
        logger.info(
            "app_hardening.phase1.started",
            backend_id=backend_id,
            backend_name=backend.name,
            run_id=run_id,
            container=container_name(backend.name),
            duration_sec=duration_sec,
            evidence_dir=str(evidence_dir),
        )

    container = container_name(backend.name)
    try:
        if not container_exists(
            container, timeout_sec=settings.command_timeout_status_sec
        ):
            raise RuntimeError(f"container not found: {container}")
        logger.info(
            "app_hardening.phase1.container_found",
            backend_id=backend_id,
            run_id=run_id,
            container=container,
        )
        await asyncio.to_thread(
            _collect_phase1_evidence,
            container,
            evidence_dir,
            duration_sec,
            settings,
            run_id,
        )
        logger.info(
            "app_hardening.phase1.evidence_collected",
            backend_id=backend_id,
            run_id=run_id,
            evidence_dir=str(evidence_dir),
        )
        ratings, details = await asyncio.to_thread(_rate_phase1, evidence_dir)
        rating_counts = {
            rating: list(ratings.values()).count(rating) for rating in PHASE1_RATINGS
        }
        logger.info(
            "app_hardening.phase1.rated",
            backend_id=backend_id,
            run_id=run_id,
            evidence_dir=str(evidence_dir),
            **rating_counts,
        )
        async with _session(settings) as session:
            run = await session.get(BackendHardeningRun, run_id)
            if run is None:
                logger.warning(
                    "app_hardening.phase1.run_missing_before_finish",
                    backend_id=backend_id,
                    run_id=run_id,
                )
                return
            if run.status == "cancelled":
                logger.info(
                    "app_hardening.phase1.finish_skipped_cancelled",
                    backend_id=backend_id,
                    run_id=run_id,
                )
                return
            run.ratings_json = _json_dumps(ratings)
            existing_details = _json_loads_dict(run.details_json)
            final_details = {**existing_details, **details}
            if existing_details.get("stop_requested"):
                final_details["message"] = "Phase 1 monitor stopped early."
                final_details["stopped_early"] = True
            await _finish_run(session, run, "success", details=final_details)
            await _emit_hardening_run_event(
                settings,
                backend_id=backend.id,
                backend_name=backend.name,
                run=run,
                ratings=ratings,
                details=final_details,
            )
            logger.info(
                "app_hardening.phase1.finished",
                backend_id=backend_id,
                run_id=run_id,
                status="success",
            )
    except Exception as exc:
        logger.warning(
            "app_hardening.phase1_failed",
            backend_id=backend_id,
            run_id=run_id,
            error=str(exc),
        )
        async with _session(settings) as session:
            run = await session.get(BackendHardeningRun, run_id)
            if run is not None and run.status != "cancelled":
                await _finish_run(session, run, "failed", error=str(exc))


async def _run_phase2_test_async(
    settings: Settings, run_id: int, backend_id: int
) -> None:
    ratings: dict[str, str] = {}
    details: dict[str, Any] = {"tested": [], "skipped": []}
    completed_names: set[str] = set()
    total_settings = len(STRICT_SETTINGS)
    completed_settings = 0

    async with _session(settings) as session:
        run = await session.get(BackendHardeningRun, run_id)
        backend = await _load_backend(session, backend_id)
        if run is None or backend is None:
            logger.warning(
                "app_hardening.phase2.missing_run_or_backend",
                backend_id=backend_id,
                run_id=run_id,
            )
            return
        if backend.kind != "app":
            logger.warning(
                "app_hardening.phase2.unsupported_backend",
                backend_id=backend_id,
                run_id=run_id,
                kind=backend.kind,
            )
            await _finish_run(
                session,
                run,
                "failed",
                error="hardening is only available for app outputs",
            )
            return
        evidence_dir = _ensure_evidence_dir(run)
        phase1 = await _latest_successful_phase1(session, backend_id)
        if phase1 is None:
            logger.warning(
                "app_hardening.phase2.phase1_missing",
                backend_id=backend_id,
                run_id=run_id,
            )
            await _finish_run(
                session, run, "failed", error="run Phase 1 before Phase 2"
            )
            return
        phase1_ratings = _json_loads_dict(phase1.ratings_json)
        phase1_values = (
            _json_loads_dict(_read(Path(phase1.evidence_dir) / "values.json"))
            if phase1.evidence_dir
            else {}
        )
        (evidence_dir / "phase1-values.json").write_text(
            _json_dumps(phase1_values), encoding="utf-8"
        )
        ratings = _phase2_seed_ratings(run)
        details = _phase2_runtime_details(_json_loads_dict(run.details_json), ratings)
        completed_names = _phase2_completed_setting_names(ratings, details)
        completed_settings = sum(
            1 for setting in STRICT_SETTINGS if setting.name in completed_names
        )
        await _update_run(
            session,
            run,
            "running",
            details=_phase2_progress_details(
                details,
                total=total_settings,
                completed=completed_settings,
                state="queued",
                substate="waiting",
            ),
        )
        logger.info(
            "app_hardening.phase2.started",
            backend_id=backend_id,
            backend_name=backend.name,
            run_id=run_id,
            phase1_run_id=phase1.id,
            evidence_dir=str(evidence_dir),
            test_seconds=settings.hardening_phase2_test_sec,
        )

    clone_backend: Backend | None = None
    if completed_settings >= total_settings:
        async with _session(settings) as session:
            run = await session.get(BackendHardeningRun, run_id)
            if run is not None and run.status != "cancelled":
                run.ratings_json = _json_dumps(ratings)
                await _finish_run(
                    session,
                    run,
                    "success",
                    details=_phase2_progress_details(
                        details,
                        total=total_settings,
                        completed=total_settings,
                        state="finished",
                        substate="complete",
                    ),
                )
                await _emit_hardening_run_event(
                    settings,
                    backend_id=backend.id,
                    backend_name=backend.name,
                    run=run,
                    ratings=ratings,
                    details=_json_loads_dict(run.details_json),
                )
        return

    try:
        async with _session(settings) as session:
            source = await _load_backend(session, backend_id)
            if source is None:
                raise RuntimeError("source output not found")
            clone_name = await _phase2_clone_name(session, source.name, run_id)
            target_port = await _next_phase2_port(session, source.port)
            logger.info(
                "app_hardening.phase2.clone.requested",
                backend_id=backend_id,
                source_name=source.name,
                run_id=run_id,
                clone_name=clone_name,
                clone_port=target_port,
            )
            await _record_phase2_progress(
                settings,
                run_id,
                ratings,
                details,
                total=total_settings,
                completed=completed_settings,
                state="cloning",
                substate="copying_output",
            )
            result = await backend_commands.clone_backend(
                session,
                source.id,
                BackendCloneIn(name=clone_name, port=target_port),
                settings,
            )
            clone_backend = result["backend"]
            if not isinstance(clone_backend, Backend):
                raise RuntimeError("clone did not return backend")
            logger.info(
                "app_hardening.phase2.clone.created",
                backend_id=backend_id,
                run_id=run_id,
                clone_backend_id=clone_backend.id,
                clone_name=clone_backend.name,
                clone_port=clone_backend.port,
            )

        await _record_phase2_progress(
            settings,
            run_id,
            ratings,
            details,
            total=total_settings,
            completed=completed_settings,
            state="network",
            substate="creating_network",
        )
        _create_test_network(clone_backend.name, evidence_dir, settings)
        for setting in STRICT_SETTINGS:
            if await _run_was_cancelled(settings, run_id):
                logger.info(
                    "app_hardening.phase2.cancel_seen",
                    backend_id=backend_id,
                    run_id=run_id,
                )
                raise _RunCancelled
            if setting.name in completed_names:
                logger.info(
                    "app_hardening.phase2.setting.resume_skip",
                    backend_id=backend_id,
                    run_id=run_id,
                    setting=setting.name,
                )
                continue
            if phase1_ratings.get(setting.name) == "certain_unsafe":
                await _record_phase2_progress(
                    settings,
                    run_id,
                    ratings,
                    details,
                    total=total_settings,
                    completed=completed_settings,
                    state="skipping",
                    substate="phase1_certain_unsafe",
                    current_setting=setting.name,
                )
                ratings[setting.name] = "certain_unsafe"
                details["skipped"].append(
                    {"setting": setting.name, "reason": "phase1_certain_unsafe"}
                )
                completed_names.add(setting.name)
                completed_settings += 1
                await _record_phase2_progress(
                    settings,
                    run_id,
                    ratings,
                    details,
                    total=total_settings,
                    completed=completed_settings,
                    state="skipped",
                    substate="complete",
                    current_setting=setting.name,
                )
                logger.info(
                    "app_hardening.phase2.setting.skipped",
                    backend_id=backend_id,
                    run_id=run_id,
                    setting=setting.name,
                    reason="phase1_certain_unsafe",
                )
                continue
            logger.info(
                "app_hardening.phase2.setting.started",
                backend_id=backend_id,
                run_id=run_id,
                clone_name=clone_backend.name,
                setting=setting.name,
                flags=list(setting.flags),
            )
            await _record_phase2_progress(
                settings,
                run_id,
                ratings,
                details,
                total=total_settings,
                completed=completed_settings,
                state="testing",
                substate="queued",
                current_setting=setting.name,
            )

            def publish_substate(substate: str) -> None:
                _record_phase2_progress_sync(
                    settings,
                    run_id,
                    ratings,
                    details,
                    total=total_settings,
                    completed=completed_settings,
                    state="testing",
                    substate=substate,
                    current_setting=setting.name,
                )

            rating = await asyncio.to_thread(
                _test_strict_setting,
                clone_backend,
                setting,
                evidence_dir,
                settings,
                publish_substate,
            )
            ratings[setting.name] = rating
            details["tested"].append({"setting": setting.name, "rating": rating})
            completed_names.add(setting.name)
            completed_settings += 1
            logger.info(
                "app_hardening.phase2.setting.finished",
                backend_id=backend_id,
                run_id=run_id,
                clone_name=clone_backend.name,
                setting=setting.name,
                rating=rating,
            )
            async with _session(settings) as session:
                run = await session.get(BackendHardeningRun, run_id)
                if run is not None and run.status != "cancelled":
                    run.ratings_json = _json_dumps(ratings)
                    run.details_json = _json_dumps(
                        _phase2_progress_details(
                            details,
                            total=total_settings,
                            completed=completed_settings,
                            state="tested",
                            substate="complete",
                            current_setting=setting.name,
                        )
                    )
                    await session.commit()

        async with _session(settings) as session:
            run = await session.get(BackendHardeningRun, run_id)
            if run is not None and run.status != "cancelled":
                run.ratings_json = _json_dumps(ratings)
                await _finish_run(
                    session,
                    run,
                    "success",
                    details=_phase2_progress_details(
                        details,
                        total=total_settings,
                        completed=total_settings,
                        state="finished",
                        substate="complete",
                    ),
                )
                rating_counts = {
                    rating: list(ratings.values()).count(rating)
                    for rating in PHASE2_RATINGS
                }
                await _emit_hardening_run_event(
                    settings,
                    backend_id=backend_id,
                    backend_name=backend.name,
                    run=run,
                    ratings=ratings,
                    details=_json_loads_dict(run.details_json),
                )
                logger.info(
                    "app_hardening.phase2.finished",
                    backend_id=backend_id,
                    run_id=run_id,
                    status="success",
                    **rating_counts,
                )
    except _RunCancelled:
        async with _session(settings) as session:
            run = await session.get(BackendHardeningRun, run_id)
            if run is not None:
                await _finish_run(
                    session,
                    run,
                    "cancelled",
                    details=_phase2_progress_details(
                        details,
                        total=total_settings,
                        completed=completed_settings,
                        state="cancelled",
                        substate="stopped",
                    ),
                )
                logger.info(
                    "app_hardening.phase2.finished",
                    backend_id=backend_id,
                    run_id=run_id,
                    status="cancelled",
                )
    except Exception as exc:
        logger.warning(
            "app_hardening.phase2_failed",
            backend_id=backend_id,
            run_id=run_id,
            error=str(exc),
        )
        async with _session(settings) as session:
            run = await session.get(BackendHardeningRun, run_id)
            if run is not None and run.status != "cancelled":
                run.ratings_json = _json_dumps(ratings)
                await _finish_run(
                    session,
                    run,
                    "failed",
                    error=str(exc),
                    details=_phase2_progress_details(
                        details,
                        total=total_settings,
                        completed=completed_settings,
                        state="failed",
                        substate="error",
                    ),
                )
    finally:
        if clone_backend is not None:
            logger.info(
                "app_hardening.phase2.cleanup.started",
                backend_id=backend_id,
                run_id=run_id,
                clone_backend_id=clone_backend.id,
                clone_name=clone_backend.name,
            )
            await _record_phase2_progress(
                settings,
                run_id,
                ratings,
                details,
                total=total_settings,
                completed=completed_settings,
                state="cleanup",
                substate="removing_clone",
            )
            cleanup = _cleanup_phase2_clone(clone_backend, settings)
            if bool(cleanup.get("ok")):
                async with _session(settings) as session:
                    db_clone = (
                        await session.execute(
                            select(Backend).where(Backend.id == clone_backend.id)
                        )
                    ).scalar_one_or_none()
                    if db_clone is not None:
                        await session.delete(db_clone)
                        await session.commit()
                        logger.info(
                            "app_hardening.phase2.cleanup.clone_row_deleted",
                            backend_id=backend_id,
                            run_id=run_id,
                            clone_backend_id=clone_backend.id,
                            clone_name=clone_backend.name,
                        )
            else:
                logger.warning(
                    "app_hardening.phase2.cleanup.clone_row_preserved",
                    backend_id=backend_id,
                    run_id=run_id,
                    clone_backend_id=clone_backend.id,
                    clone_name=clone_backend.name,
                    cleanup=cleanup,
                )
            logger.info(
                "app_hardening.phase2.cleanup.finished",
                backend_id=backend_id,
                run_id=run_id,
                clone_name=clone_backend.name,
            )


def _collect_phase1_evidence(
    container: str,
    evidence_dir: Path,
    duration_sec: int,
    settings: Settings,
    run_id: int,
) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "app_hardening.phase1.collect.started",
        run_id=run_id,
        container=container,
        duration_sec=duration_sec,
        evidence_dir=str(evidence_dir),
    )
    logger.info(
        "app_hardening.phase1.collect.inspect", run_id=run_id, container=container
    )
    _write_command(
        evidence_dir / "inspect.json",
        ["podman", "inspect", container],
        settings.command_timeout_status_sec,
    )
    logger.info(
        "app_hardening.phase1.collect.diff_start", run_id=run_id, container=container
    )
    _write_command(
        evidence_dir / "diff.start",
        ["podman", "diff", container],
        settings.command_timeout_status_sec,
    )
    logger.info(
        "app_hardening.phase1.collect.logs_snapshot",
        run_id=run_id,
        container=container,
        tail=500,
    )
    _write_command(
        evidence_dir / "logs.snapshot",
        ["podman", "logs", "--tail", "500", container],
        settings.command_timeout_status_sec,
    )
    logger.info(
        "app_hardening.phase1.collect.events_snapshot",
        run_id=run_id,
        container=container,
    )
    _write_command(
        evidence_dir / "events.snapshot",
        [
            "podman",
            "events",
            "--stream=false",
            "--since",
            f"{max(1, duration_sec)}s",
            "--filter",
            f"container={container}",
        ],
        settings.command_timeout_status_sec,
    )
    sample_deadline = datetime.now(UTC).timestamp() + duration_sec
    stats_lines: list[str] = []
    inside_lines: list[str] = []
    fd_lines: list[str] = []
    sample_count = 0
    while datetime.now(UTC).timestamp() < sample_deadline:
        if _run_was_cancelled_sync(settings, run_id):
            logger.info(
                "app_hardening.phase1.collect.cancel_seen",
                run_id=run_id,
                container=container,
            )
            break
        stats = run_command(
            [
                "podman",
                "stats",
                "--no-stream",
                "--format",
                "{{.MemUsage}}|{{.PIDs}}|{{.CPU}}|{{.NetIO}}|{{.BlockIO}}",
                container,
            ],
            timeout_sec=settings.command_timeout_status_sec,
        )
        sample_count += 1
        logger.info(
            "app_hardening.phase1.collect.stats_sample",
            run_id=run_id,
            container=container,
            sample=sample_count,
            ok=stats.ok,
        )
        stats_lines.append(
            f"{datetime.now(UTC).isoformat()}|{stats.stdout if stats.ok else ''}"
        )
        if len(inside_lines) == 0 or len(stats_lines) % 3 == 0:
            inside = run_command(
                [
                    "podman",
                    "exec",
                    container,
                    "sh",
                    "-lc",
                    "(ss -H -lntu 2>/dev/null || netstat -lntu 2>/dev/null || true); "
                    "echo '--- resolv/hosts ---'; cat /etc/hosts 2>/dev/null || true; "
                    "echo '--- shm ---'; df -B1 /dev/shm 2>/dev/null || true; du -sb /dev/shm 2>/dev/null || true",
                ],
                timeout_sec=settings.command_timeout_status_sec,
            )
            inside_lines.append(f"### {datetime.now(UTC).isoformat()}\n{inside.stdout}")
            fd_lines.extend(_fd_sample(container, settings))
            logger.info(
                "app_hardening.phase1.collect.inside_sample",
                run_id=run_id,
                container=container,
                sample=sample_count,
                ok=inside.ok,
            )
        if duration_sec <= 5:
            break
        asyncio.run(
            asyncio.sleep(
                min(2, max(0, sample_deadline - datetime.now(UTC).timestamp()))
            )
        )
    (evidence_dir / "stats.log").write_text("\n".join(stats_lines), encoding="utf-8")
    (evidence_dir / "inside-sample.log").write_text(
        "\n".join(inside_lines), encoding="utf-8"
    )
    (evidence_dir / "fds.log").write_text("\n".join(fd_lines), encoding="utf-8")
    logger.info(
        "app_hardening.phase1.collect.diff_end", run_id=run_id, container=container
    )
    _write_command(
        evidence_dir / "diff.end",
        ["podman", "diff", container],
        settings.command_timeout_status_sec,
    )
    for name in ("capable", "execsnoop", "opensnoop", "tcpconnect"):
        (evidence_dir / f"{name}.unavailable").write_text(
            "live eBPF monitor is not collected by the web worker; install/run host tracer integration for this signal\n",
            encoding="utf-8",
        )
        logger.info(
            "app_hardening.phase1.collect.optional_unavailable",
            run_id=run_id,
            container=container,
            monitor=name,
        )
    if not (evidence_dir / "seccomp.json").exists():
        (evidence_dir / "seccomp.note").write_text(
            "no generated seccomp profile found; run oci-seccomp-bpf-hook on a traced run to populate this signal\n",
            encoding="utf-8",
        )
        logger.info(
            "app_hardening.phase1.collect.seccomp_profile_missing",
            run_id=run_id,
            container=container,
        )
    logger.info(
        "app_hardening.phase1.collect.finished",
        run_id=run_id,
        container=container,
        evidence_dir=str(evidence_dir),
        stats_samples=len(stats_lines),
        inside_samples=len(inside_lines),
        fd_samples=len(fd_lines),
    )


def _rate_phase1(evidence_dir: Path) -> tuple[dict[str, str], dict[str, Any]]:
    inspect = _json_loads_any(_read(evidence_dir / "inspect.json"))
    inspect_data = (
        inspect[0]
        if isinstance(inspect, list) and inspect and isinstance(inspect[0], dict)
        else {}
    )
    mounts = [
        str(item.get("Destination") or item.get("destination") or "").rstrip("/") or "/"
        for item in inspect_data.get("Mounts", [])
        if isinstance(item, dict)
        and (item.get("Destination") or item.get("destination"))
    ]
    diff = f"{_read(evidence_dir / 'diff.start')}\n{_read(evidence_dir / 'diff.end')}"
    logs = f"{_read(evidence_dir / 'logs.snapshot')}\n{_read(evidence_dir / 'events.snapshot')}"
    inside = _read(evidence_dir / "inside-sample.log")
    stats = _read(evidence_dir / "stats.log")
    fds = _read(evidence_dir / "fds.log")
    dpaths = _diff_paths(diff)
    temp_prefixes = ("/tmp", "/run", "/var/tmp", "/dev/shm")

    def rootfs_writes() -> bool:
        for path in dpaths:
            if _path_under(path, mounts) or _path_under(path, temp_prefixes):
                continue
            if path in ("/etc/hosts", "/etc/resolv.conf", "/etc/hostname"):
                continue
            return True
        return False

    def temp_writes() -> bool:
        return any(_path_under(path, temp_prefixes) for path in dpaths)

    def mount_writes() -> bool:
        return any(_path_under(path, mounts) for path in dpaths)

    cap_available = not (evidence_dir / "capable.unavailable").exists()
    cap_seen = False if cap_available else None
    network_seen = bool(
        re.search(
            r"\bLISTEN\b|connect|listening|server started|https?://|dns|resolve",
            f"{inside}\n{logs}",
            re.I,
        )
    )
    shm_seen = "/dev/shm" in f"{diff}\n{logs}" or bool(
        re.search(r"\b(shmget|shmat|semget|msgget)\b", logs, re.I)
    )
    device_seen = bool(
        re.search(
            r"/dev/(?!null|zero|full|random|urandom|tty|console|ptmx|pts/|fd/|shm/)[A-Za-z0-9_./:-]+",
            logs,
        )
    )
    host_proc_seen = bool(re.search(r"/proc/[0-9]+|/sys/fs/cgroup", logs))
    sysctl_seen = "/proc/sys" in logs or bool(re.search(r"\bsysctl\b", logs, re.I))
    permission_seen = bool(
        re.search(r"permission denied|EACCES|operation not permitted", logs, re.I)
    )
    setuid_seen = bool(
        re.search(
            r"\b(su|sudo|newuidmap|newgidmap|setuid|setgid|filecap)\b", logs, re.I
        )
    )
    hostname_seen = bool(
        re.search(r"\bsethostname|/etc/hostname|hostname\b", logs, re.I)
    )
    masked_seen = any(
        path in logs
        for path in ("/proc/kcore", "/proc/keys", "/sys/firmware", "/sys/fs/selinux")
    )

    ratings = {setting: "uncertain" for setting in HARDENING_SETTINGS}
    ratings.update(
        {
            "privileged_false": "uncertain",
            "cap_drop_all": "likely_unsafe"
            if cap_seen
            else ("uncertain" if cap_seen is None else "likely_safe"),
            "cap_add_exact_exceptions": "likely_unsafe"
            if cap_seen
            else ("uncertain" if cap_seen is None else "likely_safe"),
            "no_new_privileges": "likely_unsafe" if setuid_seen else "uncertain",
            "seccomp_custom_profile": "likely_safe"
            if (evidence_dir / "seccomp.json").exists()
            else "uncertain",
            "seccomp_not_unconfined": "likely_safe",
            "apparmor_confined": "uncertain",
            "selinux_label_separation": "uncertain",
            "selinux_nested_disabled": "likely_safe",
            "default_masked_paths": "likely_unsafe" if masked_seen else "likely_safe",
            "unmask_all_disabled": "likely_safe",
            "read_only_rootfs": "certain_unsafe" if rootfs_writes() else "likely_safe",
            "read_only_tmpfs_false": "certain_unsafe"
            if temp_writes()
            else "likely_safe",
            "explicit_tmpfs_paths": "likely_unsafe" if temp_writes() else "likely_safe",
            "tmpfs_noexec": "likely_safe",
            "tmpfs_nosuid": "likely_unsafe"
            if setuid_seen and temp_writes()
            else "likely_safe",
            "tmpfs_nodev": "certain_unsafe"
            if device_seen and temp_writes()
            else "likely_safe",
            "existing_mounts_ro": "certain_unsafe" if mount_writes() else "likely_safe",
            "existing_mounts_noexec": "likely_safe",
            "existing_mounts_nosuid": "likely_unsafe"
            if setuid_seen and mounts
            else "likely_safe",
            "existing_mounts_nodev": "certain_unsafe"
            if device_seen and mounts
            else "likely_safe",
            "mount_idmap_or_U_only_if_needed": "likely_unsafe"
            if permission_seen
            else "uncertain",
            "no_extra_host_devices": "certain_unsafe" if device_seen else "likely_safe",
            "exact_device_permissions_if_needed": "likely_unsafe"
            if device_seen
            else "likely_safe",
            "no_device_cgroup_rules_unless_hit": "likely_safe",
            "network_none": "certain_unsafe" if network_seen else "likely_safe",
            "isolated_network_not_host": "likely_unsafe"
            if re.search(r"127\.0\.0\.1|localhost|::1", logs, re.I)
            else "likely_safe",
            "no_publish_all": "likely_safe",
            "no_hosts_file_if_safe": "uncertain",
            "no_add_host_unless_hit": "likely_safe",
            "pid_private": "likely_unsafe" if host_proc_seen else "likely_safe",
            "ipc_none": "certain_unsafe" if shm_seen else "likely_safe",
            "ipc_private": "likely_safe",
            "cgroupns_private": "likely_unsafe" if host_proc_seen else "likely_safe",
            "uts_private": "likely_unsafe" if hostname_seen else "likely_safe",
            "nonroot_user": "likely_unsafe"
            if permission_seen or cap_seen
            else "uncertain",
            "userns_auto_or_nomap": "likely_unsafe" if permission_seen else "uncertain",
            "no_keep_groups_unless_hit": "likely_unsafe"
            if permission_seen
            else "likely_safe",
            "no_custom_sysctls": "likely_unsafe" if sysctl_seen else "likely_safe",
            "pids_limit": "likely_safe",
            "memory_limit": "likely_safe",
            "nofile_ulimit": "likely_safe",
            "shm_size": "likely_safe",
            "env_host_false": "uncertain",
        }
    )
    values = _recommended_values(stats, fds)
    (evidence_dir / "values.json").write_text(_json_dumps(values), encoding="utf-8")
    return ratings, {
        "message": "Phase 1 monitor finished.",
        "values": values,
        "evidence_dir": str(evidence_dir),
    }


def _setting_label(setting: str | None) -> str:
    return str(setting or "").replace("_", " ")


def _phase2_progress_message(
    *,
    total: int,
    completed: int,
    state: str,
    substate: str,
    current_setting: str | None = None,
) -> str:
    label = _setting_label(current_setting)
    position = (
        min(total, completed + 1)
        if current_setting and completed < total
        else completed
    )
    step = f"{position} of {total}" if total > 0 else "0 of 0"
    substate_labels = {
        "waiting": "waiting to start",
        "copying_output": "cloning test output",
        "creating_network": "preparing isolated network",
        "queued": "queued",
        "preclean": "preparing",
        "create": "creating test container",
        "start": "starting test container",
        "watch": "watching health",
        "cleanup": "cleaning test container",
        "phase1_certain_unsafe": "skipping",
        "seccomp_profile_missing": "missing seccomp profile",
        "complete": "complete",
        "removing_clone": "removing clone",
        "stopped": "stopped",
        "error": "failed",
    }
    substate_label = substate_labels.get(substate, substate.replace("_", " "))
    if state == "finished":
        return "Phase 2 finished."
    if state == "cancelled":
        return "Phase 2 stopped."
    if state == "failed":
        return "Phase 2 failed."
    if current_setting:
        return f"Phase 2 {substate_label}: {label} ({step})."
    if state == "cleanup":
        return "Phase 2 cleaning up clone."
    if state == "cloning":
        return "Phase 2 cloning test output."
    if state == "network":
        return "Phase 2 preparing isolated test network."
    return "Phase 2 preparing."


def _phase2_progress_details(
    details: dict[str, Any],
    *,
    total: int,
    completed: int,
    state: str,
    substate: str,
    current_setting: str | None = None,
) -> dict[str, Any]:
    bounded_total = max(0, total)
    bounded_completed = max(0, min(completed, bounded_total))
    progress = {
        "completed": bounded_completed,
        "total": bounded_total,
        "state": state,
        "substate": substate,
        "current_setting": current_setting or "",
        "current_label": _setting_label(current_setting) if current_setting else "",
    }
    return {
        **details,
        "message": _phase2_progress_message(
            total=bounded_total,
            completed=bounded_completed,
            state=state,
            substate=substate,
            current_setting=current_setting,
        ),
        "progress": progress,
    }


async def _record_phase2_progress(
    settings: Settings,
    run_id: int,
    ratings: dict[str, str],
    details: dict[str, Any],
    *,
    total: int,
    completed: int,
    state: str,
    substate: str,
    current_setting: str | None = None,
) -> None:
    async with _session(settings) as session:
        run = await session.get(BackendHardeningRun, run_id)
        if run is None or run.status not in {"queued", "running"}:
            return
        run.ratings_json = _json_dumps(ratings)
        run.details_json = _json_dumps(
            _phase2_progress_details(
                details,
                total=total,
                completed=completed,
                state=state,
                substate=substate,
                current_setting=current_setting,
            )
        )
        await session.commit()


def _record_phase2_progress_sync(
    settings: Settings,
    run_id: int,
    ratings: dict[str, str],
    details: dict[str, Any],
    *,
    total: int,
    completed: int,
    state: str,
    substate: str,
    current_setting: str | None = None,
) -> None:
    try:
        asyncio.run(
            _record_phase2_progress(
                settings,
                run_id,
                ratings,
                details,
                total=total,
                completed=completed,
                state=state,
                substate=substate,
                current_setting=current_setting,
            )
        )
    except Exception as exc:
        logger.warning(
            "app_hardening.phase2.progress_update_failed",
            run_id=run_id,
            state=state,
            substate=substate,
            error=str(exc),
        )


def _test_strict_setting(
    backend: Backend,
    setting: StrictSetting,
    evidence_dir: Path,
    settings: Settings,
    progress: Callable[[str], None] | None = None,
) -> str:
    def publish(substate: str) -> None:
        if progress is not None:
            progress(substate)

    setting_dir = evidence_dir / "settings" / setting.name
    setting_dir.mkdir(parents=True, exist_ok=True)
    container = container_name(backend.name)
    publish("preclean")
    logger.info(
        "app_hardening.phase2.setting.preclean",
        backend_name=backend.name,
        setting=setting.name,
        container=container,
    )
    preclean = run_command(
        ["podman", "rm", "-f", container],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    if not preclean.ok:
        logger.warning(
            "app_hardening.phase2.setting.preclean_failed",
            backend_name=backend.name,
            setting=setting.name,
            container=container,
            returncode=preclean.returncode,
        )
    flags = list(setting.flags)
    phase1_values = _json_loads_dict(_read(evidence_dir / "phase1-values.json"))
    if setting.name == "pids_limit":
        flags.append(f"--pids-limit={phase1_values.get('pids_limit', 512)}")
    elif setting.name == "memory_limit":
        flags.append(f"--memory={phase1_values.get('memory_limit', '512M')}")
    elif setting.name == "nofile_ulimit":
        value = phase1_values.get("nofile_limit", 4096)
        flags.append(f"--ulimit=nofile={value}:{value}")
    elif setting.name == "shm_size":
        flags.append(f"--shm-size={phase1_values.get('shm_size', '64M')}")
    elif setting.name == "seccomp_custom_profile":
        profile = evidence_dir.parent / "phase1" / "seccomp.json"
        if not profile.exists():
            (setting_dir / "result.txt").write_text(
                "seccomp profile unavailable\n", encoding="utf-8"
            )
            publish("seccomp_profile_missing")
            logger.info(
                "app_hardening.phase2.setting.uncertain",
                backend_name=backend.name,
                setting=setting.name,
                reason="seccomp_profile_missing",
            )
            return "uncertain"
        flags.append(f"--security-opt=seccomp={profile}")
    try:
        command = _strict_create_command(backend, settings, flags)
        _write_json(setting_dir / "command.json", command)
        logger.info(
            "app_hardening.phase2.setting.create",
            backend_name=backend.name,
            setting=setting.name,
            container=container,
            flags=flags,
        )
        publish("create")
        create = run_command(command, timeout_sec=settings.command_timeout_apply_sec)
        _write_result(setting_dir / "create", create)
        if not create.ok:
            rating = (
                "certain_unsafe"
                if _hit_detected(setting.name, create.stdout, create.stderr)
                else "uncertain"
            )
            logger.info(
                "app_hardening.phase2.setting.create_failed",
                backend_name=backend.name,
                setting=setting.name,
                container=container,
                rating=rating,
                returncode=create.returncode,
            )
            return rating
        start_ts = datetime.now(UTC)
        logger.info(
            "app_hardening.phase2.setting.start",
            backend_name=backend.name,
            setting=setting.name,
            container=container,
        )
        publish("start")
        start = run_command(
            ["podman", "start", container],
            timeout_sec=settings.command_timeout_apply_sec,
        )
        _write_result(setting_dir / "start", start)
        if not start.ok:
            rating = (
                "certain_unsafe"
                if _hit_detected(setting.name, start.stdout, start.stderr)
                else "uncertain"
            )
            logger.info(
                "app_hardening.phase2.setting.start_failed",
                backend_name=backend.name,
                setting=setting.name,
                container=container,
                rating=rating,
                returncode=start.returncode,
            )
            return rating
        logger.info(
            "app_hardening.phase2.setting.watch",
            backend_name=backend.name,
            setting=setting.name,
            container=container,
        )
        publish("watch")
        ok, blob = _watch_container(backend, container, setting_dir, settings, start_ts)
        if ok and not _hit_detected(setting.name, blob):
            return "certain_safe"
        if _hit_detected(setting.name, blob):
            return "certain_unsafe"
        return "uncertain"
    finally:
        publish("cleanup")
        cleanup = run_command(
            ["podman", "rm", "-f", container],
            timeout_sec=settings.command_timeout_apply_sec,
        )
        if cleanup.ok:
            logger.info(
                "app_hardening.phase2.setting.cleanup",
                backend_name=backend.name,
                setting=setting.name,
                container=container,
            )
        else:
            logger.warning(
                "app_hardening.phase2.setting.cleanup_failed",
                backend_name=backend.name,
                setting=setting.name,
                container=container,
                returncode=cleanup.returncode,
            )


def _strict_create_command(
    backend: Backend, settings: Settings, flags: list[str]
) -> list[str]:
    base_profile = build_resource_profile(settings, [backend])
    command = build_app_container_create_command(
        backend, settings, base_profile=base_profile
    )
    try:
        rootfs_index = command.index("--rootfs")
    except ValueError:
        rootfs_index = len(command)
    filtered = (
        _remove_conflicting_flags(command[:rootfs_index], flags)
        + command[rootfs_index:]
    )
    rootfs_index = (
        filtered.index("--rootfs") if "--rootfs" in filtered else len(filtered)
    )
    return [*filtered[:rootfs_index], *flags, *filtered[rootfs_index:]]


def _remove_conflicting_flags(command: list[str], flags: list[str]) -> list[str]:
    if "--network=none" in flags:
        command = _drop_option_with_value(command, "--network")
    if any(flag.startswith("--userns=") for flag in flags):
        command = _drop_option_with_value(command, "--userns")
    if any(flag.startswith("--memory=") for flag in flags):
        command = _drop_option_with_value(command, "--memory")
    return command


def _watch_container(
    backend: Backend,
    container: str,
    evidence_dir: Path,
    settings: Settings,
    start_ts: datetime,
) -> tuple[bool, str]:
    deadline = datetime.now(UTC).timestamp() + settings.hardening_phase2_test_sec
    lines: list[str] = []
    ok = True
    while datetime.now(UTC).timestamp() < deadline:
        _result, inspect = inspect_container(
            container, timeout_sec=settings.command_timeout_status_sec
        )
        status = str(((inspect or {}).get("State") or {}).get("Status") or "")
        lines.append(f"{datetime.now(UTC).isoformat()} state={status}")
        if status in {"exited", "dead"}:
            ok = False
            break
        if backend.port is not None:
            probe = probe_backend_health(
                backend,
                host="127.0.0.1",
                port=backend.port,
                timeout_sec=settings.command_timeout_status_sec,
            )
            lines.append(
                f"probe ok={probe.ok} status={probe.http_status} error={probe.error}"
            )
            if probe.ok:
                break
        asyncio.run(asyncio.sleep(3))
    logs = run_command(
        ["podman", "logs", "--since", start_ts.isoformat(), container],
        timeout_sec=settings.command_timeout_status_sec,
    )
    inspect_result = run_command(
        ["podman", "inspect", container],
        timeout_sec=settings.command_timeout_status_sec,
    )
    _write_result(evidence_dir / "logs", logs)
    _write_result(evidence_dir / "inspect", inspect_result)
    health_text = "\n".join(lines)
    (evidence_dir / "health.log").write_text(health_text, encoding="utf-8")
    return ok, "\n".join(
        (
            health_text,
            logs.stdout,
            logs.stderr,
            inspect_result.stdout,
            inspect_result.stderr,
        )
    )


def _hit_detected(setting: str, *parts: str) -> bool:
    blob = "\n".join(parts)
    patterns = {
        "cap_drop_all": r"operation not permitted|EPERM|capability|permission denied",
        "cap_add_exact_exceptions": r"operation not permitted|EPERM|capability|permission denied",
        "no_new_privileges": r"setuid|setgid|filecap|operation not permitted|permission denied|no new privileges",
        "seccomp_custom_profile": r"seccomp|SIGSYS|bad system call|operation not permitted|EPERM",
        "seccomp_not_unconfined": r"seccomp|SIGSYS|bad system call|operation not permitted|EPERM",
        "apparmor_confined": r"apparmor|DENIED|operation not permitted|permission denied",
        "selinux_label_separation": r"avc:|selinux|permission denied|operation not permitted",
        "selinux_nested_disabled": r"avc:|selinux|permission denied|operation not permitted",
        "read_only_rootfs": r"read-only file system|EROFS|permission denied|EACCES",
        "read_only_tmpfs_false": r"read-only file system|EROFS|permission denied|EACCES",
        "network_none": r"network unreachable|connection refused|connection timed out|could not resolve|dns|lookup|host not found",
        "nofile_ulimit": r"too many open files|EMFILE",
        "memory_limit": r"oom|out of memory|exit code 137|cannot allocate memory|ENOMEM",
        "pids_limit": r"resource temporarily unavailable|pids\.max|cannot allocate thread|pthread_create|EAGAIN",
        "shm_size": r"no space left|ENOSPC|SIGBUS|shm",
    }
    return bool(
        re.search(
            patterns.get(
                setting,
                r"permission denied|operation not permitted|read-only file system|unhealthy|failed|error",
            ),
            blob,
            re.I,
        )
    )


def _create_test_network(
    backend_name: str, evidence_dir: Path, settings: Settings
) -> None:
    network = network_name(backend_name)
    logger.info(
        "app_hardening.phase2.network.preclean",
        backend_name=backend_name,
        network=network,
    )
    preclean = run_command(
        ["podman", "network", "rm", "-f", network],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    if not preclean.ok:
        logger.warning(
            "app_hardening.phase2.network.preclean_failed",
            backend_name=backend_name,
            network=network,
            returncode=preclean.returncode,
        )
    result = run_command(
        [
            "podman",
            "network",
            "create",
            "--disable-dns",
            "--label",
            "io.cnc.managed=true",
            "--label",
            f"io.cnc.backend={backend_name}",
            network,
        ],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    _write_result(evidence_dir / "network-create", result)
    if result.ok:
        logger.info(
            "app_hardening.phase2.network.created",
            backend_name=backend_name,
            network=network,
        )
    else:
        logger.warning(
            "app_hardening.phase2.network.create_failed",
            backend_name=backend_name,
            network=network,
            returncode=result.returncode,
        )
        raise CommandError(result)


def _cleanup_phase2_clone(backend: Backend, settings: Settings) -> dict[str, object]:
    container = container_name(backend.name)
    network = network_name(backend.name)
    container_cleanup = run_command(
        ["podman", "rm", "-f", container],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    if container_cleanup.ok:
        logger.info(
            "app_hardening.phase2.cleanup.container_removed",
            clone_name=backend.name,
            container=container,
        )
    else:
        logger.warning(
            "app_hardening.phase2.cleanup.container_remove_failed",
            clone_name=backend.name,
            container=container,
            returncode=container_cleanup.returncode,
        )
    network_cleanup = run_command(
        ["podman", "network", "rm", "-f", network],
        timeout_sec=settings.command_timeout_apply_sec,
    )
    if network_cleanup.ok:
        logger.info(
            "app_hardening.phase2.cleanup.network_removed",
            clone_name=backend.name,
            network=network,
        )
    else:
        logger.warning(
            "app_hardening.phase2.cleanup.network_remove_failed",
            clone_name=backend.name,
            network=network,
            returncode=network_cleanup.returncode,
        )
    sandbox_dir = settings.app_sandbox_dir / backend.name
    control_dir = settings.app_control_dir / backend.name
    shutil.rmtree(sandbox_dir, ignore_errors=True)
    logger.info(
        "app_hardening.phase2.cleanup.sandbox_removed",
        clone_name=backend.name,
        path=str(sandbox_dir),
    )
    shutil.rmtree(control_dir, ignore_errors=True)
    logger.info(
        "app_hardening.phase2.cleanup.control_removed",
        clone_name=backend.name,
        path=str(control_dir),
    )
    return {
        "ok": container_cleanup.ok and network_cleanup.ok,
        "container_removed": container_cleanup.ok,
        "network_removed": network_cleanup.ok,
        "container_returncode": getattr(container_cleanup, "returncode", None),
        "network_returncode": getattr(network_cleanup, "returncode", None),
    }


async def _run_was_cancelled(settings: Settings, run_id: int) -> bool:
    async with _session(settings) as session:
        run = await session.get(BackendHardeningRun, run_id)
        details = _json_loads_dict(run.details_json) if run is not None else {}
        return run is not None and (
            run.status == "cancelled" or bool(details.get("stop_requested"))
        )


def _run_was_cancelled_sync(settings: Settings, run_id: int) -> bool:
    return asyncio.run(_run_was_cancelled(settings, run_id))


async def _load_backend(session: AsyncSession, backend_id: int) -> Backend | None:
    return (
        await session.execute(
            select(Backend)
            .options(selectinload(Backend.inputs))
            .where(Backend.id == backend_id)
        )
    ).scalar_one_or_none()


async def _latest_successful_phase1(
    session: AsyncSession, backend_id: int
) -> BackendHardeningRun | None:
    return (
        await session.execute(
            select(BackendHardeningRun)
            .where(
                BackendHardeningRun.backend_id == backend_id,
                BackendHardeningRun.phase == "phase1",
                BackendHardeningRun.status == "success",
            )
            .order_by(
                BackendHardeningRun.started_at.desc(), BackendHardeningRun.id.desc()
            )
            .limit(1)
        )
    ).scalar_one_or_none()


def _interrupted_hardening_details(run: BackendHardeningRun) -> dict[str, Any]:
    details = _json_loads_dict(run.details_json)
    details["interrupted"] = True
    if run.phase == "phase1":
        details["message"] = "Phase 1 monitor interrupted by CNC restart."
    elif run.phase == "phase2":
        details["message"] = "Phase 2 clone test interrupted by CNC restart."
    else:
        details["message"] = "Hardening run interrupted by CNC restart."
    progress = details.get("progress")
    if isinstance(progress, dict):
        progress["state"] = "failed"
        progress["substate"] = "interrupted"
        details["progress"] = progress
    return details


def _phase2_run_resumable(run: BackendHardeningRun) -> bool:
    if run.phase != "phase2" or run.status != "failed":
        return False
    details = _json_loads_dict(run.details_json)
    return details.get("interrupted") is True or run.error == HARDENING_RESTART_ERROR


def _phase2_seed_ratings(run: BackendHardeningRun) -> dict[str, str]:
    ratings = _json_loads_dict(run.ratings_json)
    known_settings = set(HARDENING_SETTINGS)
    known_ratings = set(PHASE2_RATINGS)
    return {
        str(setting): str(rating)
        for setting, rating in ratings.items()
        if str(setting) in known_settings and str(rating) in known_ratings
    }


def _phase2_seed_details(
    run: BackendHardeningRun, ratings: dict[str, str]
) -> dict[str, Any]:
    details = _phase2_runtime_details(_json_loads_dict(run.details_json), ratings)
    details["resumed_from_run_id"] = run.id
    details["message"] = "Phase 2 resume queued."
    return details


def _phase2_runtime_details(
    raw_details: dict[str, Any], ratings: dict[str, str]
) -> dict[str, Any]:
    tested: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    seen: set[str] = set()

    for item in raw_details.get("tested", []):
        if not isinstance(item, dict):
            continue
        setting = str(item.get("setting") or "")
        if setting not in ratings:
            continue
        tested.append({"setting": setting, "rating": ratings[setting]})
        seen.add(setting)

    for item in raw_details.get("skipped", []):
        if not isinstance(item, dict):
            continue
        setting = str(item.get("setting") or "")
        if setting not in ratings or setting in seen:
            continue
        skipped.append(
            {"setting": setting, "reason": str(item.get("reason") or "previous_run")}
        )
        seen.add(setting)

    for setting, rating in ratings.items():
        if setting in seen:
            continue
        tested.append({"setting": setting, "rating": rating})
        seen.add(setting)

    details: dict[str, Any] = {"tested": tested, "skipped": skipped}
    resumed_from = raw_details.get("resumed_from_run_id")
    if isinstance(resumed_from, int):
        details["resumed_from_run_id"] = resumed_from
    return details


def _phase2_completed_setting_names(
    ratings: dict[str, str], details: dict[str, Any]
) -> set[str]:
    names = set(ratings)
    for bucket in ("tested", "skipped"):
        for item in details.get(bucket, []):
            if isinstance(item, dict):
                setting = str(item.get("setting") or "")
                if setting:
                    names.add(setting)
    return names


def _phase2_clone_base_name(source_name: str, run_id: int) -> str:
    return (
        safe_slug(f"hardening-{source_name}-{run_id}")[:58].rstrip("-")
        or f"hardening-{run_id}"
    )


def _phase2_clone_name_matches(candidate: str, base: str) -> bool:
    if candidate == base:
        return True
    suffix = candidate.removeprefix(f"{base}-")
    return suffix != candidate and suffix.isdigit()


async def _phase2_clone_backends_for_run(
    session: AsyncSession, source_name: str, run_id: int
) -> list[Backend]:
    base = _phase2_clone_base_name(source_name, run_id)
    matches = (
        (
            await session.execute(
                select(Backend).where(
                    Backend.kind == "app",
                    Backend.name.like(f"{base}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        backend for backend in matches if _phase2_clone_name_matches(backend.name, base)
    ]


async def _phase2_clone_name(
    session: AsyncSession, source_name: str, run_id: int
) -> str:
    base = _phase2_clone_base_name(source_name, run_id)
    candidate = base
    counter = 1
    while (
        await session.execute(
            select(Backend.id).where(Backend.name == candidate).limit(1)
        )
    ).scalar_one_or_none():
        counter += 1
        suffix = f"-{counter}"
        candidate = f"{base[: 63 - len(suffix)].rstrip('-')}{suffix}"
    return candidate


async def _next_phase2_port(session: AsyncSession, source_port: int | None) -> int:
    existing_ports = {
        port
        for port in (
            await session.execute(
                select(Backend.port).where(
                    Backend.kind == "app", Backend.port.is_not(None)
                )
            )
        ).scalars()
        if isinstance(port, int)
    }
    candidate = (
        source_port + 1000
        if isinstance(source_port, int) and source_port > 0
        else 20000
    )
    while candidate in existing_ports or candidate > 65535:
        candidate = 20000 if candidate > 65535 else candidate + 1
        if candidate in existing_ports:
            candidate += 1
    return candidate


def _run_evidence_dir(
    settings: Settings, backend_name: str, run_id: int, phase: str
) -> Path:
    return (
        settings.app_control_dir / backend_name / "hardening" / f"run-{run_id}" / phase
    )


def _ensure_evidence_dir(run: BackendHardeningRun) -> Path:
    if not run.evidence_dir:
        raise RuntimeError("hardening run evidence directory missing")
    path = Path(run.evidence_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _update_run(
    session: AsyncSession,
    run: BackendHardeningRun,
    status: str,
    *,
    details: dict[str, Any],
) -> None:
    run.status = status
    run.details_json = _json_dumps(details)
    await session.commit()


async def _finish_run(
    session: AsyncSession,
    run: BackendHardeningRun,
    status: str,
    *,
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    run.status = status
    run.error = error
    run.finished_at = datetime.now(UTC)
    if details is not None:
        run.details_json = _json_dumps(details)
    await session.commit()


def _hardening_recommendation_counts(
    phase1_ratings: dict[str, Any],
    phase2_ratings: dict[str, Any] | None = None,
) -> dict[str, int]:
    phase2_ratings = phase2_ratings or {}
    counts = {"recommended": 0, "not_recommended": 0, "uncertain": 0}
    for setting in HARDENING_SETTINGS:
        rating = str(phase2_ratings.get(setting) or phase1_ratings.get(setting) or "")
        if not rating:
            continue
        if rating in {"certain_safe", "likely_safe"}:
            counts["recommended"] += 1
        elif rating in {"certain_unsafe", "likely_unsafe"}:
            counts["not_recommended"] += 1
        else:
            counts["uncertain"] += 1
    return counts


async def _emit_hardening_run_event(
    settings: Settings,
    *,
    backend_id: int | None,
    backend_name: str,
    run: BackendHardeningRun,
    ratings: dict[str, Any],
    details: dict[str, Any] | None = None,
) -> None:
    phase_label = (
        "Security baseline" if run.phase == "phase1" else "Security clone test"
    )
    status = str(run.status or "")
    if status == "success":
        summary = f"{phase_label} complete"
        severity = "success"
        status_label = "complete"
    elif status == "cancelled":
        summary = f"{phase_label} stopped"
        severity = "info"
        status_label = "stopped"
    else:
        summary = f"{phase_label} failed"
        severity = "error"
        status_label = "failed"
    counts = _hardening_recommendation_counts(ratings)
    await emit_control_event(
        settings,
        kind=f"hardening_{run.phase}_{status_label}",
        source="hardening",
        summary=summary,
        severity=severity,
        scope="backend",
        backend_name=backend_name,
        subevents=[
            {"label": "run", "value": f"#{run.id}"},
            {"label": "recommended", "value": str(counts["recommended"])},
            {"label": "not recommended", "value": str(counts["not_recommended"])},
            {"label": "uncertain", "value": str(counts["uncertain"])},
        ],
        details={
            "backend_id": backend_id,
            "run_id": run.id,
            "status": status,
            "recommended": counts["recommended"],
            "not_recommended": counts["not_recommended"],
            "uncertain": counts["uncertain"],
            "message": (details or {}).get("message") or run.error or "",
        },
        notify=False,
    )


def _run_payload(run: BackendHardeningRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "phase": run.phase,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else "",
        "finished_at": run.finished_at.isoformat() if run.finished_at else "",
        "ratings": _json_loads_dict(run.ratings_json),
        "details": _json_loads_dict(run.details_json),
        "error": run.error or "",
        "evidence_dir": run.evidence_dir or "",
    }


def _feature_rows(
    phase1: BackendHardeningRun | None, phase2: BackendHardeningRun | None
) -> list[dict[str, str]]:
    phase1_ratings = _json_loads_dict(phase1.ratings_json) if phase1 is not None else {}
    phase2_ratings = _json_loads_dict(phase2.ratings_json) if phase2 is not None else {}
    rows = []
    for setting in HARDENING_SETTINGS:
        phase2_rating = str(phase2_ratings.get(setting) or "")
        phase1_rating = str(phase1_ratings.get(setting) or "")
        rating = phase2_rating or phase1_rating
        if not rating:
            continue
        recommendation = "uncertain"
        if rating in {"certain_safe", "likely_safe"}:
            recommendation = "recommended"
        elif rating in {"certain_unsafe", "likely_unsafe"}:
            recommendation = "do not apply"
        group = _SETTING_GROUPS.get(setting, {"group": "other", "group_label": "Other"})
        rows.append(
            {
                "setting": setting,
                "label": setting.replace("_", " "),
                "description": HARDENING_SETTING_DESCRIPTIONS.get(
                    setting, "Podman runtime hardening setting."
                ),
                "group": group["group"],
                "group_label": group["group_label"],
                "phase1": phase1_rating,
                "phase2": phase2_rating,
                "rating": rating,
                "recommendation": recommendation,
                "evidence": _feature_evidence(
                    setting, phase1, phase1_rating, phase2, phase2_rating
                ),
            }
        )
    return rows


def _feature_evidence(
    setting: str,
    phase1: BackendHardeningRun | None,
    phase1_rating: str,
    phase2: BackendHardeningRun | None,
    phase2_rating: str,
) -> str:
    _ = phase1, phase2
    if phase2_rating:
        blurb = PHASE2_EVIDENCE_BLURBS.get(
            setting, "Phase 2 enforced this setting on a temporary clone."
        )
        return f"{blurb} {_phase2_rating_sentence(phase2_rating)}"
    if phase1_rating:
        blurb = PHASE1_EVIDENCE_BLURBS.get(
            setting, "Phase 1 observed the running container for this setting."
        )
        return f"{blurb} {_phase1_rating_sentence(phase1_rating)}"
    return ""


def _phase1_rating_sentence(rating: str) -> str:
    if rating == "certain_unsafe":
        return "Result: Phase 1 directly observed behavior this setting would block, so it is marked certain unsafe."
    if rating == "likely_unsafe":
        return "Result: Phase 1 observed a dependency signal, but enforcement has not confirmed it yet."
    if rating == "likely_safe":
        return "Result: Phase 1 did not observe a dependency signal; Phase 2 is still required before calling it certain safe."
    return "Result: Phase 1 did not collect enough decisive runtime evidence for this setting."


def _phase2_rating_sentence(rating: str) -> str:
    if rating == "certain_safe":
        return (
            "Result: the clone stayed healthy and no matching denial, startup failure, health failure, or configured hit pattern "
            "appeared during the test window."
        )
    if rating == "certain_unsafe":
        return (
            "Result: the clone hit a matching create, start, health, log, inspect, or kernel-style failure pattern, so the strict "
            "setting is marked certain unsafe."
        )
    return "Result: the test did not produce enough clean evidence to call the setting safe or unsafe."


def _fd_sample(container: str, settings: Settings) -> list[str]:
    top = run_command(
        ["podman", "top", container, "hpid"],
        timeout_sec=settings.command_timeout_status_sec,
    )
    if not top.ok:
        return []
    lines = []
    for hpid in re.findall(r"^\s*(\d+)\b", top.stdout, re.M):
        fd_dir = Path("/proc") / hpid / "fd"
        try:
            count = len(list(fd_dir.iterdir()))
        except OSError:
            continue
        lines.append(f"hpid={hpid} fd_count={count}")
    return lines


def _recommended_values(stats: str, fds: str) -> dict[str, Any]:
    max_mem = 0
    max_pids = 0
    max_fd = 0
    for line in stats.splitlines():
        parts = line.split("|")
        if len(parts) >= 3:
            max_mem = max(
                max_mem, _parse_size_to_bytes(parts[1].split("/")[0].strip()) or 0
            )
            try:
                max_pids = max(max_pids, int(re.sub(r"\D", "", parts[2]) or "0"))
            except ValueError:
                pass
    for match in re.finditer(r"fd_count=(\d+)", fds):
        max_fd = max(max_fd, int(match.group(1)))
    pids_limit = max(128, int(math.ceil(max_pids * 2 + 32))) if max_pids else 512
    nofile_limit = max(1024, int(math.ceil(max_fd * 2 + 128))) if max_fd else 4096
    memory_limit = (
        max(256 * 1024**2, int(math.ceil(max_mem * 1.75))) if max_mem else 512 * 1024**2
    )
    return {
        "pids_limit": pids_limit,
        "nofile_limit": nofile_limit,
        "memory_limit": _human_bytes(memory_limit),
        "shm_size": "64M",
    }


def _parse_size_to_bytes(value: str) -> int | None:
    match = re.match(r"([0-9.]+)\s*([KMGTPE]?i?B|[KMGTPE]?B)?", value, re.I)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "B").upper().replace("IB", "B")
    multiplier = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
        "T": 1000**4,
    }.get(unit, 1)
    return int(number * multiplier)


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "K", "M", "G", "T"):
        if amount < 1024 or unit == "T":
            return f"{int(math.ceil(amount))}{unit}"
        amount /= 1024
    return f"{value}B"


def _diff_paths(diff: str) -> list[str]:
    return [
        match.group(1).strip() for match in re.finditer(r"^[ACD]\s+(.+)$", diff, re.M)
    ]


def _path_under(path: str, prefixes: Iterable[str]) -> bool:
    normalized = path.rstrip("/") or "/"
    for prefix in prefixes:
        candidate = str(prefix).rstrip("/") or "/"
        if normalized == candidate or normalized.startswith(candidate + "/"):
            return True
    return False


def _drop_option_with_value(command: list[str], option: str) -> list[str]:
    output: list[str] = []
    index = 0
    while index < len(command):
        item = command[index]
        if item == option:
            index += 2
            continue
        if item.startswith(option + "="):
            index += 1
            continue
        output.append(item)
        index += 1
    return output


def _write_command(path: Path, command: list[str], timeout_sec: int) -> None:
    result = run_command(command, timeout_sec=timeout_sec)
    _write_result(path, result)


def _write_result(path: Path, result: CommandResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.stdout, encoding="utf-8")
    path.with_suffix(path.suffix + ".err").write_text(result.stderr, encoding="utf-8")
    path.with_suffix(path.suffix + ".code").write_text(
        str(result.returncode), encoding="utf-8"
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _json_dumps(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def _json_loads_dict(raw: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_loads_any(raw: str | None) -> Any:
    try:
        return json.loads(raw or "")
    except json.JSONDecodeError:
        return None


class _RunCancelled(Exception):
    pass


class _session:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine = create_configured_async_engine(
            settings.database_url,
            future=True,
            echo=False,
            connect_args=_sqlite_connect_args(settings.database_url),
        )
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> AsyncSession:
        factory = async_sessionmaker(
            bind=self._engine, expire_on_commit=False, class_=AsyncSession
        )
        self._session = factory()
        return self._session

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._session is not None:
            await self._session.close()
        await self._engine.dispose()


def _sqlite_connect_args(database_url: str) -> dict[str, int]:
    return {"timeout": 30} if database_url.startswith("sqlite") else {}


__all__ = [
    "HARDENING_SETTINGS",
    "cancel_latest_hardening_run",
    "active_hardening_run",
    "create_hardening_run",
    "create_resumed_phase2_run",
    "latest_hardening_summary",
    "latest_resumable_phase2_run",
    "request_phase1_monitor_stop",
    "run_phase1_monitor",
    "run_phase2_test",
]
