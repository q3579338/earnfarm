# Bybit (v5 Unified API)

## 返佣码机制

### mechanism

HTTP HEADER — not an orderLinkId prefix, not a body field. This is important: unlike Binance/OKX, Bybit does NOT use a client-order-id prefix. The header is `X-Referer` (alias `Referer` — both accepted, docs list them as equivalent: "X-Referer or Referer — the header for broker users only"). Value = your Broker ID. Send it on EVERY order-creating request (/v5/order/create, /v5/order/create-batch, /v5/order/amend). CRITICAL IMPLEMENTATION NOTE: the referer header is NOT part of the signature. X-BAPI-SIGN covers only timestamp+apiKey+recvWindow+payload, so you can inject/change the referer header freely without touching signing code. Confirmed in the official pybit SDK (pybit/_http_manager.py): `if self.referral_id: self.client.headers.update({"Referer": self.referral_id})` — set once session-wide, signature built separately in _prepare_headers(). FOR WEBSOCKET TRADE (wss://stream.bybit.com/v5/trade) it is NOT an HTTP header — it goes inside the JSON envelope as `header.Referer`, alongside X-BAPI-TIMESTAMP and X-BAPI-RECV-WINDOW. Docs: "The referer identifier for API broker user". Docs: https://bybit-exchange.github.io/docs/v5/guide , https://bybit-exchange.github.io/docs/v5/order/create-order , https://bybit-exchange.github.io/docs/v5/websocket/trade/guideline

### example

REST (POST /v5/order/create):
  POST https://api.bybit.com/v5/order/create
  X-BAPI-API-KEY: <key>
  X-BAPI-TIMESTAMP: 1672211928338
  X-BAPI-RECV-WINDOW: 5000
  X-BAPI-SIGN: <hmac-sha256 hex>
  X-BAPI-SIGN-TYPE: 2
  Content-Type: application/json
  X-Referer: api.Abc            <-- your broker code, user-configurable, blank = omit header entirely
  body: {"category":"linear","symbol":"BTCUSDT","side":"Buy","orderType":"Limit","qty":"0.001","price":"60000","timeInForce":"IOC","orderLinkId":"arb-1a2b","positionIdx":0}

Python (requests, one place to inject):
  sess = requests.Session()
  if cfg.broker_code:
      sess.headers['X-Referer'] = cfg.broker_code   # never signed, safe to set globally

pybit:
  from pybit.unified_trading import HTTP
  s = HTTP(api_key=k, api_secret=sec, referral_id=cfg.broker_code)  # -> sets Referer header

WebSocket trade envelope (NOT an HTTP header here):
  {"reqId":"arb-1",
   "header":{"X-BAPI-TIMESTAMP":"1711437566950","X-BAPI-RECV-WINDOW":"5000","Referer":"api.Abc"},
   "op":"order.create",
   "args":[{"category":"linear","symbol":"BTCUSDT","side":"Buy","orderType":"Limit","qty":"0.001","price":"60000","timeInForce":"IOC"}]}

Doc example broker id string is "api.Abc" (the "api." prefix appears in every official example, including the legacy v3 form `&referer=api.Abc&`). I could NOT find a documented spec for max length or allowed character set of the broker id — treat the value as an opaque string from Bybit and pass through verbatim; do not validate/normalize it client-side.

### howToGet

Two DIFFERENT programs, do not confuse them:

1) API Broker Program (this is what produces the X-Referer Broker ID). Process per Bybit help center: register a Bybit account (this account receives rebates and gets the Broker dashboard) -> apply for an affiliate account -> submit the broker application form; a dedicated manager responds within ~48h to run onboarding. Bybit issues a unique Broker ID after you supply your Bybit account UID. Contact: broker_program@bybit.com. Landing page: https://www.bybit.com/en/promo/global/broker/ . FAQ: https://www.bybit.com/en/help-center/article/FAQ-API-Brokers-Program (I hit repeated fetch timeouts on the help-center article itself; the above is from Bybit search snippets + the broker landing page). This is aimed at platforms/bots/institutions that bring users, NOT at an individual trading their own account. I could NOT verify a published minimum-volume or KYB threshold.

2) Affiliate / referral code (the ordinary retail 'invite code'). Retrievable from the Bybit app/web referral page — https://www.bybit.com/en/help-center/article/How-to-obtain-your-referral-code . This is applied at signup, NOT via any API header. Passing it in X-Referer does nothing.

