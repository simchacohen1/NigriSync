"""
Read-only Nigri Attendance History reader, v3.

Changes from v2:
- Reads only real student rows (tr[childid]) so nested parent tables do not
  create duplicate student records.
- Extracts the actual date headers.
- Captures computed CSS/background information for each attendance cell so
  Nigri's icon/status classes can be mapped accurately without guessing.
"""

import re
from playwright.sync_api import sync_playwright
from nigri_playwright import NIGRI_BASE_URL, REWARDS_CHILD_IDS, login

ATTENDANCE_HISTORY_URL = (
    f"{NIGRI_BASE_URL}/main/default_os_prog.asp"
    "?section=teachers&spec=logs&xmlFile=os"
)


def _find_history_frame(page):
    """Return the frame containing Nigri's real attendance history table."""
    for frame in page.frames:
        try:
            if frame.locator("table.logsTbl tr[childid]").count() >= 3:
                return frame
        except Exception:
            pass
    raise RuntimeError("Could not locate Nigri Attendance History student rows.")


def _cell_payload(cell):
    """Read one attendance cell without changing anything."""
    try:
        return cell.evaluate(
            """el => {
                const cs = getComputedStyle(el);
                const before = getComputedStyle(el, '::before');
                const after = getComputedStyle(el, '::after');
                return {
                    text: (el.innerText || el.textContent || '').trim(),
                    html: el.innerHTML || '',
                    title: el.getAttribute('title') || '',
                    className: el.className || '',
                    backgroundImage: cs.backgroundImage || '',
                    backgroundColor: cs.backgroundColor || '',
                    color: cs.color || '',
                    beforeContent: before.content || '',
                    beforeBackgroundImage: before.backgroundImage || '',
                    afterContent: after.content || '',
                    afterBackgroundImage: after.backgroundImage || '',
                    images: Array.from(el.querySelectorAll('img')).map(img => ({
                        src: img.getAttribute('src') || '',
                        alt: img.getAttribute('alt') || '',
                        title: img.getAttribute('title') || '',
                        className: img.className || ''
                    }))
                };
            }"""
        )
    except Exception:
        return {
            "text": "",
            "html": "",
            "title": "",
            "className": "",
            "backgroundImage": "",
            "backgroundColor": "",
            "color": "",
            "beforeContent": "",
            "beforeBackgroundImage": "",
            "afterContent": "",
            "afterBackgroundImage": "",
            "images": [],
        }


def _read_dates(frame):
    """Read the actual visible date columns from currdate attributes."""
    dates = []
    headers = frame.locator("table.logsTbl thead td[currdate]")
    for i in range(headers.count()):
        h = headers.nth(i)
        try:
            dates.append(h.get_attribute("currdate"))
        except Exception:
            pass
    return [d for d in dates if d]


def _read_selects(frame):
    result = []
    nodes = frame.locator("select")
    for i in range(nodes.count()):
        sel = nodes.nth(i)
        try:
            result.append(
                sel.evaluate(
                    """el => ({
                        id: el.id || '',
                        name: el.name || '',
                        value: el.value || '',
                        selectedText:
                            el.selectedOptions && el.selectedOptions.length
                            ? el.selectedOptions[0].textContent.trim()
                            : '',
                        options: Array.from(el.options).map(o => ({
                            value: o.value,
                            text: (o.textContent || '').trim()
                        }))
                    })"""
                )
            )
        except Exception:
            pass
    return result


def read_attendance():
    """
    Read Nigri Attendance History.

    SAFETY: read-only. No attendance checkbox, Save button, or form submission
    is used anywhere in this function.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        login(page)
        page.goto(ATTENDANCE_HISTORY_URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1200)

        frame = _find_history_frame(page)
        dates = _read_dates(frame)

        rows = frame.locator("table.logsTbl tbody tr[childid]")
        students = []

        id_to_name = {str(v): k for k, v in REWARDS_CHILD_IDS.items()}

        for i in range(rows.count()):
            row = rows.nth(i)
            child_id = (row.get_attribute("childid") or "").strip()
            cells = row.locator(":scope > td")
            if cells.count() < 2:
                continue

            visible_name = ""
            try:
                visible_name = cells.nth(0).inner_text().strip()
            except Exception:
                pass

            name = id_to_name.get(child_id) or visible_name
            status_cells = [
                _cell_payload(cells.nth(j))
                for j in range(1, cells.count())
            ]

            # Align cells to actual dates. If Nigri ever returns a mismatch,
            # preserve all cells and expose the mismatch instead of guessing.
            by_date = []
            for j, cell in enumerate(status_cells):
                by_date.append({
                    "date": dates[j] if j < len(dates) else None,
                    **cell,
                })

            students.append({
                "name": name,
                "display_name": visible_name,
                "child_id": child_id,
                "days": by_date,
            })

        frame_text = frame.locator("body").inner_text()
        range_match = re.search(
            r"Logs\s+for\s+(\d{1,2}/\d{1,2}/\d{4})"
            r"\s*-\s*(\d{1,2}/\d{1,2}/\d{4})",
            frame_text,
            flags=re.I,
        )

        # Compact status-style dictionary: one example for every distinct
        # class/style combination found. This makes mapping easy.
        style_examples = {}
        for student in students:
            for day in student["days"]:
                key = day.get("className", "")
                if key not in style_examples:
                    style_examples[key] = {
                        "className": key,
                        "backgroundImage": day.get("backgroundImage"),
                        "backgroundColor": day.get("backgroundColor"),
                        "color": day.get("color"),
                        "beforeContent": day.get("beforeContent"),
                        "beforeBackgroundImage": day.get("beforeBackgroundImage"),
                        "afterContent": day.get("afterContent"),
                        "afterBackgroundImage": day.get("afterBackgroundImage"),
                    }

        result = {
            "read_only": True,
            "source_url": page.url,
            "history_frame_name": frame.name,
            "history_frame_url": frame.url,
            "range_start": range_match.group(1) if range_match else None,
            "range_end": range_match.group(2) if range_match else None,
            "dates": dates,
            "student_count": len(students),
            "students": students,
            "status_style_examples": style_examples,
            "selects": _read_selects(frame),
        }

        browser.close()
        return result
