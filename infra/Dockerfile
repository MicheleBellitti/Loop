# One image, nine commands. The services differ by which entry point they run,
# not by what they contain, so there is one layer cache and one thing to patch.
#
# Two languages, one image. Node exists only to turn the PWA into static files;
# nothing in the runtime stage can run JavaScript, which is the point — the
# build toolchain is the largest part of the attack surface and it does not
# ship.

FROM node:22-alpine AS client

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build


FROM python:3.12-slim

WORKDIR /app

# `uv` resolves and installs from the lock file, so the image gets the versions
# the tests ran against rather than whatever resolves today.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# uv's default link mode clones out of its cache with a reflink, falling back to
# a hardlink — which is what a copy-on-write filesystem wants and what the
# overlay a build runs on cannot always give. The clone comes back `EAGAIN`, and
# the step that fails is uv installing its own build backend into the isolated
# environment it builds `loop` in, so the error names hatchling and not
# anything this repository wrote.
#
# `copy` is the mode for building an image. The cost is one extra copy of each
# wheel, into a layer that is discarded before the runtime stage anyway.
ENV UV_LINK_MODE=copy

# Dependencies before source: this layer is invalidated by a lock change, not by
# every edit to a route.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev --extra all

COPY backend/loop ./loop
COPY backend/migrations ./migrations
COPY backend/rules ./rules
COPY backend/scripts ./scripts
RUN uv sync --frozen --no-dev --extra all

# `loop/api/app.py` resolves the built client as `../frontend/dist` relative to
# the package root, which is `/app` here — so this is the same relative place it
# sits in a checkout, and `CLIENT_DIR` does not need setting.
COPY --from=client /build/dist /frontend/dist

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Nothing here needs root, and nothing here needs to *write* either — the tree
# is read and executed, never modified. So the user is created and switched to,
# and no ownership is rewritten: a `chown -R /app` touches the metadata of every
# file in the virtualenv, and overlayfs copies a whole file up when its metadata
# changes, which stored a second complete copy of it in the image.
RUN useradd --create-home --uid 10001 loop
USER loop

# Only the API serves HTTP. The queue-driven services answer this with a
# non-zero exit, which compose reads as unhealthy for something that has no
# port to be healthy on — so they get their own check below in compose.yaml.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import os,urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','3000')+'/health').status==200 else 1)"

CMD ["python", "-m", "loop", "api"]
