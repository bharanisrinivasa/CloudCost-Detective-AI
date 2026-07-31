# Production Deployment & Operation Guide

This guide describes how to configure, run, and maintain the **CloudCost Detective AI** platform in a secure, production-ready environment using Docker and Docker Compose.

---

## 1. Architecture Overview

In production, the application is structured as a secure, multi-tier system:

```
                  [ Internet ]
                       |
                       v  (HTTP / Port 80 by Default)
                 +-----------+
                 |   Nginx   | (Static Media Router & Proxy)
                 +-----------+
                       |
                       v  (Forwarded requests on Internal network)
         +-------------+-------------+
         |                           |
         v (Port 8000)               v (Port 8000)
    +----------+                +----------+
    |  Web 1   |                |  Web 2   | (Horizontal scaling target)
    | Gunicorn |                | Gunicorn |
    +----------+                +----------+
         |                           |
         +-------------+-------------+
                       |
        +--------------+--------------+
        |                             |
        v                             v
  +------------+                +------------+
  | PostgreSQL | (Database)     |   Redis    | (Broker/Backend)
  +------------+                +------------+
                                      ^
                                      |
                       +--------------+--------------+
                       |                             |
                       v                             v
               +---------------+             +---------------+
               | Celery Worker |             |  Celery Beat  |
               +---------------+             +---------------+
```

- **DEFAULT Deployment Traffic Flow**: Client -> HTTP port 80 -> Nginx -> Gunicorn/Django. Port 443 and SSL/TLS redirects are disabled by default until SSL certificates are provisioned on the host.
- **Nginx**: Reverses proxies, serves static/media files directly, intercepts 5xx errors to prevent leaking sensitive debug information. Can be configured to terminate TLS (port 443) during the production SSL transition.
- **Gunicorn**: WSGI server executing Django processes concurrently.
- **PostgreSQL 16**: Primary data store for tenants, projects, user sessions, OCI sync results, and legacy upload data.
- **Redis 7**: Broker and result store for Celery task routing.
- **Celery Worker**: Asynchronously executes background OCI resource sync operations.
- **Celery Beat**: Regularly triggers scheduled OCI cost and telemetry synchronization tasks.

---

## 2. Environment Configuration Checklist

