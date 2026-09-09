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
import re
import base64
import datetime
from playwright.sync_api import sync_playwright

NIGRI_BASE_URL = "https://www.nigrijewishonlineschool.com"
NIGRI_LOGIN_URL = f"{NIGRI_BASE_URL}/main/default_os_prog.asp?section=teachers"
NIGRI_USERNAME = os.environ.get("NIGRI_USERNAME")
NIGRI_PASSWORD = os.environ.get("NIGRI_PASSWORD")

# UNCONFIRMED -- carried over from the browser-extension version of this
# project (nigricontent.js / background.js), which guessed this URL for
# the School Marks / Tests section and never got to test it against the
# live site either. This is exactly what debug_marks_page() below exists
# to confirm or correct.
MARKS_URL = f"{NIGRI_BASE_URL}/main/default_os_prog.asp?section=teachers&subSection=tests&side=1"

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

POINTS_PERIOD_INDEX = 3  # 0-indexed -> "Morning Class 3" (or "Friday Class 3" on Fridays)

# On Fridays, Davening is unchanged but Morning Class 1/2/3 don't exist as
# selectable periods at all -- the site replaces them with Friday Class
# 1/2/3 instead (confirmed 2026-08-28, after a sync failed looking for
# "Morning Class 1" on a Friday and it turned out that option simply
# wasn't in the dropdown that day). Points still go on the last one
# (Friday Class 3), same pattern as the weekday Morning Class 3.
FRIDAY_CLASS_PERIODS = {
    "B3 WT": [
        "B3 WT - Davening",
        "B3 WT - Friday Class 1",
        "B3 WT - Friday Class 2",
        "B3 WT - Friday Class 3",   # <- points go here on Fridays
    ],
    "B3 ET": [
        "B3 ET - Davening",
        "B3 ET - Friday Class 1",
        "B3 ET - Friday Class 2",
        "B3 ET - Friday Class 3",   # <- points go here on Fridays
    ],
}


def get_periods_for_date(class_section, date_str):
    """
    Returns the right period list (weekday vs. Friday) for a given
    class_section + date string (expects "YYYY-MM-DD"). PERIOD_KEYS
    still index-aligns with whichever list comes back, since both
    lists are the same length/shape (Davening, Class 1, 2, 3).
    """
    if class_section not in CLASS_PERIODS:
        raise ValueError(f"Unknown class_section: {class_section}")

    parsed = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    is_friday = parsed.weekday() == 4  # Monday=0 ... Friday=4

    return FRIDAY_CLASS_PERIODS[class_section] if is_friday else CLASS_PERIODS[class_section]

# Fixed per-student childID used in Rewards tab URLs (confirmed via
# debug_rewards_page HTML dump, 2026-08-26). Matching students by name
# in the attendance iframe turned out to be unreliable, but these IDs
# are stable, so points are now applied via direct navigation to each
# student's own "Add Points" page instead of via the attendance form.
REWARDS_CHILD_IDS = {
    "Greenberg Ari": "24610",
    "Rosenfeld Zev": "26298",
    "Schtroks Levi": "24428",
    "Simmonds Yisroel Aryeh": "24319",
    "Vogel Leibel": "24356",
    "Wolf Yisroel Arye Leib": "24598",
    "Chaikin Mayer Chaim": "21141",
    "Gourarie Yossi": "24532",
    "Huebner Sholom DovBer": "24385",
    "Lapine Moshe": "26292",
    "Notik Kehos": "17440",
    "Oirechman Yisroel": "17439",
    "Raichman Moshe Tuvia": "24423",
    "Rosenfeld Avrohom": "26284",
    "Rozmarin Levi": "21009",
    "Traxler Arik": "21227",
}


class SyncError(Exception):
    """
    Raised when run_sync fails partway through. Carries whatever
    periods/results were already completed successfully, so a failure
    on (say) the 3rd period doesn't leave you guessing whether the
    first two actually saved.
    """
    def __init__(self, message, partial_results=None):
        super().__init__(message)
        self.partial_results = partial_results or []


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


