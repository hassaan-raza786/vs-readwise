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
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "https://vs-readwise.vercel.app",
    "https://readwise.vercel.app",
}

def cors_headers(response):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return response

app.after_request(cors_headers)

@app.route("/api/progress", methods=["OPTIONS"])
@app.route("/api/books", methods=["OPTIONS"])
@app.route("/api/books/<book_id>", methods=["OPTIONS"])
def preflight():
    return jsonify({}), 204


# ─────────────────────────────────────────────
# STORAGE LAYER  (swap implementations freely)
# ─────────────────────────────────────────────

def _kv_key(key: str) -> str:
    """Namespaced Redis key so you can share a single KV store."""
    return f"readwise:{key}"

def _kv_key_book(book_id: str) -> str:
    return f"readwise:book:{book_id}"

def _kv_key_user_books(user_id: str) -> str:
    return f"readwise:user:{user_id}:books"

def _kv_key_user_annotations(user_id: str) -> str:
    return f"readwise:user:{user_id}:annotations"


# ── Option A: Vercel KV via upstash-redis ────────────────────────────────────
try:
    from upstash_redis import Redis as UpstashRedis          # pip install upstash-redis
    _redis = UpstashRedis(
        url=os.environ.get("KV_REST_API_URL", ""),
        token=os.environ.get("KV_REST_API_TOKEN", ""),
    )

    def storage_get(key: str) -> dict | None:
        raw = _redis.get(key)
        return json.loads(raw) if raw else None

    def storage_set(key: str, data: dict) -> None:
        _redis.set(key, json.dumps(data))
        
    def storage_delete(key: str) -> None:
        _redis.delete(key)

    STORAGE_BACKEND = "vercel-kv"

except (ImportError, KeyError, Exception):
    # ── Option C: In-memory fallback (dev / when KV not configured) ──────────
    _mem: dict = {}

    def storage_get(key: str) -> dict | None:
        return _mem.get(key)

    def storage_set(key: str, data: dict) -> None:
        _mem[key] = data
        
    def storage_delete(key: str) -> None:
        if key in _mem:
            del _mem[key]

    STORAGE_BACKEND = "in-memory"


# ── Option B: Vercel Postgres (uncomment to enable) ──────────────────────────
# import psycopg2, psycopg2.extras                          # pip install psycopg2-binary
# _pg_url = os.environ.get("POSTGRES_URL")
#
# def _pg_conn():
#     return psycopg2.connect(_pg_url, cursor_factory=psycopg2.extras.RealDictCursor)
#
# def storage_get(key: str) -> dict | None:
#     with _pg_conn() as conn, conn.cursor() as cur:
#         cur.execute("SELECT data FROM kv_store WHERE key = %s", (key,))
#         row = cur.fetchone()
#         return row["data"] if row else None
#
# def storage_set(key: str, data: dict) -> None:
#     with _pg_conn() as conn, conn.cursor() as cur:
#         cur.execute("""
#             INSERT INTO kv_store (key, data, updated_at)
#             VALUES (%s, %s, NOW())
#             ON CONFLICT (key) DO UPDATE
#               SET data = EXCLUDED.data, updated_at = NOW()
#         """, (key, json.dumps(data)))
#         conn.commit()
#
# def storage_delete(key: str) -> None:
#     with _pg_conn() as conn, conn.cursor() as cur:
#         cur.execute("DELETE FROM kv_store WHERE key = %s", (key,))
#         conn.commit()
#
# STORAGE_BACKEND = "postgres"


# ─────────────────────────────────────────────
# BOOK SCHEMA (mirrors your JS `books` array)
# ─────────────────────────────────────────────
#
# {
#   "id":                  "b1715000000001234",
#   "display_name":        "Deep Work",
#   "total_pages":         304,
#   "current_page":        47,
#   "progress_percentage": 15.46,
#   "status":              "reading",
#   "updated_at":          1715012345678
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
# PROGRESS ROUTES (Individual book progress)
# ─────────────────────────────────────────────

@app.route("/api/progress", methods=["POST"])
def update_progress():
    """
    POST /api/progress
    Body (JSON):
      {
        "book_id":      "b1715000000001234",
        "current_page": 47,
        "total_pages":  304,
        "display_name": "Deep Work",
        "status":       "reading"
      }
    """
    body = request.get_json(silent=True) or {}

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

    existing  = storage_get(_kv_key_book(book_id)) or {}
    status    = body.get("status", existing.get("status", "reading"))
    disp_name = body.get("display_name", existing.get("display_name", ""))

    record = build_book_record(
        book_id=book_id,
        current_page=current_page,
        total_pages=total_pages,
        display_name=disp_name,
        status=status,
    )

    storage_set(_kv_key_book(book_id), record)

    return jsonify({
        "ok":              True,
        "book":            record,
        "storage_backend": STORAGE_BACKEND,
    })


