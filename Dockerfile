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
# display; headless is a fingerprint). Xvfb is where KasmVNC drops in later.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdbus-1-3 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libx11-xcb1 libfontconfig1 libx11-6 \
    libxcb1 libxext6 libxshmfence1 \
    libglib2.0-0 libgtk-3-0 libpangocairo-1.0-0 libcairo-gobject2 \
    libgdk-pixbuf-2.0-0 libxss1 libxtst6 \
    libgl1-mesa-dri libegl-mesa0 \
    xvfb fontconfig procps ca-certificates \
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
# Settings, the Chromium binary cache, profiles, and (later) jobs + evidence.
VOLUME /data

# Railway injects PORT; honour it without needing a rebuild.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
