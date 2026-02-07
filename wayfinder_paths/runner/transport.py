from __future__ import annotations

import errno
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class RunnerTransport(Protocol):
    def roundtrip(self, payload: bytes) -> bytes: ...

    def describe(self) -> str: ...


@dataclass(frozen=True)
class UnixSocketTransport:
    sock_path: Path
    timeout_s: float = 5.0

    def roundtrip(self, payload: bytes) -> bytes:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(float(self.timeout_s))
            s.connect(str(self.sock_path))
            s.sendall(payload)
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError as exc:
                # Best-effort: some platforms can raise ENOTCONN here even after a
                # successful connect/send. The request is newline-delimited, so we
                # can proceed without a half-close.
                if exc.errno not in {errno.ENOTCONN, errno.EINVAL}:
                    raise

            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(1024 * 1024)
                if not chunk:
                    break
                buf += chunk
            return buf

    def describe(self) -> str:
        return f"unix:{self.sock_path}"
