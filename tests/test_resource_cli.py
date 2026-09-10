import argparse
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.cli import input as input_cli, output as output_cli, route as route_cli
from app.cli.resource_common import configuration, run_resource
from app.config import Settings
from app.database import Base, create_configured_async_engine
from app.models.entities import Operation
from app.schemas.backends import BackendIn, BackendUpdate
from app.schemas.inputs import InputIn
from app.services import cli_resources
from app.services.sandbox_profiles import default_app_sandbox_profile
from app.services.backend_commands import validate_config_graph
from app.services.operations import HostMutationLockError


def parse(*argv):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(required=True)
    for module in (input_cli, output_cli, route_cli):
        module.register(commands)
    return parser.parse_args(argv)


@pytest_asyncio.fixture
async def resources(tmp_path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'data.db'}",
        apply_lock_path=tmp_path / "apply.lock",
        app_control_dir=tmp_path / "control",
    )
    engine = create_configured_async_engine(settings.database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    applies = []

    async def apply(session, settings, *, operation, operation_handle):
        assert operation_handle.id is not None
        await validate_config_graph(session)
        await session.commit()
        applies.append(operation)
        return SimpleNamespace(applied=True)

    monkeypatch.setattr(cli_resources, "commit_and_apply", apply)
    return settings, applies


async def seed(resources):
    settings, _ = resources
    await cli_resources.mutate_resource(
        settings,
        "output",
        "create",
        BackendIn(
            name="first",
            kind="app",
            sandbox_profile=default_app_sandbox_profile(),
            port=18081,
        ),
    )
    await cli_resources.mutate_resource(
        settings,
        "output",
        "create",
        BackendIn(
            name="second",
            kind="app",
            sandbox_profile=default_app_sandbox_profile(),
            port=18082,
        ),
    )
    result = await cli_resources.mutate_resource(
        settings, "input", "create", InputIn(value="demo.example.com")
    )
    return str(result["input"]["id"])


def test_configuration_overrides_file_and_preserves_omission(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"port": 18080, "enabled": False, "notes": "keep"}))
    args = parse("output", "update", "demo", "--config", str(path), "--enabled")
    assert configuration(args).model_dump(exclude_unset=True) == {
        "port": 18080,
        "enabled": True,
        "notes": "keep",
    }
    args = parse("input", "update", "1", "--no-enabled")
    assert configuration(args).model_dump(exclude_unset=True) == {"enabled": False}


def test_configuration_rejects_typos_and_does_not_echo_invalid_secret(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"enabledd": True}))
    with pytest.raises(ValueError, match="unknown configuration fields"):
        configuration(
            parse("input", "create", "demo.example.com", "--config", str(path))
        )
    path.write_text(json.dumps({"shield_access_code": {"secret": "never-print"}}))
    with pytest.raises(ValueError) as failure:
        configuration(
            parse("input", "create", "demo.example.com", "--config", str(path))
        )
    assert "never-print" not in str(failure.value)


def test_output_retains_exec_and_diagnostic_handlers():
    args = parse("output", "exec", "demo", "--", "printf", "hello")
    assert args.backend == "demo"
    assert args.exec_args[-2:] == ["printf", "hello"]
    assert callable(parse("output", "doctor", "demo", "--json")._cli_handler)


@pytest.mark.asyncio
async def test_routes_preserve_other_links_and_retries_do_not_apply(resources):
    reference = await seed(resources)
    settings, applies = resources
    await cli_resources.change_route(settings, reference, "first", connect=True)
    await cli_resources.change_route(settings, reference, "second", connect=True)
    count = len(applies)
    repeated = await cli_resources.change_route(
        settings, reference, "first", connect=True
    )
    assert repeated["changed"] is False
    assert len(applies) == count
    routes = await cli_resources.read_routes(settings)
    assert [item["output"] for item in routes["routes"]] == ["first", "second"]
    await cli_resources.change_route(settings, reference, "first", connect=False)
    routes = await cli_resources.read_routes(settings)
    assert [item["output"] for item in routes["routes"]] == ["second"]
    count = len(applies)
    assert not (
        await cli_resources.change_route(settings, reference, "first", connect=False)
    )["changed"]
    assert len(applies) == count


@pytest.mark.asyncio
async def test_failed_apply_rolls_back_and_marks_operation_failed(
    resources, monkeypatch
):
    reference = await seed(resources)
    settings, _ = resources

    async def fail(session, settings, **kwargs):
        await session.flush()
        return SimpleNamespace(
            applied=False, apply_response=SimpleNamespace(message="apply failed")
        )

    monkeypatch.setattr(cli_resources, "commit_and_apply", fail)
    with pytest.raises(ValueError, match="apply failed"):
        await cli_resources.change_route(settings, reference, "first", connect=True)
    assert (await cli_resources.read_routes(settings))["routes"] == []
    async with cli_resources.resource_session(settings) as session:
        last = (
            await session.execute(
                select(Operation).order_by(Operation.id.desc()).limit(1)
            )
        ).scalar_one()
        assert last.kind == "cli.route.connect"
        assert last.status == "failed"


@pytest.mark.asyncio
async def test_inspection_excludes_secrets_and_update_preserves_fields(resources):
    settings, _ = resources
    await cli_resources.mutate_resource(
        settings,
        "output",
        "create",
        BackendIn(
            name="first",
            kind="app",
            sandbox_profile=default_app_sandbox_profile(),
            port=18081,
            notes="keep",
        ),
    )
    await cli_resources.mutate_resource(
        settings, "output", "update", BackendUpdate(enabled=False), "first"
    )
    payload = await cli_resources.read_resources(settings, "output", "first")
    assert payload["output"]["notes"] == "keep"
    assert payload["output"]["port"] == 18081
    assert payload["output"]["enabled"] is False
    assert "shield_code_hash" not in payload["output"]
    assert "shield_access_code" not in payload["output"]
    assert "ssh_private_key" not in payload["output"]


@pytest.mark.asyncio
async def test_lock_conflict_precedes_resource_reads(resources, monkeypatch):
    settings, _ = resources
    from app.services.file_locks import FileLock

    with FileLock(
        settings.apply_lock_path, blocking=False, lock_path=settings.apply_lock_path
    ):
        with pytest.raises(HostMutationLockError):
            await cli_resources.change_route(
                settings, "missing", "missing", connect=True
            )


@pytest.mark.asyncio
async def test_one_operation_per_mutation(resources):
    settings, _ = resources
    result = await run_resource(
        parse("input", "create", "demo.example.com", "--json"), settings
    )
    assert result[0] == 0
    async with cli_resources.resource_session(settings) as session:
        operations = (await session.execute(select(Operation))).scalars().all()
        assert len(operations) == 1
        assert operations[0].status == "success"


def test_output_create_defaults_and_input_output_vocabulary(tmp_path):
    result = configuration(parse("output", "create", "demo"))
    assert result.sandbox_profile == default_app_sandbox_profile()
    assert result.kind == "app"
    path = tmp_path / "config.json"
    path.write_text('{"output_ids": [2, 3]}')
    result = configuration(parse("input", "update", "1", "--config", str(path)))
    assert result.backend_ids == [2, 3]
