# ticket-from-slack

AWS Lambda integration that lets Slack users look up Jira work and create Jira
worklogs without leaving Slack.

## Features

- `/read-jira logwork today` lists the current user's Jira worklogs for today,
  grouped by issue, with a daily total.
- `/read-jira list` lists the current user's assigned Jira issues, status, and
  total time logged.
- **Log More** / **Log Time** opens a Slack modal to add a worklog to a Jira
  issue. The form supports the work date, start time, duration, description,
  and remaining-estimate behavior.
- Jira reads and worklog creation run as asynchronous Lambda invocations so
  Slack receives a prompt acknowledgement.

## Project structure

```text
ticket-from-slack/
├── lambda/
│   └── read_jira_lambda.py          # Lambda entry point; verifies and routes Slack requests
├── jira/
│   ├── run_today_worklogs_task.py   # Fetches and formats today's worklogs
│   ├── run_list_assigned_task.py    # Fetches and formats assigned Jira issues
│   └── run_create_worklog_task.py   # Validates and creates Jira worklogs
├── util/
│   ├── jira_helper.py               # Jira/Slack API calls, signatures, modal and user mapping
│   └── common.py                    # Shared formatting, validation and response helpers
└── README.md
```

## Request flow

1. Slack sends a slash-command or interactive-component request to the Lambda
   endpoint.
2. The handler verifies Slack's request signature and resolves the Slack user
   to a Jira account ID.
3. For slash commands, the handler acknowledges Slack and invokes the matching
   Jira task asynchronously. Interactive actions open or submit the worklog
   modal.
4. The task calls the Jira REST API and returns the result to Slack.

## Configuration

Set these environment variables on the Lambda function. Keep credentials in
Lambda environment configuration or a secrets manager; do not commit them.

| Variable | Required | Description |
| --- | --- | --- |
| `JIRA_BASE_URL` | Yes | Jira site URL, for example `https://example.atlassian.net`. |
| `JIRA_EMAIL` | Yes | Email address for the Jira API account. |
| `JIRA_API_TOKEN` | Yes | Jira API token for that account. |
| `SLACK_SIGNING_SECRET` | Yes | Signing secret for validating Slack requests. |
| `SLACK_BOT_TOKEN` | Yes | Slack bot token used for modal and ephemeral-message API calls. |
| `SLACK_JIRA_USER_MAP` | Yes | JSON object mapping Slack user IDs to Jira account IDs. |
| `ASYNC_TASK_SECRET` | Yes | Shared secret used to authorize internal asynchronous task invocations. |
| `JIRA_TIMEZONE` | No | Time zone for “today” and worklog timestamps; defaults to `Asia/Ho_Chi_Minh`. |

Example user mapping (replace both example IDs with real IDs):

```json
{
  "U0123456789": "5b10a2844c20165700ede21g"
}
```

## Slack and AWS setup

1. Deploy the project as an AWS Lambda function and expose it through an HTTPS
   endpoint that forwards the original request body and headers.
2. Configure the Slack slash command `/read-jira` to use that endpoint.
3. Configure Slack Interactivity to use the same endpoint, so buttons and modal
   submissions reach the handler.
4. Set the endpoint URL as the command and Interactivity request URL in Slack,
   and set all required Lambda environment variables above.
5. Ensure the Lambda execution role can invoke the same function
   asynchronously. Package the project modules with the deployment so the
   imports under `jira` and `util` are available.

The slash command accepts:

```text
/read-jira logwork today
/read-jira list
```

## Notes

- Jira worklog durations accept Jira-style values such as `30m`, `1h`,
  `1h 30m`, or `1d`.
- Worklog and “today” date handling use `JIRA_TIMEZONE`.
- Slack user IDs must be mapped to Jira account IDs in `SLACK_JIRA_USER_MAP`;
  users without a mapping cannot run the Jira commands.


## Slack configure scope and slash command
![slack scope](/img_guideline/slack_scope.png)
![slash command](/img_guideline/slack_slash.png)