"""
Readwise — /api/progress.py
Vercel Python Serverless Function (Flask)

Storage strategy (choose ONE, comment out the other):
  A) Vercel KV  — via upstash-redis  (recommended for production)
  B) Vercel Postgres — via psycopg2  (if you need relational queries)
  C) In-memory fallback              (dev only, data lost on cold-start)

Set environment variables in Vercel dashboard → Settings → Environment Variables:
  KV_URL               e.g. rediss://default:<token>@<host>:6380
  KV_REST_API_URL      (Upstash REST URL, alternative to raw redis)
  KV_REST_API_TOKEN    (Upstash REST token)
"""

from flask import Flask, request, jsonify
from functools import wraps
import os, json, math, time

app = Flask(__name__)

# ─────────────────────────────────────────────
# CORS — allow your Vercel domain + localhost
# ─────────────────────────────────────────────
ALLOWED_ORIGINS = {
    "http://localhost:3000",
    "http://127.0.0.1:5500",
    # Add your Vercel domain once deployed:
    # "https://readwise-yourname.vercel.app",
}

def cors_headers(response):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

app.after_request(cors_headers)

@app.route("/api/progress", methods=["OPTIONS"])
def preflight():
    return jsonify({}), 204


# ─────────────────────────────────────────────
# STORAGE LAYER  (swap implementations freely)
# ─────────────────────────────────────────────

def _kv_key(book_id: str) -> str:
    """Namespaced Redis key so you can share a single KV store."""
    return f"readwise:book:{book_id}"


# ── Option A: Vercel KV via upstash-redis ────────────────────────────────────
try:
    from upstash_redis import Redis as UpstashRedis          # pip install upstash-redis
    _redis = UpstashRedis(
        url=os.environ["KV_REST_API_URL"],
        token=os.environ["KV_REST_API_TOKEN"],
    )

    def storage_get(book_id: str) -> dict | None:
        raw = _redis.get(_kv_key(book_id))
        return json.loads(raw) if raw else None

    def storage_set(book_id: str, data: dict) -> None:
        _redis.set(_kv_key(book_id), json.dumps(data))

    STORAGE_BACKEND = "vercel-kv"

except (ImportError, KeyError):
    # ── Option C: In-memory fallback (dev / when KV not configured) ──────────
    _mem: dict = {}

    def storage_get(book_id: str) -> dict | None:
        return _mem.get(book_id)

    def storage_set(book_id: str, data: dict) -> None:
        _mem[book_id] = data

    STORAGE_BACKEND = "in-memory"


# ── Option B: Vercel Postgres (uncomment to enable) ──────────────────────────
# import psycopg2, psycopg2.extras                          # pip install psycopg2-binary
# _pg_url = os.environ.get("POSTGRES_URL")
#
# def _pg_conn():
#     return psycopg2.connect(_pg_url, cursor_factory=psycopg2.extras.RealDictCursor)
#
# def storage_get(book_id: str) -> dict | None:
#     with _pg_conn() as conn, conn.cursor() as cur:
#         cur.execute("SELECT data FROM reading_progress WHERE book_id = %s", (book_id,))
#         row = cur.fetchone()
#         return row["data"] if row else None
#
# def storage_set(book_id: str, data: dict) -> None:
#     with _pg_conn() as conn, conn.cursor() as cur:
#         cur.execute("""
#             INSERT INTO reading_progress (book_id, data, updated_at)
#             VALUES (%s, %s, NOW())
#             ON CONFLICT (book_id) DO UPDATE
#               SET data = EXCLUDED.data, updated_at = NOW()
#         """, (book_id, json.dumps(data)))
#         conn.commit()
#
# STORAGE_BACKEND = "postgres"


# ─────────────────────────────────────────────
# BOOK SCHEMA (mirrors your JS `books` array)
# ─────────────────────────────────────────────
#
# {
#   "id":                  "b1715000000001234",   // matches your JS id scheme
#   "display_name":        "Deep Work",
#   "total_pages":         304,
#   "current_page":        47,
#   "progress_percentage": 15.46,                 // always calculated server-side
#   "status":              "reading",             // "pending" | "reading" | "completed"
#   "updated_at":          1715012345678          // epoch ms
# }

def build_book_record(book_id: str, current_page: int, total_pages: int,
                       display_name: str = "", status: str = "reading") -> dict:
    """
    Constructs a canonical Book record.
    progress_percentage is ALWAYS derived — never stored raw — to avoid drift.
    """
    pct = round((current_page / total_pages) * 100, 2) if total_pages > 0 else 0.0
    return {
        "id":                  book_id,
        "display_name":        display_name,
        "total_pages":         total_pages,
        "current_page":        current_page,
        "progress_percentage": pct,
        "status":              status,
        "updated_at":          int(time.time() * 1000),
    }


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route("/api/progress", methods=["POST"])
def update_progress():
    """
    POST /api/progress
    Body (JSON):
      {
        "book_id":      "b1715000000001234",   required
        "current_page": 47,                    required
        "total_pages":  304,                   required
        "display_name": "Deep Work",           optional
        "status":       "reading"              optional
      }

    Returns:
      {
        "ok": true,
        "book": { ...Book record with progress_percentage... },
        "storage_backend": "vercel-kv"
      }
    """
    body = request.get_json(silent=True) or {}

    # ── Validate ──────────────────────────────
    book_id      = body.get("book_id", "").strip()
    current_page = body.get("current_page")
    total_pages  = body.get("total_pages")

    errors = []
    if not book_id:
        errors.append("book_id is required")
    if not isinstance(current_page, int) or current_page < 1:
        errors.append("current_page must be a positive integer")
    if not isinstance(total_pages, int) or total_pages < 1:
        errors.append("total_pages must be a positive integer")
    if current_page and total_pages and current_page > total_pages:
        errors.append("current_page cannot exceed total_pages")

    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    # ── Merge with existing record ────────────
    existing  = storage_get(book_id) or {}
    status    = body.get("status", existing.get("status", "reading"))
    disp_name = body.get("display_name", existing.get("display_name", ""))

    record = build_book_record(
        book_id=book_id,
        current_page=current_page,
        total_pages=total_pages,
        display_name=disp_name,
        status=status,
    )

    storage_set(book_id, record)

    return jsonify({
        "ok":              True,
        "book":            record,
        "storage_backend": STORAGE_BACKEND,
    })


@app.route("/api/progress/<book_id>", methods=["GET"])
def get_progress(book_id: str):
    """
    GET /api/progress/<book_id>
    Returns the stored Book record, or 404 if not found.
    """
    record = storage_get(book_id)
    if not record:
        return jsonify({"ok": False, "error": "Book not found"}), 404
    return jsonify({"ok": True, "book": record})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "storage_backend": STORAGE_BACKEND})


# ─────────────────────────────────────────────
# LOCAL DEV ENTRY POINT
# python api/progress.py  →  http://localhost:8000
# ─────────────────────────────────────────────
if __name__ == "__main__":
    app.run(port=8000, debug=True)
