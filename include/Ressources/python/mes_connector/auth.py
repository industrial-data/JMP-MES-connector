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
    _bump_generation()


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
    _dialog_hosts.discard(host)
    _host_auth.pop(host, None)
    _bump_generation()
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


def set_credentials(host_or_url: str, user: str, password: str,
                    remember: bool = False) -> None:
    """Store credentials (from the JSL login dialog).

    remember=True additionally writes them to the OS credential vault so
    future JMP sessions authenticate without prompting.
    """
    host = urlparse(host_or_url).hostname or host_or_url
    _basic_credentials[host] = (user, password)
    _dialog_hosts.add(host)          # dialog credentials take priority now
    _host_auth.pop(host, None)       # re-decide the mechanism with them
    if remember and save_credentials(host, user, password):
        _persisted_hosts.add(host)
    _bump_generation()               # all threads rebuild their sessions


def clear_credentials() -> None:
    _basic_credentials.clear()
    _persisted_hosts.clear()
    _dialog_hosts.clear()
    _host_auth.clear()
    _bump_generation()


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
# For every host, an ordered list of MECHANISMS is tried (each attempt is
# logged to the JMP log):
#
#   With credentials typed in the JSL login dialog THIS session (they always
#   take priority — the previous version kept using Kerberos and never
#   attached them, which made the dialog appear to "not work"):
#       1. NTLM with the typed credentials   (works on IIS servers where
#          Basic auth is disabled — the common PI Web API setup; the password
#          is never sent in clear, so NTLM is allowed even over http)
#       2. Basic with the typed credentials  (https only)
#       3. Kerberos/Negotiate single sign-on
#
#   Without dialog credentials:
#       1. Kerberos/Negotiate single sign-on (no password at all)
#       2. NTLM with OS-vault credentials
#       3. Basic with OS-vault credentials   (https only)
#
# PI Web API hosts decide the winning mechanism ONCE by probing the cheap
# /system/versions endpoint; the winning auth object is cached in _host_auth.
# Other hosts (IP21) attach the first available mechanism and let the first
# real request decide. Any 401 afterwards clears the caches, logs a
# diagnostic block with test URLs, and raises AuthRequired (-> JSL dialog).
#
# Credential hygiene on failure: vault-stored passwords that fail are deleted
# (stale); dialog-typed passwords are kept until the user retypes them, so a
# transient failure does not silently discard what the user just entered.

# host -> winning auth object (decided by probe or first success)
_host_auth: dict[str, object] = {}
# hosts whose current credentials came from the JSL dialog this session
_dialog_hosts: set[str] = set()
# bumped on every credential/TLS change so per-thread session caches rebuild
_auth_generation = 0


def _bump_generation() -> None:
    global _auth_generation
    _auth_generation += 1


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


def _ntlm_auth(user: str, password: str):
    """Explicit-credential NTLM (challenge/response — no cleartext password),
    or None when requests-ntlm is not installed."""
    try:
        from requests_ntlm import HttpNtlmAuth
        return HttpNtlmAuth(user, password)
    except ImportError:
        return None


def _sso_library_name() -> str:
    return "requests-negotiate-sspi" if sys.platform == "win32" else "requests-kerberos"


def _stored_credentials(host: str):
    """(user, pwd) from memory or the OS vault, else None."""
    if host not in _basic_credentials:
        saved = load_saved_credentials(host)
        if saved is not None:
            _basic_credentials[host] = saved
            _persisted_hosts.add(host)
            print(f"[auth] Using stored credentials for '{host}' from the OS vault.", flush=True)
    return _basic_credentials.get(host)


def _candidates(host: str, scheme: str) -> list[tuple[str, object]]:
    """Ordered (label, auth) mechanisms for this host — see module comment."""
    out: list[tuple[str, object]] = []
    creds = _stored_credentials(host)

    def add_cred_mechanisms():
        if creds is None:
            return
        user, pwd = creds
        ntlm = _ntlm_auth(user, pwd)
        if ntlm is not None:
            out.append(("ntlm", ntlm))
        else:
            print("[auth] requests-ntlm not installed - cannot try NTLM with "
                  "explicit credentials (pip name: requests-ntlm).", flush=True)
        if scheme == "http":
            print(f"[auth] Basic auth skipped for '{host}': plain http would "
                  "send the password unencrypted (NTLM/Kerberos are still tried).", flush=True)
        else:
            out.append(("basic", tuple(creds)))

    kerb = _kerberos_auth()
    if host in _dialog_hosts and creds is not None:
        add_cred_mechanisms()           # the user just typed these: use them!
        if kerb is not None:
            out.append(("kerberos", kerb))
    else:
        if kerb is not None:
            out.append(("kerberos", kerb))
        else:
            print(f"[auth] Kerberos SSO unavailable ({_sso_library_name()} not installed).", flush=True)
        add_cred_mechanisms()
    return out


