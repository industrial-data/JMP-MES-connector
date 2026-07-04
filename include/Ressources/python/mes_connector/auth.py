# -*- coding: utf-8 -*-
"""
Authentication and session handling for mes_connector.

Strategy (decided for v3.0):
1. Try single-sign-on silently:
   - Windows: requests-negotiate-sspi (uses the logged-in AD account)
   - macOS/Linux: requests-kerberos (uses an existing kinit ticket)
2. If the server answers 401 (or no SSO library is installed), raise
   `AuthRequired`. The JSL side catches this, shows a login dialog, calls
   `set_credentials(host, user, password)`, and retries the original call.

Credentials live in process memory only — never written to disk.
"""
from __future__ import annotations

import sys
import threading
from urllib.parse import urlparse

import requests
import urllib3

# Silence self-signed-certificate warnings: historian servers on plant networks
# almost never have public certs. verify=False mirrors the v2.x OLEDB behavior.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# host -> (user, password), set from the JSL login dialog
_basic_credentials: dict[str, tuple[str, str]] = {}
# (host, thread-id) -> Session; sessions are not thread-safe, so one per thread
_thread_local = threading.local()


class AuthRequired(Exception):
    """Raised when the server needs explicit credentials.

    The message is the hostname, so JSL can display it in the login dialog.
    """


def set_credentials(host_or_url: str, user: str, password: str) -> None:
    """Store credentials for a host (called from the JSL login dialog)."""
    host = urlparse(host_or_url).hostname or host_or_url
    _basic_credentials[host] = (user, password)
    # Drop any cached session for this host so the next call re-authenticates
    if hasattr(_thread_local, "sessions"):
        _thread_local.sessions.pop(host, None)


def clear_credentials() -> None:
    _basic_credentials.clear()
    if hasattr(_thread_local, "sessions"):
        _thread_local.sessions.clear()


def _sso_auth():
    """Return a silent SSO auth object if a suitable library is installed."""
    if sys.platform == "win32":
        try:
            from requests_negotiate_sspi import HttpNegotiateAuth
            return HttpNegotiateAuth()
        except ImportError:
            return None
    try:
        from requests_kerberos import HTTPKerberosAuth, OPTIONAL
        return HTTPKerberosAuth(mutual_authentication=OPTIONAL)
    except ImportError:
        return None


def get_session(server_url: str, verify: bool = False) -> requests.Session:
    """Session for `server_url`, authenticated by SSO or stored credentials.

    Cached per host and per thread (bulk extraction uses a thread pool).
    """
    host = urlparse(server_url).hostname or server_url
    if not hasattr(_thread_local, "sessions"):
        _thread_local.sessions = {}
    if host in _thread_local.sessions:
        return _thread_local.sessions[host]

    s = requests.Session()
    s.verify = verify

    if host in _basic_credentials:
        s.auth = _basic_credentials[host]
    else:
        s.auth = _sso_auth()  # may be None -> anonymous; server will 401

    _thread_local.sessions[host] = s
    return s


def check_response(resp: requests.Response) -> requests.Response:
    """raise_for_status, but converts 401 into AuthRequired for the JSL dialog."""
    if resp.status_code == 401:
        host = urlparse(resp.url).hostname or resp.url
        # Invalidate the session: its auth mode failed
        if hasattr(_thread_local, "sessions"):
            _thread_local.sessions.pop(host, None)
        raise AuthRequired(host)
    resp.raise_for_status()
    return resp
