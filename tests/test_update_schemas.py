import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.dependencies import (
    csrf_api_dependency,
    db_session_dependency,
    settings_dependency,
)
from app.routes import backends, inputs
from app.schemas.backends import BackendUpdate
from app.schemas.inputs import InputUpdate


REQUIRED_UPDATES = [
    ("backends", BackendUpdate, field)
    for field in (
        "name",
        "kind",
        "handoff_port",
        "resource_mode",
        "resource_size",
        "shield_enabled",
        "volumes_json",
        "enabled",
    )
] + [
    ("inputs", InputUpdate, field)
    for field in ("kind", "value", "enabled", "shield_enabled")
]


@pytest.mark.parametrize("resource,schema,field", REQUIRED_UPDATES)
async def test_null_required_update_is_rejected_before_service(
    monkeypatch, resource, schema, field
):
    app = FastAPI()
    app.include_router(backends.router)
    app.include_router(inputs.router)
    app.dependency_overrides[db_session_dependency] = lambda: None
    app.dependency_overrides[settings_dependency] = lambda: Settings(_env_file=None)
    app.dependency_overrides[csrf_api_dependency] = lambda: None

    async def unexpected_update(*_args, **_kwargs):
        raise AssertionError("invalid body reached the update service")

    monkeypatch.setattr(backends.backend_commands, "update_backend", unexpected_update)
    monkeypatch.setattr(inputs.input_commands, "update_input", unexpected_update)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://example.com"
    ) as client:
        response = await client.post(f"/api/{resource}/1", json={field: None})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]
    # Optional in the PATCH-like API does not mean nullable on the wire.
    contract = schema.model_json_schema()
    assert field not in contract.get("required", [])
    assert "anyOf" not in contract["properties"][field]


@pytest.mark.parametrize(
    "schema,payload",
    [
        (BackendUpdate, {}),
        (InputUpdate, {}),
        (BackendUpdate, {"notes": None}),
        (BackendUpdate, {"port": None}),
        (BackendUpdate, {"shield_code_hash": None}),
        (InputUpdate, {"shield_code_hash": None}),
        (InputUpdate, {"shield_access_code": None}),
        (BackendUpdate, {"enabled": False}),
        (InputUpdate, {"enabled": False}),
    ],
)
def test_updates_preserve_omission_clearing_and_false(schema, payload):
    assert schema(**payload).model_dump(exclude_unset=True) == payload


def test_required_update_constraints_remain_in_openapi():
    fields = BackendUpdate.model_json_schema()["properties"]
    assert fields["handoff_port"]["minimum"] == 1
    assert fields["handoff_port"]["maximum"] == 65535
    assert fields["name"]["maxLength"] == 255
    assert fields["kind"]["pattern"] == "^(static|app|shield)$"
