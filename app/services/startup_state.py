from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import FastAPI


STARTUP_PHASES = ("config", "db", "runtime_assets", "self_audit")
_STARTUP_STATE_ATTR = "cnc_startup_state"


def initialize_startup_state(app: FastAPI) -> dict[str, dict[str, Any]]:
    state = {
        phase: {
            "status": "pending",
            "error": "",
        }
        for phase in STARTUP_PHASES
    }
    setattr(app.state, _STARTUP_STATE_ATTR, state)
    return state


def get_startup_state(app: FastAPI) -> dict[str, dict[str, Any]]:
    state = getattr(app.state, _STARTUP_STATE_ATTR, None)
    if isinstance(state, dict):
        return state
    return initialize_startup_state(app)


def snapshot_startup_state(app: FastAPI) -> dict[str, dict[str, Any]]:
    return deepcopy(get_startup_state(app))


def set_startup_phase_state(app: FastAPI, phase: str, payload: dict[str, Any]) -> None:
    if phase not in STARTUP_PHASES:
        raise ValueError(f"unknown startup phase: {phase}")
    state = get_startup_state(app)
    state[phase] = dict(payload)
