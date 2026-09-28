"""
Read-only Nigri Attendance History reader — validated custom-window version.

Key fix:
Nigri ignores customStartDate/customEndDate unless daysBack=-1 ("Custom").
Previous weekly-window code used daysBack=6, so every request silently showed the
same current 7-day period. This version uses Custom mode and validates that the
page's own "Logs for X-Y" range matches each requested window before accepting it.

Safety:
- Read-only.
- Uses Attendance History display/filter parameters only.
- Never touches attendance-entry checkboxes or Save controls.
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

SCHOOL_YEAR_START = datetime.date(2026, 8, 25)


def _fmt(d):
    return f"{d.month}/{d.day}/{d.year}"


def _parse_mdy(s):
    return datetime.datetime.strptime(s, "%m/%d/%Y").date()


def _find_history_frame(page):
    for frame in page.frames:
        try:
            if "teacherAdmin.asp" in frame.url and "logs=list" in frame.url:
                return frame
        except Exception:
            pass
    raise RuntimeError("Could not locate Nigri Attendance History frame.")


def _build_custom_window_url(frame_url, start_date, end_date):
    parts = urlsplit(frame_url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))

    params["logs"] = "list"
    params["xmlFile"] = "os"

    # CRITICAL: -1 is Nigri's confirmed "Custom" value.
    params["daysBack"] = "-1"
    params["customStartDate"] = _fmt(start_date)
    params["customEndDate"] = _fmt(end_date)

    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(params),
        parts.fragment,
    ))


def _read_page_range(frame):
    try:
        txt = frame.locator("body").inner_text(timeout=1500)
    except Exception:
        return None, None

    m = re.search(
        r"Logs\s+for\s+(\d{1,2}/\d{1,2}/\d{4})"
        r"\s*-\s*(\d{1,2}/\d{1,2}/\d{4})",
        txt,
        flags=re.I,
    )
    if not m:
        return None, None
    return m.group(1), m.group(2)


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

    # Confirmed from live output:
    # sf2 + vcheck.gif = normal green check = Present
    # sf4 + vcheckd.gif = red X = Absent
    if "sf4" in cls or "vcheckd.gif" in bg:
        return "absent"

    if "sf2" in cls and "sf2n" not in cls and "vcheck.gif" in bg:
        return "present"

    # Preserve special statuses without inventing labels.
    return "unknown"


def _read_dates(frame):
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


def _read_students(frame, dates):
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
        day_records = []

        for j in range(1, cells.count()):
            payload = _cell_payload(cells.nth(j))
            payload["date"] = dates[j - 1] if (j - 1) < len(dates) else None
            payload["status"] = _normalized_status(payload)
            day_records.append(payload)

        students[child_id] = {
            "name": name,
            "display_name": display_name,
            "child_id": child_id,
            "days": day_records,
        }

    return students


def _load_validated_window(frame, page, base_url, start_date, end_date):
    requested_start = _fmt(start_date)
    requested_end = _fmt(end_date)
    target_url = _build_custom_window_url(base_url, start_date, end_date)

    frame.goto(target_url)
    frame.wait_for_load_state("networkidle")

    # Poll because this legacy page can populate after navigation.
    actual_start = actual_end = None
    for _ in range(24):
        actual_start, actual_end = _read_page_range(frame)
        rows = frame.locator("table.logsTbl tbody tr[childid]").count()
        if actual_start and actual_end and rows:
            break
        page.wait_for_timeout(250)

    dates = _read_dates(frame)
    students = _read_students(frame, dates)

    ok = (
        actual_start == requested_start
        and actual_end == requested_end
        and len(students) == 16
    )

    return {
        "ok": ok,
        "requested_start": requested_start,
        "requested_end": requested_end,
        "actual_start": actual_start,
        "actual_end": actual_end,
        "dates": dates,
        "students": students,
        "url": frame.url,
    }


def read_attendance():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        login(page)
        page.goto(ATTENDANCE_HISTORY_URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)

        frame = _find_history_frame(page)
        base_url = frame.url

        today = datetime.date.today()
        start = SCHOOL_YEAR_START if SCHOOL_YEAR_START <= today else today

        # Non-overlapping 7-day custom windows.
        windows = []
        cursor = start
        while cursor <= today:
            end = min(cursor + datetime.timedelta(days=6), today)
            windows.append((cursor, end))
            cursor = end + datetime.timedelta(days=1)

        merged = {}
        all_dates = []
        window_results = []
        failed_windows = []

        for start_date, end_date in windows:
            result = _load_validated_window(
                frame, page, base_url, start_date, end_date
            )

            window_results.append({
                "requested_start": result["requested_start"],
                "requested_end": result["requested_end"],
                "actual_start": result["actual_start"],
                "actual_end": result["actual_end"],
                "date_count": len(result["dates"]),
                "student_count": len(result["students"]),
                "ok": result["ok"],
            })

            # Never merge a window if Nigri ignored our requested dates.
            if not result["ok"]:
                failed_windows.append(window_results[-1])
                continue

            for d in result["dates"]:
                if d not in all_dates:
                    all_dates.append(d)

            for child_id, student in result["students"].items():
                dest = merged.setdefault(child_id, {
                    "name": student["name"],
                    "display_name": student["display_name"],
                    "child_id": child_id,
                    "days": [],
                })

                existing = {x.get("date") for x in dest["days"]}
                for day in student["days"]:
                    if day.get("date") and day.get("date") not in existing:
                        dest["days"].append(day)
                        existing.add(day.get("date"))

        all_dates.sort(key=_parse_mdy)

        students_out = list(merged.values())
        students_out.sort(key=lambda s: s["name"])

        for student in students_out:
            student["days"].sort(
                key=lambda x: _parse_mdy(x["date"])
                if x.get("date")
                else datetime.date.min
            )

        summaries = {}
        for student in students_out:
            counts = {"present": 0, "absent": 0, "unknown": 0}
            for day in student["days"]:
                status = day.get("status", "unknown")
                counts[status] = counts.get(status, 0) + 1
            summaries[student["name"]] = counts

        response = {
            "read_only": True,
            "strategy": "validated_custom_weekly_windows",
            "school_year_start": _fmt(start),
            "range_end": _fmt(today),
            "window_count": len(windows),
            "successful_window_count": len(windows) - len(failed_windows),
            "failed_window_count": len(failed_windows),
            "failed_windows": failed_windows,
            "window_results": window_results,
            "dates": all_dates,
            "date_count": len(all_dates),
            "student_count": len(students_out),
            "students": students_out,
            "summaries": summaries,
        }

        browser.close()
        return response
