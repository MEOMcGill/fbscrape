FROM python:3.12-slim-bookworm

ARG UID=1000
ARG GID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DISPLAY=:99 \
    SCREEN_RES=1280x800x24

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2 \
        libx11-xcb1 libxcb-shm0 libxcomposite1 libxcursor1 libxdamage1 \
        libxfixes3 libxi6 libxrandr2 libxtst6 libnss3 libpango-1.0-0 \
        fonts-liberation fonts-noto-core \
        xvfb x11vnc fluxbox x11-utils \
        novnc websockify \
        tini ca-certificates curl \
        vim less \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g ${GID} scraper \
 && useradd -m -u ${UID} -g ${GID} -s /bin/bash scraper

# Xvfb expects /tmp/.X11-unix to exist as a sticky world-writable dir.
# Without it, non-root Xvfb prints `_XSERVTransmkdir: ERROR: euid != 0`.
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY fbscrape ./fbscrape
RUN pip install --no-cache-dir --no-deps -e .

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
 && chown -R scraper:scraper /app

USER scraper

RUN python -m camoufox fetch

EXPOSE 6080

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