def get_attend_frame(page, timeout_ms=20000, poll_ms=300):
    """
    The actual attendance checkboxes/rewards/save-button form lives in
    an iframe named "attend" (confirmed via debug HTML dump), nested
    inside the list frame. Playwright's page.frame(name=...) finds a
    frame by name anywhere in the page's frame tree regardless of
    nesting depth, so we don't need to manually walk the hierarchy.

    IMPORTANT: changing the period picker makes the list frame reload,
    which detaches the OLD "attend" iframe and creates a brand new one
    with the same name. If we grab page.frame(name="attend") once and
    then just wait on that single object, we can end up holding a
    reference to the outgoing frame (or a placeholder that never
    finishes loading) and sit there for the whole timeout waiting on
    it, even though a fresh, fully-loaded "attend" frame shows up
    moments later elsewhere in the frame tree. This bit us switching
    from Davening to Morning Class 1 (2026-08-28).

    So instead of fetch-once-then-wait, this re-fetches the "attend"
    frame fresh on every poll and only accepts one that is (a) not
    detached and (b) already has at least one student checkbox
    rendered -- if that specific frame isn't ready yet, we don't keep
    waiting on it, we just loop and grab whatever "attend" frame
    exists a moment later.
    """
    waited = 0
    last_seen_but_empty = False

    while waited < timeout_ms:
        frame = page.frame(name="attend")
        if frame is not None and not frame.is_detached():
            last_seen_but_empty = True
            try:
                frame.wait_for_selector('input[name^="attend_"]', timeout=poll_ms)
                return frame
            except Exception:
                pass  # this particular frame isn't ready (or is on its way out) -- keep polling

        page.wait_for_timeout(poll_ms)
        waited += poll_ms

    if last_seen_but_empty:
        raise RuntimeError(
            "The 'attend' iframe appeared but no student checkboxes ever "
            "showed up inside it (no input[name^='attend_'] found), even "
            "after re-checking repeatedly. This usually means the period "
            "picker change didn't fully take effect before we started "
            "looking."
        )
    raise RuntimeError(
        f"Could not locate any 'attend' iframe on the page after {timeout_ms}ms"
    )


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
    page.wait_for_timeout(1500)  # let child iframes finish reloading

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


def set_attendance_status(attend_frame, present_names, absent_names, excused_names, late_minutes=None):
    """
    Sets each student's real attendance checkboxes for one period.
    Confirmed via debug HTML dump (2026-08-27) that "Excused" is NOT a
    3rd value on the attend_{childID} checkbox -- it's a SEPARATE
    checkbox (isExcused_{childID}) that combines with it:

        Present  -> attend_{childID} checked,   isExcused_{childID} unchecked
        Absent   -> attend_{childID} unchecked, isExcused_{childID} unchecked
        Excused  -> attend_{childID} unchecked, isExcused_{childID} checked

    "Late" works the same way -- it's a student who IS present, plus a
    separate isLate_{childID} checkbox and a lateMinutes_{childID} text
    field that only appears once isLate is checked. late_minutes is an
    optional {name: minutes} dict; any name in it is treated as present
    AND late (it does not need to also appear in present_names).

    Targets each checkbox directly by name using the student's fixed
    childID (see REWARDS_CHILD_IDS) instead of row-text matching --
    more reliable, and these checkboxes live in a hidden detail row
    that's easier to reach this way once "Expand all" has run.
    """
    late_minutes = late_minutes or {}
    expand_all = attend_frame.locator("text=Expand all")
    if expand_all.count() > 0:
        expand_all.first.click()
        attend_frame.page.wait_for_timeout(300)

    def _apply(name, attend_should_be_checked, excused_should_be_checked):
        if name not in REWARDS_CHILD_IDS:
            raise RuntimeError(f"No known childID for student: {name}")
        cid = REWARDS_CHILD_IDS[name]

        attend_cb = attend_frame.locator(f'input[name="attend_{cid}"]')
        if attend_cb.is_checked() != attend_should_be_checked:
            attend_cb.set_checked(attend_should_be_checked)
            attend_frame.page.wait_for_timeout(150)

        excused_cb = attend_frame.locator(f'input[name="isExcused_{cid}"]')
        if excused_cb.is_checked() != excused_should_be_checked:
            excused_cb.set_checked(excused_should_be_checked)
            attend_frame.page.wait_for_timeout(150)

        return cid

    for name in present_names:
        _apply(name, attend_should_be_checked=True, excused_should_be_checked=False)
    for name in absent_names:
        _apply(name, attend_should_be_checked=False, excused_should_be_checked=False)
    for name in excused_names:
        _apply(name, attend_should_be_checked=False, excused_should_be_checked=True)

    for name, minutes in late_minutes.items():
        cid = _apply(name, attend_should_be_checked=True, excused_should_be_checked=False)

        late_cb = attend_frame.locator(f'input[name="isLate_{cid}"]')
        if not late_cb.is_checked():
            late_cb.set_checked(True)
            attend_frame.page.wait_for_timeout(150)

        minutes = str(minutes).strip()
        if minutes:
            minutes_field = attend_frame.locator(f'input[name="lateMinutes_{cid}"]')
            minutes_field.wait_for(state="visible", timeout=3000)
            minutes_field.fill(minutes)

    attend_frame.page.wait_for_timeout(300)


def set_points_for_period(attend_frame, students_with_points):
    """
    Kept for the debug tool / backward compatibility. NOT used for the
    real points-period sync anymore -- see
    check_and_set_points_individually() below, which is what run_sync
    actually calls for the points period. This version assumes
    students are already checked (e.g. via "Select all").
    """
    for student in students_with_points:
        name = student["name"]
        points = str(student["points"])
        row = attend_frame.locator(f"tr:has-text('{name}')").first
        rewards_select = row.locator('select[name^="rewardsPoints_"]').first
        rewards_select.wait_for(state="visible", timeout=5000)
        rewards_select.select_option(points)
        rewards_select.evaluate(
            """(el) => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.blur();
            }"""
        )
        attend_frame.page.wait_for_timeout(150)


