# BRAHMA ALARM v2

A risk-controlled Dhan options bot with a live dashboard. **No database** — state is plain JSON/JSONL under `data/`.

The default mode is **paper trading with real Dhan market data**: real underlying candles, real option LTPs, local simulated fills, and **no order is ever sent to Dhan**. Live trading is an explicit, guarded switch.

---

## Quick start

```powershell
cd brahma-alarm
python -m pip install -r requirements.txt
copy .env.example .env
```

Put your Dhan credentials in `.env`:

```ini
DHAN_CLIENT_ID=...
DHAN_ACCESS_TOKEN=...
DATA_PROVIDER=dhan
BROKER=dhan
LIVE_TRADING_ENABLED=false
```

Then run:

```powershell
python run.py
```

Open <http://127.0.0.1:8000>.

The dashboard shows `PAPER` until you explicitly switch to live.

---

## Modes

| Mode | How | Market data | Fills | Orders sent to Dhan |
|---|---|---|---|---|
| **PAPER** (default) | `BROKER=dhan`, `LIVE_TRADING_ENABLED=false` | real Dhan | simulated locally | no |
| **LIVE** | dashboard **Go live**, or `LIVE_TRADING_ENABLED=true` + restart | real Dhan | real Dhan | yes |

`data/paper_account.json` holds the simulated paper cash, position and stop. `data/state.json` holds the bot's position and entry counters.

### Switching to live

Two equivalent paths:

1. **Dashboard:** click **Go live** and type `LIVE`. The engine switches from the paper adapter to the real Dhan broker, then **blocks entries** — click **Resume** when you are ready for real orders. The switch is refused while an order is pending or a paper position is open.
2. **Config:** set `LIVE_TRADING_ENABLED=true` in `.env` and restart.

Use **Paper mode** (type `PAPER`) to switch back. A restart always returns to the mode in `.env`, so production intent must be persisted there.

Recommended live guards:

```ini
USE_BROKER_SL=true
MAX_TRADES_PER_DAY=3
MAX_DAILY_LOSS=1000
```

The live adapter also refuses to place orders whenever `LIVE_TRADING_ENABLED=false`, independently of the engine.

---

## Dashboard

- **Status pills**: running / halted / session / mode / provider.
- **KPIs**: day P&L, position, underlying LTP, last signal, entries, active stop.
- **Chart**: underlying candles + the two strategy lines.
- **Risk panel**: kill switch, entry-block reason, pending order, last exit, instrument, broker SL.
- **Trades / Orders / Log**: append-only journals, no database.
- **Controls** (each requires typing the action word):
  - `HALT` — kill switch (blocks entries; protective exits still run)
  - `RESUME` — clear kill switch / entry block (refuses while an order is pending)
  - `BLOCK` / `ALLOW` — stop or resume new entries only
  - `FLATTEN` — market-exit the open position
  - `RESET` — reset the paper account (paper mode only)
  - `LIVE` — switch to real Dhan orders (paper only, flat only)
  - `PAPER` — switch back to simulated fills (live only, flat only)

Bind the dashboard to `127.0.0.1` (default). It has no authentication.

---

## Architecture

```
brahma/
  config.py          env validation, one place for every knob
  timeutils.py       IST, market sessions, candle age
  models.py          Signal / Snapshot / OrderIntent / OrderResult / Instrument
  state.py           atomic state.json + JSONL journals
  indicators.py      fast WMA/HMA/HMA3 + Kalman
  strategy.py        pure signal function (no I/O)
  instruments.py     Dhan instrument-master resolution (expiry/strike/lot/freeze)
  risk.py            pre-trade + in-trade checks
  engine.py          the loop, order state machine, reconciliation, mode switch
  providers/         dhan.py (REST polling)
  broker/            dryrun.py (paper fills) | dhan.py (live orders)
  api.py             FastAPI dashboard + control API
  web/               vanilla HTML/CSS/JS dashboard
```

### The loop

```
every tick:
  reconcile pending order (if any)
  broker snapshot
  protective exits: stop-loss / daily loss / square-off / max holding
new candle only:
  compute signal
  entry (if allowed) or strategy exit
```

Protective exits are evaluated on every tick and are never disabled by the entry block or kill switch.

### Order safety

- **Write-ahead journal**: the order intent is persisted before it reaches the broker.
- **Correlation id**: every order carries a 20-char id used as the Dhan `tag`.
- **Reconciliation**: on every tick (and at startup) a pending order is queried with `get_order_by_correlationID`. A filled order is applied; a failed one is cleared; an unknown one keeps entries blocked.
- **No guessing**: if broker P&L is missing, unrealized P&L is derived from LTP and average price and flagged; if even that is impossible, the position is treated as unsafe and entries stay blocked.
- **Paper is enforced twice**: the paper adapter never sends an order, and `DhanBroker.place_order` itself refuses to send when `LIVE_TRADING_ENABLED=false`.
- **External close**: if the broker goes flat while the engine thinks it is long (broker SL, manual square-off), the engine journals the close and blocks entries for review. If the broker is long but state is empty after a crash, the position is adopted so stops still work.

---

## Risk model

| Control | Behaviour |
|---|---|
| `MAX_DAILY_LOSS` | Blocks entries and exits the position; the block expires next trading day |
| `MAX_TRADE_LOSS` + `STOP_LOSS_PERCENT` | Effective stop is the tighter of the two |
| `MAX_TRADES_PER_DAY` | Blocks new entries (does **not** halt exits) |
| `MAX_HOLDING_MINUTES` | Optional time stop |
| `COOLDOWN_AFTER_LOSS_MIN` | Optional pause after a losing exit |
| `SQUARE_OFF_TIME` | Exits before market close |
| `NO_ENTRY_AFTER` | No new entries late in the session |
| `USE_BROKER_SL` | Places a broker-side SL-M after entry (recommended for live) |

`HALT` is a kill switch for entries. Protective exits are always evaluated.

---

## Safety notes

- This is **not financial advice** and the software stop is **not** a broker guarantee. Use `USE_BROKER_SL=true` for live positions.
- Options can gap through a stop. Position sizing (`lots`, `MAX_TRADE_LOSS`) matters more than the signal.
- Verify the Dhan response fields against your account before enabling live trading. The adapters are defensive, but broker payloads change.
- The dashboard has no login. Keep it on `127.0.0.1` or put it behind a VPN/reverse proxy.
- Paper fills use the real option LTP plus `PAPER_SLIPPAGE_BPS`; they validate plumbing, not edge.

### Windows / OneDrive

`state.json` is written atomically (temp file + rename) with retries. OneDrive, antivirus scanners and indexers can briefly lock the file, which makes the rename fail with `WinError 5`; the store then falls back to an in-place write, keeps the bot running, and surfaces the problem in the dashboard (`state: ...`) until the next successful atomic save.

If you see repeated `state:` warnings, move the project out of a synced folder (for example `C:\brahma-alarm`) or pause OneDrive sync for `data/`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required` | Fill the credentials in `.env` |
| `instrument unresolved` | Check your Dhan data plan, or set `OPTION_SECURITY_ID` manually |
| `candle is stale` | Check the Dhan data plan/rate limits; increase `MAX_CANDLE_AGE_SEC` only if you understand the risk |
| `order still PENDING` | The bot keeps entries blocked and reconciles every tick; verify the order in Dhan |
| Dashboard chart empty | The chart CDN may be blocked; the bot still runs (fallback message appears) |
| `ModuleNotFoundError` | `python -m pip install -r requirements.txt` |
