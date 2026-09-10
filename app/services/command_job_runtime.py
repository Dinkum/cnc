from __future__ import annotations

import shlex

from app.config import Settings
from app.services.command_job_files import (
    GUEST_JOB_ROOT,
    MAX_STREAM_BYTES,
    artifact_name,
)
from app.services.commands import CommandResult, run_podman_exec
from app.services.container_runtime import (
    inspect_container,
    inspect_result_reports_missing,
)
from app.services.renderers import container_name


CONTROL_TIMEOUT_SEC = 10


def unit_name(operation_id: int, execution_token: str) -> str:
    return f"cnc-command-{artifact_name(operation_id, execution_token)}.service"


def observe_runtime(output: str) -> dict:
    result, payload = inspect_container(container_name(output), CONTROL_TIMEOUT_SEC)
    if inspect_result_reports_missing(result):
        return {"state": "missing"}
    if not result.ok or not payload or not isinstance(payload.get("State"), dict):
        return {"state": "unknown"}
    state = payload["State"]
    if not payload.get("Id") or not state.get("StartedAt"):
        return {"state": "unknown"}
    return {
        "state": "running" if state.get("Running") is True else "stopped",
        "container_id": payload["Id"],
        "started_at": state["StartedAt"],
    }


def guest_launch_script(
    operation_id: int,
    argv: list[str],
    timeout_sec: int | None,
    purge_jobs: list[tuple[int, str]] | None = None,
    *,
    execution_token: str,
) -> str:
    directory = f"/{GUEST_JOB_ROOT}/{artifact_name(operation_id, execution_token)}"
    invocation = shlex.join(argv)
    # Drain excess output instead of closing the pipe: an output cap must not
    # SIGPIPE an otherwise healthy command. Separate FIFOs retain stream identity.
    runner = f"""set -u
cd {shlex.quote(directory)} || exit 1
capture() {{
    head -c {MAX_STREAM_BYTES + 1} > "$1"
    if [ "$(wc -c < "$1")" -gt {MAX_STREAM_BYTES} ]; then
        head -c {MAX_STREAM_BYTES} "$1" > "$1.capped"
        mv "$1.capped" "$1"
        printf 1 > "$1.truncated"
    fi
    cat > /dev/null
}}
mkfifo stdout.pipe stderr.pipe || exit 1
capture stdout < stdout.pipe & out_pid=$!
capture stderr < stderr.pipe & err_pid=$!
(cd / && {invocation}) < /dev/null > stdout.pipe 2> stderr.pipe
code=$?
wait "$out_pid" "$err_pid"
exit "$code"
"""
    # ExecStopPost runs after the entire service cgroup is stopped, including on
    # cancellation and an explicit RuntimeMaxSec deadline. Do not infer timeout
    # or cancellation from an application's numeric exit code.
    finalizer = (
        f"umask 077; printf '%s\\t%s\\t%s\\n' "
        '"${SERVICE_RESULT:-resources}" "${EXIT_CODE:-none}" "${EXIT_STATUS:-none}" '
        f"> {directory}/result.tmp && mv {directory}/result.tmp {directory}/result"
    )
    # Keep shell expansion inside a script file. systemd-run serializes Exec*
    # properties itself; pre-escaping percent/dollar signs corrupts the record.
    finalizer_command = f"/bin/bash {directory}/finish"
    command = [
        "systemd-run",
        "--quiet",
        f"--unit={unit_name(operation_id, execution_token)}",
        "--property=Type=exec",
        "--property=KillMode=control-group",
        "--property=TimeoutStopSec=5s",
        "--property=Restart=no",
        "--property=StandardOutput=null",
        "--property=StandardError=null",
        f"--property=ExecStopPost={finalizer_command}",
    ]
    if timeout_sec is not None:
        command.append(f"--property=RuntimeMaxSec={timeout_sec}s")
    # A file avoids systemd's argv expansion of user shell text/$/% characters.
    command.extend(["--", "/bin/bash", f"{directory}/run"])
    cleanup = (
        'cleanup_job() {\n [[ "$1" =~ ^[1-9][0-9]*(-[0-9a-f]{32})?$ ]] || return\n'
    )
    cleanup += f' old=/{GUEST_JOB_ROOT}/"$1"\n'
    cleanup += ' [ -d "$old" ] && [ ! -L "$old" ] || return\n'
    cleanup += (
        " /usr/bin/rm -f -- "
        + " ".join(
            f'"$old/{name}"'
            for name in (
                "run",
                "finish",
                "result",
                "result.tmp",
                "stdout",
                "stderr",
                "stdout.capped",
                "stderr.capped",
                "stdout.truncated",
                "stderr.truncated",
                "stdout.pipe",
                "stderr.pipe",
            )
        )
        + "\n"
    )
    cleanup += ' rmdir -- "$old" 2>/dev/null || :\n}\n'
    # Include filesystem entries whose operation metadata has already expired.
    # Reconciliation before submission commits still-active completion records.
    cleanup += f"for old_id in $(find /{GUEST_JOB_ROOT} -mindepth 1 -maxdepth 1 -type d -name '[0-9]*' -printf '%f\\n' 2>/dev/null | sort -nr | tail -n +20); do\n"
    cleanup += (
        f' [ -f /{GUEST_JOB_ROOT}/"$old_id"/result ] && cleanup_job "$old_id"\ndone\n'
    )
    for old_id, old_token in purge_jobs or []:
        old_name = artifact_name(old_id, old_token)
        if old_name == artifact_name(operation_id, execution_token):
            raise ValueError("cannot purge the command being submitted")
        # Only generated artifacts of jobs already committed terminal in CNC.
        # Cleanup executes inside the guest namespace, never through host paths.
        cleanup += f"cleanup_job {old_name}\n"
    return (
        cleanup + f"umask 077\nmkdir -p /{GUEST_JOB_ROOT}\n"
        f"mkdir {shlex.quote(directory)} || exit 1\n"
        f"printf %s {shlex.quote(runner)} > {directory}/run || exit 1\n"
        f"printf %s {shlex.quote(finalizer)} > {directory}/finish || exit 1\n"
        f"exec {shlex.join(command)}"
    )


def launch_job(
    container_id: str,
    operation_id: int,
    argv: list[str],
    timeout_sec: int | None,
    purge_jobs: list[tuple[int, str]],
    execution_token: str,
) -> CommandResult:
    # The caller holds the guest guard and has durably reserved this job. This
    # deadline bounds submission only; the detached service owns execution time.
    return run_podman_exec(
        container_id,
        [
            "/bin/bash",
            "-c",
            guest_launch_script(
                operation_id,
                argv,
                timeout_sec,
                purge_jobs,
                execution_token=execution_token,
            ),
        ],
        timeout_sec=CONTROL_TIMEOUT_SEC,
    )


def stop_job(
    settings: Settings,
    output: str,
    operation_id: int,
    container_id: str,
    execution_token: str,
) -> CommandResult:
    from app.services.guest_exec import run_backend_guest_command

    unit = unit_name(operation_id, execution_token)
    script = (
        f"state=$(systemctl show --property=LoadState --value {unit}); "
        'if [ "$state" = not-found ]; then printf CNC_COMMAND_ABSENT; exit 0; fi; '
        f'[ -n "$state" ] || exit 1; exec systemctl stop --no-block {unit}'
    )
    return run_backend_guest_command(
        output,
        settings,
        ["/bin/sh", "-c", script],
        timeout_sec=CONTROL_TIMEOUT_SEC,
        job_id=operation_id,
        source="command_cancel",
        container_id=container_id,
    )
