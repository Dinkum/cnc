# ruff: noqa: F401

import asyncio
import json
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from starlette.requests import Request
from starlette.responses import HTMLResponse

from app import main as app_main
from app.access import (
    ACCESS_COOKIE_NAME,
    access_cookie_is_valid,
    access_key_is_configured,
    hash_access_key,
    set_access_cookie,
    verify_access_key,
)
from app.config import Settings
from app.database import Base
from app.models.entities import (
    Backend,
    BackendBackup,
    BackendHardeningRun,
    BackendResourceSample,
    ClusterNode,
    ControlEvent,
    HostResourceSample,
    Input,
    Operation,
)
from app.schemas.apply import ApplyResponse
from app.schemas.backends import BackendIn
from app.services import backend_commands
from app.services import backend_metric_history as metric_history
from app.services.app_containers import write_app_control_assets
from app.services.backend_backup_service import delete_backend_backup
from app.services.backend_metric_history import (
    build_backend_metric_history,
    build_host_metric_history,
)
from app.services.bootstrap_state import write_bootstrap_state
from app.services.notifications import mask_secret
from app.services.operation_runtime import (
    CREATE_BACKEND_OPERATION_STEPS,
    create_backend_progress_steps,
    create_backend_progress_value,
    create_backend_runtime_progress,
    create_backend_runtime_summary,
    create_output_progress_plan,
    delete_input_progress_plan,
    delete_output_progress_plan,
    input_progress_pipelines,
    operation_progress_pipelines,
    operation_progress_value,
    output_save_progress_steps,
    progress_plan_value,
)
from app.services.operations import create_operation
from app.services.replica_readiness import (
    record_replica_readiness,
    replica_target_url,
)
from app.services.sandbox_profiles import app_sandbox_dir
from app.services.shield_service import make_code_hash
from app.ui import view_models
from app.ui.forms import (
    DEFAULT_UI_APP_SANDBOX_PROFILE,
    _normalize_backend_form_sandbox_image,
)
from app.ui.operations import backups as backup_operations
from app.ui.operations import cluster as cluster_operations
from app.ui.operations import inputs as input_operations
from app.ui.operations import output_lifecycle as output_lifecycle_operations
from app.ui.operations import output_save as output_save_operations
from app.ui.progress import _CreateBackendProgressRecorder
from app.ui.routes import backup_mutations as ui_backup
from app.ui.routes import hardening_mutations as ui_hardening
from app.ui.routes import input_mutations as ui_input
from app.ui.routes import output_mutations as ui_output
from app.ui.routes import pages as ui_pages
from app.ui.routes import reads as ui_reads
from app.ui.routes import settings_mutations as ui_settings


def _dashboard_client_source() -> str:
    return Path("app/static/js/dashboard.js").read_text(encoding="utf-8")


async def _mark_replica_ready(
    session,
    settings: Settings,
    *,
    backend: Backend,
    node: ClusterNode,
    setup_mode: str = "clone",
) -> None:
    await record_replica_readiness(
        session,
        settings,
        backend=backend,
        node_uid=node.node_uid,
        setup_mode=setup_mode,
        target_url=replica_target_url(backend, node, node.node_uid),
        healthcheck_result=f"tcp reachable at {node.tailnet_ip}:{backend.port}",
    )
    await session.commit()


class _FormRequest:
    def __init__(self, fields: dict[str, list[str]] | None = None) -> None:
        self._fields = fields or {}
        self.headers: dict[str, str] = {}

    async def form(self):
        return SimpleNamespace(
            get=lambda key, default=None: (
                self._fields.get(key, [default]) or [default]
            )[0],
            getlist=lambda key: list(self._fields.get(key, [])),
        )


class _PostedRequest(Request):
    def __init__(
        self,
        *,
        path: str,
        fields: dict[str, list[str]] | None = None,
        headers: list[tuple[bytes, bytes]] | None = None,
        server: tuple[str, int] = ("127.0.0.1", 9090),
    ) -> None:
        split = urlsplit(path)
        super().__init__(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": split.path,
                "raw_path": split.path.encode(),
                "query_string": split.query.encode(),
                "headers": headers or [],
                "client": ("127.0.0.1", 12345),
                "server": server,
            }
        )
        self._fields = fields or {}

    async def form(self):
        return SimpleNamespace(
            get=lambda key, default=None: (
                self._fields.get(key, [default]) or [default]
            )[0],
            getlist=lambda key: list(self._fields.get(key, [])),
        )


def _request(
    *,
    path: str = "/",
    headers: list[tuple[bytes, bytes]] | None = None,
    server: tuple[str, int] = ("127.0.0.1", 9090),
) -> Request:
    split = urlsplit(path)
    raw_path = split.path.encode()
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": split.path,
            "raw_path": raw_path,
            "query_string": split.query.encode(),
            "headers": headers or [],
            "client": ("127.0.0.1", 12345),
            "server": server,
        }
    )


def _set_cookie_headers(response) -> str:
    return "\n".join(
        value.decode("latin-1")
        for key, value in response.raw_headers
        if key.lower() == b"set-cookie"
    )


def _cookie_values(response) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in response.raw_headers:
        if key.lower() != b"set-cookie":
            continue
        cookie = SimpleCookie()
        cookie.load(value.decode("latin-1"))
        for name, morsel in cookie.items():
            values[name] = morsel.value
    return values


def _healthy_status(*, backend_count: int) -> dict[str, object]:
    return {
        "services": [],
        "nginx": {"ok": True, "data": {"ActiveState": "active", "SubState": "running"}},
        "resource_profile": {
            "mode": "auto",
            "backend_count": backend_count,
            "host_cpu_count": 2,
            "host_memory_bytes": 1024,
        },
        "resource_profile_drift": {"changed": False},
        "last_apply": {},
        "last_update": {},
    }


async def _make_session(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)
