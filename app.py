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
from nigri_playwright import (
    run_sync,
    run_marks_sync,
    debug_attendance_page,
    debug_points_attempt,
    debug_rewards_page,
    debug_marks_page,
    debug_marks_create_and_view,
    debug_marks_full_flow,
    SyncError,
)

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
    except SyncError as e:
        return jsonify({"status": "error", "detail": str(e), "completed_before_failure": e.partial_results}), 500
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


@app.route("/debug-rewards", methods=["POST"])
def debug_rewards():
    # --- auth check ---
    provided_key = request.headers.get("X-Sync-Key")
    if not SYNC_API_KEY or provided_key != SYNC_API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(force=True, silent=True) or {}
    student_name = body.get("student_name")  # optional

    try:
        result = debug_rewards_page(student_name=student_name)
        return jsonify({"status": "success", **result})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/debug-marks", methods=["GET", "POST"])
def debug_marks():
    # --- auth check ---
    # GET is here specifically so this can be triggered by just pasting a
    # URL into a browser's address bar -- no terminal/curl needed. The key
    # goes in the URL itself (?key=...) for GET, or the X-Sync-Key header
    # for POST (used by the other debug endpoints / the real sync later).
    provided_key = request.headers.get("X-Sync-Key") or request.args.get("key")
    if not SYNC_API_KEY or provided_key != SYNC_API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        click_texts = body.get("click_texts", ["Create New Mark"])
        screenshot = bool(body.get("screenshot", False))
    else:
        # GET: ?click=Create New Mark,Some Other Button  (comma-separated)
        # ?click=  (empty) means "click nothing, just show the overview"
        # &screenshot=1 includes a base64 screenshot in the JSON
        raw_click = request.args.get("click")
        if raw_click is None:
            click_texts = ["Create New Mark"]
        elif raw_click.strip() == "":
            click_texts = []
        else:
            click_texts = [t.strip() for t in raw_click.split(",") if t.strip()]
        screenshot = request.args.get("screenshot") in ("1", "true", "True")

    try:
        result = debug_marks_page(click_texts=click_texts, screenshot=screenshot)
        return jsonify({"status": "success", **result})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/debug-marks-create", methods=["GET", "POST"])
def debug_marks_create():
    # --- auth check ---
    # WARNING: unlike every other /debug-* endpoint, this one writes a
    # real (obviously-fake) mark to the live Nigri site, then deletes it
    # again by default. See debug_marks_create_and_view()'s docstring.
    provided_key = request.headers.get("X-Sync-Key") or request.args.get("key")
    if not SYNC_API_KEY or provided_key != SYNC_API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        class_section = body.get("class_section", "B3 ET")
        topic_label = body.get("topic", "Chumash")
        mark_type = body.get("type", "quiz")
        test_name = body.get("name", "ZZZ_DEBUG_DELETE_ME")
        test_date = body.get("date")
        delete_after = bool(body.get("delete", True))
        screenshot = bool(body.get("screenshot", False))
    else:
        # GET, so this can be triggered by pasting a URL into a browser:
        #   /debug-marks-create?key=...
        #   &class=B3 ET&topic=Chumash&type=quiz&name=ZZZ_DEBUG_DELETE_ME
        #   &delete=1&screenshot=0
        class_section = request.args.get("class", "B3 ET")
        topic_label = request.args.get("topic", "Chumash")
        mark_type = request.args.get("type", "quiz")
        test_name = request.args.get("name", "ZZZ_DEBUG_DELETE_ME")
        test_date = request.args.get("date")
        delete_after = request.args.get("delete", "1") in ("1", "true", "True")
        screenshot = request.args.get("screenshot") in ("1", "true", "True")

    try:
        result = debug_marks_create_and_view(
            class_section=class_section,
            topic_label=topic_label,
            mark_type=mark_type,
            test_name=test_name,
            test_date=test_date,
            delete_after=delete_after,
            screenshot=screenshot,
        )
        return jsonify({"status": "success", **result})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/sync-marks", methods=["POST"])