UX recommendation for your tool: expose one free-text field 'Bybit Broker ID (X-Referer)', default empty, with helper text that it requires the API Broker Program and is not the retail referral code. Ship your own code as the default only if you have one, and let the user overwrite it. Note the doc-stated interaction: if a user registered under an affiliate then trades through a broker program, affiliate rebate is capped at 45%. Once integrated, brokers must notify Bybit broker support after any change so the API module can be re-verified.

Unverified: whether an X-Referer on your OWN self-trades earns anything. The documented model is that Bybit aggregates trading volume BY Broker ID and pays the broker; I found no statement excluding the broker's own account, but do not promise self-rebate to users.

### canBeEmpty

Yes — fully optional. Docs mark X-Referer/Referer as "for broker users only"; every canonical curl example for /v5/order/create omits it entirely. Orders place normally without it; you just get no broker attribution and no rebate. Behavior if empty/garbage: Bybit does not reject the order — an unrecognized referer is simply not credited (there is no documented validation error for a bad broker id). IMPLEMENTATION RULE: if the user leaves the field blank, OMIT the header rather than sending `X-Referer: ""` — an empty header value is untested and pointless. Same for the WS envelope: drop the `Referer` key rather than sending an empty string.

## 认证签名

HMAC-SHA256 (default) or RSA-SHA256. Doc: https://bybit-exchange.github.io/docs/v5/guide

Signing string (concatenate, NO separators, in exactly this order):
  GET  : sign_payload = timestamp + api_key + recv_window + queryString
  POST : sign_payload = timestamp + api_key + recv_window + rawJsonBody

- timestamp: UTC epoch MILLISECONDS as a decimal string, e.g. "1672211928338"
- recv_window: decimal string, e.g. "5000"
- queryString: the URL query EXACTLY as it appears on the wire, no leading '?', e.g. "category=linear&symbol=BTCUSDT&limit=200". Bybit does NOT require alphabetical sorting — it requires byte-identical match with what you actually send.
- rawJsonBody: the serialized JSON string EXACTLY as it will be transmitted (same key order, same whitespace). Serialize once to a str, sign that str, then POST that same str as the body. Do NOT hand a dict to your HTTP client after signing — re-serialization reorders/reformats and breaks the signature (retCode 10004 "error sign").

Signature encoding:
  HMAC : hex_lowercase( HMAC_SHA256(key=api_secret, msg=sign_payload) )
  RSA  : base64( RSA_SHA256_PKCS1v15(private_key, sign_payload) )

Headers on every private request:
  X-BAPI-API-KEY      : <api key>
  X-BAPI-TIMESTAMP    : <ms timestamp, same value used in sign_payload>
  X-BAPI-SIGN         : <signature>
  X-BAPI-SIGN-TYPE    : 2            (HMAC; optional but sent by official pybit)
  X-BAPI-RECV-WINDOW  : 5000         (ms, default 5000 if omitted)
  Content-Type        : application/json   (POST only)
  X-Referer / Referer : <broker id>  (optional, NOT signed)

Timestamp tolerance window (verbatim from docs):
  server_time - recv_window <= timestamp < server_time + 1000
TRAP: forward tolerance is a hard 1000 ms no matter how large you set recv_window. A clock running >1s fast fails with retCode 10002 even with recv_window=60000. Sync clock via NTP, or poll GET /v5/market/time (public, unsigned) and carry a persistent offset. For a 500ms convergence loop, recv_window=5000 is right; don't inflate it hoping to fix clock skew.

Python reference:
  ts = str(int(time.time()*1000) + offset_ms)
  body = json.dumps(payload, separators=(',',':'))   # serialize ONCE
  sign = hmac.new(secret.encode(), (ts+key+"5000"+body).encode(), hashlib.sha256).hexdigest()
  requests.post(url, data=body, headers={...})       # send `body`, not `json=payload`

Private WebSocket auth (different scheme):
  expires = int((time.time()+5)*1000)
  sig = hmac_sha256_hex(secret, "GET/realtime" + str(expires))
  send {"op":"auth","args":[api_key, expires, sig]}
Doc: https://bybit-exchange.github.io/docs/v5/ws/connect

Base URLs: https://api.bybit.com (mainnet; alt host api.bytick.com), https://api-testnet.bybit.com (testnet), https://api-demo.bybit.com (demo trading — commonly documented, I did not re-verify it this session).

## REST 端点

### fundingCurrent

