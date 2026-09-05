from __future__ import annotations

import socket

from app.services.validators import ValidationError


def is_loopback_port_free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def ensure_loopback_port_free(port: int, *, field_name: str = "port") -> None:
    if not is_loopback_port_free(port):
        raise ValidationError(f"{field_name} {port} is already in use on 127.0.0.1")
