FROM alpine:3.23

RUN apk add --no-cache python3 py3-pip monitoring-plugins

WORKDIR /app

COPY pyproject.toml README.md ./
COPY pluginexecutor.py .

RUN apk add --no-cache --virtual .build-deps git && \
    python3 -m venv /venv && \
    /venv/bin/pip install --no-cache-dir . && \
    /venv/bin/pip install --no-cache-dir "git+https://github.com/bb-Ricardo/check_redfish@v2.1.2" && \
    apk del .build-deps

ENV PATH="/venv/bin:$PATH"

USER nobody

ENTRYPOINT ["pluginexecutor"]
CMD ["/app/config.yaml"]
