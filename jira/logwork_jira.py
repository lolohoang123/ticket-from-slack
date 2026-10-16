import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
from urllib.parse import parse_qs

logger = logging.getLogger()
logger.setLevel(logging.INFO)

lambda_client = boto3.client("lambda")


def format_jira_started(date_str: str, time_str: str) -> str:
    """Convert date + time to Jira's required datetime format."""
    tz = ZoneInfo(os.getenv("JIRA_TIMEZONE", "Asia/Ho_Chi_Minh"))

    local_dt = datetime.strptime(
        f"{date_str} {time_str}",
        "%Y-%m-%d %H:%M"
    ).replace(tzinfo=tz)

    return local_dt.strftime("%Y-%m-%dT%H:%M:%S.000%z")

# ---------- HTTP / Slack helpers ----------

def json_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(body) if not isinstance(body, str) else body,
    }


def get_raw_body(event):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return body


def verify_slack_signature(event, raw_body):
    signing_secret = os.environ["SLACK_SIGNING_SECRET"]
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")

    print("===============body=========")
    print(raw_body)

    if not timestamp or not signature:
        return False

    try:
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return False
    except ValueError:
        return False

    base_string = f"v0:{timestamp}:{raw_body}".encode("utf-8")
    digest = hmac.new(
        signing_secret.encode("utf-8"),
        base_string,
        hashlib.sha256,
    ).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


def slack_api(method, payload):
    token = os.environ["SLACK_BOT_TOKEN"]
    request = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError(
            f"Slack API {method} failed: {result.get('error', 'unknown_error')}"
        )
    return result


def post_slack_response(response_url, message):
    parsed = urllib.parse.urlparse(response_url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "hooks.slack.com",
        "hooks.slack-gov.com",
    }:
        raise ValueError("Invalid Slack response_url host")

    request = urllib.request.Request(
        response_url,
        data=json.dumps(message).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        response.read()


def slack_escape(value):
    value = str(value or "")
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- Jira API helpers ----------

def jira_request(path, params=None, method="GET", data=None):
    print("===========jira_request============")
    base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
    email = os.environ["JIRA_EMAIL"]
    api_token = os.environ["JIRA_API_TOKEN"]

    url = f"{base_url}{path}"
    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{url}{'&' if '?' in url else '?'}{query}"

    auth = base64.b64encode(f"{email}:{api_token}".encode("utf-8")).decode("ascii")
    body = None if data is None else json.dumps(data).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        logger.warning("Jira API error %s: %s", exc.code, details[:1000])
        raise RuntimeError(f"Jira API returned HTTP {exc.code}: {details[:500]}") from exc


def get_jira_account_id_for_slack_user(slack_user_id):
    mapping = json.loads(os.environ.get("SLACK_JIRA_USER_MAP", "{}"))
    account_id = mapping.get(slack_user_id)
    if not account_id:
        raise ValueError(
            f"No Jira accountId configured for Slack user {slack_user_id}"
        )
    return account_id


def find_issues(account_id, day):
    jql = (
        f'worklogAuthor = "{account_id}" AND '
        f'worklogDate = "{day.strftime("%Y/%m/%d")}"'
    )
    results = []
    next_page_token = None

    for _ in range(10):
        body = {
            "jql": jql,
            "fields": ["summary"],
            "maxResults": 100,
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token

        result = jira_request("/rest/api/3/search/jql", method="POST", data=body)
        results.extend(result.get("issues", []))
        next_page_token = result.get("nextPageToken")
        if not next_page_token:
            break

    return results


def get_issue_worklogs(issue_key, start_ms, end_ms):
    worklogs = []
    start_at = 0

    for _ in range(20):
        result = jira_request(
            f"/rest/api/3/issue/{urllib.parse.quote(issue_key, safe='')}/worklog",
            params={
                "startAt": start_at,
                "maxResults": 100,
                "startedAfter": start_ms,
                "startedBefore": end_ms,
            },
        )
        page = result.get("worklogs", [])
        worklogs.extend(page)
        start_at += len(page)

        if not page or start_at >= result.get("total", 0):
            break

    return worklogs


def comment_to_text(comment):
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


def format_duration(seconds):
    minutes = max(0, int(seconds)) // 60
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def fetch_today_worklogs(account_id):
    timezone = ZoneInfo(os.environ.get("JIRA_TIMEZONE", "Asia/Ho_Chi_Minh"))
    now = datetime.now(timezone)
    day = now.date()
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone)
    end = datetime.combine(day, datetime.max.time(), tzinfo=timezone)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    entries = []
    for issue in find_issues(account_id, day):
        issue_key = issue["key"]
        summary = issue.get("fields", {}).get("summary", "")
        for worklog in get_issue_worklogs(issue_key, start_ms, end_ms):
            author = worklog.get("author", {})
            started_raw = worklog.get("started")
            if author.get("accountId") != account_id or not started_raw:
                continue

            try:
                started = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
                started_local = started.astimezone(timezone)
            except ValueError:
                continue

            if started_local.date() != day:
                continue

            entries.append({
                "key": issue_key,
                "summary": summary,
                "started": started_local,
                "seconds": int(worklog.get("timeSpentSeconds", 0)),
                "comment": comment_to_text(worklog.get("comment")),
            })

    entries.sort(key=lambda item: item["started"])
    return day, entries


# ---------- Slack worklog message / per-ticket button ----------

def build_slack_blocks(day, worklogs):
    jira_base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
    total_seconds = sum(item["seconds"] for item in worklogs)

    blocks = [{
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*Jira Worklogs — {day:%d/%m/%Y}*",
        },
    }]

    if not worklogs:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "No worklogs found for today."},
        })

    for item in worklogs[:25]:
        key = item["key"]
        summary = item["summary"]
        issue_url = (
            f"{jira_base_url}/browse/"
            f"{urllib.parse.quote(key, safe='')}"
        )
        text = (
            f"<{issue_url}|*{slack_escape(key)} "
            f"{slack_escape(summary)}*>\n"
            f"{item['started']:%Y-%m-%d %H:%M} | "
            f" :clock1: {format_duration(item['seconds'])}"
        )
        if item.get("comment"):
            text += f"\n :pencil: {slack_escape(item['comment'][:250])}"

        blocks.extend([
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            },
            {
                "type": "actions",
                "elements": [{
                    "type": "button",
                    "action_id": "open_log_time",
                    "text": {"type": "plain_text", "text": "Log More"},
                    "style": "primary",
                    "value": key,
                }],
            },
        ])

    if len(worklogs) > 25:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"_Showing 25 of {len(worklogs)} worklogs._",
            }],
        })

    blocks.extend([
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"* :fire: Total: {format_duration(total_seconds)}*",
            },
        },
    ])
    return blocks


