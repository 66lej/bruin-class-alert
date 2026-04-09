#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode


DEFAULT_MYUCLA_LOGIN_URL = "https://my.ucla.edu/directLink.aspx?featureID=203"
DEFAULT_MYUCLA_RESULTS_BASE_URL = "https://sa.ucla.edu/ro/ClassSearch/Results"
DEFAULT_MYUCLA_SEARCH_URL = "https://sa.ucla.edu/ro/classsearch"
DEFAULT_CHROME_APP_NAME = "Google Chrome"
DEFAULT_OPEN_TIMEOUT_SECONDS = 20
DEFAULT_ACTION_TIMEOUT_SECONDS = 45
DEFAULT_POLL_INTERVAL_SECONDS = 1.0


class AutoEnrollError(Exception):
    """Raised when local auto-enrollment cannot be attempted."""


@dataclass(frozen=True)
class AutoEnrollResult:
    status: str
    message: str
    details: dict[str, Any]


def build_results_url(*, term: str, subject: str, catalog: str, base_url: str = DEFAULT_MYUCLA_RESULTS_BASE_URL) -> str:
    params = {
        "t": term,
        "sBy": "subject",
        "subj": subject,
        "catlg": catalog,
        "btnIsInIndex": "btn_inIndex",
    }
    return f"{base_url}?{urlencode(params)}"


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise AutoEnrollError("Local auto-enroll currently only supports macOS with Google Chrome.")


