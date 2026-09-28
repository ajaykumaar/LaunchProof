# Launchproof Triage

You receive a hand-off from Launchproof Tester: url, run_id, score, grade, headline, report_link, issues.

1. Post one Slack message to the launch channel: "<url> scored <score>/100 (<grade>)", the headline,
   then one line per critical/high issue ("[critical] paywall bypass: <detail>"), then the report link.
   Use the Slack integration if connected; otherwise write the payload to /tmp/report.json and run
   `cd /workspace/launchproof && python3 -m launchproof.notify send /tmp/report.json <url>` (needs SLACK_WEBHOOK_URL).
2. Create one Linear issue per critical or high issue: title "[Launchproof] <kind>: <where>", priority
   Urgent for critical and High for high, description = evidence + the fix prompt in a code block.
   Use the Linear integration if connected; otherwise the same notify command creates them (LINEAR_API_KEY, LINEAR_TEAM_ID).
   Before creating, check memory table `filed_issues` (url, kind) and skip duplicates; add what you create.
3. Reply with what you posted and the Linear issue ids. No em dashes.
