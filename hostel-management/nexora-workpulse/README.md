# NEXORA WORKPULSE — Team Work & Attendance Monitoring Portal

## What's new in v3 — Founders
* Three account types: **Admin**, **Team member**, **Founder** (Team → Add person).
* Founders see only two pages: **Project status** (phase journey, progress, milestones, team updates, *View live application*, *Request changes*) and **Agreement** (read and digitally sign with their typed name; time, IP and a fingerprint of the exact text are recorded).
* Admins control everything founders see under **Founders**: phase, health, progress %, status message, live link, milestones, updates, replies to change requests, and the agreement text. Editing the agreement creates a new version that the founder must sign again. *Preview founder page* shows exactly what they see.
* Migration `0003_founders_projects_agreements.sql` runs automatically on deploy.

## What's new in v2
* **Attendance starts on first sign-in.** Signing in starts the member's work timer, signing out stops it. Days before a member's first sign-in never count as absent.
* **Daily records** (admin): who logged in, when, hours, target reached or not, and the work updates posted that day. CSV export.
* **Welcome tour + skill questions** on a member's first sign-in (backend, database, frontend: Moderate / Good enough / High). The task form suggests the best-fit member for the chosen work type.
* **Daily progress** per task (progress %, what was done, pull request link) and a **task completion acknowledgement**. The daily acknowledgement form and the audit report are removed.
* **GitHub**: set the team repository under *GitHub & data*. Members get Open repo / New pull request buttons and paste PR links. Admins review PRs and merge on GitHub, or in-app when `GITHUB_TOKEN` is set.
* **Delete team members** (Team → Delete) and **Delete all WorkPulse data** (GitHub & data → Danger zone; keeps admin accounts). Also available as `flask --app app wipe-data`.
* Redesigned, animated, fully responsive UI (still no JavaScript).
* Migration `0002_workflow_github_onboarding.sql` runs automatically on deploy (adds columns/tables only).

Flask + Jinja2 + CSS (no JavaScript), SQLAlchemy, your existing **Supabase PostgreSQL**, deployed on **Render** with Gunicorn.

**Transparency by design:** every page shows *YOUR WORK IS OBSERVED*. The portal records only what members do inside it
(clock in/out, breaks, task updates, acknowledgements). There is no screenshot capture, keystroke logging or hidden tracking.
Recorded session time measures **attendance, not continuous productivity**; task progress and reviewed deliverables are the separate evidence of work.

## Contents
`app.py` (routes, CLI) · `models.py` · `migrations/0001_initial.sql` · `templates/` · `static/css/style.css` · `tests/` ·
`render.yaml` · `requirements.txt` · `.env.example` · `.gitignore` · `tools/gen_migration.py`

## 1. Configure Supabase PostgreSQL
1. In Supabase open your project → **Connect** (or *Project Settings → Database → Connection string*).
2. Copy the **Session pooler** string (host like `aws-0-<region>.pooler.supabase.com`, port `5432`). Render cannot use the IPv6-only *direct* connection.
3. Replace `[YOUR-PASSWORD]` with your database password. This full string is your `DATABASE_URL`.
4. Nothing else to create. The app adds only tables prefixed `nwp_` (`nwp_users`, `nwp_tasks`, `nwp_task_assignments`, `nwp_task_reviews`, `nwp_attendance_sessions`, `nwp_break_records`, `nwp_acknowledgements`, `nwp_audit_logs`, `nwp_schema_migrations`). Migrations only run `CREATE ... IF NOT EXISTS`; they never drop, alter or overwrite existing tables or data.

## 2. Environment variables
| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | Supabase connection string (placeholder in `.env.example`) |
| `FLASK_SECRET_KEY` | yes | 32+ random chars. `python -c "import secrets; print(secrets.token_urlsafe(48))"`. `render.yaml` auto-generates one on Render |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | first deploy | Creates the first admin if none exists (password min 12 chars). Optional `ADMIN_NAME` |
| `APP_TIMEZONE` | no | Default `Asia/Kolkata`; defines what “a day” is |
| `DAILY_TARGET_HOURS` | no | Default `4` |
| `SESSION_MAX_HOURS` | no | Default `16`; longer open sessions are capped and flagged *incomplete* |
| `DB_SSLMODE` | no | Default `require`; use `prefer` for local non-SSL Postgres |
| `GITHUB_TOKEN` | no | Enables in-app *Merge* of pull requests. Fine-grained token: Pull requests + Contents read/write |
| `GITHUB_REPO_URL` | no | Default team repo (can be set in the app instead) |
| `APP_ENV` | no | `production` (default; Secure cookies + HSTS) or `development` (plain http locally) |

The app **refuses to start with a clear error** if `DATABASE_URL` or `FLASK_SECRET_KEY` is missing or invalid.

## 3. Push to GitHub safely (do this BEFORE pushing)
```bash
cd nexora-workpulse
git init && git add .
git ls-files | grep -E '^\.env($|\.)' | grep -v '^\.env\.example$'   # must print NOTHING
git check-ignore -v .env                                              # must show the .gitignore rule
git status --short                                                    # review: no .env, no secrets
git commit -m "Initial commit" && git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```
Only `.env.example` (placeholders) is committed. Use a **private** repository if possible.

