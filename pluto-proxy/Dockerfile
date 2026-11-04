# Runtime for pluto-proxy: Python plus Alpine's own ffmpeg package (decrypt,
# copy-remux and black/silent ad slates). The script itself is not in the
# image: it is k8s/apps/media/pluto-proxy/pluto_proxy.py, mounted from the
# pluto-proxy-script ConfigMap and re-exec'd when that changes, so a script
# change needs no image build.
FROM python:3.15-alpine

LABEL org.opencontainers.image.source="https://github.com/arishaig/nest"

RUN apk add --no-cache ffmpeg

USER 65534:65534
