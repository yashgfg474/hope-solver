# Deploy Hope solver on Render (free) — 5 minutes

You need: a GitHub account, a Render account (free, no card).

## 1. Put the code on GitHub
1. Create a new repo on github.com (any name, e.g. `hope-solver`). Private is fine.
2. Upload everything from this zip into the repo root — keep the layout exactly:
   `Dockerfile`, `render.yaml`, `requirements.txt`, and the `our_solver/` folder.
   (Easiest: GitHub web → Add file → Upload files, drag all of them in.)

## 2. Deploy on Render
1. Go to dashboard.render.com → **New +** → **Web Service**.
2. Connect your GitHub account when asked, pick the `hope-solver` repo.
3. Render auto-detects `render.yaml` — name `hope-solver`, plan **Free**, Docker runtime.
4. Hit **Create Web Service**. First build takes ~5-8 min (installs Chromium).

## 3. Verify
Open `https://hope-solver.onrender.com/health` — you should see
`{"ok": true, "service": "manus-turnstile-solver"}`.

## 4. Wire it into the bot (tunnel mode)
The bot talks to the solver at the tunnel URL. Once your Render URL is live,
tell your assistant the URL — he'll point the bot's tunnel solver at it and
set up a keep-alive ping so Render never sleeps on you.

## Notes
- Free Render sleeps after 15 min idle; a small keep-alive ping every 10 min
  keeps the warm browser workers hot, so solves stay fast.
- If the service ever feels memory-tight, lower `SOLVER_WORKERS` to 1 in the
  Render dashboard (Environment tab) and redeploy.
- API: `POST /cloudflare` with `{"mode":"turnstile","domain":url,"siteKey":key}`
  → `{"token":"..."}`.
