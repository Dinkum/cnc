from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from app.models.entities import Backend


class HardeningPolicy(BaseModel):
    """Reviewed container controls, never arbitrary Podman or systemd arguments."""

    model_config = ConfigDict(extra="forbid", strict=True)

    cap_drop_all: StrictBool = False
    cap_add: list[str] = Field(default_factory=list, max_length=41)
    no_new_privileges: StrictBool = False
    read_only_rootfs: StrictBool = False
    read_only_tmpfs_false: StrictBool = False
    tmpfs_paths: Literal["none", "tmp", "run", "tmp_run"] = "none"
    tmpfs_noexec: StrictBool = False
    tmpfs_nosuid: StrictBool = False
    tmpfs_nodev: StrictBool = False
    existing_mounts_ro: StrictBool = False
    existing_mounts_noexec: StrictBool = False
    existing_mounts_nosuid: StrictBool = False
    existing_mounts_nodev: StrictBool = False
    no_hosts_file_if_safe: StrictBool = False
    pid_private: StrictBool = False
    ipc: Literal["default", "private", "none"] = "default"
    cgroupns_private: StrictBool = False
    uts_private: StrictBool = False
    apparmor_profile: str = Field(
        default="", max_length=128, pattern=r"^[A-Za-z0-9_.-]*$"
    )
    pids_limit: int | None = Field(default=None, ge=16, le=4_194_304)
    nofile_ulimit: int | None = Field(default=None, ge=64, le=1_048_576)
    shm_size_mib: int | None = Field(default=None, ge=1, le=1_048_576)

    @field_validator("apparmor_profile")
    @classmethod
    def confined_profile(cls, value: str) -> str:
        if value.lower() == "unconfined":
            raise ValueError(
                "Choose a confined AppArmor profile or leave the runtime default."
            )
        return value

    @field_validator("cap_add")
    @classmethod
    def exact_capabilities(cls, values: list[str]) -> list[str]:
        capabilities = {
            "AUDIT_CONTROL",
            "AUDIT_READ",
            "AUDIT_WRITE",
            "BLOCK_SUSPEND",
            "BPF",
            "CHECKPOINT_RESTORE",
            "CHOWN",
            "DAC_OVERRIDE",
            "DAC_READ_SEARCH",
            "FOWNER",
            "FSETID",
            "IPC_LOCK",
            "IPC_OWNER",
            "KILL",
            "LEASE",
            "LINUX_IMMUTABLE",
            "MAC_ADMIN",
            "MAC_OVERRIDE",
            "MKNOD",
            "NET_ADMIN",
            "NET_BIND_SERVICE",
            "NET_BROADCAST",
            "NET_RAW",
            "PERFMON",
            "SETFCAP",
            "SETGID",
            "SETPCAP",
            "SETUID",
            "SYS_ADMIN",
            "SYS_BOOT",
            "SYS_CHROOT",
            "SYS_MODULE",
            "SYS_NICE",
            "SYS_PACCT",
            "SYS_PTRACE",
            "SYS_RAWIO",
            "SYS_RESOURCE",
            "SYS_TIME",
            "SYS_TTY_CONFIG",
            "SYSLOG",
            "WAKE_ALARM",
        }
        normalized = sorted({value.upper().removeprefix("CAP_") for value in values})
        if any(value not in capabilities for value in normalized):
            raise ValueError(
                "Capability exceptions must be exact Linux capability names."
            )
        return normalized

    @model_validator(mode="after")
    def coherent_controls(self) -> HardeningPolicy:
        if self.cap_add and not self.cap_drop_all:
            raise ValueError(
                "Enable cap-drop=ALL before adding exact capability exceptions."
            )
        if self.read_only_tmpfs_false and not self.read_only_rootfs:
            raise ValueError(
                "Disable automatic tmpfs only with a read-only root filesystem."
            )
        if self.tmpfs_paths == "none" and any(
            (self.tmpfs_noexec, self.tmpfs_nosuid, self.tmpfs_nodev)
        ):
            raise ValueError(
                "Choose explicit tmpfs paths before setting their mount options."
            )
        if self.ipc == "none" and self.shm_size_mib is not None:
            raise ValueError("Shared-memory size cannot be set with IPC disabled.")
        return self

    def persisted(self) -> str:
        return json.dumps(
            self.model_dump(exclude_defaults=True),
            sort_keys=True,
            separators=(",", ":"),
        )


def read_hardening_policy(backend: Backend) -> HardeningPolicy:
    return HardeningPolicy.model_validate_json(backend.hardening_config_json or "{}")


