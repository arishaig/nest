FROM python:3.15-slim

LABEL org.opencontainers.image.source="https://github.com/arishaig/nest"

RUN useradd -m -u 1000 app

WORKDIR /build
COPY pyproject.toml .
COPY meshcore_bridge/ meshcore_bridge/
RUN pip install --no-cache-dir .

USER app
EXPOSE 8080
CMD ["meshcore-bridge"]
