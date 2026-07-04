# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# ── builder ──
FROM base AS builder
WORKDIR /build
COPY . .
RUN pip install build && python -m build

# ── runtime ──
FROM python:3.12-slim

RUN adduser --system --group sysstable

RUN apt-get update && apt-get install -y --no-install-recommends \
    procps \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install /tmp/rapidwebs_sysstable-*.whl && rm /tmp/rapidwebs_sysstable-*.whl

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD sysstable status >/dev/null 2>&1 || exit 1

USER sysstable
ENTRYPOINT ["sysstable"]
CMD ["start", "--foreground"]
