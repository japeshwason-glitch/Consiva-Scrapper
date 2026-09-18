# Unbound built from Alpine rather than a prebuilt image.
#
# mvance/unbound is amd64 and mvance/unbound-rpi is 32-bit ARMv7; the GB10 is
# arm64, and both failed on it -- the second with "exec format error". Alpine
# publishes a real arm64 base and carries unbound in its repositories, so this
# builds natively on whatever the host is.
FROM alpine:3.20

# drill comes from ldns, and is what the healthcheck uses.
RUN apk add --no-cache unbound drill \
    && mkdir -p /etc/unbound && chown -R unbound:unbound /etc/unbound

COPY unbound.conf /etc/unbound/unbound.conf

EXPOSE 53/udp 53/tcp
# -d keeps it in the foreground, which is what the container needs.
CMD ["unbound", "-d", "-c", "/etc/unbound/unbound.conf"]
