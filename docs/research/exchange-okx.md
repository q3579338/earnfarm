# OKX (v5 API, Unified/Trading Account)

## 返佣码机制

### mechanism

JSON REQUEST BODY FIELD named `tag`, on the order-placing call itself. NOT a header, NOT a clOrdId prefix, NOT an API-key-level setting — it must be sent on EVERY order.

Applies to: POST /api/v5/trade/order, POST /api/v5/trade/batch-orders (per-order), POST /api/v5/trade/order-algo, POST /api/v5/trade/close-position, plus grid/recurring-buy/block-trading/spread(sprd)/convert/financial endpoints — anywhere the request schema exposes `tag`.

FORMAT (verbatim from OKX v5 docs, 'tag' row of Place Order): 'Order tag. A combination of case-sensitive alphanumerics, all numbers, or all letters of up to 16 characters.' => regex ^[A-Za-z0-9]{1,16}$. No hyphens, underscores, spaces or punctuation. Case IS significant. Docs do not state the length of codes OKX actually issues, only the field cap of 16.

Sibling field `clOrdId`: 'A combination of case-sensitive alphanumerics, all numbers, or all letters of up to 32 characters.' Same charset rule — no hyphens, so a raw UUID will be REJECTED; strip the dashes.

LEGACY PATH: brokers onboarded before 2022 were attributed via `clOrdId` (broker code as a prefix of the client order id). Brokers onboarded from 2022 onward use `tag`. OKX still reads both for attribution/statistics, but for a new integration use `tag` only, and keep `clOrdId` free for your own idempotency key.

OKX does not reject an unknown/arbitrary `tag` — it is a free-form field. If the string is not a registered broker code it is simply an untracked label. So your config can accept any user-supplied 16-char alnum string and pass it through unvalidated beyond the regex.

### example

REST:
POST https://openapi.okx.com/api/v5/trade/order
Headers: OK-ACCESS-KEY / OK-ACCESS-SIGN / OK-ACCESS-TIMESTAMP / OK-ACCESS-PASSPHRASE / Content-Type: application/json
Body (this exact string is what you sign):
{"instId":"BTC-USDT-SWAP","tdMode":"cross","side":"buy","posSide":"net","ordType":"limit","px":"111234.4","sz":"1.5","clOrdId":"arb1a2b3c4d5e6f7","tag":"YOURCODE01","reduceOnly":false}

WebSocket (/ws/v5/private, after login) — tag goes in the same place:
{"id":"1","op":"order","args":[{"instId":"BTC-USDT-SWAP","tdMode":"cross","side":"sell","ordType":"fok","sz":"1.5","tag":"YOURCODE01"}]}

Batch (tag repeats per element, it is NOT hoisted to the top level):
POST /api/v5/trade/batch-orders
[{"instId":"BTC-USDT-SWAP","tdMode":"cross","side":"buy","ordType":"limit","px":"111234.4","sz":"1","tag":"YOURCODE01"},
 {"instId":"ETH-USDT-SWAP","tdMode":"cross","side":"sell","ordType":"limit","px":"4210.5","sz":"2","tag":"YOURCODE01"}]

Suggested config surface: okx.broker_tag = "" (optional, validate ^[A-Za-z0-9]{1,16}$, omit the key entirely when blank rather than sending "").

### howToGet

Two different programs — do not confuse them:

1) BROKER PROGRAM (this is what gives you a `tag` code). Apply at https://www.okx.com/broker/home -> 'Become a broker' (also reachable from okx.com top nav: More > Broker). Fill the application form. OKX states review completes in about 2 days; on approval an account manager contacts you and the unique broker code appears in your Broker Dashboard. Eligibility is business-shaped, not retail: 'if your business platform offers cryptocurrency services' — integrated trading platforms, trading bots / bot providers, copy-trading platforms, quantitative strategy institutions, asset management platforms. Docs list no minimum volume or capital threshold.
   Sub-types:
     - DMA / non-disclosed (API) broker: the user creates their own OKX API key and gives it to your software; your software stamps `tag` on each order. THIS IS YOUR TOOL'S MODEL.
     - Fully-disclosed (FD) broker: either API-FD or OAuth-FD (you implement OAuth2, one-click authorization mints the API key without the user pasting secrets). Marketing material cites 'up to 50% commission' for FD.
   Commission = user's traded volume via API x the ratio for your broker level; levels are reviewed monthly and can be upgraded on performance. Rebates settle in USDT to the bound account on an hourly (T+1 hour) basis.
   Docs: https://www.okx.com/docs-v5/broker_en/ and https://www.okx.com/help/introduction-of-rules-on-okx-brokers