GET /v5/market/tickers?category=linear[&symbol=BTCUSDT]  (public, unsigned)
Doc: https://bybit-exchange.github.io/docs/v5/market/tickers
`symbol` is documented as required=false for linear/inverse/spot — omit it to get ALL symbols in the category in one call (only category=option requires symbol or baseCoin). That one call is your whole funding-rate screen.
Fields used for funding arb: fundingRate (decimal string, e.g. "0.0001" = 0.01% for the UPCOMING settlement, keeps moving until settlement — it is predicted, not realized), nextFundingTime (ms epoch string), markPrice, indexPrice, lastPrice, bid1Price, bid1Size, ask1Price, ask1Size, openInterest, openInterestValue, volume24h, turnover24h, price24hPcnt.
Note: tickers gives you nextFundingTime but NOT the interval length — you need instruments-info.fundingInterval to know the period (see fundingInterval field).

### fundingHistory

GET /v5/market/funding/history?category=linear&symbol=BTCUSDT[&startTime=&endTime=&limit=200]  (public, unsigned)
Doc: https://bybit-exchange.github.io/docs/v5/market/history-fund-rate
- limit: [1,200], default 200. Max 200 records per request.
- symbol is REQUIRED here (no bulk mode) — one call per symbol, so a 400-symbol backfill is 400+ calls; pace against the 600-req/5s IP budget.
- Response: result.list[] of {symbol, fundingRate, fundingRateTimestamp}. fundingRateTimestamp is the SETTLEMENT time in ms.
- PAGINATION CAVEAT (verbatim from docs): "Passing only startTime returns an error. Passing only endTime returns 200 records up till endTime. Passing neither returns 200 records up till the current time." So to backfill you walk BACKWARDS: call with endTime only, take the oldest fundingRateTimestamp in the batch, subtract 1 ms, use that as the next endTime, repeat until the list comes back short/empty.
- HOW FAR BACK: the docs state NO retention limit. Empirically it walks back to contract launch, but since it is undocumented, do not hardcode an assumption — just page backwards until the response stops shrinking your cursor. For BTCUSDT at 8h intervals, 200 records = ~66 days per call.
- CRITICAL for annualizing: do NOT assume 8h spacing. Derive the period from consecutive fundingRateTimestamp deltas per record (see quirks — Bybit now switches contracts to 1h dynamically).

### placeOrder

POST /v5/order/create   Doc: https://bybit-exchange.github.io/docs/v5/order/create-order
Body (linear perp, one-way mode): {category:"linear", symbol:"BTCUSDT", side:"Buy"|"Sell", orderType:"Limit"|"Market", qty:"0.001" (string), price:"60000" (string, required for Limit), timeInForce:"GTC"|"IOC"|"FOK"|"PostOnly"|"RPI", orderLinkId:"<=36 chars, unique", positionIdx:0 (one-way) |1 (hedge-buy) |2 (hedge-sell), reduceOnly:bool, closeOnTrigger:bool}
Other fields: triggerPrice, triggerDirection (1=rise,2=fall), triggerBy (LastPrice|IndexPrice|MarkPrice), takeProfit, stopLoss, tpOrderType, slOrderType, tpLimitPrice, slLimitPrice, tpTriggerBy, slTriggerBy, tpslMode (Full|Partial), orderFilter (Order|tpslOrder|StopOrder), isLeverage (spot: 0=spot,1=margin), marketUnit (spot market orders: baseCoin|quoteCoin), slippageToleranceType/slippageTolerance, smpType, bboSideType/bboLevel, mmp, orderIv.
Response: retCode 0 + result{orderId, orderLinkId}. ASYNCHRONOUS — retCode 0 means ACCEPTED, not live/filled. Confirm via private WS `order` topic.
BATCH (use this for two-leg atomicity-ish behavior): POST /v5/order/create-batch — max 20 orders for linear/inverse/option, 10 for spot. Doc: https://bybit-exchange.github.io/docs/v5/order/batch-place . Per-order results split across result.list AND retExtInfo.list (parallel arrays) — a top-level retCode 0 does NOT mean every leg succeeded; you MUST inspect retExtInfo.list[i].code.
AMEND (for a 500ms convergence loop, cheaper than cancel+replace): POST /v5/order/amend — category, symbol, orderId|orderLinkId, qty, price, triggerPrice. Only unfilled or partially-filled orders. Also async. Doc: https://bybit-exchange.github.io/docs/v5/order/amend-order
CANCEL: POST /v5/order/cancel . OPEN ORDERS: GET /v5/order/realtime (50/s).

### positions

