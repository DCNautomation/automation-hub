# Sending events to the hub from n8n

`send-to-hub.json` is a four-node n8n workflow that signs an event the way the hub expects and posts it.

1. In n8n, choose Import from file and pick `send-to-hub.json`.
2. In **Sign it**, replace `REPLACE_WITH_THE_SOURCE_SIGNING_SECRET` with the signing secret of your hub source.
3. In **Build the request**, set `SOURCE` to the source name and build `EVENT` from the data your flow has. Keep `KEY` stable for a given event (an order id works well), so a retry is recognised instead of running twice.
4. In **Send to the hub**, change the URL if the hub isn't at `http://127.0.0.1:8000`. If n8n runs in Docker, use `http://host.docker.internal:8000`.
5. Run it. A 202 means the hub accepted it. A 200 means the hub already had that key. Open the dashboard to watch the run.

## What the hub checks

| Header | Value |
|---|---|
| `X-Idempotency-Key` | the stable id for this event |
| `X-Hub-Timestamp` | unix seconds when you send it (rejected beyond 5 minutes either way) |
| `X-Hub-Signature-256` | `sha256=` plus the hex HMAC-SHA256, with the source's secret, of `timestamp`, newline, `key`, newline, then the raw body |

The raw body bytes that are signed must be the bytes that are sent. That is why the body is built as one string in the Code node, signed from that same string, and sent as a raw body instead of letting the HTTP node re-serialise JSON.

## What was and wasn't tested

- Tested: the Code node's JavaScript, run outside n8n, produced a request the live hub accepted (202), recognised as a duplicate on resend (200), and refused with a wrong secret (401). The Crypto node's HMAC is what Node's `crypto.createHmac('sha256', secret)` computes.
- Not tested: importing the file into a running n8n. The node settings (Crypto node and HTTP Request node parameters) follow n8n's published node formats but have not been opened in an n8n editor. If a field looks off after import, the header table above is the source of truth.

## Random error demo (`random-error-demo.json`)

A webhook-triggered flow for showing the hub off. Each call to its webhook picks a random failure (Shopify 429, Google Sheets 403, ClickUp 404, and so on), and reports it to the hub with the app, the error code and what to do about it. The hub's `demo` handler fails on it, so the run retries and ends as "Gave up" on the dashboard.

1. Create a hub source with the `demo` handler: `python -m app.sources create n8n-demo --handler demo --max-attempts 3`.
2. Import the file into n8n. In **Make a random error**, set `HUB_URL` to an address n8n can reach. In **Sign it**, paste the source's signing secret.
3. Activate the workflow and send any POST to its production webhook URL (path `hub-random-error`).

Tested: imported into a local n8n 2.x in Docker, activated, called once. The hub accepted the signed event and the run started retrying with the full error text in its payload. Not tested: n8n Cloud, and a hub reachable from the internet.