def check_and_set_points_individually(attend_frame, students_with_points):
    """
    For the points period (Morning Class 3) specifically, the site
    apparently only populates/enables a row's rewards dropdown when
    THAT row's own checkbox is checked one at a time -- the "Select
    all" bulk action does not trigger whatever per-row logic wires up
    the dropdown's options. So for this period we go student by
    student: check the box, wait for that row's dropdown to be ready,
    set the points value, then move to the next student.
    """
    expand_all = attend_frame.locator("text=Expand all")
    if expand_all.count() > 0:
        expand_all.first.click()
        attend_frame.page.wait_for_timeout(300)

    for student in students_with_points:
        name = student["name"]
        points = str(student["points"])
        row = attend_frame.locator(f"tr:has-text('{name}')").first

        checkbox = row.locator('input[type="checkbox"][name^="attend_"]').first
        checkbox.check()
        attend_frame.page.wait_for_timeout(300)

        rewards_select = row.locator('select[name^="rewardsPoints_"]').first
        rewards_select.wait_for(state="visible", timeout=5000)

        # Give the page a brief moment after the checkbox check to
        # finish whatever AJAX/JS populates this row's option list,
        # then confirm the option we need actually exists before
        # trying to select it, instead of blindly calling
        # select_option and timing out with an unhelpful error.
        rewards_select.wait_for(state="attached", timeout=5000)
        option_values = rewards_select.evaluate(
            "el => Array.from(el.options).map(o => o.value)"
        )
        if points not in option_values:
            attend_frame.page.wait_for_timeout(500)
            option_values = rewards_select.evaluate(
                "el => Array.from(el.options).map(o => o.value)"
            )

        if points not in option_values:
            raise RuntimeError(
                f"Points value '{points}' not found in rewards dropdown "
                f"for {name}. Actual option values present: {option_values!r}"
            )

        rewards_select.select_option(points)
        rewards_select.evaluate(
            """(el) => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.blur();
            }"""
        )
        attend_frame.page.wait_for_timeout(200)


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
    period_name = get_periods_for_date(class_section, date)[POINTS_PERIOD_INDEX]
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
    period_name = get_periods_for_date(class_section, date)[0]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        login(page)
        go_to_attendance_tab(page)
        attend_frame = select_period(page, period_name, date)
        if target == "list":
            html = get_list_frame(page).content()
        else:
            # Expand rows so the per-student detail (Excused, No Headset,
            # No Mic, No Webcam, No Book, etc.) is present in the HTML --
            # it's hidden/collapsed by default and won't appear otherwise.
            expand_all = attend_frame.locator("text=Expand all")
            if expand_all.count() > 0:
                expand_all.first.click()
                attend_frame.page.wait_for_timeout(300)
            html = attend_frame.content()
        browser.close()

    return html


# Order MUST match CLASS_PERIODS[class_section] index-for-index --
# this is how each student's per-period status (sent from sync.html
# as student["attendance"][period_key]) gets matched to the right
# period on the Nigri site.
PERIOD_KEYS = ["davening", "class1", "class2", "class3"]


def run_sync(class_section, date, students):
    """
    One button, two phases, one browser session:
      1. For each of the 4 periods, mark each student Present, Absent,
         or Excused per that student's OWN per-period status (sent from
         sync.html as student["attendance"][period_key]) -- see
         set_attendance_status. Replaces the old behavior of blindly
         marking everyone Present every period.
      2. Give each student their points via the Rewards tab directly
         (see add_points_for_student) -- unchanged, confirmed working.
    """
    periods = get_periods_for_date(class_section, date)
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        login(page)

        # --- Phase 1: attendance, per period, per-student status ---
        go_to_attendance_tab(page)
        for period_index, period_name in enumerate(periods):
            period_key = PERIOD_KEYS[period_index]
            present_names, absent_names, excused_names = [], [], []
            late_minutes = {}
            for student in students:
                status = student.get("attendance", {}).get(period_key, "present")
                if status == "absent":
                    absent_names.append(student["name"])
                elif status == "excused":
                    excused_names.append(student["name"])
                elif status == "late":
                    # Late students are present -- handled entirely via
                    # late_minutes below, not added to present_names too.
                    late_minutes[student["name"]] = student.get("lateMinutes", {}).get(period_key, "")
                else:
                    present_names.append(student["name"])

            try:
                attend_frame = select_period(page, period_name, date)
                set_attendance_status(attend_frame, present_names, absent_names, excused_names, late_minutes)
                save_period(page, attend_frame)
            except Exception as e:
                raise SyncError(f"Failed during period '{period_name}': {e}", partial_results=results) from e

            results.append(
                f"{period_name}: saved (present={len(present_names)}, "
                f"absent={len(absent_names)}, excused={len(excused_names)}, "
                f"late={len(late_minutes)})"
            )

        # --- Phase 2: points, via Rewards tab, one student at a time ---
        for student in students:
            name = student["name"]
            points = student["points"]
            try:
                add_points_for_student(page, name, points)
                results.append(f"{name}: {points} points saved")
            except Exception as e:
                results.append(f"{name}: POINTS FAILED - {e}")

        browser.close()

    return results


