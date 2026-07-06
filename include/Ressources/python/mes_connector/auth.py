# -*- coding: utf-8 -*-
"""
Authentication and session handling for mes_connector.

Strategy (v3.0):
1. Try single-sign-on silently:
   - Windows: requests-negotiate-sspi (uses the logged-in AD account)
   - macOS/Linux: requests-kerberos (uses an existing kinit ticket)
2. Else, use credentials saved in the OPERATING SYSTEM credential vault
   (Windows Credential Manager / macOS Keychain, via the `keyring` package)
   from a previous "remember me" login.
3. Else, raise `AuthRequired`: the JSL side shows a login dialog, calls
   `set_credentials(host, user, password, remember)` and retries. With
   remember=True the credentials are written to the OS vault so the user
   never types them again on this machine.

Security / compliance measures ("password cannot be intercepted"):
- Passwords are stored ONLY in the OS credential vault — encrypted at rest
  by the OS (DPAPI on Windows, Keychain on macOS) and readable only by the
  logged-in user. Never written to files, logs, or JMP variables.
- Passwords are never placed in URLs or query strings; they travel only in
  the Authorization header of the HTTPS request.
- Basic credentials are REFUSED over plain http:// (cleartext would be
  sniffable on the plant network). Kerberos/Negotiate is still allowed on
  http because the password itself never crosses the wire.
- TLS certificates are verified by default (VERIFY_TLS). The `truststore`
  package (when available) validates against the OS certificate store, so
  corporate CAs work exactly like in the browser. Plants running
  self-signed certificates can set int.TLSVerify = 0 in config.jsl — a
  logged, deliberate opt-out.
- A stored password that stops working (401) is deleted from the vault
  immediately, so stale secrets don't linger.
"""
from __future__ import annotations

import sys
import threading
from urllib.parse import urlparse

import requests
import urllib3

SERVICE_NAME = "JMP MES Data Retrieval"  # entry name shown in the OS vault

# TLS verification default. configure_tls() overrides it from JSL config.
VERIFY_TLS = True

# host -> (user, password), in-memory for this JMP session only
_basic_credentials: dict[str, tuple[str, str]] = {}
# hosts whose credentials came from (or were saved to) the OS vault —
# on 401 these are deleted from the vault as stale
_persisted_hosts: set[str] = set()
# sessions are not thread-safe -> one cache per thread
_thread_local = threading.local()

_truststore_injected = False


class AuthRequired(Exception):
    """Raised when the server needs explicit credentials.

    The message is the hostname, so JSL can display it in the login dialog.
    """


# ---------------------------------------------------------------------------
# Configuration hooks (called from JSL at init)
# ---------------------------------------------------------------------------
def configure_tls(verify: bool) -> None:
    """Set TLS verification policy (from config.jsl int.TLSVerify)."""
    global VERIFY_TLS
    VERIFY_TLS = bool(verify)
    if not VERIFY_TLS:
        print("[auth] WARNING: TLS certificate verification is DISABLED "
              "(int.TLSVerify = 0 in config.jsl).", flush=True)
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    _drop_all_sessions()


def _inject_truststore() -> None:
    """Validate TLS against the OS certificate store (corporate CAs) when the
    optional `truststore` package is available; certifi otherwise."""
    global _truststore_injected
    if _truststore_injected:
        return
    try:
        import truststore
        truststore.inject_into_ssl()
        _truststore_injected = True
        print("[auth] TLS validation uses the OS certificate store (truststore).", flush=True)
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# OS credential vault (Windows Credential Manager / macOS Keychain)
# ---------------------------------------------------------------------------
def _keyring():
    try:
        import keyring
        # a backend that actually persists (not the fail backend)
        if keyring.get_keyring() is None:  # pragma: no cover
            return None
        return keyring
    except Exception:  # ImportError or misconfigured backend
        return None


def save_credentials(host: str, user: str, password: str) -> bool:
    """Persist credentials in the OS vault. Returns True when stored."""
    kr = _keyring()
    if kr is None:
        print("[auth] keyring not available - credentials kept in memory only.", flush=True)
        return False
    # username is stored next to the secret under a per-host service entry
    kr.set_password(f"{SERVICE_NAME}:{host}", user, password)
    kr.set_password(f"{SERVICE_NAME}:{host}", "__last_user__", user)
    print(f"[auth] Credentials for '{host}' stored in the OS credential vault.", flush=True)
    return True


