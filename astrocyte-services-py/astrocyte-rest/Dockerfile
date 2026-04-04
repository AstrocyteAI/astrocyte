# Build from repository root:
#   docker build -f astrocytes-services-py/astrocytes-rest/Dockerfile -t astrocytes-rest .
FROM python:3.11-slim

WORKDIR /app

COPY astrocytes-py /build/astrocytes-py
COPY astrocytes-services-py/astrocytes-pgvector /build/astrocytes-pgvector
COPY astrocytes-services-py/astrocytes-rest /build/astrocytes-rest

RUN pip install --no-cache-dir /build/astrocytes-py \
    && pip install --no-cache-dir /build/astrocytes-pgvector \
    && pip install --no-cache-dir /build/astrocytes-rest \
    && rm -rf /build

ENV ASTROCYTES_HOST=0.0.0.0
ENV ASTROCYTES_PORT=8080

EXPOSE 8080

CMD ["astrocytes-rest"]