def add_points_for_student(page, name, points):
    """
    Navigates directly to a student's "Add Points" page (via their
    fixed childID -- see REWARDS_CHILD_IDS) and submits the points
    form there. Confirmed exact form structure via debug_rewards_page
    HTML dump (2026-08-26):
      <select name="rewardsPoints" id="rewardsPoints"> ... </select>
      <input name="rewardsReason" ...>
      <input type="submit" value="Save!">
    """
    if name not in REWARDS_CHILD_IDS:
        raise RuntimeError(f"No known childID for student: {name}")
    child_id = REWARDS_CHILD_IDS[name]

    url = (
        f"{NIGRI_BASE_URL}/main/default_os_prog.asp"
        f"?section=teachers&spec=rewards&rewards=&rewardsGradeSelect=&childID={child_id}"
    )
    page.goto(url)
    page.wait_for_load_state("networkidle")

    points_select = page.locator("select#rewardsPoints")
    points_select.wait_for(state="visible", timeout=8000)

    target = str(points)
    available_values = points_select.evaluate(
        "el => Array.from(el.options).map(o => o.value)"
    )
    if target not in available_values:
        raise RuntimeError(
            f"Points value '{target}' not available for {name} "
            f"(childID={child_id}). Available options: {available_values!r}. "
            f"This usually means the student already hit today's max allowed points."
        )

    points_select.select_option(target)

    save_btn = page.locator('input[type="submit"][value="Save!"]')
    save_btn.wait_for(state="visible", timeout=5000)
    save_btn.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(300)