def load_saved_credentials(host: str) -> tuple[str, str] | None:
    kr = _keyring()
    if kr is None:
        return None
    try:
        user = kr.get_password(f"{SERVICE_NAME}:{host}", "__last_user__")
        if not user:
            return None
        pwd = kr.get_password(f"{SERVICE_NAME}:{host}", user)
        if pwd is None:
            return None
        return (user, pwd)
    except Exception:
        return None


def forget_credentials(host_or_url: str) -> None:
    """Remove stored credentials (called on 401 with a stored password)."""
    host = urlparse(host_or_url).hostname or host_or_url
    _basic_credentials.pop(host, None)
    _persisted_hosts.discard(host)
    kr = _keyring()
    if kr is not None:
        try:
            user = kr.get_password(f"{SERVICE_NAME}:{host}", "__last_user__")
            if user:
                kr.delete_password(f"{SERVICE_NAME}:{host}", user)
            kr.delete_password(f"{SERVICE_NAME}:{host}", "__last_user__")
            print(f"[auth] Stored credentials for '{host}' removed from the OS vault.", flush=True)
        except Exception:
            pass
    _drop_session(host)


def set_credentials(host_or_url: str, user: str, password: str,
                    remember: bool = False) -> None:
    """Store credentials (from the JSL login dialog).

    remember=True additionally writes them to the OS credential vault so
    future JMP sessions authenticate without prompting.
    """
    host = urlparse(host_or_url).hostname or host_or_url
    _basic_credentials[host] = (user, password)
    if remember and save_credentials(host, user, password):
        _persisted_hosts.add(host)
    _drop_session(host)


def clear_credentials() -> None:
    _basic_credentials.clear()
    _persisted_hosts.clear()
    _drop_all_sessions()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def _drop_session(host: str) -> None:
    if hasattr(_thread_local, "sessions"):
        _thread_local.sessions.pop(host, None)


def _drop_all_sessions() -> None:
    if hasattr(_thread_local, "sessions"):
        _thread_local.sessions.clear()


# ---------------------------------------------------------------------------
# Authentication strategy
# ---------------------------------------------------------------------------
# The order of attempts, for every host (each stage is logged to the JMP log):
#
#   Stage 1  KERBEROS / NEGOTIATE (no password): Windows SSPI via
#            requests-negotiate-sspi, or requests-kerberos elsewhere.
#            Tried FIRST — most PI Web API deployments accept the logged-in
#            Windows/AD identity, so the user never types anything.
#   Stage 2  OS-VAULT CREDENTIALS: username/password remembered earlier in the
#            Windows Credential Manager / macOS Keychain.
#   Stage 3  DIALOG CREDENTIALS: whatever the user typed this session.
#
# For PI Web API hosts, the winning stage is decided ONCE by probing the cheap
# /system/versions endpoint and cached in _host_stage. Other hosts (IP21) skip
# the probe: the first real request decides, with the same stage order.
#
# When every stage fails, check_response() raises AuthRequired (-> JSL login
# dialog) and the log documents each attempted stage plus test URLs so an
# administrator can reproduce the failure outside JMP.

# host -> ("kerberos" | "basic") once a working strategy is known
_host_stage: dict[str, str] = {}


def _kerberos_auth():
    """Silent no-password auth object, or None when no SSO library exists."""
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


def _sso_library_name() -> str:
    return "requests-negotiate-sspi" if sys.platform == "win32" else "requests-kerberos"


def _basic_auth_for(host: str, scheme: str):
    """Stored/typed credentials for host, refusing cleartext transport."""
    if host not in _basic_credentials:
        saved = load_saved_credentials(host)
        if saved is not None:
            _basic_credentials[host] = saved
            _persisted_hosts.add(host)
            print(f"[auth] Using stored credentials for '{host}' from the OS vault.", flush=True)
    if host not in _basic_credentials:
        return None
    # Compliance: never send a password over an unencrypted channel.
    if scheme == "http":
        raise RuntimeError(
            f"Refusing to send credentials to '{host}' over plain http:// "
            "(the password would cross the network unencrypted). Use an "
            "https:// WebAPI URL, or rely on Kerberos SSO (no password on the wire)."
        )
    return _basic_credentials[host]


