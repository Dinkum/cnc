import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import app_runtime, guest_isolation
from app.services.apply_core import ApplyFailed


@pytest.fixture
def storage_tree(tmp_path, monkeypatch):
    managed = tmp_path.resolve() / "apps"
    managed.mkdir(mode=0o755)
    source = managed / "web"
    source.mkdir(mode=0o750)
    (source / "data").write_text("preserve me")
    real_stat = Path.stat

    def host_stat(path, **kwargs):
        result = list(real_stat(path, **kwargs))
        result[4:6] = [0, 0]
        if path in managed.parents:
            result[0] = (result[0] & ~0o777) | 0o755
        return os.stat_result(result)

    monkeypatch.setattr(Path, "stat", host_stat)
    monkeypatch.setattr(os, "listxattr", lambda path: [], raising=False)
    return managed, source


def test_repairs_only_outer_boundary_and_is_idempotent(storage_tree):
    managed, source = storage_tree
    options = dict(managed_root=managed, mounted_sources=[source])
    volumes = [f"{source}:/srv/web"]
    assert guest_isolation.preflight_guest_storage(volumes, **options) == [str(managed)]
    assert managed.stat().st_mode & 0o777 == 0o700
    assert source.stat().st_mode & 0o777 == 0o750
    assert (source / "data").read_text() == "preserve me"
    assert guest_isolation.preflight_guest_storage(volumes, **options) == []


@pytest.mark.parametrize(
    "unsafe",
    [
        "mounted_parent",
        "aliased_parent",
        "symlink",
        "writable_parent",
        "acl",
        "owner",
        "group",
        "special",
    ],
)
def test_refuses_unsafe_repairs(storage_tree, monkeypatch, unsafe):
    managed, source = storage_tree
    sources = [source]
    if unsafe == "mounted_parent":
        sources.append(managed)
    elif unsafe == "aliased_parent":
        alias = managed.parent / "alias"
        alias.symlink_to(managed, target_is_directory=True)
        sources.append(alias)
    elif unsafe == "symlink":
        alias = managed / "alias"
        alias.symlink_to(source, target_is_directory=True)
        source = alias
    elif unsafe == "writable_parent":
        managed.chmod(0o777)
    elif unsafe == "acl":
        monkeypatch.setattr(os, "listxattr", lambda path: ["system.posix_acl_access"])
    else:
        host_stat = Path.stat

        def custom_stat(path, **kwargs):
            result = list(host_stat(path, **kwargs))
            if path == managed:
                if unsafe == "special":
                    result[0] |= 0o2000
                else:
                    result[4 if unsafe == "owner" else 5] = 1000
            return os.stat_result(result)

        monkeypatch.setattr(Path, "stat", custom_stat)
    before = managed.stat().st_mode
    with pytest.raises(ValueError):
        guest_isolation.preflight_guest_storage(
            [f"{source}:/srv/web"], managed_root=managed, mounted_sources=sources
        )
    assert managed.stat().st_mode == before


def test_custom_storage_is_not_changed(storage_tree):
    managed, source = storage_tree
    with pytest.raises(ValueError):
        guest_isolation.preflight_guest_storage(
            [f"{source}:/srv/web"],
            managed_root=managed / "different",
            mounted_sources=[source],
        )
    assert managed.stat().st_mode & 0o777 == 0o755


def test_preflight_checks_all_selected_outputs_and_collects_blockers(monkeypatch):
    backends = [
        SimpleNamespace(name=name, volumes_json="[]") for name in ["a", "b", "c"]
    ]
    desired = SimpleNamespace(
        known_app_backends=backends,
        runtime_graph=SimpleNamespace(enabled_app_backend_names={"a", "b", "c"}),
    )
    monkeypatch.setattr(
        app_runtime, "read_hardening_policy", lambda backend: backend.name
    )
    monkeypatch.setattr(
        app_runtime, "hardening_volumes", lambda name, volumes: [f"/data/{name}:/data"]
    )
    checked = []

    def check(volumes, **kwargs):
        checked.extend(volumes)
        raise ValueError("private parent required")

    monkeypatch.setattr(app_runtime, "preflight_guest_storage", check)
    with pytest.raises(ApplyFailed) as failure:
        app_runtime.preflight_app_storage(desired, {"a", "c"})
    assert checked == ["/data/a:/data", "/data/c:/data"]
    assert failure.value.phase == "storage_preflight"
    assert [item["backend"] for item in failure.value.details["storage_blockers"]] == [
        "a",
        "c",
    ]