def debug_rewards_page(student_name=None):
    """
    Diagnostic: logs in, navigates to Rewards -> Rewards points, dumps
    that page's HTML (to see the student list links). If student_name
    is given (must be a key in REWARDS_CHILD_IDS), navigates DIRECTLY
    via that student's childID URL to their "Add Points" page and
    dumps its HTML too (to see the points form's real field names).
    Nothing is saved/submitted either way.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        login(page)

        page.click("text=Rewards")
        page.wait_for_load_state("networkidle")
        page.click("text=Rewards points")
        page.wait_for_load_state("networkidle")

        rewards_list_html = page.content()
        student_page_html = None

        if student_name:
            if student_name not in REWARDS_CHILD_IDS:
                raise RuntimeError(f"No known childID for student: {student_name}")
            child_id = REWARDS_CHILD_IDS[student_name]
            url = (
                f"{NIGRI_BASE_URL}/main/default_os_prog.asp"
                f"?section=teachers&spec=rewards&rewards=&rewardsGradeSelect=&childID={child_id}"
            )
            page.goto(url)
            page.wait_for_load_state("networkidle")
            student_page_html = page.content()

        browser.close()

    return {
        "rewards_list_html": rewards_list_html,
        "student_page_html": student_page_html,
    }


def _dump_all_frames(page):
    """
    Shared helper: returns [{name, url, html}] for every frame currently
    on the page (the main page counts as one frame with name ""). Used
    by both Marks debug functions so we never miss content that turns
    out to live in a nested iframe, the way Attendance's did.
    """
    frames_dump = []
    for frame in page.frames:
        entry = {"name": frame.name, "url": frame.url}
        try:
            entry["html"] = frame.content()
        except Exception as e:
            entry["error"] = str(e)
        frames_dump.append(entry)
    return frames_dump


def debug_marks_page(click_texts=None, screenshot=False):
    """
    Diagnostic ONLY -- this is the discovery step for School Marks/quiz
    marks, the same way debug_attendance_page/debug_rewards_page were
    used to nail down the real selectors for Attendance and Rewards.
    NOTHING about the marks flow below has been confirmed against the
    live site yet.

    Logs in, goes to MARKS_URL (currently just a guess -- see the note
    above it), then optionally clicks through a sequence of link/button
    texts one at a time (e.g. click_texts=["Create New Mark"]) to reach
    a deeper screen. After that, it dumps:
      - every frame's name, URL, and full HTML (the main page counts as
        one "frame" with name None) -- Attendance turned out to live in
        a nested iframe named "attend", so this grabs everything rather
        than assuming Marks does or doesn't work the same way
      - optionally a full-page screenshot (base64-encoded PNG), which
        can be quicker to eyeball than raw HTML for figuring out what
        screen we actually landed on

    Safety: this never clicks anything resembling a save/submit control
    on its own -- only whatever exact text you pass in click_texts, and
    if any click in the sequence fails to find a match, it stops there
    (recorded in click_results) rather than guessing at what comes next.
    Landing on a blank "create new mark" form is expected to be safe
    (nothing persists until an actual Save is clicked), but reaching the
    per-student marks table appears -- going by the old browser-extension
    code -- to require submitting that header form first, which IS a
    real save on Nigri's side. That's deliberately a separate, later,
    opt-in step (do it once with an obviously-fake test title/date so
    it's easy to find and delete) -- see debug_marks_create_and_view().
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        login(page)
        page.goto(MARKS_URL)
        page.wait_for_load_state("networkidle")

        click_results = []
        for text in (click_texts or []):
            try:
                page.click(f"text={text}", timeout=5000)
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(500)
                click_results.append({"text": text, "ok": True})
            except Exception as e:
                click_results.append({"text": text, "ok": False, "error": str(e)})
                break  # later clicks are probably meaningless if this one failed

        main_url = page.url
        frames_dump = _dump_all_frames(page)

        screenshot_b64 = None
        if screenshot:
            screenshot_b64 = base64.b64encode(page.screenshot(full_page=True)).decode("ascii")

        browser.close()

    return {
        "marks_url_used": MARKS_URL,
        "landed_on_url": main_url,
        "click_results": click_results,
        "frames": frames_dump,
        "screenshot_b64": screenshot_b64,
    }


# Real grade IDs confirmed via debug_marks_page HTML dump (2026-09-08).
# select_option can also match by visible label text directly, so these
# aren't strictly required, but kept here since we now know them for
# certain and they may be useful later (e.g. for direct-URL navigation).
MARKS_GRADE_IDS = {
    "B3 ET": "22590",
    "B3 WT": "22453",
}


# Real topic IDs confirmed via debug_marks_page's HTML dump (2026-09-08)
# -- this is the FULL master list of topic options as shown BEFORE a
# grade is picked. Selecting a topic by its value (id) instead of its
# visible label sidesteps the AJAX-refresh timing problem found while
# testing (see select_topic_robust below): the ids stay the same after
# the grade-triggered refresh even though the select's DOM node itself
# gets rebuilt.
MARKS_TOPIC_IDS = {
    "A Chossid is a Lamdan": "184",
    "Behavior": "134",
    "Biurei Tefila": "42",
    "Chumash": "10",
    "Chumash Comprehension": "137",
    "Chumash Havana": "194",
    "Chumash Translation": "138",
    "Chumash-HW": "163",
    "Chumash-Rashi": "139",
    "Class Participation": "135",
    "Devar Torah": "11",
    "Halacha-Yahadus": "145",
    "Inyonei Geulo UMoshiach": "27",
    "Kriah": "58",
    "Niggun": "7",
    "Parsha": "9",
    "Programs": "167",
    "Shoroshim": "142",
    "Tefila": "40",
    "Worksheets": "189",
}


def select_topic_robust(page, class_section, topic_label, timeout_ms=15000, poll_ms=500):
    """
    Picking a grade (select#testGradeID) kicks off an AJAX call that
    repopulates select#testTopicID -- confirmed (2026-09-08, testing
    B3 ET) to be slow/unpredictable enough that selecting a topic right
    after picking a grade can time out entirely. Instead of one blind
    select_option call, this polls the LIVE option list on the select
    element until the topic we want actually shows up, then selects it
    -- by option value when we know the topic's id (MARKS_TOPIC_IDS;
    more reliable than label text, which is what timed out before), or
    by label as a fallback for a topic that isn't in that map.
    """
    topic_id = MARKS_TOPIC_IDS.get(topic_label)
    waited = 0
    while waited < timeout_ms:
        try:
            if topic_id is not None:
                values = page.eval_on_selector(
                    "select#testTopicID",
                    "el => Array.from(el.options).map(o => o.value)",
                )
                if topic_id in values:
                    page.select_option("select#testTopicID", value=topic_id)
                    return
            else:
                labels = page.eval_on_selector(
                    "select#testTopicID",
                    "el => Array.from(el.options).map(o => o.textContent.trim())",
                )
                if topic_label in labels:
                    page.select_option("select#testTopicID", label=topic_label)
                    return
        except Exception:
            pass  # the select may be mid-reload right now -- just keep polling
        page.wait_for_timeout(poll_ms)
        waited += poll_ms

    # Ran out of time -- report exactly what options WERE available, so a
    # wrong/misspelled/unavailable-for-this-grade topic is obvious
    # instead of just a bare timeout.
    try:
        available = page.eval_on_selector(
            "select#testTopicID",
            "el => Array.from(el.options).map(o => o.textContent.trim())",
        )
    except Exception:
        available = None
    raise RuntimeError(
        f"Topic '{topic_label}' never appeared in the topic dropdown for "
        f"class '{class_section}' after {timeout_ms}ms. Options actually "
        f"available: {available!r}"
    )


def fill_student_mark(page, name, mark=None, attendance="present", comment=None, report_base64=None, report_filename=None):
    """
    Fills in one student's row on the per-student marks screen (the
    screen that only exists after "Create Mark!" has been submitted --
    see run_marks_sync). Confirmed real field names via
    debug_marks_create_and_view's HTML dump (2026-09-08):
        testChild_{childID}_mark              (text input, the score)
        testChild_{childID}_markSpecialStatus  (select: ''=Present,
            '1'=Absent, '2'=N/A, '3'=Excused)
        testChild_{childID}_markComment        (textarea)
        testChild_{childID}_File1              (file upload, one of
            File1/File2/File3 -- only File1 is used here)

    attendance is one of "present", "absent", or "review":
      - "present" (default): leaves markSpecialStatus at its default
        (blank = Present) and fills in the mark, if one was given.
      - "absent": sets markSpecialStatus to Absent and does NOT fill in
        a mark (an absent student doesn't have a quiz score).
      - "review": skipped entirely -- neither the mark nor the status
        field is touched for this student, so they're left exactly as
        "Create Mark!" set them up (blank/default), per instructions
        not to guess at a status for these.

    report_base64, if given, is a base64-encoded PDF (the per-student
    question-by-question report generated client-side in
    B3SchoolMarksBridge.html) attached directly into the real File1
    upload field -- no temp file needed, Playwright can attach an
    in-memory buffer straight to a file input.
    """
    if name not in REWARDS_CHILD_IDS:
        raise RuntimeError(f"No known childID for student: {name}")
    cid = REWARDS_CHILD_IDS[name]

    if attendance == "review":
        return
    if attendance not in ("present", "absent"):
        raise ValueError(f"Unknown attendance value for {name}: {attendance!r}")

    if attendance == "absent":
        page.locator(f'select[name="testChild_{cid}_markSpecialStatus"]').select_option("1")
    elif mark not in (None, ""):
        mark_input = page.locator(f'input[name="testChild_{cid}_mark"]')
        mark_input.fill(str(mark))
        # The real field has an onchange handler (test_markFix) Nigri
        # uses to validate/reformat what was typed -- fire a real
        # change event so that runs, same as a person tabbing out of
        # the box would trigger.
        mark_input.evaluate(
            """(el) => {
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.blur();
            }"""
        )

    if comment:
        page.locator(f'textarea[name="testChild_{cid}_markComment"]').fill(str(comment))

    if report_base64:
        page.locator(f'input[name="testChild_{cid}_File1"]').set_input_files({
            "name": report_filename or f"{name} - report.pdf",
            "mimeType": "application/pdf",
            "buffer": base64.b64decode(report_base64),
        })


def run_marks_sync(
    class_section,
    topic,
    test_name,
    students,
    mark_type="quiz",
    test_date=None,
    description=None,
    mark_all_done=True,
):
    """
    The real, production version of the School Marks push -- the quiz
    equivalent of run_sync() for attendance/points. One browser session:
      1. Create a new mark (header form): grade, topic, type, name, and
         optionally a date/description.
      2. Fill in each student's score/status/comment on the resulting
         per-student screen (see fill_student_mark).
      3. Mark the whole thing "Marking done" (unless mark_all_done is
         False) and submit the real final Save!.

    Unlike the debug create-and-delete function, this does NOT delete
    the mark afterward -- this is the real thing being pushed to the
    gradebook, meant to stay there.

    class_section: "B3 ET" or "B3 WT".
    topic: a real topic label -- see MARKS_TOPIC_IDS for the confirmed
      list (e.g. "Chumash", "Parsha", "Shoroshim", ...).
    test_name: the mark's title, shown in Nigri's mark list.
    students: [{"name": ..., "mark": ..., "attendance": "present" |
      "absent" | "review", "comment": (optional)}, ...] -- "name" must
      be a key in REWARDS_CHILD_IDS.
    mark_type: one of "homework", "test", "quiz", "classwork".
    """
    if class_section not in MARKS_GRADE_IDS:
        raise ValueError(f"Unknown class_section: {class_section}")
    if mark_type not in ("homework", "test", "quiz", "classwork"):
        raise ValueError(f"Unknown mark_type: {mark_type}")

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        login(page)
        page.goto(MARKS_URL)
        page.wait_for_load_state("networkidle")

        try:
            page.click("text=Create New Mark", timeout=5000)
            page.wait_for_load_state("networkidle")
        except Exception as e:
            raise SyncError(f"Could not open 'Create New Mark': {e}", partial_results=results) from e

        try:
            page.select_option("select#testGradeID", label=class_section)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1000)  # let the topic-list AJAX refresh land
        except Exception as e:
            raise SyncError(f"Could not select grade '{class_section}': {e}", partial_results=results) from e

        try:
            select_topic_robust(page, class_section, topic)
        except Exception as e:
            raise SyncError(f"Could not select topic '{topic}': {e}", partial_results=results) from e

        try:
            page.check(f"input#testType_{mark_type}")
        except Exception as e:
            raise SyncError(f"Could not select mark type '{mark_type}': {e}", partial_results=results) from e

        try:
            page.fill('input[name="testName"]', test_name)
        except Exception as e:
            raise SyncError(f"Could not fill in the mark name: {e}", partial_results=results) from e

        if test_date:
            try:
                page.fill('input[name="testDate"]', test_date)
            except Exception as e:
                raise SyncError(f"Could not fill in the date: {e}", partial_results=results) from e

        if description:
            try:
                page.fill('textarea[name="testDesc"]', description)
            except Exception as e:
                raise SyncError(f"Could not fill in the description: {e}", partial_results=results) from e

        try:
            page.click('input[type="submit"][value="Create Mark!"]')
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
        except Exception as e:
            raise SyncError(f"Could not submit 'Create Mark!': {e}", partial_results=results) from e

        landed_on_url = page.url
        match = re.search(r"testID=(\d+)", landed_on_url)
        test_id = match.group(1) if match else None
        if not test_id or test_id == "0":
            raise SyncError(
                f"Mark was submitted but no real testID came back in the URL "
                f"({landed_on_url}) -- stopping before touching any student rows.",
                partial_results=results,
            )
        results.append(f"Mark created (testID={test_id})")

        for student in students:
            name = student["name"]
            attendance = student.get("attendance", "present")
            try:
                fill_student_mark(
                    page,
                    name=name,
                    mark=student.get("mark"),
                    attendance=attendance,
                    comment=student.get("comment"),
                    report_base64=student.get("report_base64"),
                    report_filename=student.get("report_filename"),
                )
                results.append(f"{name}: filled (attendance={attendance})")
            except Exception as e:
                results.append(f"{name}: FAILED - {e}")

        if mark_all_done:
            try:
                # Confirmed via debug_marks_create_and_view's HTML dump
                # (2026-09-08) that this field has no id -- it must be
                # targeted by its name attribute, not select#testCompleted.
                page.select_option('select[name="testCompleted"]', value="1")
            except Exception as e:
                results.append(f"Could not set 'Marking done': {e}")

        try:
            page.click('input[type="submit"][value="Save!"]')
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            results.append("Final Save! submitted")
        except Exception as e:
            raise SyncError(
                f"Mark {test_id} was created and student rows were filled, but "
                f"the final Save! failed: {e}",
                partial_results=results,
            ) from e

        browser.close()

    return {"test_id": test_id, "results": results}