def sync_marks():
    # --- auth check ---
    # This is the real, production endpoint -- it creates and saves a
    # real mark on the live Nigri site (it does NOT auto-delete). Use
    # /debug-marks-full first to test the whole flow safely.
    provided_key = request.headers.get("X-Sync-Key")
    if not SYNC_API_KEY or provided_key != SYNC_API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(force=True, silent=True) or {}

    # Expected shape (matches the quiz app's "packet"):
    # {
    #   "class_section": "B3 ET" | "B3 WT",
    #   "topic": "Chumash",
    #   "test_name": "Weekly Quiz - Chumash 9/8",
    #   "type": "quiz",              (optional, default "quiz")
    #   "date": "2026-09-08",        (optional)
    #   "description": "...",       (optional)
    #   "mark_all_done": true,       (optional, default true)
    #   "students": [
    #       {"name": "Greenberg Ari", "mark": 8, "attendance": "present", "comment": ""},
    #       {"name": "Rosenfeld Zev", "mark": null, "attendance": "absent"},
    #       {"name": "Vogel Leibel", "mark": null, "attendance": "review"},
    #       ...
    #   ]
    # }
    class_section = body.get("class_section")
    topic = body.get("topic")
    test_name = body.get("test_name")
    students = body.get("students", [])
    mark_type = body.get("type", "quiz")
    test_date = body.get("date")
    description = body.get("description")
    mark_all_done = bool(body.get("mark_all_done", True))

    if not class_section or not topic or not test_name or not students:
        return jsonify({"error": "missing class_section, topic, test_name, or students"}), 400

    try:
        result = run_marks_sync(
            class_section=class_section,
            topic=topic,
            test_name=test_name,
            students=students,
            mark_type=mark_type,
            test_date=test_date,
            description=description,
            mark_all_done=mark_all_done,
        )
        return jsonify({"status": "success", **result})
    except SyncError as e:
        return jsonify({"status": "error", "detail": str(e), "completed_before_failure": e.partial_results}), 500
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/debug-marks-full", methods=["GET", "POST"])
def debug_marks_full():
    # --- auth check ---
    # WARNING: like /debug-marks-create, this writes a real (obviously
    # fake) mark to the live site -- but unlike that one, this also
    # fills in one real student's score/comment and clicks the FINAL
    # Save!, exercising the exact same code path as the real
    # /sync-marks endpoint above. Deletes the test mark afterward by
    # default, same safety net as /debug-marks-create.
    provided_key = request.headers.get("X-Sync-Key") or request.args.get("key")
    if not SYNC_API_KEY or provided_key != SYNC_API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        class_section = body.get("class_section", "B3 ET")
        topic = body.get("topic", "Chumash")
        mark_type = body.get("type", "quiz")
        test_name = body.get("name", "ZZZ_DEBUG_DELETE_ME")
        test_date = body.get("date")
        delete_after = bool(body.get("delete", True))
        student_name = body.get("student", "Chaikin Mayer Chaim")
        student_mark = body.get("mark", "9")
        with_report = bool(body.get("with_report", False))
    else:
        # GET, so this can be triggered by pasting a URL into a browser:
        #   /debug-marks-full?key=...
        #   &class=B3 ET&topic=Chumash&type=quiz&name=ZZZ_DEBUG_DELETE_ME
        #   &student=Chaikin Mayer Chaim&mark=9&delete=1&with_report=1
        class_section = request.args.get("class", "B3 ET")
        topic = request.args.get("topic", "Chumash")
        mark_type = request.args.get("type", "quiz")
        test_name = request.args.get("name", "ZZZ_DEBUG_DELETE_ME")
        test_date = request.args.get("date")
        delete_after = request.args.get("delete", "1") in ("1", "true", "True")
        student_name = request.args.get("student", "Chaikin Mayer Chaim")
        student_mark = request.args.get("mark", "9")
        with_report = request.args.get("with_report") in ("1", "true", "True")

    try:
        student = {
            "name": student_name,
            "mark": student_mark,
            "attendance": "present",
            "comment": "test",
        }
        if with_report:
            from nigri_playwright import _TEST_REPORT_PDF_BASE64
            student["report_base64"] = _TEST_REPORT_PDF_BASE64
            student["report_filename"] = "test-report-attachment.pdf"
        result = debug_marks_full_flow(
            class_section=class_section,
            topic=topic,
            test_name=test_name,
            mark_type=mark_type,
            test_date=test_date,
            students=[student],
            delete_after=delete_after,
        )
        return jsonify({"status": "success", **result})
    except SyncError as e:
        return jsonify({"status": "error", "detail": str(e), "completed_before_failure": e.partial_results}), 500
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
