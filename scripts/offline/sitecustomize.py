"""Offline guard, auto-loaded by Python when scripts/offline is on PYTHONPATH (scripts/run_offline.sh does this).

Blocks every non-Unix-socket network operation in this Python process tree (children inherit PYTHONPATH).
This is the fallback layer; the primary layer is an empty network namespace (unshare -rn) when available."""
import socket as _s

_MSG = "network disabled by offline test guard"
_orig_connect, _orig_connect_ex, _orig_sendto = _s.socket.connect, _s.socket.connect_ex, _s.socket.sendto


def _is_unix(sock):
    return getattr(_s, "AF_UNIX", None) is not None and sock.family == _s.AF_UNIX


def _connect(self, addr):
    if not _is_unix(self):
        raise OSError(_MSG)
    return _orig_connect(self, addr)


def _connect_ex(self, addr):
    if not _is_unix(self):
        raise OSError(_MSG)
    return _orig_connect_ex(self, addr)


def _sendto(self, *a):
    if not _is_unix(self):
        raise OSError(_MSG)
    return _orig_sendto(self, *a)


def _blocked(*a, **k):
    raise OSError(_MSG)


_s.socket.connect, _s.socket.connect_ex, _s.socket.sendto = _connect, _connect_ex, _sendto
_s.create_connection = _blocked
_s.getaddrinfo = _blocked
_s.gethostbyname = _blocked
