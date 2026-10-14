"""
Manus (manus.im) cookie checker.

  session_id cookie (JWT) → Bearer token
  POST https://api.manus.im/user.v1.UserService/UserInfo
  POST https://api.manus.im/user.v1.UserService/GetAvailableCredits
  Headers: Connect-Protocol-Version: 1
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import httpx

from .cookie_parser import ParsedManusCookie

API_HOST = "https://api.manus.im"
USER_INFO = f"{API_HOST}/user.v1.UserService/UserInfo"
CREDITS = f"{API_HOST}/user.v1.UserService/GetAvailableCredits"
DASHBOARD = "https://manus.im/app"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

PAID_TIERS = (
    "pro",
    "plus",
    "premium",
    "team",
    "business",
    "enterprise",
    "membership_tier_pro",
    "membership_tier_plus",
    "membership_tier_team",
)


def build_cookie_string(cookie: ParsedManusCookie) -> str:
    jar: Dict[str, str] = {}
    for row in cookie.all_cookies or []:
        n = (row.get("name") or "").strip()
        v = str(row.get("value") or "")
        if n and v:
            jar[n] = v
    if cookie.session_id:
        jar["session_id"] = cookie.session_id
    return "; ".join(f"{k}={v}" for k, v in jar.items() if v)


def build_netscape_lines(cookie: ParsedManusCookie) -> List[str]:
    if cookie.netscape_lines:
        return list(cookie.netscape_lines)
    lines: List[str] = []
    for row in cookie.all_cookies or []:
        name = row.get("name") or ""
        value = row.get("value") or ""
        if not name or not value:
            continue
        if row.get("raw_line"):
            lines.append(row["raw_line"])
            continue
        dom = row.get("domain") or ".manus.im"
        path = row.get("path") or "/"
        secure = row.get("secure") or "TRUE"
        exp = row.get("expires") or "0"
        sub = "FALSE" if row.get("hostOnly") == "TRUE" else "TRUE"
        lines.append(f"{dom}\t{sub}\t{path}\t{secure}\t{exp}\t{name}\t{value}")
    if not lines and cookie.session_id:
        lines.append(
            f".manus.im\tTRUE\t/\tTRUE\t0\tsession_id\t{cookie.session_id}"
        )
    return lines


def _normalize_tier(raw: Any) -> str:
    if raw is None:
        return "free"
    s = str(raw).strip()
    if not s:
        return "free"
    # MEMBERSHIP_TIER_PRO → Pro
    s2 = s.replace("MEMBERSHIP_TIER_", "").replace("membership_tier_", "")
    s2 = s2.replace("_", " ").strip()
    if not s2 or s2.lower() in ("free", "basic", "none", "unknown"):
        return "Free"
    return s2.title() if s2.isupper() or "_" in s else s2


def is_premium_plan(plan: str) -> bool:
    p = (plan or "").lower().replace(" ", "_").replace("-", "_")
    if not p or p in ("free", "basic", "unknown", "n/a", "none"):
        return False
    return any(t in p for t in PAID_TIERS)


def plan_kind(plan: str) -> str:
    p = (plan or "").lower()
    if any(x in p for x in ("team", "business", "enterprise")):
        return "TEAM"
    return "PREMIUM" if is_premium_plan(plan) else "FREE"


def plan_folder(plan: str) -> str:
    kind = plan_kind(plan)
    if kind == "TEAM":
        return "Team"
    if kind == "PREMIUM":
        return "Pro"
    return "Free"


def _unwrap_credits(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    for key in ("data", "result", "response", "availableCredits"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            if any(
                k in inner
                for k in (
                    "totalCredits",
                    "freeCredits",
                    "periodicCredits",
                    "refreshCredits",
                )
            ):
                return inner
    return payload


async def check_manus_cookie(cookie: ParsedManusCookie) -> Dict[str, Any]:
    sid = (cookie.session_id or cookie.session_token or "").strip()
    details: Dict[str, Any] = {"method": "connect-rpc"}
    if not sid:
        return {
            "cookie": cookie,
            "status": "invalid",
            "message": "No session_id",
            "details": details,
        }

    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {sid}",
        "Connect-Protocol-Version": "1",
        "Origin": "https://manus.im",
        "Referer": "https://manus.im/app",
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(22.0, connect=10.0),
            follow_redirects=True,
            headers=headers,
            http2=False,
        ) as client:
            ui = await client.post(USER_INFO, json={})
            cr = await client.post(CREDITS, json={})
            details["userHttp"] = ui.status_code
            details["creditsHttp"] = cr.status_code

            if ui.status_code in (401, 403) or cr.status_code in (401, 403):
                return {
                    "cookie": cookie,
                    "status": "expired",
                    "message": "Invalid / expired session_id",
                    "details": details,
                }

            try:
                udata = ui.json() if ui.status_code == 200 else {}
            except Exception:
                udata = {}
            try:
                cdata = cr.json() if cr.status_code == 200 else {}
            except Exception:
                cdata = {}

            # Connect error envelope
            if isinstance(udata, dict) and udata.get("code") in (
                "unauthenticated",
                "permission_denied",
            ):
                return {
                    "cookie": cookie,
                    "status": "expired",
                    "message": str(udata.get("message") or "unauthenticated"),
                    "details": details,
                }

            if ui.status_code != 200:
                return {
                    "cookie": cookie,
                    "status": "error",
                    "message": f"UserInfo HTTP {ui.status_code}",
                    "details": details,
                }

            email = str(
                udata.get("email")
                or udata.get("Email")
                or cookie.email
                or "unknown"
            )
            name = str(
                udata.get("displayname")
                or udata.get("displayName")
                or udata.get("firstname")
                or udata.get("name")
                or ""
            )
            user_id = str(udata.get("userId") or udata.get("uid") or "")
            mi = udata.get("membershipInfo")
            mi_ver = mi.get("membershipVersion") if isinstance(mi, dict) else ""
            tier_raw = (
                udata.get("membershipTier")
                or udata.get("membershipTierName")
                or mi_ver
                or udata.get("membershipVersion")
                or udata.get("tier")
                or udata.get("plan")
                or "Free"
            )
            plan = _normalize_tier(tier_raw)
            credits = _unwrap_credits(cdata)
            total = credits.get("totalCredits")
            periodic = credits.get("periodicCredits")
            free_c = credits.get("freeCredits")
            refresh = credits.get("refreshCredits")
            pro_monthly = credits.get("proMonthlyCredits")

            # Paid if tier says so OR has periodic/pro credits
            if not is_premium_plan(plan):
                try:
                    if int(periodic or 0) > 0 or int(pro_monthly or 0) > 0:
                        plan = "Pro"
                except Exception:
                    pass

            folder = plan_folder(plan)
            kind = plan_kind(plan)
            sms_verified = udata.get("smsVerified")
            details.update(
                {
                    "email": email if "@" in email else (email or user_id or "unknown"),
                    "name": name or (email.split("@")[0] if "@" in email else "manus"),
                    "userId": user_id,
                    "plan": plan,
                    "planFolder": folder,
                    "kind": kind,
                    "isPaid": kind != "FREE",
                    "totalCredits": total,
                    "periodicCredits": periodic,
                    "freeCredits": free_c,
                    "refreshCredits": refresh,
                    "proMonthlyCredits": pro_monthly,
                    "nextRefreshTime": credits.get("nextRefreshTime"),
                    "refreshInterval": credits.get("refreshInterval"),
                    # Phone gate: accounts with smsVerified === false need
                    # phone verification before full use (mail-mode skips them).
                    "smsVerified": sms_verified,
                    "phoneGate": sms_verified is False,
                }
            )
            cookie.email = str(details["email"])
            cookie.plan = plan
            cookie.name = str(details["name"])
            cookie.raw_cookie = build_cookie_string(cookie)

            return {
                "cookie": cookie,
                "status": "valid",
                "message": f"{plan} · {details['email']} · credits={total}",
                "details": details,
            }
    except httpx.TimeoutException:
        return {
            "cookie": cookie,
            "status": "error",
            "message": "timeout",
            "details": details,
        }
    except Exception as e:
        return {
            "cookie": cookie,
            "status": "error",
            "message": str(e)[:160],
            "details": details,
        }


def format_account_file(result: Dict[str, Any], index: int = 1) -> str:
    d = result.get("details") or {}
    c: ParsedManusCookie = result.get("cookie")
    email = d.get("email") or getattr(c, "email", None) or "unknown"
    plan = d.get("plan") or getattr(c, "plan", None) or "unknown"
    lines = [
        f"# Manus account #{index}",
        f"# Email: {email}",
        f"# Name: {d.get('name') or ''}",
        f"# Plan: {plan}",
        f"# User ID: {d.get('userId') or ''}",
        f"# Credits: total={d.get('totalCredits')} periodic={d.get('periodicCredits')} "
        f"refresh={d.get('refreshCredits')}",
        f"# Dashboard: {DASHBOARD}",
        "# Netscape HTTP Cookie File",
    ]
    if c:
        lines.extend(build_netscape_lines(c))
    lines.append("")
    return "\n".join(lines)


def hit_filename(result: Dict[str, Any]) -> str:
    d = result.get("details") or {}
    c = result.get("cookie")
    email = str(d.get("email") or getattr(c, "email", "") or "unknown")
    folder = str(d.get("planFolder") or plan_folder(str(d.get("plan") or "")))
    safe_email = re.sub(r"[^\w.\-@+]+", "_", email)[:80]
    return f"[{safe_email}][{folder}].txt"
