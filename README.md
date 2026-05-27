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
  create_persona.py     # POST personas/<name>.json to Tavus /v2/personas
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

## Related repos

- **realtime-replica** (`../realtime-replica`) — the CVI conversation backend
  that consumes this persona at call time. The `InputSchema` in
  `src/utils/input_schema.py` is the dict the predictor receives; the public
  persona API fields here map onto it via `request-handler`.
- **request-handler** (`../request-handler`) — owns `POST /v2/personas` and
  the persona validation schema (`src/v2/persona_validation_schema.py`).
