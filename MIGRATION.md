# Migration Guide

This guide explains how to migrate **All Bills Tracker** to another server with
its application data.

## What must be migrated

This app stores data in three places when running with Docker:

- PostgreSQL bill and document records
- uploaded files in `/app/uploads`
- app instance data in `/app/instance`

You also need the application code itself.

## Current Docker setup

The Docker stack uses:

- `app`: Flask app
- `db`: PostgreSQL 16

Named Docker volumes:

- `postgres_data`
- `uploads_data`
- `app_instance`

The app port is currently:

```text
5000
```

## Recommended migration method

Use:

1. logical PostgreSQL export
2. file archive backup for uploads and instance data
3. project folder copy

This is safer and more portable than copying raw Docker volume internals
between hosts.

## Migration overview

On the old server:

1. verify the containers are healthy
2. create backups
3. copy the project and backup files to the new server

On the new server:

1. start the Docker stack
2. restore uploads
3. restore app instance data
4. restore the database
5. verify the app

## Before starting

Run from the project directory:

```bash
cd /home/opc/current_bill_tracker
```

Check the running containers:

```bash
docker compose -f docker/docker-compose.yml ps
```

Create a migration folder if it does not exist:

```bash
mkdir -p migration
```

## Quick automated option

Instead of running each backup or restore command manually, you can use the
provided scripts from the project root:

### Create backups

Data-only backup:

```bash
./backup.sh
```

Full schema plus data backup:

```bash
./backup.sh --full
```

### Restore backups

Auto-detect backup file:

```bash
./restore.sh
```

Force data-only restore:

```bash
./restore.sh --data-only
```

Force full backup restore:

```bash
./restore.sh --full
```

## Step 1: Back up PostgreSQL

### Option A: data-only backup

Recommended if the destination app already has the same schema version.

```bash
docker compose -f docker/docker-compose.yml exec -T db \
  pg_dump --data-only --inserts -U bill_user -d bill_tracker \
  > migration/bill_tracker_data.sql
```

### Option B: full schema plus data backup

Useful if you want one single SQL file containing both schema and data.

```bash
docker compose -f docker/docker-compose.yml exec -T db \
  pg_dump -U bill_user -d bill_tracker \
  > migration/bill_tracker_full.sql
```

## Step 2: Back up uploaded source files

```bash
docker compose -f docker/docker-compose.yml exec -T app \
  sh -lc 'cd /app/uploads && tar czf - .' \
  > migration/uploads.tar.gz
```

## Step 3: Back up app instance data

This usually contains OCR cache and other local runtime state.

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

At minimum, make sure the new server receives:

- application code
- `docker/`
- `requirements.txt`
- `migration/bill_tracker_data.sql` or `migration/bill_tracker_full.sql`
- `migration/uploads.tar.gz`
- `migration/app_instance.tar.gz`

## Step 5: Prepare the new server

Install Docker and Docker Compose on the new server first.

Then go to the project directory:

```bash
cd /home/opc/current_bill_tracker
```

Start the stack:

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

## Step 8: Restore PostgreSQL

### If you created the data-only backup

```bash
cat migration/bill_tracker_data.sql | docker compose -f docker/docker-compose.yml exec -T db \
  psql -U bill_user -d bill_tracker
```

### If you created the full backup

```bash
cat migration/bill_tracker_full.sql | docker compose -f docker/docker-compose.yml exec -T db \
  psql -U bill_user -d bill_tracker
```

## Step 9: Restart the app container

```bash
docker compose -f docker/docker-compose.yml restart app
```

## Step 10: Verify the migration

Check container status:

```bash
docker compose -f docker/docker-compose.yml ps
```

Check logs:

```bash
docker compose -f docker/docker-compose.yml logs --tail=100 app
docker compose -f docker/docker-compose.yml logs --tail=100 db
```

Open the app:

```text
http://SERVER_IP:5000
```

Verify the following:

- the Workspace page opens
- the Library page shows existing records
- uploaded file source links work
- the Dashboard shows totals and charts
- OCR import still works
- manual records are still editable

## Firewall and network checks

If the new server must be accessible from another machine:

- allow inbound TCP port `5000`
- update cloud security lists, security groups, or firewall rules

If you want a different external port, edit:

- [docker/docker-compose.yml](/home/opc/current_bill_tracker/docker/docker-compose.yml)

Example:

```yaml
ports:
  - "8080:5000"
```

Then open:

```text
http://SERVER_IP:8080
```

## Recommended production changes after migration

Before using the new server publicly, update:

- `POSTGRES_PASSWORD`
- `SECRET_KEY`
- reverse proxy configuration if needed
- TLS or HTTPS setup if needed

## Rollback plan

If something looks wrong after restore:

1. stop the new stack
2. keep the old server running
3. inspect `app` and `db` logs on the new server
4. restore again from the same migration artifacts

## Optional alternative: volume-level backups

You can back up Docker volumes directly, but this guide prefers logical SQL
export plus file archives because it is easier to move safely between servers
and Docker hosts.
