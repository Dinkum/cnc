#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/../.." && pwd)"
image="${SHIELD_IMAGE:-localhost/cnc-shield:current}"

cd "$repo_root"
exec podman build -t "$image" -f packaging/shield/Containerfile .
