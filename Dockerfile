# Hope solver — Render Dockerfile
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    SOLVER_WORKERS=2

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install --with-deps chromium

COPY our_solver ./our_solver

WORKDIR /app/our_solver
EXPOSE 10000

# Render injects $PORT; the API serves POST /cloudflare and GET /health
CMD ["sh", "-c", "python -m sites.manus.solver_api --host 0.0.0.0 --port ${PORT:-10000}"]
