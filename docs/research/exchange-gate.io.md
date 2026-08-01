# Gate.io (Gate.com) — USDT-margined perpetual futures, API v4. REST base `https://api.gateio.ws/api/v4`, testnet `https://api-testnet.gateapi.io/api/v4` (the old `fx-api-testnet.gateio.ws` now returns 502 — verified). All futures paths take a `{settle}` segment: `usdt` (857 contracts) or `btc` (inverse, only `BTC_USD`). Docs: https://www.gate.com/docs/developers/apiv4/en/ , https://www.gate.com/docs/developers/futures/ws/en/ . NOTE: gate.com blocks plain fetchers (HTTP 403) — everything below was cross-checked against live API responses and the official OpenAPI-generated SDK docs (https://github.com/gateio/gateapi-python/blob/master/docs/FuturesApi.md).

## 返佣码机制

### mechanism

REST: a plain HTTP request header `X-Gate-Channel-Id: <your code>` on the order-placement request (CCXT actually sets it on every request). It is NOT part of the signature — headers other than KEY/SIGN/Timestamp do not enter the sign string, so you can add/change it freely without touching signing code. It is NOT a clientOrderId prefix — do not confuse it with the `text` field.

WEBSOCKET API (channel futures.order_place / futures.order_batch_place, event "api"): the code goes inside the payload as a header map. WARNING — Gate is internally inconsistent here and you should test both: the official field table documents the key as `payload.req_header` (an "apiv4 custom header" object, only `x-gate-exptime` is enumerated in it), but Gate's own Python and Go code samples on the futures WS page write it as `payload.header` -> {"x-gate-channel-id":"xxxx"}. CCXT uses `req_header`. Key casing appears case-insensitive (docs samples use lowercase `x-gate-channel-id`, CCXT uses `X-Gate-Channel-Id`).

Separate and unrelated: the order's `text` field (client order id) must be prefixed `t-`, max 28 bytes excluding the `t-` prefix, charset [0-9 A-Z a-z _ - .]. If you omit it Gate fills a reserved source tag (`api`, `apiv4-ws`, `web`, `app`). Putting your broker code in `text` does NOT earn rebate.

### example

REST (curl):
  curl -X POST https://api.gateio.ws/api/v4/futures/usdt/orders \
    -H 'KEY: <api_key>' -H 'SIGN: <hmac_sha512_hex>' -H 'Timestamp: 1785548892' \
    -H 'Content-Type: application/json' \
    -H 'X-Gate-Channel-Id: myfundingbot' \
    -H 'x-gate-exptime: 1785548893500' \
    -d '{"contract":"BTC_USDT","size":10,"price":"62900.1","tif":"gtc","text":"t-leg1-0001","reduce_only":false}'

Python requests: just add it to the session headers once —
  session.headers.update({'X-Gate-Channel-Id': BROKER_CODE})

