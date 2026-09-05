from pathlib import Path

from app.services import cgroup_diagnostics
from app.services.commands import CommandResult


def test_collect_container_cgroup_diagnostics_reads_host_only_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    cgroup = tmp_path / "system.slice" / "cnc-app-web.service"
    cgroup.mkdir(parents=True)
    (cgroup / "memory.current").write_text("272539648\n", encoding="utf-8")
    (cgroup / "memory.high").write_text("max\n", encoding="utf-8")
    (cgroup / "memory.max").write_text("272629760\n", encoding="utf-8")
    (cgroup / "memory.peak").write_text("274857984\n", encoding="utf-8")
    (cgroup / "memory.events").write_text(
        "low 0\nhigh 0\nmax 1999941\noom 3\noom_kill 2\n",
        encoding="utf-8",
    )
    (cgroup / "memory.pressure").write_text(
        "some avg10=99.84 avg60=99.56 avg300=97.42 total=6091111330\n"
        "full avg10=35.93 avg60=35.35 avg300=38.95 total=3695632658\n",
        encoding="utf-8",
    )
    (cgroup / "pids.current").write_text("147\n", encoding="utf-8")
    (cgroup / "pids.max").write_text("2308\n", encoding="utf-8")
    (cgroup / "pids.events").write_text("max 4\n", encoding="utf-8")
    (cgroup / "cpu.pressure").write_text("some avg10=1.25 total=9\n", encoding="utf-8")
    (cgroup / "io.pressure").write_text("some avg10=83.93 total=11\n", encoding="utf-8")
    monkeypatch.setattr(cgroup_diagnostics, "CGROUP_ROOT", tmp_path)

    payload = cgroup_diagnostics.collect_container_cgroup_diagnostics(
        {"State": {"CgroupPath": "/system.slice/cnc-app-web.service"}}
    )

    assert payload["available"] is True
    assert payload["memory"]["current"] == 272539648
    assert payload["memory"]["high"] == "max"
    assert payload["memory"]["events"]["max"] == 1999941
    assert payload["memory"]["events"]["oom_kill"] == 2
    assert payload["memory"]["pressure"]["some"]["avg10"] == 99.84
    assert payload["pids"]["current"] == 147
    assert payload["io_pressure"]["some"]["avg10"] == 83.93


def test_collect_container_cgroup_diagnostics_resolves_podman_process_cgroup(
    monkeypatch, tmp_path: Path
) -> None:
    proc_root = tmp_path / "proc"
    proc_cgroup = proc_root / "4242" / "cgroup"
    proc_cgroup.parent.mkdir(parents=True)
    cgroup_path = (
        "/machine.slice/libpod-"
        "3de7a364f9de8d8665d657d82d6503fb3f0bc7b275609d3cf8fd53f17f18296b.scope"
        "/container"
    )
    proc_cgroup.write_text(f"0::{cgroup_path}\n", encoding="utf-8")
    cgroup_root = tmp_path / "cgroup"
    cgroup = cgroup_root / cgroup_path.lstrip("/")
    cgroup.mkdir(parents=True)
    (cgroup / "memory.current").write_text("1048576\n", encoding="utf-8")
    monkeypatch.setattr(cgroup_diagnostics, "PROC_ROOT", proc_root)
    monkeypatch.setattr(cgroup_diagnostics, "CGROUP_ROOT", cgroup_root)

    payload = cgroup_diagnostics.collect_container_cgroup_diagnostics(
        {
            "State": {
                "Pid": 4242,
                # Podman does not consistently expose the process's effective path here.
                "CgroupPath": "/machine.slice/stale.scope",
            }
        }
    )

    assert payload["available"] is True
    assert payload["path"] == cgroup_path
    assert payload["memory"]["current"] == 1048576