@app.route("/api/progress/<book_id>", methods=["GET"])
def get_progress(book_id: str):
    """GET /api/progress/<book_id> - Returns stored Book record"""
    record = storage_get(_kv_key_book(book_id))
    if not record:
        return jsonify({"ok": False, "error": "Book not found"}), 404
    return jsonify({"ok": True, "book": record})


# ─────────────────────────────────────────────
# LIBRARY ROUTES (Full user library sync)
# ─────────────────────────────────────────────

@app.route("/api/books", methods=["POST"])
def save_books():
    """
    POST /api/books
    Body: { "user_id": "user_xxx", "books": [...], "annotations": {...} }
    Saves entire library to KV storage (cloud backup)
    """
    body = request.get_json(silent=True) or {}
    
    user_id = body.get("user_id", "default_user")
    books_data = body.get("books", [])
    annotations_data = body.get("annotations", {})
    
    if not isinstance(books_data, list):
        return jsonify({"ok": False, "error": "books must be an array"}), 400
    
    try:
        # Save books list
        storage_set(_kv_key_user_books(user_id), {"books": books_data, "updated_at": int(time.time() * 1000)})
        # Save annotations
        storage_set(_kv_key_user_annotations(user_id), {"annotations": annotations_data, "updated_at": int(time.time() * 1000)})
        
        # Also save individual book progress for each book
        for book in books_data:
            if book.get("id") and book.get("totalPages"):
                progress_record = build_book_record(
                    book_id=book["id"],
                    current_page=book.get("currentPage", 1),
                    total_pages=book.get("totalPages", 1),
                    display_name=book.get("displayName", ""),
                    status=book.get("status", "reading"),
                )
                storage_set(_kv_key_book(book["id"]), progress_record)
        
        return jsonify({
            "ok": True,
            "message": f"Saved {len(books_data)} books to cloud",
            "storage_backend": STORAGE_BACKEND
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/books", methods=["GET"])
def get_books():
    """
    GET /api/books?user_id=user_xxx
    Returns saved library from KV storage
    """
    user_id = request.args.get("user_id", "default_user")
    
    books_data = storage_get(_kv_key_user_books(user_id))
    annotations_data = storage_get(_kv_key_user_annotations(user_id))
    
    books_list = books_data.get("books", []) if books_data else []
    annotations_dict = annotations_data.get("annotations", {}) if annotations_data else {}
    
    # Also try to get individual book progress and merge
    for i, book in enumerate(books_list):
        book_id = book.get("id")
        if book_id:
            progress = storage_get(_kv_key_book(book_id))
            if progress:
                books_list[i]["currentPage"] = progress.get("current_page", book.get("currentPage", 1))
                books_list[i]["totalPages"] = progress.get("total_pages", book.get("totalPages", 0))
                books_list[i]["status"] = progress.get("status", book.get("status", "pending"))
    
    return jsonify({
        "ok": True,
        "books": books_list,
        "annotations": annotations_dict,
        "storage_backend": STORAGE_BACKEND
    })


@app.route("/api/books/<book_id>", methods=["DELETE"])
def delete_book(book_id):
    """
    DELETE /api/books/<book_id>?user_id=user_xxx
    Removes a book from storage
    """
    user_id = request.args.get("user_id", "default_user")
    
    try:
        # Get current books
        books_data = storage_get(_kv_key_user_books(user_id))
        books_list = books_data.get("books", []) if books_data else []
        
        # Remove book
        books_list = [b for b in books_list if b.get("id") != book_id]
        
        # Also remove its annotations
        annotations_data = storage_get(_kv_key_user_annotations(user_id))
        annotations_dict = annotations_data.get("annotations", {}) if annotations_data else {}
        if book_id in annotations_dict:
            del annotations_dict[book_id]
        
        # Remove individual progress
        storage_delete(_kv_key_book(book_id))
        
        # Save back
        storage_set(_kv_key_user_books(user_id), {"books": books_list, "updated_at": int(time.time() * 1000)})
        storage_set(_kv_key_user_annotations(user_id), {"annotations": annotations_dict, "updated_at": int(time.time() * 1000)})
        
        return jsonify({"ok": True, "message": "Book deleted from cloud"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/health", methods=["GET"])
def health():
    """Health check endpoint"""
    return jsonify({
        "ok": True,
        "storage_backend": STORAGE_BACKEND,
        "status": "running"
    })


# ─────────────────────────────────────────────
# LOCAL DEV ENTRY POINT
# python api/progress.py  →  http://localhost:8000
# ─────────────────────────────────────────────
if __name__ == "__main__":
    app.run(port=8000, debug=True)