GET /v5/position/list?category=linear&settleCoin=USDT[&symbol=&limit=&cursor=]
Doc: https://bybit-exchange.github.io/docs/v5/position
REQUIRED: category. For linear you MUST pass symbol OR settleCoin — omitting both errors. `settleCoin=USDT` + limit=200 is the right call for a portfolio sweep (cursor-paginate via nextPageCursor).
Fields: positionIdx (0 one-way / 1 hedge-buy / 2 hedge-sell), symbol, side (Buy|Sell|'' when flat), size, avgPrice/entryPrice, positionValue, leverage, markPrice, liqPrice, bustPrice, positionIM, positionMM, unrealisedPnl, curRealisedPnl, cumRealisedPnl, takeProfit, stopLoss, trailingStop, tpslMode, positionStatus, adlRankIndicator, autoAddMargin, isReduceOnly, seq, createdTime, updatedTime.
Note: when you pass an explicit symbol it "returns data regardless of having position or not" (size="0"), which is convenient for a fixed watchlist. Rate limit 50/s.
For a 500ms loop prefer the private WS `position` topic over polling (see websocket field).

### balance

GET /v5/account/wallet-balance?accountType=UNIFIED[&coin=USDT,USDC]
Doc: https://bybit-exchange.github.io/docs/v5/account/wallet-balance
accountType required: UNIFIED (trading) | FUND (funding account). Rate limit 50/s.
Account-level: totalEquity, totalWalletBalance, totalMarginBalance, totalAvailableBalance (cross-margin headroom — this is the number to size against), totalPerpUPL, totalInitialMargin, totalMaintenanceMargin, accountIMRate, accountMMRate, accountLTV.
Per-coin (coin[] array): coin, equity, walletBalance, usdValue, unrealisedPnl, cumRealisedPnl, borrowAmount, spotBorrow, availableToBorrow, totalOrderIM, totalPositionIM, totalPositionMM, collateralSwitch, marginCollateral.
GOTCHA: `availableToWithdraw` is DEPRECATED for accountType=UNIFIED as of 2025-01-09 and returns empty under UTA 2.0. Use account-level totalAvailableBalance for margin capacity; use the transferable-balance / spot-available endpoints if you actually need withdrawable.
For a 500ms loop prefer the private WS `wallet` topic.

### setLeverage

POST /v5/position/set-leverage
Doc: https://bybit-exchange.github.io/docs/v5/position/leverage
Body: {category:"linear"|"inverse", symbol:"BTCUSDT", buyLeverage:"5", sellLeverage:"5"}  — leverage values are STRINGS, range [1, maxLeverage from instruments-info.leverageFilter], must respect leverageStep.
CONSTRAINT: buyLeverage must EQUAL sellLeverage in (a) one-way position mode always, and (b) hedge mode + cross margin. They may differ only in hedge mode + isolated margin.
Success returns retCode 0 with an empty result object. Rate limit 10/s.
UNVERIFIED IN DOCS: calling it with the leverage already at that value is widely reported to return retCode 110043 ("leverage not modified") — the docs page does not list this code. Treat a 'not modified' response as success and idempotent; set leverage ONCE at startup per symbol, never inside the 500ms loop (it would eat the 10/s budget and is a no-op).

### instruments

GET /v5/market/instruments-info?category=linear[&symbol=&status=Trading&baseCoin=&limit=1000&cursor=]  (public, unsigned)
Doc: https://bybit-exchange.github.io/docs/v5/market/instrument
limit: [1,1000], default 500. Cursor-paginate via nextPageCursor (spot does not paginate). Fetch all linear instruments in ~1-2 calls.
Precision/size fields you need:
  priceFilter.tickSize     - price increment (also minPrice, maxPrice)
  lotSizeFilter.qtyStep    - quantity increment
  lotSizeFilter.minOrderQty / maxOrderQty (Limit & PostOnly) / maxMktOrderQty (Market)
  lotSizeFilter.minNotionalValue  - YES this exists for linear too (e.g. "5" USDT)
  lotSizeFilter.postOnlyMaxOrderQty - deprecated, use maxOrderQty
Other: symbol, contractType (LinearPerpetual|LinearFutures|InversePerpetual|InverseFutures), status (Trading|PreLaunch|Delivering|Closed), baseCoin, quoteCoin, settleCoin, launchTime, deliveryTime, fundingInterval (MINUTES — see fundingInterval field), leverageFilter{minLeverage,maxLeverage,leverageStep}, priceScale, unifiedMarginTrade, copyTrading, riskParameters.
Example linear response:
  "lotSizeFilter":{"maxOrderQty":"1190.000","minOrderQty":"0.001","qtyStep":"0.001","maxMktOrderQty":"500.000","minNotionalValue":"5"}
  "priceFilter":{"minPrice":"0.10","maxPrice":"1999999.80","tickSize":"0.10"}
