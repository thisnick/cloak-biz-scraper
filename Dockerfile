# cloak-biz-scraper — the whole image, buildable by anyone, from public sources.
#
# Three things this image deliberately does NOT contain:
#
#   * The CloakBrowser Pro Chromium. It is proprietary and non-redistributable,
#     and a mounted volume shadows the image layer anyway, so baking it in was
#     always pointless. The cloakbrowser package downloads it on demand into
#     /data/.cloakbrowser on first launch (CLOAKBROWSER_CACHE_DIR).
#   * Any build secret. Nothing here needs a license key, so `docker build`
#     works for a stranger with no credentials at all.
#   * Windows fonts. They are proprietary and cannot ship publicly.
#
# System-lib list adapted from CloakBrowser-Manager (MIT).

FROM python:3.12-slim-bookworm

# Chromium runtime libraries, fontconfig, and Xvfb (headed Chromium needs a
# display; headless is a fingerprint). Xvfb stays as the fallback for when Xvnc
# is unavailable — the pool still runs, just without live view.
# novnc is the viewer served at /novnc; static assets, no build step. Its version
# is PINNED (`novnc=1:1.3.0-1`, what bookworm ships) rather than floating: an
# unpinned viewer is one that can change its client behaviour under us between two
# identical builds, and the live-view panes speak to it directly. Pinning matches
# how the in-page JS libraries (readability.js, turndown.js) are already handled.
# If a bookworm point release ever retires this exact version and the build fails
# to find it, that is the signal to vendor a known noVNC release instead — the
# same call the CloakBrowser pin taught us to make deliberately.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdbus-1-3 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libx11-xcb1 libfontconfig1 libx11-6 \
    libxcb1 libxext6 libxshmfence1 \
    libglib2.0-0 libgtk-3-0 libpangocairo-1.0-0 libcairo-gobject2 \
    libgdk-pixbuf-2.0-0 libxss1 libxtst6 \
    libgl1-mesa-dri libegl-mesa0 \
    xvfb fontconfig procps ca-certificates wget "novnc=1:1.3.0-1" \
    && rm -rf /var/lib/apt/lists/*

# KasmVNC — Xvnc: an X server that also serves its framebuffer over a websocket,
# which is what makes live inspection possible. A drop-in for Xvfb; the browser
# cannot tell the difference.
#
# TARGETARCH is required here and only here. Everything else in this image is
# apt or npm and resolves per-architecture on its own; this is a *direct binary
# download*, so it is the one thing that must name an architecture. Railway
# builds amd64, a Mac builds arm64, and each has to fetch its own — hardcoding
# either would produce an image that cannot run on the other.
ARG TARGETARCH
RUN wget -q "https://github.com/kasmtech/KasmVNC/releases/download/v1.3.3/kasmvncserver_bookworm_1.3.3_${TARGETARCH}.deb" \
    && apt-get update \
    && apt-get install -y -f --no-install-recommends "./kasmvncserver_bookworm_1.3.3_${TARGETARCH}.deb" \
    && rm "kasmvncserver_bookworm_1.3.3_${TARGETARCH}.deb" \
    && rm -rf /var/lib/apt/lists/*

# Node, for exactly one job: markdown -> Notion blocks via martian. Notion's
# block schema is fiddly enough (nested rich_text, per-object limits, which
# markdown it silently drops) that reimplementing it in Python would be a
# permanent liability for no gain.
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# After the pip layer so editing the script does not reinstall Python deps.
RUN mkdir -p /opt/md2blocks && cd /opt/md2blocks && npm init -y >/dev/null \
    && npm install @tryfabric/martian@1.2.4 && npm cache clean --force
COPY md2blocks/md2blocks.mjs /opt/md2blocks/md2blocks.mjs

COPY app/ /app/app/
COPY scripts/ /app/scripts/

# No CLOAKBROWSER_LICENSE_KEY or CLOAKBROWSER_VERSION here on purpose: both are
# settings, read from the volume and passed to launch as arguments. Baking a pin
# into the image would silently outrank whatever the user later sets in the UI.
ENV DATA_DIR=/data \
    PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# No `VOLUME /data` here, deliberately. /data holds the settings, the Chromium
# binary cache, profiles, jobs and evidence — but Railway *rejects the image at
# parse time* if the Dockerfile declares it: "docker VOLUME at Line 78 is not
# supported, use Railway Volumes". The build fails in ~3s before a single layer
# runs, so it cannot be caught by any local build. The mount is supplied from
# outside instead: `data:/data` in docker-compose.yml, a Railway Volume in the
# template.

# Railway injects PORT; honour it without needing a rebuild.
#
# --forwarded-allow-ips=*: Railway terminates TLS at its edge and speaks plain
# HTTP to this container, so without trusting its X-Forwarded-* headers uvicorn
# reports every request as http and every caller as the edge's address. That
# breaks two things that are invisible locally — the OAuth issuer would advertise
# http:// (which RFC 8414 clients refuse) and the login rate limiter would put
# every user in one bucket. `*` is the right value *here* because nothing can
# reach this container except through that edge; it would be wrong on a host
# where the port is exposed directly.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --forwarded-allow-ips='*'"]
