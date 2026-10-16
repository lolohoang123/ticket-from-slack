import logging
import os
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo
from util.common import format_duration, comment_to_text, slack_escape
from util.jira_helper import JiraHelper
logger = logging.getLogger()
logger.setLevel(logging.INFO)


class RunTodayWorklogsTask:
    def __init__(self, event):
        self.event = event
        self.jira_helper = JiraHelper()

    def run_today_worklogs_task(self):
        """Run the task to fetch and display today's Jira worklogs."""

        event = self.event
        response_url = event["response_url"]
        try:
            day, worklogs = self.fetch_today_worklogs(event["account_id"])
            self.jira_helper.post_slack_response(response_url, {
                "response_type": "ephemeral",
                "replace_original": True,
                "text": f"Jira Worklogs — {day:%d/%m/%Y}",
                "blocks": self.build_slack_blocks(day, worklogs),
            })
        except Exception:
            logger.exception("Failed to fetch today's Jira worklogs")
            self.jira_helper.post_slack_response(response_url, {
                "response_type": "ephemeral",
                "replace_original": True,
                "text": "Could not fetch Jira worklogs. Please check the configuration and Lambda logs.",
            })
    
    def fetch_today_worklogs(self,account_id):
        """Fetch Jira worklogs for the given accountId for today."""

        timezone = ZoneInfo(os.environ.get("JIRA_TIMEZONE", "Asia/Ho_Chi_Minh"))
        now = datetime.now(timezone)
        day = now.date()
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone)
        end = datetime.combine(day, datetime.max.time(), tzinfo=timezone)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        entries = []
        for issue in self.find_issues(account_id, day):
            issue_key = issue["key"]
            summary = issue.get("fields", {}).get("summary", "")
            for worklog in self.get_issue_worklogs(issue_key, start_ms, end_ms):
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

    def build_slack_blocks(self, day, worklogs):
        """Build Slack message blocks to display the list of worklogs for today."""

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

    def find_issues(self, account_id, day):
        """Find Jira issues that have worklogs for the given accountId on the specified day."""

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

            result = self.jira_helper.jira_request("/rest/api/3/search/jql", method="POST", data=body)
            results.extend(result.get("issues", []))
            next_page_token = result.get("nextPageToken")
            if not next_page_token:
                break

        return results

    def get_issue_worklogs(self, issue_key, start_ms, end_ms):
        """Get worklogs for a specific Jira issue within the specified time range."""

        worklogs = []
        start_at = 0

        for _ in range(20):
            result = self.jira_helper.jira_request(
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

    