# ---------- Log Time modal ----------

def build_log_time_modal(user_id, channel_id, default_date, issue_key=""):
    return {
        "type": "modal",
        "callback_id": "log_time_submit",
        "private_metadata": json.dumps({
            "user_id": user_id,
            "channel_id": channel_id,
        }),
        "title": {"type": "plain_text", "text": "Log Time"},
        "submit": {"type": "plain_text", "text": "Log Time"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "issue_block",
                "label": {"type": "plain_text", "text": "Issue"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "issue_key",
                    "initial_value": issue_key,
                    "placeholder": {"type": "plain_text", "text": "PROJ-123"},
                },
            },
            {
                "type": "input",
                "block_id": "date_block",
                "label": {"type": "plain_text", "text": "Date"},
                "element": {
                    "type": "datepicker",
                    "action_id": "work_date",
                    "initial_date": default_date,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Select a date",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "duration_block",
                "label": {"type": "plain_text", "text": "Duration"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "duration",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "1h 30m",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "start_time_block",
                "label": {"type": "plain_text", "text": "Start Time"},
                "element": {
                    "type": "timepicker",
                    "action_id": "start_time",
                    "initial_time": "09:00",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Select start time",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "description_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Description"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "description",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "What did you work on?",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "estimate_block",
                "label": {
                    "type": "plain_text",
                    "text": "Remaining estimate",
                },
                "element": {
                    "type": "static_select",
                    "action_id": "remaining_estimate",
                    "initial_option": {
                        "text": {
                            "type": "plain_text",
                            "text": "Use existing estimate",
                        },
                        "value": "leave",
                    },
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Use existing estimate",
                            },
                            "value": "leave",
                        },
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Adjust automatically",
                            },
                            "value": "auto",
                        },
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "Set a new estimate",
                            },
                            "value": "new",
                        },
                    ],
                },
            },
            {
                "type": "input",
                "block_id": "new_estimate_block",
                "optional": True,
                "label": {
                    "type": "plain_text",
                    "text": "New remaining estimate (if selected above)",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "new_estimate",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g. 2h or 1d",
                    },
                },
            },
        ],
    }


