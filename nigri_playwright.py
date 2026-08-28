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
    if class_section not in CLASS_PERIODS:
        raise ValueError(f"Unknown class_section: {class_section}")

    periods = CLASS_PERIODS[class_section]
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

            attend_frame = select_period(page, period_name, date)
            set_attendance_status(attend_frame, present_names, absent_names, excused_names, late_minutes)
            save_period(page, attend_frame)
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
