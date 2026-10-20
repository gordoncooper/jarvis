# Minimal Ubuntu image with systemd as PID 1 for base-os Ansible container tests.
ARG UBUNTU_VERSION=24.04
FROM ubuntu:${UBUNTU_VERSION}

ENV container=docker
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chrony \
        dbus \
        python3 \
        python3-apt \
        sudo \
        systemd \
        systemd-sysv \
        tzdata \
        unattended-upgrades \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Drop unit symlinks that pull in full desktop/multi-user deps in Docker.
RUN find /etc/systemd/system /lib/systemd/system -type f -path '*.wants/*' \
    ! -name 'systemd-journald.service' \
    -delete 2>/dev/null || true

STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
