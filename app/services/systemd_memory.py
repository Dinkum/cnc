from __future__ import annotations

import re
from typing import Any


_MEMORY_VALUE_RE = re.compile(
    r"^(?P<number>\d+(?:\.\d+)?)(?P<unit>[KMGTP]I?B?|B)?$",
    re.IGNORECASE,
)
_MEMORY_MULTIPLIERS = {
    "B": 1,
    "K": 1024,
    "KB": 1000,
    "KI": 1024,
    "KIB": 1024,
    "M": 1024**2,
    "MB": 1000**2,
    "MI": 1024**2,
    "MIB": 1024**2,
    "G": 1024**3,
    "GB": 1000**3,
    "GI": 1024**3,
    "GIB": 1024**3,
    "T": 1024**4,
    "TB": 1000**4,
    "TI": 1024**4,
    "TIB": 1024**4,
    "P": 1024**5,
    "PB": 1000**5,
    "PI": 1024**5,
    "PIB": 1024**5,
}
_UNLIMITED_VALUES = {"infinity", "max", "infinite", "[not set]", ""}


class SystemdMemoryPolicyError(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.details = details


def memory_value_bytes(value: int | str | None) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    normalized = str(value or "").strip()
    if normalized.lower() in _UNLIMITED_VALUES:
        return None
    match = _MEMORY_VALUE_RE.match(normalized)
    if match is None:
        return None
    number = float(match.group("number"))
    unit = (match.group("unit") or "B").upper()
    multiplier = _MEMORY_MULTIPLIERS.get(unit)
    if multiplier is None:
        return None
    return int(number * multiplier)


def _parse_show_properties(stdout: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key:
            properties[key] = value.strip()
    return properties


async def read_systemd_memory_policy(
    unit: str,
    *,
    services: Any,
    timeout_sec: float,
) -> dict[str, Any]:
    result = await services.run_command_checked_async(
        [
            "systemctl",
            "show",
            unit,
            "--property=ControlGroup,MemoryCurrent,MemoryHigh,MemoryMax",
        ],
        timeout_sec=timeout_sec,
    )
    properties = _parse_show_properties(result.stdout)
    return {
        "control_group": properties.get("ControlGroup", ""),
        "memory_current_bytes": memory_value_bytes(properties.get("MemoryCurrent")),
        "memory_high_bytes": memory_value_bytes(properties.get("MemoryHigh")),
        "memory_max_bytes": memory_value_bytes(properties.get("MemoryMax")),
    }


async def converge_systemd_memory_policy(
    unit: str,
    *,
    services: Any,
    timeout_sec: float,
    memory_high: int | str | None = None,
    memory_max: int | str | None = None,
    require_apps_slice: bool = False,
) -> dict[str, Any]:
    desired = {
        "MemoryHigh": memory_value_bytes(memory_high),
        "MemoryMax": memory_value_bytes(memory_max),
    }
    requested = {
        key: value
        for key, value in (("MemoryHigh", memory_high), ("MemoryMax", memory_max))
        if value is not None
    }
    if not requested or any(desired[key] is None for key in requested):
        raise ValueError(f"invalid systemd memory policy for {unit}")

    before = await read_systemd_memory_policy(
        unit, services=services, timeout_sec=timeout_sec
    )
    control_group = str(before.get("control_group") or "")
    if require_apps_slice and "cnc-apps.slice" not in control_group.split("/"):
        raise SystemdMemoryPolicyError(
            f"{unit} is not inside cnc-apps.slice",
            details={"unit": unit, "observed": before},
        )

    observed_keys = {
        "MemoryHigh": "memory_high_bytes",
        "MemoryMax": "memory_max_bytes",
    }
    changes = {
        key: value
        for key, value in requested.items()
        if before.get(observed_keys[key]) != desired[key]
    }
    if changes:
        await services.run_command_checked_async(
            [
                "systemctl",
                "set-property",
                "--runtime",
                unit,
                *(f"{key}={value}" for key, value in changes.items()),
            ],
            timeout_sec=timeout_sec,
        )

    after = await read_systemd_memory_policy(
        unit, services=services, timeout_sec=timeout_sec
    )
    mismatches = {
        key: {
            "expected_bytes": desired[key],
            "observed_bytes": after.get(observed_keys[key]),
        }
        for key in requested
        if after.get(observed_keys[key]) != desired[key]
    }
    if mismatches:
        raise SystemdMemoryPolicyError(
            f"systemd memory policy did not converge for {unit}",
            details={
                "unit": unit,
                "control_group": after.get("control_group") or control_group,
                "mismatches": mismatches,
                "observed_before": before,
                "observed_after": after,
            },
        )

    desired_max = desired.get("MemoryMax")
    current_before = before.get("memory_current_bytes")
    return {
        "unit": unit,
        "control_group": after.get("control_group") or control_group,
        "changed": bool(changes),
        "changed_properties": sorted(changes),
        "memory_current_bytes": after.get("memory_current_bytes"),
        "memory_high_bytes": after.get("memory_high_bytes"),
        "memory_max_bytes": after.get("memory_max_bytes"),
        "memory_max_lowered_below_usage": bool(
            "MemoryMax" in changes
            and isinstance(current_before, int)
            and isinstance(desired_max, int)
            and current_before > desired_max
        ),
        "verified": True,
    }
