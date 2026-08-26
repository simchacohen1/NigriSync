"""
Core automation logic. Uses Playwright (sync API) to:
  1. Log into nigrijewishonlineschool.com
  2. For the given class_section + date, go through EVERY period
     and mark all students Present
  3. On the 3rd period specifically, also set each student's
     Rewards dropdown to their BrightPath point total
  4. Save each period

Real selectors confirmed via dev-tools inspection (2026-08-26):
  - Login page:  /main/default_os_prog.asp?section=teachers
      username -> input[name="login"]
      password -> input[name="password"]
      submit   -> input[name="submit"]
  - Attendance UI lives inside a nested iframe (id="attend").
      period picker      -> select#selNewCours
      per-student present -> input[name="attend_{childID}"]
      per-student rewards -> select[name="rewardsPoints_{childID}"]
      save button          -> input#attendSubmitBtn

*** STILL TODO ***
  - Real period names/order per class (currently guessed) -> see
    STILL_NEED_PERIOD_NAMES below. Waiting on a screenshot of the
    opened "Attendance for" dropdown for B3 WT and B3 ET.
  - Never tested against the live site (no network access from this
    sandbox to nigrijewishonlineschool.com) -- run this in a real
    environment and expect to debug timing/frame issues on first pass.
"""

import os
from playwright.sync_api import sync_playwright

NIGRI_BASE_URL = "https://www.nigrijewishonlineschool.com"
NIGRI_LOGIN_URL = f"{NIGRI_BASE_URL}/main/default_os_prog.asp?section=teachers"
NIGRI_USERNAME = os.environ.get("NIGRI_USERNAME")
NIGRI_PASSWORD = os.environ.get("NIGRI_PASSWORD")

# Confirmed real period names/order from the "Attendance for" dropdown
# (2026-08-26). Friday Class 1/2/3 exist too but are skipped here since
# they're only used on Fridays -- not part of the regular daily sync.
CLASS_PERIODS = {
    "B3 WT": [
        "B3 WT - Davening",
        "B3 WT - Morning Class 1",
        "B3 WT - Morning Class 2",
        "B3 WT - Morning Class 3",   # <- points go here
    ],
    "B3 ET": [
        "B3 ET - Davening",
        "B3 ET - Morning Class 1",
        "B3 ET - Morning Class 2",
        "B3 ET - Morning Class 3",   # <- points go here
    ],
}

POINTS_PERIOD_INDEX = 3  # 0-indexed -> "Morning Class 3"


def login(page):
    page.goto(NIGRI_LOGIN_URL)
    page.fill('input[name="login"]', NIGRI_USERNAME)
    page.fill('input[name="password"]', NIGRI_PASSWORD)
    page.click('input[name="submit"]')
    page.wait_for_load_state("networkidle")


def get_attendance_frame(page):
    """
    The attendance form lives inside a nested iframe. Rather than
    hardcode a frame name (which may not be stable), find whichever
    frame currently contains the attendance form.
    """
    page.wait_for_timeout(500)
    for frame in page.frames:
        try:
            if frame.query_selector("form#attendFrm") or frame.query_selector("select#selNewCours"):
                return frame
        except Exception:
            continue
    raise RuntimeError("Could not locate the attendance iframe on the page")


def go_to_attendance_tab(page):
    page.click("text=Attendance")
    page.wait_for_load_state("networkidle")


def select_period(page, period_label, date_str):
    frame = get_attendance_frame(page)
    # select_option operates on the underlying <select> DOM node directly,
    # so it works even though the dropdown is visually a select2 widget.
    frame.select_option("select#selNewCours", label=period_label)
    page.wait_for_load_state("networkidle")
    frame = get_attendance_frame(page)

    # The page defaults to TODAY's date automatically when a period is
    # selected (confirmed via screenshot), and this sync always targets
    # today, so no calendar navigation is needed. As a safety check, log
    # a warning (but don't fail) if the displayed date doesn't look like
    # it matches what we expect -- better to proceed than hard-crash on
    # a brittle calendar click.
    try:
        page_text = frame.locator("body").inner_text(timeout=5000)
        year, month, day = date_str.split("-")
        day_num = str(int(day))
        if day_num not in page_text:
            print(f"WARNING: day '{day_num}' not found in attendance page text; "
                  f"proceeding anyway since page should default to today")
    except Exception as e:
        print(f"WARNING: could not verify displayed date ({e}); proceeding anyway")

    return frame


def mark_present_all(frame, student_names):
    # Expand every row so checkboxes are interactable
    expand_all = frame.locator("text=Expand all")
    if expand_all.count() > 0:
        expand_all.first.click()

    for name in student_names:
        row = frame.locator(f"tr:has-text('{name}')").first
        checkbox = row.locator('input[type="checkbox"][name^="attend_"]').first
        checkbox.check()


def set_points_for_period(frame, students_with_points):
    for student in students_with_points:
        name = student["name"]
        points = str(student["points"])
        row = frame.locator(f"tr:has-text('{name}')").first
        rewards_select = row.locator('select[name^="rewardsPoints_"]').first
        rewards_select.select_option(points)


def save_period(page, frame):
    frame.click("input#attendSubmitBtn")
    page.wait_for_load_state("networkidle")


def run_sync(class_section, date, students):
    if class_section not in CLASS_PERIODS:
        raise ValueError(f"Unknown class_section: {class_section}")

    periods = CLASS_PERIODS[class_section]
    student_names = [s["name"] for s in students]
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        login(page)
        go_to_attendance_tab(page)

        for idx, period_name in enumerate(periods):
            frame = select_period(page, period_name, date)
            mark_present_all(frame, student_names)

            if idx == POINTS_PERIOD_INDEX:
                set_points_for_period(frame, students)

            save_period(page, frame)
            results.append(
                f"{period_name}: attendance saved"
                + (" + points" if idx == POINTS_PERIOD_INDEX else "")
            )

        browser.close()

    return results
