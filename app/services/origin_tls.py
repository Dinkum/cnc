"""Node-local self-signed origin TLS, also executable on standalone followers."""

from __future__ import annotations

import argparse
import base64
import json
import re
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
from typing import Iterator

DEFAULT_CONTROL_DIR = Path("/var/lib/cnc/app-control")
RENEW_BEFORE_SECONDS = 30 * 86400


def certificate_path(control_dir: Path = DEFAULT_CONTROL_DIR) -> Path:
    return control_dir / "origin-tls" / "selfsigned.pem"


@contextmanager
def certificate_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    with (path.parent / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _openssl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["openssl", *args], capture_output=True, text=True, timeout=30, check=True
    )


def certificate_valid(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        _openssl(
            "x509", "-in", str(path), "-noout", "-checkend", str(RENEW_BEFORE_SECONDS)
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(path))
    except (subprocess.CalledProcessError, ssl.SSLError, OSError):
        return False
    return True


def _write_atomic(path: Path, content: bytes) -> None:
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".certificate-", delete=False
    ) as temporary:
        os.fchmod(temporary.fileno(), 0o600)
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        staged = Path(temporary.name)
    staged.replace(path)


def ensure_certificate(path: Path) -> bool:
    """Caller holds certificate_lock; keep the key/certificate pair atomic."""
    if certificate_valid(path):
        path.chmod(0o600)
        activated = path.with_suffix(".activated")
        return (
            not activated.exists()
            or activated.read_text() != certificate_fingerprint(path)
        )
    with tempfile.TemporaryDirectory(dir=path.parent) as directory:
        key = Path(directory) / "key.pem"
        cert = Path(directory) / "cert.pem"
        # Full does not validate hostname or trust. Each node owns its own key;
        # no secret is copied in cluster routing bundles or config snapshots.
        _openssl(
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-sha256",
            "-days",
            "365",
            "-subj",
            "/CN=cnc-origin.invalid",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        )
        _write_atomic(path, cert.read_bytes() + key.read_bytes())
    return True


def certificate_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def certificate_activated(path: Path, fingerprint: str | None = None) -> None:
    if path.exists():
        _write_atomic(
            path.with_suffix(".activated"),
            (fingerprint or certificate_fingerprint(path)).encode(),
        )


def prepare_certificate(files: dict[str, str], path: Path) -> str | None:
    """Return the fingerprint needing reload; acknowledge only that generation."""
    if not any(f'ssl_certificate "{path}";' in content for content in files.values()):
        return None
    with certificate_lock(path):
        return certificate_fingerprint(path) if ensure_certificate(path) else None


