# One image, six Railway services. Each service overrides startCommand;
# see DEPLOY.md for the table.
#
# Tectonic is installed from the upstream release tarball. Debian slim images
# do not consistently ship a tectonic apt package.
# We do NOT install Playwright browsers — the apply worker talks to Bright
# Data's hosted Chrome over CDP, so only the Python bindings ship.

FROM python:3.11-slim-bookworm

ARG TECTONIC_VERSION=0.16.9

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fontconfig \
        libfontconfig1 \
        libfreetype6 \
        libgraphite2-3 \
        libharfbuzz0b \
        libharfbuzz-icu0 \
        libicu72 \
        libpng16-16 \
        libssl3 \
        zlib1g \
 && mkdir -p /tmp/tectonic \
 && curl -fsSL \
        "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/tectonic-${TECTONIC_VERSION}-x86_64-unknown-linux-gnu.tar.gz" \
        -o /tmp/tectonic.tar.gz \
 && tar -xzf /tmp/tectonic.tar.gz -C /tmp/tectonic \
 && cp "$(find /tmp/tectonic -type f -name tectonic | head -n 1)" /usr/local/bin/tectonic \
 && chmod +x /usr/local/bin/tectonic \
 && tectonic --version \
 && rm -rf /tmp/tectonic /tmp/tectonic.tar.gz \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer cache than COPY src first).
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# Cron services need scripts/; web + workers don't, but it's a few KB.
COPY scripts ./scripts

EXPOSE 8000

# Default = API. Workers and crons override via Railway's startCommand.
CMD ["uvicorn", "applyd.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
