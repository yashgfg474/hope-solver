"""Manus password-login (email + password) via the real web API.

Reversed from the manus.im Next.js bundle (2026-10):
  base    = https://api.manus.im
  service = user.v1.UserAuthPublicService   (Connect-RPC, JSON, public)
  flow:
    1) getUserPlatforms {email, cfCaptchaCode} -> {platforms, tempToken}
       (Cloudflare Turnstile token mandatory; sitekey below)
    2) loginByEmail {email, password, token: tempToken}
       headers: x-client-dcr (device fp, best-effort), x-client-id (uuid)
       -> {token}
    3) token works as Bearer for UserInfo / GetAvailableCredits
       (same as the `session_id` cookie used by sites/manus/checker.py)

Captcha is solved via Capsolver (preferred) or 2captcha when an API key
is provided via env (CAPSOLVER_KEY / TWOCAPTCHA_KEY) or argument.
Without a solver key the functions return status="captcha".
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional

import httpx

API_HOST = "https://api.manus.im"
AUTH = f"{API_HOST}/user.v1.UserAuthPublicService"
LOGIN_URL = "https://manus.im/login"
TURNSTILE_SITEKEY = "0x4AAAAAAA_sd0eRNCinWBgU"

# Community Turnstile solver (Cloudflare Tunnel; URL changes on restart —
# override via TUNNEL_SOLVER_URL env). POST {"mode","domain","siteKey"}.
TUNNEL_SOLVER_URL = (
    __import__("os").environ.get("TUNNEL_SOLVER_URL")
    or "https://novels-expand-fastest-hear.trycloudflare.com/cloudflare"
)


def solve_via_tunnel(
    *,
    url: str = "",
    sitekey: str = "",
    endpoint: str = "",
    timeout: float = 120.0,
) -> str:
    """Turnstile solver over Cloudflare Tunnel. Returns token or ''.

    POST {endpoint} {"mode":"turnstile","domain":full_page_url,"siteKey":key}
      -> {"token":"..."}  (500 + message on failure)
    NOTE: domain must be a FULL URL (https://...) — bare hosts 500.
    """
    ep = (endpoint or "").strip() or TUNNEL_SOLVER_URL
    if not ep:
        return ""
    payload = {
        "mode": "turnstile",
        "domain": (url or "").strip() or LOGIN_URL,
        "siteKey": (sitekey or "").strip() or TURNSTILE_SITEKEY,
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(float(timeout), connect=10.0)) as c:
            r = c.post(ep, json=payload)
            if r.status_code != 200:
                return ""
            try:
                data = r.json()
            except Exception:
                return ""
            tok = str(data.get("token") or "")
            return tok if len(tok) > 20 else ""
    except Exception:
        return ""

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

CLIENT_ID_FILE = "/tmp/opencode/manus_client_id.txt"


def get_client_id() -> str:
    """Stable per-install client id (the web client persists one; a fresh
    uuid per call is a bot signal)."""
    try:
        with open(CLIENT_ID_FILE, encoding="utf-8") as f:
            cid = (f.read() or "").strip()
            if cid:
                return cid
    except Exception:
        pass
    cid = str(uuid.uuid4()).replace("-", "")[:22] or str(uuid.uuid4())
    try:
        with open(CLIENT_ID_FILE, "w", encoding="utf-8") as f:
            f.write(cid)
    except Exception:
        pass
    return cid


def _base_headers() -> Dict[str, str]:
    return {
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "Origin": "https://manus.im",
        "Referer": LOGIN_URL,
        # Full web-client header set (matches the real bundle exactly).
        "x-client-id": get_client_id(),
        "x-client-locale": "en",
        "x-client-timezone": "Asia/Calcutta",
        "x-client-timezone-offset": "-330",
        "x-client-type": "web",
        "x-client-version": "prod-2.0.46",
    }


def solve_via_ours(*, url: str = "", sitekey: str = "",
                   timeout: float = 120.0) -> str:
    """Our own solver API (sites/manus/solver_api.py). Returns token or ''.

    Uses MANUS_SOLVER_API base (default http://127.0.0.1:8077) — silent
    skip when the server isn't running.
    """
    import os as _os

    base = (_os.environ.get("MANUS_SOLVER_API") or "http://127.0.0.1:8077").rstrip("/")
    page = (url or "").strip() or LOGIN_URL
    key = (sitekey or "").strip() or TURNSTILE_SITEKEY
    import urllib.parse as _up

    qs = _up.urlencode({"url": page, "sitekey": key})
    try:
        with httpx.Client(timeout=httpx.Timeout(float(timeout), connect=5.0)) as c:
            r = c.get(f"{base}/solve?{qs}")
            if r.status_code != 200:
                return ""
            try:
                data = r.json()
            except Exception:
                return ""
            if str(data.get("status") or "").lower() != "success":
                return ""
            tok = str(data.get("token") or "")
            return tok if len(tok) > 20 else ""
    except Exception:
        return ""


def solve_via_fahim(
    *,
    url: str = "",
    sitekey: str = "",
    timeout: float = 90.0,
) -> str:
    """Free Turnstile solver API (smfahim.xyz). Returns token or ''.

    GET /tools/captchasolver?url={page}&sitekey={key}
      -> {"status":"success","token":"...","time_taken":4.77}
    """
    import urllib.parse

    page = (url or "").strip() or LOGIN_URL
    key = (sitekey or "").strip() or TURNSTILE_SITEKEY
    qs = urllib.parse.urlencode({"url": page, "sitekey": key})
    # Two attempts: the free API intermittently fails under load.
    for attempt in (1, 2):
        try:
            with httpx.Client(timeout=httpx.Timeout(float(timeout), connect=10.0)) as c:
                r = c.get(f"https://smfahim.xyz/tools/captchasolver?{qs}",
                          headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code != 200:
                    continue
                try:
                    data = r.json()
                except Exception:
                    continue
                if str(data.get("status") or "").lower() != "success":
                    continue
                tok = str(data.get("token") or "")
                if len(tok) > 20:
                    return tok
        except Exception:
            pass
        if attempt == 1:
            time.sleep(5)
    return ""


def solve_turnstile(
    *,
    capsolver_key: str = "",
    twocaptcha_key: str = "",
    proxy: str = "",
    timeout: float = 120.0,
    email: str = "",
    no_browser: bool = False,
    solver: str = "",
) -> str:
    """Solve Cloudflare Turnstile for manus.im/login. Returns token or ''.

    Default (""): Fahim free API only — fastest reliable path.
    "ours"/"auto": OUR widget-page solver first, then Fahim.
    "all": + tunnel → browser harvest → Capsolver → 2captcha.
    Pass no_browser=True to skip local Chromium.
    """
    try:
        # Our own API if running (fast fail when down).
        tok = solve_via_ours(url=LOGIN_URL, sitekey=TURNSTILE_SITEKEY,
                             timeout=min(timeout, 20.0))
        if tok:
            return tok
    except Exception:
        pass
    if solver in ("ours", "auto") and not no_browser:
        try:
            from sites.manus import turnstile as _ts
        except Exception:
            try:
                from . import turnstile as _ts  # type: ignore
            except Exception:
                _ts = None  # type: ignore
        if _ts is not None:
            try:
                tok = _ts.solve_via_widget_page(
                    url=LOGIN_URL, sitekey=TURNSTILE_SITEKEY,
                    timeout=min(timeout, 45.0), proxy=proxy)
                if tok:
                    return tok
            except Exception:
                pass
    if solver == "all":
        try:
            tok = solve_via_tunnel(url=LOGIN_URL, sitekey=TURNSTILE_SITEKEY,
                                   timeout=min(timeout, 60.0))
            if tok:
                return tok
        except Exception:
            pass
    try:
        tok = solve_via_fahim(url=LOGIN_URL, sitekey=TURNSTILE_SITEKEY,
                              timeout=min(timeout, 90.0))
        if tok:
            return tok
    except Exception:
        pass
    if solver != "all":
        return ""
    if not no_browser:
        try:
            from sites.manus import turnstile as _ts
        except Exception:
            try:
                from . import turnstile as _ts  # type: ignore
            except Exception:
                _ts = None  # type: ignore
        if _ts is not None:
            try:
                # Capped: the browser path rarely pays off vs waiting for the
                # free APIs; keep worst-case per-combo time bounded.
                tok = _ts.harvest_token(email=email, timeout=min(timeout, 60.0), proxy=proxy)
                if tok:
                    return tok
            except Exception:
                pass
    if capsolver_key:
        try:
            with httpx.Client(timeout=timeout) as c:
                task = {
                    "type": "AntiTurnstileTaskProxyLess",
                    "websiteURL": LOGIN_URL,
                    "websiteKey": TURNSTILE_SITEKEY,
                    "metadata": {"action": "login_by_email"},
                }
                r = c.post(
                    "https://api.capsolver.com/createTask",
                    json={"clientKey": capsolver_key, "task": task},
                ).json()
                task_id = (r.get("solution") or {}).get("taskId") or r.get("taskId") or ""
                if not task_id and r.get("errorId"):
                    return ""
                t0 = time.time()
                while time.time() - t0 < timeout:
                    time.sleep(3)
                    g = c.post(
                        "https://api.capsolver.com/getTaskResult",
                        json={"clientKey": capsolver_key, "taskId": task_id},
                    ).json()
                    if g.get("status") == "ready":
                        return str(((g.get("solution") or {}).get("token")) or "")
                    if g.get("status") == "failed":
                        return ""
        except Exception:
            return ""
    if twocaptcha_key:
        try:
            with httpx.Client(timeout=timeout) as c:
                r = c.post(
                    "https://api.2captcha.com/createTask",
                    json={
                        "clientKey": twocaptcha_key,
                        "task": {
                            "type": "TurnstileTaskProxyless",
                            "websiteURL": LOGIN_URL,
                            "websiteKey": TURNSTILE_SITEKEY,
                        },
                    },
                ).json()
                task_id = r.get("taskId") or ""
                if not task_id:
                    return ""
                t0 = time.time()
                while time.time() - t0 < timeout:
                    time.sleep(5)
                    g = c.post(
                        "https://api.2captcha.com/getTaskResult",
                        json={"clientKey": twocaptcha_key, "taskId": task_id},
                    ).json()
                    if g.get("status") == "ready":
                        return str(((g.get("solution") or {}).get("token")) or "")
        except Exception:
            return ""
    return ""


def decode_api_error(data: Any, http: int = 0) -> tuple:
    """Extract (code, clean_message) from a Manus Connect error envelope.

    Envelope: {"code":"invalid_argument","message":"Error<1014>",
               "details":[{"type":"common.v1.ErrorDetail",
                           "value":"<base64>","debug":{"code":"1014",
                           "message":"Password does not match"}}]}
    Known debug codes: 1014 wrong password · 1715 captcha required.
    """
    code, msg = "", ""
    try:
        dets = (data or {}).get("details") or []
        if dets and isinstance(dets[0], dict):
            dbg = dets[0].get("debug") or {}
            code = str(dbg.get("code") or "")
            msg = str(dbg.get("message") or "")
            if not msg:
                import base64 as _b64

                v = str(dets[0].get("value") or "")
                try:
                    msg = _b64.b64decode(v + "=" * (-len(v) % 4)).decode(
                        "utf-8", errors="ignore")
                except Exception:
                    msg = v
    except Exception:
        pass
    if not msg:
        try:
            msg = str((data or {}).get("message") or f"HTTP {http}")
        except Exception:
            msg = f"HTTP {http}"
    return code, msg[:160]


def classify_login_error(code: str, msg: str, http: int = 0) -> tuple:
    """Map (debug code, message, http) → (status, clean message)."""
    low = (msg or "").lower()
    if code == "1014" or any(
            k in low for k in ("password does not match", "wrong password",
                               "incorrect password", "invalid password")):
        return ("invalid", "wrong password")
    if code == "1715" or "captcha" in low:
        return ("captcha", "Turnstile required (token rejected)")
    if http == 429 or "rate" in low or "too many" in low:
        return ("rate_limit", "rate limited — retry later")
    if any(k in low for k in ("phone", "sms", "verify", "otp", "2fa",
                              "two-factor", "mfa")):
        return ("2fa", msg[:160] or "verification required")
    if any(k in low for k in ("locked", "disabled", "suspended", "banned")):
        return ("locked", msg[:160] or "locked")
    if any(k in low for k in ("not found", "no account", "not registered",
                              "user does not exist", "unauthenticated")):
        return ("invalid", "no Manus account for this email")
    return ("error", msg[:160] or f"HTTP {http}")


import threading as _threading

_TLS = _threading.local()


def thread_client(proxy_url: Optional[str] = None,
                  timeout: float = 30.0) -> httpx.Client:
    """Persistent per-thread HTTP client (connection reuse saves ~1s/combo
    vs fresh TLS handshakes every call). Thread-local = thread-safe."""
    cache = getattr(_TLS, "clients", None)
    if cache is None:
        cache = {}
        _TLS.clients = cache
    key = (proxy_url or "", float(timeout))
    cli = cache.get(key)
    if cli is None:
        kw: Dict[str, Any] = {
            "timeout": httpx.Timeout(float(timeout), connect=10.0),
            "follow_redirects": True,
            "http2": False,
        }
        if proxy_url:
            kw["proxy"] = proxy_url
        cli = httpx.Client(**kw)
        cache[key] = cli
    return cli


def _client(proxy_url: Optional[str], timeout: float) -> httpx.Client:
    kw: Dict[str, Any] = {"timeout": httpx.Timeout(float(timeout), connect=10.0),
                          "follow_redirects": True, "http2": False}
    if proxy_url:
        kw["proxy"] = proxy_url
    return httpx.Client(**kw)


def email_login(
    email: str,
    password: str,
    *,
    captcha_token: str = "",
    client_id: str = "",
    dcr: str = "",
    proxy_url: Optional[str] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Full email+password login. Never raises.

    Returns {"status": "valid"/"invalid"/"captcha"/"rate_limit"/"error",
             "token": <session token on valid>, "message": ..., "details": {...}}
    """
    out: Dict[str, Any] = {"status": "error", "message": "", "token": "",
                           "details": {"engine": "manus_auth"}}
    email, password = (email or "").strip(), (password or "").strip()
    if not email or not password or "@" not in email:
        out.update(status="invalid", message="bad combo format")
        return out
    client_id = client_id or get_client_id()
    try:
        # Persistent per-thread client: platforms + login share one
        # keep-alive connection (no per-call TLS handshake).
        c = thread_client(proxy_url, timeout)
        if True:
            # Step 1: platforms + tempToken (captcha mandatory)
            h = _base_headers()
            if dcr:
                h["x-client-dcr"] = dcr
            try:
                r = c.post(f"{AUTH}/GetUserPlatforms",
                           json={"email": email, "cfCaptchaCode": captcha_token},
                           headers=h)
            except Exception as e:
                out.update(status="error", message=f"platforms net error: {e}"[:160])
                return out
            if r.status_code != 200:
                try:
                    edata = r.json()
                except Exception:
                    edata = {}
                ecode, emsg = decode_api_error(edata, r.status_code)
                st, clean = classify_login_error(ecode, emsg, r.status_code)
                # platforms step: a captcha verdict here means the token was
                # missing/rejected — keep the captcha status explicit.
                if st == "error" and ("captcha" in emsg.lower()
                                      or "challenge" in emsg.lower()):
                    st, clean = "captcha", "Turnstile required (no valid token)"
                out["details"]["platforms_http"] = r.status_code
                out["details"]["api_code"] = ecode or str(r.status_code)
                out.update(status=st, message=clean)
                return out
            try:
                pdata = r.json()
            except Exception:
                pdata = {}
            platforms = pdata.get("platforms") or []
            temp_token = str(pdata.get("tempToken") or pdata.get("temp_token") or "")
            out["details"]["platforms"] = platforms
            if not temp_token:
                # email-only account (Google/Apple/Microsoft SSO) — no password login
                out.update(status="invalid",
                           message=f"no password login for this email (platforms={platforms})"[:160])
                return out
            # Step 2: password login
            h2 = _base_headers()
            if dcr:
                h2["x-client-dcr"] = dcr
            try:
                r2 = c.post(f"{AUTH}/LoginByEmail",
                            json={"email": email, "password": password, "token": temp_token},
                            headers=h2)
            except Exception as e:
                out.update(status="error", message=f"login net error: {e}"[:160])
                return out
            if r2.status_code != 200:
                try:
                    edata = r2.json()
                except Exception:
                    edata = {}
                ecode, emsg = decode_api_error(edata, r2.status_code)
                st, clean = classify_login_error(ecode, emsg, r2.status_code)
                out["details"]["api_code"] = ecode or str(r2.status_code)
                out.update(status=st, message=clean)
                return out
            try:
                ldata = r2.json()
            except Exception:
                ldata = {}
            token = str(ldata.get("token") or ldata.get("session_id") or "")
            if not token:
                out.update(status="error", message=f"login ok but no token: {r2.text[:120]}")
                return out
            out.update(status="valid", message="login ok", token=token)
            out["details"]["token_len"] = len(token)
            return out
    except Exception as e:
        out.update(status="error", message=str(e)[:160])
        return out
