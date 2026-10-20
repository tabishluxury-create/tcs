# Student Collective deployment

This folder is a standalone Flask app for deploying the Student Collective site to Render.

## Deploy with Render

1. Put this folder in a Git repository and push it to a Git provider connected to Render. Do not include `.env`, database files, or real credentials.
2. In Render, create a Blueprint and select this folder's `render.yaml`.
3. Set `TSC_ADMIN_PASSWORD` and `TSC_MASTER_PASSWORD` privately in Render. Both must contain at least 5 characters. The master password continues to work after the admin password changes, so protect it and do not share it.
4. Render creates the web service, a persistent disk for the SQLite admin account, and a private Key Value service for rate limiting. The web service uses one worker because SQLite and the attached disk are single-instance storage.
5. Wait for `/healthz` to report healthy, then visit `/admin/login` on the deployed site to sign in.

The Blueprint uses a paid Render web-service plan because persistent disks are only available for paid web services. Review Render's current pricing before creating services.

## Local development

Create a virtual environment, install `requirements-tsc-admin.txt`, copy `.env.example` to `.env`, and replace every placeholder with private local values. Export those values into the shell before starting the app:

```sh
set -a
source .env
set +a
python tsc_server.py
```

The local server listens on `127.0.0.1:5000`. `TSC_COOKIE_SECURE=false` is for local HTTP only; the Render configuration forces secure cookies.

## What is persisted

The Flask server persists the admin account's password hash and submitted member registrations in SQLite on the Render disk. The member form submits to the Flask backend; registration data is validated, protected by CSRF validation, and rate-limited. Only signed-in administrators can view registrations in the dashboard or export them as a CSV. Admin authentication and password changes are also server-backed, protected by CSRF validation, and rate-limited using the private Key Value service.

The gallery images, website notice, and social-link settings are still stored in each visitor's browser using IndexedDB. They are not shared between visitors or devices and are not backed up by the server. A shared production content system would require a separate migration to server-side storage and is not included in this deployment setup.

## Important environment variables

Render supplies `TSC_SESSION_SECRET` automatically. Set both passwords privately in the Render dashboard. Sign-in accepts the stored admin password or the master password. Do not commit real secrets or the local `.env` file. The admin password is hashed in the database; changing `TSC_ADMIN_PASSWORD` does not reset an account already created on the persistent disk. The master password remains a valid sign-in credential until `TSC_MASTER_PASSWORD` is changed.

- `TSC_AUTH_DATABASE`: SQLite database path; Render sets this under the persistent disk mount.
- `TSC_RATE_LIMIT_STORAGE_URI`: private Key Value connection URL; Render supplies this from the Blueprint.
- `TSC_COOKIE_SECURE`: must remain `true` in production.
- `TSC_ENV`: must remain `production` on Render.
