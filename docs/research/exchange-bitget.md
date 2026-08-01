# Bitget (USDT-M perpetual futures, "classic"/v2 API). REST base: https://api.bitget.com — docs at https://www.bitget.com/api-doc/classic/intro (note: /api-doc/contract/... redirects to /api-doc/classic/contract/...). All paths below were verified against the live public API on 2026-08-01, not just read from docs.

## 返佣码机制

### mechanism

HTTP request HEADER named exactly `X-CHANNEL-API-CODE`. Not a body field, not a clientOid prefix.

Verbatim from the official Place Order doc source (www.bitget.com/api-doc/classic/contract/trade/Place-Order): "API Broker rebate identifier: The following code block needs to be added to the HTTP Header of the request." followed by the block `X-CHANNEL-API-CODE: your-channel-api-code`.

WHERE IT APPLIES: the doc block appears on the order-placement endpoints — POST /api/v2/mix/order/place-order, POST /api/v2/mix/order/batch-place-order, the TP/SL plan-order endpoints, spot place-order — and the WebSocket private Place-Order channel also supports carrying the broker API code. Attribution is per-request: the header must be on the order request itself, not set once at login.

PRACTICAL IMPLEMENTATION: just attach it to every signed request. CCXT does exactly this — see ccxt/ts/src/bitget.ts sign() line 11330, where 'X-CHANNEL-API-CODE': broker sits unconditionally in the headers dict alongside ACCESS-KEY/ACCESS-SIGN/ACCESS-TIMESTAMP/ACCESS-PASSPHRASE for ALL private endpoints. It is ignored on non-order endpoints and costs nothing.

CRITICAL: the header is NOT part of the signature. The prestring is timestamp+method+requestPath+queryString+body only. Adding, changing, or removing the broker code never invalidates the signature — so it is safe to make it a user-editable config value.

FORMAT RESTRICTIONS: I could NOT find any documented length or charset constraint — Bitget's docs give only the placeholder 'your-channel-api-code' and state no rules. Do not invent a validator. Observed real-world codes are short lowercase alphanumeric (CCXT's own default is the 5-char 'p4sve', bitget.ts line 1491). Treat it as an opaque ASCII string, trim whitespace, and omit the header entirely when the user's config value is empty rather than sending an empty string.

CLIENT-OID RED HERRING: Bitget's own signature example in the Rest API intro uses `"clientOid":"channel#123456"`, which looks like a broker-tagging convention. It is NOT — it is only a sample value, and no Bitget doc states that a clientOid prefix carries broker attribution. Do not build attribution on clientOid. (It does confirm '#' is a legal clientOid character.)

### example

Python (requests) — header added to an otherwise ordinary signed order:

import time, hmac, hashlib, base64, json, requests

API_KEY, SECRET, PASSPHRASE = "...", "...", "..."
BROKER_CODE = "your-channel-api-code"   # user-supplied; may be empty

