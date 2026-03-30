ARG PY_BASE_IMAGE=python:3.12-slim
FROM ${PY_BASE_IMAGE}

ARG APT_DEBIAN_MIRROR=
ARG APT_DEBIAN_SECURITY_MIRROR=
ARG PLAYWRIGHT_DOWNLOAD_HOST=https://playwright.azureedge.net
ARG PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ARG SKIP_PLAYWRIGHT_BROWSER_DOWNLOAD=0

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV DEBIAN_FRONTEND=noninteractive
ENV PLAYWRIGHT_DOWNLOAD_HOST=${PLAYWRIGHT_DOWNLOAD_HOST}
ENV PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH}

WORKDIR /app

RUN set -eux; \
    . /etc/os-release; \
    if [ -z "$APT_DEBIAN_MIRROR" ]; then APT_DEBIAN_MIRROR="https://deb.debian.org/debian"; fi; \
    if [ -z "$APT_DEBIAN_SECURITY_MIRROR" ]; then APT_DEBIAN_SECURITY_MIRROR="https://security.debian.org/debian-security"; fi; \
    rm -f /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list || true; \
    echo "deb $APT_DEBIAN_MIRROR $VERSION_CODENAME main contrib non-free non-free-firmware" > /etc/apt/sources.list; \
    echo "deb $APT_DEBIAN_MIRROR $VERSION_CODENAME-updates main contrib non-free non-free-firmware" >> /etc/apt/sources.list; \
    echo "deb $APT_DEBIAN_SECURITY_MIRROR $VERSION_CODENAME-security main contrib non-free non-free-firmware" >> /etc/apt/sources.list; \
    (apt-get update -o Acquire::ForceIPv4=true -o Acquire::Retries=5 -o Acquire::http::Timeout=60 -o Acquire::https::Timeout=60) || (sleep 3 && apt-get update -o Acquire::ForceIPv4=true -o Acquire::Retries=5 -o Acquire::http::Timeout=60 -o Acquire::https::Timeout=60); \
    (apt-get install -y --no-install-recommends -o Acquire::ForceIPv4=true \
    build-essential \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    curl \
    wget \
    unzip \
    ) || (sleep 3 && apt-get install -y --no-install-recommends -o Acquire::ForceIPv4=true build-essential libjpeg62-turbo-dev zlib1g-dev curl wget unzip); \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

RUN set -eux; \
    python -m playwright install-deps chromium || (sleep 3 && python -m playwright install-deps chromium)

RUN set -eux; \
    if [ "$SKIP_PLAYWRIGHT_BROWSER_DOWNLOAD" = "1" ]; then \
      echo "Skip Playwright browser download (offline)."; \
      exit 0; \
    fi; \
    for h in "$PLAYWRIGHT_DOWNLOAD_HOST" "https://cdn.playwright.dev" "https://playwright-akamai.azureedge.net" "https://playwright-verizon.azureedge.net"; do \
      if [ -z "$h" ]; then continue; fi; \
      echo "Trying PLAYWRIGHT_DOWNLOAD_HOST=$h"; \
      PLAYWRIGHT_DOWNLOAD_HOST="$h" python -m playwright install --with-deps chromium && exit 0; \
      sleep 3; \
    done; \
    exit 1

COPY . /app

RUN python manage.py collectstatic --noinput

ENV DJANGO_DEBUG=false
ENV DJANGO_ALLOWED_HOSTS=*
ENV PORT=8003

CMD python manage.py migrate --noinput && gunicorn qa_platform.wsgi:application --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 120
