# syntax=docker/dockerfile:1
#
# fun-prompts — gallery of zero-shot AI apps + the builder that makes them.
# One all-in-one image: FastAPI app/admin/API on 8080, artifact origin on 8081,
# SQLite + encryption key under the /data volume. Rootless-friendly by design:
# nothing in the image needs root at runtime.

FROM docker.io/library/python:3.13-slim AS deps
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

FROM docker.io/library/python:3.13-slim
LABEL org.opencontainers.image.title="fun-prompts" \
      org.opencontainers.image.description="Gallery of zero-shot applications built by AI, and the builder that makes them."

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FP_HOST=0.0.0.0 \
    FP_PORT=8080 \
    FP_ARTIFACT_PORT=8081 \
    FP_ARTIFACT_BASE_URL=http://localhost:8081 \
    FP_DATA_DIR=/data \
    FP_LOG_LEVEL=INFO

WORKDIR /srv/app
COPY --from=deps /install /usr/local
COPY app ./app
COPY gallery_static ./gallery_static
COPY prompts ./prompts
COPY seed ./seed

RUN groupadd -r -g 10001 funprompts \
    && useradd -r -u 10001 -g 10001 -d /data -s /usr/sbin/nologin funprompts \
    && mkdir -p /data \
    && chown -R 10001:10001 /data /srv/app

USER 10001:10001

EXPOSE 8080 8081
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=4s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"]

CMD ["python", "-m", "app.main"]
