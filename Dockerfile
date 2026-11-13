FROM pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/RAGnarok
COPY . .
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir -e .

ENTRYPOINT ["ragnarok"]
CMD ["--help"]