def hardening_podman_args(policy: HardeningPolicy) -> list[str]:
    flags = []
    for field, flag in (
        ("cap_drop_all", "--cap-drop=ALL"),
        ("no_new_privileges", "--security-opt=no-new-privileges"),
        ("read_only_rootfs", "--read-only"),
        ("read_only_tmpfs_false", "--read-only-tmpfs=false"),
        ("no_hosts_file_if_safe", "--no-hosts"),
        ("pid_private", "--pid=private"),
        ("cgroupns_private", "--cgroupns=private"),
        ("uts_private", "--uts=private"),
    ):
        if getattr(policy, field):
            flags.append(flag)
    flags.extend(f"--cap-add={capability}" for capability in policy.cap_add)
    if policy.ipc != "default":
        flags.append(f"--ipc={policy.ipc}")
    if policy.apparmor_profile:
        flags.append(f"--security-opt=apparmor={policy.apparmor_profile}")
    if policy.pids_limit is not None:
        flags.append(f"--pids-limit={policy.pids_limit}")
    if policy.nofile_ulimit is not None:
        flags.append(f"--ulimit=nofile={policy.nofile_ulimit}:{policy.nofile_ulimit}")
    if policy.shm_size_mib is not None:
        flags.append(f"--shm-size={policy.shm_size_mib}m")
    for path, size in (("/tmp", "128m"), ("/run", "32m")):
        if policy.tmpfs_paths not in (path[1:], "tmp_run"):
            continue
        options = ["rw", f"size={size}"]
        options.extend(
            option
            for option in ("noexec", "nosuid", "nodev")
            if getattr(policy, f"tmpfs_{option}")
        )
        flags.append(f"--tmpfs={path}:{','.join(options)}")
    return flags


def hardening_volumes(policy: HardeningPolicy, volumes: list[str]) -> list[str]:
    result = []
    for volume in volumes:
        parts = volume.split(":", 2)
        options = parts[2].split(",") if len(parts) > 2 else []
        for option, opposite in (
            ("ro", "rw"),
            ("noexec", "exec"),
            ("nosuid", "suid"),
            ("nodev", "dev"),
        ):
            if getattr(policy, f"existing_mounts_{option}"):
                options = [
                    value for value in options if value not in (option, opposite)
                ]
                options.append(option)
        result.append(
            ":".join(parts[:2]) + (":" + ",".join(options) if options else "")
        )
    return result


