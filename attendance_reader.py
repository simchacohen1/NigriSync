"""
Read-only Nigri Attendance History reader, v5.

Fix for Last-60-days:
Nigri's JavaScript was keeping stale customStartDate/customEndDate values in
the generated URL. v5 navigates the already-authenticated history iframe
directly with daysBack=59 and REMOVES those stale custom-date parameters.

Safety: read-only. This only changes the Attendance History display URL.
It never touches attendance-entry controls or Save buttons.
"""

import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from playwright.sync_api import sync_playwright
from nigri_playwright import NIGRI_BASE_URL, REWARDS_CHILD_IDS, login

ATTENDANCE_HISTORY_URL = (
    f"{NIGRI_BASE_URL}/main/default_os_prog.asp"
    "?section=teachers&spec=logs&xmlFile=os"
)

DAYS_BACK_VALUE = "59"


def _find_history_frame(page):
    for frame in page.frames:
        try:
            if "teacherAdmin.asp" in frame.url and "logs=list" in frame.url:
                return frame
        except Exception:
            pass
    raise RuntimeError("Could not locate Nigri Attendance History frame.")


def _build_60_day_url(frame_url):
    parts = urlsplit(frame_url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))

    params["logs"] = "list"
    params["xmlFile"] = "os"
    params["daysBack"] = DAYS_BACK_VALUE

    # These stale values caused Nigri to keep the old 7-day custom range
    # even after daysBack was changed to 59.
    params.pop("customStartDate", None)
    params.pop("customEndDate", None)

    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(params),
        parts.fragment,
    ))


def _cell_payload(cell):
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
    dates = []
    headers = frame.locator("table.logsTbl thead td[currdate]")
    for i in range(headers.count()):
        try:
            d = headers.nth(i).get_attribute("currdate")
            if d:
                dates.append(d)
        except Exception:
            pass
    return dates


def _normalized_status(cell):
    cls = (cell.get("className") or "").split()
    bg = (cell.get("backgroundImage") or "").lower()

    if "sf4" in cls or "vcheckd.gif" in bg:
        return "absent"

    if "sf2" in cls and "sf2n" not in cls and "vcheck.gif" in bg:
        return "present"

    return "unknown"


def read_attendance():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        login(page)
        page.goto(ATTENDANCE_HISTORY_URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)

        frame = _find_history_frame(page)

        # Navigate the authenticated inner history frame directly to a clean
        # 60-day URL so stale custom dates cannot override daysBack=59.
        clean_url = _build_60_day_url(frame.url)
        frame.goto(clean_url)
        frame.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)

        # Reacquire in case Nigri replaced the iframe document.
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
