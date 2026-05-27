FROM alpine:3.23

RUN apk add --no-cache python3 py3-pip

WORKDIR /app

COPY pyproject.toml README.md ./
COPY pluginexecutor.py .

RUN python3 -m venv /venv && \
    /venv/bin/pip install --no-cache-dir .

ENV PATH="/venv/bin:$PATH"

USER nobody

ENTRYPOINT ["pluginexecutor"]
CMD ["/app/config.yaml"]
