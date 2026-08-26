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


def get_list_frame(page):
    """
    Finds the frame that contains the period picker (select#selNewCours).
    This frame ALSO contains two child iframes: #cal (calendar) and
    #attend (the actual student checkboxes/rewards form) -- those live
    one level deeper and must be fetched separately via get_attend_frame.
    """
    page.wait_for_timeout(500)
    for frame in page.frames:
        try:
            if frame.query_selector("select#selNewCours"):
                return frame
        except Exception:
            continue
    raise RuntimeError("Could not locate the frame containing the period picker")


def get_attend_frame(page, timeout_ms=8000, poll_ms=300):
    """
    The actual attendance checkboxes/rewards/save-button form lives in
    an iframe named "attend" (confirmed via debug HTML dump), nested
    inside the list frame. Playwright's page.frame(name=...) finds a
    frame by name anywhere in the page's frame tree regardless of
    nesting depth, so we don't need to manually walk the hierarchy.

    The site sometimes takes longer than expected to swap in the new
    "attend" iframe after the period picker changes, so this polls
    for it instead of giving up after a single fixed-length wait.
    """
    waited = 0
    frame = page.frame(name="attend")
    while frame is None and waited < timeout_ms:
        page.wait_for_timeout(poll_ms)
        waited += poll_ms
        frame = page.frame(name="attend")

    if frame is None:
        raise RuntimeError(
            f"Could not locate the 'attend' iframe on the page after {timeout_ms}ms"
        )

    # Also make sure the frame has actually finished loading its own
    # content (not just been created as an empty placeholder) by
    # waiting for a known element inside it.
    try:
        frame.wait_for_selector("form#attendFrm", timeout=timeout_ms)
    except Exception:
        pass  # fall through and let the caller's own logic surface any real problem

    return frame


def go_to_attendance_tab(page):
    page.click("text=Attendance")
    page.wait_for_load_state("networkidle")


def select_period(page, period_label, date_str):
    list_frame = get_list_frame(page)
    # select_option operates on the underlying <select> DOM node directly,
    # so it works even though the dropdown is visually a select2 widget.
    # Changing it navigates the list_frame itself (document.location=...),
    # which also causes its child iframes (#cal, #attend) to reload with
    # the new classID.
    list_frame.select_option("select#selNewCours", label=period_label)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)  # let child iframes finish reloading

    # The #attend iframe's src URL already includes today's day (dy=26,
    # etc) automatically -- confirmed via debug HTML dump -- so no
    # calendar click is needed since this sync always targets today.
    return get_attend_frame(page)


def mark_present_all(attend_frame, student_names):
    # Expand every row so checkboxes are interactable
    expand_all = attend_frame.locator("text=Expand all")
    if expand_all.count() > 0:
        expand_all.first.click()
        attend_frame.page.wait_for_timeout(300)

    # Prefer the page's own "Select all" bulk action -- far more reliable
    # than checking each row individually by name match.
    select_all = attend_frame.locator("text=Select all")
    if select_all.count() > 0:
        select_all.first.click()
        # Give the page time to reveal each row's rewards dropdown --
        # these are hidden until the checkbox is checked, and that
        # reveal isn't necessarily instant across all rows.
        attend_frame.page.wait_for_timeout(800)
        return

    # Fallback: check each student's box individually by name match.
    for name in student_names:
        row = attend_frame.locator(f"tr:has-text('{name}')").first
        checkbox = row.locator('input[type="checkbox"][name^="attend_"]').first
        checkbox.check()
    attend_frame.page.wait_for_timeout(500)


def set_points_for_period(attend_frame, students_with_points):
    for student in students_with_points:
        name = student["name"]
        points = str(student["points"])
        row = attend_frame.locator(f"tr:has-text('{name}')").first
        rewards_select = row.locator('select[name^="rewardsPoints_"]').first
        # Wait for this specific dropdown to actually be visible/attached
        # before trying to set it -- it's hidden until the row's
        # checkbox is checked, and may render slightly after the
        # checkbox click completes.
        rewards_select.wait_for(state="visible", timeout=5000)
        rewards_select.select_option(points)

        # Explicitly fire change/input/blur events in case the page's
        # own JS state (e.g. a running point total, or a "dirty" flag
        # needed for save) only updates on those events rather than
        # from Playwright's internal value-set.
        rewards_select.evaluate(
            """(el) => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.blur();
            }"""
        )
        attend_frame.page.wait_for_timeout(150)


def save_period(page, attend_frame):
    save_btn = attend_frame.locator("input#attendSubmitBtn")
    save_btn.wait_for(state="visible", timeout=5000)
    save_btn.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)


def debug_points_attempt(class_section, date, students):
    """
    Diagnostic: logs in, goes to the POINTS period (Morning Class 3),
    selects all students, then tries to set points -- but instead of
    saving, it captures each rewards dropdown's outerHTML (including
    the actual <option> list and whichever value ended up selected)
    so we can see exactly what the automation sees at that moment,
    before any save/reload can reset/mask it.
    """
    if class_section not in CLASS_PERIODS:
        raise ValueError(f"Unknown class_section: {class_section}")

    period_name = CLASS_PERIODS[class_section][POINTS_PERIOD_INDEX]
    student_names = [s["name"] for s in students]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        login(page)
        go_to_attendance_tab(page)
        attend_frame = select_period(page, period_name, date)
        mark_present_all(attend_frame, student_names)

        report = []
        errors = []
        for student in students:
            name = student["name"]
            points = str(student["points"])
            row = attend_frame.locator(f"tr:has-text('{name}')").first
            rewards_select = row.locator('select[name^="rewardsPoints_"]').first
            entry = {"name": name, "target_points": points}
            try:
                entry["count_found"] = rewards_select.count()
                entry["is_visible"] = rewards_select.is_visible()
                entry["outer_html_before"] = rewards_select.evaluate("el => el.outerHTML")
                rewards_select.select_option(points)
                entry["value_after_select"] = rewards_select.evaluate("el => el.value")
            except Exception as e:
                errors.append(f"{name}: {str(e)}")
            report.append(entry)

        browser.close()

    return {"period": period_name, "rows": report, "errors": errors}


def debug_attendance_page(class_section, date, target="attend"):
    """
    Diagnostic helper: logs in, navigates to the FIRST period of the
    given class_section, and returns the raw HTML of either the list
    frame (target="list") or the attend frame (target="attend",
    default) -- whichever we currently need to inspect real selectors
    on, instead of guessing.
    """
    if class_section not in CLASS_PERIODS:
        raise ValueError(f"Unknown class_section: {class_section}")

    period_name = CLASS_PERIODS[class_section][0]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        login(page)
        go_to_attendance_tab(page)
        attend_frame = select_period(page, period_name, date)
        if target == "list":
            html = get_list_frame(page).content()
        else:
            html = attend_frame.content()
        browser.close()

    return html


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
            attend_frame = select_period(page, period_name, date)
            mark_present_all(attend_frame, student_names)

            if idx == POINTS_PERIOD_INDEX:
                set_points_for_period(attend_frame, students)

            save_period(page, attend_frame)
            results.append(
                f"{period_name}: attendance saved"
                + (" + points" if idx == POINTS_PERIOD_INDEX else "")
            )

        browser.close()

    return results
