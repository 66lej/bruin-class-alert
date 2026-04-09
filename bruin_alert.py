#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import smtplib
import subprocess
import sys
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from myucla_auto_enroll import AutoEnrollResult, attempt_auto_enroll

BASE_URL = "https://sa.ucla.edu/ro"
SOC_URL = f"{BASE_URL}/Public/SOC"
RESULTS_URL = f"{BASE_URL}/Public/SOC/Results"
DEFAULT_CONFIG_PATH = "config.json"
DEFAULT_STATE_PATH = ".bruin_alert_state.json"
DEFAULT_POLL_INTERVAL_SECONDS = 60
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_REQUEST_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 2.0
USER_AGENT = "bruin-class-alert/1.0"
DEFAULT_DOTENV_PATH = ".env"

LEFT_RE = re.compile(r"(?P<left>\d+)\s+of\s+(?P<cap>\d+)\s+left", re.IGNORECASE)
TAKEN_RE = re.compile(r"(?P<taken>\d+)\s+of\s+(?P<cap>\d+)\s+taken", re.IGNORECASE)
SEARCH_PANEL_SUBJECT_RE = re.compile(
    r"SearchPanelSetup\('(?P<payload>\[.*?\])',\s*'select_filter_subject'",
    re.DOTALL,
)


class ConfigError(Exception):
    """Raised when the configuration file is invalid."""


@dataclass(frozen=True)
class SectionStatus:
    row_id: str
    course_title: str
    course_label: str
    section: str
    status: str
    waitlist: str
    days: str
    time: str
    location: str
    units: str
    instructor: str
    detail_url: str | None
    seats_left: int | None
    seat_capacity: int | None
    waitlist_taken: int | None
    waitlist_capacity: int | None
    is_open: bool
    waitlist_has_space: bool


