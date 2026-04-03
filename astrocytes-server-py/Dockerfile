# Build from repository root: docker build -f astrocytes-server-py/Dockerfile -t astrocytes-server .
FROM python:3.11-slim

WORKDIR /app

COPY astrocytes-py /build/astrocytes-py
COPY astrocytes-server-py /build/astrocytes-server-py

RUN pip install --no-cache-dir /build/astrocytes-py \
    && pip install --no-cache-dir /build/astrocytes-server-py \
    && rm -rf /build

ENV ASTROCYTES_HOST=0.0.0.0
ENV ASTROCYTES_PORT=8080

EXPOSE 8080

CMD ["astrocytes-server"]
