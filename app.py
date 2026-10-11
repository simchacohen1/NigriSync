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
    read_marks,
    debug_attendance_page,
    debug_points_attempt,
    debug_rewards_page,
    debug_marks_page,
    debug_marks_create_and_view,
    debug_marks_full_flow,
    SyncError,
)
from attendance_reader import read_attendance

app = Flask(__name__)
CORS(app, resources={
    r"/classtime/.*": {"origins": ["https://simchacohen1.github.io", "https://funtorahtools.com", "https://www.funtorahtools.com", "https://funtorahtools.org", "https://www.funtorahtools.org"]},
    r"/(?!classtime/).*": {"origins": "*"},  # Preserve existing Attendance/Weekly Quiz behavior.
})

# Existing browser routes share the same cross-process slot with Classtime.
# Their authentication and Nigri behavior are unchanged; a busy service returns 409.
@app.before_request
def serialize_existing_browser_routes():
    if request.path in ('/health',) or request.path.startswith('/classtime/') or request.method == 'OPTIONS': return
    from browser_runtime import browser_slot
    from flask import g
    slot = browser_slot()
    try: slot.__enter__()
    except ValueError as exc: return jsonify({"error": str(exc)}), 409
    g.browser_slot = slot

@app.teardown_request
def release_browser_slot(error):
    from flask import g
    slot = g.pop('browser_slot', None)
    if slot: slot.__exit__(None, None, None)


SYNC_API_KEY = os.environ.get("SYNC_API_KEY")


def nigri_authorized(allow_query_key=False):
    """A Nigri route is allowed if EITHER the old shared key matches (exactly as before) OR the request carries the
    website owner's verified Google sign-in (Authorization: Bearer <token>). The Classtime diagnostic token does not count here."""
    import hmac
    provided = request.headers.get("X-Sync-Key") or (request.args.get("key") if allow_query_key else None)
    if SYNC_API_KEY and provided and hmac.compare_digest(provided.encode("utf-8"), SYNC_API_KEY.encode("utf-8")):
        return True
    from classtime_auth import review_owner
    return bool(review_owner(allow_diagnostic=False))


# Read-only Classtime diagnostic, separate from every Nigri write route.
@app.route("/classtime/resources", methods=["GET"])
def classtime_resource_status():
    from classtime_auth import review_owner
    from browser_runtime import resource_status
    if not review_owner(): return jsonify({"error": "unauthorized"}), 401
    response = jsonify(resource_status())
    response.headers["Cache-Control"] = "no-store"
    return response


# Browser-free Nigri page-structure probe. The only POST it can send to Nigri
# is sign-in; it never submits the Create Mark!, Save!, or delete forms.
@app.route("/classtime/nigri-http-probe", methods=["POST"])
def classtime_nigri_http_probe():
    from classtime_auth import review_owner
    if not review_owner(allow_diagnostic=False):
        return jsonify({"error": "unauthorized"}), 401
    from browser_runtime import run_browser
    from nigri_http_probe import probe
    try:
        result = run_browser(probe, _time_limit=90)
    except Exception:
        result = {"status": "probe_stopped", "nigri_writes": False}
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


# A previous test was stopped mid-save at the RAM cutoff. Until the new
# HTTP save path has been verified and approved, block all Classtime writes
# on the server, including calls bypassing the website's disabled controls.
CLASSTIME_SAVES_PAUSED = True


# Hybrid diagnostic: use the established browser login only, CLOSE Chromium,
# then test a read-only HTTP GET of School Marks. No grade writes.
@app.route("/classtime/nigri-hybrid-probe", methods=["POST"])
def classtime_nigri_hybrid_probe():
    from classtime_auth import review_owner
    if not review_owner(allow_diagnostic=False):
        return jsonify({"error": "unauthorized"}), 401
    from browser_runtime import run_browser
    from nigri_http_probe import probe_hybrid
    try:
        result = run_browser(probe_hybrid, _time_limit=90)
    except Exception:
        result = {"status": "hybrid_stopped_by_memory_guard", "nigri_writes": False}
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


