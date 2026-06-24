FROM python:3.14-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 app

WORKDIR /build
COPY pyproject.toml .
COPY nest_mcp/ nest_mcp/
RUN pip install --no-cache-dir .

USER app
EXPOSE 8765
CMD ["nest-mcp-serve"]
