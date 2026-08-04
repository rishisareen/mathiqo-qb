# mathiqo-qb REST server.
#
# Built and run from the consumer app's compose stack
# (maplemath-adaptive-spike/deploy/do/docker-compose.yml), which publishes no
# host port for it — the server is reachable only through Caddy, on its own
# subdomain, over TLS. That matters: tools/rest_server.py's own README says
# transport hardening "is your deployment's job", and its bind default is
# loopback precisely because it expects something in front.
#
# ── two things to know before this goes live ────────────────────────────────
#
# 1. This runs tools/rest_server.py, which CANNOT SERVE IDENTITY. It builds
#    ServingFacade(banks_dir, logs_dir, content) with no `identity=` argument
#    and exposes no flag for one, so POST /students, /students/{id}/link and
#    /students/lookup all return 400. Worse and quieter: with no registry the
#    link check is skipped on every consumer-facing operation, so open_session
#    succeeds for a student that was never registered. A deployment that means
#    to enforce R17 needs that flag added upstream, or an entrypoint that wires
#    a registry in (see the consumer repo's scripts/mathiqo-qb-dev-server.py for
#    the shape — three lines).
#
# 2. uvicorn is imported by tools/rest_server.py but is NOT in requirements.txt.
#    It resolves today only because `mcp` happens to depend on it. That is a
#    transitive accident, not a declared dependency, so it is pinned explicitly
#    below rather than left to chance in a clean image.

FROM python:3.12-slim

# Fail fast and log straight through to the container runtime.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a content or code change does not re-resolve them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir "uvicorn>=0.30"

# The application. banks/, logs/ and credentials.json are deliberately NOT
# baked in — they are mounts. A bank is build output that changes on its own
# schedule, logs are state, and credentials are secret.
COPY orchestrator/ ./orchestrator/
COPY tools/ ./tools/
COPY schemas/ ./schemas/
COPY content/ ./content/

# Runs as nobody. The only paths it writes are the mounted logs directory and
# the identity log, both of which the compose file owns.
RUN useradd --system --create-home --uid 10001 qb \
 && mkdir -p /app/logs /app/banks \
 && chown -R qb:qb /app
USER qb

# 8000 on the compose network only. There is no `ports:` mapping for this
# service — `expose` documents the port for Caddy and nothing else.
EXPOSE 8000

# 0.0.0.0 is correct INSIDE a container with no published port: the only thing
# that can reach it is another service on the same compose network, i.e. Caddy.
# It is not the same as binding 0.0.0.0 on the host.
CMD ["python", "tools/rest_server.py", \
     "--banks-dir", "/app/banks", \
     "--logs-dir", "/app/logs", \
     "--credentials-file", "/run/secrets/qb-credentials.json", \
     "--content", "/app/content/fractions", \
     "--host", "0.0.0.0", \
     "--port", "8000"]
