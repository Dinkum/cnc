from __future__ import annotations

import argparse

from app.cli.resource_common import register_resources


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("input", help="Manage traffic entrypoints")
    register_resources(
        parser.add_subparsers(dest="input_command", required=True), "input"
    )
