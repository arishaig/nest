# Verifier image for verify.py. Built locally by migrate-local.sh; it runs on
# an --internal Docker network, so dependencies are baked in at build time.
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
RUN pip install --no-cache-dir "psycopg[binary]==3.3.6"
COPY verify.py /verify.py
ENTRYPOINT ["python3", "-I", "/verify.py"]
