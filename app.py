"""
nigri-sync
Small Flask service that BrightPath's "Sync to Nigri" button calls.
Receives the day's roster + points from BrightPath, then drives a
headless browser to mark attendance + points on the Nigri site.

Deploy this the same way as posuk-scorer (Render, Python service).
Required env vars:
    NIGRI_USERNAME
    NIGRI_PASSWORD
    SYNC_API_KEY      -- shared secret so random people can't hit your endpoint
"""

import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from nigri_playwright import run_sync, debug_attendance_page, debug_points_attempt

app = Flask(__name__)
CORS(app)  # allow calls from simchacohen1.github.io

SYNC_API_KEY = os.environ.get("SYNC_API_KEY")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/sync-points", methods=["POST"])
def sync_points():
    # --- auth check ---
    provided_key = request.headers.get("X-Sync-Key")
    if not SYNC_API_KEY or provided_key != SYNC_API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(force=True, silent=True) or {}

    # Expected shape from BrightPath:
    # {
    #   "class_section": "B3 WT" | "B3 ET",
    #   "date": "2026-08-26",
    #   "students": [
    #       {"name": "Ari Greenberg", "points": 6},
    #       {"name": "Leib Wolf", "points": 6},
    #       ...
    #   ]
    # }
    class_section = body.get("class_section")
    date = body.get("date")
    students = body.get("students", [])

    if not class_section or not date or not students:
        return jsonify({"error": "missing class_section, date, or students"}), 400

    try:
        result = run_sync(class_section=class_section, date=date, students=students)
        return jsonify({"status": "success", "detail": result})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/debug-page", methods=["POST"])
def debug_page():
    # --- auth check ---
    provided_key = request.headers.get("X-Sync-Key")
    if not SYNC_API_KEY or provided_key != SYNC_API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(force=True, silent=True) or {}
    class_section = body.get("class_section")
    date = body.get("date")
    target = body.get("target", "attend")

    if not class_section or not date:
        return jsonify({"error": "missing class_section or date"}), 400

    try:
        html = debug_attendance_page(class_section=class_section, date=date, target=target)
        return jsonify({"status": "success", "html": html})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/debug-points", methods=["POST"])
def debug_points():
    # --- auth check ---
    provided_key = request.headers.get("X-Sync-Key")
    if not SYNC_API_KEY or provided_key != SYNC_API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(force=True, silent=True) or {}
    class_section = body.get("class_section")
    date = body.get("date")
    students = body.get("students", [])

    if not class_section or not date or not students:
        return jsonify({"error": "missing class_section, date, or students"}), 400

    try:
        result = debug_points_attempt(class_section=class_section, date=date, students=students)
        return jsonify({"status": "success", **result})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