def test_collect_container_cgroup_diagnostics_prefers_payload_over_init_scope(
    monkeypatch, tmp_path: Path
) -> None:
    proc_root = tmp_path / "proc"
    proc_cgroup = proc_root / "4242" / "cgroup"
    proc_cgroup.parent.mkdir(parents=True)
    payload_path = "/system.slice/cnc-app-web.service/libpod-payload-abc"
    proc_cgroup.write_text(f"0::{payload_path}/init.scope\n", encoding="utf-8")

    cgroup_root = tmp_path / "cgroup"
    payload = cgroup_root / payload_path.lstrip("/")
    init_scope = payload / "init.scope"
    init_scope.mkdir(parents=True)
    (payload / "memory.current").write_text("294215680\n", encoding="utf-8")
    (payload / "memory.max").write_text("449839104\n", encoding="utf-8")
    (init_scope / "memory.current").write_text("138166272\n", encoding="utf-8")
    (init_scope / "memory.max").write_text("max\n", encoding="utf-8")
    monkeypatch.setattr(cgroup_diagnostics, "PROC_ROOT", proc_root)
    monkeypatch.setattr(cgroup_diagnostics, "CGROUP_ROOT", cgroup_root)

    diagnostics = cgroup_diagnostics.collect_container_cgroup_diagnostics(
        {"State": {"Pid": 4242, "CgroupPath": payload_path}}
    )

    assert diagnostics["path"] == payload_path
    assert diagnostics["memory"]["current"] == 294215680
    assert diagnostics["memory"]["max"] == 449839104


def test_collect_container_cgroup_diagnostics_handles_missing_path(
    tmp_path: Path,
) -> None:
    assert cgroup_diagnostics.collect_container_cgroup_diagnostics(None) == {
        "available": False
    }


def test_collect_app_cgroup_prefers_systemd_service_and_reports_parent_slice(
    monkeypatch, tmp_path: Path
) -> None:
    cgroup_root = tmp_path / "cgroup"
    service_path = "/cnc-apps.slice/cnc-app-web.service"
    service = cgroup_root / service_path.lstrip("/")
    init_scope = service / "libpod-payload-abc" / "init.scope"
    init_scope.mkdir(parents=True)
    (service / "memory.current").write_text("294215680\n", encoding="utf-8")
    (service / "memory.high").write_text("359661568\n", encoding="utf-8")
    (service / "memory.max").write_text("540016640\n", encoding="utf-8")
    (service / "memory.peak").write_text("409403392\n", encoding="utf-8")
    (service / "memory.events").write_text("max 3\noom_kill 1\n", encoding="utf-8")
    (init_scope / "memory.current").write_text("138166272\n", encoding="utf-8")
    (init_scope / "memory.max").write_text("max\n", encoding="utf-8")
    apps_slice = cgroup_root / "cnc-apps.slice"
    (apps_slice / "memory.current").write_text("600000000\n", encoding="utf-8")
    (apps_slice / "memory.max").write_text("3000000000\n", encoding="utf-8")
    proc_root = tmp_path / "proc"
    proc_file = proc_root / "4242" / "cgroup"
    proc_file.parent.mkdir(parents=True)
    proc_file.write_text(
        f"0::{service_path}/libpod-payload-abc/init.scope\n", encoding="utf-8"
    )
    monkeypatch.setattr(cgroup_diagnostics, "CGROUP_ROOT", cgroup_root)
    monkeypatch.setattr(cgroup_diagnostics, "PROC_ROOT", proc_root)

    def fake_run(command: list[str], _timeout: float) -> CommandResult:
        return CommandResult(command, 0, service_path, "")

    payload = cgroup_diagnostics.collect_container_cgroup_diagnostics(
        {"State": {"Pid": 4242}},
        backend_name="web",
        command_runner=fake_run,
    )

    assert payload["selection_source"] == "systemd_service"
    assert payload["process_path"].endswith("init.scope")
    assert payload["selected_path"] == service_path
    assert payload["memory"]["current"] == 294215680
    assert payload["memory"]["high"] == 359661568
    assert payload["memory"]["max"] == 540016640
    assert payload["memory"]["peak"] == 409403392
    assert payload["parent_slice"]["path"] == "/cnc-apps.slice"
    assert payload["parent_slice"]["memory"]["max"] == 3000000000