def get_modal_form(view):
    values = view["state"]["values"]

    def value(block_id, action_id):
        return values[block_id][action_id].get("value")

    return {
        "issue_key": (value("issue_block", "issue_key") or "").strip().upper(),
        "date": values["date_block"]["work_date"].get("selected_date"),
        "duration": (value("duration_block", "duration") or "").strip(),
        "start_time": values["start_time_block"]["start_time"].get("selected_time"),
        "description": (value("description_block", "description") or "").strip(),
        "remaining_estimate": values["estimate_block"][
            "remaining_estimate"
        ]["selected_option"]["value"],
        "new_estimate": (value("new_estimate_block", "new_estimate") or "").strip(),
    }


def validate_duration(duration):
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


def create_jira_worklog(form):
    print("==================form logwork=========")
    print(form)
    issue_key = form["issue_key"]
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", issue_key):
        raise ValueError("Issue key is invalid. Example: PROJ-123.")

    duration = validate_duration(form["duration"])
    if form["remaining_estimate"] == "new" and not form["new_estimate"]:
        raise ValueError("Enter a new remaining estimate or choose another option.")

    timezone = ZoneInfo(os.environ.get("JIRA_TIMEZONE", "Asia/Ho_Chi_Minh"))
    started = format_jira_started(form["date"], form['start_time'])
    
    # started_text = started.strftime("%Y-%m-%dT%H:%M:%S.%f%z")
    # # Jira accepts offsets such as +0700. Convert to +07:00 for ISO format.
    # started_text = started_text[:-2] + ":" + started_text[-2:]

    body = {
        "started": started,
        "timeSpent": duration,
    }
    if form["description"]:
        body["comment"] = {
            "type": "doc",
            "version": 1,
            "content": [{
                "type": "paragraph",
                "content": [{
                    "type": "text",
                    "text": form["description"],
                }],
            }],
        }

    estimate = form["remaining_estimate"]
    params = {"adjustEstimate": estimate}
    if estimate == "new":
        params["newEstimate"] = form["new_estimate"]
    
    print("==============params==========")
    print(params)
    print("==============body==========")
    print(body)

    return jira_request(
        f"/rest/api/3/issue/{urllib.parse.quote(issue_key, safe='')}/worklog",
        params=params,
        method="POST",
        data=body,
    )


def handle_block_actions(payload):
    action = next(
        (
            item for item in payload.get("actions", [])
            if item.get("action_id") == "open_log_time"
        ),
        None,
    )
    if not action:
        return json_response(200, "")

    user_id = payload["user"]["id"]
    channel_id = payload.get("channel", {}).get("id", "")
    timezone = ZoneInfo(os.environ.get("JIRA_TIMEZONE", "Asia/Ho_Chi_Minh"))
    today = datetime.now(timezone).date().isoformat()

    slack_api("views.open", {
        "trigger_id": payload["trigger_id"],
        "view": build_log_time_modal(
            user_id=user_id,
            channel_id=channel_id,
            default_date=today,
            issue_key=action.get("value", ""),
        ),
    })
    return json_response(200, "")


def handle_view_submission(payload, context):
    view = payload["view"]
    form = get_modal_form(view)
    metadata = json.loads(view.get("private_metadata") or "{}")

    errors = {}
    if not form["issue_key"]:
        errors["issue_block"] = "Issue is required."
    elif not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", form["issue_key"]):
        errors["issue_block"] = "Enter a valid issue key, e.g. PROJ-123."

    if not form["date"]:
        errors["date_block"] = "Date is required."
    if not form["start_time"]:
        errors["start_time_block"] = "Start Time is required."
    try:
        validate_duration(form["duration"])
    except ValueError as exc:
        errors["duration_block"] = str(exc)

    if form["remaining_estimate"] == "new" and not form["new_estimate"]:
        errors["new_estimate_block"] = "Enter a new remaining estimate."

    if errors:
        return json_response(200, {"response_action": "errors", "errors": errors})

    # Run the Jira write asynchronously so Slack's modal submission stays fast.
    task = {
        "_async_task": "create_jira_worklog",
        "_task_secret": os.environ["ASYNC_TASK_SECRET"],
        "form": form,
        "user_id": metadata["user_id"],
        "channel_id": metadata["channel_id"],
    }
    lambda_client.invoke(
        FunctionName=context.function_name,
        InvocationType="Event",
        Payload=json.dumps(task).encode("utf-8"),
    )
    return json_response(200, {"response_action": "clear"})


