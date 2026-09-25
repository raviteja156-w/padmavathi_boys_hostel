# 🏠 Hostel Management System

A complete, production-ready **Flask** web application for managing hostel residents across an **Old Hostel** and a **New Hostel**, each with 10 rooms and 1–6 sharing. Built with Flask, SQLite, Tailwind CSS, vanilla JavaScript, ReportLab (PDF) and openpyxl (Excel).

---

## 🔐 Access Model

- **Public / user-facing site** — the home page (`/`) *is* the "Add Student" form. That's the only thing a regular visitor sees or can do. There is **no visible link, button, or menu item anywhere on the public pages that points to the admin area.**
- **Admin area** — only reachable by typing the URL directly into the browser's address bar: `/adminlogin`. Once logged in, the nav bar shows "Admin" / "Logout" links for convenience during that session only.

## ✨ Features

- **Add Student** (home page) — mobile-first form with live balance calculation, Indian mobile number validation, and JPG photo upload (auto-compressed).
- **Automatic room placement** — students are instantly placed under the correct Hostel → Room based on the form selection.
- **Room capacity enforcement** — prevents saving a student into a room that has reached its sharing capacity.
- **Admin dashboard** — login-protected, with summary cards (total students, old/new hostel counts, total paid, balance due).
- **Room-wise view** — Old Hostel and New Hostel, each always showing Room 1 → Room 10 in order, including empty rooms.
- **Instant search & filters** — search by name, contact or room number; filter by hostel and room.
- **Edit / Delete students** — with automatic room re-placement on edit and a confirmation modal before delete.
- **Excel export/import** — export to a multi-sheet workbook (Old Hostel / New Hostel / Summary); import students from a spreadsheet with validation and a success/skip report.
- **PDF reports** — Old Hostel PDF, New Hostel PDF, and a Complete Hostel PDF, all preserving room order (1–10) with summary totals, page numbers and a generation date.
- **Mobile-responsive** — works cleanly from 360px phones up to large desktop monitors.
- **Security** — environment-based secrets, session-based admin auth, CSRF tokens on all state-changing forms, parameterized SQL, safe filenames, file-type/size validation.

---

## 🗂 Project Structure

```
hostel-management/
├── app.py               # Main Flask application
├── requirements.txt
├── Procfile              # gunicorn app:app
├── runtime.txt
├── .env.example
├── README.md
├── templates/
├── static/
│   ├── css/style.css
│   ├── js/app.js, admin.js
│   └── uploads/          # uploaded student photos
└── data/
    └── hostel.db         # SQLite database (auto-created on first run)
```

The SQLite database and its table are **created automatically** the first time the app starts — no manual migration step is required.

---

## 💻 Local Setup

1. **Create and activate a virtual environment**

   ```bash
   python -m venv venv
   source venv/bin/activate      # on Windows: venv\Scripts\activate
   ```

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables**

   Copy `.env.example` to `.env` and fill in real values:

   ```bash
   cp .env.example .env
   ```

   ```
   SECRET_KEY=your-random-secret-key
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=your-secure-password
   DATABASE_PATH=data/hostel.db
   ```

4. **Run the app**

   ```bash
   python app.py
   ```

   Visit `http://localhost:5000` — this is the "Add Student" form, and it's all a regular visitor ever sees. The database starts **completely empty** — no fake/demo students are seeded.

   To manage students, go directly to `http://localhost:5000/adminlogin` and log in with the credentials from your `.env`. This page is intentionally not linked from anywhere in the public UI.

---

## ☁️ Render Deployment

1. **Create a GitHub repository** and push this project to it (make sure `.env` is *not* committed — it's already in `.gitignore`).

2. **Create a new Web Service on Render**
   - Go to [render.com](https://render.com) → New → Web Service.
   - Connect your GitHub repository.

3. **Build command**

   ```
   pip install -r requirements.txt
   ```

4. **Start command**

   ```
   gunicorn app:app
   ```

   (This is also declared in the included `Procfile`.)

5. **Environment variables** — in the Render dashboard, under "Environment", add:

   | Key              | Value                                   |
   |------------------|------------------------------------------|
   | `SECRET_KEY`     | a long random string                     |
   | `ADMIN_USERNAME` | your chosen admin username                |
   | `ADMIN_PASSWORD` | a strong password                         |
   | `DATABASE_PATH`  | `/var/data/hostel.db` (see disk note below) |

6. **Configure a persistent disk (important, see below)**
   - In the Render service settings, add a **Disk** (e.g. mount path `/var/data`, size 1 GB is plenty to start).
   - Set `DATABASE_PATH=/var/data/hostel.db` so the SQLite file lives on that disk.
   - Consider also pointing photo uploads at the same persistent disk if you need uploaded photos to survive redeploys (see "Upload storage" below).

7. Deploy. On first boot, the app automatically creates the SQLite database and the `students` table at the configured `DATABASE_PATH`.

### ⚠️ Why a persistent disk matters

Render's default web service filesystem is **ephemeral** — anything written to local disk (including a SQLite `.db` file created in the project folder) is **wiped on every redeploy or restart**. If you don't attach a persistent disk and point `DATABASE_PATH` at it, your student records will disappear the next time you deploy a change.

**Recommended setup:** attach a Render persistent disk, mount it (e.g. at `/var/data`), and set:

```
DATABASE_PATH=/var/data/hostel.db
```

### 📷 Upload storage limitation

Uploaded student photos are saved under `static/uploads/`, which is also on the ephemeral filesystem by default. For photos to survive redeploys:

- Mount your persistent disk so it also covers the uploads folder (e.g. symlink or configure `UPLOAD_FOLDER` to a path under the same persistent disk), **or**
- For larger deployments, move to an external object store (e.g. S3-compatible storage) — this app's `save_photo()` / `uploaded_file()` functions in `app.py` are the two places you'd adapt for that.

Without either step, photos will still work fine while the service is running, but may be lost on a redeploy — only the SQLite data path is guaranteed to be safe once configured with a persistent disk.

---

## 🔑 Required Environment Variables

```
SECRET_KEY=
ADMIN_USERNAME=
ADMIN_PASSWORD=
DATABASE_PATH=
```

---

## 🧪 What Was Tested

- Adding students to Old Hostel and New Hostel, Room 1 through Room 10, Sharing 1 through Sharing 6, including rent/balance calculation and JPG upload.
- Room capacity enforcement (rejecting a save once a room's sharing capacity is reached).
- Admin login, dashboard totals, search, hostel/room filters, edit (including moving a student to a different room), delete with confirmation.
- All 10 rooms displaying in numeric order in both hostels, including empty rooms.
- Excel export (3 sheets) and import (valid rows inserted, invalid rows skipped and reported).
- PDF generation for Old Hostel, New Hostel, and the Complete report, preserving room order with page numbers and summaries.
- Responsive layout at 360px, 390px, 430px, 768px, 1024px and 1440px viewport widths.

---

## 🔒 Security Notes

- Admin credentials and the Flask secret key are read from environment variables only — never hardcoded.
- All forms include a CSRF token validated server-side.
- All SQL queries use parameterized statements.
- Uploaded files are validated by extension and by actually opening them as images (Pillow); filenames are randomized with `uuid4` before saving.
- Session cookies are signed using `SECRET_KEY`; admin routes are protected by a `login_required` decorator.

---

## 📄 License

Built for internal hostel administration use. Adapt freely.
