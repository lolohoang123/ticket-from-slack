import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

def slack_escape(value):
    '''Escape a string for Slack formatting, replacing &, <, and > with HTML entities.'''
    value = str(value or "")
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def format_duration(seconds):
    '''Format a duration in seconds into a human-readable string like "1h 30m" or "45m".'''
    minutes = max(0, int(seconds)) // 60
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"

def json_response(status_code, body):
    '''Return a JSON response with the given status code and body.'''
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(body) if not isinstance(body, str) else body,
    }

def format_jira_started(date_str: str, time_str: str) -> str:
    """Convert date + time to Jira's required datetime format."""
    tz = ZoneInfo(os.getenv("JIRA_TIMEZONE", "Asia/Ho_Chi_Minh"))

    local_dt = datetime.strptime(
        f"{date_str} {time_str}",
        "%Y-%m-%d %H:%M"
    ).replace(tzinfo=tz)

    return local_dt.strftime("%Y-%m-%dT%H:%M:%S.000%z")

def validate_duration(duration):
    """Validate that the duration string is in a valid Jira format."""
    # Accept Jira-style durations such as 30m, 1h, 1h 30m, 1d, 1w.
    if not re.fullmatch(
        r"(?i)(?=.*\d)\s*(?:\d+\s*w\s*)?(?:\d+\s*d\s*)?"
        r"(?:\d+\s*h\s*)?(?:\d+\s*m\s*)?",
        duration,
    ):
        raise ValueError("Duration must look like 30m, 1h, 1h 30m, or 1d.")
    if not re.search(r"\d", duration):
        raise ValueError("Duration is required.")
    return duration

def comment_to_text(comment):
    """Convert a Jira comment object to plain text."""
    if not isinstance(comment, dict):
        return ""

    parts = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text" and node.get("text"):
                parts.append(node["text"])
            for child in node.get("content", []):
                walk(child)

    walk(comment)
    return " ".join(" ".join(parts).split())
