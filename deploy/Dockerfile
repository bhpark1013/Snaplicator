# Build from the repository root:
#   docker build -f deploy/Dockerfile --target manager -t snaplicator-manager .
#   docker build -f deploy/Dockerfile --target web     -t snaplicator-web .
# (docker compose does this for you — see deploy/docker-compose.yml)

# ── frontend build ────────────────────────────────────────────────────
FROM node:20-alpine AS frontend-build
WORKDIR /fe
COPY frontend/package.json frontend/pnpm-lock.yaml ./
# npm-installed pnpm instead of corepack: corepack's signature check on its
# pnpm download is broken on current node images (Cannot find matching keyid)
RUN npm install -g pnpm@9 && (pnpm install --frozen-lockfile || pnpm install)
COPY frontend/ ./
RUN pnpm build

# ── web: nginx serving the SPA, proxying /api → manager ──────────────
FROM nginx:1.27-alpine AS web
COPY deploy/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=frontend-build /fe/dist /usr/share/nginx/html
# WEB_PORT / BACKEND_PORT are substituted into the template at start-up
ENV WEB_PORT=8080 BACKEND_PORT=8888

# ── manager: the FastAPI backend + every host tool it shells out to ──
FROM python:3.12-slim AS manager

# btrfs-progs   snapshot/subvolume operations on the bind-mounted pool
# postgresql-client  direct psql to the publisher
# iproute2      `ss -ltn` free-port scanning (host netns via network_mode: host)
# attr          setfattr for snapshot metadata
# sudo          the codebase invokes `sudo -n ...`; as root it's a no-op wrapper
RUN apt-get update && apt-get install -y --no-install-recommends \
        btrfs-progs postgresql-client iproute2 attr sudo procps curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# docker CLI only (static binary) — the daemon is the host's, via the socket
ARG DOCKER_VERSION=27.1.1
RUN curl -fsSL "https://download.docker.com/linux/static/stable/$(uname -m)/docker-${DOCKER_VERSION}.tgz" \
    | tar xz --strip-components=1 -C /usr/local/bin docker/docker

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY configs/ configs/
COPY scripts/ scripts/
COPY replication/ replication/
COPY Makefile Makefile

# no --reload: this is the packaged runtime, not the dev loop
CMD ["sh", "-c", "cd backend && exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${BACKEND_PORT:-8888}"]
