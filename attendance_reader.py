"""
Read-only Nigri Attendance History reader, v4.

This version automatically switches Attendance History to "Last 60 days"
before reading it. For the current school year (which began in late August),
that covers the full year-to-date attendance history.

Safety: read-only. It changes only the History page's display filter; it never
touches attendance-entry checkboxes, Save buttons, or attendance forms.
"""

import re
from playwright.sync_api import sync_playwright
from nigri_playwright import NIGRI_BASE_URL, REWARDS_CHILD_IDS, login

ATTENDANCE_HISTORY_URL = (
    f"{NIGRI_BASE_URL}/main/default_os_prog.asp"
    "?section=teachers&spec=logs&xmlFile=os"
)

# Nigri's confirmed "Last 60 days" option value.
DAYS_BACK_VALUE = "59"


def _find_history_frame(page):
    """Return the frame containing Nigri's real attendance-history controls/table."""
    for frame in page.frames:
        try:
            if frame.locator("select#daysBack").count() and frame.locator("table.logsTbl").count():
                return frame
        except Exception:
            pass

    # On a very fast load, the table may not be populated yet. Accept the
    # controls frame and let the caller wait/reload it.
    for frame in page.frames:
        try:
            if frame.locator("select#daysBack").count():
                return frame
        except Exception:
            pass

    raise RuntimeError("Could not locate Nigri Attendance History frame.")


def _set_60_day_history(page):
    """
    Set the History display to Last 60 days and apply it.

    This is a READ-ONLY display/filter action. It does not alter attendance.
    """
    frame = _find_history_frame(page)

    current = None
    try:
        current = frame.locator("select#daysBack").input_value()
    except Exception:
        pass

    if current != DAYS_BACK_VALUE:
        frame.locator("select#daysBack").select_option(DAYS_BACK_VALUE)
        frame.page.wait_for_timeout(250)

        # Nigri shows an explicit Go! button beside the date-range dropdown.
        go = frame.locator('input[type="button"][value*="Go"]')
        if go.count():
            go.first.click()

        # The inner frame can refresh/reload after applying the filter.
        page.wait_for_timeout(1500)

    # Always reacquire the live frame after the filter action.
    return _find_history_frame(page)


def _cell_payload(cell):
    """Read one attendance-history cell without changing anything."""
    try:
        return cell.evaluate(
            """el => {
                const cs = getComputedStyle(el);
                return {
                    text: (el.innerText || el.textContent || '').trim(),
                    html: el.innerHTML || '',
                    title: el.getAttribute('title') || '',
                    className: el.className || '',
                    backgroundImage: cs.backgroundImage || '',
                    backgroundColor: cs.backgroundColor || '',
                    color: cs.color || '',
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
            "images": [],
        }


def _read_dates(frame):
    """Read the real date columns from Nigri's currdate attributes."""
    dates = []
    headers = frame.locator("table.logsTbl thead td[currdate]")
    for i in range(headers.count()):
        try:
            value = headers.nth(i).get_attribute("currdate")
            if value:
                dates.append(value)
        except Exception:
            pass
    return dates


def _normalized_status(cell):
    """
    Normalize only statuses we have positively identified.

    sf2 / vcheck.gif  = normal green check = Present
    sf4 / vcheckd.gif = red X = Absent

    Other Nigri variants remain 'unknown' until their meaning is confirmed.
    """
    cls = (cell.get("className") or "").split()
    bg = (cell.get("backgroundImage") or "").lower()

    if "sf4" in cls or "vcheckd.gif" in bg:
        return "absent"
    if "sf2" in cls and "sf2n" not in cls and "vcheck.gif" in bg:
        return "present"
    return "unknown"


def read_attendance():
    """
    Read up to 60 days of Nigri Attendance History.

    The endpoint returns:
      - date columns actually present in Nigri
      - all 16 B3 students
      - per-date status cells
      - conservative normalized status (present / absent / unknown)
      - raw CSS/icon metadata for still-unmapped statuses

    SAFETY: read-only.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        login(page)
        page.goto(ATTENDANCE_HISTORY_URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)

        frame = _set_60_day_history(page)
        page.wait_for_timeout(750)

        # If the table is still loading, wait briefly for student rows.
        try:
            frame.locator("table.logsTbl tbody tr[childid]").first.wait_for(
                state="attached", timeout=5000
            )
        except Exception:
            # Reacquire once more in case the frame was replaced.
            frame = _find_history_frame(page)

        dates = _read_dates(frame)
        rows = frame.locator("table.logsTbl tbody tr[childid]")

        id_to_name = {str(v): k for k, v in REWARDS_CHILD_IDS.items()}
        students = []

        for i in range(rows.count()):
            row = rows.nth(i)
            child_id = (row.get_attribute("childid") or "").strip()
            cells = row.locator(":scope > td")
            if cells.count() < 2:
                continue

            try:
                display_name = cells.nth(0).inner_text().strip()
            except Exception:
                display_name = ""

            name = id_to_name.get(child_id) or display_name
            day_cells = []

            for j in range(1, cells.count()):
                payload = _cell_payload(cells.nth(j))
                payload["date"] = dates[j - 1] if (j - 1) < len(dates) else None
                payload["status"] = _normalized_status(payload)
                day_cells.append(payload)

            students.append({
                "name": name,
                "display_name": display_name,
                "child_id": child_id,
                "days": day_cells,
            })

        frame_text = ""
        try:
            frame_text = frame.locator("body").inner_text()
        except Exception:
            pass

        range_match = re.search(
            r"Logs\s+for\s+(\d{1,2}/\d{1,2}/\d{4})"
            r"\s*-\s*(\d{1,2}/\d{1,2}/\d{4})",
            frame_text,
            flags=re.I,
        )

        # Compact summary by student, useful for the unified record.
        summaries = {}
        for student in students:
            counts = {"present": 0, "absent": 0, "unknown": 0}
            for day in student["days"]:
                status = day.get("status", "unknown")
                counts[status] = counts.get(status, 0) + 1
            summaries[student["name"]] = counts

        result = {
            "read_only": True,
            "history_days_requested": 60,
            "source_url": page.url,
            "history_frame_name": frame.name,
            "history_frame_url": frame.url,
            "range_start": range_match.group(1) if range_match else None,
            "range_end": range_match.group(2) if range_match else None,
            "dates": dates,
            "date_count": len(dates),
            "student_count": len(students),
            "students": students,
            "summaries": summaries,
        }

        browser.close()
        return result
