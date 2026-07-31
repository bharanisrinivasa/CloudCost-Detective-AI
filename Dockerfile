# Builder stage
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/

RUN pip install --no-cache-dir --user -r requirements.txt

# Runtime stage
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH=/home/django/.local/bin:$PATH

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated non-root user
RUN groupadd -r django && useradd -r -g django -m -d /home/django django

# Copy dependencies from builder
COPY --from=builder /root/.local /home/django/.local
RUN chown -R django:django /home/django/.local

# Copy application source
COPY --chown=django:django . /app

# Setup static and media folders with safe permissions
RUN mkdir -p /app/staticfiles /app/media \
    && chown -R django:django /app/staticfiles /app/media \
    && chmod -R 775 /app/staticfiles /app/media

USER django

EXPOSE 8000

CMD ["gunicorn", "-c", "deployment/gunicorn.conf.py", "cloud_cost_detective.wsgi:application"]