# A tiny, valid, hand-built one-page PDF ("TEST REPORT ATTACHMENT") used
# ONLY by debug_marks_full_flow's with_report option, to safely confirm
# Playwright's set_input_files() (used by fill_student_mark for the real
# per-student report upload) actually works against Nigri's real File1
# field before trusting it with real quiz reports.
_TEST_REPORT_PDF_BASE64 = (
    "JVBERi0xLjQKMSAwIG9iajw8L1R5cGUvQ2F0YWxvZy9QYWdlcyAyIDAgUj4+ZW5kb2JqCjIgMCBv"
    "Ymo8PC9UeXBlL1BhZ2VzL0tpZHNbMyAwIFJdL0NvdW50IDE+PmVuZG9iagozIDAgb2JqPDwvVHlw"
    "ZS9QYWdlL1BhcmVudCAyIDAgUi9NZWRpYUJveFswIDAgMjAwIDEwMF0vUmVzb3VyY2VzPDwvRm9u"
    "dDw8L0YxIDQgMCBSPj4+Pi9Db250ZW50cyA1IDAgUj4+ZW5kb2JqCjQgMCBvYmo8PC9UeXBlL0Zv"
    "bnQvU3VidHlwZS9UeXBlMS9CYXNlRm9udC9IZWx2ZXRpY2E+PmVuZG9iago1IDAgb2JqPDwvTGVu"
    "Z3RoIDYyPj5zdHJlYW0KQlQgL0YxIDEyIFRmIDIwIDYwIFRkIChURVNUIFJFUE9SVCBBVFRBQ0hN"
    "RU5UKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCnhyZWYKMCA2CjAwMDAwMDAwMDAgNjU1MzUgZiAK"
    "dHJhaWxlcjw8L1NpemUgNi9Sb290IDEgMCBSPj4Kc3RhcnR4cmVmCjAKJSVFT0Y="
)


