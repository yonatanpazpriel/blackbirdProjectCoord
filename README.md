# blackbirdProjectCoord

Charlie — a Tavus CVI persona that acts as a project coordinator. Files Linear
tickets, schedules check-in calls, and confirms details before acting.

Built against the Tavus persona API (`POST /v2/personas`) used by
`realtime-replica` as the conversation backend.

## Layout

```
personas/
  charlie.json          # persona payload (system_prompt, greeting, layers, tools)
scripts/
  create_persona.py              # POST personas/<name>.json to Tavus /v2/personas
  google_oauth_refresh_token.py  # one-time helper to mint a Calendar refresh token
server/
  app.py                # Flask: POST /tavus/webhook (tool_call receiver)
  tools.py              # tool-name dispatcher
  linear_client.py      # Linear GraphQL client
  google_calendar.py    # Google Calendar v3 + Meet client (refresh-token auth)
  slack_client.py       # Slack Web API client
  priority.py           # persona priority -> Linear int mapping
```

## Create the persona

1. Set your API key:

   ```bash
   cp .env.example .env
   # edit .env, paste TAVUS_API_KEY=...
   ```

2. Inspect the payload (optional):

   ```bash
   python scripts/create_persona.py personas/charlie.json --dry-run
   ```

3. Create it:

   ```bash
   pip install -r requirements.txt
   python scripts/create_persona.py personas/charlie.json            # prod
   python scripts/create_persona.py personas/charlie.json --env stg  # staging
   ```

   The response includes `persona_id`. Save it — you'll pass it as `persona_id`
   when starting a conversation (`POST /v2/conversations`).

## TODOs before going live

The persona JSON has two unwired fields that need to be filled in once the
backing services exist:

- **`default_replica_id`** — set to a Tavus replica UUID (the face/voice that
  appears on video). Currently `""`, which makes the persona text/audio-only
  until set. Patch it with:

  ```bash
  curl -X PATCH https://tavusapi.com/v2/personas/<persona_id> \
    -H "x-api-key: $TAVUS_API_KEY" \
    -H "Content-Type: application/json" \
    -d '[{"op": "replace", "path": "/default_replica_id", "value": "r1234..."}]'
  ```

- **`objectives_id`** — `null` in the payload. If you want the "Project
  Coordinator" objective from the spec to drive conversation flow, create it
  separately (`POST /v2/objectives`) and PATCH the returned ID onto the persona.

- **Tool webhook delivery.** Tavus delivers `tool_call` events for
  `create_linear_ticket` and `schedule_calendar_call` to the **conversation's**
  `callback_url`, not the persona's. Set it per-conversation:

  ```json
  POST /v2/conversations
  {
    "persona_id": "<persona_id>",
    "callback_url": "https://your-service.example.com/tavus/webhook",
    ...
  }
  ```

  Your webhook handler is responsible for actually creating the Linear ticket
  and the calendar invite — Tavus only forwards the model's tool call.

## Running the webhook server

The persona tools are delivered to whatever URL is set as the conversation's
`callback_url`. This repo ships a minimal Flask receiver that turns those tool
calls into real Linear tickets and Charlie Meet follow-up Slack DMs.

1. Add the new env vars to `.env`:

   ```
   LINEAR_API_KEY=lin_api_...                  # Linear Personal API Key
   LINEAR_DEFAULT_TEAM_KEY=ENT                 # Linear team to file tickets under
   SLACK_BOT_TOKEN=xoxb-...                    # Slack bot token with users:read.email, im:write, chat:write
   GOOGLE_OAUTH_CLIENT_ID=...                  # GCP OAuth client id (see Google Calendar setup below)
   GOOGLE_OAUTH_CLIENT_SECRET=...              # GCP OAuth client secret
   GOOGLE_OAUTH_REFRESH_TOKEN=...              # produced by scripts/google_oauth_refresh_token.py
   GOOGLE_CALENDAR_ID=primary                  # calendar to write events to (default: primary)
   CHARLIE_MEET_EMAIL=charlie-meet@example.com # second invitee on every check-in (Tavus auto-joins this address)
   TAVUS_WEBHOOK_SECRET=<a long random string> # shared with the conversation
   ```

   Generate a Linear Personal API Key at
   `https://linear.app/<workspace>/settings/account/security`.

2. Install deps and run the server:

   ```bash
   pip install -r requirements.txt
   flask --app server.app run --port 8080
   ```

3. Expose it publicly for Tavus to reach (during dev):

   ```bash
   ngrok http 8080
   ```

4. Point the conversation at it. The `callback_url` lives on the
   conversation, not the persona:

   ```bash
   curl -X POST https://tavusapi.com/v2/conversations \
     -H "x-api-key: $TAVUS_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{
       "persona_id": "<persona_id>",
       "callback_url": "https://<your-ngrok-subdomain>.ngrok.app/tavus/webhook"
     }'
   ```

   Tavus will POST each `tool_call` to that URL. Configure the same
   `TAVUS_WEBHOOK_SECRET` value as the `X-Webhook-Secret` header on the
   Tavus side (or use a proxy that injects it) — the server rejects
   requests without a matching secret with 401.