def _log_auth_failure(host: str, base: str, tried: list[str]) -> None:
    """One readable block in the JMP log explaining WHAT failed and HOW to test."""
    kerb = _kerberos_auth()
    print(
        "[auth] ============================================================\n"
        f"[auth] Authentication to '{host}' FAILED. Stages tried: {', '.join(tried) or 'none'}.\n"
        f"[auth]  - Kerberos/Negotiate library installed: "
        f"{'yes' if kerb is not None else 'NO (install ' + _sso_library_name() + ' for password-less SSO)'}\n"
        f"[auth]  - Test in your browser (should log you in or prompt): {base}/system/versions\n"
        f"[auth]  - Test Kerberos outside JMP:  curl --negotiate -u : {base}/system/versions\n"
        f"[auth]  - Test basic credentials:     curl -u USER {base}/system/versions\n"
        "[auth] If the browser works but JMP does not, the server likely only\n"
        "[auth] accepts Negotiate/Kerberos - check the SSO library above and the\n"
        "[auth] server's SPN configuration with the PI administrator.\n"
        "[auth] ============================================================",
        flush=True,
    )


def _decide_stage(base: str, host: str, scheme: str, verify) -> tuple[str, object]:
    """Probe /system/versions to pick the first working stage (PI hosts only).

    Returns (stage_name, auth_object). Raises AuthRequired when nothing works.
    """
    tried: list[str] = []

    candidates: list[tuple[str, object]] = []
    kerb = _kerberos_auth()
    if kerb is not None:
        candidates.append(("kerberos", kerb))
    else:
        print(f"[auth] Kerberos SSO unavailable ({_sso_library_name()} not installed).", flush=True)
    basic = _basic_auth_for(host, scheme)
    if basic is not None:
        candidates.append(("basic", basic))

    for stage, auth_obj in candidates:
        try:
            r = requests.get(f"{base}/system/versions", auth=auth_obj,
                             verify=verify, timeout=20, allow_redirects=True)
        except Exception as ex:  # network-level failure: not an auth problem
            raise RuntimeError(f"Cannot reach {base}/system/versions: {ex}") from ex
        print(f"[auth] Probe GET {base}/system/versions [{stage}] -> {r.status_code}", flush=True)
        if r.status_code != 401:
            print(f"[auth] '{host}': authenticating with {stage}"
                  + (" (no password needed)" if stage == "kerberos" else ""), flush=True)
            return stage, auth_obj
        tried.append(stage)
        if stage == "basic":
            # stored password rejected -> stale: remove it from the vault
            if host in _persisted_hosts:
                forget_credentials(host)
            _basic_credentials.pop(host, None)

    _log_auth_failure(host, base, tried)
    raise AuthRequired(host)


def get_session(server_url: str, verify: bool | None = None) -> requests.Session:
    """Session for `server_url`, authenticated per the staged strategy above.

    Cached per host and per thread (bulk extraction uses a thread pool).
    """
    _inject_truststore()
    if verify is None:
        verify = VERIFY_TLS

    parsed = urlparse(server_url)
    host = parsed.hostname or server_url
    if not hasattr(_thread_local, "sessions"):
        _thread_local.sessions = {}
    if host in _thread_local.sessions:
        return _thread_local.sessions[host]

    s = requests.Session()
    s.verify = verify
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    is_pi = "piwebapi" in server_url.lower()
    if is_pi and host not in _host_stage:
        # First contact with a PI host: decide the strategy once, with an
        # explicit, logged probe of /system/versions.
        stage, auth_obj = _decide_stage(server_url.rstrip("/"), host, parsed.scheme, verify)
        _host_stage[host] = stage
        s.auth = auth_obj
    elif _host_stage.get(host) == "basic":
        s.auth = _basic_auth_for(host, parsed.scheme)
    elif _host_stage.get(host) == "kerberos":
        s.auth = _kerberos_auth()
    else:
        # No probe (IP21 hosts): same order, decided by the first real
        # request — Kerberos first (no password), then stored/typed credentials.
        s.auth = _kerberos_auth() or _basic_auth_for(host, parsed.scheme)

    _thread_local.sessions[host] = s
    return s


def check_response(resp: requests.Response) -> requests.Response:
    """raise_for_status, but converts 401 into AuthRequired for the JSL dialog.

    On 401: log a full diagnostic block (stages, test URLs), drop the stale
    stage cache and any stored password that just failed, then raise
    AuthRequired so the JSL login dialog can collect fresh credentials.
    """
    if resp.status_code == 401:
        host = urlparse(resp.url).hostname or resp.url
        base = f"{urlparse(resp.url).scheme}://{host}"
        _log_auth_failure(host, base, [_host_stage.get(host, "first-request")])
        if host in _persisted_hosts:
            forget_credentials(host)
        _basic_credentials.pop(host, None)
        _host_stage.pop(host, None)
        _drop_session(host)
        raise AuthRequired(host)
    resp.raise_for_status()
    return resp