def collapse_ws(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(value.split())


def compact_key(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", collapse_ws(value).upper())


def looks_like_subject_code(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9 .&/-]*", collapse_ws(value).upper()))


def clean_text(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return collapse_ws(node)

    text_parts = [str(piece).strip() for piece in node.find_all(string=True)]
    if not text_parts and getattr(node, "string", None) is not None:
        text_parts = [str(node.string).strip()]
    return collapse_ws(" ".join(part for part in text_parts if part))


def dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file is not valid JSON: {path}: {exc}") from exc


def save_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_local_dotenv(path: Path, logger: logging.Logger) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not os.environ.get(key):
            os.environ[key] = value

    logger.debug("Loaded environment variables from %s.", path)


def resolve_config_value(config: dict[str, Any], key: str, default: str = "") -> str:
    env_key = config.get(f"{key}_env")
    if env_key:
        env_value = os.environ.get(str(env_key), "")
        if env_value:
            return str(env_value)

    direct_value = config.get(key, default)
    if direct_value is None:
        return ""
    return str(direct_value)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def fetch_text(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None,
    timeout: int,
    retries: int = DEFAULT_REQUEST_RETRIES,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    logger: logging.Logger | None = None,
) -> str:
    attempts = max(1, retries)
    last_error: requests.RequestException | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= attempts:
                break

            delay_seconds = retry_backoff_seconds * attempt
            if logger is not None:
                logger.info(
                    "Request to %s failed on attempt %d/%d: %s. Retrying in %.1fs.",
                    url,
                    attempt,
                    attempts,
                    exc,
                    delay_seconds,
                )
            time.sleep(delay_seconds)

    if last_error is not None:
        raise last_error
    raise RuntimeError("fetch_text() exited without a response or captured error.")


def fetch_soc_page(
    session: requests.Session,
    timeout: int,
    *,
    retries: int = DEFAULT_REQUEST_RETRIES,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    logger: logging.Logger | None = None,
) -> str:
    return fetch_text(
        session,
        SOC_URL,
        params=None,
        timeout=timeout,
        retries=retries,
        retry_backoff_seconds=retry_backoff_seconds,
        logger=logger,
    )


def parse_terms(page_html: str) -> dict[str, str]:
    soup = BeautifulSoup(page_html, "html.parser")
    select = soup.select_one("select#optSelectTerm")
    if select is None:
        raise ConfigError("Could not find the UCLA term selector on the Schedule of Classes page.")

    terms: dict[str, str] = {}
    for option in select.select("option"):
        value = collapse_ws(option.get("value"))
        label = collapse_ws(option.get("data-yeartext") or option.get_text(" ", strip=True))
        if value and label:
            terms[value] = label
    return terms


def parse_subjects(page_html: str) -> list[dict[str, str]]:
    match = SEARCH_PANEL_SUBJECT_RE.search(page_html)
    if match is None:
        raise ConfigError("Could not extract the UCLA subject list from the Schedule of Classes page.")

    payload = html.unescape(match.group("payload"))
    raw_subjects = json.loads(payload)
    subjects: list[dict[str, str]] = []
    for subject in raw_subjects:
        label = collapse_ws(subject.get("label"))
        value = collapse_ws(subject.get("value"))
        if label and value:
            subjects.append({"label": label, "value": value})
    return subjects


def resolve_term_code(user_value: str, terms: dict[str, str]) -> str:
    query = collapse_ws(user_value)
    if not query:
        raise ConfigError("Each watch entry must include a non-empty term.")

    query_upper = query.upper()
    if query in terms:
        return query
    for code, label in terms.items():
        if query_upper == label.upper():
            return code
    raise ConfigError(f"Unknown UCLA term: {user_value!r}. Run --list-terms to see valid values.")


def resolve_subject_code(user_value: str, subjects: list[dict[str, str]]) -> str:
    query = collapse_ws(user_value)
    if not query:
        raise ConfigError("Each watch entry must include a non-empty subject.")

    query_upper = query.upper()
    query_compact = compact_key(query)
    exact_matches: list[str] = []
    loose_matches: list[str] = []

    for subject in subjects:
        value = collapse_ws(subject["value"])
        label = collapse_ws(subject["label"])
        label_prefix = collapse_ws(label.split("(")[0])
        parenthetical_match = re.search(r"\(([^)]+)\)$", label)
        parenthetical = collapse_ws(parenthetical_match.group(1)) if parenthetical_match else ""

        comparisons = [
            value.upper(),
            label.upper(),
            label_prefix.upper(),
            parenthetical.upper(),
        ]
        compact_comparisons = {compact_key(item) for item in comparisons if item}

        if query_upper in comparisons:
            exact_matches.append(value)
        elif query_compact and query_compact in compact_comparisons:
            loose_matches.append(value)

    matches = dedupe_keep_order(exact_matches or loose_matches)
    if len(matches) == 1:
        return matches[0]
    if not matches and looks_like_subject_code(query):
        return query_upper
    if not matches:
        raise ConfigError(
            f"Unknown UCLA subject: {user_value!r}. Use the official subject code like 'COM SCI' or run --list-subjects."
        )
    raise ConfigError(
        f"Subject {user_value!r} matched multiple UCLA subjects: {', '.join(matches)}. Please be more specific."
    )


def generate_catalog_candidates(raw_catalog: str) -> list[str]:
    catalog = re.sub(r"\s+", "", raw_catalog.upper())
    if not catalog:
        raise ConfigError("Each watch entry must include a non-empty catalog/course number.")

    candidates = [catalog]

    digits_suffix = re.fullmatch(r"(\d+)([A-Z]*)", catalog)
    if digits_suffix:
        digits, suffix = digits_suffix.groups()
        candidates.append(f"{int(digits):04d}{suffix}")

    prefix_digits_suffix = re.fullmatch(r"([A-Z]+)(\d+)([A-Z]*)", catalog)
    if prefix_digits_suffix:
        prefix, digits, suffix = prefix_digits_suffix.groups()
        candidates.append(f"{prefix}{int(digits):04d}{suffix}")

    return dedupe_keep_order(candidates)


def parse_int_pair(pattern: re.Pattern[str], value: str) -> tuple[int | None, int | None]:
    match = pattern.search(value)
    if match is None:
        return None, None
    return int(match.group("left" if "left" in match.groupdict() else "taken")), int(match.group("cap"))


def normalize_time_text(days: str, time_text: str) -> str:
    cleaned_days = collapse_ws(days)
    cleaned_time = collapse_ws(time_text)
    if not cleaned_days or not cleaned_time:
        return cleaned_time

    prefix = f"{cleaned_days} "
    while cleaned_time.upper().startswith(prefix.upper()):
        cleaned_time = collapse_ws(cleaned_time[len(prefix) :])
    return cleaned_time


def parse_section_rows(page_html: str) -> list[SectionStatus]:
    soup = BeautifulSoup(page_html, "html.parser")
    output: list[SectionStatus] = []

    for result in soup.select("#divClassNames .results"):
        title_node = result.select_one(".row-fluid.class-title h3.head button")
        course_title = clean_text(title_node)
        course_label = course_title.split(" - ", 1)[0] if " - " in course_title else course_title

        for row in result.select("div[id$='-children'] > .row-fluid.data_row.class-info"):
            row_id = row.get("id", "")
            section_anchor = row.select_one(".sectionColumn a")
            section_fallback = row.select_one(".sectionColumn [data-poload]")
            section = clean_text(section_anchor or section_fallback or row.select_one(".sectionColumn"))
            status = clean_text(row.select_one(".statusColumn"))
            waitlist = clean_text(row.select_one(".waitlistColumn"))
            days = clean_text(row.select_one(".dayColumn"))
            time_text = normalize_time_text(days, clean_text(row.select_one(".timeColumn")))
            location = clean_text(row.select_one(".locationColumn"))
            units = clean_text(row.select_one(".unitsColumn"))
            instructor = clean_text(row.select_one(".instructorColumn"))

            detail_href = section_anchor.get("href") if section_anchor and section_anchor.has_attr("href") else None
            detail_url = urljoin(BASE_URL, detail_href) if detail_href else None

            seats_left, seat_capacity = parse_int_pair(LEFT_RE, status)
            waitlist_taken, waitlist_capacity = parse_int_pair(TAKEN_RE, waitlist)

            is_open = seats_left is not None and seats_left > 0
            if not is_open and status.lower().startswith("open"):
                is_open = True

            waitlist_has_space = (
                waitlist_taken is not None
                and waitlist_capacity is not None
                and waitlist_taken < waitlist_capacity
            )

            if not any([row_id, section, status, waitlist, course_title]):
                continue

            output.append(
                SectionStatus(
                    row_id=row_id or f"{course_label}:{section}",
                    course_title=course_title,
                    course_label=course_label,
                    section=section,
                    status=status,
                    waitlist=waitlist,
                    days=days,
                    time=time_text,
                    location=location,
                    units=units,
                    instructor=instructor,
                    detail_url=detail_url,
                    seats_left=seats_left,
                    seat_capacity=seat_capacity,
                    waitlist_taken=waitlist_taken,
                    waitlist_capacity=waitlist_capacity,
                    is_open=is_open,
                    waitlist_has_space=waitlist_has_space,
                )
            )

    return output


def section_matches(requested: str | None, actual: str) -> bool:
    if not requested:
        return True

    requested_compact = compact_key(requested)
    actual_compact = compact_key(actual)
    if not requested_compact or not actual_compact:
        return False
    return (
        requested_compact == actual_compact
        or actual_compact.endswith(requested_compact)
        or requested_compact.endswith(actual_compact)
    )


def resolve_sections_for_watch(
    session: requests.Session,
    watch: dict[str, Any],
    *,
    timeout: int,
    retries: int,
    retry_backoff_seconds: float,
    logger: logging.Logger,
) -> tuple[str | None, list[SectionStatus]]:
    candidates = generate_catalog_candidates(str(watch["catalog"]))
    cached_candidate = watch.get("_resolved_catalog")
    ordered_candidates = dedupe_keep_order(([cached_candidate] if cached_candidate else []) + candidates)

    for candidate in ordered_candidates:
        params: dict[str, Any] = {
            "t": watch["_term_code"],
            "sBy": "subject",
            "subj": watch["_subject_code"],
            "catlg": candidate,
        }
        if watch.get("session_group"):
            params["s_g_cd"] = watch["session_group"]

        page_html = fetch_text(
            session,
            RESULTS_URL,
            params=params,
            timeout=timeout,
            retries=retries,
            retry_backoff_seconds=retry_backoff_seconds,
            logger=logger,
        )
        sections = parse_section_rows(page_html)
        if sections:
            watch["_resolved_catalog"] = candidate
            return candidate, sections

    logger.warning(
        "No UCLA sections found for %s (%s %s) after trying catalog values: %s",
        watch["_name"],
        watch["_subject_code"],
        watch["catalog"],
        ", ".join(ordered_candidates),
    )
    return None, []


def build_watch_id(watch: dict[str, Any]) -> str:
    fingerprint_source = json.dumps(
        {
            "term": watch["_term_code"],
            "subject": watch["_subject_code"],
            "catalog": str(watch["catalog"]),
            "section": watch.get("section"),
            "session_group": watch.get("session_group"),
        },
        sort_keys=True,
    )
    return hashlib.sha1(fingerprint_source.encode("utf-8")).hexdigest()[:12]


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"open_alerts": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"open_alerts": []}
    if not isinstance(payload, dict):
        return {"open_alerts": []}
    open_alerts = payload.get("open_alerts")
    if not isinstance(open_alerts, list):
        payload["open_alerts"] = []
    return payload


def persist_state(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    save_json_file(tmp_path, payload)
    tmp_path.replace(path)


def build_long_message(watch: dict[str, Any], section: SectionStatus) -> str:
    lines = [
        f"{watch['_subject_code']} {watch['_resolved_catalog'] or watch['catalog']} {section.section} is open.",
        f"Course: {section.course_title}",
        f"Status: {section.status}",
    ]
    if section.waitlist:
        lines.append(f"Waitlist: {section.waitlist}")
    if section.days or section.time:
        lines.append(f"Schedule: {collapse_ws(f'{section.days} {section.time}')}")
    if section.location:
        lines.append(f"Location: {section.location}")
    if section.instructor:
        lines.append(f"Instructor: {section.instructor}")
    if section.detail_url:
        lines.append(f"Detail: {section.detail_url}")
    return "\n".join(lines)


def format_auto_enroll_line(result: AutoEnrollResult) -> str:
    return f"Auto-enroll: {result.status} - {result.message}"


def build_short_message(section: SectionStatus) -> str:
    parts = [section.section, section.status]
    if section.days or section.time:
        parts.append(collapse_ws(f"{section.days} {section.time}"))
    if section.location:
        parts.append(section.location)
    return " | ".join(part for part in parts if part)


class Notifier:
    def notify(self, title: str, body: str) -> None:
        raise NotImplementedError


class StdoutNotifier(Notifier):
    def notify(self, title: str, body: str) -> None:
        print("=" * 80)
        print(title)
        print(body)


class MacOSNotifier(Notifier):
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def notify(self, title: str, body: str) -> None:
        def escape(value: str) -> str:
            return value.replace("\\", "\\\\").replace('"', '\\"')

        try:
            completed = subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{escape(body)}" with title "{escape(title)}"',
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                self.logger.warning(
                    "macOS notification failed with exit code %s: %s",
                    completed.returncode,
                    collapse_ws(completed.stderr),
                )
        except FileNotFoundError:
            self.logger.warning("macOS notification skipped because 'osascript' is not available on this machine.")


class DiscordWebhookNotifier(Notifier):
    def __init__(self, url: str, session: requests.Session) -> None:
        self.url = url
        self.session = session

    def notify(self, title: str, body: str) -> None:
        response = self.session.post(
            self.url,
            json={"content": f"**{title}**\n{body}"},
            timeout=20,
        )
        response.raise_for_status()


class EmailNotifier(Notifier):
    def __init__(self, config: dict[str, Any]) -> None:
        password = resolve_config_value(config, "password")
        username = resolve_config_value(config, "username")
        to_email = resolve_config_value(config, "to_email")
        from_email = resolve_config_value(config, "from_email", username)

        if not password:
            raise ConfigError(
                "Email notifier is enabled, but no SMTP password was found. Set password_env or password."
            )
        if not username:
            raise ConfigError("Email notifier is enabled, but no SMTP username was found.")
        if not to_email:
            raise ConfigError("Email notifier is enabled, but no recipient email was found.")

        self.smtp_host = resolve_config_value(config, "smtp_host", "smtp.gmail.com")
        self.smtp_port = int(resolve_config_value(config, "smtp_port", "587"))
        self.use_tls = bool(config.get("use_tls", True))
        self.username = username
        self.password = password
        self.from_email = from_email or self.username
        self.to_emails = [item.strip() for item in to_email.split(",") if item.strip()]
        if not self.to_emails:
            raise ConfigError("Email notifier is enabled, but no valid recipient email was found.")

    def notify(self, title: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = title
        message["From"] = self.from_email
        message["To"] = ", ".join(self.to_emails)
        message.set_content(body)

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=20) as smtp:
            if self.use_tls:
                smtp.starttls()
            smtp.login(self.username, self.password)
            smtp.send_message(message, to_addrs=self.to_emails)


def build_notifiers(config: dict[str, Any], session: requests.Session, logger: logging.Logger) -> list[Notifier]:
    notifiers: list[Notifier] = [StdoutNotifier()]
    notifier_config = config.get("notifiers", {})
    if not isinstance(notifier_config, dict):
        raise ConfigError("'notifiers' must be a JSON object.")

    if notifier_config.get("macos", True):
        notifiers.append(MacOSNotifier(logger))

    discord_url = ""
    discord_env_key = notifier_config.get("discord_webhook_env")
    if discord_env_key:
        discord_url = collapse_ws(os.environ.get(str(discord_env_key), ""))
    if not discord_url:
        discord_url = collapse_ws(notifier_config.get("discord_webhook_url"))
    if discord_url:
        notifiers.append(DiscordWebhookNotifier(discord_url, session))
    elif discord_env_key:
        logger.info(
            "Discord notifier is configured via %s, but that environment variable is not set yet.",
            discord_env_key,
        )

    email_config = notifier_config.get("email")
    if isinstance(email_config, dict) and email_config.get("enabled"):
        try:
            notifiers.append(EmailNotifier(email_config))
        except ConfigError as exc:
            logger.info("Email notifier is enabled in config but not ready yet: %s", exc)

    return notifiers


def resolve_auto_enroll_config(config: dict[str, Any]) -> dict[str, Any] | None:
    auto_enroll_config = config.get("auto_enroll")
    if not isinstance(auto_enroll_config, dict):
        return None
    if not auto_enroll_config.get("enabled"):
        return None
    return dict(auto_enroll_config)


def should_attempt_auto_enroll(
    auto_enroll_config: dict[str, Any] | None,
    section: SectionStatus,
) -> bool:
    if not auto_enroll_config:
        return False
    if section.is_open:
        return True
    return bool(auto_enroll_config.get("allow_waitlist_auto_enroll", False) and section.waitlist_has_space)


def notify_all(notifiers: list[Notifier], title: str, body: str, logger: logging.Logger) -> None:
    failures: list[str] = []
    for notifier in notifiers:
        try:
            notifier.notify(title, body)
        except Exception as exc:  # pragma: no cover - best effort per channel
            failures.append(f"{notifier.__class__.__name__}: {exc}")
    if failures:
        logger.warning("Some notification channels failed: %s", "; ".join(failures))


def validate_watchlist(config: dict[str, Any], terms: dict[str, str], subjects: list[dict[str, str]]) -> list[dict[str, Any]]:
    watchlist = config.get("watchlist")
    if not isinstance(watchlist, list) or not watchlist:
        raise ConfigError("Config must contain a non-empty 'watchlist' array.")

    prepared: list[dict[str, Any]] = []
    for raw_watch in watchlist:
        if not isinstance(raw_watch, dict):
            raise ConfigError("Each watchlist entry must be a JSON object.")

        term = raw_watch.get("term")
        subject = raw_watch.get("subject")
        catalog = raw_watch.get("catalog")
        if term is None or subject is None or catalog is None:
            raise ConfigError("Each watchlist entry must include 'term', 'subject', and 'catalog'.")

        watch = dict(raw_watch)
        watch["_term_code"] = resolve_term_code(str(term), terms)
        watch["_subject_code"] = resolve_subject_code(str(subject), subjects)
        watch["_name"] = collapse_ws(str(raw_watch.get("name") or f"{watch['_subject_code']} {catalog}"))
        watch["_watch_id"] = build_watch_id(watch)
        prepared.append(watch)

    return prepared


def log_sections(logger: logging.Logger, watch: dict[str, Any], sections: list[SectionStatus]) -> None:
    logger.debug("Resolved %s to %s and found %d section(s).", watch["_name"], watch["_resolved_catalog"], len(sections))
    for section in sections:
        logger.debug(
            "  %s | %s | waitlist=%s | %s %s | %s",
            section.section,
            section.status,
            section.waitlist or "-",
            section.days,
            section.time,
            section.location,
        )


def evaluate_watch(
    session: requests.Session,
    watch: dict[str, Any],
    *,
    timeout: int,
    retries: int,
    retry_backoff_seconds: float,
    logger: logging.Logger,
) -> list[SectionStatus]:
    _, sections = resolve_sections_for_watch(
        session,
        watch,
        timeout=timeout,
        retries=retries,
        retry_backoff_seconds=retry_backoff_seconds,
        logger=logger,
    )
    if logger.isEnabledFor(logging.DEBUG) and sections:
        log_sections(logger, watch, sections)

    requested_section = watch.get("section")
    notify_on_waitlist = bool(watch.get("notify_on_waitlist", False))

    matched_sections = [section for section in sections if section_matches(requested_section, section.section)]
    if requested_section and not matched_sections:
        logger.info(
            "No section matched %r for %s. Available sections: %s",
            requested_section,
            watch["_name"],
            ", ".join(section.section for section in sections) or "none",
        )

    alerts: list[SectionStatus] = []
    for section in matched_sections:
        if section.is_open:
            alerts.append(section)
        elif notify_on_waitlist and section.waitlist_has_space:
            alerts.append(section)
    return alerts


def run_cycle(
    session: requests.Session,
    notifiers: list[Notifier],
    watches: list[dict[str, Any]],
    *,
    timeout: int,
    retries: int,
    retry_backoff_seconds: float,
    auto_enroll_config: dict[str, Any] | None,
    state_path: Path,
    logger: logging.Logger,
) -> None:
    state = load_state(state_path)
    open_alerts = set(str(item) for item in state.get("open_alerts", []))

    for watch in watches:
        try:
            open_sections = evaluate_watch(
                session,
                watch,
                timeout=timeout,
                retries=retries,
                retry_backoff_seconds=retry_backoff_seconds,
                logger=logger,
            )
        except requests.RequestException as exc:
            logger.warning("Network error while checking %s: %s", watch["_name"], exc)
            continue
        except Exception as exc:
            logger.exception("Unexpected error while checking %s: %s", watch["_name"], exc)
            continue

        watch_prefix = f"{watch['_watch_id']}::"
        previous_for_watch = {key for key in open_alerts if key.startswith(watch_prefix)}
        current_for_watch = {f"{watch_prefix}{section.row_id}" for section in open_sections}

        for section in open_sections:
            section_key = f"{watch_prefix}{section.row_id}"
            if section_key in previous_for_watch:
                continue

            auto_enroll_result: AutoEnrollResult | None = None
            if should_attempt_auto_enroll(auto_enroll_config, section):
                try:
                    auto_enroll_result = attempt_auto_enroll(
                        term=watch["_term_code"],
                        subject=watch["_subject_code"],
                        catalog=str(watch.get("_resolved_catalog") or watch["catalog"]),
                        section=section.section,
                        course_title=section.course_title,
                        secondary_section=watch.get("secondary_section"),
                        logger=logger,
                        config=auto_enroll_config,
                    )
                    logger.info(
                        "Auto-enroll result for %s: %s (%s).",
                        watch["_name"],
                        auto_enroll_result.status,
                        auto_enroll_result.message,
                    )
                except Exception as exc:
                    logger.warning("Auto-enroll failed for %s: %s", watch["_name"], exc)

            title = f"Bruin Class Alert: {watch['_subject_code']} {watch['_resolved_catalog'] or watch['catalog']} {section.section}"
            body = build_long_message(watch, section)
            if auto_enroll_result is not None:
                body = f"{body}\n{format_auto_enroll_line(auto_enroll_result)}"
            notify_all(notifiers, title, body, logger)
            logger.info("Notification sent for %s (%s).", watch["_name"], build_short_message(section))

        open_alerts -= previous_for_watch - current_for_watch
        open_alerts |= current_for_watch

    persist_state(state_path, {"open_alerts": sorted(open_alerts)})


def print_terms(terms: dict[str, str]) -> None:
    for code, label in terms.items():
        print(f"{code}\t{label}")


def print_subjects(subjects: list[dict[str, str]]) -> None:
    for subject in subjects:
        print(f"{subject['value']}\t{subject['label']}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alert when a UCLA class section opens.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help=f"Path to the JSON config file. Default: {DEFAULT_CONFIG_PATH}")
    parser.add_argument("--state-file", default=DEFAULT_STATE_PATH, help=f"Path to the alert state file. Default: {DEFAULT_STATE_PATH}")
    parser.add_argument("--once", action="store_true", help="Check once and exit.")
    parser.add_argument("--list-terms", action="store_true", help="Print the current UCLA term codes and exit.")
    parser.add_argument("--list-subjects", action="store_true", help="Print the current UCLA subject codes and exit.")
    parser.add_argument("--debug", action="store_true", help="Enable verbose logging.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("bruin-alert")

    load_local_dotenv(Path(DEFAULT_DOTENV_PATH), logger)

    session = make_session()

    config: dict[str, Any] = {}
    config_path = Path(args.config)
    state_path = Path(args.state_file)
    request_retries = DEFAULT_REQUEST_RETRIES
    retry_backoff_seconds = DEFAULT_RETRY_BACKOFF_SECONDS
    auto_enroll_config: dict[str, Any] | None = None

    try:
        if not args.list_terms and not args.list_subjects:
            config = load_json_file(config_path)
            request_retries = int(config.get("request_retries", DEFAULT_REQUEST_RETRIES))
            retry_backoff_seconds = float(config.get("retry_backoff_seconds", DEFAULT_RETRY_BACKOFF_SECONDS))

        page_html = fetch_soc_page(
            session,
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
            retries=request_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            logger=logger,
        )
        terms = parse_terms(page_html)
        subjects = parse_subjects(page_html)

        if args.list_terms:
            print_terms(terms)
            return 0
        if args.list_subjects:
            print_subjects(subjects)
            return 0

        poll_interval = int(config.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS))
        timeout = int(config.get("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS))
        watches = validate_watchlist(config, terms, subjects)
        notifiers = build_notifiers(config, session, logger)
        auto_enroll_config = resolve_auto_enroll_config(config)
    except ConfigError as exc:
        logger.error(str(exc))
        return 2
    except requests.RequestException as exc:
        logger.error("Failed to reach the UCLA Schedule of Classes site: %s", exc)
        return 1

    logger.info("Loaded %d watch item(s). Poll interval: %ss.", len(watches), poll_interval)

    if args.once:
        run_cycle(
            session,
            notifiers,
            watches,
            timeout=timeout,
            retries=request_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            auto_enroll_config=auto_enroll_config,
            state_path=state_path,
            logger=logger,
        )
        return 0

    try:
        while True:
            run_cycle(
                session,
                notifiers,
                watches,
                timeout=timeout,
                retries=request_retries,
                retry_backoff_seconds=retry_backoff_seconds,
                auto_enroll_config=auto_enroll_config,
                state_path=state_path,
                logger=logger,
            )
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
