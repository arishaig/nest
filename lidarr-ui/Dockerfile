FROM python:3.14-slim

RUN useradd -m -u 1000 app

WORKDIR /build
COPY pyproject.toml .
COPY lidarr_ui/ lidarr_ui/
RUN pip install --no-cache-dir .

USER app
EXPOSE 8080
CMD ["lidarr-ui"]
