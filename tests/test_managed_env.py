import os
from pathlib import Path
import stat
import threading
import time

from app.config import Settings
from app.services import file_locks
from app.services import managed_env
from app.services import host_state
from app.services.managed_env import apply_managed_env_updates, update_managed_env_file


def test_update_managed_env_file_replaces_adds_and_removes_keys(tmp_path) -> None:
    env_path = tmp_path / "cnc.env"
    env_path.write_text(
        "# comment\nEXISTING_KEY=keep\nPUSHOVER_APP_TOKEN=oldtoken\nPUSHOVER_USER_KEY=olduser\n",
        encoding="utf-8",
    )

    update_managed_env_file(
        env_path,
        {
            "PUSHOVER_APP_TOKEN": "newtoken",
            "PUSHOVER_USER_KEY": None,
            "NEW_KEY": "fresh",
        },
    )

    assert env_path.read_text(encoding="utf-8").splitlines() == [
        "# comment",
        "EXISTING_KEY=keep",
        "PUSHOVER_APP_TOKEN=newtoken",
        "NEW_KEY=fresh",
    ]


def test_apply_managed_env_updates_updates_process_environment(
    monkeypatch, tmp_path
) -> None:
    env_path = tmp_path / "cnc.env"
    settings = Settings(managed_env_file_path=env_path)
    monkeypatch.delenv("PUSHOVER_APP_TOKEN", raising=False)
    monkeypatch.delenv("PUSHOVER_USER_KEY", raising=False)

    refreshed = apply_managed_env_updates(
        settings,
        {
            "PUSHOVER_APP_TOKEN": "apptoken",
            "PUSHOVER_USER_KEY": "userkey",
        },
    )

    assert os.environ["PUSHOVER_APP_TOKEN"] == "apptoken"
    assert os.environ["PUSHOVER_USER_KEY"] == "userkey"
    assert refreshed.pushover_app_token == "apptoken"
    assert refreshed.pushover_user_key == "userkey"


def test_update_managed_env_file_serializes_concurrent_writers(
    monkeypatch, tmp_path
) -> None:
    env_path = tmp_path / "cnc.env"
    env_path.write_text("BASE=keep\n", encoding="utf-8")
    active_calls = 0
    max_active_calls = 0
    counter_lock = threading.Lock()
    original = managed_env._read_managed_env_text

    def wrapped(path) -> str:
        nonlocal active_calls, max_active_calls
        with counter_lock:
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
        time.sleep(0.05)
        try:
            return original(path)
        finally:
            with counter_lock:
                active_calls -= 1

    monkeypatch.setattr(managed_env, "_read_managed_env_text", wrapped)
    barrier = threading.Barrier(2)

    def worker(updates: dict[str, str]) -> None:
        barrier.wait()
        update_managed_env_file(env_path, updates)

    first = threading.Thread(target=worker, args=({"FIRST_KEY": "one"},))
    second = threading.Thread(target=worker, args=({"SECOND_KEY": "two"},))
    first.start()
    second.start()
    first.join()
    second.join()

    assert max_active_calls == 1
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "BASE=keep"
    assert set(lines[1:]) == {"FIRST_KEY=one", "SECOND_KEY=two"}


def test_managed_env_updates_use_shared_lock_root_for_etc_env(
    monkeypatch, tmp_path
) -> None:
    lock_root = tmp_path / "locks"
    settings = Settings(managed_env_file_path=Path("/etc/cnc.env"))
    monkeypatch.setattr(file_locks, "DEFAULT_SHARED_LOCK_ROOT", lock_root)
    monkeypatch.setattr(managed_env, "_read_managed_env_text", lambda _path: "")
    writes: list[tuple[Path, str]] = []

    def fake_write(path: Path, content: str) -> None:
        writes.append((path, content))

    monkeypatch.setattr(managed_env, "_write_managed_env_text", fake_write)
    monkeypatch.setattr(managed_env, "reload_settings", lambda: settings)

    refreshed = apply_managed_env_updates(settings, {"ACCESS_KEY_HASH": "hashvalue"})

    assert refreshed is settings
    assert writes == [(Path("/etc/cnc.env"), "ACCESS_KEY_HASH=hashvalue\n")]
    assert (lock_root / "etc" / "cnc.env.lock").exists()


def test_write_text_atomic_rewrites_protected_file_in_place(
    monkeypatch, tmp_path
) -> None:
    protected_root = tmp_path / "protected"
    protected_root.mkdir()
    target = protected_root / "cnc.env"
    target.write_text("OLD=1\n", encoding="utf-8")
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    protected_root.chmod(stat.S_IRUSR | stat.S_IXUSR)
    monkeypatch.setattr(host_state, "_SHARED_LOCK_ROOTS", (protected_root,))
    try:
        host_state.write_text_atomic(target, "NEW=1\n")
    finally:
        protected_root.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    assert target.read_text(encoding="utf-8") == "NEW=1\n"
    assert not (protected_root / "cnc.env.tmp").exists()