def run_create_worklog_task(event):
    user_id = event["user_id"]
    channel_id = event["channel_id"]
    form = event["form"]

    try:
        result = create_jira_worklog(form)
        issue_key = form["issue_key"]
        jira_url = (
            f"{os.environ['JIRA_BASE_URL'].rstrip('/')}/browse/"
            f"{urllib.parse.quote(issue_key, safe='')}"
        )
        message = (
            f"✅ Worklog added to <{jira_url}|*{slack_escape(issue_key)}*> "
            f"for *{slack_escape(form['duration'])}*."
        )
        if result.get("id"):
            message += f"\nWorklog ID: `{slack_escape(result['id'])}`"
    except Exception:
        logger.exception("Failed to create Jira worklog")
        message = (
            "❌ Could not create the Jira worklog. "
            "Please check the issue key, permissions, duration, and Lambda logs."
        )

    slack_api("chat.postEphemeral", {
        "channel": channel_id,
        "user": user_id,
        "text": message,
    })


def run_today_worklogs_task(event):
    response_url = event["response_url"]
    try:
        day, worklogs = fetch_today_worklogs(event["account_id"])
        post_slack_response(response_url, {
            "response_type": "ephemeral",
            "replace_original": True,
            "text": f"Jira Worklogs — {day:%d/%m/%Y}",
            "blocks": build_slack_blocks(day, worklogs),
        })
    except Exception:
        logger.exception("Failed to fetch today's Jira worklogs")
        post_slack_response(response_url, {
            "response_type": "ephemeral",
            "replace_original": True,
            "text": "Could not fetch Jira worklogs. Please check the configuration and Lambda logs.",
        })

# ---------- Get list jira task ------------------
def find_assigned_issues(account_id):
    """Return Jira issues currently assigned to the given accountId."""
    jql = f'assignee = "{account_id}" ORDER BY updated DESC'
    results = []
    next_page_token = None
    for _ in range(10):
        body = {
            "jql": jql,
            "fields": ["summary", "status"],
            "maxResults": 100,
        }
        if next_page_token:
            body["nextPageToken"] = next_page_token
        result = jira_request("/rest/api/3/search/jql", method="POST", data=body)
        results.extend(result.get("issues", []))
        next_page_token = result.get("nextPageToken")
        if not next_page_token:
            break
    return results

def build_assigned_issues_blocks(issues):
    jira_base_url = os.environ["JIRA_BASE_URL"].rstrip("/")
    blocks = [{
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*============== Your assigned Jira tickets ==============*\nShowing up to 20 tickets, ordered by recently updated.",
        },
    }]
    if not issues:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "No Jira tickets are currently assigned to you."},
        })
        return blocks
    for issue in issues[:20]:
        key = issue["key"]
        fields = issue.get("fields", {})
        summary = fields.get("summary", "(No summary)")
        status = (fields.get("status") or {}).get("name", "Unknown status")
        total_seconds = sum(
            int(worklog.get("timeSpentSeconds", 0))
            for worklog in get_all_issue_worklogs(key)
        )
        issue_url = f"{jira_base_url}/browse/{urllib.parse.quote(key, safe='')}"
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"<{issue_url}|*{slack_escape(key)} — {slack_escape(summary)}*>\n"
                    f"Status: {slack_escape(status)} | "
                    f"Total logged: *{format_duration(total_seconds)}*"
                ),
            },
            "accessory": {
                "type": "button",
                "action_id": "open_log_time",
                "text": {"type": "plain_text", "text": "Log Time"},
                "style": "primary",
                "value": key,
            },
        })
    if len(issues) > 20:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"_Showing 20 of {len(issues)} assigned tickets._",
            }],
        })
    return blocks


