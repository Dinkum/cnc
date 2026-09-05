from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings


@dataclass(frozen=True)
class SandboxProvisionStep:
    step_id: str
    label: str
    command: str


@dataclass(frozen=True)
class SandboxProfile:
    profile_id: str
    label: str
    seed_image: str
    init_command: tuple[str, ...]
    seed_revision: int = 1
    provision_steps: tuple[SandboxProvisionStep, ...] = ()
    self_test_command: tuple[str, ...] = ()
    multi_user_target: str = "multi-user.target"

    @property
    def provision_script(self) -> str:
        return "; ".join(step.command for step in self.provision_steps)


UBUNTU_24_04_SYSTEMD_PROFILE = "ubuntu-24.04-systemd"

_APP_SANDBOX_PROFILES: dict[str, SandboxProfile] = {
    UBUNTU_24_04_SYSTEMD_PROFILE: SandboxProfile(
        profile_id=UBUNTU_24_04_SYSTEMD_PROFILE,
        label="Ubuntu 24.04 + systemd",
        seed_image="docker.io/library/ubuntu:24.04",
        init_command=("/sbin/init",),
        seed_revision=4,
        provision_steps=(
            SandboxProvisionStep(
                step_id="provision_package_catalog",
                label="Preparing package catalog",
                command="export DEBIAN_FRONTEND=noninteractive; apt-get update",
            ),
            SandboxProvisionStep(
                step_id="provision_guest_runtime",
                label="Installing guest runtime",
                command=(
                    "export DEBIAN_FRONTEND=noninteractive; "
                    "apt-get install -y systemd systemd-sysv dbus"
                ),
            ),
            SandboxProvisionStep(
                step_id="provision_guest_tools",
                label="Installing guest tools",
                command=(
                    "export DEBIAN_FRONTEND=noninteractive; "
                    "apt-get install -y "
                    "bash ca-certificates curl git jq procps psmisc ripgrep sqlite3 sudo wget "
                    "dnsutils iproute2 iputils-ping"
                ),
            ),
            SandboxProvisionStep(
                step_id="provision_package_cleanup",
                label="Cleaning package cache",
                command="apt-get clean; rm -rf /var/lib/apt/lists/*",
            ),
        ),
        self_test_command=(
            "/bin/bash",
            "-lc",
            "test -x /sbin/init && systemctl --version >/dev/null",
        ),
    ),
}


def default_app_sandbox_profile() -> str:
    return UBUNTU_24_04_SYSTEMD_PROFILE


def app_sandbox_profiles() -> tuple[SandboxProfile, ...]:
    return tuple(_APP_SANDBOX_PROFILES.values())


def sandbox_profile_ids() -> tuple[str, ...]:
    return tuple(_APP_SANDBOX_PROFILES)


def get_app_sandbox_profile(profile_id: str | None) -> SandboxProfile:
    normalized = str(profile_id or "").strip().lower()
    if not normalized:
        normalized = default_app_sandbox_profile()
    try:
        return _APP_SANDBOX_PROFILES[normalized]
    except KeyError as exc:
        raise ValueError(f"unknown sandbox profile: {profile_id}") from exc


def app_sandbox_dir(settings: Settings, backend_name: str) -> Path:
    return settings.app_sandbox_dir / backend_name


def app_sandbox_rootfs_path(settings: Settings, backend_name: str) -> Path:
    return app_sandbox_dir(settings, backend_name) / "rootfs"


def app_sandbox_profile_state_path(settings: Settings, backend_name: str) -> Path:
    return app_sandbox_dir(settings, backend_name) / "profile.json"


def render_sandbox_profile_state(profile: SandboxProfile) -> str:
    return json.dumps(
        {
            "sandbox_profile": profile.profile_id,
            "seed_image": profile.seed_image,
            "init_command": list(profile.init_command),
            "seed_revision": profile.seed_revision,
        },
        indent=2,
        sort_keys=True,
    )
