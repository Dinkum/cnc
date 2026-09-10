import subprocess
import sys

import pytest

from app.services import guest_isolation as isolation


def test_pool_avoids_existing_reservations_and_is_idempotent(tmp_path):
    uid, gid, lock = (tmp_path / name for name in ("subuid", "subgid", "lock"))
    uid.write_text("existing:268435456:65536\n")
    gid.write_text("another:268500992:65536\n")
    options = dict(subuid=uid, subgid=gid, lock=lock, host_ids={0, 1000})
    pool = isolation.ensure_subid_pool(**options)
    assert pool.start == 268566528
    original = (uid.read_bytes(), gid.read_bytes())
    assert isolation.ensure_subid_pool(**options) == pool
    assert (uid.read_bytes(), gid.read_bytes()) == original


def test_interrupted_pool_write_reuses_the_first_reserved_range(monkeypatch, tmp_path):
    uid, gid, lock = (tmp_path / name for name in ("subuid", "subgid", "lock"))
    options = dict(subuid=uid, subgid=gid, lock=lock, host_ids={0})
    write = isolation._atomic_write

    def fail_second(path, content, mode):
        if path == gid:
            raise OSError("interrupted")
        write(path, content, mode)

    monkeypatch.setattr(isolation, "_atomic_write", fail_second)
    with pytest.raises(OSError, match="interrupted"):
        isolation.ensure_subid_pool(**options)
    first = uid.read_text()
    monkeypatch.setattr(isolation, "_atomic_write", write)
    isolation.ensure_subid_pool(**options)
    assert uid.read_text() == gid.read_text() == first


@pytest.mark.parametrize(
    "uid_text,gid_text",
    [
        ("containers:268435456:65536\n", "containers:268500992:65536\n"),
        ("containers:268435456:65536\nother:268435456:65536\n", ""),
        ("containers:0:65536\n", ""),
        ("invalid\n", ""),
    ],
)
def test_invalid_or_overlapping_pools_fail_without_rewriting(
    tmp_path, uid_text, gid_text
):
    uid, gid, lock = (tmp_path / name for name in ("subuid", "subgid", "lock"))
    uid.write_text(uid_text)
    gid.write_text(gid_text)
    with pytest.raises(ValueError):
        isolation.ensure_subid_pool(subuid=uid, subgid=gid, lock=lock, host_ids={0})
    assert uid.read_text() == uid_text
    assert gid.read_text() == gid_text


def test_hardening_cannot_replace_baseline_or_silently_drop_init_capabilities():
    with pytest.raises(ValueError, match="AppArmor"):
        isolation.validate_guest_hardening({"apparmor_profile": "containers-default"})
    with pytest.raises(ValueError, match="SYS_ADMIN"):
        isolation.validate_guest_hardening({"cap_drop_all": True})
    isolation.validate_guest_hardening(
        {"cap_drop_all": True, "cap_add": list(isolation.GUEST_INIT_CAPABILITIES)}
    )


def test_binds_retain_restrictions_without_ownership_rewrite():
    values = isolation.guest_volume_arguments(
        ["/data:/var/lib/service:ro,nosuid,nodev"]
    )
    assert values == ["/data:/var/lib/service:ro,nosuid,nodev," + isolation.GUEST_IDMAP]
    for option in ("U", "O", "idmap"):
        with pytest.raises(ValueError, match="ownership"):
            isolation.guest_volume_arguments([f"/data:/data:{option}"])


def test_shared_profile_does_not_allow_writable_mounts_at_arbitrary_destinations():
    profile = isolation.guest_apparmor_profile()
    for line in profile.splitlines():
        if "mount " in line and "-> /**," in line:
            assert "options=(ro, remount, bind" in line
            assert "options in" not in line
    assert (
        "pivot_root oldroot=/run/systemd/mount-rootfs/ /run/systemd/mount-rootfs/,"
        in profile
    )
    assert "unconfined," not in profile.replace("signal (receive) peer=unconfined,", "")


def test_helper_is_deterministic_and_runs_without_installed_app(tmp_path):
    content = isolation.guest_helper_bytes()
    assert isolation.guest_helper_bytes() == content
    helper = tmp_path / "cnc-prepare-guest"
    helper.write_bytes(content)
    result = subprocess.run(
        [sys.executable, "-I", str(helper), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--extract-rootfs" in result.stdout


def test_guest_volume_guard_rejects_symlink_ancestors_and_shared_writes(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(data, target_is_directory=True)
    (data / "nested").mkdir()
    with pytest.raises(ValueError, match="symlinks"):
        isolation.validate_guest_volume_paths([f"{alias}/nested:/data:ro"])
    with pytest.raises(ValueError, match="private directory"):
        isolation.validate_guest_volume_paths([f"{data}:/data:rw"])
    isolation.validate_guest_volume_paths([f"{data}:/data:ro"])
