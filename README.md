# CloudCost Detective AI

CloudCost Detective AI is an Explainable GenAI-powered Cost Optimization Platform for Oracle Cloud Infrastructure (OCI). The platform monitors, analyzes, forecasts, simulates, and optimizes cloud expenditure.

---

## Tech Stack
* **Framework**: Django 6.0, Django REST Framework 3.16
* **Database**: PostgreSQL (Production) / SQLite (Local Development Fallback)
* **Data Processing**: pandas
* **Containerization**: Docker & Docker Compose
* **Asynchronous Integration**: Celery 5.6 & Redis 5.0 (Planned for background syncing and task automation)

---

## Project Structure
Below is a high-level representation of the project structure, showing the core modules and applications:

```text
CloudCost Detective AI/
├── manage.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── cloud_cost_detective/    # Global settings, URLs, ASGI/WSGI, Celery config
├── accounts/                # Module 1: Authentication System
├── billing/                 # Modules 2 & 3: CSV Upload and Data Processing
├── templates/               # UI Templates (login, dashboard, billing uploads)
├── static/                  # Style sheets and assets
├── dashboard/               # Module 4: Dashboard views
├── analytics/               # Modules 5, 6, 10: Anomalies, waste, and prediction
├── ai_engine/               # Modules 7, 8, 9: AI, assistant, rightsizing
├── simulator/               # Module 11: Cost Simulator
├── reports/                 # Module 12: PDF/Excel Reports generator
├── oci_connector/           # Module 14: OCI API ingestion connector
├── remediation/             # Module 15: Approval workflow executor
├── scheduler/               # Scheduled cron trigger definitions
└── api/                     # Central API routes mapping
```

---

## Completed Features (Modules 1–3)

### Module 1 — Authentication System
- **Custom User Model**: Extended user profiles to support organizations and phone numbers.
- **Secure Authentication**: Session-based login, registration, password validation, and profile management views.

### Module 2 — OCI Billing Upload
- **Validation**: Strict size and file extension (CSV-only) verification.
- **Billing & Usage Uploads**: Distinct web and API routes to handle both billing invoices and raw utilization data.
- **Upload Management**: Detailed history table listing previous uploads with statuses, file details, and direct deletion options.

### Module 3 — Billing Data Processing
- **CSV Ingestion**: Handles raw OCI cost reporting columns using Pandas.
- **Header & Row Validation**: Detects and enforces minimum required columns, mapping custom headers dynamically.
- **Data Deduplication**: Automatically skips identical billing records.
- **Graceful Error Handling**: Skips malformed rows (e.g. missing dates or non-decimal costs) and records skipped line count statistics.
- **Bulk Database Insertion**: High-performance database inserts to handle large CSV datasets efficiently.
- **Cost Aggregations**: Pre-aggregates imported rows to calculate service-wise, daily, monthly, and region-wise costs.
- **Import Metrics**: Displays rows read, rows imported, rows skipped, and parsing statuses.

*Note: In the current version, upload processing is executed synchronously within the request flow for simplicity and easier local development verification.*

---

## Installation Steps
Follow these instructions to run the application on your local machine:

### Prerequisites
- Python 3.11 or later
- pip (Python package installer)

### Setup Instructions
1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd "CloudCost Detective AI"
   ```

2. **Create a virtual environment**:
   ```bash
   python -m venv .venv
   ```

3. **Activate the virtual environment**:
   * **Windows (PowerShell)**:
     ```powershell
     .venv\Scripts\Activate.ps1
     ```
   * **Linux / macOS**:
     ```bash
     source .venv/bin/activate
     ```

4. **Install project dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

*Note: Modules 1–3 run locally using Python and the default SQLite database. Installing/running Redis or Celery is not required for local developer testing of these modules.*

---

## Environment Variables
Configuration is handled via environment variables. To run the project:

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
2. Adjust the values inside `.env` if necessary.

| Variable Name | Description | Default |
|---|---|---|
| `SECRET_KEY` | Django security signing key | `django-insecure-placeholder` |
| `DEBUG` | Django debug toggle | `True` |
| `DB_ENGINE` | Database engine | `django.db.backends.sqlite3` |
| `DB_NAME` | Database filename/identifier | `db.sqlite3` |
| `DB_HOST` | Database host | `localhost` |
| `DB_PORT` | Database port | `5432` |

---

## Running the Project
1. **Run Database Migrations**:
   ```bash
   python manage.py migrate
   ```

2. **Create an Admin Superuser**:
   ```bash
   python manage.py createsuperuser
   ```

3. **Start the Development Server**:
   ```bash
   python manage.py runserver
   ```

4. **Access the Portal**:
   Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/) in your browser and log in with your superuser credentials.

---

## Future Roadmap (Modules 4–15)
The platform is designed to scale across the following module specifications:

- **Module 4 — Interactive Dashboard**: High-level cost graphs, upload trends, and KPI summaries.
- **Module 5 — Cost Anomaly Detection**: Z-score and IQR models identifying unexpected spending spikes.
- **Module 6 — Resource Waste Detection**: Identification of idle compute instances, unattached block storage volumes, and orphan public IPs.
- **Module 7 — Explainable AI Engine**: Generation of plain-text optimization recommendations with evidence and confidence scores.
- **Module 8 — Natural Language Cost Chat**: Interactive LLM agent converting user questions to SQL queries and data summaries.
- **Module 9 — Rightsizing Recommendations**: OCI instance scaling suggestions matching performance requirements to resource sizing.
- **Module 10 — Monthly Cost Prediction**: Regression forecasting predicting end-of-month OCI invoices.
- **Module 11 — What-if Cost Simulator**: Interactive planning console measuring sandbox modifications against historical spend data.
- **Module 12 — Executive PDF Reports**: Automated email/downloadable PDF digests for financial reviews.
- **Module 13 — Multi-user & Team Features**: Advanced role-based access controls (RBAC) and tenancy isolation.
- **Module 14 — OCI API Integration**: Direct link to live OCI Usage APIs and SDK configuration.
- **Module 15 — Deployment & Production**: Fully automated cloud deployment, production configuration, and scale-out setups.
