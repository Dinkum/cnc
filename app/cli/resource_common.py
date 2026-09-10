from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from pydantic import ValidationError as SchemaValidationError
from sqlalchemy.exc import IntegrityError

from app.cli.common import bind_command
from app.config import Settings
from app.schemas.backends import BackendIn, BackendUpdate
from app.schemas.inputs import InputIn, InputUpdate
from app.services import cli_resources
from app.services.operations import HostMutationLockError
from app.services.sandbox_profiles import default_app_sandbox_profile
from app.services.validators import ValidationError


def format_resource_text(payload: dict[str, object]) -> str:
    # A compact key/value object remains useful for full configuration inspection.
    return json.dumps(payload, indent=2, sort_keys=True)


def bind_resource(
    parser: argparse.ArgumentParser, *, resource: str, action: str
) -> None:
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.set_defaults(resource=resource, resource_action=action)
    bind_command(
        parser,
        handler=run_resource,
        formatter=format_resource_text,
        needs_settings=True,
    )


def register_resources(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], resource: str
) -> None:
    listing = subparsers.add_parser("list", help=f"List {resource}s")
    bind_resource(listing, resource=resource, action="list")
    show = subparsers.add_parser("show", help=f"Inspect one {resource}")
    show.add_argument(
        "reference", help="Output name" if resource == "output" else "Input ID or value"
    )
    bind_resource(show, resource=resource, action="show")
    for action in ("create", "update"):
        parser = subparsers.add_parser(action, help=f"{action.title()} an {resource}")
        parser.add_argument(
            "reference",
            help=(
                "Output name"
                if resource == "output"
                else "Input value"
                if action == "create"
                else "Input ID or value"
            ),
        )
        parser.add_argument(
            "--config",
            metavar="FILE",
            help="Read configuration JSON from FILE, or - for stdin; explicit flags take precedence",
        )
        parser.add_argument(
            "--enabled",
            action=argparse.BooleanOptionalAction,
            default=argparse.SUPPRESS,
        )
        if resource == "output":
            parser.add_argument(
                "--kind", choices=("app", "static", "shield"), default=argparse.SUPPRESS
            )
            parser.add_argument("--port", type=int, default=argparse.SUPPRESS)
            parser.add_argument("--handoff-port", type=int, default=argparse.SUPPRESS)
            parser.add_argument("--static-root", default=argparse.SUPPRESS)
            parser.add_argument(
                "--resource-mode", choices=("auto", "manual"), default=argparse.SUPPRESS
            )
            parser.add_argument(
                "--resource-size",
                choices=("small", "medium", "large"),
                default=argparse.SUPPRESS,
            )
            parser.add_argument("--notes", default=argparse.SUPPRESS)
        else:
            parser.add_argument(
                "--kind",
                choices=("domain", "tailnet_path", "tailnet_service", "shield"),
                default=argparse.SUPPRESS,
            )
            if action == "update":
                parser.add_argument("--value", default=argparse.SUPPRESS)
        bind_resource(parser, resource=resource, action=action)


def configuration(
    args: argparse.Namespace,
) -> BackendIn | BackendUpdate | InputIn | InputUpdate:
    data: dict[str, object] = {}
    if args.config:
        try:
            text = (
                sys.stdin.read()
                if args.config == "-"
                else Path(args.config).read_text(encoding="utf-8")
            )
            value = json.loads(text)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("cannot read configuration JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("configuration must be a JSON object")
        data.update(value)
    schema = (
        (BackendIn if args.resource_action == "create" else BackendUpdate)
        if args.resource == "output"
        else (InputIn if args.resource_action == "create" else InputUpdate)
    )
    if args.resource == "input" and "output_ids" in data:
        if "backend_ids" in data:
            raise ValueError("use only output_ids for input connections")
        data["backend_ids"] = data.pop("output_ids")
    # Reject typos instead of letting Pydantic's HTTP-compatible extra policy ignore them.
    unknown = set(data) - set(schema.model_fields)
    if unknown:
        raise ValueError("unknown configuration fields: " + ", ".join(sorted(unknown)))
    for key in (
        "kind",
        "enabled",
        "port",
        "handoff_port",
        "static_root",
        "resource_mode",
        "resource_size",
        "notes",
        "value",
    ):
        if hasattr(args, key):
            data[key] = getattr(args, key)
    if args.resource_action == "create":
        data["name" if args.resource == "output" else "value"] = args.reference
        data.setdefault("kind", "app" if args.resource == "output" else "domain")
        if args.resource == "output" and data["kind"] == "app":
            data.setdefault("sandbox_profile", default_app_sandbox_profile())
    if not data:
        raise ValueError("supply a configuration field to update")
    try:
        return schema.model_validate(data)
    except SchemaValidationError as exc:
        # Validation errors must not repeat supplied access codes or other secret values.
        errors = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in exc.errors(include_input=False)
        )
        raise ValueError(errors) from exc


async def run_resource(
    args: argparse.Namespace, settings: Settings | None
) -> tuple[int, dict[str, object] | None, str | None]:
    assert settings is not None
    try:
        action = args.resource_action
        if args.resource == "route":
            payload = (
                await cli_resources.read_routes(settings)
                if action == "list"
                else await cli_resources.change_route(
                    settings, args.input, args.output, connect=action == "connect"
                )
            )
        elif action in {"list", "show"}:
            payload = await cli_resources.read_resources(
                settings, args.resource, getattr(args, "reference", None)
            )
        else:
            payload = await cli_resources.mutate_resource(
                settings, args.resource, action, configuration(args), args.reference
            )
        return 0, payload, None
    except HostMutationLockError as exc:
        return 1, None, str(exc)
    except IntegrityError:
        return 1, None, "configuration conflicts with an existing resource"
    except (LookupError, ValueError, ValidationError) as exc:
        return 1, None, str(exc)
