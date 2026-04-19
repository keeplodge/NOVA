# Session 2026-04-18 17:50 EST — Weekend Trading Strategy Explored and Declined

Sir explored building a weekend-only BTCUSD day-trading strategy with a 3-Saturday / 2-Sunday cadence and a strict $200 per-trade profit target (target ~$52K/year from discipline-based weekend scalps). After working through broker/account constraints, he concluded the idea doesn't make sense at this stage and chose to stay focused on the weekday NOVA ICT system. Save this so we don't re-propose weekend trading in future sessions unless the underlying constraints change.

## The idea

- Persona: billionaire crypto day-trader whose edge was pure weekend discipline.
- Structure: 3 trades Saturday + 2 trades Sunday, $200 fixed profit target per trade, exit the instant target prints. No 6th trade, no extending winners, no revenge trades.
- Auto-traded via a Pine Script on TradingView, alerting into the NOVA Railway webhook, fanning out through TradersPost to 3 accounts — same pipeline pattern as NOVA ICT.
- Goal: repeatable ~$1,000/weekend × 52 weeks ≈ $52K/year from disciplined weekend crypto.

## Why it was declined

- **Structural blocker: CME is closed from Friday 5:00pm ET to Sunday 6:00pm ET.** Sir's 3 prop accounts (Apex 50K, Apex 100K, Lucid 50K) route exclusively to CME. Saturday trading is physically impossible on these accounts. Sunday before 6:00pm ET is also dead. The earliest weekend window the prop accounts even offer is Sunday 6-11pm ET, which overlaps the Asia session NOVA ICT already owns.
- **No CME crypto product trades the weekend.** MBT (Micro BTC) and MET (Micro ETH) are on CME, so they inherit CME's closed-weekend hours. There's no workaround — the exchange is off, not the broker.
- **Opening a separate crypto broker (Alpaca / Coinbase / Binance) was the only real path to true Saturday + Sunday trading.** Sir weighed it and said no — real personal capital at risk on an unvalidated strategy, plus new-broker integration risk on day one, plus splitting his trading attention between two lanes before the first lane is fully milked.
- **His weekday NOVA ICT is already compounding.** Three prop accounts all sitting within $3-6K of their pass targets. The marginal value of adding unvalidated weekend trades is small compared to the downside of letting discipline slip.

## Options that were on the table (none chosen)

1. **CME Micro BTC futures (MBT) on the existing 3 prop accounts** — works but only Sunday 6pm ET onward, overlaps Asia session.
2. **Alpaca Crypto linked to TradersPost** — gives real 24/7 BTCUSD but requires a new broker account with real personal capital.
3. **Coinbase Advanced via TradersPost** — same shape as Alpaca, slightly more setup friction.
4. **Sunday 6-8pm ET re-open scalp on NQ / MBT through Apex/Lucid** — real structural edge (50 hours of offline news priced in at reopen) but only 2h/weekend and steps on Asia.
5. **No weekend trading** — chosen. Weekday system already works.

## Guidance for future sessions

- **Do not re-propose weekend trading on the prop accounts.** The CME-closed-Friday-5pm constraint is not going to change. Only re-open the conversation if Sir asks directly, or if he opens a separate crypto/forex broker for 24/7 access.
- **If Sir ever asks about "weekend edge" again**, the honest answer is: the Sunday 6pm ET CME reopen is the only structural weekend-adjacent edge his current accounts can capture. Everything else requires a new broker.
- **Micro BTC/ETH on CME (MBT/MET) ARE compatible with Apex/Lucid** during CME hours (Sun 6pm - Fri 5pm ET). Worth remembering as a weekday-crypto option if he ever wants to diversify product mix on the prop accounts, independent of this weekend idea.
- **Pattern to watch for:** aspirational roleplay ("imagine you're a ___ trader who does ___") can be useful for framing, but always reality-check against Sir's actual account constraints before building. The weekend-billionaire persona was the hook, but the account reality broke the plan.

## What Sir is actually doing well here

- Noticed the idea didn't line up, stopped, and said so. That's the same muscle that exits winners at $200 instead of hoping for $400 — the ability to kill a trade idea before it eats time and capital is a core part of the edge. Worth flagging for future reflection: "declining a setup" is as valuable as executing one.
