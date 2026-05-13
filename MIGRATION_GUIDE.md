# Readwise → Full-Stack Vercel Upgrade Guide

A precise, end-to-end reference for migrating your single-file Readwise app to a
Python-backed, Vercel-hosted full-stack application.

---

## 1 · Data Schema

Your existing `books` array already stores `totalPages` and `currentPage`.
The server mirrors this with a canonical **Book record** — `progress_percentage`
is always *calculated*, never stored raw, to avoid drift between client and server.

```json
{
  "id":                  "b1715000000001234",
  "display_name":        "Deep Work",
  "total_pages":         304,
  "current_page":        47,
  "progress_percentage": 15.46,
  "status":              "reading",
  "updated_at":          1715012345678
}
```

| Field                  | Type    | Source         | Notes                                      |
|------------------------|---------|----------------|--------------------------------------------|
| `id`                   | string  | JS (`b`+epoch) | Matches your existing `b.id` scheme        |
| `display_name`         | string  | JS             | Sent on every POST, stored for GET queries |
| `total_pages`          | int     | pdf.js         | `pdfDoc.numPages`                          |
| `current_page`         | int     | JS             | `pageNum` after each `renderPage()`        |
| `progress_percentage`  | float   | **Server**     | `round((current / total) * 100, 2)`        |
| `status`               | string  | JS             | `"pending"` \| `"reading"` \| `"completed"`|
| `updated_at`           | int ms  | Server         | `time.time() * 1000` at write time         |

---

## 2 · Project Structure

```
readwise/
├── api/
│   └── progress.py          ← Python serverless function (POST + GET)
├── public/
│   └── index.html           ← Your existing HTML (moved here)
├── vercel.json              ← Build + routing config
├── requirements.txt         ← Flask + your storage driver
├── .gitignore
└── MIGRATION_GUIDE.md       ← This file
```

Vercel's routing rules in `vercel.json`:

```
/api/*      →  api/*.py       (Python serverless)
/*          →  public/*       (static files)
```

This means `/api/progress` hits `api/progress.py` automatically —
**no Express, no Node proxy, no manual routing**.

---

## 3 · Backend API Reference (`api/progress.py`)

### `POST /api/progress`

Update reading progress. Called automatically by the frontend on every page flip.

**Request body:**

```json
{
  "book_id":      "b1715000000001234",
  "current_page": 47,
  "total_pages":  304,
  "display_name": "Deep Work",
  "status":       "reading"
}
```

**Success response `200`:**

```json
{
  "ok":               true,
  "book": {
    "id":                  "b1715000000001234",
    "display_name":        "Deep Work",
    "total_pages":         304,
    "current_page":        47,
    "progress_percentage": 15.46,
    "status":              "reading",
    "updated_at":          1715012345678
  },
  "storage_backend": "vercel-kv"
}
```

**Error response `400`:**

```json
{
  "ok":     false,
  "errors": ["current_page cannot exceed total_pages"]
}
```

---

### `GET /api/progress/<book_id>`

Fetch a book's persisted progress (useful on page load to reconcile server state).

**Success `200`** — same `{ "ok": true, "book": {...} }` shape as above.  
**Not found `404`** — `{ "ok": false, "error": "Book not found" }`.

---

## 4 · Frontend Changes

Two functions were modified in `public/index.html`.

### 4a · `changePage` (was 1 line → now calls `syncProgress`)

**Before:**
```js
function changePage(d) {
  if (!pdfDoc) return;
  const n = pageNum + d;
  if (n < 1 || n > pdfDoc.numPages) return;
  renderPage(n);
}
```

**After (in your new `public/index.html`):**
```js
function changePage(d) {
  if (!pdfDoc) return;
  const n = pageNum + d;
  if (n < 1 || n > pdfDoc.numPages) return;
  renderPage(n);
  if (readerBookId) syncProgress(readerBookId, n, pdfDoc.numPages);
}
```

`syncProgress` is debounced (600 ms) so rapid arrow-key flipping doesn't spam
the API. On success, it reconciles `b.currentPage` with the server's response and
live-updates the library card's progress bar — no full re-render needed.

---

### 4b · Progress bar in Library View

The `bookCard()` function already renders `.progress-bar` and `.book-progress-text`.
The new code in `syncProgress` targets these via the new `data-book-id` attribute:

