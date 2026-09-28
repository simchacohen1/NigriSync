"""
Read-only Nigri Attendance History reader, robust weekly-window version.

Why this approach:
Nigri reliably returns populated attendance rows for its 7-day history view,
but the 60-day view can return only the range header with no student table.
Instead of relying on that flaky long-range rendering, this reader requests
the history in consecutive 7-day windows and combines the results.

Safety:
- Read-only.
- Uses only the Attendance History display URL.
- Never touches attendance-entry checkboxes, Save buttons, or forms.
"""

import re
import datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from playwright.sync_api import sync_playwright
from nigri_playwright import NIGRI_BASE_URL, REWARDS_CHILD_IDS, login

ATTENDANCE_HISTORY_URL = (
    f"{NIGRI_BASE_URL}/main/default_os_prog.asp"
    "?section=teachers&spec=logs&xmlFile=os"
)

# Current school year started in late August 2026.
# Pull from Aug 25 through today in reliable 7-day windows.
SCHOOL_YEAR_START = datetime.date(2026, 8, 25)


def _find_history_frame(page):
    for frame in page.frames:
        try:
            if "teacherAdmin.asp" in frame.url and "logs=list" in frame.url:
                return frame
        except Exception:
            pass
    raise RuntimeError("Could not locate Nigri Attendance History frame.")


def _build_window_url(frame_url, start_date, end_date):
    """
    Preserve Nigri's working history URL parameters, but force a 7-day-style
    custom window. This mirrors the shape of the URL that already returned
    populated student data successfully.
    """
    parts = urlsplit(frame_url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))

    params["logs"] = "list"
    params["xmlFile"] = "os"
    params["daysBack"] = "6"
    params["customStartDate"] = f"{start_date.month}/{start_date.day}/{start_date.year}"
    params["customEndDate"] = f"{end_date.month}/{end_date.day}/{end_date.year}"

    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(params),
        parts.fragment,
    ))


def _fmt_date(d):
    return f"{d.month}/{d.day}/{d.year}"


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
                    color: cs.color || ''
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
        }


def _normalized_status(cell):
    cls = (cell.get("className") or "").split()
    bg = (cell.get("backgroundImage") or "").lower()

    if "sf4" in cls or "vcheckd.gif" in bg:
        return "absent"

    if "sf2" in cls and "sf2n" not in cls and "vcheck.gif" in bg:
        return "present"

    return "unknown"


def _read_one_window(frame):
    dates = []
    headers = frame.locator("table.logsTbl thead td[currdate]")
    for i in range(headers.count()):
        try:
            d = headers.nth(i).get_attribute("currdate")
            if d:
                dates.append(d)
        except Exception:
            pass

    rows = frame.locator("table.logsTbl tbody tr[childid]")
    id_to_name = {str(v): k for k, v in REWARDS_CHILD_IDS.items()}
    students = {}

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
        days = []

        for j in range(1, cells.count()):
            payload = _cell_payload(cells.nth(j))
            payload["date"] = dates[j - 1] if (j - 1) < len(dates) else None
            payload["status"] = _normalized_status(payload)
            days.append(payload)

        students[child_id] = {
            "name": name,
            "display_name": display_name,
            "child_id": child_id,
            "days": days,
        }

    return dates, students


def read_attendance():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        login(page)
        page.goto(ATTENDANCE_HISTORY_URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)

        frame = _find_history_frame(page)
        base_frame_url = frame.url

        today = datetime.date.today()
        start = SCHOOL_YEAR_START
        if start > today:
            start = today - datetime.timedelta(days=59)

        # Build non-overlapping 7-day windows.
        windows = []
        cursor = start
        while cursor <= today:
            end = min(cursor + datetime.timedelta(days=6), today)
            windows.append((cursor, end))
            cursor = end + datetime.timedelta(days=1)

        all_dates = []
        merged = {}
        window_results = []

        for start_date, end_date in windows:
            url = _build_window_url(base_frame_url, start_date, end_date)

            frame.goto(url)
            frame.wait_for_load_state("networkidle")

            # Old Nigri pages can populate slightly after navigation.
            # Poll for either rows or a "Logs for..." range header.
            for _ in range(20):
                row_count = frame.locator("table.logsTbl tbody tr[childid]").count()
                body_text = ""
                try:
                    body_text = frame.locator("body").inner_text(timeout=500)
                except Exception:
                    pass
                if row_count or "Logs for" in body_text:
                    # Give dynamic table population one extra moment.
                    page.wait_for_timeout(350)
                    break
                page.wait_for_timeout(250)

            dates, students = _read_one_window(frame)

            window_results.append({
                "start": _fmt_date(start_date),
                "end": _fmt_date(end_date),
                "date_count": len(dates),
                "student_count": len(students),
                "url": frame.url,
            })

            for d in dates:
                if d not in all_dates:
                    all_dates.append(d)

            for child_id, student in students.items():
                dest = merged.setdefault(child_id, {
                    "name": student["name"],
                    "display_name": student["display_name"],
                    "child_id": child_id,
                    "days": [],
                })

                existing_dates = {x.get("date") for x in dest["days"]}
                for day in student["days"]:
                    if day.get("date") and day.get("date") not in existing_dates:
                        dest["days"].append(day)
                        existing_dates.add(day.get("date"))

        # Sort dates chronologically and each student's day records to match.
        def parse_mdy(s):
            try:
                return datetime.datetime.strptime(s, "%m/%d/%Y").date()
            except Exception:
                return datetime.date.min

        all_dates.sort(key=parse_mdy)

        students_out = list(merged.values())
        students_out.sort(key=lambda s: s["name"])
        for student in students_out:
            student["days"].sort(key=lambda d: parse_mdy(d.get("date") or ""))

        summaries = {}
        for student in students_out:
            counts = {"present": 0, "absent": 0, "unknown": 0}
            for day in student["days"]:
                status = day.get("status", "unknown")
                counts[status] = counts.get(status, 0) + 1
            summaries[student["name"]] = counts

        result = {
            "read_only": True,
            "strategy": "weekly_windows",
            "school_year_start": _fmt_date(start),
            "range_end": _fmt_date(today),
            "window_count": len(windows),
            "window_results": window_results,
            "dates": all_dates,
            "date_count": len(all_dates),
            "student_count": len(students_out),
            "students": students_out,
            "summaries": summaries,
        }

        browser.close()
        return result
