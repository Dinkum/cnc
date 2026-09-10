from __future__ import annotations

import argparse

from app.cli import app
from app.cli.resource_common import register_resources


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("output", help="Manage outputs and run app commands")
    output_commands = parser.add_subparsers(dest="output_command", required=True)
    register_resources(output_commands, "output")
    app.register_runtime_commands(output_commands)
