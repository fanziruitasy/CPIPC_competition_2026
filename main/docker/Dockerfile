# syntax=docker/dockerfile:1.7

FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm@sha256:7876580dc67fd460fd962f004cbeb480027e9bbc0657096f1087db11f9eaff39 AS runtime-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /workspace/main

# Docling 和 PyTorch 在无桌面环境运行所需的最小动态库。
RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && apt-get update \
    && apt-get install --yes --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade "pip==25.2" \
    && python -m pip install .

COPY configs ./configs
COPY resources ./resources
COPY scripts ./scripts
COPY docs ./docs


FROM runtime-base AS rag-api

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"

CMD ["python", "scripts/api/run.py"]


FROM runtime-base AS knowledge-builder

# DOC/XLS 转换必须使用 LibreOffice，中文字体用于 Docling 页面与图片渲染。
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        libreoffice-calc \
        libreoffice-writer \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

ENV LIBREOFFICE_EXECUTABLE=/usr/bin/soffice

CMD ["python", "scripts/indexing/build_unified_corpus.py", "--help"]