Before deployment, create the production `.env` file from [.env.example](file:///c:/Users/bhara/OneDrive/Documents/projects/CloudCost%20Detective%20AI/.env.example). Fill in all variables and store them securely:

| Environment Variable | Required/Default | Description |
| :--- | :--- | :--- |
| `SECRET_KEY` | **Required** | Strong, unique django secret string. Do not reuse development keys. |
| `DEBUG` | `False` | Must be `False` in production to prevent traceback leaks. |
| `ALLOWED_HOSTS` | **Required** | Comma-separated list of domains/IPs allowed to query the app. |
| `CSRF_TRUSTED_ORIGINS` | **Required** | Trusted origins for CSRF protection (e.g., `https://cloudcost.yourdomain.com`). |
| `DB_NAME` | `db.sqlite3` | PostgreSQL database name. |
| `DB_USER` | `cloudcost` | PostgreSQL database user. |
| `DB_PASSWORD` | **Required** | Strong password for PostgreSQL access. |
| `DB_HOST` | `db` | Service hostname defined in compose (`db`). |
| `DB_PORT` | `5432` | PostgreSQL container connection port. |
| `CELERY_BROKER_URL` | `redis://redis:6379/0` | URL for Celery message broker. |
| `CELERY_RESULT_BACKEND`| `redis://redis:6379/0` | URL for Celery task results. |
| `OCI_ENCRYPTION_KEY` | **Required** | AES-256 base64-encoded key used to encrypt customer OCI API private keys. |
| `GEMINI_API_KEY` | **Required** | Google Gemini model API key for generating AI cost-anomaly explanations. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | The specific model deployment version to use. |
| `SECURE_SSL_REDIRECT` | `True` | Directs HTTP traffic to HTTPS (Highly Recommended once SSL is wired). |
| `SESSION_COOKIE_SECURE`| `True` | Ensures session cookies are only transmitted over HTTPS. |
| `CSRF_COOKIE_SECURE` | `True` | Ensures CSRF cookies are only transmitted over HTTPS. |

> [!WARNING]
> Never commit the production `.env` file to version control. Set restrictive file access permissions on the production VM (`chmod 600 .env`).

---

## 3. OCI Compute VM Deployment Steps

Follow these steps to deploy standard builds onto your target Oracle Cloud Infrastructure (OCI) Ubuntu/CentOS compute instances:

### Step 3.1: Hardening OCI Security Lists (Network Firewalls)
Locate the Virtual Cloud Network (VCN) associated with your production instance and customize ingress rules:
1. **Allow HTTP (Port 80)**: Source CIDR `0.0.0.0/0` (temporary or for cert renewal).
2. **Allow HTTPS (Port 443)**: Source CIDR `0.0.0.0/0` (public access).
3. **Allow SSH (Port 22)**: Restrict to target developer/operator bastion IPs.
4. **Block Port 8000 & 5432**: Ensure these are closed to external networks.

### Step 3.2: VM Host Packages & Docker Installation
Connect via SSH and install required host packages:
```bash
# Update and install Docker + Docker Compose
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git

# Enable docker daemon
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker
```

### Step 3.3: Set Up Project Files
Clone the repository and prepare runtime directories:
```bash
git clone <repository_url> cloudcost-detective
cd cloudcost-detective

# Create directories for persistent volumes
mkdir -p static media
```

---

## 4. Initializing & Starting Production Services

Deploy with compose by following this sequence:

### 1. Verify Configuration
Validate compose syntax and environment parsing:
```bash
docker compose config
```

### 2. Run Database Migrations
Provision PostgreSQL schemas safely using a one-off task execution:
```bash
docker compose run --rm web python manage.py migrate --noinput
```

### 3. Collect Static Assets
Gather styles, scripts, and media into the static volume:
```bash
docker compose run --rm web python manage.py collectstatic --noinput
```

### 4. Create an Initial Admin User
Provision the first administrative credential:
```bash
docker compose run --rm web python manage.py createsuperuser
```

### 5. Start All Containers
Launch Nginx, Django Web, Celery Worker, Celery Beat, Redis, and PostgreSQL in the background:
```bash
docker compose up -d
```

### 6. Verify Service Status
Ensure all containers are healthy:
```bash
docker compose ps
```

---

## 5. Security & Key Management Guidelines

1. **OCI API Keys**: When users configure OCI connections in the app, their private API key is encrypted using AES-256 via Django's field encryption, mapping back to the `OCI_ENCRYPTION_KEY`.
2. **Harden Host Keys**: Ensure any local private key files uploaded/transferred to target compartments are secured:
   ```bash
   chmod 600 /path/to/private_keys/*
   ```
3. **Container Capabilities Hardening**: Containers run inside a private bridge network without root privileges or write privileges to critical host folders, except for bound storage volumes (`media/` and `staticfiles/`).

---

## 6. SSL Certificate Provisioning & Transition to HTTPS

To transition the deployment from HTTP-only to HTTPS, follow these steps:

### Step 6.1: Initial HTTP Deployment & DNS Setup
1. Ensure the platform is successfully deployed and running over port 80 (HTTP).
2. Configure your DNS provider to point your domain name (e.g., `cloudcost.yourdomain.com`) to the public IP address of the OCI Compute VM.

### Step 6.2: Obtain TLS Certificates via Certbot
Run Certbot on the host machine using the Webroot plugin:
1. **Install Certbot**:
   ```bash
   sudo apt-get update
   sudo apt-get install -y certbot
   ```
2. **Generate Certificate**:
   ```bash
   sudo certbot certonly --webroot -w ./static -d cloudcost.yourdomain.com
   ```

### Step 6.3: Update Nginx to serve HTTPS
Modify `deployment/nginx/default.conf` to configure SSL endpoints:
```nginx
server {
    listen 80;
    server_name cloudcost.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name cloudcost.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/cloudcost.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/cloudcost.yourdomain.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    # ... existing static, media, and upstream proxy pass configurations ...
}
```

### Step 6.4: Enable Port 443 in Docker Compose
1. Edit `docker-compose.yml` to publish port 443 on the `nginx` container:
   ```yaml
     nginx:
       image: nginx:1.25-alpine
       ports:
         - "80:80"
         - "443:443"
       volumes:
         - /etc/letsencrypt:/etc/letsencrypt:ro
         - ./deployment/nginx/default.conf:/etc/nginx/conf.d/default.conf:ro
         - staticfiles:/app/staticfiles:ro
         - media:/app/media:ro
   ```
2. Reload Nginx configuration without downtime:
   ```bash
   docker compose up -d
   docker compose exec nginx nginx -s reload
   ```

### Step 6.5: Enable HTTPS Settings in Application Environment
1. Once HTTPS connectivity is verified manually, enable redirects in your environment (`.env`):
   ```ini
   SECURE_SSL_REDIRECT=True
   ```
2. After confirming the redirection and certificate chain are stable, enable long-duration HSTS settings:
   ```ini
   SECURE_HSTS_SECONDS=31536000
   SECURE_HSTS_INCLUDE_SUBDOMAINS=True
   SECURE_HSTS_PRELOAD=True
   ```
3. Restart containers to apply settings:
   ```bash
   docker compose restart web celery_worker celery_beat
   ```

---

## 7. Automated Healthchecks & Diagnostics

We provide health and readiness checkpoints to verify deployment state:

### 1. Basic Health Check
Checks if Gunicorn/Django is running and routing requests:
```bash
curl -I http://localhost/health/
```
**Response**: `200 OK` (with JSON body `{"status": "ok"}`).

### 2. Readiness Check
Checks if downstream database connectivity is active and responding:
```bash
curl -I http://localhost/ready/
```
- **Healthy Response**: `200 OK` (with JSON body `{"status": "ready"}`).
- **Failure Response**: `503 Service Unavailable` (with JSON body `{"status": "unavailable"}`) if database queries fail, signaling routing orchestrators (e.g., Load Balancers, Kubernetes, OCI Health Checks) to redirect traffic away from the instance. Raw exception details are never exposed.

---

## 8. Backup & Maintenance

### Database Backup & Restore

Extract a raw SQL dump of PostgreSQL data:
```bash
# Backup using configured production credentials
docker compose exec db pg_dump -U cloudcost cloudcost > backup_$(date +%F).sql
```

Restore the SQL dump back to PostgreSQL:
```bash
# Restore backup
docker compose exec -T db psql -U cloudcost -d cloudcost < backup.sql
```

### Reviewing Application Logs
Stream live output from the Web or Celery workers:
```bash
# View Web application logs
docker compose logs -f web

# View Celery background sync logs
docker compose logs -f celery_worker
```
