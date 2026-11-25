"""Every TestClient sends the running app's per-launch API token (server.py requires it on /api/* and /files/*), as the
desktop window's cookie does. A test that passes its own headers= gets exactly those (to check the refusal)."""
import sys

from starlette.testclient import TestClient

_init = TestClient.__init__


def _with_token(self, *a, headers=None, **k):
    srv = sys.modules.get("reelwright.server")  # looked up per client: a test may have reloaded server (new token)
    if headers is None and srv is not None:
        headers = {srv.TOKEN_HEADER: srv.TOKEN}
    _init(self, *a, headers=headers, **k)


TestClient.__init__ = _with_token
