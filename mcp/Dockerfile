FROM python:3.14-slim

# ffmpeg: dispatcharr_probe_channels decodes a few frames per live channel.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 app

WORKDIR /build
COPY pyproject.toml .
COPY nest_mcp/ nest_mcp/
RUN pip install --no-cache-dir .

USER app
EXPOSE 8765
CMD ["nest-mcp-serve"]
