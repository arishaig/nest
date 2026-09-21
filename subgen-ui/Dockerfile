FROM python:3.14-slim

LABEL org.opencontainers.image.source="https://github.com/arishaig/nest"

RUN useradd -m -u 1000 app

WORKDIR /build
COPY pyproject.toml .
COPY subgen_ui/ subgen_ui/
RUN pip install --no-cache-dir .

USER app
EXPOSE 8080
CMD ["subgen-ui"]