Universe filter for a perp funding bot: category=linear + contractType=="LinearPerpetual" + status=="Trading".

### feeRate

GET /v5/account/fee-rate?category=linear[&symbol=BTCUSDT]   (SIGNED — this is an account endpoint, not market data)
Doc: https://bybit-exchange.github.io/docs/v5/account/fee-rate
Params: category (spot|linear|inverse|option) required; symbol optional for spot/linear/inverse; baseCoin for option.
Response result.list[] of {symbol, takerFeeRate, makerFeeRate} (decimal strings, e.g. "0.00055"/"0.0002").
This returns YOUR effective rates (VIP tier + any per-symbol/market-maker override already applied), not a public tier table. Omit symbol to pull every symbol's rate for the category in one call — do this once at startup and cache; it is your break-even input for whether a funding spread actually clears round-trip costs on both legs.

## WebSocket

Docs: https://bybit-exchange.github.io/docs/v5/ws/connect , https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook , https://bybit-exchange.github.io/docs/v5/websocket/public/ticker , https://bybit-exchange.github.io/docs/v5/websocket/private/position

ENDPOINTS
  Public linear  : wss://stream.bybit.com/v5/public/linear     (testnet: wss://stream-testnet.bybit.com/v5/public/linear)
  Public spot    : wss://stream.bybit.com/v5/public/spot
  Public inverse : wss://stream.bybit.com/v5/public/inverse
  Public option  : wss://stream.bybit.com/v5/public/option
  Private (all)  : wss://stream.bybit.com/v5/private            (testnet: wss://stream-testnet.bybit.com/v5/private)
  WS trade (order entry, lower latency than REST): wss://stream.bybit.com/v5/trade
  Optional query param: ?max_active_time=1m  (30s–600s) forces server-side disconnect if idle — useful as a dead-connection watchdog.

BEST BID/ASK — two options, pick based on your 500ms loop:
  (a) orderbook.1.{symbol}  — topic `orderbook.{depth}.{symbol}`. Linear depths: 1 (10ms push), 50 (20ms), 200 (100ms), 500/1000 (200ms). Depth 1 at 10ms is the cheapest true top-of-book.
      Message: {"topic":"orderbook.1.BTCUSDT","type":"snapshot"|"delta","ts":<ms>,"data":{"s":"BTCUSDT","b":[["60000.10","5.5"],...],"a":[["60000.20","3.1"],...],"u":<updateId>,"seq":<crossSeq>},"cts":<matching-engine ms>}
      Delta rules (verbatim): "If you receive an amount that is 0, delete the entry. If you receive an amount that does not exist, insert it. If the entry exists, you simply update the value." And: u == 1 means service restart — DISCARD your local book and rebuild from that message as a fresh snapshot. Use `seq` to dedupe/order across reconnects; `cts` is matching-engine time (use it, not `ts`, for latency measurement).
  (b) tickers.{symbol} — carries bid1Price/bid1Size/ask1Price/ask1Size AND fundingRate/nextFundingTime in one stream, 100ms push for derivatives. For a funding-arb tool this is usually the better single subscription: you get top-of-book and funding together.

FUNDING PUSH: there is no dedicated funding channel. fundingRate + nextFundingTime ride on `tickers.{symbol}`.
  MAJOR TRAP: for linear/inverse, tickers messages are type "snapshot" OR "delta", and "If a response param is not found in the message, then its value has not changed." You must MERGE deltas into a local per-symbol dict. If you naively read msg["data"]["fundingRate"] you will get KeyError/None on most messages. Spot and option tickers are snapshot-only (50ms for spot) — different handling.
  Other tickers fields (linear): lastPrice, markPrice, indexPrice, openInterest, openInterestValue, volume24h, turnover24h, price24hPcnt, prevPrice24h, highPrice24h, lowPrice24h.

