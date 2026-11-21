FROM python:3.12-slim@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker-cli docker-compose git openssh-client systemd && rm -rf /var/lib/apt/lists/*
