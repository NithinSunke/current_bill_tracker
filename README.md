# Current Bill Tracker

Bill-tracking app for uploading electricity bills as PDF or image files, reviewing extracted details, and saving everything inside the app.

## What this version does

- Uploads a bill file to the local `uploads/` folder
- Saves bill metadata in a database configured by `DATABASE_URL`
- Lets you review and edit saved bills
- Imports text-based PDFs and drafts common bill fields automatically
- Runs local OCR for scanned PDFs and image uploads
- Accepts pasted OCR text or notes and tries to prefill common bill fields
- Includes a dashboard page and a bills table view

## Local setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Then open `http://127.0.0.1:5000`.

By default, local development uses SQLite at `instance/bill_tracker.db`.

To use PostgreSQL locally without Docker, set:

```bash
export DATABASE_URL="postgresql://bill_user:bill_password@localhost:5432/bill_tracker"
```

## Docker setup

```bash
docker compose -f docker/docker-compose.yml up --build
```

Then open:

```text
http://127.0.0.1:5000
```

The Docker setup includes:

- `app`: Flask app with OCR support
- `db`: PostgreSQL 16
- persistent Docker volumes for PostgreSQL data, uploads, and OCR cache

## Migration

To move the app to another server with data, use the documented backup and
restore steps in:

- `MIGRATION.md`

## Notes

- OCR quality depends on scan clarity and image quality, so review the drafted fields before saving.
- Uploaded files are stored in `uploads/`.
- OCR cache is stored in `instance/ocr_cache/`.
- In Docker, bill metadata is stored in PostgreSQL instead of SQLite.
