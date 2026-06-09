# ── Stage 1: install Chrome ────────────────────────────────────────────────
FROM python:3.9-slim

# System deps + Google Chrome stable
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg2 ca-certificates apt-transport-https \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub \
       | gpg --dearmor > /etc/apt/trusted.gpg.d/google.gpg \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 2: Python deps ───────────────────────────────────────────────────
WORKDIR /app

COPY requirements_concerts.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 3: app code ──────────────────────────────────────────────────────
COPY app.py .
COPY templates/ templates/

# Railway injects $PORT at runtime; default 5001 for local docker run
ENV FLASK_DEBUG=0
EXPOSE 5001

CMD ["python", "app.py"]
