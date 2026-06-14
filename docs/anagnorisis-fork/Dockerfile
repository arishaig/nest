# Drop-in replacement for volotat/Anagnorisis's Dockerfile.
# Changes from upstream:
#   1. CPU-only PyTorch (no CUDA wheels) — saves ~5 GB of image size
#   2. Adds `COPY . /app` so the source is baked in (required for k8s; upstream relies on bind-mount)
#   3. Removes NVIDIA-specific env vars (unused on CPU)

# ── Stage 1: builder ─────────────────────────────────────────────────────────
FROM python:3.10-slim-bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip

# CPU-only PyTorch — significantly smaller than the CUDA build
RUN pip install --no-cache-dir torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ARG MODULE_REQS_CACHE_BUST=1
COPY modules/ /tmp/module_reqs/
RUN find /tmp/module_reqs/ -name 'requirements.txt' -exec pip install --no-cache-dir -r {} \; || true

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.10-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsm6 \
    libxext6 \
    curl \
    ca-certificates \
    gcc \
    libc6-dev \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /venv /venv

ENV VENV_PATH=/venv
ENV PATH="/venv/bin:$PATH"

WORKDIR /app

# Bake source into the image (required for k8s — no bind-mount available)
COPY . /app

EXPOSE 5001

# Default CMD redirects to a log file; k8s overrides this with `command: ["python", "app.py"]`
# to emit to stdout so Loki can collect logs.
CMD ["bash", "-c", "/venv/bin/python app.py > /app/logs/${CONTAINER_NAME:-container}_log.txt 2>&1"]
