"""Manus (manus.im) cookie parser — requires session_id JWT."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

MANUS_DOMAIN_OK = (
    "manus.im",
    "www.manus.im",
    "api.manus.im",
    "manus.ai",
    "open.manus.ai",
)

AUTH_NAMES = ("session_id", "session-id", "sessionid")


@dataclass
class ParsedManusCookie:
    session_token: str  # session_id JWT
    raw_cookie: str
    email: str = "unknown"
    name: str = "unknown"
    plan: str = "unknown"
    source: Optional[str] = None
    session_id: str = ""
    all_cookies: List[Dict[str, str]] = field(default_factory=list)
    netscape_lines: List[str] = field(default_factory=list)

    @property
    def sessionToken(self) -> str:
        return self.session_token


def _is_manus_domain(domain: str) -> bool:
    d = (domain or "").lower().lstrip(".")
    return any(ok in d for ok in MANUS_DOMAIN_OK) or d.endswith("manus.im")


def _hint_email(text: str) -> str:
    m = EMAIL_RE.search(text or "")
    return m.group(0) if m else "unknown"


def _parse_netscape_lines(text: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for line in text.splitlines():
        l = line.strip().lstrip("\ufeff")
        if not l:
            continue
        httponly = False
        if l.startswith("#") and not re.match(r"#HttpOnly_", l, re.I):
            continue
        if re.match(r"#HttpOnly_", l, re.I):
            httponly = True
            l = re.sub(r"^#HttpOnly_", "", l, flags=re.I)

        if "\t" not in l and "=" in l and (
            "session_id=" in l.lower()
            or "manus.im" in l.lower()
            or "manus.ai" in l.lower()
        ):
            for part in re.split(r";\s*", l):
                if "=" not in part:
                    continue
                n, v = part.split("=", 1)
                n, v = n.strip(), v.strip().strip('"')
                if n and v:
                    out.append(
                        {
                            "domain": ".manus.im",
                            "name": n,
                            "value": v,
                            "path": "/",
                            "secure": "TRUE",
                            "hostOnly": "FALSE",
                            "httpOnly": "TRUE",
                            "raw_line": "",
                        }
                    )
            continue

        m = re.match(r"^(session_id|session-id)\s*[:=\t]\s*(.+)$", l, re.I)
        if m:
            out.append(
                {
                    "domain": ".manus.im",
                    "name": "session_id",
                    "value": m.group(2).strip().strip('"'),
                    "path": "/",
                    "secure": "TRUE",
                    "hostOnly": "FALSE",
                    "httpOnly": "TRUE",
                    "raw_line": "",
                }
            )
            continue

        if "\t" not in l:
            # bare JWT that looks like session
            if l.count(".") == 2 and l.startswith("eyJ"):
                out.append(
                    {
                        "domain": ".manus.im",
                        "name": "session_id",
                        "value": l,
                        "path": "/",
                        "secure": "TRUE",
                        "hostOnly": "FALSE",
                        "httpOnly": "TRUE",
                        "raw_line": "",
                    }
                )
            continue

        parts = l.split("\t")
        if len(parts) < 7:
            if len(parts) == 2 and parts[0].strip().lower() in AUTH_NAMES:
                out.append(
                    {
                        "domain": ".manus.im",
                        "name": "session_id",
                        "value": parts[1].strip(),
                        "path": "/",
                        "secure": "TRUE",
                        "hostOnly": "FALSE",
                        "httpOnly": "TRUE",
                        "raw_line": "",
                    }
                )
            continue
        domain = parts[0].strip()
        if domain.startswith("#HttpOnly_"):
            httponly = True
            domain = domain[len("#HttpOnly_") :]
        name = parts[5].strip()
        value = parts[6].strip()
        if not name or not value:
            continue
        out.append(
            {
                "domain": domain,
                "name": name,
                "value": value,
                "path": (parts[2] or "/").strip() or "/",
                "secure": (parts[3] or "TRUE").strip().upper() or "TRUE",
                "expires": (parts[4] or "0").strip() or "0",
                "hostOnly": "TRUE"
                if (parts[1] or "").strip().upper() == "FALSE"
                else "FALSE",
                "httpOnly": "TRUE" if httponly else "FALSE",
                "raw_line": line.rstrip("\n"),
            }
        )
    return out


def _parse_json_cookies(text: str) -> List[Dict[str, str]]:
    try:
        data = json.loads(text)
    except Exception:
        return []
    rows: List[Dict[str, str]] = []
    items = data if isinstance(data, list) else []
    if isinstance(data, dict):
        if isinstance(data.get("cookies"), list):
            items = data["cookies"]
        else:
            for k, v in data.items():
                if str(k).lower() in AUTH_NAMES and v:
                    rows.append(
                        {
                            "domain": ".manus.im",
                            "name": "session_id",
                            "value": str(v),
                            "path": "/",
                            "secure": "TRUE",
                            "hostOnly": "FALSE",
                            "httpOnly": "TRUE",
                            "raw_line": "",
                        }
                    )
            return rows
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or it.get("Name") or "")
        value = str(it.get("value") or it.get("Value") or "")
        if not name or not value:
            continue
        rows.append(
            {
                "domain": str(it.get("domain") or it.get("Domain") or ".manus.im"),
                "name": name,
                "value": value,
                "path": str(it.get("path") or "/"),
                "secure": "TRUE",
                "hostOnly": "FALSE",
                "httpOnly": "FALSE",
                "raw_line": "",
            }
        )
    return rows


def parse_cookie_file(
    text: str, *, source: Optional[str] = None
) -> Dict[str, Any]:
    errors: List[str] = []
    rows = _parse_netscape_lines(text)
    if not rows:
        rows = _parse_json_cookies(text)
    if not rows:
        m = re.search(r"session_id\s*=\s*([^\s;]+)", text or "", re.I)
        if m:
            rows = [
                {
                    "domain": ".manus.im",
                    "name": "session_id",
                    "value": m.group(1).strip(),
                    "path": "/",
                    "secure": "TRUE",
                    "hostOnly": "FALSE",
                    "httpOnly": "TRUE",
                    "raw_line": "",
                }
            ]
    if not rows:
        return {
            "cookies": [],
            "parseErrors": [f"no manus session_id in {source or 'file'}"],
        }

    jar: Dict[str, str] = {}
    all_rows: List[Dict[str, str]] = []
    for r in rows:
        name = r.get("name") or ""
        val = r.get("value") or ""
        dom = r.get("domain") or ""
        if not name or not val:
            continue
        nl = name.lower().replace("-", "_")
        if not (
            _is_manus_domain(dom)
            or nl in ("session_id", "sessionid")
            or (val.count(".") == 2 and val.startswith("eyJ"))
        ):
            continue
        if nl in ("session_id", "sessionid", "session-id"):
            jar["session_id"] = val
            name = "session_id"
        else:
            jar[name] = val
        r = dict(r)
        r["name"] = name
        all_rows.append(r)

    sid = jar.get("session_id") or ""
    if not sid:
        return {
            "cookies": [],
            "parseErrors": [f"missing session_id in {source or 'file'}"],
        }

    netscape_lines: List[str] = []
    for r in all_rows:
        if r.get("raw_line"):
            netscape_lines.append(r["raw_line"])
        else:
            dom = r.get("domain") or ".manus.im"
            path = r.get("path") or "/"
            secure = r.get("secure") or "TRUE"
            exp = r.get("expires") or "0"
            sub = "FALSE" if r.get("hostOnly") == "TRUE" else "TRUE"
            netscape_lines.append(
                f"{dom}\t{sub}\t{path}\t{secure}\t{exp}\t{r['name']}\t{r['value']}"
            )

    header = "; ".join(f"{k}={v}" for k, v in jar.items() if v)
    cookie = ParsedManusCookie(
        session_token=sid,
        raw_cookie=header,
        email=_hint_email(text),
        source=source,
        session_id=sid,
        all_cookies=all_rows,
        netscape_lines=netscape_lines,
    )
    return {"cookies": [cookie], "parseErrors": errors}


def parse_multiple_files(files: List[Dict[str, Any]]) -> Dict[str, Any]:
    seen: Set[str] = set()
    cookies: List[ParsedManusCookie] = []
    errors: List[str] = []
    for f in files or []:
        name = f.get("name") or f.get("filename") or f.get("source") or "file"
        text = f.get("content") or f.get("text") or f.get("data") or ""
        if isinstance(text, bytes):
            text = text.decode("utf-8", "ignore")
        parsed = parse_cookie_file(str(text), source=str(name))
        errors.extend(parsed.get("parseErrors") or [])
        for c in parsed.get("cookies") or []:
            key = (c.session_id or c.session_token or "")[:80]
            if not key or key in seen:
                continue
            seen.add(key)
            cookies.append(c)
    return {"cookies": cookies, "parseErrors": errors}