def debug_marks_full_flow(
    class_section,
    topic,
    test_name="ZZZ_DEBUG_DELETE_ME",
    mark_type="quiz",
    test_date=None,
    students=None,
    delete_after=True,
    with_report=False,
):
    """
    Runs the REAL production run_marks_sync() end-to-end (create mark,
    fill student row(s), click the real final Save!) against an
    obviously-fake test_name -- then, if delete_after is true (the
    default), deletes that test mark afterward the same way
    debug_marks_create_and_view does. This is the safe way to test the
    WHOLE real flow (topic-selection fix included) with a real-shaped
    student before wiring this up to a real quiz's data.

    Defaults to one harmless test student if none is given. If
    with_report is true and no students were given, attaches the tiny
    test PDF above to that default student, to confirm the file-upload
    step itself works before trusting it with a real generated report.
    """
    if not students:
        students = [{"name": "Chaikin Mayer Chaim", "mark": "9", "attendance": "present", "comment": "test"}]
        if with_report:
            students[0]["report_base64"] = _TEST_REPORT_PDF_BASE64
            students[0]["report_filename"] = "test-report-attachment.pdf"

    sync_result = run_marks_sync(
        class_section=class_section,
        topic=topic,
        test_name=test_name,
        students=students,
        mark_type=mark_type,
        test_date=test_date,
    )

    test_id = sync_result.get("test_id")
    delete_result = None
    if delete_after and test_id:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            login(page)
            del_url = (
                f"{NIGRI_BASE_URL}/main/default_os_prog.asp"
                f"?section=teachers&spec=tests&action=del_test&testID={test_id}"
            )
            page.goto(del_url)
            page.wait_for_load_state("networkidle")
            delete_result = {"ok": True, "test_id": test_id, "final_url": page.url}
            browser.close()

    return {**sync_result, "delete_result": delete_result}


