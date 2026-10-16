from util.common import json_response, validate_duration
import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
import time
from zoneinfo import ZoneInfo
from datetime import datetime
import base64
import hashlib
import hmac
import re

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class JiraHelper:
    """
    Helper class for interacting with Jira and handling Slack requests."""
    def __init__(self):
        pass

    def jira_request(self, path, params=None, method="GET", data=None):
        """Make a request to the Jira API with the given path, parameters, method, and data."""

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

    def get_modal_form(self, view):
        '''
        Extract form values from a Slack modal view.
        '''
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

    def build_log_time_modal(self, user_id, channel_id, default_date, issue_key=""):
        """
        Build a Slack modal view for logging time to Jira."""
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

    def get_raw_body(self,event):
        """Get the raw request body from the event, decoding from base64 if necessary."""
        
        body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        return body


    def get_jira_account_id_for_slack_user(self,slack_user_id):
        """Get the Jira accountId for a given Slack user ID from the environment variable mapping."""
        
        mapping = json.loads(os.environ.get("SLACK_JIRA_USER_MAP", "{}"))
        account_id = mapping.get(slack_user_id)
        if not account_id:
            raise ValueError(
                f"No Jira accountId configured for Slack user {slack_user_id}"
            )
        return account_id

    def verify_slack_signature(self,event, raw_body):
        """Verify the Slack request signature using the signing secret."""

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

    def handle_block_actions(self,payload):
        """Handle Slack block_actions payloads, specifically for opening the log time modal."""
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

        self.slack_api("views.open", {
            "trigger_id": payload["trigger_id"],
            "view": self.build_log_time_modal(
                user_id=user_id,
                channel_id=channel_id,
                default_date=today,
                issue_key=action.get("value", ""),
            ),
        })
        return json_response(200, "")

    def slack_api(self,method, payload):
        """Call the Slack API with the given method and payload."""

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

    
    def handle_view_submission(self, payload, context, lambda_client):
        """Handle Slack view_submission payloads, validating the form and invoking the async Jira worklog task."""
        
        view = payload["view"]
        form = self.get_modal_form(view)
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

    def post_slack_response(self,response_url, message):
        """Post a message to Slack using the provided response_url."""
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