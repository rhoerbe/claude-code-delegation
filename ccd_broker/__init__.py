"""ccd — local cross-backend agent delegation broker.

`ccd_broker` is a small, stdlib-only message broker. It keeps one in-memory
queue and one roster entry per session *handle*, and hands messages to whoever
calls `recv` for that handle. It knows nothing about LLM backends, auth, or
Claude Code internals — sessions reach it through the `ccd` CLI.

Public surface:

    from ccd_broker import Broker, Transport, serve
    from ccd_broker.transport_uds import UnixSocketTransport

    serve(UnixSocketTransport)
"""

from .broker import VERSION, Broker, ClientContext, Transport, serve

__all__ = ["VERSION", "Broker", "ClientContext", "Transport", "serve"]
__version__ = VERSION