def debug_marks_create_and_view(
    class_section,
    topic_label,
    mark_type="quiz",
    test_name="ZZZ_DEBUG_DELETE_ME",
    test_date=None,
    delete_after=True,
    screenshot=False,
):
    """
    Diagnostic step 2 for School Marks. UNLIKE debug_marks_page, this one
    DOES write to the real Nigri site: it fills in the real header form
    (confirmed via debug_marks_page -- select#testGradeID, #testTopicID,
    input#testType_{homework|test|quiz|classwork}, input[name=testDate],
    input[name=testName], textarea[name=testDesc]) with obviously-fake
    test values, clicks "Create Mark!", and captures whatever the
    resulting per-student marks screen looks like -- that screen's real
    field names are the one thing debug_marks_page couldn't reach, since
    getting there requires an actual save.

    class_section must be "B3 ET" or "B3 WT" (the real dropdown text).
    topic_label must match one of the real topic option texts (e.g.
    "Chumash", "Shoroshim", "Parsha", "Chumash Comprehension", ...) --
    see debug_marks_page's dumped HTML for the full confirmed list.
    mark_type must be one of "homework", "test", "quiz", "classwork".

    Safety: if delete_after is true (the default), this finds the new
    testID from the post-save URL and immediately deletes that same test
    mark by hitting the same URL Nigri's own "delete test" link uses
    (tests_delTest() in their JS: ...&action=del_test&testID=...), so the
    obviously-fake entry doesn't linger in the real gradebook. Every step
    is recorded in "steps" regardless of whether it succeeded, so a
    failure partway through still tells us exactly how far it got.
    """
    steps = []

    def record(name, ok, detail=None):
        entry = {"step": name, "ok": ok}
        if detail is not None:
            entry["detail"] = detail
        steps.append(entry)

    landed_on_url = None
    test_id = None
    frames_dump = []
    screenshot_b64 = None
    delete_result = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        login(page)
        page.goto(MARKS_URL)
        page.wait_for_load_state("networkidle")

        try:
            page.click("text=Create New Mark", timeout=5000)
            page.wait_for_load_state("networkidle")
            record("click_create_new_mark", True)
        except Exception as e:
            record("click_create_new_mark", False, str(e))
            frames_dump = _dump_all_frames(page)
            browser.close()
            return {"steps": steps, "aborted": True, "frames": frames_dump}

        try:
            page.select_option("select#testGradeID", label=class_section)
            page.wait_for_load_state("networkidle")
            # Selecting the grade triggers an AJAX call that repopulates
            # the topic dropdown -- give it a beat to land before we try
            # to select a topic from it.
            page.wait_for_timeout(1000)
            record("select_grade", True, class_section)
        except Exception as e:
            record("select_grade", False, str(e))

        try:
            page.select_option("select#testTopicID", label=topic_label)
            record("select_topic", True, topic_label)
        except Exception as e:
            record("select_topic", False, str(e))

        try:
            page.check(f"input#testType_{mark_type}")
            record("select_type", True, mark_type)
        except Exception as e:
            record("select_type", False, str(e))

        try:
            page.fill('input[name="testName"]', test_name)
            record("fill_name", True, test_name)
        except Exception as e:
            record("fill_name", False, str(e))

        if test_date:
            try:
                page.fill('input[name="testDate"]', test_date)
                record("fill_date", True, test_date)
            except Exception as e:
                record("fill_date", False, str(e))

        try:
            page.click('input[type="submit"][value="Create Mark!"]')
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            record("submit_create", True)
        except Exception as e:
            record("submit_create", False, str(e))
            frames_dump = _dump_all_frames(page)
            browser.close()
            return {"steps": steps, "aborted": True, "frames": frames_dump}

        landed_on_url = page.url
        match = re.search(r"testID=(\d+)", landed_on_url)
        test_id = match.group(1) if match else None
        record("extract_test_id", bool(test_id) and test_id != "0", test_id)

        frames_dump = _dump_all_frames(page)

        if screenshot:
            screenshot_b64 = base64.b64encode(page.screenshot(full_page=True)).decode("ascii")

        if delete_after:
            if test_id and test_id != "0":
                try:
                    del_url = (
                        f"{NIGRI_BASE_URL}/main/default_os_prog.asp"
                        f"?section=teachers&spec=tests&action=del_test&testID={test_id}"
                    )
                    page.goto(del_url)
                    page.wait_for_load_state("networkidle")
                    delete_result = {"ok": True, "test_id": test_id, "final_url": page.url}
                    record("delete_test", True, test_id)
                except Exception as e:
                    delete_result = {"ok": False, "test_id": test_id, "error": str(e)}
                    record("delete_test", False, str(e))
            else:
                delete_result = {"ok": False, "reason": "no test_id found in URL -- nothing deleted"}
                record("delete_test", False, "no test_id found")

        browser.close()

    return {
        "steps": steps,
        "landed_on_url": landed_on_url,
        "test_id": test_id,
        "frames": frames_dump,
        "screenshot_b64": screenshot_b64,
        "delete_result": delete_result,
    }
