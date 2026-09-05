from __future__ import annotations

from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_SCRIPT = REPO_ROOT / "scripts" / "update_from_github.sh"


def _load_env_with_script(env_file: Path) -> subprocess.CompletedProcess[str]:
    command = f'''
set -Eeuo pipefail
export ENV_FILE="{env_file}"
source "{UPDATE_SCRIPT}"
load_env
printf 'ACCESS_KEY_HASH=%s\\n' "${{ACCESS_KEY_HASH:-}}"
printf 'OPENAI_MODEL=%s\\n' "${{OPENAI_MODEL:-}}"
printf 'GITHUB_READONLY_PAT=%s\\n' "${{GITHUB_READONLY_PAT:-}}"
'''
    return subprocess.run(
        ["bash", "-lc", command],
        check=False,
        capture_output=True,
        text=True,
    )


def test_update_script_load_env_treats_argon_hash_as_opaque_value(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "cnc.env"
    env_file.write_text(
        "\n".join(
            [
                "ACCESS_KEY_HASH=$argon2id$v=19$m=65536,t=3,p=4$abc$def",
                'OPENAI_MODEL="gpt-5-mini"',
                "GITHUB_READONLY_PAT=token123",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = _load_env_with_script(env_file)

    assert result.returncode == 0, result.stderr
    assert "ACCESS_KEY_HASH=$argon2id$v=19$m=65536,t=3,p=4$abc$def" in result.stdout
    assert "OPENAI_MODEL=gpt-5-mini" in result.stdout
    assert "GITHUB_READONLY_PAT=token123" in result.stdout


def test_update_script_load_env_ignores_comments_and_blank_lines(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "cnc.env"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "   ; another comment",
                "",
                "GITHUB_READONLY_PAT=token123",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = _load_env_with_script(env_file)

    assert result.returncode == 0, result.stderr
    assert "GITHUB_READONLY_PAT=token123" in result.stdout
