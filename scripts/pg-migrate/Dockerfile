# Verifier image for verify.py. Built locally by migrate-local.sh; it runs on
# an --internal Docker network, so dependencies are baked in at build time.
FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
RUN pip install --no-cache-dir "psycopg[binary]==3.3.6"
COPY verify.py /verify.py
ENTRYPOINT ["python3", "-I", "/verify.py"]
