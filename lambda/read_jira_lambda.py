

from jira.run_create_worklog_task import RunCreateWorklogsTask
from jira.run_list_assigned_task import RunListAssignedTask
from jira.run_today_worklogs_task import RunTodayWorklogsTask
from util.jira_helper import JiraHelper
from util.common import json_response
import json
import os
import urllib.parse

import boto3
from urllib.parse import parse_qs


lambda_client = boto3.client("lambda")

def lambda_handler(event, context): 
    '''
    Main entry point for the Lambda function.
    Handles incoming requests from Slack and routes them to the appropriate task handler.
    '''
    jira_helper = JiraHelper()  
    # Internal async tasks are invoked by this Lambda itself.
    if event.get("_async_task"):
        if event.get("_task_secret") != os.environ.get("ASYNC_TASK_SECRET"):
            return json_response(403, {"error": "forbidden"})

        if event["_async_task"] == "jira_worklog":
            today_worklogs = RunTodayWorklogsTask(event)
            today_worklogs.run_today_worklogs_task(event)
            return json_response(200, {"ok": True})

        if event["_async_task"] == "create_jira_worklog":
            create_worklog = RunCreateWorklogsTask(event)
            create_worklog.run_create_worklog_task(event)
            return json_response(200, {"ok": True})
        
        if event["_async_task"] == "list_assigned_issues":
            list_assigned = RunListAssignedTask(event)
            list_assigned.run_list_assigned_task(event)
            return json_response(200, {"ok": True})

        return json_response(400, {"error": "unknown task"})

    raw_body = jira_helper.get_raw_body(event)
    if not jira_helper.verify_slack_signature(event, raw_body):
        return json_response(401, {"error": "invalid Slack signature"})

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    content_type = headers.get("content-type", "")

    # Slack interactive components send form-urlencoded data with JSON in "payload".
    if "application/x-www-form-urlencoded" in content_type:
        form_data = parse_qs(raw_body)
        if "payload" in form_data:
            payload = json.loads(form_data["payload"][0])
            if payload.get("type") == "block_actions":
                return jira_helper.handle_block_actions(payload)
            if payload.get("type") == "view_submission":
                return jira_helper.handle_view_submission(payload, context, lambda_client)
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
        account_id = jira_helper.get_jira_account_id_for_slack_user(slack_user_id)
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




