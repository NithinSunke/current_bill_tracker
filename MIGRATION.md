# Migration Guide

This guide explains how to move the Current Bill Tracker app to another server
with its data.

## What needs to move

For this Docker setup, there are three kinds of data:

- PostgreSQL bill records
- uploaded bill files stored in `/app/uploads`
- app instance data stored in `/app/instance`

You also need the application code itself.

## Current Docker setup

The app currently uses these Docker volumes:

- `postgres_data`
- `uploads_data`
- `app_instance`

## Recommended migration approach

Use logical database export plus file archives:

1. copy the project folder to the new server
2. export PostgreSQL data from the old server
3. archive uploaded files and app instance files
4. start the app on the new server
5. restore database and files
6. verify the app

This approach is portable and safer than copying raw database internals between
different Docker hosts.

## Before you start

On the old server, confirm the app is healthy:

```bash
docker compose -f docker/docker-compose.yml ps
```

Create a folder for migration artifacts:

```bash
mkdir -p migration
```

## Step 1: Back up the database

Run from the project directory:

```bash
docker compose -f docker/docker-compose.yml exec -T db \
  pg_dump --data-only --inserts -U bill_user -d bill_tracker \
  > migration/bill_tracker_data.sql
```

If you want schema plus data in one file, use this instead:

```bash
docker compose -f docker/docker-compose.yml exec -T db \
  pg_dump -U bill_user -d bill_tracker \
  > migration/bill_tracker_full.sql
```

## Step 2: Back up uploaded files

```bash
docker compose -f docker/docker-compose.yml exec -T app \
  sh -lc 'cd /app/uploads && tar czf - .' \
  > migration/uploads.tar.gz
```

## Step 3: Back up app instance data

This may include OCR cache and other app-side state:

```bash
docker compose -f docker/docker-compose.yml exec -T app \
  sh -lc 'cd /app/instance && tar czf - .' \
  > migration/app_instance.tar.gz
```

## Step 4: Copy the project to the new server

Example with `rsync`:

```bash
rsync -av /home/opc/current_bill_tracker/ user@NEW_SERVER:/home/opc/current_bill_tracker/
```

If you already copied the project separately, make sure at least these files are
present on the new server:

- `docker/docker-compose.yml`
- `docker/Dockerfile`
- `requirements.txt`
- `app.py`
- `bill_tracker/`
- `templates/`
- `static/`
- `migration/bill_tracker_data.sql`
- `migration/uploads.tar.gz`
- `migration/app_instance.tar.gz`

## Step 5: Prepare the new server

Install Docker and Docker Compose first.

Then from the project directory on the new server:

```bash
docker compose -f docker/docker-compose.yml up -d --build
```

Wait for the database to become healthy:

```bash
docker compose -f docker/docker-compose.yml ps
```

## Step 6: Restore uploaded files

```bash
cat migration/uploads.tar.gz | docker compose -f docker/docker-compose.yml exec -T app \
  sh -lc 'cd /app/uploads && tar xzf -'
```

## Step 7: Restore app instance data

```bash
cat migration/app_instance.tar.gz | docker compose -f docker/docker-compose.yml exec -T app \
  sh -lc 'cd /app/instance && tar xzf -'
```

## Step 8: Restore the database

If you created the data-only backup:

```bash
cat migration/bill_tracker_data.sql | docker compose -f docker/docker-compose.yml exec -T db \
  psql -U bill_user -d bill_tracker
```

If you created the full backup instead:

```bash
cat migration/bill_tracker_full.sql | docker compose -f docker/docker-compose.yml exec -T db \
  psql -U bill_user -d bill_tracker
```

## Step 9: Restart the app

```bash
docker compose -f docker/docker-compose.yml restart app
```

## Step 10: Verify migration

Check containers:

```bash
docker compose -f docker/docker-compose.yml ps
```

Check logs:

```bash
docker compose -f docker/docker-compose.yml logs --tail=100 app
docker compose -f docker/docker-compose.yml logs --tail=100 db
```

Open the app in a browser:

```text
http://SERVER_IP:5000
```

Verify:

- bills table shows existing records
- uploaded bill links open correctly
- dashboard totals look correct
- OCR imports still work

## Network and firewall notes

If the new server should be reachable from another machine:

- allow inbound port `5000`
- open the same port in cloud security lists or firewall rules

If you want to serve the app on a different port, update:

- `docker/docker-compose.yml`

Example:

```yaml
ports:
  - "8080:5000"
```

Then the app will be available at:

```text
http://SERVER_IP:8080
```

## Recommended production changes after migration

Before exposing the app publicly, update:

- PostgreSQL password
- Flask `SECRET_KEY`
- reverse proxy and TLS if needed

## Quick rollback plan

If something looks wrong on the new server:

1. stop the new containers
2. keep the old server running
3. inspect restore logs
4. restore again from the same migration artifacts

## Optional: volume-level backup instead of logical export

You can also back up Docker volumes directly, but logical PostgreSQL export is
the better default for portability.
