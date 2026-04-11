# Build from repository root:
#   docker build -f astrocyte-services-py/astrocyte-rest/Dockerfile -t astrocyte-rest .
FROM python:3.11-slim

WORKDIR /app

COPY astrocyte-py /build/astrocyte-py
COPY adapters-py/astrocyte-pgvector /build/astrocyte-pgvector
COPY astrocyte-services-py/astrocyte-rest /build/astrocyte-rest

RUN pip install --no-cache-dir /build/astrocyte-py \
    && pip install --no-cache-dir /build/astrocyte-pgvector \
    && pip install --no-cache-dir /build/astrocyte-rest \
    && rm -rf /build

ENV ASTROCYTES_HOST=0.0.0.0
ENV ASTROCYTES_PORT=8080

EXPOSE 8080

CMD ["astrocyte-rest"]
