# Telegram Hosting Bot — Railway + PostgreSQL

This is a Telegram bot service, not a browser-based dashboard. It runs Telegram long polling and a `/health` endpoint on Railway's automatically supplied `PORT`. When Railway provides `DATABASE_URL`, the live panel database uses PostgreSQL; SQLite is retained for portable backup/import compatibility and for local runs without `DATABASE_URL`.

The first-use CAPTCHA gate has been removed: `/start` opens the panel directly. The separate optional force-join channel check remains available in the admin settings.

## Deploy from GitHub

1. Extract this ZIP and push the **contents** of `Telegram-Hosting-Bot-Railway` to a GitHub repository. Keep the repository private because the bot can manage uploaded code and configuration.
2. In Railway, create a project and deploy that repository. Railway detects the included `Dockerfile`; you do not need to set a custom build or start command.
3. In the Railway project, select **Add → Database → PostgreSQL**. Railway provisions the Postgres service and its database storage.
4. In the `worker` service's Variables, add `DATABASE_URL` as a reference to the Postgres service's `DATABASE_URL`. If the database service is named `Postgres`, the reference is typically `${{Postgres.DATABASE_URL}}`; use Railway's variable-reference picker if the service has another name. This connection is what makes the worker use PostgreSQL.
5. Add a separate **Volume to the worker service** mounted at `/data`. This is for hosted bot files, uploads, settings, logs, and custom images. The PostgreSQL service's own volume stores the database; it does not persist the worker's files.
6. Add these worker Variables:
   - `BOT_TOKEN` — token from BotFather.
   - `OWNER_ID` — your numeric Telegram user ID.
   - `DATABASE_URL` — Railway reference to the PostgreSQL service.
   - `PANEL_DATA_DIR` — `/data` (matches the worker volume mount).
7. Keep the worker at one replica because it uses Telegram long polling. Deploy. Do not set `PORT` yourself—Railway supplies it. The included health check uses `/health`; Telegram polling does not require a public domain or webhook.

The service graph should have a `worker` connected to a `Postgres` service, with a Postgres volume on the database. For persistent hosted scripts and uploads, also attach the worker's `/data` volume.

## Optional variables

- `AUTO_START_HOSTED_BOTS=1` — relaunch saved user-hosted bots after a service restart. It defaults to `0`; enable only if wanted and the worker has enough resources.
- `ANNOUNCE_CHANNEL` — optional announcement channel.
- `GITHUB_TOKEN`, `GITHUB_REPO`, and `GITHUB_BRANCH` — optional GitHub backup configuration. Put tokens in Railway Variables, never in the repository.

## Data and migration

This clean package excludes the uploaded archive's Git history and prior database, logs, credentials/configuration state, hosted bot files, and sandbox. It starts with an empty PostgreSQL database. To migrate, export a full backup from the current bot through owner **Admin → Import/Export**, deploy the new service, then restore that backup through the new bot. The importer reads the backup's SQLite snapshot and writes the records into PostgreSQL. Full backups can contain bot credentials or session files; transfer and store them privately.

The stock menu images are included and copied to the worker volume on first start without replacing customized images already there. PostgreSQL stores the panel records; the `/data` volume stores files that PostgreSQL does not hold.

## Local checks

Install `requirements.txt`, then run `python -m unittest discover -s . -p 'test_*.py'`. Without `DATABASE_URL`, the bot uses local SQLite. For Railway, configure `DATABASE_URL` and `PANEL_DATA_DIR=/data` as described above.