# Owner-only read-only blank New Mark form inspection. At most one GET beyond
# the hybrid School Marks listing; never submits Create Mark! or grade data.
@app.route("/classtime/nigri-blank-mark-probe", methods=["POST"])
def classtime_nigri_blank_mark_probe():
    from classtime_auth import review_owner
    if not review_owner(allow_diagnostic=False):
        return jsonify({"error": "unauthorized"}), 401
    from browser_runtime import run_browser
    from nigri_http_probe import probe_hybrid
    try:
        result = run_browser(probe_hybrid, inspect_blank_form=True, _time_limit=90)
    except Exception:
        result = {"status": "blank_form_stopped_safely", "nigri_writes": False}
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/classtime/test-session", methods=["POST"])
def classtime_test_session():
    import hmac
    from classtime_reader import test_session
    expected = os.environ.get("CLASSTIME_TEST_TOKEN", "")
    supplied = request.headers.get("Authorization", "")
    if not expected or not hmac.compare_digest(supplied, "Bearer " + expected):
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    code = str(body.get("session_code", "")).strip().upper()
    import re
    if not re.fullmatch(r"[A-Z0-9]{6}", code):
        return jsonify({"error": "A six-character session code is required"}), 400
    try:
        response = jsonify(test_session(code, inspect_exports=body.get("inspect_exports") is True))
    except ValueError as exc:
        response = jsonify({"status": "stopped", "detail": str(exc), "nigri_writes": False})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/classtime/reviews", methods=["POST"])
def classtime_create_review():
    from classtime_auth import review_owner
    from classtime_review import start_review
    owner = review_owner()
    if not owner: return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True)
    if not isinstance(body, dict): return jsonify({"error": "Invalid request"}), 400
    import re
    code = str(body.get("session_code", "")).strip().upper()
    section = body.get("class_section")
    if not re.fullmatch(r"[A-Z0-9]{6}", code) or section not in ("B3 ET", "B3 WT"):
        return jsonify({"error": "Choose B3 ET or B3 WT and a six-character session code"}), 400
    try:
        from classtime_reader import load_review
        key = start_review(owner, code, section, load_review)
        response = jsonify({"review_id": key, "read_only": True, "sync_enabled": False})
        response.headers["Cache-Control"] = "no-store"
        return response, 202
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


@app.route("/classtime/reviews/import", methods=["POST"])
def classtime_import_review():
    from classtime_auth import review_owner
    from classtime_review import import_review_archive, store_review
    owner = review_owner()
    if not owner: return jsonify({"error": "unauthorized"}), 401
    if request.content_length is None or request.content_length > 20 * 1024 * 1024:
        return jsonify({"error": "Archive exceeds the size limit"}), 413
    try:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory(prefix='classtime-upload-') as folder:
            path = Path(folder) / 'review.zip'
            with path.open('wb') as output:
                import shutil
                shutil.copyfileobj(request.stream, output, length=65536)
            from browser_runtime import run_browser
            result, pdfs = run_browser(import_review_archive, path)
        key = store_review(owner, result, pdfs)
        response = jsonify({"review_id": key, "result": result, "read_only": True, "sync_enabled": False})
        response.headers["Cache-Control"] = "no-store"
        return response, 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Archive could not be verified"}), 400


@app.route("/classtime/reviews/<review_id>", methods=["GET"])
def classtime_get_review(review_id):
    from classtime_auth import review_owner
    from classtime_review import get_review
    owner = review_owner()
    if not owner: return jsonify({"error": "unauthorized"}), 401
    job = get_review(review_id, owner)
    if not job: return jsonify({"error": "Review expired or unavailable. Load it again."}), 404
    response = jsonify({"status": job["status"], "detail": job.get("detail"), "result": job.get("result")})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/classtime/reviews/<review_id>/sync-preview", methods=["POST"])
def classtime_sync_preview(review_id):
    """DRY RUN of 'Sync to Nigri'. Owner Google sign-in only (never the old key, never the diagnostic token).
    Writes nothing to Nigri and does not launch a browser or contact Nigri."""
    from classtime_auth import review_owner
    from classtime_review import get_review
    owner = review_owner(allow_diagnostic=False)
    if not owner: return jsonify({"error": "unauthorized"}), 401
    job = get_review(review_id, owner)
    if not job: return jsonify({"error": "Review expired or unavailable. Load it again."}), 404
    if job["status"] != "ready": return jsonify({"error": "The review is not ready yet"}), 409
    import classtime_sync
    from nigri_playwright import MARKS_TOPIC_IDS
    try:
        options = classtime_sync.validate_options(request.get_json(silent=True), job["result"], MARKS_TOPIC_IDS)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        response = jsonify(classtime_sync.preview(job["result"], job["pdfs"], options, None, duplicate_check_disabled=True))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/classtime/reviews/<review_id>/sync-save", methods=["POST"])
