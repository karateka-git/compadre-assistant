FROM python:3.12-slim@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker-cli docker-compose git openssh-client systemd && rm -rf /var/lib/apt/lists/*

# Числовые UID/GID совпадают с владельцем bind mounts на hosted runner.
ARG WORKER_UID
ARG WORKER_GID
RUN test "$WORKER_UID" -gt 0 && test "$WORKER_GID" -gt 0 \
    && (getent group "$WORKER_GID" >/dev/null || groupadd --gid "$WORKER_GID" compatibility) \
    && useradd --uid "$WORKER_UID" --gid "$WORKER_GID" --create-home --no-log-init compatibility
ENV HOME=/home/compatibility
USER compatibility
