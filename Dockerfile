FROM alpine:3.23
LABEL maintainer="Rob Smith https://github.com/RobXYZ"

# TARGETARCH is set automatically by `docker buildx build` (amd64,
# arm64, …). Plain `docker build` does NOT set it; in that case we
# fall back to `apk --print-arch`, which reports the actual
# container architecture (x86_64, aarch64, …) and is correct under
# both native builds and QEMU emulation.
ARG TARGETARCH

# System deps:
# - python3 + pip: runtime + installing web deps
# - ffmpeg: exports + thumbnails
# - bash, shadow, tzdata: entrypoint + PUID/PGID remapping
# - intel-media-driver, libva-utils (Intel x86_64 only): VA-API
#   userspace + diagnostic tool. ffmpeg's h264_qsv / h264_vaapi
#   need iHD_drv_video.so to talk to an Intel iGPU when the host
#   maps /dev/dri into the container; without it the MFX runtime
#   fails immediately with "MFX session: -9". `vainfo` from
#   libva-utils is a one-liner diagnostic the operator can run via
#   `docker exec` to verify the passthrough is wired up correctly.
#   These packages don't exist on linux/arm64. The app's encoder
#   probe (web/services/exporter.py) runtime-tests every candidate
#   and falls back to libx264 software encode if QSV / VAAPI
#   aren't available, so the missing packages on ARM degrade
#   transparently.
RUN apk add --no-cache \
        bash python3 py3-pip ffmpeg shadow su-exec tzdata && \
    arch="${TARGETARCH:-$(apk --print-arch)}" && \
    case "$arch" in \
        amd64|x86_64) apk add --no-cache intel-media-driver libva-utils ;; \
    esac && \
    useradd -UMr dashcam

COPY COPYING /
COPY setuid.sh /setuid.sh
COPY entrypoint.sh /entrypoint.sh

ENV PUID="" \
    PGID="" \
    RECORDINGS="/recordings"

# Install Python deps into the system site-packages. Alpine's
# pip refuses by default (PEP 668); --break-system-packages is
# safe inside a container.
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir --break-system-packages \
        -r /requirements.txt

COPY --chown=dashcam viofosync_lib /viofosync_lib
COPY --chown=dashcam web /web

EXPOSE 8080

RUN sed -i 's/\r$//' /entrypoint.sh /setuid.sh \
    && chmod +x /entrypoint.sh /setuid.sh

ENTRYPOINT [ "/entrypoint.sh"]
