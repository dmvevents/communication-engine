"""tests/netguard — refuse every non-loopback socket while the suite runs.

Put this directory first on PYTHONPATH and Python imports it at startup as `sitecustomize`:

    PYTHONPATH=tests/netguard python3 -m unittest discover -s tests -q

Any connect, bind or name lookup that is not 127.0.0.1 / ::1 then raises NetworkRefused
loudly, in the test that caused it. The suite's promise is that nothing in it reaches
slack.com (docs/QUICKSTART.md); the independent review of ENH-31 (round 3, R3-1) caught one
test that did, silently, behind a best-effort exception handler — a guard the test run
itself carries turns that class into a red test instead of a line in a reviewer's log. CI
and the pre-push hook run the suite under it; the mutation sandbox copies this directory too.

Loopback is the architecture, not an exception: the Slack stub, the dashboard and every
probe bind 127.0.0.1. The guard is inert when this directory is not on PYTHONPATH.
"""
import socket

LOOPBACK = {"127.0.0.1", "::1", "localhost", "", None}
_MARKER = "comms-engine-netguard"


class NetworkRefused(RuntimeError):
    """A test tried to leave the host."""


def _host_of(address):
    if isinstance(address, (tuple, list)) and address:
        return address[0]
    if isinstance(address, (str, bytes)):          # a unix socket path
        return None
    return address


def _refuse_unless_loopback(host, what):
    if isinstance(host, bytes):
        host = host.decode("utf-8", "replace")
    if host in LOOPBACK or (isinstance(host, str) and host.startswith("127.")):
        return
    raise NetworkRefused(f"{_MARKER}: refused {what} to {host!r} — the suite must stay on loopback")


_connect, _connect_ex, _bind, _getaddrinfo = (socket.socket.connect, socket.socket.connect_ex,
                                               socket.socket.bind, socket.getaddrinfo)


def connect(self, address):
    if self.family in (socket.AF_INET, getattr(socket, "AF_INET6", None)):
        _refuse_unless_loopback(_host_of(address), "connect")
    return _connect(self, address)


def connect_ex(self, address):
    if self.family in (socket.AF_INET, getattr(socket, "AF_INET6", None)):
        _refuse_unless_loopback(_host_of(address), "connect")
    return _connect_ex(self, address)


def bind(self, address):
    if self.family in (socket.AF_INET, getattr(socket, "AF_INET6", None)):
        _refuse_unless_loopback(_host_of(address), "bind")
    return _bind(self, address)


def getaddrinfo(host, *args, **kwargs):
    _refuse_unless_loopback(host, "name lookup")
    return _getaddrinfo(host, *args, **kwargs)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.socket.bind = bind
socket.getaddrinfo = getaddrinfo
