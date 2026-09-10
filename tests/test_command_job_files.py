import json
import os

import pytest


from app.config import Settings
from app.services import command_job_files as files
from app.services.file_locks import FileLock
from app.services.sandbox_profiles import app_sandbox_rootfs_path


TOKEN = "a" * 32
OTHER_TOKEN = "b" * 32


@pytest.fixture
def spool(tmp_path):
    settings = Settings(
        app_control_dir=tmp_path / "control", app_sandbox_dir=tmp_path / "sandboxes"
    )
    root = app_sandbox_rootfs_path(settings, "demo")
    directory = root / files.GUEST_JOB_ROOT / files.artifact_name(1, TOKEN)
    directory.mkdir(parents=True)
    return settings, directory


def test_read_rejects_leaf_symlink_and_fifo(spool, tmp_path):
    settings, directory = spool
    secret = tmp_path / "outside"
    secret.write_text("must not be read")
    (directory / "stdout").symlink_to(secret)
    with pytest.raises(OSError):
        files.read_guest_file(settings, "demo", 1, "stdout", execution_token=TOKEN)
    os.mkfifo(directory / "stderr")
    with pytest.raises(OSError, match="not a regular file"):
        files.read_guest_file(settings, "demo", 1, "stderr", execution_token=TOKEN)


def test_read_rejects_directory_symlink(spool, tmp_path):
    settings, directory = spool
    elsewhere = tmp_path / "outside"
    elsewhere.mkdir()
    (elsewhere / "stdout").write_text("must not be read")
    (directory.parent / files.artifact_name(2, TOKEN)).symlink_to(
        elsewhere, target_is_directory=True
    )
    with pytest.raises(OSError):
        files.read_guest_file(settings, "demo", 2, "stdout", execution_token=TOKEN)


def test_read_is_bounded_and_missing_output_is_empty(spool):
    settings, directory = spool
    (directory / "stdout").write_bytes(b"a" * (files.MAX_READ_BYTES + 10))
    assert (
        len(
            files.read_guest_file(
                settings, "demo", 1, "stdout", limit=10**9, execution_token=TOKEN
            )
        )
        == files.MAX_READ_BYTES
    )
    assert (
        files.read_guest_file(settings, "demo", 1, "stderr", execution_token=TOKEN)
        == b""
    )


def test_utf8_cursor_retains_incomplete_character_across_reads(spool):
    settings, directory = spool
    path = directory / "stdout"
    path.write_bytes(b"hello \xe2\x82")
    first = files.read_job_stream(
        settings, "demo", 1, "stdout", offset=0, terminal=False, execution_token=TOKEN
    )
    assert first["data"] == "hello "
    assert first["next_offset"] == 6
    assert not first["eof"]
    with path.open("ab") as output:
        output.write(b"\xac!")
    second = files.read_job_stream(
        settings,
        "demo",
        1,
        "stdout",
        offset=first["next_offset"],
        terminal=True,
        execution_token=TOKEN,
    )
    assert second["data"] == "€!"
    assert second["next_offset"] == 10
    assert second["eof"]


def test_utf8_cursor_flushes_incomplete_terminal_character(spool):
    settings, directory = spool
    (directory / "stdout").write_bytes(b"\xe2")
    result = files.read_job_stream(
        settings, "demo", 1, "stdout", offset=0, terminal=True, execution_token=TOKEN
    )
    assert result["data"] == "�"
    assert result["next_offset"] == 1
    assert result["eof"]


def test_completed_reservation_does_not_block_next_job(spool):
    settings, directory = spool
    files.reserve_job(settings, "demo", 1, execution_token=TOKEN)
    assert files.reserved_job(settings, "demo") == 1
    (directory / "result").write_text("success\texited\t0\n")
    assert files.reserved_job(settings, "demo") is None


@pytest.mark.parametrize(
    "record",
    [
        b"\xff",
        b"success\texited\tnope\n",
        b"success\texited\t256\n",
        b"bogus\texited\t0\n",
    ],
)
def test_corrupt_completion_cannot_release_reservation(spool, record):
    settings, directory = spool
    files.reserve_job(settings, "demo", 1, execution_token=TOKEN)
    (directory / "result").write_bytes(record)
    with pytest.raises(OSError):
        files.reserved_job(settings, "demo")


@pytest.mark.parametrize("record", [b"\xff", b"{", b"[]", b'{"operation_id":true}'])
def test_corrupt_reservation_fails_closed(spool, record):
    settings, _ = spool
    path = files.reservation_path(settings, "demo")
    path.parent.mkdir(parents=True)
    path.write_bytes(record)
    with pytest.raises(OSError):
        files.reserved_job(settings, "demo")


def test_old_release_cannot_erase_new_reservation(spool):
    settings, directory = spool
    files.reserve_job(settings, "demo", 1, execution_token=TOKEN)
    (directory / "result").write_text("success\texited\t0\n")
    path = files.reservation_path(settings, "demo")
    lock_path = path.parent / "exec-probe.lock"
    with FileLock(lock_path, blocking=False, lock_path=lock_path):
        assert files.reserved_job(settings, "demo") is None
        # A terminal observer racing with submission must defer instead of
        # reading A's reservation and later unlinking B's replacement.
        files.release_job(settings, "demo", 1, execution_token=TOKEN)
        assert path.exists()
        files.reserve_job(settings, "demo", 2, execution_token=TOKEN)
        files.release_job(settings, "demo", 1, execution_token=TOKEN)
    files.release_job(settings, "demo", 1, execution_token=TOKEN)
    assert json.loads(path.read_text())["operation_id"] == 2
    files.release_job(settings, "demo", 2, execution_token=TOKEN)
    assert not path.exists()


def test_retention_distinguishes_missing_empty_and_interrupted_logs(spool):
    settings, directory = spool
    missing = files.read_job_stream(
        settings, "demo", 1, "stdout", offset=0, terminal=True, execution_token=TOKEN
    )
    assert not missing["retained"]
    (directory / "stdout").write_bytes(b"")
    empty = files.read_job_stream(
        settings, "demo", 1, "stdout", offset=0, terminal=True, execution_token=TOKEN
    )
    assert empty["retained"]
    assert empty["data"] == ""
    (directory / "stdout").write_bytes(b"partial output before interruption")
    interrupted = files.read_job_stream(
        settings, "demo", 1, "stdout", offset=0, terminal=True, execution_token=TOKEN
    )
    assert interrupted["retained"]
    assert interrupted["data"] == "partial output before interruption"


def test_old_execution_cannot_release_reused_operation_id_reservation(spool):
    settings, directory = spool
    files.reserve_job(settings, "demo", 1, execution_token=OTHER_TOKEN)
    (directory / "result").write_text("success\texited\t0\n")
    assert files.reserved_job(settings, "demo") == 1
    files.release_job(settings, "demo", 1, execution_token=TOKEN)
    assert files.reserved_job(settings, "demo") == 1
    assert (
        files.read_job_result(settings, "demo", 1, execution_token=OTHER_TOKEN) is None
    )


@pytest.mark.parametrize("token", ["", "../outside", "a" * 31, "A" * 32, None])
def test_invalid_execution_token_cannot_form_guest_path(token):
    with pytest.raises(ValueError):
        files.artifact_name(1, token)