def hardening_revision(backend: Backend, summary: dict) -> str:
    # Bind review to both the saved output and the exact persisted evidence.
    payload = {
        "backend": {
            column.name: getattr(backend, column.name)
            for column in backend.__table__.columns
        },
        "phase1": summary.get("phase1"),
        "phase2": summary.get("phase2"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


POLICY_CONTROLS = (
    ("cap_drop_all", "Drop all capabilities", "--cap-drop=ALL", "boolean", ()),
    ("cap_add", "Capability exceptions", "--cap-add", "capabilities", ()),
    (
        "no_new_privileges",
        "No new privileges",
        "--security-opt=no-new-privileges",
        "boolean",
        (),
    ),
    ("apparmor_profile", "AppArmor profile", "--security-opt=apparmor", "text", ()),
    ("read_only_rootfs", "Read-only root filesystem", "--read-only", "boolean", ()),
    (
        "read_only_tmpfs_false",
        "Disable automatic writable tmpfs",
        "--read-only-tmpfs=false",
        "boolean",
        (),
    ),
    (
        "tmpfs_paths",
        "Explicit tmpfs (128 / 32 MiB)",
        "--tmpfs",
        "select",
        ("none", "tmp", "run", "tmp_run"),
    ),
    ("tmpfs_noexec", "Explicit tmpfs: no execution", "noexec", "boolean", ()),
    ("tmpfs_nosuid", "Explicit tmpfs: no setuid", "nosuid", "boolean", ()),
    ("tmpfs_nodev", "Explicit tmpfs: no devices", "nodev", "boolean", ()),
    ("existing_mounts_ro", "All configured mounts: read-only", "ro", "boolean", ()),
    (
        "existing_mounts_noexec",
        "All configured mounts: no execution",
        "noexec",
        "boolean",
        (),
    ),
    (
        "existing_mounts_nosuid",
        "All configured mounts: no setuid",
        "nosuid",
        "boolean",
        (),
    ),
    (
        "existing_mounts_nodev",
        "All configured mounts: no devices",
        "nodev",
        "boolean",
        (),
    ),
    (
        "no_hosts_file_if_safe",
        "Disable generated hosts file",
        "--no-hosts",
        "boolean",
        (),
    ),
    ("pid_private", "Private PID namespace", "--pid=private", "boolean", ()),
    ("ipc", "IPC namespace", "--ipc", "select", ("default", "private", "none")),
    (
        "cgroupns_private",
        "Private cgroup namespace",
        "--cgroupns=private",
        "boolean",
        (),
    ),
    ("uts_private", "Private hostname namespace", "--uts=private", "boolean", ()),
    ("pids_limit", "Process / thread limit", "--pids-limit", "number", ()),
    ("nofile_ulimit", "Open-file limit", "--ulimit=nofile", "number", ()),
    ("shm_size_mib", "Shared memory (MiB)", "--shm-size", "number", ()),
)

ADVISOR_MANAGED = {
    "apparmor_confined": "CNC manages the confined Ubuntu systemd profile.",
    "userns_auto_or_nomap": "CNC assigns each Ubuntu guest its own user namespace.",
    "mount_idmap_or_U_only_if_needed": "CNC preserves guest ownership through idmapped storage.",
    "privileged_false": "CNC does not enable privileged mode.",
    "seccomp_not_unconfined": "CNC keeps the runtime seccomp default; it does not request unconfined.",
    "selinux_label_separation": "CNC keeps runtime labeling defaults.",
    "selinux_nested_disabled": "CNC does not enable nested SELinux labeling.",
    "default_masked_paths": "CNC keeps runtime path masks.",
    "unmask_all_disabled": "CNC does not unmask all kernel paths.",
    "no_extra_host_devices": "CNC does not pass additional host devices.",
    "no_device_cgroup_rules_unless_hit": "CNC does not add device cgroup rules.",
    "isolated_network_not_host": "CNC manages isolated app networks.",
    "no_publish_all": "CNC publishes only the configured handoff port.",
    "no_add_host_unless_hit": "CNC does not add host aliases.",
    "no_keep_groups_unless_hit": "CNC does not forward host supplementary groups.",
    "no_custom_sysctls": "CNC does not add custom sysctls.",
    "memory_limit": "Memory limits are managed by the output's resource settings.",
    "env_host_false": "CNC does not forward the host environment.",
}
ADVISOR_UNAVAILABLE = {
    "seccomp_custom_profile": "Requires a reviewed, installed syscall profile; no profile is selected automatically.",
    "exact_device_permissions_if_needed": "Requires an exact device and permission review; CNC does not pass host devices.",
    "network_none": "Would remove the networking CNC needs for app routing and SSH access.",
    "nonroot_user": "CNC's systemd guest starts as root; set app-service users inside the guest.",
}


def validate_policy_for_backend(backend: Backend, policy: HardeningPolicy) -> None:
    from app.services.validators import parse_volumes_json

    if backend.kind != "app" and policy.persisted() != "{}":
        raise ValueError("Hardening configuration is only available for app outputs.")
    paths = {"tmp": ("/tmp",), "run": ("/run",), "tmp_run": ("/tmp", "/run")}.get(
        policy.tmpfs_paths, ()
    )
    for volume in parse_volumes_json(backend.volumes_json):
        target = volume.split(":")[1].rstrip("/")
        if any(
            target == path
            or target.startswith(path + "/")
            or path.startswith(target + "/")
            for path in paths
        ):
            raise ValueError(
                "Explicit tmpfs overlaps a configured mount. Review the mount before applying."
            )


def recommended_hardening_policy(
    backend: Backend, summary: dict
) -> tuple[HardeningPolicy, list[dict[str, str]]]:
    phase1 = summary.get("phase1") or {}
    if phase1.get("status") != "success":
        raise ValueError("Complete a Phase 1 baseline before applying recommendations.")
    phase2 = summary.get("phase2") or {}
    current = read_hardening_policy(backend).model_dump()
    skipped = []
    recommended = set()
    for row in summary.get("features", []):
        if row.get("recommendation") != "recommended":
            continue
        setting = row["setting"]
        if row.get("phase2") and phase2.get("status") != "success":
            skipped.append(
                {
                    "setting": setting,
                    "reason": "The Phase 2 run did not finish successfully; review this setting manually.",
                }
            )
        else:
            recommended.add(setting)

    def skip(setting: str, reason: str) -> None:
        skipped.append({"setting": setting, "reason": reason})

    boolean_fields = {
        key
        for key, _label, _flag, kind, _choices in POLICY_CONTROLS
        if kind == "boolean"
    }
    for setting in sorted(recommended):
        if setting in boolean_fields:
            current[setting] = True
        elif setting in ADVISOR_MANAGED:
            skip(setting, ADVISOR_MANAGED[setting])
        elif setting in ADVISOR_UNAVAILABLE:
            skip(setting, ADVISOR_UNAVAILABLE[setting])
        elif setting == "cap_add_exact_exceptions":
            skip(
                setting,
                "Choose exact capability exceptions in the flag editor; the advisor does not supply a proven capability list.",
            )
        elif setting == "explicit_tmpfs_paths":
            current["tmpfs_paths"] = "tmp_run"
        elif setting in ("ipc_none", "ipc_private"):
            # Select one namespace mode; never emit contradictory IPC flags.
            current["ipc"] = "none" if "ipc_none" in recommended else "private"
        elif setting in ("pids_limit", "nofile_ulimit", "shm_size"):
            values = (phase1.get("details") or {}).get("values") or {}
            value = values.get(
                {
                    "pids_limit": "pids_limit",
                    "nofile_ulimit": "nofile_limit",
                    "shm_size": "shm_size",
                }[setting]
            )
            if setting == "shm_size":
                if (
                    isinstance(value, str)
                    and value.lower().endswith("m")
                    and value[:-1].isdigit()
                ):
                    current["shm_size_mib"] = int(value[:-1])
                else:
                    skip(
                        setting,
                        "No exact shared-memory size in MiB is available; set it manually.",
                    )
            elif type(value) is int:
                current[setting] = value
            else:
                skip(setting, "No exact numeric limit is available; set it manually.")

    if current["read_only_tmpfs_false"] and not current["read_only_rootfs"]:
        current["read_only_tmpfs_false"] = False
        skip("read_only_tmpfs_false", "Requires a reviewed read-only root filesystem.")
    if current["tmpfs_paths"] == "none":
        for setting in ("tmpfs_noexec", "tmpfs_nosuid", "tmpfs_nodev"):
            if current[setting]:
                current[setting] = False
                skip(
                    setting,
                    "Choose explicit tmpfs paths before setting their mount options.",
                )
    if current["ipc"] == "none" and current["shm_size_mib"] is not None:
        # Keep an existing shared-memory allocation rather than silently removing it.
        if read_hardening_policy(backend).shm_size_mib is not None:
            current["ipc"] = read_hardening_policy(backend).ipc
            skip("ipc_none", "Conflicts with the saved shared-memory allocation.")
        else:
            current["shm_size_mib"] = None
            skip("shm_size", "IPC none disables shared memory.")
    policy = HardeningPolicy.model_validate(current)
    validate_policy_for_backend(backend, policy)
    return policy, skipped


def hardening_configuration(backend: Backend, summary: dict) -> dict:
    policy = read_hardening_policy(backend)
    return {
        "current": policy.model_dump(),
        "has_previous": backend.hardening_previous_json is not None,
        "revision": hardening_revision(backend, summary),
        "controls": [
            {"key": key, "label": label, "flag": flag, "type": kind, "choices": choices}
            for key, label, flag, kind, choices in POLICY_CONTROLS
        ],
        "other_controls": [
            {"setting": setting, "state": state, "reason": reason}
            for state, rows in (
                ("CNC default", ADVISOR_MANAGED),
                ("Separate review", ADVISOR_UNAVAILABLE),
            )
            for setting, reason in rows.items()
        ],
    }


def hardening_preview(
    backend: Backend, summary: dict, mode: str, configuration: str = "{}"
) -> dict:
    from app.services.validators import parse_volumes_json

    if any(
        (summary.get(phase) or {}).get("status") in ("queued", "running")
        for phase in ("phase1", "phase2")
    ):
        raise ValueError(
            "Stop or finish the active hardening run before changing its configuration."
        )
    skipped: list[dict[str, str]] = []
    if mode == "recommended":
        policy, skipped = recommended_hardening_policy(backend, summary)
    elif mode == "previous":
        if backend.hardening_previous_json is None:
            raise ValueError("There is no previous hardening configuration to restore.")
        policy = HardeningPolicy.model_validate_json(backend.hardening_previous_json)
    elif mode == "manual":
        if len(configuration) > 16_384:
            raise ValueError("Hardening configuration is too large.")
        policy = HardeningPolicy.model_validate_json(configuration)
    else:
        raise ValueError("Unknown hardening configuration action.")
    validate_policy_for_backend(backend, policy)
    previous = read_hardening_policy(backend).model_dump()
    selected = policy.model_dump()
    volumes = parse_volumes_json(backend.volumes_json)
    changes = [
        {
            "setting": key,
            "label": label,
            "before": previous[key],
            "after": selected[key],
        }
        for key, label, _flag, _kind, _choices in POLICY_CONTROLS
        if previous[key] != selected[key]
    ]
    return {
        "revision": hardening_revision(backend, summary),
        "mode": mode,
        "configuration": selected,
        "changes": changes,
        "skipped": skipped,
        "flags": hardening_podman_args(policy),
        "mounts": hardening_volumes(policy, volumes),
        "will_restart": bool(backend.enabled and changes),
    }