def signed_post(path, payload):
    ts   = str(int(time.time() * 1000))
    body = json.dumps(payload, separators=(',', ':'))   # sign EXACTLY what you send
    pre  = ts + "POST" + path + body
    sign = base64.b64encode(
        hmac.new(SECRET.encode(), pre.encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "ACCESS-KEY":        API_KEY,
        "ACCESS-SIGN":       sign,
        "ACCESS-TIMESTAMP":  ts,
        "ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type":      "application/json",
        "locale":            "en-US",
    }
    if BROKER_CODE:                       # omit entirely when unset
        headers["X-CHANNEL-API-CODE"] = BROKER_CODE
    return requests.post("https://api.bitget.com" + path,
                         headers=headers, data=body, timeout=5).json()

signed_post("/api/v2/mix/order/place-order", {
    "symbol":     "BTCUSDT",
    "productType":"USDT-FUTURES",
    "marginMode": "crossed",
    "marginCoin": "USDT",
    "size":       "0.01",
    "price":      "62900.0",
    "side":       "buy",
    "orderType":  "limit",
    "force":      "gtc",
    "clientOid":  "leg1-1785548132637",
})

CCXT equivalent (overrides CCXT's own default 'p4sve'):
    ex = ccxt.bitget({'apiKey':..., 'secret':..., 'password':...})
    ex.options['broker'] = 'your-channel-api-code'

### howToGet

Via the Bitget Broker Program, NOT the ordinary retail affiliate flow. Steps per https://www.bitget.com/support/articles/12560603831454 : (1) register at the Bitget Partner site WITHOUT entering any referral code, (2) coordinate backend configuration with a BD rep and technical support, (3) integrate the broker ID into your platform's API calls. "Once your broker status is approved, Bitget will generate the broker's API channel code on the same day."

ELIGIBILITY IS B2B, NOT INDIVIDUAL. Stated eligible categories: crypto exchanges, trading terminals, asset management firms, social trading platforms, TRADING BOT PROVIDERS, and wallets — "If your business falls within these categories and can integrate with Bitget via API, you are eligible." A funding-arb tool distributed to users plausibly qualifies as a trading bot provider; a solo trader running it on their own account does not, and should use the ordinary affiliate/referral program instead (open to anyone with >100 followers or a >500-member community, per the affiliates FAQ) — but note that referral code is a signup-link mechanism and does NOT flow through this header.

Rebate tiers run Level 0 (new brokers, up to 25%) to Level 5 (>=$40M monthly volume, up to 50% spot). Rebates settle daily by 21:00 UTC+8 on T+1. Brokers can then query attribution via GET /api/v2/broker/rebate-info?uid=<uid> (20/sec/UID, 30-day retention), which returns affiliationType (affiliate|official), userLevel, clientSpotRebateRatio, clientFuturesRebateRatio.

Because approval is gated and slow, make the code a plain config string with a documented default of empty — never block the tool from trading on its absence.

### canBeEmpty

Yes — completely safe. Omitting the header (or leaving the config blank) has NO effect on order acceptance: the order is placed and fills identically, you simply forfeit broker rebate attribution for that order. The header is not signed, not validated as a required field, and not listed among the required request parameters for place-order.

Evidence: CCXT ships bitget with options['broker'] defaulting to its own code and, when a user clears it, passes an undefined/absent value — orders execute normally either way. The place-order docs list only symbol, productType, marginMode, marginCoin, size, side, orderType as required; the broker header sits in a separate advisory note.

Implementation guidance: send the header only when the user's value is non-empty. Sending an empty-string header is untested and pointless — Python's requests will transmit `X-CHANNEL-API-CODE:` with a blank value, so branch on truthiness as shown in the example.

An invalid/unrecognized code: I found NO documentation of an error code for a bad broker code, and could not test it without API keys. The related documented error is 30017 "apikey's broker id does not match", which concerns broker-managed SUBACCOUNT api keys (the /api/v2/broker/* subaccount tree), not this rebate header. Expected behavior for an unknown code on a normal account is silent no-attribution rather than rejection, but treat that as unverified and have the caller confirm rebates are landing via /api/v2/broker/rebate-info before assuming the code is live.

## 认证签名

HMAC-SHA256 + Base64, in headers. Verbatim from https://www.bitget.com/api-doc/classic/quickStart/intro

HEADERS (all private requests):
  ACCESS-KEY: <apiKey>
  ACCESS-SIGN: <base64 signature>
  ACCESS-TIMESTAMP: <ms since epoch, e.g. 1659076670000>
  ACCESS-PASSPHRASE: <passphrase set at key creation>
  Content-Type: application/json   (required on ALL POST)
  locale: en-US                    (optional; zh-CN also accepted)
  X-CHANNEL-API-CODE: <broker code> (optional, see brokerCode)

PRESTRING (+ = string concat):
  if queryString empty:     timestamp + method.toUpperCase() + requestPath + body
  if queryString non-empty: timestamp + method.toUpperCase() + requestPath + "?" + queryString + body
  body omitted entirely for GET.

Then: sign = base64( hmac_sha256(secretKey, prestring) )

OFFICIAL EXAMPLES (verbatim from docs):
  GET:  16273667805456GET/api/mix/v2/market/depth?limit=20&symbol=BTCUSDT
  POST: 16273667805456POST/api/v2/mix/order/place-order{"productType":"usdt-futures","symbol":"BTCUSDT","size":"8","marginMode":"crossed","side":"buy","orderType":"limit","clientOid":"channel#123456"}

Note the POST prestring appends the EXACT serialized JSON body byte-for-byte — sign the same string you transmit (no re-serialization, no whitespace differences).

TWO SIGNING GOTCHAS FOR GET (these bite hard):
1. Query params must be sorted ascending alphabetically by key. The docs' own RSA sample carries the inline comment: "Need to be sorted in ascending alphabetical order by key" (params = 'clientOid=123&coin=USDT&endTime=...&pageNo=1&pageSize=20&startTime=...').
2. Bitget signs the RAW (non-percent-encoded) query string, while the URL carries the percent-encoded form. CCXT handles this explicitly — see ccxt/ts/src/bitget.ts sign() lines 11316-11321, comment: "bitget signs the raw (non-percent-encoded) query string, so the signature must use the decoded values". Irrelevant for pure-ASCII symbols like BTCUSDT, but will break on any param with reserved chars. Simplest safe rule: build the sorted raw query string once, use it for BOTH signature and URL.

TIMESTAMP FORMAT/WINDOW: milliseconds since epoch, integer as string. "The timestamp of the request must be within 30 seconds of the API server time, otherwise the request will be considered expired and rejected." Sync via GET /api/v2/public/time.

RSA (SHA256WithRSA) is also supported as an alternative to HMAC — same prestring, sign with RSA privateKey then base64.

WEBSOCKET LOGIN (different prestring — requestPath is a fixed literal):
  sign = base64( hmac_sha256(secretKey, timestamp + "GET" + "/user/verify") )
  {"op":"login","args":[{"apiKey":"...","passphrase":"...","timestamp":"...","sign":"..."}]}
  Also expires in 30 seconds. Failed login auto-disconnects.

## REST 端点

### fundingCurrent

GET /api/v2/mix/market/current-fund-rate  — 20 times/1s (IP), public/unsigned.
Params: productType (required, USDT-FUTURES|COIN-FUTURES|USDC-FUTURES; lowercase 'usdt-futures' also accepted — verified), symbol (OPTIONAL).
Omitting symbol returns ALL symbols in one call — do this, it is one request for the whole book.
VERIFIED LIVE: {"code":"00000","data":[{"symbol":"BTCUSDT","fundingRate":"0.000054","fundingRateInterval":"8","nextUpdate":"1785571200000","minFundingRate":"-0.003","maxFundingRate":"0.003"}]}
fundingRate is a decimal fraction per interval (0.000054 = 0.0054%). nextUpdate is ms epoch. min/maxFundingRate are the clamp bounds.
Companion: GET /api/v2/mix/market/funding-time?symbol=&productType= -> {"nextFundingTime":"1785571200000","ratePeriod":"8"} (per-symbol only).
Docs: https://www.bitget.com/api-doc/classic/contract/market/Get-Current-Funding-Rate

### fundingHistory

GET /api/v2/mix/market/history-fund-rate  — 20 times/1s (IP), public/unsigned.
Params: symbol (REQUIRED — no productType-wide query), productType (REQUIRED), pageSize (default 20, MAX 100 per docs), pageNo (1-based).
Response rows: {"symbol","fundingRate","fundingTime"} — fundingTime is ms epoch of settlement, newest first.

HOW FAR BACK — MEASURED, NOT DOCUMENTED (the docs state no retention limit; I probed it live for BTCUSDT):
  pageNo=1 -> 100 rows (oldest 2026-06-29)
  pageNo=2 -> 100 rows (oldest 2026-05-26)
  pageNo=3 ->  70 rows (oldest 2026-05-03)
  pageNo=4+ -> 0 rows
Hard cap = 270 records total. At 8h that is exactly 2160h = 90 DAYS. Expect the same 90-day window on 4h/1h symbols (which means only ~135 / ~34 records... i.e. FEWER calendar days are NOT the constraint — 270 records at 4h = 45 days, at 1h = 11 days; verify per symbol before backtesting, since the cap appears to be record-count-based).
Also confirmed: pageSize=101 and pageSize=200 both silently clamp to 100 (no error). There is NO startTime/endTime filter — pagination by pageNo is the only way back.
Budget: full 90-day history for all 733 USDT symbols = 733 x 3 = ~2199 requests; at 20/s that is ~110s. Cache aggressively.
Docs: https://www.bitget.com/api-doc/classic/contract/market/Get-History-Funding-Rate

### placeOrder

POST /api/v2/mix/order/place-order  — Rate limit: 10 requests/second/UID (1/s for copy-trade elite traders).
Body (JSON), required unless noted:
  symbol       String  e.g. "BTCUSDT"
  productType  String  USDT-FUTURES | COIN-FUTURES | USDC-FUTURES
  marginMode   String  "isolated" | "crossed"
  marginCoin   String  CAPITALIZED, e.g. "USDT"
  size         String  base-coin amount; decimals per volumePlace
  price        String  optional; REQUIRED when orderType=limit
  side         String  "buy" | "sell"   <-- SEE HEDGE-MODE TRAP IN QUIRKS
  tradeSide    String  optional; ONLY in hedge-mode: "open" | "close". Ignored in one-way-mode.
  orderType    String  "limit" | "market"
  force        String  optional; required when orderType=limit. "gtc"(DEFAULT) | "ioc" | "fok" | "post_only" — all LOWERCASE
  clientOid    String  optional; your idempotency key. '#' is legal.
  reduceOnly   String  optional; "YES" | "NO" (default "NO"). Applicable ONLY in one-way-position mode.
  stpMode      String  optional; "none"(default) | "cancel_taker" | "cancel_maker" | "cancel_both"
  presetStopSurplusPrice / presetStopLossPrice / presetStopSurplusExecutePrice / presetStopLossExecutePrice — optional TP/SL
Response data: {"orderId","clientOid"}.
Batch: POST /api/v2/mix/order/batch-place-order (same broker header supported) — use for the two legs when both are on Bitget.
Docs: https://www.bitget.com/api-doc/classic/contract/trade/Place-Order

### positions

GET /api/v2/mix/position/all-position  — Rate limit: 5 requests/sec/UID.
Params: productType (required), marginCoin (optional, capitalized).
Key response fields for a delta-neutral engine:
  symbol, marginCoin, holdSide ("long"|"short"), total (available+locked, base coin), available, locked,
  openDelegateSize (unfilled amount of working orders — subtract this when computing true target delta),
  openPriceAvg, markPrice, leverage, marginMode ("isolated"|"crossed"),
  posMode ("one_way_mode"|"hedge_mode") — READ THIS, it decides your order construction,
  unrealizedPL, liquidationPrice, marginRatio, keepMarginRate, breakEvenPrice,
  totalFee (accumulated FUNDING fee over the life of the position — your realized carry, initially empty string),
  deductedFee (accumulated trading fees), cTime, uTime.
NOTE: all-position returns NO zero-size rows, so a closed leg simply disappears — treat absence as flat, not as an error.
Single symbol: GET /api/v2/mix/position/single-position (10/s/UID) — cheaper per-symbol poll (cost 2 vs 4).
Docs: https://www.bitget.com/api-doc/classic/contract/position/get-all-position

### balance

GET /api/v2/mix/account/accounts  — Frequency limit: 10 times/1s (UID).
Params: productType (required). Returns one row per margin coin.
Fields: marginCoin, available, locked, crossedMaxAvailable, isolatedMaxAvailable, maxTransferOut,
        accountEquity (includes unrealized PnL at mark price), usdtEquity, btcEquity, crossedRiskRate,
        unrealizedPL, coupon, assetMode ("union"|...),
        unionTotalMargin / unionAvailable / unionMm + assetList[] (multi-asset mode only).
Use `usdtEquity` as the single scalar for sizing; use `crossedRiskRate` as your margin-health kill-switch input.
Single-account variant: GET /api/v2/mix/account/account?symbol=&productType=&marginCoin= (10/s).
Docs: https://www.bitget.com/api-doc/classic/contract/account/Get-Account-List

### setLeverage

POST /api/v2/mix/account/set-leverage  — Frequency limit: 5 times/1s (uid).
Body: symbol (req), productType (req), marginCoin (req, CAPITALIZED), and then:
  leverage       — use THIS for cross margin; also for one-way isolated, and for hedge-isolated when both sides share a ratio
  longLeverage / shortLeverage — ONLY for hedge-mode isolated with asymmetric ratios
  holdSide       — "long"|"short"; NOT needed for cross; NOT needed for one-way isolated; REQUIRED for hedge-mode isolated (unless you set long+short simultaneously)
Doc warning verbatim: "When adjusting leverage in cross margin mode, please use the leverage parameter instead of longLeverage or shortLeverage. Currently, there is no mandatory validation ... If these two parameters are passed in cross margin mode, they will still take effect, with longLeverage taking priority." — i.e. passing longLeverage in cross mode silently misconfigures you. Send ONLY `leverage` for cross.
Response: {symbol, marginCoin, longLeverage, shortLeverage, crossMarginLeverage, marginMode}.
Bounds come from contracts.minLever / contracts.maxLever (BTCUSDT 1-150, DOGEUSDT 1-75).
Related one-time setup: POST /api/v2/mix/account/set-margin-mode, POST /api/v2/mix/account/set-position-mode (both 5/s).
Docs: https://www.bitget.com/api-doc/classic/contract/account/Change-Leverage

### instruments

GET /api/v2/mix/market/contracts  — 20 times/1s (IP), public. Params: productType (required), symbol (optional). One call returns all 733 USDT symbols.

TICKSIZE / LOTSIZE / MINNOTIONAL ARE NOT DIRECT FIELDS — YOU MUST DERIVE THEM:
  tickSize    = priceEndStep * 10^(-pricePlace)
  priceDecimals = pricePlace
  lotSize/step  = sizeMultiplier      (quantity must be an integer multiple of this)
  qtyDecimals   = volumePlace
  minQty        = minTradeNum
  minNotional   = minTradeUSDT        (5 USDT across the board in the live sample)

VERIFIED LIVE VALUES:
  BTCUSDT      pricePlace=1 priceEndStep=1 -> tick 0.1   | volumePlace=4 sizeMultiplier=0.0001 minTradeNum=0.0001 minTradeUSDT=5 maxLever=150
  ETHUSDT      pricePlace=2 priceEndStep=1 -> tick 0.01  | volumePlace=2 sizeMultiplier=0.01   minTradeNum=0.01   minTradeUSDT=5 maxLever=150
  SOLUSDT      pricePlace=3 priceEndStep=1 -> tick 0.001 | volumePlace=1 sizeMultiplier=0.1    minTradeNum=0.1    minTradeUSDT=5 maxLever=100
  DOGEUSDT     pricePlace=5 priceEndStep=1 -> tick 1e-05 | volumePlace=0 sizeMultiplier=1      minTradeNum=1      minTradeUSDT=5 maxLever=75
priceEndStep is NOT always 1 — do not hardcode tick = 10^-pricePlace.

OTHER FIELDS YOU WANT: fundInterval ("8"/"4"/"1"), makerFeeRate/takerFeeRate (BASE tier, not your tier), buyLimitPriceRatio/sellLimitPriceRatio (price band vs mark, 0.05 = +/-5% for BTC — limit orders outside are rejected), maxMarketOrderQty, maxOrderQty, maxSymbolOrderNum (200 open orders/symbol), maxPositionNum, posLimit, minLever/maxLever, symbolStatus ("normal" — filter on this), symbolType ("perpetual"), offTime/limitOpenTime ("-1" when live), supportMarginCoins[], deliveryTime.
Docs: https://www.bitget.com/api-doc/classic/contract/market/Get-All-Symbols-Contracts

### feeRate

GET /api/v2/common/trade-rate  — Frequency limit: 10 times/1s (UID). SIGNED (returns YOUR VIP tier, unlike the contracts endpoint).
Params: symbol (required, e.g. BTCUSDT), businessType (required: "mix" for futures | "spot" | "margin").
Response: {"makerFeeRate":"0.0002","takerFeeRate":"0.0006"} — decimal fractions.
Per-symbol only, so cache it; the tier is account-wide, so one probe symbol (BTCUSDT) is enough in practice unless you trade promo pairs.
All-symbols variant: GET /api/v2/common/all-trade-rate.
Broker-rebate visibility: GET /api/v2/broker/rebate-info?uid=<uid> (20/sec/UID, 30-day retention) -> affiliationType, userLevel (VIP0..VIP7, PRO1..PRO6), clientSpotRebateRatio, clientFuturesRebateRatio.
IMPORTANT for arb math: contracts.makerFeeRate/takerFeeRate are the DEFAULT tier and will overstate your cost if you are VIP — always prefer trade-rate for edge calculations.
Docs: https://www.bitget.com/api-doc/classic/common/public/Get-Trade-Rate

## WebSocket

ENDPOINTS (from https://www.bitget.com/api-doc/classic/quickStart/websocket-intro):
  Public:  wss://ws.bitget.com/v2/ws/public
  Private: wss://ws.bitget.com/v2/ws/private

SUBSCRIBE (all verified live against the real socket):
  {"op":"subscribe","args":[{"instType":"USDT-FUTURES","channel":"books1","instId":"BTCUSDT"}]}
  instType: USDT-FUTURES | COIN-FUTURES | USDC-FUTURES (SPOT for spot). op: "subscribe" | "unsubscribe" | "login".
  Ack: {"event":"subscribe","arg":{...}} — one ack per arg. Errors: {"event":"error","code":...,"msg":...}

BEST BID/ASK — USE books1 (this is the right channel for a 500ms convergence loop):
  books1  = top of book, pushed every 10ms, full snapshot each time
  books5/books15 = 5/15 levels, 150ms, snapshot each time
  books   = full depth, 150ms, first msg snapshot then incremental updates
Live books1 payload:
  {"action":"snapshot","arg":{"instType":"USDT-FUTURES","channel":"books1","instId":"BTCUSDT"},
   "data":[{"asks":[["62914","2.8221"]],"bids":[["62913.9","6.9558"]],"ts":"1785548251835","seq":793680826410,"pseq":0}],"ts":1785548251836}
  asks/bids are [price, size] string pairs. `seq` increments monotonically — use it to detect out-of-order/dropped packets. `pseq` is always 0 for books1/5/15 (non-zero only for incremental `books`).

FUNDING RATE OVER WS — THERE IS NO DEDICATED FUNDING CHANNEL. The only public futures channels are ticker, candle*, trade, books*. Funding rides on `ticker`:
  {"instId":"BTCUSDT","lastPr":"62914","bidPr":"62913.9","askPr":"62914","bidSz":"6.9558","askSz":"2.8237",
   "fundingRate":"0.000054","nextFundingTime":"1785571200000","markPrice":"62914","indexPrice":"62938.991",
   "holdingAmount":"...","open24h":..,"high24h":..,"low24h":..,"baseVolume":..,"quoteVolume":..,"symbolType":"1","ts":"..."}
  ticker pushes on change at 300-400ms. It carries bidPr/askPr/bidSz/askSz too — so ONE ticker subscription per symbol gives you top-of-book AND live funding AND markPrice, which is usually the better trade than ticker+books1 (halves your channel count). Use books1 only where you need 10ms freshness.
  Note ticker does NOT carry fundingRateInterval — get that once from REST /contracts.fundInterval.

HEARTBEAT (verbatim from docs):
  "Users set a 30 seconds timer to a send string 'ping', and expect a string 'pong' as response. If no string 'pong' received, please reconnect"
  "Websocket server will disconnect the connection if there is no string 'ping' received for 2 min"
  It is a RAW TEXT FRAME "ping" / "pong" — NOT JSON, not a websocket protocol ping. Your reader loop must special-case the literal string before json.loads(), or it will throw.

LIMITS:
  Connections:   300 connection requests / IP / 5 min; MAX 100 concurrent connections / IP
  Subscriptions: 240 subscribe requests / hour / connection; MAX 1000 channels / connection
  Messages:      max 10 messages/sec per connection (a "ping" counts as a message). Exceed it and you are disconnected; repeat offenders get IP-blocked.
  Payload:       args in a single subscribe message must total <= 4096 bytes
  Docs recommend "less than 50 channels in one connection" for stability, despite the 1000 cap.

BATCHING — VERIFIED: I subscribed 60 args in a SINGLE message and received 60 acks, no error. This matters because the 240-subscribes/hour/connection budget is per REQUEST, not per channel: batching ~50 args per message lets you cover the whole board in a handful of requests. With 733 symbols and the <=50-channels-per-connection guidance, plan ~15-20 connections (well under the 100/IP cap) and reconnect with jitter.

PRIVATE CHANNELS (after op:"login", see auth): positions, account, orders, fill, place-order (the private Place-Order channel also accepts the broker API code). Use instId:"default" to receive all symbols on private topics. Prefer the `positions` push over polling all-position — it removes the 5/s REST ceiling from your 500ms convergence loop.

## 资金费周期

NOT uniformly 8h — this is the single biggest modeling trap on Bitget, and it is worse here than on most venues.

LIVE CENSUS (pulled 2026-08-01 from GET /api/v2/mix/market/contracts?productType=usdt-futures, 733 symbols):
  8h: 354 symbols (BTCUSDT, ETHUSDT, XRPUSDT, BCHUSDT, LTCUSDT, ADAUSDT, ETCUSDT, LINKUSDT...)
  4h: 375 symbols (XTZUSDT, AXSUSDT, ALICEUSDT, ENJUSDT, GMTUSDT, ZILUSDT, ZRXUSDT, KAVAUSDT...)
  1h: 4 symbols   (COTIUSDT, DEXEUSDT, BANKUSDT, ERAUSDT)
So the MAJORITY of USDT perps (51%) are 4h, not 8h. coin-futures (11 symbols) and usdc-futures (49 symbols) are currently all 8h.

THREE API FIELDS GIVE YOU THE INTERVAL (all return hours as a decimal string):
1. GET /api/v2/mix/market/contracts?productType=usdt-futures  -> field `fundInterval` (e.g. "8", "4", "1").
   Best option: one call returns all 733 symbols. Cache with the rest of your specs.
2. GET /api/v2/mix/market/current-fund-rate?productType=usdt-futures -> field `fundingRateInterval`.
   Also works productType-wide with no symbol; also gives minFundingRate/maxFundingRate caps.
3. GET /api/v2/mix/market/funding-time?symbol=&productType= -> field `ratePeriod` (+ `nextFundingTime`).

Verified live response shapes:
  contracts:          "fundInterval":"8"
  current-fund-rate:  {"symbol":"BTCUSDT","fundingRate":"0.000054","fundingRateInterval":"8","nextUpdate":"1785571200000","minFundingRate":"-0.003","maxFundingRate":"0.003"}
  funding-time:       {"symbol":"BTCUSDT","nextFundingTime":"1785571200000","ratePeriod":"8"}

NORMALIZATION (mandatory before any cross-venue/cross-symbol comparison):
  annualized = fundingRate * (24 / fundInterval) * 365
A raw 0.01% on a 4h symbol is 2x the carry of 0.01% on an 8h symbol, and a 1h symbol is 8x. Comparing raw `fundingRate` across Bitget symbols — or against an 8h-only venue — is simply wrong. Bitget's minFundingRate/maxFundingRate caps are also per-interval, so the cap is tighter in annualized terms on 8h names.

Settlement clock: 8h symbols settle 00:00/08:00/16:00 UTC. Do not assume the phase for 4h/1h symbols — read `nextUpdate`/`nextFundingTime` per symbol and roll forward by fundInterval, since new listings and interval changes shift the grid.

## 坑

- HEDGE-MODE `side` MEANS POSITION DIRECTION, NOT ORDER DIRECTION. This is the #1 way to accidentally build a 2x position instead of closing one. Docs verbatim: 'Open long: side=buy, tradeSide=open; Close long: side=buy, tradeSide=close; Open short: side=sell, tradeSide=open; Close short: side=sell, tradeSide=close.' So closing a long uses side=BUY, not sell. In one-way-mode, tradeSide is ignored and side reverts to normal buy/sell semantics with reduceOnly=YES/NO. ALWAYS read posMode from /all-position and branch — do not assume. (CCXT inverts side internally for this reason; see bitget.ts ~line 5506.)
- QUANTITY PRECISION: size must be rounded DOWN to volumePlace decimals AND be an integer multiple of sizeMultiplier, then satisfy BOTH minTradeNum and minTradeUSDT (5 USDT notional). Note volumePlace and sizeMultiplier encode the same step in the live data (BTC: volumePlace=4, sizeMultiplier=0.0001) but validate against both. Use integer/Decimal arithmetic — float rounding on 1e-05 ticks silently produces rejected orders. Price must be rounded to tickSize = priceEndStep * 10^(-pricePlace); priceEndStep is NOT always 1, so tick != 10^-pricePlace in general.
- GET SIGNATURES REQUIRE ALPHABETICALLY SORTED PARAMS, and the signed query string must byte-match the URL query string. Bitget signs the RAW (non-percent-encoded) form. Build the sorted raw query once and reuse it for both the signature and the request URL, or you will get intermittent 40xx auth failures that only appear on endpoints with multiple params.
- FOK IS FULLY SUPPORTED. force enum = gtc (DEFAULT) | ioc | fok | post_only, all LOWERCASE, and force is required when orderType=limit. Beware: CCXT sends UPPERCASE 'GTC'/'FOK'/'IOC' but lowercase 'post_only' for this endpoint — if you hand-roll, stick to lowercase everywhere as the docs show. For a two-leg hedge, fok on the second leg is the clean way to avoid a half-filled delta.
- RATE LIMITS: global cap 6000 requests/IP/minute; public market-data endpoints capped at 20 req/s; per-endpoint UID limits are separate and counted independently (place-order 10/s/UID, all-position 5/s/UID, set-leverage 5/s/UID, accounts 10/s/UID, trade-rate 10/s/UID). Breach returns HTTP 429. Bitget echoes remaining quota in a Binance-style header — VERIFIED LIVE: `X-MBX-USED-REMAIN-LIMIT: 19` on a public GET. Read it for adaptive backoff. At 500ms convergence, polling all-position (5/s) is fine for one product type but will not scale across many symbols — use the private `positions` WS channel instead.
- SYMBOL NAMING DIFFERS PER PRODUCT LINE, and only USDT-M maps 1:1 to spot. usdt-futures = BTCUSDT (IDENTICAL to the spot symbol BTCUSDT — verified against /api/v2/spot/public/symbols, so no translation table needed for spot-vs-perp basis trades). coin-futures = BTCUSD. usdc-futures = BTCPERP. productType is accepted in both cases (USDT-FUTURES and usdt-futures both work — verified live) but docs use uppercase; normalize to uppercase.
- history-fund-rate HAS AN UNDOCUMENTED 270-RECORD HARD CAP (pageNo 1,2,3 then empty). That is 90 days for 8h symbols but only ~45 days for the 375 4h symbols and ~11 days for the 1h symbols. There is no startTime/endTime filter. If your backtest needs more than ~1.5 months of carry history on 4h names, Bitget cannot supply it — start persisting your own snapshots from day one.
- PRICE BANDS REJECT AGGRESSIVE LIMITS: buyLimitPriceRatio/sellLimitPriceRatio (0.05 = +/-5% for BTCUSDT) bound limit prices against mark. A convergence loop that crosses hard during a wick will get rejections, not fills. Also maxSymbolOrderNum=200 open orders per symbol and maxMarketOrderQty/maxOrderQty per-order size caps — chunk large rebalances.
- ONE-WAY-MODE REDUCE-ONLY CAN RETURN A RESPONSE WITH NO orderId. Docs verbatim: if the new reduce-only order plus existing reduce-only orders exceed position size, the system cancels existing reduce-only orders in creation order, and 'the response for the latest reduce-only order request will not include an orderId. You can use the clientOid set in the request to query order details.' So ALWAYS send a unique clientOid and be prepared to resolve state by clientOid — never key your state machine solely on orderId.
- HEDGE-MODE PARTIAL-CLOSE SEMANTICS ARE SURPRISING: with a position of 100 and a limit close order occupying 70, a market close for 50 does NOT error and does NOT cancel the limit order — it closes only 30 (the unencumbered remainder). Your 'flatten now' path can silently under-close. Reconcile against /all-position (using `total` minus `openDelegateSize`) rather than trusting the close order's requested size.
- STP IS AVAILABLE AND WORTH USING: stpMode = none (default) | cancel_taker | cancel_maker | cancel_both. If both legs of a pair ever land on Bitget, self-trade prevention stops you from crossing your own quotes during convergence.
- DEMO/PAPER TRADING EXISTS: send header `PAPTRADING: 1` and use productType SUSDT-FUTURES. VERIFIED LIVE but the universe is tiny — only 3 symbols (SBTCSUSDT, SETHSUSDT, SXRPSUSDT). Good for wiring/auth smoke tests, useless for realistic funding-arb rehearsal. (Source: ccxt bitget.ts sign(), lines 11336-11346.)
- FEE SOURCE MATTERS: contracts.makerFeeRate/takerFeeRate is the DEFAULT tier (0.0002/0.0006), not yours. Use the signed /api/v2/common/trade-rate for real edge math — on a funding carry the taker/maker spread is frequently larger than the funding differential you are harvesting.
- TIMESTAMP WINDOW IS 30 SECONDS (both REST and WS login). On a 500ms loop a drifting clock produces sporadic auth failures that look like network flakiness. Sync against GET /api/v2/public/time at startup and periodically, and keep a measured offset rather than trusting the host clock.
- WS 'ping'/'pong' ARE RAW TEXT FRAMES, not JSON and not protocol-level pings. json.loads() on them throws. Send every 30s; the server drops you after 2 minutes of silence. Also, every ping counts against the 10-messages/sec/connection budget.