def run_list_assigned_task(event):
    response_url = event["response_url"]
    try:
        issues = find_assigned_issues(event["account_id"])
        # The renderer fetches per-issue worklogs to calculate the full total.
        blocks = build_assigned_issues_blocks(issues)
        post_slack_response(response_url, {
            "response_type": "ephemeral",
            "replace_original": True,
            "text": "Your assigned Jira tickets",
            "blocks": blocks,
        })
    except Exception:
        logger.exception("Failed to list assigned Jira issues")
        post_slack_response(response_url, {
            "response_type": "ephemeral",
            "replace_original": True,
            "text": "Could not list assigned Jira tickets. Please check the configuration and Lambda logs.",
        })



def get_all_issue_worklogs(issue_key):
    """Fetch worklogs for an issue and return them for total-time calculation."""
    worklogs = []
    start_at = 0
    for _ in range(100):
        result = jira_request(
            f"/rest/api/3/issue/{urllib.parse.quote(issue_key, safe='')}/worklog",
            params={"startAt": start_at, "maxResults": 100},
        )
        page = result.get("worklogs", [])
        worklogs.extend(page)
        start_at += len(page)
        if not page or start_at >= result.get("total", 0):
            break
    return worklogs

# ---------- Lambda entry point ----------

def lambda_handler(event, context):
    # Internal async tasks are invoked by this Lambda itself.
    if event.get("_async_task"):
        if event.get("_task_secret") != os.environ.get("ASYNC_TASK_SECRET"):
            return json_response(403, {"error": "forbidden"})

        if event["_async_task"] == "jira_worklog":
            run_today_worklogs_task(event)
            return json_response(200, {"ok": True})

        if event["_async_task"] == "create_jira_worklog":
            run_create_worklog_task(event)
            return json_response(200, {"ok": True})
        
        if event["_async_task"] == "list_assigned_issues":
            run_list_assigned_task(event)
            return json_response(200, {"ok": True})

        return json_response(400, {"error": "unknown task"})

    raw_body = get_raw_body(event)
    if not verify_slack_signature(event, raw_body):
        return json_response(401, {"error": "invalid Slack signature"})

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    content_type = headers.get("content-type", "")

    # Slack interactive components send form-urlencoded data with JSON in "payload".
    if "application/x-www-form-urlencoded" in content_type:
        form_data = parse_qs(raw_body)
        if "payload" in form_data:
            payload = json.loads(form_data["payload"][0])
            if payload.get("type") == "block_actions":
                return handle_block_actions(payload)
            if payload.get("type") == "view_submission":
                return handle_view_submission(payload, context)
            return json_response(200, "")

    # Slash command request.
    command_data = parse_qs(raw_body)
    print("===============command_data=========")
    print(command_data)
    command = command_data.get("command", [""])[0]
    text_value = " ".join(command_data.get("text", [""])[0].split()).lower()
    slack_user_id = command_data.get("user_id", [""])[0]
    response_url = command_data.get("response_url", [""])[0]

    if command != "/read-jira" or text_value not in {"logwork today", "list"}:
        return json_response(200, {
            "response_type": "ephemeral",
            "text": "Usage: `/read-jira logwork today` or `/read-jira list`",
        })

    parsed = urllib.parse.urlparse(response_url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "hooks.slack.com",
        "hooks.slack-gov.com",
    }:
        return json_response(400, {"error": "invalid response_url"})

    try:
        account_id = get_jira_account_id_for_slack_user(slack_user_id)
    except ValueError:
        return json_response(200, {
            "response_type": "ephemeral",
            "text": "Your Slack user is not mapped to a Jira account. Please contact the administrator.",
        })
    	
    is_list_command = text_value == "list"
    lambda_client.invoke(
        FunctionName=context.function_name,
        InvocationType="Event",
        Payload=json.dumps({
            "_async_task": "list_assigned_issues" if is_list_command else "jira_worklog",
            "_task_secret": os.environ["ASYNC_TASK_SECRET"],
            "account_id": account_id,
            "response_url": response_url,
        }).encode("utf-8"),
    )

    return json_response(200, {
        "response_type": "ephemeral",
        "text": "Fetching your assigned Jira tickets..." if is_list_command else "Fetching your Jira worklogs for today...",
    })
