# Runtime for pluto-proxy: Python plus Alpine's own ffmpeg package (decrypt,
# copy-remux and black/silent ad slates) and GStreamer's closed-caption
# elements (Pluto's subtitles as CEA-608 in the video, CC_CHANNELS). The
# script itself is not in the image: it is
# k8s/apps/media/pluto-proxy/pluto_proxy.py, mounted from the
# pluto-proxy-script ConfigMap and re-exec'd when that changes, so a script
# change needs no image build.
FROM python:3.15-alpine

LABEL org.opencontainers.image.source="https://github.com/arishaig/nest"

# The root filesystem is read-only at runtime, so GStreamer's plugin registry
# is built here and never rebuilt.
ENV GST_REGISTRY=/usr/lib/gstreamer-1.0/registry.bin \
    GST_REGISTRY_UPDATE=no

RUN apk add --no-cache ffmpeg gstreamer-tools gst-plugins-base gst-plugins-bad gst-plugins-rs-rsclosedcaption \
    && gst-inspect-1.0 tttocea608 > /dev/null \
    && chmod 644 "$GST_REGISTRY"

USER 65534:65534
