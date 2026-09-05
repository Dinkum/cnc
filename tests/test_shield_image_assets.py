from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_shield_image_assets_define_ubuntu_runtime() -> None:
    containerfile = (ROOT / "packaging" / "shield" / "Containerfile").read_text(
        encoding="utf-8"
    )
    start = (ROOT / "packaging" / "shield" / "start.sh").read_text(encoding="utf-8")
    build = (ROOT / "packaging" / "shield" / "build.sh").read_text(encoding="utf-8")

    assert "FROM docker.io/library/ubuntu:24.04" in containerfile
    assert "COPY app /opt/cnc/app" in containerfile
    assert 'ENTRYPOINT ["/usr/local/bin/cnc-shield"]' in containerfile
    assert "uvicorn app.shield:app" in start
    assert "SHIELD_PORT:-1026" in start
    assert "podman build" in build
