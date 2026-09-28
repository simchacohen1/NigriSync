"""
Read-only Nigri Attendance History reader.

This module is intentionally separate from nigri_playwright.py so the
existing working attendance/points/marks automation does not need to be
modified. It imports the already-confirmed login helper and student IDs.
"""

import re
from playwright.sync_api import sync_playwright
from nigri_playwright import (
    NIGRI_BASE_URL,
    REWARDS_CHILD_IDS,
    login,
)

ATTENDANCE_HISTORY_URL = (
    f"{NIGRI_BASE_URL}/main/default_os_prog.asp"
    "?section=teachers&spec=logs&xmlFile=os"
)


def _attendance_history_table(page):
    """
    Find the history table by choosing the table containing the largest
    number of known B3 student names.
    """
    names = list(REWARDS_CHILD_IDS.keys())
    tables = page.locator("table")
    best = None
    best_score = 0

    for i in range(tables.count()):
        table = tables.nth(i)
        try:
            text = table.inner_text(timeout=1000)
        except Exception:
            continue

        score = sum(1 for name in names if name in text)
        if score > best_score:
            best = table
            best_score = score

    if best is None or best_score < 3:
        raise RuntimeError(
            "Could not locate the Attendance History table containing B3 students."
        )

    return best


def _cell_payload(cell):
    """
    Preserve text and icon metadata. Nigri uses icons for some attendance
    statuses, so this first read intentionally does not guess what an icon means.
    """
    try:
        return cell.evaluate(
            """el => ({
                text: (el.innerText || el.textContent || '').trim(),
                html: el.innerHTML || '',
                title: el.getAttribute('title') || '',
                className: el.className || '',
                images: Array.from(el.querySelectorAll('img')).map(img => ({
                    src: img.getAttribute('src') || '',
                    alt: img.getAttribute('alt') || '',
                    title: img.getAttribute('title') || '',
                    className: img.className || ''
                }))
            })"""
        )
    except Exception:
        return {
            "text": "",
            "html": "",
            "title": "",
            "className": "",
            "images": [],
        }


def read_attendance():
    """
    Read the currently displayed Nigri Attendance History page.

    SAFETY:
      - Logs in.
      - Opens Attendance History.
      - Reads DOM/table values only.
      - Does NOT click attendance checkboxes.
      - Does NOT click Save.
      - Does NOT submit an attendance form.

    The returned raw cell/icon information lets us identify Nigri's real
    Present/Absent/Late/Excused encoding before we normalize it.
    """
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

        for row_index in range(rows.count()):
            row = rows.nth(row_index)
            cells = row.locator("th,td")
            payloads = [
                _cell_payload(cells.nth(i))
                for i in range(cells.count())
            ]

            if not payloads:
                continue

            raw_rows.append(payloads)

            row_text = " ".join(
                payload.get("text", "")
                for payload in payloads
            ).strip()

            matched_name = next(
                (name for name in known_names if name in row_text),
                None,
            )
            if not matched_name:
                continue

            name_index = next(
                (
                    i
                    for i, payload in enumerate(payloads)
                    if matched_name in payload.get("text", "")
                ),
                0,
            )

            students.append(
                {
                    "name": matched_name,
                    "child_id": REWARDS_CHILD_IDS.get(matched_name),
                    "cells": payloads[name_index + 1 :],
                    "raw_row": payloads,
                }
            )

        body_text = page.locator("body").inner_text()

        range_match = re.search(
            r"Logs\s+for\s+(\d{1,2}/\d{1,2}/\d{4})"
            r"\s*-\s*(\d{1,2}/\d{1,2}/\d{4})",
            body_text,
            flags=re.I,
        )

        # Capture the real dropdown IDs, values, selected labels and options.
        # This lets the next revision support exact date ranges/grades without
        # guessing at Nigri's selector or option values.
        selects = []
        select_nodes = page.locator("select")

        for i in range(select_nodes.count()):
            sel = select_nodes.nth(i)
            try:
                selects.append(
                    sel.evaluate(
                        """el => ({
                            id: el.id || '',
                            name: el.name || '',
                            value: el.value || '',
                            selectedText:
                                el.selectedOptions &&
                                el.selectedOptions.length
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

        result = {
            "read_only": True,
            "source_url": page.url,
            "range_start": (
                range_match.group(1)
                if range_match
                else None
            ),
            "range_end": (
                range_match.group(2)
                if range_match
                else None
            ),
            "student_count": len(students),
            "students": students,
            "raw_rows": raw_rows,
            "selects": selects,
        }

        browser.close()
        return result
