# All Bills Tracker

All Bills Tracker is a Flask-based document and bill management app for storing
PDF or image bills, drafting details with OCR, editing records manually, and
tracking everything from a library and dashboard.

## What the app does

- stores uploaded PDF and image bills
- supports manual entry even without file upload
- drafts fields from text-based PDFs
- runs local OCR for scanned PDFs and images
- lets you review and edit every saved record
- supports multiple bill or document types
- includes a filterable Library page
- includes a Dashboard page with charts and summaries

## Supported document types

The app is designed to handle mixed household and office billing records such
as:

- electricity
- water
- gas
- internet
- mobile
- rent
- insurance
- school fee
- maintenance
- loan
- credit card
- other

## Main pages

- `Workspace`: create a new record, import and draft, or save manually
- `Library`: search, filter, open, and delete saved records
- `Dashboard`: review totals, type mix, due items, and yearly or monthly trends

## Data storage

The app supports two storage modes:

- local SQLite for simple non-Docker development
- PostgreSQL when running through Docker

Uploaded files are stored separately from database records.

## Local development

Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Run the app:

```bash
.venv/bin/python app.py
```

Open:

```text
http://127.0.0.1:5000
```

By default, local development uses SQLite at:

```text
instance/bill_tracker.db
```

## Local PostgreSQL without Docker

If you want to run the Flask app directly but store records in PostgreSQL, set:

```bash
export DATABASE_URL="postgresql://bill_user:bill_password@localhost:5432/bill_tracker"
```

Then start the app normally:

```bash
.venv/bin/python app.py
```

## Docker setup

Start the app:

```bash
docker compose -f docker/docker-compose.yml up --build
```

Run in detached mode:

```bash
docker compose -f docker/docker-compose.yml up --build -d
```

Stop the stack:

```bash
docker compose -f docker/docker-compose.yml down
```

View status:

```bash
docker compose -f docker/docker-compose.yml ps
```

View logs:

```bash
docker compose -f docker/docker-compose.yml logs -f app
docker compose -f docker/docker-compose.yml logs -f db
```

Open:

```text
http://127.0.0.1:5000
```

## Docker services

The Docker setup includes:

- `app`: Flask app with OCR support
- `db`: PostgreSQL 16

Persistent Docker volumes:

- `postgres_data`
- `uploads_data`
- `app_instance`

## Important folders

- `uploads/`: uploaded source files
- `instance/ocr_cache/`: OCR cache for repeated imports
- `docker/`: Dockerfile and compose setup
- `templates/`: Jinja templates
- `static/`: CSS assets

## OCR and extraction notes

- text-based PDFs are imported quickly
- scanned PDFs or image uploads use OCR
- OCR quality depends on scan quality, lighting, skew, and print clarity
- drafted fields should always be reviewed before saving

## Library actions

From the Library page you can:

- search records
- filter by bill type
- filter by review status
- open a saved record
- open the original source file if available
- delete a record

If a deleted record is the last one using a stored uploaded file, that file is
also removed from disk.

## Migration

To move the app to another server with its data, use:

- [MIGRATION.md](/home/opc/current_bill_tracker/MIGRATION.md)

Automation scripts are also available:

- `./backup.sh`
- `./restore.sh`

That guide covers:

- backup
- transfer
- restore
- verification
- rollback

## Production checklist

Before exposing the Docker app publicly, update:

- PostgreSQL password
- `SECRET_KEY`
- host firewall or cloud security rules
- reverse proxy and TLS if needed

## Repository

GitHub repository:

```text
https://github.com/NithinSunke/current_bill_tracker
```
