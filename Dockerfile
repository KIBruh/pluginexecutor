FROM alpine:3.23

WORKDIR /tmp/build

COPY pyproject.toml README.md ./
COPY pluginexecutor/ ./pluginexecutor/

RUN apk update && apk upgrade && \
    apk add --no-cache python3 py3-pip monitoring-plugins && \
    apk add --no-cache --virtual .build-deps git curl && \
    python3 -m venv --system-site-packages /venv && \
    /venv/bin/pip install --no-cache-dir . && \
    /venv/bin/pip install --no-cache-dir "git+https://github.com/bb-Ricardo/check_redfish@v2.1.2" && \
    curl -fsSL 'https://raw.githubusercontent.com/nbuchwitz/check_pve/8711172166802584d0beb87f1cfe764e3eef35e0/check_pve.py' -o /venv/bin/check_pve && \
    chmod 0755 /venv/bin/check_pve && \
    apk del .build-deps && \
    rm -rf /tmp/build && \
    mkdir -p /app

ENV PATH="/venv/bin:$PATH"

WORKDIR /app

USER nobody

ENTRYPOINT ["pluginexecutor"]
CMD ["config.yaml"]