def _log_auth_failure(host: str, base: str, tried: list[str]) -> None:
    """One readable block in the JMP log explaining WHAT failed and HOW to test."""
    kerb = _kerberos_auth()
    ntlm_available = _ntlm_auth("probe", "probe") is not None
    print(
        "[auth] ============================================================\n"
        f"[auth] Authentication to '{host}' FAILED. Mechanisms tried on the real request: {', '.join(tried) or 'none available'}.\n"
        f"[auth]  - Kerberos/Negotiate library: "
        f"{'installed' if kerb is not None else 'MISSING (' + _sso_library_name() + ')'}"
        f" | NTLM library: {'installed' if ntlm_available else 'MISSING (requests-ntlm)'}\n"
        f"[auth]  - Test in your browser:              {base}/system/versions\n"
        f"[auth]  - Test Kerberos SSO outside JMP:     curl --negotiate -u : {base}/system/versions\n"
        f"[auth]  - Test NTLM with your credentials:   curl --ntlm -u DOMAIN\\\\user {base}/system/versions\n"
        f"[auth]  - Test Basic with your credentials:  curl -u user {base}/system/versions\n"
        "[auth] For domain accounts type the user as DOMAIN\\\\user or user@domain.\n"
        "[auth] If the browser works but every mechanism above fails, ask the PI\n"
        "[auth] administrator which authentication methods the server allows\n"
        "[auth] (IIS: Windows Authentication providers / Basic) and check the SPN.\n"
        "[auth] ============================================================",
        flush=True,
    )


def _as_auth_callable(auth_obj):
    """requests auth objects are callables on PreparedRequest; wrap tuples."""
    if isinstance(auth_obj, tuple):
        from requests.auth import HTTPBasicAuth
        return HTTPBasicAuth(*auth_obj)
    return auth_obj


def _retry_with_mechanisms(resp: requests.Response) -> requests.Response | None:
    """Replay the EXACT failing request with each candidate mechanism.

    This is the core of the strategy: mechanisms are validated against the
    real endpoint that rejected us — never against a probe endpoint, which
    on PI Web API can answer anonymously and validate nothing. The first
    mechanism that gets a non-401 is cached for the host and its response is
    returned so the caller continues transparently.
    """
    parsed = urlparse(resp.url)
    host = parsed.hostname or resp.url
    tried: list[str] = []

    for label, auth_obj in _candidates(host, parsed.scheme):
        new_req = resp.request.copy()
        new_req.headers.pop("Authorization", None)   # let the mechanism re-sign
        try:
            prepared = _as_auth_callable(auth_obj)(new_req)
            with requests.Session() as s2:
                s2.verify = VERIFY_TLS
                r2 = s2.send(prepared, timeout=300, allow_redirects=True)
        except Exception as ex:
            print(f"[auth] Retry [{label}] failed to send: {ex}", flush=True)
            tried.append(label)
            continue
        print(f"[auth] Retry {new_req.method} {new_req.url} [{label}] -> {r2.status_code}", flush=True)
        if r2.status_code != 401:
            _host_auth[host] = auth_obj
            _bump_generation()   # every thread's next session uses the winner
            print(f"[auth] '{host}': authenticated with {label}"
                  + (" (no password needed)" if label == "kerberos" else ""), flush=True)
            return r2
        tried.append(label)

    # every mechanism failed on the real request
    base = f"{parsed.scheme}://{host}" + ("/piwebapi" if "piwebapi" in resp.url.lower() else "")
    if host in _persisted_hosts and host not in _dialog_hosts:
        forget_credentials(host)   # stale vault password
    _log_auth_failure(host, base, tried)
    return None


def get_session(server_url: str, verify: bool | None = None) -> requests.Session:
    """Session for `server_url` with the best-known mechanism attached.

    No probing: the winning mechanism is discovered by check_response() on
    the first real 401 and cached in _host_auth. Sessions are cached per
    host and per thread, invalidated by _auth_generation whenever the
    credentials/TLS/winner change.
    """
    _inject_truststore()
    if verify is None:
        verify = VERIFY_TLS

    parsed = urlparse(server_url)
    host = parsed.hostname or server_url
    if not hasattr(_thread_local, "sessions"):
        _thread_local.sessions = {}
    cached = _thread_local.sessions.get(host)
    if cached is not None and cached[0] == _auth_generation:
        return cached[1]

    s = requests.Session()
    s.verify = verify
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if host in _host_auth:
        s.auth = _host_auth[host]                     # known winner
    else:
        cands = _candidates(host, parsed.scheme)
        s.auth = cands[0][1] if cands else None       # best guess; 401 decides
        if cands:
            print(f"[auth] '{host}': starting with {cands[0][0]} "
                  "(a 401 will try the other mechanisms on the same request).", flush=True)

    _thread_local.sessions[host] = (_auth_generation, s)
    return s


def check_response(resp: requests.Response) -> requests.Response:
    """Return a successful response, cycling auth mechanisms on 401.

    IMPORTANT: callers must use the RETURN VALUE (`r = check_response(r)`) —
    when the first mechanism gets a 401, the request is transparently
    replayed with the remaining mechanisms and the successful response is
    the one returned. Only when every mechanism fails on the real request is
    AuthRequired raised (-> JSL login dialog), with the diagnostic block and
    test URLs already in the log.
    """
    if resp.status_code == 401:
        r2 = _retry_with_mechanisms(resp)
        if r2 is not None:
            return r2
        host = urlparse(resp.url).hostname or resp.url
        _host_auth.pop(host, None)
        _bump_generation()
        raise AuthRequired(host)
    resp.raise_for_status()
    return resp