5. Smoke test without Tavus:

   ```bash
   curl -X POST http://localhost:8080/tavus/webhook \
     -H "X-Webhook-Secret: $TAVUS_WEBHOOK_SECRET" \
     -H "Content-Type: application/json" \
     -d '{
       "tool_call": {
         "name": "create_linear_ticket",
         "arguments": {
           "assignee": "yonatan@tavus.io",
           "title": "Test ticket from Charlie",
           "description": "Filed via the webhook smoke test.",
           "priority": "medium"
         }
       }
     }'
   ```

   On success the response is
   `{"result": {"issue_id": "...", "identifier": "ENT-123", "url": "https://linear.app/..."}}`
   and a ticket appears in the target Linear team.

6. Smoke test Charlie Meet's ticket context lookup:

   ```bash
   curl -X POST http://localhost:8080/tavus/webhook \
     -H "X-Webhook-Secret: $TAVUS_WEBHOOK_SECRET" \
     -H "Content-Type: application/json" \
     -d '{
       "tool_call": {
         "name": "get_linear_ticket_context",
         "arguments": {
           "ticket_id_or_url": "ENT-123"
         }
       }
     }'
   ```

7. Smoke test Charlie Meet's creator summary Slack DM:

   ```bash
   curl -X POST http://localhost:8080/tavus/webhook \
     -H "X-Webhook-Secret: $TAVUS_WEBHOOK_SECRET" \
     -H "Content-Type: application/json" \
     -d '{
       "tool_call": {
         "name": "send_linear_creator_summary",
         "arguments": {
           "ticket_id_or_url": "ENT-123",
           "creator": "requester@company.com",
           "attendee": "assignee@company.com",
           "summary": "The assignee understands the request and confirmed the expected outcome.",
           "blockers": [],
           "questions": ["Should this include the older import path?"],
           "next_steps": ["Creator to answer the import-path question.", "Assignee to begin after confirmation."]
         }
       }
     }'
   ```

   The server fetches the Linear issue, prefers the issue creator's email from
   Linear, opens a Slack DM with that person, and posts the meeting summary.

8. Smoke test `schedule_calendar_call`:

   ```bash
   curl -X POST http://localhost:8080/tavus/webhook \
     -H "X-Webhook-Secret: $TAVUS_WEBHOOK_SECRET" \
     -H "Content-Type: application/json" \
     -d '{
       "tool_call": {
         "name": "schedule_calendar_call",
         "arguments": {
           "attendee_name": "yonatan@tavus.io",
           "start_time": "2026-06-01T15:00:00-07:00",
           "duration": 30,
           "topic": "Check-in on ENT-123 progress and blockers.",
           "ticket_id": "ENT-123"
         }
       }
     }'
   ```

   On success the response is
   `{"result": {"event_id": "...", "html_link": "https://www.google.com/calendar/event?eid=...", "meet_link": "https://meet.google.com/...", "attendees": [...], "ticket": {"identifier": "ENT-123", ...}}}`
   and Google emails the invite to both the attendee and `CHARLIE_MEET_EMAIL`.

## Google Calendar setup

`schedule_calendar_call` writes events to a Google Calendar via the v3 REST
API and lets Google auto-mint a Meet link for each event. We authenticate
with the OAuth **refresh-token** grant — no service account, no per-request
human OAuth dance.

1. **Enable the API.** In [Google Cloud Console](https://console.cloud.google.com/),
   create or pick a project, then **APIs & Services → Library → Google
   Calendar API → Enable**.

2. **Configure the consent screen.** **APIs & Services → OAuth consent
   screen.** Pick **External** (or **Internal** if the project is in a
   Workspace), fill the required fields, add scope
   `https://www.googleapis.com/auth/calendar.events`, and while the app is in
   "Testing" mode add yourself as a **Test user** — otherwise Google blocks
   the consent screen.

3. **Create OAuth credentials.** **APIs & Services → Credentials → Create
   credentials → OAuth client ID → Application type: Desktop app.** Copy the
   `client_id` and `client_secret` into `.env` as `GOOGLE_OAUTH_CLIENT_ID` and
   `GOOGLE_OAUTH_CLIENT_SECRET`.

4. **Mint the refresh token.** Run:

   ```bash
   python scripts/google_oauth_refresh_token.py
   ```

   The script opens your browser, you consent as the calendar's owner, Google
   redirects to a one-shot local server, and the script prints the refresh
   token. Paste it into `.env` as `GOOGLE_OAUTH_REFRESH_TOKEN`.

   The OAuth user whose token you mint is who appears as the **event
   organizer** on every invite Charlie schedules. Pick whatever account owns
   the calendar that should hold these check-ins (often a shared service
   mailbox).

5. **Set the remaining env vars.** `GOOGLE_CALENDAR_ID` defaults to `primary`
   (the OAuth user's primary calendar) but accepts any calendar id the OAuth
   user has write access to. `CHARLIE_MEET_EMAIL` is the email address Tavus
   is configured to auto-join meets for — every check-in invites this address
   as a second attendee so charlie-meet seats itself in the Meet at call
   time.

## Related repos

- **realtime-replica** (`../realtime-replica`) — the CVI conversation backend
  that consumes this persona at call time. The `InputSchema` in
  `src/utils/input_schema.py` is the dict the predictor receives; the public
  persona API fields here map onto it via `request-handler`.
- **request-handler** (`../request-handler`) — owns `POST /v2/personas` and
  the persona validation schema (`src/v2/persona_validation_schema.py`).