def classtime_sync_save(review_id):
    """The REAL save of a Classtime review into Nigri (through the existing run_marks_sync code).
    Owner Google sign-in only. Rows are rebuilt here from the loaded review; nothing from the browser is trusted but the choices."""
    from classtime_auth import review_owner
    owner = review_owner(allow_diagnostic=False)
    if not owner: return jsonify({"error": "unauthorized"}), 401
    if CLASSTIME_SAVES_PAUSED:
        return jsonify({"saved": False, "error": "Classtime saving is paused during the browser-free Nigri investigation. Nothing was written."}), 423
    import re
    import classtime_save as save
    import classtime_sync
    from nigri_playwright import MARKS_TOPIC_IDS
    from browser_runtime import run_browser
    body = request.get_json(silent=True)
    job, refusal = save.claim(review_id, owner)   # the lock: a second click or a second save is refused here
    if refusal: return jsonify({"saved": False, "error": refusal[1]}), refusal[0]
    result, pdfs = job["result"], job["pdfs"]
    key = (result["session_code"], result["class_section"])
    def respond(payload, code):
        response = jsonify(payload); response.headers["Cache-Control"] = "no-store"
        return response, code
    try:
        try:
            planned = save.plan(result, pdfs, body, MARKS_TOPIC_IDS)
        except ValueError as e:
            save.settle(review_id, "none"); return respond({"saved": False, "error": str(e)}, 400)
        if planned["hard_blockers"]:
            save.settle(review_id, "none")
            return respond({"saved": False, "error": "Not saved: " + "; ".join(planned["hard_blockers"]), "blockers": planned["hard_blockers"], "can_override": False}, 409)
        # No Nigri duplicate lookup: it launched a second browser and could
        # exhaust Render Free memory. Keep the existing in-process ledger.
        status, detail = "clear", None
        if key in save.LEDGER:
            status, detail = "duplicate", "saved earlier by this server (mark #" + str(save.LEDGER[key].get("test_id") or "?") + ")"
        blocked = save.check_override(body, planned["options"], status, detail, result["session_code"])
        if blocked:
            save.settle(review_id, "none")
            return respond({"saved": False, "error": blocked[1], "duplicate_check": {"status": status, "detail": detail}, "can_override": status in save.OVERRIDABLE}, blocked[0])
    except Exception:
        save.settle(review_id, "none")
        return respond({"saved": False, "error": "Nothing was saved. Something went wrong before the save started."}, 500)
    # Past this point Nigri may be written to. Anything that goes wrong is reported as a possible partial mark and never retried.
    try:
        outcome = run_browser(save.save_job, result, pdfs, body, MARKS_TOPIC_IDS, _time_limit=save.save_time_limit())
    except ValueError as e:
        message = str(e)
        if message.startswith(save.NOT_STARTED):
            save.settle(review_id, "none")
            return respond({"saved": False, "error": "Nothing was saved: the server is busy with another browser job. Try again in a minute."}, 409)
        save.settle(review_id, "unknown", key=key)
        print("Classtime save: state=unknown reason=" + save.classify_stop(message), flush=True)
        return respond({"saved": False, "partial_mark_may_exist": True, "do_not_retry": True,
                        "message": "The save was stopped (" + save.classify_stop(message) + "). A partial mark MAY exist in Nigri. Check Nigri before doing anything else. It will not be retried automatically."}, 500)
    except Exception:
        save.settle(review_id, "unknown", key=key)
        print("Classtime save: state=unknown reason=error", flush=True)
        return respond({"saved": False, "partial_mark_may_exist": True, "do_not_retry": True,
                        "message": "The save failed unexpectedly. A partial mark MAY exist in Nigri. Check Nigri before doing anything else. It will not be retried automatically."}, 500)
    per_boy, warnings, complete = save.interpret(outcome, planned["rows"], planned["only"] is not None)
    lines = outcome.get("results") or []
    test_id = outcome.get("test_id")
    if not test_id:
        found = next((re.search(r"testID=(\d+)", l) for l in lines if l.startswith("Mark created")), None)
        test_id = found.group(1) if found else None
    final_saved = any("Final Save! submitted" in l for l in lines)
    state = "saved" if complete else "partial"
    save.settle(review_id, state, test_id=test_id, key=key)
    print("Classtime save: state=" + state + " one_boy=" + str(planned["only"] is not None) + " test_id=" + str(test_id)
          + " saved=" + str(sum(b["outcome"] == "saved" for b in per_boy)) + " failed=" + str(sum(b["outcome"] in ("failed", "not reached") for b in per_boy)), flush=True)
    if complete:
        message = "Saved to Nigri as mark #" + str(test_id) + "."
    elif final_saved:
        message = "Partly saved: mark #" + str(test_id) + " exists in Nigri but some boys did not save. Check Nigri. It will not be retried automatically."
    else:
        message = "The save stopped partway" + ((": mark #" + str(test_id) + " was created") if test_id else "") + ". A partial mark MAY exist in Nigri. Check Nigri. It will not be retried automatically."
    return respond({"saved": complete, "partial_mark_may_exist": not complete, "do_not_retry": not complete, "nigri_writes": True,
                    "test_id": test_id, "mark_name": planned["options"]["test_name"], "class_section": result["class_section"],
                    "one_boy": planned["only"] is not None, "marking_done": planned["only"] is None,
                    "per_boy": per_boy, "warnings": warnings, "message": message}, 200 if final_saved else 500)