CCXT (hardcoded default, override via headers):
  ccxt.gate({'apiKey':k,'secret':s,'headers':{'X-Gate-Channel-Id':'myfundingbot'}})
  (ccxt/ts/src/gate.ts line ~730 sets 'X-Gate-Channel-Id': 'ccxt' as the exchange-level default; see https://github.com/ccxt/ccxt/issues/24251)

WS API (per Gate's own sample on the futures WS page):
  {"time":1785548892,"channel":"futures.order_place","event":"api",
   "payload":{"header":{"x-gate-channel-id":"myfundingbot"},
              "req_id":"leg1-0001","timestamp":"1785548892","api_key":"<key>","signature":"<sig>",
              "req_param":{"contract":"BTC_USDT","size":10,"price":"62900.1","tif":"gtc"}}}
  CCXT sends the same thing under "req_header" instead of "header".

### howToGet

There is no self-service portal — the channel id is issued manually by Gate after you join the Broker Program.
1. Fill the Broker Program application form: https://www.gate.com/questionnaire/2716 (the older form https://www.gate.io/questionnaire/692 also circulates). A Gate business manager reviews and contacts you, stated turnaround 72 hours.
2. Program landing pages: https://www.gate.com/broker and https://www.gate.com/institution/broker ; activity page https://www.gate.com/activities/global-broker-program
3. Contacts: brokerage@gate.com (broker program), institutional@gate.io, mm@mail.gate.io (market maker).
4. Eligible applicant types explicitly listed: trading bots, trading platforms, copy-trading platforms, asset-management platforms, aggregators, community/social sites, exchanges, media, wallets. A solo quant tool qualifies as a "trading bot".
5. Terms (announcement 28793, https://www.gate.com/announcements/article/28793): up to 60% permanent rebate, first 3 months are an assessment-exempt grace period, ratio only ratchets up. Rebate is credited as account-book type `ab_rebate` (API Broker Rebate Income, code 3390) / `eb_rebate` (Exchange Broker, code 3410) — you can audit it via GET /futures/{settle}/account_book and GET /rebate/broker/commission_history.
6. Program note: if you use only a channel code, rewards are computed purely from volume attributed to that code; if you also use a referral code, the two volumes are combined.

I could NOT find any official page that documents the literal string `X-Gate-Channel-Id` for REST — Gate never lists it in the apiv4 reference. The only official occurrence is in the futures WebSocket page's order_place/order_batch_place code samples (`x-gate-channel-id`). The REST header name is established by CCXT's implementation (used in production by thousands of users) and is the same header. Confirm the exact string with your account manager when they issue the code.

FORMAT LIMITS: not documented anywhere. Observed values are short lowercase ASCII slugs (`ccxt`). Assume a short alphanumeric/hyphen token and use exactly what Gate issues you.

### canBeEmpty

Yes — it is entirely optional. Omitting it (or sending an unregistered value) does not fail the request; the order simply executes with no broker attribution and no rebate. Verified empirically: a signed request carrying `X-Gate-Channel-Id: mytestcode` (a value nobody registered) was rejected only by the auth layer with 401 INVALID_KEY — the header itself was accepted, never validated, never echoed as an error. That also means a typo in the code silently costs you the rebate with zero feedback: there is no API endpoint that echoes back which channel id an order was attributed to. Verify attribution by placing one live order and checking GET /rebate/broker/commission_history (or your broker dashboard) the next day.

## 认证签名

HMAC-SHA512, all in headers. Verified live against api.gateio.ws.

SIGN STRING (5 parts joined by literal "\n", no trailing newline):
  METHOD + "\n" + PATH + "\n" + QUERY + "\n" + HexEncode(SHA512(body)) + "\n" + TIMESTAMP
 - METHOD: uppercase, e.g. "POST", "GET"
 - PATH: full path INCLUDING the `/api/v4` prefix, no scheme/host/port. e.g. `/api/v4/futures/usdt/orders`
 - QUERY: raw, NOT url-encoded, in the exact same order as it appears in the request URL, e.g. `contract=BTC_USDT&status=finished&limit=50`. Empty string "" if no query params.
 - body hash: hex SHA-512 of the raw request body. For an empty body use the constant
   cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e
 - TIMESTAMP: same string you put in the Timestamp header.
SIGN = HexEncode(HMAC_SHA512(key=api_secret, msg=sign_string))  (lowercase hex)

HEADERS:
  KEY: <api key>
  SIGN: <hex digest above>
  Timestamp: <unix seconds as string>   <-- SECONDS, not ms
  Content-Type: application/json        <-- required on POST/PUT
  Accept: application/json
  X-Gate-Channel-Id: <broker code>      <-- optional, see brokerCode
  x-gate-exptime: <unix ms>             <-- optional, per-request TTL; supported on POST /futures/{settle}/orders, POST /futures/{settle}/batch_orders and PUT /futures/{settle}/orders/{order_id}. If Gate receives the request after this ms timestamp it rejects it. Strongly recommended for a 500ms convergence loop.

TOLERANCE WINDOW: 60 seconds. Verified empirically — a Timestamp 300s in the past returns HTTP 403
  {"label":"REQUEST_EXPIRED","message":"gap between request Timestamp and server time exceeds 60 current_time:... header.timestamp:..."}
The timestamp check runs BEFORE key validation (a stale request with a bogus key returns REQUEST_EXPIRED, not INVALID_KEY). Other verified errors: missing headers -> 400 {"label":"MISSING_REQUIRED_HEADER","message":"Missing required header: Timestamp"}; bad key -> 401 {"label":"INVALID_KEY"}; bad signature -> INVALID_SIGNATURE.

GOTCHA (bites everyone): for futures POST endpoints whose path contains `positions` or `dual_...` (set leverage, set margin, set risk limit, cross_mode), the parameters go in the QUERY STRING, not the JSON body. The SDK docs list them as "Content-Type: Not defined" — i.e. body is empty and its hash is the empty-string constant, while QUERY carries `leverage=10`. Only `/orders`, `/batch_orders`, `/price_orders`, `/bbo_orders`, `/countdown_cancel_all` take a JSON body.

WEBSOCKET AUTH is different (two flavours):
 - Private channel subscribe (futures.orders / futures.positions / futures.usertrades): sign string is `channel=<channel>&event=<event>&time=<sec>`, HMAC-SHA512 hex, sent in the request body as `"auth":{"method":"api_key","KEY":"<key>","SIGN":"<sig>"}`.
 - WebSocket API (event:"api", e.g. futures.order_place): sign string is `<event>\n<channel>\n<json(req_param)>\n<timestamp_sec>`, HMAC-SHA512 hex, placed in `payload.signature` alongside `payload.api_key`, `payload.timestamp`, `payload.req_id`. Same 60s window ("Gap between request time and server time must not exceed 60 seconds").

## REST 端点

### fundingCurrent

Two ways, both public.
(a) GET /futures/{settle}/contracts/{contract} — the single best call: returns `funding_rate` (current settled-period rate), `funding_rate_indicative` (live predicted), `funding_interval` (SECONDS), `funding_offset`, `funding_next_apply` (unix sec of next settlement), `funding_rate_limit` (per-period cap, e.g. "0.003" for BTC, "0.02" for 1h contracts), `funding_cap_ratio`, `funding_impact_value`.
  Live sample (BTC_USDT): {"funding_rate":"0.000034","funding_rate_indicative":"0.000034","funding_interval":28800,"funding_offset":0,"funding_next_apply":1785571200,"funding_rate_limit":"0.003"}
(b) GET /futures/{settle}/tickers[?contract=X] — one call for ALL contracts, returns per contract: `funding_rate`, `funding_rate_indicative`, `mark_price`, `index_price`, `highest_bid`/`highest_size`, `lowest_ask`/`lowest_size`, `quanto_multiplier`, `total_size`. CAUTION: REST /tickers does NOT include `funding_interval` (the WS futures.tickers push DOES). So for a cross-venue funding scanner you must join /tickers against /contracts to annualize correctly.
Also: GET /futures/{settle}/premium_index (candlestick of the premium index, max 1000 points) if you want to model the next rate yourself.

### fundingHistory

GET /futures/{settle}/funding_rate?contract={contract}&limit={n}&from={sec}&to={sec}  (public, no auth)
Response is compact: [{"r":"0.00002","t":1785542401}, ...] — `r` = rate as string, `t` = unix seconds of the settlement, newest first.

LIMITS, all probed live on 2026-08-01:
 - `limit` default 100, MAX 1000. limit=1001 -> HTTP 400 {"label":"INVALID_PARAM_VALUE","message":"Invalid request parameter `limit` value: 1001"}
 - LOOKBACK: 180 days hard. from=1690000000 -> HTTP 400 {"label":"INVALID_PARAM_VALUE","message":"from time exceeds 180-day limit"}
 - IMPORTANT UNDOCUMENTED BEHAVIOUR: if you pass `from` WITHOUT `to`, the server silently clamps the window to 30 days starting at `from` — from=179d-ago with limit=1000 returned only 90 records (30 days). You MUST pass an explicit `to` to get a wide window: from=179d-ago & to=now & limit=1000 returned 537 records spanning 2026-02-03 -> 2026-08-01. Same with no from/to at all: returns last 30 days only (90 records for an 8h contract), regardless of limit.
 - So the correct full-history pull is: from=now-180d, to=now, limit=1000, then page backwards with `to`.
 - The 1000-record cap binds differently by interval: 8h -> 1000 recs = 333 days (never binds, 180d = 540 recs); 4h -> 180d = 1080 recs, so you need 2 pages; 1h -> 1000 recs = ~41 days, so you need ~5 pages to cover 180d.
Verified 4h contract ACT_USDT returns 180 recs for a 30-day window; 1h contract T_USDT settles at :00 every hour.

### placeOrder

POST /futures/{settle}/orders   (JSON body, Content-Type: application/json)
Body fields (model FuturesOrder):
  contract    string  required, e.g. "BTC_USDT"
  size        int (or decimal string on decimal-enabled contracts) — required. SIGNED: positive = buy/long, negative = sell/short. Units are CONTRACTS, not coins. 0 only for close-position orders.
  price       string  — "0" means market order (must be paired with tif=ioc or fok). Otherwise limit price, must be a multiple of `order_price_round`.
  tif         string  — "gtc" (default) | "ioc" (taker only) | "poc" (post-only, always maker) | "fok" (fill-or-kill). FOK IS SUPPORTED.
  reduce_only bool    — true to guarantee the order can only reduce the position (use this on the unwind leg)
  close       bool    — true + size=0 closes the whole position (single-position mode)
  auto_size   string  — "close_long" | "close_short", dual-position mode close, requires size=0
  iceberg     int/string
  text        string  — client order id, must start with "t-", <=28 bytes after the prefix, [0-9A-Za-z_.-]
  stp_act     string  — "cn"|"co"|"cb" self-trade prevention (only if you joined an STP group, else it errors)
  market_order_slip_ratio  string — max slippage for market orders
  pid         int     — position id (split-position mode)
Optional header: x-gate-exptime (unix ms) — server rejects if it arrives late. Ideal for a 500ms loop.
Other order endpoints:
  POST /futures/{settle}/batch_orders   — up to 10 orders; each order REQUIRES a `text`
  POST /futures/{settle}/bbo_orders     — place at a BBO level without knowing the price (added v4.105.10); very useful for delta convergence
  PUT  /futures/{settle}/orders/{order_id}  — amend price/size in place (accepts the `text` custom id instead of order_id while the order is on the book, and up to 60s after it finishes). Cheaper than cancel+replace.
  POST /futures/{settle}/batch_amend_orders
  DELETE /futures/{settle}/orders?contract=X[&side=]   — cancel all for a contract
  DELETE /futures/{settle}/orders/{order_id}
  POST /futures/{settle}/countdown_cancel_all          — dead-man switch; essential for a two-leg bot
  GET  /futures/{settle}/orders?contract=&status=open|finished
  GET  /futures/{settle}/my_trades , /futures/{settle}/my_trades_timerange

### positions

GET /futures/{settle}/positions?holding=true&limit=&offset=   (all positions; `holding=true` filters to non-zero)
GET /futures/{settle}/positions/{contract}                      (single)
GET /futures/{settle}/dual_comp/positions/{contract}            (dual/hedge mode — returns both sides)
Key fields (model Position): size (int, SIGNED — negative = short), leverage ("0" means cross margin, positive = isolated), entry_price, mark_price, liq_price, value, margin, unrealised_pnl, realised_pnl, and the PnL decomposition you need for funding accounting: pnl_pnl (price P/L), pnl_fund (FUNDING FEES), pnl_fee (trading fees), history_pnl, adl_ranking (1..5, 6 = flat), maintenance_rate, risk_limit, leverage_max, settlement_currency, pos_margin_mode (isolated|cross), lever.
Use `pnl_fund` to reconcile realized funding per leg without re-deriving it from rates.
Also: GET /futures/{settle}/position_close (close history), GET /futures/{settle}/account_book?type=fund (funding cashflow ledger).

### balance

GET /futures/{settle}/accounts   — one futures wallet per settle currency.
Fields (model FuturesAccount): total, available, unrealised_pnl, position_margin, order_margin, point, currency, bonus, position_mode ("single" | "dual" | "dual_plus"), in_dual_mode (deprecated), enable_credit (portfolio margin), margin_mode (0=classic, 1=cross-currency, 2=combined), plus the new-classic cross fields cross_available / cross_margin_balance / cross_mmr / cross_imr / cross_initial_margin / cross_maintenance_margin, and unified-account fields position_initial_margin / maintenance_margin.
For delta-neutral sizing use `available` (note: it INCLUDES bonus, which cannot be withdrawn) and `cross_available` when running new-classic cross margin.
Ledger: GET /futures/{settle}/account_book?type=dnw|pnl|fee|refr|fund — `fund` rows are the funding payments.

### setLeverage

POST /futures/{settle}/positions/{contract}/leverage?leverage={n}&cross_leverage_limit={n}
  *** Parameters go in the QUERY STRING, not the body (SDK marks Content-Type as "Not defined"; CCXT special-cases any futures POST path containing `positions` or `dual_` to url-encode instead of body-encode). The empty body still hashes to the cf83e1... constant for the signature. ***
  - isolated margin: set `leverage` to the value, leave `cross_leverage_limit` empty
  - cross margin:    set `leverage=0` AND set `cross_leverage_limit` to the value
  - optional `pid` (position id) for split-position mode
Newer, simpler alternative (added v4.106.12): POST /futures/{settle}/positions/{contract}/set_leverage — "update leverage for specified mode", plus GET /futures/{settle}/get_leverage/{contract} to read it back (model FuturesLeverage).
Dual/hedge mode: POST /futures/{settle}/dual_comp/positions/{contract}/leverage
Margin mode / position mode:
  POST /futures/{settle}/positions/cross_mode
  POST /futures/{settle}/set_position_mode   (replaces the old POST /futures/{settle}/dual_mode)
Risk limit (raise it before large size, or max leverage drops): POST /futures/{settle}/positions/{contract}/risk_limit ; tiers via GET /futures/{settle}/risk_limit_tiers.

### instruments

GET /futures/{settle}/contracts            (all; 857 rows on usdt, ~1.1 MB)
GET /futures/{settle}/contracts/{contract} (one)
Mapping to the usual tickSize/lotSize/minNotional concepts — Gate has NO field literally named any of those:
  tickSize   = `order_price_round`   (BTC_USDT "0.1", ETH_USDT "0.01", DOGE_USDT "0.00001")
               `mark_price_round` is the separate rounding of the mark price.
  lotSize    = 1 contract for 842 of 857 contracts (`order_size_min`:1, integer sizes only).
               For the 15 contracts with `enable_decimal`:true (ETH, SOL, XRP, TRX, PEPE, M, LAB, SIREN, UB, RIVER, RAVE, ARIA, ESPORTS, JDON, SOXS as of 2026-08-01) `order_size_min` is 0 and size may be a DECIMAL STRING. The decimal step is not exposed as a field — treat sizes as strings and probe.
  contract value = `quanto_multiplier` = coins per contract (BTC 0.0001, ETH 0.01, DOGE 10, T 100). Coin qty = size * quanto_multiplier. `quanto_multiplier` is "0" for the inverse BTC_USD contract.
  minNotional = NOT PROVIDED. Effective minimum = order_size_min * quanto_multiplier * price (BTC_USDT ~ $6.3 at 63k). Violations return label ERR_QUOTA_LOWER_LIMIT ("Not meeting the minimum order amount").
  max size   = `order_size_max` (12,000,000 for BTC), `market_order_size_max` separately for market orders.
Other fields you want for a funding bot: leverage_min / leverage_max / cross_leverage_default, maintenance_rate, risk_limit_base/step/max, maker_fee_rate & taker_fee_rate (BASE tier, not yours: BTC_USDT "-0.0001"/"0.00075"), order_price_deviate (max % away from mark you may place — 0.03 for BTC, 0.2 for DOGE), orders_limit (100 open orders per contract), in_delisting, status, is_pre_market, launch_time, type ("direct"|"inverse"), contract_type (""=crypto, "stocks", "indices", "metals", "commodities", "forex").
Order book: GET /futures/{settle}/order_book?contract=&limit=&with_id=true -> {"id":..,"current":..,"update":..,"asks":[{"s":59197,"p":"62902.2"}],"bids":[...]}

### feeRate

GET /futures/{settle}/fee[?contract=BTC_USDT]  (private, apiv4 auth)
Returns a MAP keyed by market: {"BTC_USDT":{"taker_fee":"0.0005","maker_fee":"0.0002"}} — these are YOUR effective rates (VIP tier + GT discount + broker-set sub-account rates applied). Omit `contract` to get every market.
Alternative / cross-product: GET /wallet/fee — returns spot and futures taker/maker plus gt_discount flags in one call.
Do NOT use the `maker_fee_rate`/`taker_fee_rate` on GET /futures/{settle}/contracts for P&L — those are the public base-tier numbers, not your tier.
Per-fill actuals come back on each order/trade as `mkfr`/`tkfr` (and `refr` = referral rebate rate), so you can reconcile.

## WebSocket

ENDPOINTS (verified live 2026-08-01):
  prod  USDT perp:  wss://fx-ws.gateio.ws/v4/ws/usdt
  prod  BTC inverse: wss://fx-ws.gateio.ws/v4/ws/btc
  prod  SBE binary:  wss://fx-ws.gateio.ws/v4/ws/usdt/sbe   (append /sbe; JSON requests, SBE pushes)
  testnet:           wss://ws-testnet.gate.com/v4/ws/futures/usdt
  (delivery futures: wss://fx-ws.gateio.ws/v4/ws/delivery/usdt)
Connect-time header worth setting: `X-Gate-Size-Decimal: 1` — makes all size/volume fields strings that may carry decimals. WITHOUT it sizes are integers TRUNCATED toward zero (a 1.7-contract level is pushed as 1). Gate has said the integer push will eventually be retired. Affects futures.book_ticker (B, A), futures.order_book_update (a.s/b.s), futures.trades, futures.tickers (total_size), futures.positions (size), futures.orders (size/left/iceberg). The same header works on REST — verified: ETH_USDT order_book asks went from {"s":5116} to {"s":"5116.8"}.

SUBSCRIBE FORMAT (all channels):
  {"time": <unix_sec>, "id": <int32>, "channel": "<name>", "event": "subscribe", "payload": [...]}
Ack comes back with "event":"subscribe" and result {"status":"success"}; check `error` is null (Gate says do not parse `result`). `id` must be an int32.

BEST BID/ASK — channel `futures.book_ticker`, payload = list of contract names. Live capture:
  {"time":1785548245,"time_ms":1785548245220,"channel":"futures.book_ticker","event":"update",
   "result":{"t":1785548245214,"u":120900607904,"s":"BTC_USDT","b":"62914.2","B":20884,"a":"62914.3","A":33644}}
  t = ms timestamp, u = order book update id, s = contract, b/B = best bid px/size, a/A = best ask px/size. Empty string on b or a means that side is empty. This is the channel to drive a 500ms convergence loop.

INCREMENTAL DEPTH — channel `futures.order_book_update`, payload = [contract, frequency, level].
  frequency: "20ms" or "100ms" only (the "1000ms" interval and the "10" level were REMOVED 2024-11-18).
  level: "100" | "50" | "20"; with "20ms" only "20" is allowed.
  Live capture: {"channel":"futures.order_book_update","event":"update","result":{"t":1785548245213,"U":120900607897,"u":120900607901,"s":"BTC_USDT","a":[],"b":[{"p":"62914.2","s":21129}],"l":"20"}}
  Sync rule: first message after subscribe is a full snapshot (`full`:true) — replace local book and set local id = u. Then each delta must satisfy U == local_id + 1; if not, you MUST unsubscribe and resubscribe. A level with size "0" is a deletion.
  Order Book V2: channel `futures.obu`, payload = ["ob.BTC_USDT.400"] or ["ob.BTC_USDT.50"]; level 400 updates every 100ms, level 50 every 20ms.

FUNDING RATE PUSH — channel `futures.tickers`, payload = list of contracts. This is the only streaming source of funding data and it carries the settlement schedule, which REST /tickers does not. Live capture:
  {"channel":"futures.tickers","event":"update","result":[{"contract":"BTC_USDT","last":"62914.3","mark_price":"62914.3","index_price":"62937.6",
    "funding_rate":"0.000033","funding_rate_indicative":"0.000033","funding_interval":28800,"funding_offset":0,"funding_next_apply":1785571200,
    "total_size":"651185722","volume_24h":"518068345","high_24h":...,"low_24h":...,"change_percentage":"-3.1290","t":1785548246983}]}
  There is NO dedicated funding channel — subscribe futures.tickers. Push cadence observed ~1-2s; it is not tick-by-tick.

PRIVATE CHANNELS (need `auth` in the request body, see auth section; `payload` must be prefixed with your numeric user id for futures):
  futures.orders, futures.usertrades, futures.positions, futures.balances, futures.liquidates, futures.autoorders, futures.auto_deleverages

HEARTBEAT / RECONNECT:
  - Gate futures uses PROTOCOL-LEVEL ping/pong: the SERVER sends ping frames and disconnects you if your client does not pong. Every mainstream WS library answers automatically; if you disable auto-pong (e.g. python-websockets ping_interval=None only disables client-initiated pings, which is fine) make sure library-level pong replies stay on.
  - Optional application-level heartbeat: send {"time":<sec>,"channel":"futures.ping","id":<n>} and you get back {"channel":"futures.pong","event":"","result":null} — verified live. Use this to measure RTT / detect a half-open socket.
  - Watch for the shutdown warning: channel "futures.system", event "update", result.type == "upgrade" means reconnect now.
  - Resubscribe everything after reconnect; nothing is restored automatically.

SUBSCRIPTION LIMITS:
  - No documented cap on the number of contracts per connection, and none observed: a single subscribe with 200 contracts on futures.book_ticker was accepted and streamed fine.
  - Documented cap: **300 connections per IP**.
  - Documented per-channel rule: for the same contract's depth stream, one connection may subscribe only ONCE — a duplicate subscribe errors.
  - WS trading rate limit: futures batch/single place + amend + single/bulk cancel = 100 r/s total. Everything else unlimited.
  - ATOMIC SUBSCRIBE GOTCHA (found live): a subscribe payload fails ENTIRELY if any single symbol is unknown. Subscribing all 857 usdt contracts returned {"error":{"code":2,"message":"unknown currency pair 币安人生_USDT"},"result":{"status":"fail"}} and streamed nothing. Batch your subscriptions (100-200 at a time) and pre-filter symbols.
  - WS API responses carry x_gate_ratelimit_requests_remain / x_gate_ratelimit_limit / x_gat_ratelimit_reset_timestamp (note the typo `x_gat_` in the third field — it is spelled that way in the official docs) inside the response `header`, plus x_in_time / x_out_time in microseconds for server-side latency attribution.
  - Error codes on subscribe: 1 invalid argument struct, 2 invalid argument, 3 service error, 4 authentication fail.

## 资金费周期

STANDARD IS 8 HOURS, BUT GATE HAS BOTH 4h AND 1h CONTRACTS — this matters a lot for a funding-arb tool, and a naive "rate * 3 * 365" annualization will be wrong on 40% of the book.

Live census of GET /futures/usdt/contracts on 2026-08-01 (857 contracts):
  funding_interval = 28800 (8h)  -> 516 contracts   (BTC_USDT, most majors and stock perps)
  funding_interval = 14400 (4h)  -> 336 contracts   (ACT, AERO, AEVO, 2Z, ACU, AEON, AGT, ... most of the alt book)
  funding_interval =  3600 (1h)  ->   5 contracts   (BANK_USDT, COTI_USDT, DEXE_USDT, ERA_USDT, T_USDT)
`funding_offset` was 0 on all 857.

HOW TO QUERY IT PER CONTRACT (this is the authoritative way, do not hardcode 8h):
  GET /futures/{settle}/contracts/{contract}  ->  `funding_interval` (SECONDS), `funding_offset`, `funding_next_apply` (unix sec of the next settlement), `funding_rate` (current period), `funding_rate_indicative` (predicted), `funding_rate_limit` (per-period clamp), `funding_cap_ratio`, `funding_impact_value`.
  Or pull all of them in one shot with GET /futures/{settle}/contracts and build a {contract -> interval} map at startup, refreshing periodically (Gate changes intervals per contract; `config_change_time` tells you when the config last changed).
  Streaming: the WS `futures.tickers` push carries funding_interval / funding_offset / funding_next_apply per message, so you get schedule changes for free if you already subscribe there.
  IMPORTANT: REST GET /futures/{settle}/tickers does NOT include funding_interval — only the WS version and /contracts do. If you build your scanner off REST /tickers you will silently annualize 4h and 1h contracts as if they were 8h (3x and 8x understatement of carry).

Annualized funding = rate * (31,536,000 / funding_interval).
  8h -> rate * 1095 ;  4h -> rate * 2190 ;  1h -> rate * 8760.

SETTLEMENT CLOCK (verified from funding_rate history timestamps):
  8h contracts settle at 00:00 / 08:00 / 16:00 UTC (records stamped e.g. 1785542401 = 2026-08-01 00:00:01Z; the +1s offset is normal).
  4h contracts settle at 00:00 / 04:00 / 08:00 / 12:00 / 16:00 / 20:00 UTC.
  1h contracts settle on the hour (T_USDT history: 2026-08-01 01:00:00Z, 00:00:01Z, 2026-07-31 23:00:00Z, 22:00:00Z).
Compute the next settlement as `funding_next_apply` directly rather than deriving it from interval+offset.

Per-period rate cap is per contract via `funding_rate_limit`: 0.003 (0.3%) on BTC_USDT, 0.02 (2%) on the 1h contract T_USDT. Factor this into worst-case carry.

## 坑

- SIZE IS IN CONTRACTS, NOT COINS, AND IT IS SIGNED. `size` on POST /futures/{settle}/orders is an integer count of contracts; positive = buy/long, negative = sell/short. There is no `side` field. Coin quantity = size * quanto_multiplier (BTC_USDT 0.0001 BTC/contract, ETH_USDT 0.01, DOGE_USDT 10, T_USDT 100). To go short 0.5 BTC you send size = -5000. Rounding: floor(target_coins / quanto_multiplier) for 842 of 857 contracts, since order_size_min is 1 and only integers are accepted.
- DECIMAL SIZES ARE A NEW, PARTIAL ROLLOUT. 15 of 857 usdt contracts have `enable_decimal`:true and `order_size_min`:0 (ETH, SOL, XRP, TRX, PEPE, M, LAB, SIREN, UB, RIVER, RAVE, ARIA, ESPORTS, JDON, SOXS as of 2026-08-01) — on those `size` may be a decimal string. The step size is NOT exposed as a field. Worse, you only SEE decimals if you send `X-Gate-Size-Decimal: 1` (REST header and WS connect header). Without it Gate truncates toward zero on the way out — verified live: ETH_USDT best ask size came back as {"s":5116} instead of {"s":"5116.8"}. Always send the header and parse sizes as strings/Decimal, never as int.
- PRICE PRECISION AND THE PRICE BAND. Limit price must be a multiple of `order_price_round` (BTC_USDT 0.1, DOGE_USDT 0.00001) — send it as a STRING, never a float, or you get rounding rejections. Separately, `order_price_deviate` caps how far from the mark price you may place: 0.03 (3%) on BTC_USDT but 0.2 (20%) on DOGE_USDT. A far-touch protective order on a major will be rejected outside 3%. Also `orders_limit` = 100 open orders per contract.
- MARKET ORDERS ARE price="0" PLUS tif. There is no `type` field on futures orders. A market order is price="0" with tif="ioc" (or "fok"); tif="gtc"/"poc" with price 0 is rejected. FOK IS FULLY SUPPORTED on futures (tif enum = gtc | ioc | poc | fok, per the FuturesOrder model), unlike some venues. `poc` = post-only (PendingOrCancelled), guaranteed maker. Note BTC_USDT maker fee is NEGATIVE at base tier (-0.0001), so poc legs can be fee-positive.
- SET-LEVERAGE PARAMS GO IN THE QUERY STRING, NOT THE BODY. POST /futures/{settle}/positions/{contract}/leverage?leverage=10 — the body is empty (so its SHA-512 is the cf83e1... constant) while the query string enters the sign string. CCXT hardcodes this: any futures POST whose path segment contains `positions` or `dual_` gets url-encoded instead of body-encoded. Signing it as a JSON body gives INVALID_SIGNATURE. Also the cross/isolated split is unintuitive: isolated = set `leverage`, leave cross_leverage_limit empty; cross = set `leverage=0` AND set `cross_leverage_limit`.
- QUERY STRING IN THE SIGNATURE IS RAW, NOT URL-ENCODED, AND ORDER-SENSITIVE. The sign string uses `contract=BTC_USDT&status=finished&limit=50` exactly as it appears in the URL — do not percent-encode it for signing (but the actual URL may be encoded). Parameter order must match the URL byte-for-byte. CCXT keeps two separate strings (rawQueryString for signing, urlencode for the URL) precisely because of this. Known trap: comma-separated list params (e.g. `currencies=A,B`) must NOT be sent as %2C.
- TIMESTAMP IS SECONDS AND THE WINDOW IS 60s, CHECKED BEFORE THE KEY. Sending milliseconds gives REQUEST_EXPIRED, not a format error. Verified: 300s stale -> HTTP 403 {"label":"REQUEST_EXPIRED","message":"gap between request Timestamp and server time exceeds 60 ..."}. On a 500ms loop, sync your clock (NTP) or track the server/local delta; ccxt exposes options.adjustForTimeDifference for exactly this. Use the optional `x-gate-exptime` header (unix MILLIseconds) on order create/amend so a request delayed in flight is rejected rather than executed stale — this is the single most valuable safety feature for a 500ms convergence loop.
- RATE LIMITS ARE PER-ENDPOINT AND SPLIT BY ACTION (official table). Public endpoints: 200 r/10s per endpoint, keyed by IP. Perpetual futures private: order place + amend (batch or single) = 100 r/s TOTAL, order cancel (batch or single) = 200 r/s, everything else = 200 r/10s per endpoint, keyed by UID and counted per sub-account. WebSocket trading: futures place/amend/cancel = 100 r/s total; connections per IP <= 300. Every REST response carries X-Gate-RateLimit-Requests-Remain / X-Gate-RateLimit-Limit / X-Gate-RateLimit-Reset-Timestamp (verified live: 199/200 on a tickers call) — read them instead of guessing. VIP 14+ accounts get fill-ratio-based tiers up to 400 r/s; low fill ratio can get you throttled, so prefer PUT /orders/{order_id} (amend) over cancel+replace in the convergence loop.
- PERP SYMBOLS ARE `BASE_QUOTE` AND MATCH SPOT EXACTLY — but the contract list is polluted. `BTC_USDT` is the perp name AND the spot currency_pair name, so cross-venue and cross-product mapping is trivial. The traps: (a) 284 of 857 usdt contracts are `contract_type`:"stocks" (AAPLX_USDT, NVDAX_USDT, AAPU_USDT...), plus 16 "indices", 12 "metals", 3 "commodities", 3 "forex" — filter on contract_type=="" for crypto only; (b) 9 contracts have is_pre_market:true (ANTHROPIC_USDT, OPENAI_USDT, KALSHI_USDT, NEURALINK_USDT...) with no spot leg; (c) TWO CONTRACTS HAVE NON-ASCII CJK NAMES (币安人生_USDT and 龙虾_USDT) and the WS rejects them — subscribing all 857 contracts at once fails atomically with {"error":{"code":2,"message":"unknown currency pair 币安人生_USDT"}} and you receive zero data. Regex-filter to ^[A-Z0-9]+_USDT$ and batch subscriptions ~100-200 at a time. Inverse contracts live under settle=btc with type:"inverse", quanto_multiplier "0" and name BTC_USD; delivery futures are a different path (/delivery/{settle}/) with names like BTC_USDT_20260327.
- NO minNotional FIELD EXISTS. Effective minimum = order_size_min * quanto_multiplier * price (about $6.3 for 1 BTC_USDT contract at 63k). Undersized orders come back with label ERR_QUOTA_LOWER_LIMIT ("Not meeting the minimum order amount"). Budget for this when the two legs have different contract granularity — matching a Gate DOGE leg (10 DOGE/contract) against a venue with 1-DOGE granularity leaves residual delta you cannot close.
- USE `pnl_fund` FROM THE POSITION, NOT YOUR OWN RATE MATH, TO BOOK FUNDING. GET /futures/{settle}/positions/{contract} decomposes realised_pnl into pnl_pnl (price), pnl_fund (funding), pnl_fee (fees); GET /futures/{settle}/account_book?type=fund is the per-event ledger. Also: the public maker_fee_rate/taker_fee_rate on /contracts is the BASE tier, not yours — your real rates come from GET /futures/{settle}/fee (a map keyed by contract) and per-fill `mkfr`/`tkfr` on each order.
- THE `text` CLIENT ORDER ID HAS HARD RULES AND IS NOT THE BROKER CODE. Must start with `t-`, at most 28 bytes AFTER the prefix, charset [0-9 A-Z a-z _ - .]. Reserved non-`t-` values identify order source (api, web, app, liquidation, liq-xxx, insurance, auto_deleveraging...). Batch order placement (POST /futures/{settle}/batch_orders, max 10) REQUIRES a `text` on every order. You may cancel/amend by `text` instead of order_id while the order is on the book and for 60s after it finishes; after that only the numeric order_id works. And finished orders vanish from the query API 10 minutes after completion — persist your own fills.
- DEAD-MAN SWITCH EXISTS AND YOU SHOULD USE IT. POST /futures/{settle}/countdown_cancel_all auto-cancels your open futures orders if you stop heartbeating. For a two-leg delta-neutral bot, a process crash between leg 1 and leg 2 is the main tail risk; this plus `reduce_only` on unwind legs and `x-gate-exptime` on every order is the minimum safety kit.