2) AFFILIATE / referral program (https://www.okx.com/join or the affiliate portal) — this is a SIGNUP-time invite code bound to the user's account, NOT a per-order field. Requires a verified KYC'd OKX account and portal approval; approval is discretionary based on audience/reach. It does NOT flow through `tag`.

Both can pay out simultaneously: per OKX's broker rules, 'Rebates will go separately to both affiliate and broker depending [on] affiliate's level and broker's level.' So a user who signed up under an affiliate can still generate broker commission for a tag-stamping tool.

Practical guidance for your tool: an ordinary retail user generally CANNOT self-issue a broker code (the program is for platforms/institutions), so ship your own code as the default and expose `okx.broker_tag` as an override for users who do have one.

### canBeEmpty

Yes — `tag` is optional. Omitting it has ZERO effect on execution: the order is accepted, matched, and filled identically, and fees are unchanged. The only loss is attribution: no broker commission accrues and the order does not appear in Broker Dashboard volume/statistics. OKX's wording is that the broker enjoys 'the corresponding commission reward, data statistics and other specific logic tracking' only when the tag is included.

There is no penalty, no rejection, and no rate-limit difference for an absent or unrecognized tag. Prefer omitting the key entirely over sending "" (empty string) — an empty string is within spec but pointless, and some SDKs will happily forward it.

Caveat: if the account was ALREADY onboarded under a different broker's fully-disclosed/OAuth relationship, attribution is governed by that binding, not by your tag.

## 认证签名

HMAC-SHA256 + Base64, 4 headers.

REQUIRED HEADERS (REST):
  OK-ACCESS-KEY:        <apiKey>
  OK-ACCESS-SIGN:       base64(HMAC_SHA256(secretKey, prehash))
  OK-ACCESS-TIMESTAMP:  ISO8601 UTC with milliseconds + 'Z', e.g. "2020-12-08T09:08:57.715Z"
  OK-ACCESS-PASSPHRASE: <passphrase chosen at key creation>
  Content-Type:         application/json      (required on every POST)
  x-simulated-trading: 1                      (ONLY for demo trading)

PREHASH STRING (exact):
  prehash = timestamp + method + requestPath + body
  - timestamp: the SAME string you put in OK-ACCESS-TIMESTAMP, character-for-character
  - method:    UPPERCASE, "GET" / "POST"
  - requestPath: path INCLUDING the query string, e.g.
      "/api/v5/public/funding-rate?instId=BTC-USDT-SWAP"
      "/api/v5/account/positions?instType=SWAP"
  - body: "" (empty string) for GET/DELETE; for POST the EXACT JSON byte string you send on the wire
    (serialize once, sign that string, send that string — do not re-serialize, key order/whitespace matter)
  Then: sign = base64(hmac_sha256(secret, prehash))

Python reference:
  ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond//1000:03d}Z'
  msg = ts + 'POST' + '/api/v5/trade/order' + body_str
  sign = base64.b64encode(hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()).decode()

TOLERANCE WINDOW: 30 seconds vs OKX server time. Outside it -> error code 50102 ("Timestamp request expired"). Server time: GET /api/v5/public/time (returns ms epoch) — use it to compute a persistent clock offset.

WEBSOCKET LOGIN (different!):
  {"op":"login","args":[{"apiKey":"...","passphrase":"...","timestamp":"1704876947","sign":"..."}]}
  timestamp here is UNIX SECONDS as a string (NOT ISO8601)
  sign = base64(HMAC_SHA256(secret, timestamp + "GET" + "/users/self/verify"))
  Success: {"event":"login","code":"0","msg":""}

BASE URLS: https://openapi.okx.com (now the recommended production domain; https://www.okx.com still works). Regional entities are separate and keys are NOT portable: us.okx.com (US/AU accounts), eea.okx.com (EU accounts).

Docs: https://www.okx.com/docs-v5/en/ (Overview > REST Authentication)

## REST 端点

### fundingCurrent

GET /api/v5/public/funding-rate?instId=BTC-USDT-SWAP  (public, no auth)
  instId is REQUIRED and single — I tested instType=SWAP alone and instId=A,B comma lists: BOTH return HTTP 400. There is no bulk 'all funding rates' endpoint, so a scanner must fan out one call per symbol (or use the WS funding-rate channel, which is the right answer for a live scanner).
  Live response 2026-08-01 (verbatim):
  {"code":"0","data":[{"formulaType":"withRate","fundingRate":"0.0000616430459607","fundingTime":"1785571200000","impactValue":"20000.0000000000000000","instId":"BTC-USDT-SWAP","instType":"SWAP","interestRate":"0.0001000000000000","maxFundingRate":"0.00375","method":"current_period","minFundingRate":"-0.00375","nextFundingRate":"","nextFundingTime":"1785600000000","premium":"-0.0004195044127414","prevFundingTime":"1785542400000","settFundingRate":"0.0000539822210069","settState":"settled","ts":"1785548167559"}],"msg":""}
  Notes: fundingTime is the UPCOMING settlement (it is > ts). nextFundingRate is usually "" — OKX stopped publishing a forward estimate for most symbols. Interval = nextFundingTime - fundingTime.
  NOT VERIFIED: the exact documented rate limit for this endpoint (the docs page is a single huge SPA that truncates on fetch). Public-data endpoints in this family are generally 20 requests / 2s per IP.

### fundingHistory

GET /api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=100  (public, no auth)
  Params: instId (required), limit (max 100, default 100), before (records NEWER than the given fundingTime, ms), after (records EARLIER than the given fundingTime, ms).
  Ordering: newest -> oldest.
  Fields per record: instType, instId, fundingRate, realizedRate, fundingTime, method, formulaType.
    -> use `realizedRate` for PnL attribution (the rate actually charged); `fundingRate` is the announced one. In my samples they were identical.

  HOW FAR BACK — I MEASURED IT, and it is much shorter than people assume:
    ~3 months only. On 2026-08-01 the oldest retrievable record for BTC-USDT-SWAP was fundingTime=1777564800000 (2026-04-30 16:00 UTC), i.e. ~93 days / ~279 records / 3 pages at 8h cadence.
    Probes: after=1783000000000 -> 100 records; after=1780128000000 -> 100 records (oldest 1777564800000); after=1778000000000 -> only 16 records; after=1777500000000 and anything older (1775000000000, 1700000000000, 1620000000000, 1600000000000) -> data:[] empty.
    So: for any backtest longer than 3 months you MUST warehouse this data yourself daily. Do not design around deep history.
  Pagination loop: page with after = oldest fundingTime of the previous page, stop when len(data) < limit or data == [].
  NOT VERIFIED: documented rate limit (commonly cited as 10 requests / 2s per IP).

### placeOrder

POST /api/v5/trade/order   — Rate limit: 60 requests / 2 s, keyed on (User ID + Instrument ID). Buckets for place / amend / cancel are INDEPENDENT.
  Body: instId, tdMode (cross|isolated for SWAP; cash for SPOT), side (buy|sell), ordType (market|limit|post_only|fok|ioc|optimal_limit_ioc), sz (IN CONTRACTS for SWAP), px (limit types), posSide (net|long|short), reduceOnly, clOrdId, tag, stpMode, ccy, tgtCcy (spot market orders only).
  Batch: POST /api/v5/trade/batch-orders — up to 20 orders per request, rate limit 300 orders / 2 s; results are NOT atomic, check per-element sCode/sMsg (sCode "0" = ok).
  Amend (use this instead of cancel+replace in a 500 ms convergence loop): POST /api/v5/trade/amend-order {instId, ordId|clOrdId, newSz, newPx, cxlOnFail} — own 60/2s bucket.
  Cancel: POST /api/v5/trade/cancel-order, POST /api/v5/trade/cancel-batch-orders (<=20).
  Sub-account hard ceiling: 1000 order requests / 2 s across everything -> error 50061. Generic rate-limit error: 50011.
  Ack != fill. The response only means OKX accepted the request; confirm terminal state (filled|canceled) on the private `orders` channel.

### positions

GET /api/v5/account/positions?instType=SWAP  (optionally &instId=BTC-USDT-SWAP, comma-separated instIds allowed here)
  Key fields: instId, posSide, pos (SIGNED size in CONTRACTS: +long / -short in net mode), availPos, avgPx, upl, uplRatio, markPx, mgnMode, lever, liqPx, imr, mmr, notionalUsd, adl, uTime.
  For delta convergence: target_delta_coins = float(pos) * ctVal * ctMult (sign-aware).
  Also useful: GET /api/v5/account/positions-history (closed positions), and the private WS `positions` channel for push-based state.

### balance

GET /api/v5/account/balance          (optional &ccy=USDT,BTC)
  Top level: totalEq, isoEq, adjEq, imr, mmr, mgnRatio, notionalUsd, uTime
  details[]: ccy, eq, cashBal, availBal, availEq, frozenBal, ordFrozen, upl, isoEq, crossLiab, mgnRatio, eqUsd, disEq (discounted equity, this is what actually backs cross margin)
  Related: GET /api/v5/account/config -> acctLv (1 Spot, 2 Spot&Futures, 3 Multi-currency margin, 4 Portfolio margin), posMode (net_mode|long_short_mode), uid, mainUid, autoLoan, ctIsoMode. Read this at startup — your sizing and posSide logic branch on it.
  Private WS `account` and `balance_and_position` channels give push updates.

### setLeverage

POST /api/v5/account/set-leverage
  Body: {"instId":"BTC-USDT-SWAP","lever":"3","mgnMode":"cross"}
  - mgnMode: cross | isolated (required)
  - For CROSS on SWAP/FUTURES you may pass instId OR ccy (ccy sets it for the whole currency's cross positions)
  - posSide (long|short) is required ONLY when mgnMode=isolated AND the account is in long_short_mode
  - Cannot be changed while it would breach margin on an open position; leverage is per (instrument, mgnMode) and persists
  Read back current leverage: GET /api/v5/account/leverage-info?instId=...&mgnMode=cross
  Max leverage available per tier: GET /api/v5/public/position-tiers

### instruments

GET /api/v5/public/instruments?instType=SWAP   (public; also SPOT, FUTURES, MARGIN, OPTION; optional &instId= or &instFamily=)
  LIVE-VERIFIED BTC-USDT-SWAP (2026-08-01), trimmed:
  {"instId":"BTC-USDT-SWAP","instType":"SWAP","instFamily":"BTC-USDT","uly":"BTC-USDT","ctType":"linear","ctVal":"0.01","ctValCcy":"BTC","ctMult":"1","settleCcy":"USDT","tickSz":"0.1","lotSz":"0.01","minSz":"0.01","maxLmtSz":"100000000","maxMktSz":"35000","maxLmtAmt":"20000000","lever":"100","state":"live","listTime":"1573557408000","instIdCode":10459,"maxPxLmtPct":"0.01","initPxLmtPct":"0.02","floatPxLmtPct":"0.005","posLmtAmt":"250000","ruleType":"normal"}
  LIVE-VERIFIED BTC-USD-SWAP (inverse): ctType="inverse", ctVal="100", ctValCcy="USD", ctMult="1", lotSz="0.1", minSz="0.1", tickSz="0.1", settleCcy="BTC"
  LIVE-VERIFIED PEPE-USDT-SWAP: ctVal="10000000", ctValCcy="PEPE", lotSz="0.1", minSz="0.1", tickSz="0.000000001", maxMktSz="16000"

  MAPPING TO YOUR ABSTRACTIONS:
    tickSize   -> tickSz          (price increment; round px to a multiple of this)
    lotSize    -> lotSz           (size increment IN CONTRACTS)
    minQty     -> minSz           (min order IN CONTRACTS)
    minNotional-> NOT PROVIDED as a field. OKX enforces a minimum via minSz in contracts, not a quote-currency floor. Compute it yourself: minSz * ctVal * ctMult * markPx. (For BTC-USDT-SWAP: 0.01 * 0.01 BTC = 0.0001 BTC ~= $11.)
    max market order size -> maxMktSz (contracts); max limit -> maxLmtSz / maxLmtAmt (quote notional).
    Filter on state == "live" and skip ruleType == "pre_market".
  Helper for contracts<->coins: GET /api/v5/public/convert-contract-coin?type=quote|open&instId=..&sz=..&px=..&unit=coin|usds
  Price bands (limit orders outside are rejected): GET /api/v5/public/price-limit?instId=... -> buyLmt, sellLmt, enabled

### feeRate

GET /api/v5/account/trade-fee?instType=SWAP  (private; optional &instId= for SPOT/MARGIN, &instFamily= or &uly= for FUTURES/SWAP/OPTION)
  Response fields: level (your fee tier, e.g. "Lv1"/"VIP2"), category (fee schedule, being deprecated), instType, ts, and:
    taker / maker   -> fee rates for CRYPTO-MARGINED (inverse) futures & perps
    takerU / makerU -> fee rates for USDT-MARGINED (linear) futures & perps   <-- this is the pair you want for BTC-USDT-SWAP
    delivery, exercise -> delivery / option-exercise fee rates
  For SPOT/MARGIN, taker/maker are the ones that apply.
  SIGN CONVENTION (observed, not stated in the change notice I read): values come back as negative strings for fees you PAY, e.g. "-0.0005" = 5 bps taker; a positive value means a maker rebate. Take abs() carefully and preserve the sign in your cost model.
  Change notice: https://www.okx.com/en-us/help/okx-will-make-changes-to-the-get-fee-rates-interface
  Related: GET /api/v5/account/max-size?instId=..&tdMode=cross -> maxBuy/maxSell in contracts (use to clamp before sending); GET /api/v5/account/max-avail-size.

## WebSocket

URLS
  Public   wss://ws.okx.com:8443/ws/v5/public     (no auth; market data, funding-rate)
  Private  wss://ws.okx.com:8443/ws/v5/private    (login required; orders, positions, account, AND order placement)
  Business wss://ws.okx.com:8443/ws/v5/business   (algo/grid/spread channels)
  Demo:    wss://wspap.okx.com:8443/ws/v5/{public|private|business}
  EEA:     wss://wseea.okx.com:8443/...   (regional entities have their own hosts)

SUBSCRIBE
  {"op":"subscribe","args":[{"channel":"bbo-tbt","instId":"BTC-USDT-SWAP"},
                            {"channel":"funding-rate","instId":"BTC-USDT-SWAP"}]}
  Ack: {"event":"subscribe","arg":{...},"connId":"..."}
  Error: {"event":"error","code":"60012","msg":"..."}

BEST BID/OFFER — use `bbo-tbt` (this is the right channel for a hedger)
  - tick-by-tick top-of-book, snapshot pushed every 10ms, nothing pushed when the book is unchanged
  - available to ALL users, no VIP requirement, no login needed
  Push:
  {"arg":{"channel":"bbo-tbt","instId":"BTC-USDT-SWAP"},
   "data":[{"asks":[["111234.5","12.3","0","5"]],
            "bids":[["111234.4","8.1","0","3"]],
            "ts":"1785548167559","seqId":123456789,"prevSeqId":123456788}]}
  Each level is [price, totalQty, nonRpiQty, orderCount] — the 3rd slot used to be a deprecated "0"; OKX repurposed it as nonRpiQty (organic-only depth) with the RPI rollout. Treat index 1 as the size you can hit.
  Continuity: chain on seqId/prevSeqId (new.prevSeqId must equal last.seqId; else resubscribe). The `checksum` field is DEPRECATED and now hard-coded to 0 — do not validate against it.
  Deeper books need VIP: books50-l2-tbt requires VIP4+, books-l2-tbt (400 levels) requires VIP5+, and BOTH require ws login before subscribing. books5 (5 levels, 100ms) and books (400 levels, 100ms snapshot+delta) are open to everyone.

FUNDING RATE PUSH
  channel "funding-rate", arg {"channel":"funding-rate","instId":"BTC-USDT-SWAP"}
  data payload mirrors the REST /public/funding-rate object (instType, instId, fundingRate, nextFundingRate, fundingTime, nextFundingTime, minFundingRate, maxFundingRate, settFundingRate, settState, method, formulaType, premium, interestRate, impactValue, ts).
  NOT VERIFIED: exact push cadence. Docs describe it as pushed on change; empirically it is on the order of tens of seconds. Do not rely on it as a heartbeat.

HEARTBEAT / RECONNECT (official rule)
  - The server drops the connection if no data has been pushed for >30s.
  - Keepalive: start a timer of N seconds (N < 30) reset on every inbound message. On timeout send the literal TEXT frame "ping" (a bare 4-char string, not JSON). Expect the text frame "pong". If no pong within N seconds, treat as dead and reconnect.
  - Protocol-level WebSocket ping frames are NOT a substitute; OKX wants the "ping" text frame.

LIMITS
  - New connections: 3 per second per IP.
  - subscribe + unsubscribe + login ops: 480 per hour PER CONNECTION. This is the real constraint — a 500ms convergence loop that churns subscriptions will blow this. Subscribe once, keep it up.
  - Total length of the args payload in one message: <= 64 KB.
  - Private channels: max 30 WebSocket connections per specific channel per sub-account (orders, account, positions, balance_and_position, liquidation-warning, account-greeks).
  - NOT VERIFIED: a documented hard cap on number of channels per connection. The binding constraints in practice are the 64KB args limit and the 480 ops/hour.

ORDER PLACEMENT OVER WS (recommended for your 500ms loop — lower overhead than REST)
  On /ws/v5/private after login:
  {"id":"req-0001","op":"order","args":[{ ...same body as REST place order, including "tag"... }]}
  `id` correlates the async response. op values: "order", "batch-orders", "cancel-order", "amend-order", "batch-amend-orders", "mass-cancel".
  These share the SAME rate-limit buckets as the REST equivalents (limits are per User ID, not per transport).
  Note (from NautilusTrader's OKX adapter): WS order submission can take the numeric `instIdCode` from /public/instruments instead of the string instId.

## 资金费周期

STANDARD: 8 hours, settling 00:00 / 08:00 / 16:00 UTC.

NON-8H EXISTS AND IS COMMON — OKX supports 8h / 4h / 2h / 1h per contract, and it changes DYNAMICALLY. Rule (https://www.okx.com/en-us/help/okx-to-enable-automatic-updates-for-funding-fee-settlement-period): when the funding rate hits its cap or floor at settlement, the frequency escalates one level (8h->4h->2h->1h); it reverts to the contract default once the rate stays within ±0.20% for 12 consecutive hours. Applies to crypto perps only — tradfi perps (XAU, XAG, NG, CL, equity/ETF perps) keep their default frequency. Historical examples of permanent 4h moves: TRB, AUCTION, GAS, MINA, ORBS (https://www.okx.com/help/okx-to-adjust-funding-rate-interval-for-certain-perpetual-futures).

HOW TO QUERY IT VIA API: there is NO dedicated "interval" field. Derive it from GET /api/v5/public/funding-rate:
    interval_ms = int(nextFundingTime) - int(fundingTime)
    (equivalently int(fundingTime) - int(prevFundingTime))

LIVE-VERIFIED 2026-08-01 (I actually called the endpoint):
  BTC-USDT-SWAP: fundingTime=1785571200000, nextFundingTime=1785600000000 -> 28800000 ms = 8h
  DOGE-USDT-SWAP: same, 8h
  TRB-USDT-SWAP: fundingTime=1785556800000, nextFundingTime=1785571200000 -> 14400000 ms = 4h  <-- confirms 4h contracts are live today

IMPORTANT FOR CARRY MATH: you must re-poll the interval per symbol (it can change without notice). Annualize as rate * (8760h / interval_hours), NOT rate*3*365.

Per-instrument caps are also NOT uniform — minFundingRate/maxFundingRate come back per symbol: BTC ±0.00375 (0.375%), DOGE ±0.0075, TRB ±0.01. Use these to bound your expected-carry model.

Also in the response: `method` = "current_period" | "next_period" (whether `fundingRate` applies to the settlement at `fundingTime` or the one after — get this wrong and you mis-time entries by one period); `settState` = "settled" | "processing"; `settFundingRate` = the rate actually being/just settled; `formulaType` = "withRate" | "noRate".

## 坑

- SIZE IS IN CONTRACTS, NOT COINS. This is the #1 way to blow up a hedger on OKX. coins = sz * ctVal * ctMult. BTC-USDT-SWAP ctVal=0.01 BTC, so sz=1 is 0.01 BTC (~$1.1k), not 1 BTC. PEPE-USDT-SWAP ctVal=10,000,000 PEPE. Inverse BTC-USD-SWAP ctVal=100 USD and settles in BTC, so its 'size' is a USD notional. Always fetch /public/instruments at startup, cache ctVal/ctMult/lotSz/tickSz/minSz per instId, and convert at the boundary. There is no per-order 'quantity in base currency' flag for SWAP.
- lotSz IS NO LONGER AN INTEGER. As of my 2026-08-01 live check, BTC-USDT-SWAP has lotSz=minSz="0.01" and PEPE-USDT-SWAP has lotSz="0.1" — OKX now allows fractional contracts. Old code that does int(contracts) will silently over/under-hedge or reject. Round with Decimal: sz = (Decimal(target) / Decimal(lotSz)).to_integral_value(ROUND_DOWN) * Decimal(lotSz), then serialize with exactly the number of decimals implied by lotSz. Same for px against tickSz (ROUND_DOWN for buys, ROUND_UP for sells). Never use float formatting — 1e-09 in a JSON body for PEPE's tickSz will be rejected; send "0.000000001".
- ALL NUMBERS ARE STRINGS, IN AND OUT. sz, px, lever, fundingRate all go in as JSON strings and come back as strings. Sending a JSON number for sz/px is an error. Empty string "" is OKX's null (e.g. nextFundingRate is often "") — parse defensively, don't float("").
- clOrdId AND tag CHARSET IS ALPHANUMERIC ONLY. clOrdId <=32 chars, tag <=16 chars, both case-sensitive, both ^[A-Za-z0-9]+$. A UUID with hyphens is REJECTED — strip the dashes (uuid4().hex is 32 chars and fits exactly). No underscores, no colons, no timestamps with dots.
- RATE LIMITS: place-order 60 req / 2s per (UserID + instId); amend 60/2s and cancel 60/2s in SEPARATE buckets; batch-orders counts EACH order (300 orders/2s, max 20 per request). Global sub-account ceiling 1000 order requests / 2s -> error 50061. Generic throttle -> error 50011. Private REST limits are per User ID (each sub-account has its own), public REST limits are per IP. REST and WebSocket order ops SHARE the same buckets — moving to WS buys you latency, not quota. A 500 ms convergence loop on N symbols must budget: 2 legs x N symbols x 2 req/s <= 60/2s per instId is fine per-symbol, but the 1000/2s sub-account cap binds at scale.
- FOK AND IOC ARE FULLY SUPPORTED on regular SWAP orders: ordType in {market, limit, post_only, fok, ioc, optimal_limit_ioc}. optimal_limit_ioc = 'market order executed as IOC at best available' and is the safest taker type for a hedge leg on thin books. EXCEPTIONS: spread-trading (sprd) orders support only LIMIT with GTC/IOC/post_only — NO FOK. Options reject market orders outright.
- TIMESTAMP FORMAT MISMATCH BETWEEN REST AND WS. REST wants ISO8601 UTC with milliseconds and a trailing Z ("2020-12-08T09:08:57.715Z"); WS login wants Unix SECONDS as a string. Getting these crossed is the most common 60006/50102 cause. Tolerance is 30 s — sync against GET /api/v5/public/time and carry a persistent offset; a Windows box with drifting NTP will fail intermittently.
- SIGNING THE BODY: you must sign the EXACT serialized JSON string you transmit. json.dumps() twice with different separators/key order produces a different signature. Serialize once into a variable, sign that variable, POST that variable. Also, requestPath in the prehash MUST include the query string for GETs.
- POSITION MODE CHANGES THE ORDER SCHEMA. GET /api/v5/account/config -> posMode. In net_mode: posSide is "net" (or omitted) and reduceOnly works. In long_short_mode: posSide (long|short) is REQUIRED on every SWAP order and reduceOnly is not the mechanism — you close by sending the opposite side with the matching posSide. For a two-leg delta-neutral tool, force net_mode (POST /api/v5/account/set-position-mode) — it makes convergence arithmetic a single signed number. Note: position mode cannot be changed while positions or pending orders exist.
- ACCOUNT LEVEL MATTERS. acctLv 1 (Spot only) cannot trade SWAP at all; 2 = Spot&Futures (per-currency margin); 3 = Multi-currency margin; 4 = Portfolio margin. For USDT-collateralized cross-margin funding carry you want 3 or 4. tdMode must be 'cross' or 'isolated' for SWAP and 'cash' for SPOT — passing 'cash' to a swap is a hard reject.
- SELF-TRADE PREVENTION IS ON BY DEFAULT with stpMode='cancel_maker', enforced at the MASTER account level across all sub-accounts. If your tool runs several strategies on one OKX account, one strategy's taker can silently cancel another's resting maker order. Set stpMode explicitly per order (cancel_maker|cancel_taker|cancel_both) or isolate strategies into separate sub-accounts.
- SYMBOL NAMING: spot 'BTC-USDT'; linear perp 'BTC-USDT-SWAP' (settleCcy=USDT, ctValCcy=BTC, ctType=linear); inverse perp 'BTC-USD-SWAP' (settleCcy=BTC, ctValCcy=USD, ctType=inverse); dated futures 'BTC-USDT-260327'; options 'BTC-USD-251226-100000-C'. instFamily/uly = 'BTC-USDT'. Spot<->perp mapping: perp_instId = spot_instId + '-SWAP', but ALWAYS confirm via instFamily and ctType rather than string-building. UNLIKE BINANCE, OKX DOES NOT USE 1000X SYMBOL PREFIXES — there is no 1000PEPE-USDT-SWAP; the multiplier lives in ctVal (10,000,000 PEPE per contract). Cross-venue symbol normalization must divide/multiply by ctVal, not parse the ticker.
- ORDER BOOK CHECKSUM IS DEAD. OKX fixed the `checksum` field on order-book channels to 0. Validate continuity with seqId / prevSeqId chaining instead (new.prevSeqId == last.seqId), and resubscribe on a gap.
- WS SUBSCRIPTION CHURN WILL GET YOU BANNED FROM THE CONNECTION: 480 subscribe/unsubscribe/login ops per hour PER CONNECTION. A loop that re-subscribes on every reconnect or rotates symbols every few minutes hits this fast. Subscribe in one batch (args payload <=64KB), keep the socket alive with the literal text-frame 'ping'/'pong' on a <30 s timer, and back off exponentially on reconnect.
- REGIONAL FRAGMENTATION: OKX global (openapi.okx.com / ws.okx.com), OKX US (us.okx.com), OKX EEA (eea.okx.com / wseea.okx.com). API keys work ONLY on the region where the account is registered, and instrument lists differ. Make the base URL and WS host configurable, don't hardcode www.okx.com.
- FUNDING HISTORY IS ONLY ~3 MONTHS DEEP (measured, see fundingHistory). Anything longer must be warehoused locally. Start the daily snapshot job on day one of the project.