def renew_active_certificate(nginx_dir: Path, path: Path) -> bool:
    files = {p.name: p.read_text() for p in nginx_dir.glob("cnc-*.conf")}
    if not any(f'ssl_certificate "{path}";' in content for content in files.values()):
        return False
    with certificate_lock(path):
        previous = path.read_bytes() if path.exists() else None
        if not ensure_certificate(path):
            return False
        try:
            subprocess.run(["nginx", "-t"], check=True, capture_output=True, timeout=30)
            subprocess.run(
                ["systemctl", "reload", "nginx"],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (subprocess.SubprocessError, OSError):
            if previous is not None:
                _write_atomic(path, previous)
            raise
        certificate_activated(path)
    return True


def origin_certificate_path(
    hostname: str, control_dir: Path = DEFAULT_CONTROL_DIR
) -> Path:
    if not re.fullmatch(r"[a-z0-9.-]+", hostname) or len(hostname) > 253:
        raise ValueError("Invalid origin hostname.")
    digest = hashlib.sha256(hostname.encode()).hexdigest()[:32]
    return control_dir / "origin-tls" / f"cloudflare-{digest}.pem"


def origin_request(
    hostname: str, control_dir: Path = DEFAULT_CONTROL_DIR
) -> dict[str, object]:
    path = origin_certificate_path(hostname, control_dir)
    with certificate_lock(path):
        if certificate_valid(path):
            _openssl("x509", "-in", str(path), "-noout", "-checkhost", hostname)
            activated = path.with_suffix(".activated")
            fingerprint = certificate_fingerprint(path)
            return {
                "ready": True,
                "fingerprint": fingerprint,
                "reload_required": not activated.exists()
                or activated.read_text() != fingerprint,
            }
        key = path.with_suffix(".key")
        if not key.exists():
            with tempfile.TemporaryDirectory(dir=path.parent) as directory:
                temporary = Path(directory) / "key.pem"
                _openssl(
                    "genpkey",
                    "-algorithm",
                    "RSA",
                    "-pkeyopt",
                    "rsa_keygen_bits:2048",
                    "-out",
                    str(temporary),
                )
                _write_atomic(key, temporary.read_bytes())
        request = _openssl(
            "req",
            "-new",
            "-key",
            str(key),
            "-subj",
            f"/CN={hostname}",
            "-addext",
            f"subjectAltName=DNS:{hostname}",
        )
        return {"ready": False, "csr": request.stdout}


def install_origin_certificate(
    hostname: str, certificate: str, control_dir: Path = DEFAULT_CONTROL_DIR
) -> str:
    path = origin_certificate_path(hostname, control_dir)
    with certificate_lock(path):
        key = path.with_suffix(".key")
        with tempfile.TemporaryDirectory(dir=path.parent) as directory:
            candidate = Path(directory) / "certificate.pem"
            _write_atomic(candidate, certificate.encode() + b"\n" + key.read_bytes())
            if not certificate_valid(candidate):
                raise ValueError(
                    "Issued origin certificate is invalid or expires too soon."
                )
            _openssl("x509", "-in", str(candidate), "-noout", "-checkhost", hostname)
            _write_atomic(path, candidate.read_bytes())
        return certificate_fingerprint(path)


def reload_origin_certificate(
    hostname: str, control_dir: Path = DEFAULT_CONTROL_DIR
) -> None:
    path = origin_certificate_path(hostname, control_dir)
    with certificate_lock(path):
        fingerprint = certificate_fingerprint(path)
        subprocess.run(["nginx", "-t"], check=True, capture_output=True, timeout=30)
        subprocess.run(
            ["systemctl", "reload", "nginx"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        certificate_activated(path, fingerprint)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Maintain active CNC origin HTTPS certificates"
    )
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--origin-request")
    parser.add_argument("--origin-install")
    parser.add_argument("--origin-reload")
    parser.add_argument("--certificate-data")
    parser.add_argument(
        "--control-dir",
        type=Path,
        default=Path(os.environ.get("APP_CONTROL_DIR", str(DEFAULT_CONTROL_DIR))),
    )
    parser.add_argument(
        "--nginx-dir",
        type=Path,
        default=Path(os.environ.get("NGINX_GENERATED_DIR", "/etc/nginx/generated")),
    )
    args = parser.parse_args()
    if args.origin_request:
        print(json.dumps(origin_request(args.origin_request, args.control_dir)))
        return
    if args.origin_install:
        certificate = base64.b64decode(args.certificate_data, validate=True).decode()
        print(
            json.dumps(
                {
                    "fingerprint": install_origin_certificate(
                        args.origin_install, certificate, args.control_dir
                    )
                }
            )
        )
        return
    if args.origin_reload:
        reload_origin_certificate(args.origin_reload, args.control_dir)
        return
    path = certificate_path(args.control_dir)
    if args.prepare:
        files = {p.name: p.read_text() for p in args.nginx_dir.glob("cnc-*.conf")}
        changed = prepare_certificate(files, path)
    else:
        changed = renew_active_certificate(args.nginx_dir, path)
    print(f"origin_https: {'certificate updated' if changed else 'unchanged'}")


if __name__ == "__main__":
    main()
