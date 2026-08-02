FROM python:3.10-slim AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PATH="/opt/venv/bin:$PATH"

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY *.py ./
COPY LICENSE VERSION ./
COPY config/config.template.json /app/template/config.template.json
COPY docker-entrypoint.sh /usr/local/bin/baidu-autosave-entrypoint

RUN mkdir -p /app/config /app/log /app/state && \
    chmod 755 /usr/local/bin/baidu-autosave-entrypoint && \
    chmod 777 /app/config /app/log /app/state

ENTRYPOINT ["baidu-autosave-entrypoint"]
CMD ["daemon"]
