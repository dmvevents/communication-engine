"""tests/netguard — refuse every non-loopback socket while the suite runs.

Put this directory first on PYTHONPATH and Python imports it at startup as `sitecustomize`:

    PYTHONPATH=tests/netguard python3 -m unittest discover -s tests -q

Any TCP connect, bind, UDP sendto/sendmsg or name lookup (getaddrinfo, gethostbyname*)
that is not 127.0.0.1 / ::1 then raises NetworkRefused, in the test that caused it.
NetworkRefused derives from BaseException, not Exception, so a best-effort
`except Exception` cannot swallow it (round 4, R4-6: the first version was swallowed in the
exact shape it was built for). The suite's promise is that nothing in it reaches slack.com
(docs/QUICKSTART.md); the independent review of ENH-31 (round 3, R3-1) caught one test that
did, silently, behind such a handler — a guard the test run itself carries turns that class
into a red test instead of a line in a reviewer's log. CI and the pre-push hook run the suite
under it; the mutation sandbox copies this directory too.

What it does NOT cover, stated so nobody over-trusts it: raw sockets, a Python child started
with -S or -I or with PYTHONPATH dropped, and anything outside Python. Without this
directory on PYTHONPATH the guard is inert; tests/test_netguard.py skips its live check then
(and fails instead of skipping under CI).

Loopback is the architecture, not an exception: the Slack stub, the dashboard and every
probe bind 127.0.0.1. An empty host on bind means EVERY interface and is refused.
"""
import socket

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_MARKER = "comms-engine-netguard"


class NetworkRefused(BaseException):
    """A test tried to leave the host. A BaseException, like KeyboardInterrupt, so that no
    `except Exception` on the way up can turn it into a silent no-op."""


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


_INET = (socket.AF_INET, getattr(socket, "AF_INET6", None))
_connect, _connect_ex, _bind, _getaddrinfo = (socket.socket.connect, socket.socket.connect_ex,
                                               socket.socket.bind, socket.getaddrinfo)
_sendto, _sendmsg = socket.socket.sendto, getattr(socket.socket, "sendmsg", None)
_gethostbyname, _gethostbyname_ex = socket.gethostbyname, socket.gethostbyname_ex


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


def gethostbyname(host):
    _refuse_unless_loopback(host, "name lookup")
    return _gethostbyname(host)


def gethostbyname_ex(host):
    _refuse_unless_loopback(host, "name lookup")
    return _gethostbyname_ex(host)


def sendto(self, data, *args):
    # sendto(data, address) or sendto(data, flags, address): the address is the last argument
    if self.family in _INET and args:
        _refuse_unless_loopback(_host_of(args[-1]), "sendto")
    return _sendto(self, data, *args)


def sendmsg(self, buffers, *args):
    # sendmsg(buffers[, ancdata[, flags[, address]]])
    if self.family in _INET and len(args) >= 3 and args[2] is not None:
        _refuse_unless_loopback(_host_of(args[2]), "sendmsg")
    return _sendmsg(self, buffers, *args)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.socket.bind = bind
socket.socket.sendto = sendto
if _sendmsg is not None:
    socket.socket.sendmsg = sendmsg
socket.getaddrinfo = getaddrinfo
socket.gethostbyname = gethostbyname
socket.gethostbyname_ex = gethostbyname_ex