PRIVATE TOPICS (subscribe after op:auth succeeds)
  position | position.linear | position.inverse | position.option   — full field set mirrors /v5/position/list (symbol, side, size, positionIdx, entryPrice, markPrice, leverage, liqPrice, unrealisedPnl, curRealisedPnl, positionIM/MM, seq, updatedTime, ...)
  order      — order lifecycle; this is how you confirm async create/amend actually went live/filled
  execution  — fills
  wallet     — balance/equity changes
  PUSH-STORM WARNING (verbatim): "Every time when you create/amend/cancel an order, the position topic will generate a new message (regardless if there's any actual change)". A 500ms convergence loop that amends orders will spray position pushes back at itself. Dedupe on `seq`/`updatedTime` and treat the WS position state as a cache refreshed by REST every few seconds, not as an event trigger.

SUBSCRIBE / AUTH / HEARTBEAT
  Subscribe: {"op":"subscribe","req_id":"opt","args":["tickers.BTCUSDT","orderbook.1.BTCUSDT"]}
  Private auth first: expires=int((time.time()+5)*1000); sig=hmac_sha256_hex(secret,"GET/realtime"+str(expires)); {"op":"auth","args":[api_key,expires,sig]}
  Heartbeat: send {"op":"ping"} (optional "req_id") every 20 SECONDS. Server replies {"success":true,"ret_msg":"pong",...}. Miss it and you get disconnected. Bybit's own WS ping frames are separate — implement the JSON app-level ping, not just protocol pings.
  Reconnect: exponential backoff, and on ANY reconnect throw away local orderbooks and re-snapshot.

LIMITS
  - Max 500 NEW connections per 5-minute window per domain. Do not reconnect-storm; a crash-loop will get you throttled.
  - Up to 1000 connections per IP for market data.
  - Subscription cap is by SIZE not count: the public `args` array is limited to ~21,000 characters per subscribe request. Chunk large watchlists across multiple subscribe messages on the same connection. I found no documented hard cap on total topics per connection — the practical limits are the args-size cap and your own bandwidth (200 symbols x tickers is routine).
  - WS trade: IP limit 3,000 req/s; system frequency-protection code 10429. Account-level order rate limits are SHARED between WS trade and REST — using WS does not buy you extra order throughput.

## 资金费周期

STANDARD: 8 hours, settling at 00:00 / 08:00 / 16:00 UTC.

NON-8h CONTRACTS EXIST AND ARE COMMON: Bybit runs contracts on 1h, 2h, 4h and 8h settlement. Beyond statically-configured short intervals, Bybit launched a **Dynamic Settlement Frequency System** (rollout 30 Oct – 3 Nov 2025): "When a Perpetual Contract's funding rate reaches its preset upper or lower limit during settlement, the system will automatically shift the settlement frequency to once per hour." The change takes ~4 minutes to apply and can revert to a longer interval as conditions normalize. Initially excluded (still fixed 8h): BTCUSDT, BTCUSDC, BTCUSD, ETHUSDT, ETHUSDC, ETHUSD, ETHBTCUSDT, ETHWUSDT.
Announcement: https://announcements.bybit.com/en/article/important-update-to-perpetual-contract-funding-settlement-frequency-blt9e1f8c588fe457c7/

HOW TO QUERY A CONTRACT'S INTERVAL VIA API:
  GET /v5/market/instruments-info?category=linear&symbol=BTCUSDT
  -> result.list[0].fundingInterval
  Field description verbatim from docs: "Funding interval (minute)". UNIT IS MINUTES, integer:
     480 = 8h,  240 = 4h,  120 = 2h,  60 = 1h
  Doc: https://bybit-exchange.github.io/docs/v5/market/instrument
  One unpaginated call (limit=1000) returns fundingInterval for every linear instrument — that's your whole interval map in a single request.

  Cross-check / sanity source: GET /v5/market/tickers?category=linear gives nextFundingTime (ms) per symbol but NOT the interval length. Combine: interval = fundingInterval (minutes) from instruments-info; countdown = nextFundingTime - now from tickers.

ANNUALIZATION FORMULA (get this right or your carry numbers are up to 8x wrong):
  periods_per_year = (365 * 24 * 60) / fundingInterval_minutes
  apr = fundingRate * periods_per_year
  Do NOT reuse a cached fundingInterval across a settlement boundary — re-read it (or re-derive from history) before sizing.

DERIVING INTERVAL FROM HISTORY (required for backtests, because spacing is non-uniform):
  From /v5/market/funding/history, compute per-record period as the delta between consecutive fundingRateTimestamp values rather than assuming a constant. Each historical funding payment must be weighted by ITS OWN period, not by the contract's current fundingInterval.

