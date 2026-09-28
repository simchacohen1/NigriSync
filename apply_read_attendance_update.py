from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / 'app.py'
PW = ROOT / 'nigri_playwright.py'

if not APP.exists() or not PW.exists():
    raise SystemExit('Put this file inside your NigriSync folder, next to app.py and nigri_playwright.py, then run it.')

app = APP.read_text(encoding='utf-8')
pw = PW.read_text(encoding='utf-8')

needle = 'MARKS_URL = f"{NIGRI_BASE_URL}/main/default_os_prog.asp?section=teachers&subSection=tests&side=1"\n'
addition = needle + 'ATTENDANCE_HISTORY_URL = f"{NIGRI_BASE_URL}/main/default_os_prog.asp?section=teachers&spec=logs&xmlFile=os"\n'
if 'ATTENDANCE_HISTORY_URL =' not in pw:
    if needle not in pw:
        raise SystemExit('Could not find MARKS_URL in nigri_playwright.py; no changes made.')
    pw = pw.replace(needle, addition, 1)

attendance_code = r'''

# ---------------------------------------------------------------------------
# READ-ONLY ATTENDANCE HISTORY
# ---------------------------------------------------------------------------

def _attendance_history_table(page):
    student_names = list(REWARDS_CHILD_IDS.keys())
    tables = page.locator("table")
    best = None
    best_score = 0
    for i in range(tables.count()):
        table = tables.nth(i)
        try:
            txt = table.inner_text(timeout=1000)
        except Exception:
            continue
        score = sum(1 for name in student_names if name in txt)
        if score > best_score:
            best = table
            best_score = score
    if best is None or best_score < 3:
        raise RuntimeError("Could not locate the Attendance History table containing B3 students.")
    return best


def _attendance_cell_payload(cell):
    try:
        return cell.evaluate("""el => ({
            text: (el.innerText || el.textContent || '').trim(),
            html: el.innerHTML || '',
            images: Array.from(el.querySelectorAll('img')).map(img => ({
                src: img.getAttribute('src') || '',
                alt: img.getAttribute('alt') || '',
                title: img.getAttribute('title') || '',
                className: img.className || ''
            })),
            title: el.getAttribute('title') || '',
            className: el.className || ''
        })""")
    except Exception:
        return {"text":"", "html":"", "images":[], "title":"", "className":""}


def read_attendance():
    # SAFETY: read-only; this never touches attendance checkboxes or Save.
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        login(page)
        page.goto(ATTENDANCE_HISTORY_URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(800)

        table = _attendance_history_table(page)
        rows = table.locator("tr")
        raw_rows = []
        students = []
        known_names = set(REWARDS_CHILD_IDS.keys())

        for r in range(rows.count()):
            row = rows.nth(r)
            cells = row.locator("th,td")
            payloads = [_attendance_cell_payload(cells.nth(i)) for i in range(cells.count())]
            if not payloads:
                continue
            raw_rows.append(payloads)
            row_text = " ".join(x.get("text", "") for x in payloads).strip()
            matched_name = next((name for name in known_names if name in row_text), None)
            if not matched_name:
                continue
            name_index = next((i for i,x in enumerate(payloads) if matched_name in x.get("text", "")), 0)
            students.append({
                "name": matched_name,
                "child_id": REWARDS_CHILD_IDS.get(matched_name),
                "cells": payloads[name_index + 1:],
                "raw_row": payloads,
            })

        page_text = page.locator("body").inner_text()
        range_match = re.search(
            r"Logs\\s+for\\s+(\\d{1,2}/\\d{1,2}/\\d{4})\\s*-\\s*(\\d{1,2}/\\d{1,2}/\\d{4})",
            page_text,
            flags=re.I,
        )

        selects = []
        nodes = page.locator("select")
        for i in range(nodes.count()):
            sel = nodes.nth(i)
            try:
                selects.append(sel.evaluate("""el => ({
                    id: el.id || '',
                    name: el.name || '',
                    value: el.value || '',
                    selectedText: el.selectedOptions && el.selectedOptions.length ? el.selectedOptions[0].textContent.trim() : '',
                    options: Array.from(el.options).map(o => ({value:o.value, text:(o.textContent||'').trim()}))
                })"""))
            except Exception:
                pass

        result = {
            "read_only": True,
            "source_url": page.url,
            "range_start": range_match.group(1) if range_match else None,
            "range_end": range_match.group(2) if range_match else None,
            "student_count": len(students),
            "students": students,
            "raw_rows": raw_rows,
            "selects": selects,
        }
        browser.close()
        return result
'''

if 'def read_attendance(' not in pw:
    pw += attendance_code

if '    read_attendance,' not in app:
    import_needle = '    read_marks,\n'
    if import_needle not in app:
        raise SystemExit('Could not find read_marks import in app.py; no changes made.')
    app = app.replace(import_needle, import_needle + '    read_attendance,\n', 1)

endpoint = r'''

@app.route("/read-attendance", methods=["GET", "POST"])
def read_attendance_endpoint():
    provided_key = request.headers.get("X-Sync-Key") or request.args.get("key")
    if not SYNC_API_KEY or provided_key != SYNC_API_KEY:
        return jsonify({"error": "unauthorized"}), 401
    try:
        result = read_attendance()
        return jsonify({"status": "success", **result})
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500
'''

if '@app.route("/read-attendance"' not in app:
    insert_before = '\n@app.route("/debug-marks-full"'
    if insert_before in app:
        app = app.replace(insert_before, endpoint + insert_before, 1)
    else:
        main_needle = '\nif __name__ == "__main__":'
        if main_needle not in app:
            raise SystemExit('Could not find insertion point in app.py; no changes made.')
        app = app.replace(main_needle, endpoint + main_needle, 1)

(APP.parent / 'app.py.before-attendance').write_text(APP.read_text(encoding='utf-8'), encoding='utf-8')
(PW.parent / 'nigri_playwright.py.before-attendance').write_text(PW.read_text(encoding='utf-8'), encoding='utf-8')
APP.write_text(app, encoding='utf-8')
PW.write_text(pw, encoding='utf-8')

print('SUCCESS')
print('Updated app.py and nigri_playwright.py')
print('Backups created: app.py.before-attendance and nigri_playwright.py.before-attendance')
