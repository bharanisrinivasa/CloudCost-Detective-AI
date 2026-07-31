import os
import multiprocessing

# Gunicorn production configuration file
bind = "0.0.0.0:8000"

# Support env configuration or fallback to safe multiprocessing defaults
workers = int(os.environ.get("GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 30))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", 30))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", 2))

# Logging to stdout/stderr for container captures
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Process naming
proc_name = "cloud_cost_detective"
