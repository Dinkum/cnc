from __future__ import annotations

import argparse

from app.cli.resource_common import bind_resource


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("route", help="Connect inputs to outputs")
    commands = parser.add_subparsers(dest="route_command", required=True)
    listing = commands.add_parser("list", help="List input-to-output connections")
    bind_resource(listing, resource="route", action="list")
    for action in ("connect", "disconnect"):
        command = commands.add_parser(action)
        command.add_argument("input", help="Input ID or value")
        command.add_argument("output", help="Output name")
        bind_resource(command, resource="route", action=action)
