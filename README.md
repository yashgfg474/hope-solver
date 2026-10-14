# Hope Solver

A self-hosted **Cloudflare Turnstile** solving microservice. Give it any sitekey +
page URL, get back a valid Turnstile token. Built for speed: warm cloaked-Chromium
workers, pre-rendered widget pool, aggressive human-like clicking.

## Quick start (Windows)

1. Install Python 3.13+ from python.org (tick **"Add python.exe to PATH"**).
2. Double-click **run.bat** — first run installs everything (takes a few minutes).
   Leave the black window open while the solver is needed.
3. Check http://127.0.0.1:8077/health → `{"ok": true, ...}`.

To expose it over the internet (e.g. for a bot elsewhere), run
`cloudflared tunnel --url http://127.0.0.1:8077` and use the printed
`https://....trycloudflare.com` URL.

## Quick start (Linux / Termux)

```bash
pip install -r requirements.txt
python -m playwright install chromium   # skip on Termux, use system chromium
cd our_solver
python -m sites.manus.solver_api --host 127.0.0.1 --port 8077
```

On Termux, install the system Chromium and point the solver at it:

```bash
pkg install -y python chromium
termux-wake-lock   # keep Android from killing it; also disable battery optimization
export CHROMIUM_PATH=$PREFIX/bin/chromium
```

## API

### `POST /cloudflare` — solve a Turnstile challenge

```json
{
  "mode": "turnstile",
  "domain": "https://example.com/login",
  "siteKey": "0x4AAAAAAA-xxxx",
  "action": "",
  "cdata": "",
  "only": ""
}
```

- `domain` must be a full URL (`https://...`).
- `only: "widget"` skips the Fahim fallback race and times the local cloaked-browser
  solve alone (useful for benchmarking).

Response (200):

```json
{ "token": "<cf-turnstile-response>", "solver": "widget", "elapsed": 4.2 }
```

`solver` is `widget` (local cloaked Chromium) or `fahim` (fallback API race winner).
Errors: `{"status": "error", "message": "..."}` with 4xx/5xx.

### `GET /health`

Returns `{"ok": true, "service": "manus-turnstile-solver"}`.

## How it works

- Warm persistent Chromium workers (no launch cost per task).
- Widget page is served locally via route interception and pre-rendered between
  tasks, so the next solve starts on a hot page.
- Cloaked browser: automation flags hidden, real Windows-Chrome fingerprint,
  real mouse clicks (no script-dispatched events). The off-the-shelf stealth
  plugin is deliberately avoided — Cloudflare fingerprints it.
- Every task races the local widget solve against the Fahim API; fastest wins.

## Configuration (env vars)

| Var              | Default | Purpose                                     |
|------------------|---------|---------------------------------------------|
| `PORT`           | 8077    | Listen port                                 |
| `SOLVER_WORKERS` | 2       | Warm browser workers (1 on tiny containers) |
| `CHROMIUM_PATH`  | —       | Custom Chromium binary (e.g. Termux's)      |

## Deploy on Render (free)

`Dockerfile` + `render.yaml` are included. Connect the repo in the Render
dashboard → New Web Service → the free plan picks up `render.yaml` automatically.
`/health` is the health check. Free tier sleeps after 15 min idle — ping
`/health` every ~10 min to keep it warm.

## Layout

```
our_solver/
  sites/manus/
    solver_api.py   # HTTP service: POST /cloudflare, GET /health
    turnstile.py    # cloaked-Chromium widget solver
    auth.py         # Manus API client + Fahim fallback
    checker.py      # login checker
    cookie_parser.py
  hotmail/          # mail-mode helpers
  scripts/
run.bat             # Windows one-click starter
Dockerfile          # Render/Docker deploy
render.yaml         # Render service definition
```
