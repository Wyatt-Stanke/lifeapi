# One image for all three services (api, frontend, scraper); compose picks the command.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    LIFEAPI_DATA_DIR=/data \
    TZ=America/New_York

# tzdata: storage treats naive datetimes as local time. xvfb: the virtual display for
# headed sources (VHL), which the scraper starts while they run, and for login.sh. xdotool:
# clicks Cloudflare's checkbox as a real pointer event. x11vnc: only for
# deploy/container/login.sh (finishing a sign-in challenge by hand over VNC).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata xvfb x11vnc xdotool \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first (read from pyproject.toml) so code edits don't reinstall them.
COPY pyproject.toml ./
RUN pip install $(python -c "import tomllib; print(' '.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))")

# Google Chrome is amd64-only on Linux. On arm64 (e.g. podman on Apple Silicon) fall back
# to patchright's Chromium; the entrypoint switches the channel when Chrome is missing.
RUN if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
        patchright install --with-deps chrome; \
    else \
        patchright install --with-deps chromium; \
    fi \
 && rm -rf /var/lib/apt/lists/*

COPY lifeapi ./lifeapi
COPY frontend ./frontend
COPY deploy/container ./deploy/container
RUN pip install --no-deps . \
 && chmod +x deploy/container/*.sh \
 && useradd --create-home --uid 1000 lifeapi \
 && mkdir -p /data \
 && chown lifeapi:lifeapi /data

USER lifeapi
VOLUME /data
ENTRYPOINT ["/app/deploy/container/entrypoint.sh"]
CMD ["python", "-m", "lifeapi.api", "--host", "0.0.0.0", "--port", "8000"]
