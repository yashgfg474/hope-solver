"""Our own Turnstile solver API (Fahim-style, zero deps beyond stdlib).

Endpoints (Theyka-compatible + direct):
  POST /cloudflare  {"mode":"turnstile","domain":full_url,"siteKey":key}
      -> {"token":"..."}  (500 + message on failure)
      NOTE: domain must be a FULL URL (https://...) — bare hosts 400.
  GET /turnstile?url=<page>&sitekey=<key>[&action=][&cdata=]
      -> {"task_id": "<uuid>"}            (solving starts in background)
  GET /result?id=<task_id>
      -> {"status":"pending"} | {"status":"ready","value":"<token>","elapsed_time":s}
         | {"status":"failed","elapsed_time":s}
  GET /solve?url=<page>&sitekey=<key>      (Fahim-style direct)
      -> {"status":"success","token":"...","time_taken":s}

Solver chain per task: OUR widget-page solver → Fahim free API.
Run:  python -m sites.manus.solver_api [--host 127.0.0.1] [--port 8077]
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TASKS: dict = {}
TASKS_LOCK = threading.Lock()

# ── Fahim circuit breaker ──
# The free Fahim API sometimes dies slowly (45s+ per 500, retried twice =
# ~95s burned per request). After 2 consecutive failures, skip the Fahim
# race entirely for 10 minutes so the local widget solve answers alone.
_FAHIM_LOCK = threading.Lock()
_FAHIM_FAILS = 0
_FAHIM_DOWN_UNTIL = 0.0


def _fahim_available() -> bool:
    return time.time() >= _FAHIM_DOWN_UNTIL


def _fahim_report(success: bool) -> None:
    global _FAHIM_FAILS, _FAHIM_DOWN_UNTIL
    with _FAHIM_LOCK:
        if success:
            _FAHIM_FAILS = 0
            _FAHIM_DOWN_UNTIL = 0.0
        else:
            _FAHIM_FAILS += 1
            if _FAHIM_FAILS >= 2:
                _FAHIM_DOWN_UNTIL = time.time() + 600.0

# ── warm solver workers (2s-style speed: no launch cost per task) ──
# Each worker thread owns ONE persistent browser + ONE widget page.
# A task = re-route stub (new sitekey) + poll token. Fahim stays fallback.
import queue as _queue

WORK_Q: _queue.Queue = _queue.Queue()
N_WORKERS = int(os.environ.get("SOLVER_WORKERS", "2"))


def _worker_loop() -> None:
    """Owns a warm browser; solves tasks from WORK_Q until process ends.

    Speed design (6s/solve target):
    - Browser stays warm (no launch cost per task).
    - Widget page is PRE-RENDERED between tasks, so a task skips
      goto/render and goes straight to poll+click.
    - No dead wait: token polled every 0.2s from t=0; the checkbox is
      clicked as soon as it is clickable (~1.5s in).
    - Fahim races in the background as fallback (unchanged).
    """
    browser = None
    page = None
    pw = None
    # Pre-rendered widget pool: (sitekey, action, cdata) -> page is hot.
    pool_key: tuple = ("", "", "")
    pool_fresh = False

    def ensure_browser():
        nonlocal browser, page, pw
        if browser is not None:
            try:
                if browser.is_connected():
                    return True
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
            browser, page = None, None
        # Never leak playwright instances across reconnects.
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
            pw = None
        try:
            from playwright.sync_api import sync_playwright
            from sites.manus import turnstile as ts_mod

            pw = sync_playwright().start()
            exe = ts_mod._chromium_path()
            args = [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-quic",
                "--enable-unsafe-swiftshader",
                "--headless=new",
                "--host-resolver-rules=MAP brunhild.challenges.cloudflare.com 104.18.95.41",
                f"--user-agent={ts_mod.UA}",
            ]
            kw: dict = {"headless": False, "args": args}
            if exe:
                kw["executable_path"] = exe
            browser = pw.chromium.launch(**kw)
            page = browser.new_page(viewport={"width": 600, "height": 250},
                                    locale="en-US", timezone_id="Asia/Kolkata",
                                    extra_http_headers=ts_mod.SEC_CH_UA_HEADERS)
            ts_mod._apply_stealth(page)
            return True
        except Exception:
            browser, page = None, None
            return False

    def _render_widget(skey: str, act: str, cd: str, purl: str) -> bool:
        """Serve the widget stub page and navigate to it. True on success."""
        try:
            from sites.manus import turnstile as ts_mod

            assert page is not None
            html = ts_mod.WIDGET_HTML.format(
                sitekey=skey,
                action=f' data-action="{act}"' if act else "",
                cdata=f' data-cdata="{cd}"' if cd else "")
            try:
                page.unroute_all(handling="default")
            except Exception:
                pass
            page.route(purl, lambda route, request: route.fulfill(
                body=html, status=200, content_type="text/html"))
            page.goto(purl, wait_until="domcontentloaded", timeout=25000)
            return True
        except Exception:
            return False

    while True:
        try:
            task = WORK_Q.get()
        except Exception:
            continue
        if task is None:
            return
        task = task if len(task) == 7 else (*task, "")
        task_id, url, sitekey, action, cdata, timeout, only = task
        t0 = time.time()
        token = ""
        winner = ""
        fahim_box: dict = {}

        def _fahim_race():
            if not _fahim_available():
                return  # circuit breaker: Fahim known-down, don't burn time
            try:
                from sites.manus import auth as auth_mod

                tok = auth_mod.solve_via_fahim(url=url, sitekey=sitekey,
                                               timeout=30.0)
                _fahim_report(bool(tok))
                if tok:
                    fahim_box["token"] = tok
            except Exception:
                _fahim_report(False)

        if only != "widget" and _fahim_available():
            threading.Thread(target=_fahim_race, daemon=True).start()
        try:
            if ensure_browser():
                from sites.manus import turnstile as ts_mod

                assert page is not None
                page_url = (url or "").strip() or ts_mod.LOGIN_URL
                if not page_url.endswith("/"):
                    page_url += "/"
                login_url = ts_mod.LOGIN_URL.rstrip("/") + "/"
                # First run: pre-render the Manus widget so task 1 is fast too.
                if not pool_fresh and not pool_key[0]:
                    if _render_widget(ts_mod.SITEKEY, "", "", login_url):
                        pool_key = (ts_mod.SITEKEY, "", "")
                        pool_fresh = True
                # Fast path: pooled page already shows this widget —
                # skip goto/render entirely.
                if not (pool_fresh and pool_key == (sitekey, action, cdata)
                        and page_url == login_url):
                    _render_widget(sitekey, action, cdata, page_url)
                pool_fresh = False  # pooled page consumed by this task
                ts_mod._mouse_wiggle(page, 1)
                deadline = time.time() + timeout
                last_click = 0.0
                while time.time() < deadline:
                    if fahim_box.get("token"):
                        token = fahim_box["token"]
                        winner = "fahim"
                        break
                    token = ts_mod._read_token(page)
                    if token:
                        winner = "widget"
                        break
                    # No dead wait: give managed auto-pass ~0.8s, then
                    # click the checkbox as soon as it is clickable,
                    # re-clicking every 1s until the token lands.
                    now = time.time()
                    if now - t0 > 0.8 and now - last_click > 1.0:
                        if ts_mod._human_click_iframe(page):
                            last_click = now
                    time.sleep(0.2)
        except Exception:
            token = ""
        if not token and fahim_box.get("token"):
            token = fahim_box["token"]
            winner = winner or "fahim"
        with TASKS_LOCK:
            TASKS[task_id] = {
                "status": "ready" if token else "failed",
                "value": token,
                "solver": winner,
                "elapsed_time": round(time.time() - t0, 3),
            }
        # Re-render between tasks so the next solve starts on a hot widget
        # (cost is off the critical path).
        try:
            if browser is not None and page is not None:
                from sites.manus import turnstile as _ts

                _login = _ts.LOGIN_URL.rstrip("/") + "/"
                if _render_widget(sitekey, action, cdata, _login):
                    pool_key = (sitekey, action, cdata)
                    pool_fresh = True
        except Exception:
            pass
        try:
            WORK_Q.task_done()
        except Exception:
            pass


_WORKERS_STARTED = False


def _ensure_workers(n: int = N_WORKERS) -> None:
    global _WORKERS_STARTED
    if _WORKERS_STARTED:
        return
    _WORKERS_STARTED = True
    for _ in range(max(1, n)):
        threading.Thread(target=_worker_loop, daemon=True).start()


def _solve_task(task_id: str, url: str, sitekey: str, action: str, cdata: str) -> None:
    t0 = time.time()
    token = ""
    try:
        from sites.manus import turnstile as ts_mod

        token = ts_mod.solve_via_widget_page(
            url=url, sitekey=sitekey, action=action or "", cdata=cdata or "",
            timeout=110.0)
    except Exception:
        token = ""
    if not token:
        try:
            from sites.manus import auth as auth_mod

            token = auth_mod.solve_via_fahim(url=url, sitekey=sitekey, timeout=90.0)
        except Exception:
            token = ""
    with TASKS_LOCK:
        TASKS[task_id] = {
            "status": "ready" if token else "failed",
            "value": token,
            "elapsed_time": round(time.time() - t0, 3),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "ManusSolver/1.0"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, obj, code: int = 200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            parts = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parts.query)
            arg = lambda k, d="": (qs.get(k) or [d])[0]
            if parts.path == "/turnstile":
                url, sitekey = arg("url"), arg("sitekey")
                if not url or not sitekey:
                    return self._send({"status": "error",
                                       "message": "need ?url= &sitekey="}, 400)
                tid = str(uuid4())
                with TASKS_LOCK:
                    TASKS[tid] = {"status": "pending"}
                _ensure_workers()
                WORK_Q.put((tid, url, sitekey, arg("action"), arg("cdata"), 110.0))
                return self._send({"task_id": tid})
            if parts.path == "/result":
                with TASKS_LOCK:
                    t = TASKS.get(arg("id"), {})
                if not t:
                    return self._send({"status": "error", "message": "unknown id"}, 404)
                return self._send(t)
            if parts.path == "/solve":
                url, sitekey = arg("url"), arg("sitekey")
                if not url or not sitekey:
                    return self._send({"status": "error",
                                       "message": "need ?url= &sitekey="}, 400)
                t0 = time.time()
                tid = f"sync-{uuid4()}"
                with TASKS_LOCK:
                    TASKS[tid] = {"status": "pending"}
                _ensure_workers()
                WORK_Q.put((tid, url, sitekey, arg("action"), arg("cdata"), 110.0))
                deadline = time.time() + 150.0
                token = ""
                while time.time() < deadline:
                    time.sleep(1.0)
                    with TASKS_LOCK:
                        t = TASKS.get(tid, {})
                    if t.get("status") in ("ready", "failed"):
                        token = t.get("value") or ""
                        break
                if not token:
                    try:
                        from sites.manus import auth as auth_mod

                        token = auth_mod.solve_via_fahim(url=url, sitekey=sitekey,
                                                         timeout=60.0)
                    except Exception:
                        token = ""
                if token:
                    return self._send({"status": "success", "token": token,
                                       "time_taken": round(time.time() - t0, 3)})
                return self._send({"status": "error",
                                   "message": "unsolved"}, 500)
            if parts.path in ("/", "/health"):
                return self._send({"ok": True, "service": "manus-turnstile-solver",
                                   "fahim": ("down" if not _fahim_available()
                                             else "up")})
            return self._send({"status": "error", "message": "unknown path"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._send({"status": "error", "message": str(e)[:120]}, 500)
            except Exception:
                pass

    def do_POST(self):
        """POST /cloudflare  {"mode":"turnstile","domain":full_url,"siteKey":key}
        -> {"token":"..."} (200) | {"status":"error","message":...} (4xx/5xx).

        This is the endpoint bot clients call through the Cloudflare Tunnel.
        Solved via the warm worker pool (same chain as GET /solve).
        """
        try:
            parts = urllib.parse.urlparse(self.path)
            if parts.path != "/cloudflare":
                return self._send({"status": "error", "message": "unknown path"}, 404)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except Exception:
                length = 0
            raw = self.rfile.read(max(0, length)) if length > 0 else b""
            try:
                body = json.loads((raw or b"").decode("utf-8") or "{}")
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            if str(body.get("mode") or "").lower() != "turnstile":
                return self._send(
                    {"status": "error", "message": "mode must be 'turnstile'"}, 400)
            url = str(body.get("domain") or "").strip()
            sitekey = str(
                body.get("siteKey") or body.get("sitekey") or "").strip()
            if not url or not sitekey:
                return self._send(
                    {"status": "error", "message": "need domain and siteKey"}, 400)
            # NOTE: domain must be a FULL URL (https://...) — bare hosts fail.
            if not url.lower().startswith("http"):
                return self._send(
                    {"status": "error",
                     "message": "domain must be a full URL (https://...)"}, 400)
            # Optional: {"only": "widget"} skips the Fahim race and times the
            # local widget solve alone. {"only": "fahim"} skips the widget.
            only = str(body.get("only") or "").lower().strip()
            t0 = time.time()
            tid = f"cf-{uuid4()}"
            with TASKS_LOCK:
                TASKS[tid] = {"status": "pending"}
            _ensure_workers()
            WORK_Q.put((tid, url, sitekey,
                        str(body.get("action") or ""),
                        str(body.get("cdata") or ""), 110.0, only))
            deadline = time.time() + 150.0
            token = ""
            while time.time() < deadline:
                time.sleep(1.0)
                with TASKS_LOCK:
                    t = TASKS.get(tid, {})
                if t.get("status") in ("ready", "failed"):
                    token = t.get("value") or ""
                    break
            if token:
                return self._send({"token": token,
                                   "solver": t.get("solver") or "",
                                   "elapsed": t.get("elapsed_time") or
                                   round(time.time() - t0, 1)})
            return self._send({"status": "error", "message": "unsolved",
                               "elapsed": round(time.time() - t0, 1)}, 500)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._send({"status": "error", "message": str(e)[:120]}, 500)
            except Exception:
                pass


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8077)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"solver API on http://{args.host}:{args.port} "
          f"(/turnstile?url=&sitekey= → /result?id= | /solve direct)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