```js
const cardBar = document.querySelector(`[data-book-id="${bookId}"] .progress-bar`);
const cardPct = document.querySelector(`[data-book-id="${bookId}"] .book-progress-text`);
if (cardBar) cardBar.style.width = book.progress_percentage + '%';
if (cardPct) cardPct.textContent = `p.${book.current_page} / ${book.total_pages} · ${book.progress_percentage}%`;
```

The bar width uses the server-authoritative `progress_percentage`, so the UI
always reflects the persisted value — not a locally-computed estimate.

---

## 5 · Storage Setup

### Option A — Vercel KV (recommended)

1. In Vercel dashboard → **Storage** → **Create KV Database**
2. Link it to your project — Vercel auto-injects `KV_REST_API_URL` and
   `KV_REST_API_TOKEN` as environment variables.
3. Leave `requirements.txt` as-is (`upstash-redis` is already there).

Keys are namespaced as `readwise:book:<book_id>` so you can safely share
one KV store across multiple projects.

### Option B — Vercel Postgres

1. **Storage** → **Create Postgres Database**, link to project.
2. Run this DDL once (use the Vercel SQL editor or `psql`):
   ```sql
   CREATE TABLE reading_progress (
     book_id    TEXT PRIMARY KEY,
     data       JSONB NOT NULL,
     updated_at TIMESTAMPTZ DEFAULT NOW()
   );
   ```
3. Uncomment the Postgres block in `api/progress.py` and comment out the
   Upstash block. Swap `requirements.txt` accordingly:
   ```
   psycopg2-binary>=2.9.0
   ```

---

## 6 · Git Commit Strategy

Separate the frontend UI work from backend logic so your history is bisectable
and PR reviews are scoped correctly.

```bash
# ── One-time setup ────────────────────────────────────────────────────────────
git init
echo "node_modules/\n__pycache__/\n*.pyc\n.env\n.vercel" > .gitignore
git add .gitignore
git commit -m "chore: init repo with .gitignore"

# ── Commit 1: Python backend ──────────────────────────────────────────────────
git add api/progress.py requirements.txt vercel.json
git commit -m "feat(backend): add Python progress API + Vercel config

- POST /api/progress  — upserts reading progress, returns progress_percentage
- GET  /api/progress/<id> — fetches stored progress for a book
- Storage: Vercel KV (upstash-redis) with in-memory fallback for dev
- vercel.json routes /api/* → Python, /* → public/ static
"

# ── Commit 2: Frontend integration ───────────────────────────────────────────
git add public/index.html
git commit -m "feat(frontend): sync reading progress to backend API

- changePage() now calls syncProgress() after every page flip
- syncProgress() is debounced 600 ms to batch rapid key-presses
- On API success, live-updates library card progress bar (no re-render)
- seekPage() (progress bar click) also triggers a sync
- bookCard() adds data-book-id attribute for targeted DOM updates
"

# ── Commit 3: Docs ────────────────────────────────────────────────────────────
git add MIGRATION_GUIDE.md
git commit -m "docs: add full-stack migration guide"
```

---

## 7 · Local Development

```bash
# Install Python deps
pip install -r requirements.txt

# Run the Flask API locally (http://localhost:8000)
python api/progress.py

# Serve the frontend (any static server)
npx serve public -p 3000

# Test the API
curl -X POST http://localhost:8000/api/progress \
  -H "Content-Type: application/json" \
  -d '{"book_id":"b123","current_page":47,"total_pages":304,"display_name":"Deep Work"}'
```

> **Note:** When running locally, the frontend at `:3000` calls `/api/progress`
> which hits the same origin — but the Flask server is on `:8000`. Either:
> - Run both on the same port with `flask run --port 3000` and serve `public/`
>   as a static folder from Flask (add `app.static_folder = '../public'`), **or**
> - Use the Vercel CLI: `npm i -g vercel && vercel dev` — it emulates the full
>   routing table from `vercel.json` locally.

---

## 8 · Deployment Checklist

- [ ] `vercel.json` is in the repo root
- [ ] `requirements.txt` is in the repo root
- [ ] `api/progress.py` is in `/api/`
- [ ] `public/index.html` is in `/public/`
- [ ] KV or Postgres linked in Vercel dashboard
- [ ] `KV_REST_API_URL` + `KV_REST_API_TOKEN` env vars set (or `POSTGRES_URL`)
- [ ] CORS `ALLOWED_ORIGINS` in `progress.py` includes your Vercel domain
- [ ] `git push` → Vercel auto-deploys on every push to `main`
