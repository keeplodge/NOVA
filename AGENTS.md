# NOVA Trading Agent Hierarchy

Every webhook now flows through a commander that delegates to specialist sub-agents. No dropped signals — if TradersPost fails 3 retries, the signal is queued and a Discord DM with a one-tap fire URL is sent to Sir's phone.

## Architecture

```
TradingCommander (app.py → _get_commander())
├── SignalIntelligence       — fingerprints + enriches raw payload
├── RiskGuardian             — enforces gates (wraps evaluate_gates)
├── ExecutionDispatcher      — tries venues in order until one accepts
│     ├── RetryVenue(TradersPostVenue, max_attempts=3, backoff=1.5s×2ⁿ)
│     └── ManualEscalationVenue — queues trade, blasts Discord with /fire URL
└── Observability            — mirrors to Discord + in-memory ledger
```

## Railway env vars

| Variable | Required | What it does |
|----------|----------|--------------|
| `TRADERSPOST_WEBHOOK_URL` | yes | Primary execution venue (unchanged) |
| `NOVA_WEBHOOK_SECRET` | **strongly recommended** | Shared secret. Clients must send `X-Nova-Secret: <value>` header or `"secret": "<value>"` in body. Unset = webhook is public |
| `NOVA_DISCORD_WEBHOOK_URL` | recommended | Discord channel webhook for phone mirror. Unset = no notifications |
| `NOVA_PUBLIC_BASE` | recommended | Public base URL (e.g. `https://nova-production-72f5.up.railway.app`). Used to build `/fire` URLs in Discord alerts. Defaults to Railway URL |
| `NOVA_FIRE_SECRET` | recommended | HMAC salt for one-tap fire token signing. Any random string |

## New endpoints

### `POST /webhook`
Existing endpoint — now runs the full agent chain and returns the dispatch trail:
```json
{
  "status": "ok" | "escalated" | "rejected" | "error",
  "signal_id": "d3082e0e65",
  "message": "...",
  "dispatch": {
    "chosen": "Retry:TradersPost" | "ManualEscalation" | "none",
    "attempts": [
      {"venue": "TradersPost",      "success": false, "message": "timed out"},
      {"venue": "TradersPost",      "success": false, "message": "timed out"},
      {"venue": "TradersPost",      "success": true,  "message": "accepted (200)"}
    ]
  }
}
```

### `GET|POST /fire?token=<t>&sig=<s>`
One-tap manual fire. Sir receives this URL in a Discord DM when TradersPost primary fails. Tapping it pops the queued trade and fires TP once. Single-use. Token expires in 30 minutes. Tamper-resistant (HMAC-signed).

### `GET /agents/ledger?limit=50`
Rolling ledger of every commander decision. Useful for debugging the chain:
```json
{"entries": [
  {"ts": "...", "event": "signal_received",   "signal_id": "...", "ticker": "BTCUSDT"},
  {"ts": "...", "event": "signal_escalated",  "signal_id": "...", "fire_url": "https://.../fire?..."},
  {"ts": "...", "event": "signal_manual_fired","signal_id": "...", "success": true}
]}
```

## Signal flow (happy path)

```
TradingView → POST /webhook (with X-Nova-Secret)
    → SignalIntelligence.enrich()
    → RiskGuardian.approve()         (session/weekend/loss/grade gates)
    → ExecutionDispatcher.dispatch()
        → RetryVenue → TradersPostVenue  → 200 OK
    → Observability.signal_executed()    → Discord ✅ embed
```

## Signal flow (TP down — escalation)

```
TradingView → POST /webhook
    → enrich → gates pass
    → RetryVenue tries TP  (fail 1)  backoff 1.5s
                   tries TP  (fail 2)  backoff 3.0s
                   tries TP  (fail 3)
    → ManualEscalationVenue
        → queue_pending(token, sig) — 30-min TTL
        → Observability.escalate()  → Discord 🚨 with /fire URL
    → Sir taps link on phone → GET /fire?token=...&sig=...
        → TradersPostVenue.fire() once more
        → Observability.manual_fired() → Discord ✅ or ❗
```

## TradingView alert template

Update your Pine `alert()` calls to include the secret:

```pine
alert_msg = '{"ticker":"' + syminfo.ticker + '","action":"' + action + '","price":' + str.tostring(close) + ',"sl":' + str.tostring(sl) + ',"tp":' + str.tostring(tp) + ',"grade":"' + grade + '","secret":"YOUR_SECRET_HERE"}'
alert(alert_msg, alert.freq_once_per_bar_close)
```

Or use the header-based approach — set `X-Nova-Secret: YOUR_SECRET_HERE` on the TradingView alert's webhook configuration (if supported).

## Testing locally

```bash
# 1. Start with test secret
PORT=5001 \
NOVA_WEBHOOK_SECRET=testsecret \
TRADERSPOST_WEBHOOK_URL=http://127.0.0.1:59999/fake \
python app.py &

# 2. Try without secret (should 401)
curl -X POST http://localhost:5001/webhook -d '{}'

# 3. Try with secret (full chain, TP will fail → escalation)
curl -X POST http://localhost:5001/webhook \
  -H 'X-Nova-Secret: testsecret' \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"BTCUSDT","action":"buy","price":61420.5,"grade":"A"}'

# 4. Check the ledger
curl http://localhost:5001/agents/ledger
```
