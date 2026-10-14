"""
Parse Hotmail / Outlook / Live combo lines and proxy lines.

Combos:
  email:pass
  email|pass
  email;pass
  email pass

Proxies:
  host:port
  host:port:user:pass
  user:pass@host:port
  http://host:port
  socks5://user:pass@host:port
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlparse

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$"
)


@dataclass
class HotmailCombo:
    email: str
    password: str
    raw: str = ""
    line_no: int = 0


@dataclass
class ProxyItem:
    """Normalized proxy for httpx."""
    url: str  # e.g. http://user:pass@host:port or socks5://...
    raw: str = ""
    line_no: int = 0


def _clean(s: str) -> str:
    s = (s or "").strip().lstrip("\ufeff")
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    return s.strip()


def parse_combo_line(line: str, line_no: int = 0) -> Optional[HotmailCombo]:
    line = _clean(line)
    if not line or line.startswith("#") or line.startswith("//"):
        return None

    email = password = ""

    # email:pass | email;pass | email|pass  (password may contain : after first split)
    for sep in (":", ";", "|"):
        if sep in line:
            left, right = line.split(sep, 1)
            left, right = left.strip(), right.strip()
            if EMAIL_RE.match(left) and right:
                email, password = left, right
                break

    if not email:
        # email pass (first token email, rest password)
        parts = line.split(None, 1)
        if len(parts) == 2 and EMAIL_RE.match(parts[0]) and parts[1].strip():
            email, password = parts[0], parts[1].strip()

    if not email or not password:
        return None
    if not EMAIL_RE.match(email):
        return None
    return HotmailCombo(
        email=email.lower(),
        password=password,
        raw=line,
        line_no=line_no,
    )


def parse_hotmail_lines(text: str) -> Tuple[List[HotmailCombo], List[str]]:
    combos: List[HotmailCombo] = []
    errors: List[str] = []
    seen = set()
    for i, line in enumerate((text or "").splitlines(), 1):
        c = parse_combo_line(line, i)
        if not c:
            s = (line or "").strip()
            if s and not s.startswith("#"):
                errors.append(f"L{i}: not email:pass")
            continue
        key = f"{c.email}:{c.password}"
        if key in seen:
            continue
        seen.add(key)
        combos.append(c)
    return combos, errors


def _proxy_from_parts(
    host: str,
    port: str,
    user: str = "",
    password: str = "",
    scheme: str = "http",
) -> Optional[str]:
    host = (host or "").strip().strip("[]")
    port = (port or "").strip()
    if not host or not port.isdigit():
        return None
    p = int(port)
    if not (1 <= p <= 65535):
        return None
    scheme = (scheme or "http").lower()
    if scheme not in ("http", "https", "socks5", "socks4", "socks5h"):
        scheme = "http"
    auth = ""
    if user:
        # raw embed — httpx accepts user:pass@host
        from urllib.parse import quote

        u = quote(user, safe="")
        pw = quote(password or "", safe="")
        auth = f"{u}:{pw}@"
    return f"{scheme}://{auth}{host}:{p}"


def parse_proxy_line(line: str, line_no: int = 0) -> Optional[ProxyItem]:
    line = _clean(line)
    if not line or line.startswith("#") or line.startswith("//"):
        return None

    # Full URL
    if "://" in line:
        try:
            u = urlparse(line)
            if not u.hostname or not u.port:
                return None
            # unknown scheme (socks://, ftp:// …) must be rejected — building
            # http://socks://… produced a garbage "valid" proxy before
            if u.scheme.lower() not in ("http", "https", "socks5", "socks4", "socks5h"):
                return None
            return ProxyItem(url=line, raw=line, line_no=line_no)
        except Exception:
            return None

    # user:pass@host:port
    if "@" in line:
        cred, hostpart = line.rsplit("@", 1)
        if ":" in cred and ":" in hostpart:
            user, password = cred.split(":", 1)
            host, port = hostpart.rsplit(":", 1)
            url = _proxy_from_parts(host, port, user, password)
            if url:
                return ProxyItem(url=url, raw=line, line_no=line_no)

    parts = line.split(":")
    # host:port
    if len(parts) == 2:
        url = _proxy_from_parts(parts[0], parts[1])
        if url:
            return ProxyItem(url=url, raw=line, line_no=line_no)
    # host:port:user:pass
    if len(parts) >= 4:
        host, port = parts[0], parts[1]
        user = parts[2]
        password = ":".join(parts[3:])
        url = _proxy_from_parts(host, port, user, password)
        if url:
            return ProxyItem(url=url, raw=line, line_no=line_no)
    # host:port:user:pass with scheme prefix socks5 host port ...
    return None


def parse_proxy_lines(text: str) -> Tuple[List[ProxyItem], List[str]]:
    out: List[ProxyItem] = []
    errs: List[str] = []
    seen = set()
    for i, line in enumerate((text or "").splitlines(), 1):
        p = parse_proxy_line(line, i)
        if not p:
            s = (line or "").strip()
            if s and not s.startswith("#"):
                errs.append(f"L{i}: bad proxy")
            continue
        if p.url in seen:
            continue
        seen.add(p.url)
        out.append(p)
    return out, errs
