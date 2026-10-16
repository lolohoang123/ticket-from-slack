from util.common import format_duration, slack_escape
import logging
import os
import urllib.parse

from util.jira_helper import JiraHelper

logger = logging.getLogger()
logger.setLevel(logging.INFO)

class RunListAssignedTask:
    def __init__(self, event):
        self.event = event
        self.jira_helper = JiraHelper()

    def run_list_assigned_task(self,event):
        """List Jira issues currently assigned to the user who invoked the command."""

        response_url = event["response_url"]
        try:
            issues = self.find_assigned_issues(event["account_id"])
            # The renderer fetches per-issue worklogs to calculate the full total.
            blocks = self.build_assigned_issues_blocks(issues)
            self.jira_helper.post_slack_response(response_url, {
                "response_type": "ephemeral",
                "replace_original": True,
                "text": "Your assigned Jira tickets",
                "blocks": blocks,
            })
        except Exception:
            logger.exception("Failed to list assigned Jira issues")
            self.jira_helper.post_slack_response(response_url, {
                "response_type": "ephemeral",
                "replace_original": True,
                "text": "Could not list assigned Jira tickets. Please check the configuration and Lambda logs.",
            })

    def find_assigned_issues(self, account_id):
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
            result = self.jira_helper.jira_request("/rest/api/3/search/jql", method="POST", data=body)
            results.extend(result.get("issues", []))
            next_page_token = result.get("nextPageToken")
            if not next_page_token:
                break
        return results

    def build_assigned_issues_blocks(self, issues):
        """Build Slack message blocks to display the list of assigned Jira issues."""

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
                for worklog in self.get_all_issue_worklogs(key)
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

    def get_all_issue_worklogs(self, issue_key):
        """Fetch worklogs for an issue and return them for total-time calculation."""
        worklogs = []
        start_at = 0
        for _ in range(100):
            result = self.jira_helper.jira_request(
                f"/rest/api/3/issue/{urllib.parse.quote(issue_key, safe='')}/worklog",
                params={"startAt": start_at, "maxResults": 100},
            )
            page = result.get("worklogs", [])
            worklogs.extend(page)
            start_at += len(page)
            if not page or start_at >= result.get("total", 0):
                break
        return worklogs

    