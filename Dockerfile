# ─────────────────────────────────────────────────────────────────────────────
#  Resume ATS Optimizer — Telegram Bot
#  Hosted on Koyeb  |  Python 3.12 slim
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Prevent .pyc files and enable unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ── System deps ───────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libxml2 \
        libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps ───────────────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── App source ────────────────────────────────────────────────────────────────
COPY bot.py resume_processor.py ./

# ── Non-root user (Koyeb best practice) ──────────────────────────────────────
RUN adduser --disabled-password --gecos "" botuser
USER botuser

# ── Runtime ───────────────────────────────────────────────────────────────────
# PORT is injected by Koyeb automatically; bot reads it.
EXPOSE 8443

CMD ["python", "bot.py"]