REFRESH POLICY RECOMMENDATION: poll instruments-info for the full linear universe every 1–5 minutes (it's public, unsigned, ~1-2 requests, cheap against the 600/5s IP budget) and treat fundingInterval as a live field, not static config. This is the correct defense against the dynamic 8h->1h switch, which is precisely the regime your arb tool most wants to detect.

Additional refs: https://www.bybit.com/en/help-center/article/Introduction-to-Funding-Rate , https://www.bybit.com/en/help-center/article/Funding-fee-calculation , https://www.bybit.com/en/announcement-info/fund-rate/

## 坑

- QTY/PRICE PRECISION — send STRINGS, never floats. Every qty must be an exact multiple of lotSizeFilter.qtyStep and every price an exact multiple of priceFilter.tickSize, or you get retCode 10001 params error. Correct method: use Decimal, quantize DOWN (floor) to the step, then format with exactly the decimal-place count implied by the step STRING (qtyStep="0.001" -> 3dp -> "0.001"). Never str(float) — Python will emit "1e-05" or "0.30000000000000004" and Bybit rejects both. Also honor lotSizeFilter.minOrderQty, maxOrderQty (Limit/PostOnly) vs maxMktOrderQty (Market — a different, smaller cap), and minNotionalValue (commonly "5" USDT on linear). A convergence loop that computes tiny residual deltas will constantly generate sub-minNotional orders — clamp: if |residual| notional < minNotionalValue, either skip the adjustment or round the residual up to the minimum, don't spam rejects.
- DYNAMIC FUNDING INTERVAL — the single biggest trap for a funding-arb tool on Bybit. Since the 30 Oct – 3 Nov 2025 rollout, fundingInterval is NOT a static per-symbol constant. Bybit's Dynamic Settlement Frequency System: "When a Perpetual Contract's funding rate reaches its preset upper or lower limit during settlement, the system will automatically shift the settlement frequency to once per hour" — the switch completes in ~4 minutes and can revert later. Excluded majors: BTCUSDT, BTCUSDC, BTCUSD, ETHUSDT, ETHUSDC, ETHUSD, ETHBTCUSDT, ETHWUSDT. Consequences: (1) never hardcode 8h; (2) re-read instruments-info.fundingInterval before every sizing/annualization decision and cache it for minutes, not days; (3) an 8h->1h switch multiplies realized carry 8x — exactly the situation your tool most wants to catch, and exactly where a stale cache produces an 8x wrong number; (4) the historical series from /v5/market/funding/history has NON-UNIFORM spacing, so you must compute each period from consecutive fundingRateTimestamp deltas rather than assuming a fixed multiplier. Announcement: https://announcements.bybit.com/en/article/important-update-to-perpetual-contract-funding-settlement-frequency-blt9e1f8c588fe457c7/
- TIMESTAMP — forward tolerance is a HARD 1000 ms and recv_window does not widen it: server_time - recv_window <= timestamp < server_time + 1000. A clock 1.5s fast fails with recv_window=60000. Fix the clock (NTP) or maintain an offset from GET /v5/market/time; do not inflate recv_window. retCode 10002 is the timestamp error, retCode 10004 is a bad signature — they are different failures, log them differently.
- SIGNATURE BYTE-EXACTNESS — sign the exact bytes you send. Serialize the JSON body to a string ONCE, HMAC that string, then transmit that same string (requests: data=body, NOT json=payload). If the HTTP client re-serializes (key reorder, added spaces after ':'), the signature breaks. Same for GET: the queryString you sign must byte-match the URL. Bybit does not require sorted params, only consistency. Also note X-Referer is NOT part of the signed payload — adding/removing the broker code never invalidates a signature.
- RATE LIMITS — /v5/order/create, /v5/order/amend, /v5/order/cancel and /v5/position/set-leverage are 10/s per account (some linear tiers show 20/s); /v5/order/realtime, /v5/position/list, /v5/account/wallet-balance are 50/s. IP: 600 requests per 5-second window. A 500ms convergence loop across 2 legs is only ~4 order-req/s — fine for one pair, but N pairs x 2 legs at 2Hz saturates the 10/s order budget at N=2-3. Batch via /v5/order/create-batch (20 linear / 10 spot per request) and read the X-Bapi-Limit-Status / X-Bapi-Limit / X-Bapi-Limit-Reset-Timestamp response headers to back off proactively. Breach = retCode 10006 'Too many visits!'; an HTTP-level IP breach returns 403 with a 10-MINUTE ban — an unthrottled retry loop will take your bot offline for 10 minutes. WS trade shares the same account-level order budget, so it buys latency, not throughput.
- FOK IS SUPPORTED — timeInForce accepts GTC, IOC, FOK, PostOnly, RPI on linear/inverse/spot/option. But for delta-neutral two-leg execution FOK is usually the wrong tool: on thin books it fails outright and leaves you re-submitting, whereas the failure mode you actually fear is a partial fill on one leg. Prefer IOC with a bounded aggressive price + re-converge next tick (which is what your 500ms loop already does), or PostOnly when you're patient. Use FOK only when a partial leg is genuinely worse than no fill.
- SYMBOL NAMING — perp vs spot are NOT distinguished by the symbol string, only by `category`. Linear USDT perp is BTCUSDT and the spot pair is ALSO BTCUSDT; the only difference is category=linear vs category=spot. Cross-exchange mappers that key on symbol alone will silently conflate them. USDC perps are BTCPERP / ETHPERP (NOT BTCUSDC) — this is the classic mapping bug. Inverse perps: BTCUSD. Dated futures: USDT BTCUSDT-21FEB25, USDC BTC-24MAR23, inverse BTCUSDH23/M23/U23/Z23 (H/M/U/Z = Q1..Q4). To get ONLY perpetuals: category=linear AND contractType=="LinearPerpetual" AND status=="Trading" (status also carries PreLaunch/Delivering/Closed — PreLaunch symbols appear in the list and will reject orders).
- TICKERS WS IS DELTA-ENCODED for linear/inverse — 'If a response param is not found in the message, then its value has not changed.' You MUST merge into a per-symbol state dict. Reading fundingRate directly off an arbitrary message yields None on most ticks. Spot/option tickers are snapshot-only, so the same handler cannot be shared naively.
- ORDERBOOK DELTA RESET — size "0" means DELETE the level (not 'zero liquidity'). u == 1 signals a service restart: discard the local book entirely and treat that message as a fresh snapshot. Any reconnect must also discard and re-snapshot. Use `seq` for dedupe and `cts` (matching-engine timestamp) rather than `ts` for real latency measurement.
- ORDER PLACEMENT IS ASYNCHRONOUS — retCode 0 on /v5/order/create means ACCEPTED, not live and not filled. Never update your internal position state off the create response. Confirm via the private WS `order` topic (or /v5/order/realtime). For /v5/order/create-batch this is worse: a top-level retCode 0 with per-order failures buried in retExtInfo.list[] (parallel to result.list[]) — you must iterate both arrays or you'll believe both legs went on when only one did, which is exactly how a delta-neutral book ends up naked.
- POSITION QUERY REQUIRES A DISCRIMINATOR — GET /v5/position/list with category=linear needs symbol OR settleCoin, not neither. Use settleCoin=USDT&limit=200 for a sweep. Passing an explicit symbol returns a row even when flat (size="0"), which is handy for a fixed watchlist. In one-way mode use positionIdx=0 everywhere; in hedge mode you must tag every order with positionIdx 1/2 and reduceOnly semantics change — pick one-way mode for a two-leg hedger unless you specifically need both sides on one symbol.
- WALLET FIELD DEPRECATION — availableToWithdraw is deprecated for accountType=UNIFIED since 9 Jan 2025 and comes back empty under UTA 2.0. Sizing must use account-level totalAvailableBalance. Code written against older Bybit examples will read an empty string, coerce to 0, and refuse to trade.
- SET-LEVERAGE IS NOT LOOP-SAFE — buyLeverage must equal sellLeverage in one-way mode and in hedge+cross (they may differ only in hedge+isolated). Re-setting to the current value is widely reported to return retCode 110043 'leverage not modified' (NOT documented on the leverage page — flagging as unverified); treat it as success. Call it once per symbol at startup, never inside the convergence loop — it costs 10/s budget and does nothing.
- WS HEARTBEAT IS APPLICATION-LEVEL — you must send {"op":"ping"} JSON every 20s; protocol-level WebSocket ping frames alone will not keep the session alive. Combine with ?max_active_time= as a watchdog. Reconnects are rate-limited to 500 new connections per 5 minutes per domain, so a crash-loop gets throttled — use exponential backoff.
- orderLinkId IS FREE FOR YOUR USE — because Bybit's broker attribution rides on the X-Referer header rather than a client-order-id prefix (unlike Binance/OKX), you can use the full 36-char orderLinkId for your own leg/pair tagging with no broker-code collision. Keep it unique; it's your idempotency key for reconciling async fills.
