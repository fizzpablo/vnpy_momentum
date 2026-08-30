# Paper strategy runtime.  IB Gateway intentionally remains on the Linux host.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# PySide6 is a transitive vn.py dependency.  These shared libraries keep imports
# working in a minimal, headless image without installing a desktop environment.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libegl1 libgl1 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY vnpy ./vnpy
COPY vnpy_ib ./vnpy_ib
COPY user_strategy/requirements.txt ./user_strategy/requirements.txt

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir . \
    && python -m pip install --no-cache-dir -r user_strategy/requirements.txt ./vnpy_ib

COPY user_strategy ./user_strategy

RUN useradd --create-home --uid 10001 strategy
USER strategy

ENTRYPOINT ["python", "-m", "user_strategy.run_strategy"]
CMD ["/runtime/config/paper.yaml"]