@app.route("/classtime/reviews/<review_id>/pdfs/<pdf_id>", methods=["GET"])
def classtime_review_pdf(review_id, pdf_id):
    from classtime_auth import review_owner
    from classtime_review import get_review
    from flask import send_file
    owner = review_owner()
    if not owner: return jsonify({"error": "unauthorized"}), 401
    job = get_review(review_id, owner)
    report = job and job["pdfs"].get(pdf_id)
    if not report: return jsonify({"error": "PDF expired or unavailable"}), 404
    response = send_file(report, mimetype="application/pdf", conditional=False, max_age=0)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Disposition"] = 'inline; filename="student-report.pdf"'
    return response


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/sync-points", methods=["POST"])
def sync_points():
    # --- auth check ---
    if not nigri_authorized(): return jsonify({"error": "unauthorized"}), 401

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
    if not nigri_authorized(): return jsonify({"error": "unauthorized"}), 401

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
    if not nigri_authorized(): return jsonify({"error": "unauthorized"}), 401

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
    if not nigri_authorized(): return jsonify({"error": "unauthorized"}), 401

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
    if not nigri_authorized(allow_query_key=True): return jsonify({"error": "unauthorized"}), 401

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
    if not nigri_authorized(allow_query_key=True): return jsonify({"error": "unauthorized"}), 401

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
    if not nigri_authorized(): return jsonify({"error": "unauthorized"}), 401

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


@app.route("/read-marks", methods=["GET", "POST"])
def read_marks_endpoint():
    """
    Authenticated read-only endpoint for existing Nigri School Marks.

    POST:
      {"class_section": "B3 ET", "limit": 100}

    GET:
      /read-marks?key=...&class=B3%20ET&limit=100

    Omit class/class_section to return both B3 ET and B3 WT.
    """
    if not nigri_authorized(allow_query_key=True): return jsonify({"error": "unauthorized"}), 401

    if request.method == "POST":
        body = request.get_json(force=True, silent=True) or {}
        class_section = body.get("class_section")
        limit = body.get("limit", 100)
    else:
        class_section = request.args.get("class") or None
        limit = request.args.get("limit", 100)

    if class_section == "":
        class_section = None
    if class_section not in (None, "B3 ET", "B3 WT"):
        return jsonify(
            {"error": "class_section must be B3 ET, B3 WT, or omitted"}
        ), 400

    try:
        result = read_marks(class_section=class_section, limit=limit)
        return jsonify({"status": "success", **result})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500


@app.route("/read-attendance", methods=["GET", "POST"])
def read_attendance_endpoint():
    """
    Authenticated READ-ONLY endpoint for Nigri Attendance History.
    No attendance checkboxes or Save controls are touched.
    """
    if not nigri_authorized(allow_query_key=True): return jsonify({"error": "unauthorized"}), 401

    try:
        result = read_attendance()
        return jsonify({"status": "success", **result})
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
    if not nigri_authorized(allow_query_key=True): return jsonify({"error": "unauthorized"}), 401

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
