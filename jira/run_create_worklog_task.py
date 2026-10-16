from util.common import format_jira_started, json_response, slack_escape, validate_duration
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from util.jira_helper import JiraHelper

logger = logging.getLogger()
logger.setLevel(logging.INFO)

class RunCreateWorklogsTask:
    def __init__(self, event):
        self.event = event
        self.jira_helper = JiraHelper()

    def run_create_worklog_task(self, event):
        """Run the task to create a Jira worklog."""
        user_id = event["user_id"]
        channel_id = event["channel_id"]
        form = event["form"]
    
        try:
            result = self.create_jira_worklog(form)
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
    
        self.jira_helper.slack_api("chat.postEphemeral", {
            "channel": channel_id,
            "user": user_id,
            "text": message,
        })


    def create_jira_worklog(self,form):
        """Create a Jira worklog based on the provided form data."""

        print("==================form logwork=========")
        print(form)
        issue_key = form["issue_key"]
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", issue_key):
            raise ValueError("Issue key is invalid. Example: PROJ-123.")

        duration = validate_duration(form["duration"])
        if form["remaining_estimate"] == "new" and not form["new_estimate"]:
            raise ValueError("Enter a new remaining estimate or choose another option.")
        
        started = format_jira_started(form["date"], form['start_time'])

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

        return self.jira_helper.jira_request(
            f"/rest/api/3/issue/{urllib.parse.quote(issue_key, safe='')}/worklog",
            params=params,
            method="POST",
            data=body,
        )