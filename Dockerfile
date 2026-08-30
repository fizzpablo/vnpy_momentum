# Paper strategy runtime.  IB Gateway intentionally remains on the Linux host.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# PySide6 is a transitive vn.py dependency.  These shared libraries keep imports
# working in a minimal, headless image without installing a desktop environment.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libegl1 libgl1 libxext6 libxrender1 curl unzip \
    && rm -rf /var/lib/apt/lists/*

ARG IBAPI_URL=https://interactivebrokers.github.io/downloads/twsapi_macunix.1050.01.zip
ARG IBAPI_SHA256=aa065722ca732a41aab202c7bb72932e179b86e7ec51cefa063eb1983fe9f597

RUN curl -fL "$IBAPI_URL" -o /tmp/twsapi.zip \
    && echo "$IBAPI_SHA256  /tmp/twsapi.zip" | sha256sum -c - \
    && unzip -q /tmp/twsapi.zip -d /tmp/twsapi \
    && python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir /tmp/twsapi/IBJts/source/pythonclient \
    && rm -rf /tmp/twsapi /tmp/twsapi.zip

COPY pyproject.toml README.md ./
COPY vnpy ./vnpy
COPY vnpy_ib ./vnpy_ib
COPY user_strategy/requirements.txt ./user_strategy/requirements.txt

RUN python -m pip install --no-cache-dir . \
    && python -m pip install --no-cache-dir -r user_strategy/requirements.txt ./vnpy_ib \
    && python -c "import ibapi, inspect; from ibapi.wrapper import EWrapper; from ibapi.contract import ContractDetails; from ibapi.order_cancel import OrderCancel; assert getattr(ibapi, '__version__', '') == '10.50.1'; assert hasattr(ContractDetails(), 'minSize'); assert 'errorTime' in str(inspect.signature(EWrapper.error)); print('IB API compatibility OK:', ibapi.__version__)"

COPY user_strategy ./user_strategy

RUN useradd --create-home --uid 10001 strategy
USER strategy

ENTRYPOINT ["python", "-m", "user_strategy.run_strategy"]
CMD ["/runtime/config/paper.yaml"]