def _run_osascript(script: str, *args: str) -> str:
    completed = subprocess.run(
        ["osascript", "-", *args],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AutoEnrollError(completed.stderr.strip() or "osascript command failed.")
    return completed.stdout.strip()


def _open_chrome_tab(url: str, chrome_app_name: str) -> None:
    completed = subprocess.run(
        ["open", "-a", chrome_app_name, url],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AutoEnrollError(completed.stderr.strip() or f"Failed to open {url} in {chrome_app_name}.")


def _execute_chrome_javascript(javascript: str, chrome_app_name: str) -> str:
    escaped_app_name = chrome_app_name.replace('"', '\\"')
    script = f"""
on run argv
    set jsSource to item 1 of argv
    tell application \"{escaped_app_name}\"
        activate
        return execute active tab of front window javascript jsSource
    end tell
end run
"""
    try:
        return _run_osascript(script, javascript)
    except AutoEnrollError as exc:
        message = str(exc)
        if "JavaScript" in message and "Apple" in message:
            raise AutoEnrollError(
                "Chrome currently blocks JavaScript from Apple Events. In Chrome, go to View > Developer > Allow JavaScript from Apple Events, then rerun setup."
            ) from exc
        raise


def _json_or_empty(payload: str) -> dict[str, Any]:
    if not payload or payload == "__TAB_NOT_FOUND__":
        return {}
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return {"raw": payload}
    return decoded if isinstance(decoded, dict) else {"value": decoded}


def _build_page_state_script(primary_section: str, secondary_section: str | None = None) -> str:
    primary_section_json = json.dumps(primary_section)
    secondary_section_json = json.dumps(secondary_section or "")
    return f"""
(function(primarySectionText, secondarySectionText) {{
  function normalize(value) {{
    return (value || '').replace(/\\s+/g, ' ').trim();
  }}
  function isVisible(el) {{
    if (!el) return false;
    const style = window.getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden' && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  }}
  function getRows() {{
    return Array.from(document.querySelectorAll('.row-fluid.data_row.class-info')).map((row) => {{
      const checkbox = row.querySelector(\"input[type='checkbox'][id$='-checkbox']\");
      return {{
        id: row.id || '',
        section: normalize((row.querySelector('.sectionColumn') || {{}}).innerText),
        status: normalize((row.querySelector('.statusColumn') || {{}}).innerText),
        waitlist: normalize((row.querySelector('.waitlistColumn') || {{}}).innerText),
        checkboxId: checkbox ? checkbox.id : '',
        checked: !!(checkbox && checkbox.checked),
        disabled: !!(checkbox && checkbox.disabled),
        visible: isVisible(row)
      }};
    }});
  }}

  const bodyText = normalize(document.body ? document.body.innerText : '');
  const rows = getRows();
  const primaryRow = rows.find((row) => row.section && row.section.includes(primarySectionText)) || null;
  const secondaryRows = rows.filter((row) => row.checkboxId && (!primaryRow || row.id !== primaryRow.id));
  const preferredSecondaryRow = secondarySectionText
    ? secondaryRows.find((row) => row.section && row.section.includes(secondarySectionText)) || null
    : null;
  const warningBoxes = Array.from(document.querySelectorAll('.enroll_warning_flyout_warningCheckbox')).filter(isVisible);
  const enrollBtn = document.querySelector('#btn_Enroll');
  const waitlistBtn = document.querySelector('#btn_enrollmentAction_Enroll');
  const pteInput = document.querySelector('#txtbox_enroll_warning_pte_flyout_pte');
  const hidFlyoutType = document.querySelector('#hidFlyoutType');
  const success = !!document.querySelector('#_success_enrollment_flyout, .success_step') || /^success/i.test(hidFlyoutType ? hidFlyoutType.value : '');
  const error = !!document.querySelector('#_error_enrollment_flyout, .error_initial_panel, .error_exchange_invalid_panel') || /^error/i.test(hidFlyoutType ? hidFlyoutType.value : '');
  const errorText = normalize((document.querySelector('#_error_enrollment_flyout, .error_initial_panel, .error_exchange_invalid_panel') || {{}}).innerText);

  return JSON.stringify({{
    readyState: document.readyState,
    url: window.location.href,
    title: document.title,
    loginRequired: /UCLA Logon/i.test(document.title) || /Sign In/i.test(document.title) || /multi-factor/i.test(bodyText) || /Duo/i.test(bodyText),
    bodySnippet: bodyText.slice(0, 2000),
    rowCount: rows.length,
    rows: rows,
    primaryRow: primaryRow,
    secondaryRows: secondaryRows,
    preferredSecondaryRow: preferredSecondaryRow,
    warningCheckboxCount: warningBoxes.length,
    warningCheckboxCheckedCount: warningBoxes.filter((box) => box.checked).length,
    pteRequired: !!(pteInput && isVisible(pteInput)),
    enrollButtonVisible: !!(enrollBtn && isVisible(enrollBtn)),
    enrollButtonEnabled: !!(enrollBtn && isVisible(enrollBtn) && !enrollBtn.disabled),
    waitlistButtonVisible: !!(waitlistBtn && isVisible(waitlistBtn)),
    waitlistButtonEnabled: !!(waitlistBtn && isVisible(waitlistBtn) && !waitlistBtn.disabled),
    success: success,
    error: error,
    hidFlyoutType: hidFlyoutType ? hidFlyoutType.value : '',
    errorText: errorText
  }});
}})({primary_section_json}, {secondary_section_json});
"""


def _build_search_action_script(*, term: str, subject: str, catalog: str, course_title: str | None = None) -> str:
    term_json = json.dumps(term)
    subject_json = json.dumps(subject)
    catalog_json = json.dumps(catalog)
    subject_display_json = json.dumps(subject)
    catalog_display_json = json.dumps(course_title or catalog)
    return f"""
(function(termCode, subjectCode, catalogValue, subjectDisplay, catalogDisplay) {{
  function dispatch(el, eventName) {{
    if (!el) return;
    el.dispatchEvent(new Event(eventName, {{ bubbles: true }}));
  }}

  const searchBy = document.getElementById('search_by');
  if (searchBy && String(searchBy.value || '').toLowerCase() !== 'subject') {{
    searchBy.value = 'subject';
    dispatch(searchBy, 'change');
    return JSON.stringify({{ step: 'changed_search_by' }});
  }}

  const termSelect = document.getElementById('optSelectTerm') || document.querySelector('.select_filter_term');
  if (termSelect && termCode && termSelect.value !== termCode) {{
    termSelect.value = termCode;
    dispatch(termSelect, 'change');
    return JSON.stringify({{ step: 'changed_term', value: termSelect.value }});
  }}

  const subjectAutocomplete = document.getElementById('select_filter_subject');
  const catalogAutocomplete = document.getElementById('select_filter_catalog');
  const subjectHidden = document.getElementById('subject_area');
  const catalogHidden = document.getElementById('catalog');
  const classNumberHidden = document.getElementById('subjectArea_classNo');
  const goButton = document.getElementById('btn_go');

  if (!subjectAutocomplete || !catalogAutocomplete || !subjectHidden || !catalogHidden || !classNumberHidden || !goButton) {{
    return JSON.stringify({{ step: 'waiting_for_search_fields' }});
  }}

  subjectAutocomplete.setAttribute('full_input_value', JSON.stringify({{
    text: subjectDisplay,
    value: subjectCode
  }}));
  subjectAutocomplete.setAttribute('input_value', subjectDisplay);

  catalogAutocomplete.setAttribute('full_input_value', JSON.stringify({{
    text: catalogDisplay,
    value: {{
      crs_catlg_no: catalogValue,
      class_no: ''
    }}
  }}));
  catalogAutocomplete.setAttribute('input_value', catalogDisplay);

  subjectHidden.value = subjectCode;
  catalogHidden.value = catalogValue;
  classNumberHidden.value = '';
  goButton.click();
  return JSON.stringify({{ step: 'clicked_go' }});
}})({term_json}, {subject_json}, {catalog_json}, {subject_display_json}, {catalog_display_json});
"""


def _build_selection_action_script(primary_section: str, secondary_section: str | None = None) -> str:
    primary_section_json = json.dumps(primary_section)
    secondary_section_json = json.dumps(secondary_section or "")
    return f"""
(function(primarySectionText, secondarySectionText) {{
  function normalize(value) {{
    return (value || '').replace(/\\s+/g, ' ').trim();
  }}
  function isVisible(el) {{
    if (!el) return false;
    const style = window.getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden' && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  }}
  function getRows() {{
    return Array.from(document.querySelectorAll('.row-fluid.data_row.class-info'));
  }}

  const rows = getRows();
  const primaryRow = rows.find((row) => normalize((row.querySelector('.sectionColumn') || {{}}).innerText).includes(primarySectionText));
  if (!primaryRow) {{
    return JSON.stringify({{ step: 'primary_row_not_found' }});
  }}

  const primaryCheckbox = primaryRow.querySelector(\"input[type='checkbox'][id$='-checkbox']\");
  const secondaryRows = rows.filter((row) => row.id && row.id !== primaryRow.id);
  const preferredSecondaryRow = secondarySectionText
    ? secondaryRows.find((row) => normalize((row.querySelector('.sectionColumn') || {{}}).innerText).includes(secondarySectionText))
    : null;
  const anySecondaryRow = secondaryRows.find((row) => row.querySelector(\"input[type='checkbox'][id$='-checkbox']\"));
  const targetSecondaryRow = preferredSecondaryRow || anySecondaryRow || null;

  if (!targetSecondaryRow && secondaryRows.length === 0) {{
    const expando = document.getElementById(primaryRow.id + '-expando');
    if (expando && isVisible(expando)) {{
      expando.click();
      return JSON.stringify({{ step: 'expanded_primary_row', rowId: primaryRow.id }});
    }}
  }}

  if (primaryCheckbox && !primaryCheckbox.checked) {{
    primaryCheckbox.click();
    return JSON.stringify({{ step: 'clicked_primary_checkbox', rowId: primaryRow.id }});
  }}

  if (secondarySectionText && !preferredSecondaryRow) {{
    const expando = document.getElementById(primaryRow.id + '-expando');
    if (expando && isVisible(expando)) {{
      expando.click();
      return JSON.stringify({{ step: 'expanded_primary_row_for_secondary', rowId: primaryRow.id }});
    }}
    return JSON.stringify({{ step: 'preferred_secondary_not_found', section: secondarySectionText }});
  }}

  if (targetSecondaryRow) {{
    const targetCheckbox = targetSecondaryRow.querySelector(\"input[type='checkbox'][id$='-checkbox']\");
    const targetSection = normalize((targetSecondaryRow.querySelector('.sectionColumn') || {{}}).innerText);
    if (targetCheckbox && !targetCheckbox.checked && !targetCheckbox.disabled) {{
      targetCheckbox.click();
      return JSON.stringify({{ step: 'clicked_secondary_checkbox', rowId: targetSecondaryRow.id, section: targetSection }});
    }}
  }}

  const warningBoxes = Array.from(document.querySelectorAll('.enroll_warning_flyout_warningCheckbox')).filter(isVisible);
  const uncheckedWarnings = warningBoxes.filter((box) => !box.checked);
  if (uncheckedWarnings.length) {{
    uncheckedWarnings.forEach((box) => box.click());
    return JSON.stringify({{ step: 'checked_warning_boxes', count: uncheckedWarnings.length }});
  }}

  const pteInput = document.querySelector('#txtbox_enroll_warning_pte_flyout_pte');
  if (pteInput && isVisible(pteInput)) {{
    return JSON.stringify({{ step: 'pte_required' }});
  }}

  const enrollButton = document.getElementById('btn_Enroll');
  if (enrollButton && isVisible(enrollButton) && !enrollButton.disabled) {{
    enrollButton.click();
    return JSON.stringify({{ step: 'clicked_btn_Enroll' }});
  }}

  const waitlistButton = document.getElementById('btn_enrollmentAction_Enroll');
  if (waitlistButton && isVisible(waitlistButton) && !waitlistButton.disabled) {{
    waitlistButton.click();
    return JSON.stringify({{ step: 'clicked_btn_enrollmentAction_Enroll' }});
  }}

  return JSON.stringify({{ step: 'noop' }});
}})({primary_section_json}, {secondary_section_json});
"""


def _wait_for_page_ready(
    *,
    url: str,
    chrome_app_name: str,
    timeout_seconds: int,
    logger: logging.Logger,
) -> dict[str, Any]:
    _open_chrome_tab(url, chrome_app_name)
    deadline = time.time() + timeout_seconds
    last_state: dict[str, Any] = {}

    while time.time() < deadline:
        time.sleep(DEFAULT_POLL_INTERVAL_SECONDS)
        state = _json_or_empty(
            _execute_chrome_javascript(
                "JSON.stringify({readyState: document.readyState, title: document.title, href: location.href})",
                chrome_app_name,
            )
        )
        last_state = state or last_state
        if state.get("readyState") == "complete":
            return state

    logger.info("Timed out waiting for Chrome tab to finish loading %s.", url)
    return last_state


def open_login_page(
    *,
    login_url: str = DEFAULT_MYUCLA_LOGIN_URL,
    chrome_app_name: str = DEFAULT_CHROME_APP_NAME,
) -> None:
    _require_macos()
    _open_chrome_tab(login_url, chrome_app_name)


def self_test(
    *,
    chrome_app_name: str = DEFAULT_CHROME_APP_NAME,
    logger: logging.Logger | None = None,
) -> AutoEnrollResult:
    _require_macos()
    logger = logger or logging.getLogger("myucla-auto-enroll")
    url = "https://sa.ucla.edu/ro/Public/SOC"
    _open_chrome_tab(url, chrome_app_name)
    time.sleep(2)
    result = _execute_chrome_javascript(
        "JSON.stringify({title: document.title, readyState: document.readyState, href: location.href})",
        chrome_app_name,
    )
    payload = _json_or_empty(result)
    if payload.get("readyState") == "complete":
        return AutoEnrollResult("success", f"Chrome automation is working on {payload.get('href', url)}.", payload)
    return AutoEnrollResult("error", "Could not verify Chrome JavaScript execution.", payload)


def attempt_auto_enroll(
    *,
    term: str,
    subject: str,
    catalog: str,
    section: str,
    course_title: str | None = None,
    secondary_section: str | None = None,
    logger: logging.Logger,
    config: dict[str, Any] | None = None,
) -> AutoEnrollResult:
    _require_macos()
    config = config or {}

    chrome_app_name = str(config.get("chrome_app_name") or DEFAULT_CHROME_APP_NAME)
    search_page_url = str(config.get("search_page_url") or DEFAULT_MYUCLA_SEARCH_URL)
    open_timeout = int(config.get("open_timeout_seconds") or DEFAULT_OPEN_TIMEOUT_SECONDS)
    action_timeout = int(config.get("action_timeout_seconds") or DEFAULT_ACTION_TIMEOUT_SECONDS)

    logger.info("Opening MyUCLA class-search page for auto-enroll: %s", search_page_url)

    initial_state = _wait_for_page_ready(
        url=search_page_url,
        chrome_app_name=chrome_app_name,
        timeout_seconds=open_timeout,
        logger=logger,
    )
    if initial_state.get("loginRequired"):
        return AutoEnrollResult(
            "login_required",
            "MyUCLA login is required in your local Chrome session. Run the setup command and finish Duo once.",
            initial_state,
        )

    deadline = time.time() + action_timeout
    last_state = initial_state
    search_started = False

    while time.time() < deadline:
        state = _json_or_empty(_execute_chrome_javascript(_build_page_state_script(section, secondary_section), chrome_app_name))
        if state:
            last_state = state

        if last_state.get("loginRequired"):
            return AutoEnrollResult(
                "login_required",
                "MyUCLA session expired before auto-enroll could complete.",
                last_state,
            )
        if last_state.get("success"):
            detail_suffix = f" with {secondary_section}" if secondary_section else ""
            return AutoEnrollResult("success", f"MyUCLA reported a successful enroll flow for {section}{detail_suffix}.", last_state)
        if last_state.get("error"):
            return AutoEnrollResult("error", f"MyUCLA returned an enrollment error for {section}.", last_state)
        if last_state.get("pteRequired"):
            return AutoEnrollResult(
                "manual_review",
                "Enrollment flow is asking for a PTE number or another manual review step.",
                last_state,
            )
        if last_state.get("primaryRow"):
            break

        if "/Results?" in str(last_state.get("url", "")) and "No results found" in str(last_state.get("bodySnippet", "")):
            return AutoEnrollResult(
                "not_found",
                f"Could not find section {section!r} on the MyUCLA results page.",
                last_state,
            )

        search_result = _json_or_empty(
            _execute_chrome_javascript(
                _build_search_action_script(term=term, subject=subject, catalog=catalog, course_title=course_title),
                chrome_app_name,
            )
        )
        search_step = str(search_result.get("step", "unknown"))
        if search_step != "clicked_go" or not search_started:
            logger.info("Auto-enroll search action for %s: %s", section, search_step)
        if search_step == "clicked_go":
            search_started = True
        time.sleep(DEFAULT_POLL_INTERVAL_SECONDS)

    if not last_state.get("primaryRow"):
        return AutoEnrollResult(
            "timeout",
            f"Timed out while loading {section} on the MyUCLA results page.",
            last_state,
        )

    while time.time() < deadline:
        state = _json_or_empty(_execute_chrome_javascript(_build_page_state_script(section, secondary_section), chrome_app_name))
        if state:
            last_state = state

        if last_state.get("loginRequired"):
            return AutoEnrollResult(
                "login_required",
                "MyUCLA session expired before auto-enroll could complete.",
                last_state,
            )
        if last_state.get("success"):
            detail_suffix = f" with {secondary_section}" if secondary_section else ""
            return AutoEnrollResult("success", f"MyUCLA reported a successful enroll flow for {section}{detail_suffix}.", last_state)
        if last_state.get("error"):
            return AutoEnrollResult("error", f"MyUCLA returned an enrollment error for {section}.", last_state)
        if last_state.get("pteRequired"):
            return AutoEnrollResult(
                "manual_review",
                "Enrollment flow is asking for a PTE number or another manual review step.",
                last_state,
            )

        action_result = _json_or_empty(
            _execute_chrome_javascript(_build_selection_action_script(section, secondary_section), chrome_app_name)
        )
        logger.info("Auto-enroll browser action for %s: %s", section, action_result.get("step", "unknown"))

        if action_result.get("step") == "pte_required":
            return AutoEnrollResult(
                "manual_review",
                "Enrollment flow requires a PTE number or another protected manual step.",
                {**last_state, **action_result},
            )
        if action_result.get("step") == "preferred_secondary_not_found":
            return AutoEnrollResult(
                "not_found",
                f"Could not find the requested secondary section {secondary_section!r} for {section}.",
                {**last_state, **action_result},
            )

        time.sleep(DEFAULT_POLL_INTERVAL_SECONDS)

    return AutoEnrollResult(
        "timeout",
        f"Timed out while waiting for MyUCLA to finish the enroll flow for {section}.",
        last_state,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local MyUCLA auto-enroll helper for Bruin Class Alert.")
    parser.add_argument("--setup-login", action="store_true", help="Open the MyUCLA Find a Class and Enroll page in Chrome for manual login.")
    parser.add_argument("--self-test", action="store_true", help="Open a public UCLA page in Chrome and verify JavaScript control works.")
    parser.add_argument("--term", help="UCLA term code, for example 26S.")
    parser.add_argument("--subject", help="UCLA subject code, for example GEOG.")
    parser.add_argument("--catalog", help="UCLA catalog number, for example 0007.")
    parser.add_argument("--section", help="Section label, for example Lec 1.")
    parser.add_argument("--course-title", help="Optional course title text to show in the MyUCLA search UI.")
    parser.add_argument("--secondary-section", help="Optional secondary section label, for example Dis 1J.")
    parser.add_argument("--debug", action="store_true", help="Enable verbose logging.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("myucla-auto-enroll")

    try:
        if args.setup_login:
            open_login_page()
            logger.info("Opened MyUCLA login page in Chrome. Sign in there once, then future auto-enroll attempts can reuse that session.")
            return 0

        if args.self_test:
            result = self_test(logger=logger)
            logger.info("%s", result.message)
            return 0 if result.status == "success" else 1

        if not all([args.term, args.subject, args.catalog, args.section]):
            logger.error("term, subject, catalog, and section are required unless using --setup-login or --self-test.")
            return 2

        result = attempt_auto_enroll(
            term=args.term,
            subject=args.subject,
            catalog=args.catalog,
            section=args.section,
            course_title=args.course_title,
            secondary_section=args.secondary_section,
            logger=logger,
        )
        logger.info("Auto-enroll result: %s - %s", result.status, result.message)
        return 0 if result.status == "success" else 1
    except AutoEnrollError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
