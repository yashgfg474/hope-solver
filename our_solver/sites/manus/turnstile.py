"""In-house Cloudflare Turnstile solver for manus.im (no paid service needed).

Strategy (mirrors working community solvers):
  1) Real Chromium + stealth evasions (playwright-stealth), persistent
     profile dir so Cloudflare `cf_clearance` cookies survive across runs.
  2) Human-like mouse movement + realistic timing on the login page.
  3) Use the page's OWN widget when it renders (click checkbox), else
     explicit-render our own widget with the known sitekey and harvest
     `cf-turnstile-response` (with error capture).

Returns a token string or '' — never raises.
"""
from __future__ import annotations

import os
import random
import shutil
import tempfile
import time
from typing import Optional

LOGIN_URL = "https://manus.im/login"
SITEKEY = "0x4AAAAAAA_sd0eRNCinWBgU"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
# Client-hints matching the UA above (D3-vin style — a UA/hints mismatch
# is itself a bot signal).
SEC_CH_UA = '"Chromium";v="152", "Not(A:Brand";v="24", "Google Chrome";v="152"'
SEC_CH_UA_HEADERS = {
    "sec-ch-ua": SEC_CH_UA,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
PROFILE_DIR = "/tmp/opencode/manus_profile"

# Minimal widget page (Theyka-style): route interception serves ONLY the
# Turnstile widget — no site JS, no site CSP, instant render.
WIDGET_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ts</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
</head><body>
<div class="cf-turnstile" data-sitekey="{sitekey}"{action}{cdata}></div>
</body></html>"""


def solve_via_widget_page(*, url: str = "", sitekey: str = "",
                          action: str = "", cdata: str = "",
                          timeout: float = 75.0, proxy: str = "",
                          headless: bool = True) -> str:
    """Our own solver: custom widget page + click loop. Returns token or ''."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return ""
    exe = _chromium_path()
    if not exe:
        return ""
    key = (sitekey or "").strip() or SITEKEY
    page_url = (url or "").strip() or LOGIN_URL
    if not page_url.endswith("/"):
        page_url += "/"
    html = WIDGET_HTML.format(
        sitekey=key,
        action=f' data-action="{action}"' if action else "",
        cdata=f' data-cdata="{cdata}"' if cdata else "",
    )
    token = ""
    try:
        with sync_playwright() as p:
            # Theyka/D3-vin style: FULL browser in new-headless mode with
            # matching UA + sec-ch-ua. Falls back to plain headless.
            base_args = [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-quic",
                "--enable-unsafe-swiftshader",
                "--host-resolver-rules=MAP brunhild.challenges.cloudflare.com 104.18.95.41",
                f"--user-agent={UA}",
            ]
            launch_kw: dict = {"args": base_args}
            if exe:
                launch_kw["executable_path"] = exe
            if proxy:
                launch_kw["proxy"] = {"server": proxy}
            try:
                browser = p.chromium.launch(
                    headless=False,
                    args=base_args + ["--headless=new"], **{
                        k: v for k, v in launch_kw.items() if k != "args"})
            except Exception:
                browser = p.chromium.launch(headless=True, **launch_kw)
            try:
                page = browser.new_page(viewport={"width": 600, "height": 250},
                                        locale="en-US", timezone_id="Asia/Kolkata",
                                        extra_http_headers=SEC_CH_UA_HEADERS)
                _apply_stealth(page)
                # Path 1: route-intercept widget page (instant render).
                page.route(page_url, lambda route, request: route.fulfill(
                    body=html, status=200, content_type="text/html"))
                try:
                    page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    return ""
                try:
                    page.wait_for_selector(".cf-turnstile", timeout=20000)
                except Exception:
                    pass
                _mouse_wiggle(page, 2)
                deadline = time.time() + timeout
                while time.time() < deadline:
                    token = _read_token(page)
                    if token:
                        break
                    _human_click_iframe(page)
                    time.sleep(1.0)
                if token:
                    return token
                # Path 2 (D3-vin solve_realpage): real page + inject widget.
                try:
                    await_ok = False
                    page.unroute(page_url)
                except Exception:
                    pass
                try:
                    page.goto((url or "").strip() or LOGIN_URL,
                              wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(2000)
                    page.evaluate(
                        "(k) => { const d = document.createElement('div');"
                        " d.className = 'cf-turnstile';"
                        " d.setAttribute('data-sitekey', k);"
                        " document.body.prepend(d); }", key)
                    page.wait_for_timeout(3000)
                    _human_click_iframe(page)
                    deadline2 = time.time() + 30.0
                    while time.time() < deadline2:
                        token = _read_token(page)
                        if token:
                            break
                        _human_click_iframe(page)
                        time.sleep(1.0)
                except Exception:
                    pass
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception:
        return ""
    return token


def _chromium_path() -> Optional[str]:
    env = (os.environ.get("CHROMIUM_PATH") or "").strip()
    if env and os.path.isfile(env):
        return env
    for cand in ("/repl/tools/bin/chromium", "/usr/bin/chromium",
                 "/usr/bin/chromium-browser", "/usr/bin/google-chrome"):
        if os.path.isfile(cand):
            return cand
    return (shutil.which("chromium") or shutil.which("chromium-browser")
            or shutil.which("google-chrome"))


def _apply_stealth(page) -> None:
    # Hand-rolled minimal patches only. The full playwright-stealth lib is
    # deliberately NOT applied — its injections are themselves fingerprinted
    # by Cloudflare's challenge telemetry.
    try:
        page.add_init_script(
            "() => {"
            " try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch(e){}"
            # NOTE: plugins intentionally NOT spoofed — this build ships real
            # plugin objects; faking them with plain values scores worse.
            " try { Object.defineProperty(navigator, 'platform', {get: () => 'Win32'}); } catch(e){}"
            " try { Object.defineProperty(navigator, 'deviceMemory', {get: () => 8}); } catch(e){}"
            " try { Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']}); } catch(e){}"
            " try {"
            "  const brands=[{brand:'Chromium',version:'152'},{brand:'Not(A:Brand',version:'24'},{brand:'Google Chrome',version:'152'}];"
            "  const uaData={brands:brands,mobile:false,platform:'Windows',"
            "   getHighEntropyValues: async () => ({brands:brands,mobile:false,platform:'Windows',"
            "    platformVersion:'15.0.0',architecture:'x86',bitness:'64',model:'',"
            "    fullVersionList:brands,uaFullVersion:'152.0.0.0'})};"
            "  Object.defineProperty(navigator, 'userAgentData', {get: () => uaData});"
            " } catch(e){}"
            " try { window.chrome = window.chrome || {runtime:{},loadTimes:function(){},csi:function(){}}; } catch(e){}"
            "}"
        )
    except Exception:
        pass


def _human_click_iframe(page) -> bool:
    """D3-vin style: click the challenge checkbox via its frame bounding box
    (real mouse, offset inside the box) — falls back to frame selectors."""
    try:
        for fr in page.frames:
            if "challenges.cloudflare.com" not in (fr.url or ""):
                continue
            try:
                el = fr.frame_element()
                box = el.bounding_box() if el else None
                if box and box.get("width", 0) >= 20:
                    page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                    return True
            except Exception:
                pass
            for sel in ("input[type=checkbox]", "label", "body"):
                try:
                    fr.click(sel, timeout=800)
                    return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def _read_token(page) -> str:
    try:
        val = page.evaluate(
            "() => { const el = document.querySelector("
            "'input[name=\"cf-turnstile-response\"]');"
            " return el ? (el.value || '') : ''; }")
        if val and len(val) > 20:
            return str(val)
    except Exception:
        pass
    return ""


def _mouse_wiggle(page, n: int = 6) -> None:
    try:
        for _ in range(n):
            page.mouse.move(random.randint(100, 1200), random.randint(100, 800), steps=random.randint(4, 12))
            time.sleep(random.uniform(0.15, 0.5))
    except Exception:
        pass


def _wait_js(page, expr: str, timeout: float = 20.0) -> bool:
    """Poll a JS expression (manual loop — wait_for_function(string) trips
    the page CSP which bans unsafe-eval)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.evaluate(f"()=> !!({expr})"):
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def harvest_token(*, email: str = "", timeout: float = 120.0,
                  headless: bool = True, proxy: str = "") -> str:
    """Return a fresh Turnstile token for manus.im/login, or ''."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return ""
    exe = _chromium_path()
    if not exe:
        return ""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    token = ""
    try:
        with sync_playwright() as p:
            # D3-vin style: FULL browser in new-headless mode (NOT the old
            # headless shell) + matching sec-ch-ua + small viewport.
            args = [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-quic",
                "--enable-unsafe-swiftshader",
                "--headless=new",
                "--host-resolver-rules=MAP brunhild.challenges.cloudflare.com 104.18.95.41",
                f"--user-agent={UA}",
            ]
            launch_kw: dict = {"headless": False, "args": args}
            if exe:
                launch_kw["executable_path"] = exe
            if proxy:
                launch_kw["proxy"] = {"server": proxy}
            browser = p.chromium.launch(**launch_kw)
            try:
                page = browser.new_page(viewport={"width": 600, "height": 250},
                                        locale="en-US", timezone_id="Asia/Kolkata",
                                        extra_http_headers=SEC_CH_UA_HEADERS)
                _apply_stealth(page)
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(random.randint(4000, 7000))
                _mouse_wiggle(page)
                if email:
                    try:
                        box = page.locator('input[type="email"]').first
                        if box.count():
                            box.click(timeout=6000)
                            page.wait_for_timeout(400)
                            box.type(email, delay=random.randint(40, 110))
                            page.wait_for_timeout(1200)
                    except Exception:
                        pass
                _mouse_wiggle(page, 3)
                # Path A: the page's OWN widget — wait for it, scroll into
                # view, click its checkbox like a user. (The ci-fallback that
                # issues the token only fires after a real checkbox gesture.)
                clicked = False
                try:
                    page.wait_for_selector(
                        ".cf-turnstile",
                        timeout=45000)
                    try:
                        page.locator(".cf-turnstile").first.scroll_into_view_if_needed(
                            timeout=10000)
                    except Exception:
                        pass
                    page.wait_for_timeout(1500)
                    _mouse_wiggle(page, 2)
                    try:
                        frame = page.frame_locator(
                            "iframe[src*='challenges.cloudflare.com']").first
                        frame.locator("body").click(timeout=10000)
                        clicked = True
                    except Exception:
                        try:
                            page.locator(
                                "iframe[src*='challenges.cloudflare.com']"
                            ).first.click(timeout=8000, force=True)
                            clicked = True
                        except Exception:
                            pass
                except Exception:
                    pass
                deadline = time.time() + timeout
                if clicked:
                    # ci-fallback needs a few seconds after the click.
                    while time.time() < deadline:
                        try:
                            val = page.evaluate(
                                "() => ((document.querySelector('input[name=\"cf-turnstile-response\"]')||{}).value||'')")
                            if val and len(val) > 20:
                                token = str(val)
                                break
                        except Exception:
                            pass
                        time.sleep(2.0)
                if token:
                    return token
                # Path B: explicit-render our own widget, capture errors.
                try:
                    page.evaluate(
                        "() => { if (!document.getElementById('tsbox')) {"
                        " const d=document.createElement('div'); d.id='tsbox';"
                        " d.style.cssText='position:fixed;bottom:0;right:0;z-index:99999';"
                        " document.body.appendChild(d); }"
                        " if (typeof turnstile==='undefined') {"
                        " const s=document.createElement('script');"
                        " s.src='https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';"
                        " document.head.appendChild(s); } }")
                    _wait_js(page, "typeof turnstile !== 'undefined'", 20.0)
                    err = page.evaluate(
                        "() => { try { window._tstok=''; window._tserr='';"
                        " turnstile.render('#tsbox',{sitekey:'" + SITEKEY + "',"
                        " callback:(t)=>{window._tstok=t;},"
                        " 'error-callback':(e)=>{window._tserr=String(e);}});"
                        " return 'rendered'; } catch(e){ return 'ERR:'+e; } }")
                    _ = err
                except Exception:
                    pass
                deadline = time.time() + timeout
                while time.time() < deadline:
                    try:
                        val = page.evaluate(
                            "() => (window._tstok||'') || "
                            "((document.querySelector('input[name=\"cf-turnstile-response\"]')||{}).value||'')")
                        if val and len(val) > 20:
                            token = str(val)
                            break
                    except Exception:
                        pass
                    time.sleep(2.0)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception:
        return ""
    return token
