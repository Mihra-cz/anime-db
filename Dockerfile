FROM python:3.12-slim-trixie

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/anime.db \
    METADATA_ARTWORK_DIRECTORY=/data/artwork

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid "$APP_GID" animedb \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --no-create-home \
       --home-dir /data --shell /usr/sbin/nologin animedb \
    && mkdir -p /data/artwork \
    && chown -R "$APP_UID:$APP_GID" /data

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY app ./app
RUN pip install --no-cache-dir .
COPY deploy/nexus/preflight.py /usr/local/libexec/anime-db/nexus_preflight.py

USER animedb
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
