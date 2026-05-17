# One image, six Railway services. Each service overrides startCommand;
# see DEPLOY.md for the table.
#
# tectonic comes from Debian bookworm (python:3.11-slim is bookworm-based).
# We do NOT install Playwright browsers — the apply worker talks to Bright
# Data's hosted Chrome over CDP, so only the Python bindings ship.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tectonic \
        ca-certificates \
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