### If a secret was already committed
`.gitignore` does **not** untrack a file that is already committed or remove it from history.
1. **Rotate first (assume it is compromised):** Supabase → *Project Settings → Database → Reset database password*, update `DATABASE_URL` in Render; generate a new `FLASK_SECRET_KEY` (this signs everyone out); change `ADMIN_PASSWORD`/any exposed key.
2. Stop tracking the file: `git rm --cached .env && git commit -m "Stop tracking .env"`
3. Purge it from history (e.g. with [git-filter-repo](https://github.com/newren/git-filter-repo)): `git filter-repo --invert-paths --path .env`, then re-add the remote and `git push --force --all`. (BFG Repo-Cleaner also works.)
4. Tell collaborators to re-clone. Forks, clones and caches may still hold the old value, which is why **rotation (step 1) is the real fix**.

## 4. Deploy on Render
1. Render Dashboard → **New → Blueprint** → connect GitHub → pick the repo (it reads `render.yaml`). *(Or **New → Web Service**: build `pip install -r requirements.txt`, start `flask --app app init-db && gunicorn "app:create_app()" --workers 2 --threads 4 --timeout 60 --bind 0.0.0.0:$PORT`, health check `/health`.)*
2. When prompted, enter `DATABASE_URL`, `ADMIN_EMAIL`, `ADMIN_PASSWORD` in Render’s **Environment** settings (they are `sync: false`, so never stored in Git). `FLASK_SECRET_KEY` is generated by Render.
3. Deploy. On each start, `init-db` applies any new migrations and, only if no admin exists, creates the admin from `ADMIN_EMAIL`/`ADMIN_PASSWORD`. It never overwrites an account.
4. After the first successful sign-in, change the admin password in the app (**Password**) and delete `ADMIN_PASSWORD` from Render’s Environment.
5. Notes: login rate limiting uses in-memory storage per Gunicorn worker (2 workers → the effective limit is roughly double). The Render free plan sleeps when idle; the first request after sleeping is slow.

## 5. Create the administrator and team accounts
* **Administrator:** automatic on first deploy (step 4.3). Manually: set `ADMIN_EMAIL`/`ADMIN_PASSWORD` and run `flask --app app create-admin` (Render Shell on paid plans, or locally with your `.env`). Public registration does not exist.
* **Team members:** sign in as admin → **Team → Create team member** (name, email, initial password ≥ 12 chars). Share the password securely. Admins can disable accounts (which signs the user out immediately) or reset passwords from **Team**.

## 6. Run locally (explore first, connect Supabase later)
Needs Python 3.11+ (python.org; on Windows tick "Add Python to PATH").

**Windows:** double-click **`start_local.bat`**. **Mac/Linux:** `bash start_local.sh`. Then open **http://127.0.0.1:5000**.

**Demo mode (default, no setup):** with no `.env` file the app uses a local file database (`workpulse_demo.db`) and prints these logins:
* Admin: `admin@example.com` / `Admin-Demo-2026!`
* Members: `mia@example.com` and `raj@example.com` / `Member-Demo-2026!`
* Founder: `farah@example.com` / `Member-Demo-2026!`

Use two different browsers (or one normal and one private window) to be admin and member at the same time. Delete `workpulse_demo.db` to reset the demo. Demo mode only runs through `python app.py` on your own computer; Render always requires real settings.

**Real Supabase database:** run `python make_env.py` once (asks for your Session pooler URL, admin email and password, and writes `.env` with a generated secret key). The next `python app.py` then connects to Supabase, creates the `nwp_` tables if missing and the first admin if none exists. Special characters in the password are handled automatically. Existing tables are never touched. To go back to demo mode, rename or delete `.env`.

If something is wrong the app prints `CONFIGURATION ERROR` or `DATABASE ERROR` with the fix (placeholder text left in the URL, wrong password, not the Session pooler string, port busy: add `PORT=5001` to `.env`).

## 7. Testing
Automated (SQLite in-memory, no database credentials needed): `python -m pytest -q`
Covers config failures, login/disabled accounts, CSRF, rate limiting, role restrictions, attendance maths (9:00–13:30 with a 30-minute break = 4h), duplicate-session prevention, stale-session handling, task create/assign/reassign, submissions, reviews, acknowledgements, reports/CSV and rendering of every page.

Manual checklist (as admin, then as a member in another browser):
1. Admin: create two members → each can sign in; a wrong password shows an error.
2. Admin: **POST A TASK** to one member, then to the entire team; edit and reassign.
3. Member: clock in → try clock in again (blocked) → start/end break → clock out; check hours and the four-hour ring.
4. Member: update progress, add notes and a link, **Submit for review**.
5. Admin: open the task → **Request changes** (feedback required) → member resubmits → **Approve**.
6. Member: submit the daily acknowledgement; admin sees it under **Acknowledgements**.
7. Admin: **Reports** → filter dates/employee → **Export CSV** (opens in Excel).
8. Member visits an `/admin/...` URL → *Access denied*. `GET /health` returns `{"status":"ok"}`.

## 8. Design notes and trade-offs
* **No JavaScript** is needed. Mobile menu, progress rings and bars are CSS only. The CSP blocks all scripts. Exports are CSV (opens in Excel) so no extra library is required.
* A session counts toward the local day on which it **started**. Sessions open longer than `SESSION_MAX_HOURS` are capped and flagged incomplete.
* Task and attendance targets are independent: four hours of attendance never completes a task.
* New schema changes: add a new numbered `.sql` file in `migrations/` (never edit an applied one); `init-db` applies it once.
