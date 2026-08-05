# Backpack Exchange（REST api/v1 + wapi/v1，ED25519 签名）

调研时间 2026-08。docs.backpack.exchange 是 Redoc 单页，整份 OpenAPI 3.0 spec 以
`__redoc_state` JSON 内嵌在 HTML 里（约 390KB），本文的字段表和枚举全部从这份 spec
逐字抠出，再用**公开端点的线上真实响应**逐条核对过（2026-08-05 实测，无需鉴权的
GET 全打了一遍）。签名拼法另与 CCXT 的 backpack 适配器交叉验证。
凡是 spec 和线上都确认不了的，统一标 UNVERIFIED，不要脑补。

REST base：`https://api.backpack.exchange`
（历史类私有端点在 `/wapi/v1/history/*`，**同一个域名**，只是路径前缀不同；
公开与交易端点在 `/api/v1/*`。）
WS：`wss://ws.backpack.exchange`。

---

## 认证签名（ED25519，不是 HMAC）

这家是八家里唯一用 ED25519 的：api_key = **base64 的 32 字节公钥**（verifying key），
api_secret = **base64 的 32 字节私钥种子**（seed）。签名是 RFC 8032 Ed25519，
**确定性签名**——同一把 key 签同一串字节永远得到同一个签名，测试可以在 handler 里
用公钥 `verify()` 真实请求重建出的签名串，比比对十六进制串更强。

| header | 值 | 备注 |
| --- | --- | --- |
| `X-API-Key` | base64 公钥原文 | 必填 |
| `X-Signature` | base64(Ed25519 签名 64 字节) | 必填 |
| `X-Timestamp` | 毫秒时间戳（十进制字符串） | 必填，须与签名串里同值 |
| `X-Window` | 请求有效窗口毫秒，默认 `5000`，**最大 `60000`** | 可不发；**不发也必须把 5000 签进串里** |
| `Content-Type` | `application/json`（POST/DELETE/PATCH 带 body 时） | — |

签名串拼法（文档 Authentication 节原文步骤）：

1. 把**请求 body 或查询参数**的键值对按**键名字母序**排列，拼成查询串格式 `k=v&k2=v2`；
2. 尾部追加 `&timestamp=<毫秒>&window=<窗口>`（没传 X-Window 也要写 `window=5000`）；
3. 头部加 `instruction=<指令名>&`。

```
instruction=<TYPE>&param1=value1&param2=value2&timestamp=<ms>&window=<ms>
```

文档给的撤单例子（body 是 `{"orderId": 28, "symbol": "BTC_USDT"}`）：

```
instruction=orderCancel&orderId=28&symbol=BTC_USDT&timestamp=1614550000000&window=5000
```

逐条契约：

- **GET 签查询参数、POST/DELETE/PATCH 签 body 键值对**；没有任何参数的端点
  （accountQuery、positionQuery 不带 filter 时）只签 `instruction=..&timestamp=..&window=..`。
- 与 KuCoin 不同，**签名不吃 body 的原始字节**——服务端是把解析后的键值对重建成串再验。
  所以 `json=` 直接丢给 httpx 没问题，**但值的文本形态必须两边一致**：
  布尔写小写 `true`/`false`（Python `str(True)` 是 `"True"`，必须 `.lower()` 或手写），
  数量/价格全用字符串型 decimal（API 本来就要求 string decimal，别让 json 把 `"141"`
  变 `141.0`），`clientId` 是 JSON 整数、签名串里就写十进制整数。
- 参数值不做 URL-encode（文档例子是原文）。这家的值域（符号、枚举、decimal 串）
  全是 URL-safe 字符，percent-encode 永远不会触发，照原文拼即可。
- 批量下单（`POST /api/v1/orders`）特殊：**每单**各自按字母序拼串、各自加
  `instruction=orderExecute&` 前缀，然后把各单的串用 `&` 连接，`timestamp/window`
  只在最末尾出现一次：

```
instruction=orderExecute&orderType=Limit&price=141&quantity=12&side=Bid&symbol=SOL_USDC_PERP&instruction=orderExecute&orderType=Limit&price=140&quantity=11&side=Bid&symbol=SOL_USDC_PERP&timestamp=1750793021519&window=5000
```

- WS 私有流订阅的签名串是 `instruction=subscribe&timestamp=<ms>&window=<ms>`，
  签名放在 SUBSCRIBE 消息的 `signature` 数组里（见 WebSocket 节）。

可直接照抄的实现（cryptography 已在依赖里，**不要**引 pynacl）：

```python
import base64
from cryptography.hazmat.primitives.asymmetric import ed25519

sk = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(api_secret))

def _sign(instruction: str, params: dict, ts_ms: int, window_ms: int = 5000) -> str:
    # 布尔必须小写；调用方保证 params 的值已是最终文本形态（decimal 串 / 整数）
    parts = [f"{k}={str(v).lower() if isinstance(v, bool) else v}"
             for k, v in sorted(params.items())]
    core = f"instruction={instruction}&"
    if parts:
        core += "&".join(parts) + "&"
    core += f"timestamp={ts_ms}&window={window_ms}"
    return base64.b64encode(sk.sign(core.encode())).decode()
```

curl（GET 查持仓，无参数）：

```bash
curl 'https://api.backpack.exchange/api/v1/position' \
  -H 'X-API-Key: <base64 pubkey>' \
  -H 'X-Signature: <base64 sig of "instruction=positionQuery&timestamp=1785948047107&window=5000">' \
  -H 'X-Timestamp: 1785948047107' \
  -H 'X-Window: 5000'
```

### instruction 对照表（适配器用得到的全集）

| 端点 | method | instruction |
| --- | --- | --- |
| `/api/v1/account` | GET | `accountQuery` |
| `/api/v1/account` | PATCH | `accountUpdate` |
| `/api/v1/account/limits/order` | GET | `maxOrderQuantity` |
| `/api/v1/capital` | GET | `balanceQuery` |
| `/api/v1/position` | GET | `positionQuery` |
| `/api/v1/order` | POST | `orderExecute` |
| `/api/v1/order` | DELETE | `orderCancel` |
| `/api/v1/order` | GET | `orderQuery` |
| `/api/v1/orders` | POST | `orderExecute`（批量拼法见上） |
| `/api/v1/orders` | GET | `orderQueryAll` |
| `/api/v1/orders` | DELETE | `orderCancelAll` |
| `/wapi/v1/history/orders` | GET | `orderHistoryQueryAll` |
| `/wapi/v1/history/fills` | GET | `fillHistoryQueryAll` |
| `/wapi/v1/history/funding` | GET | `fundingHistoryQueryAll` |
| WS 私有流订阅 | — | `subscribe` |

### 返佣 / broker（本仓库明确不做）

下单端点有可选 header `X-BROKER-ID`（uint16）和 `X-BROKER-KEY`——这就是 Backpack 的
返佣注入机制。**本仓库一个都不发**（用户明确要求不带任何返佣码）。CCXT 会自动塞
`X-Broker-Id: 1400`，抄它的实现时务必把这行剔掉。测试里要钉死：请求 header 里
不出现任何 `X-BROKER-*`。

---

## 符号模型（币本位数量，没有"张"）

- 永续符号：`BTC_USDC_PERP`（`baseSymbol=BTC`，`quoteSymbol=USDC`，`marketType=PERP`）；
  现货是 `BTC_USDC`。2026-08-05 实测 `/api/v1/markets` 共 171 个市场，其中 84 个 PERP。
- **数量单位就是 base 币**（BTC 合约下 1 = 1 BTC），没有张、没有乘数、没有千倍前缀，
  `contract_size = 1`。
- `marketType` 枚举：`SPOT / PERP / IPERP / DATED / PREDICTION / RFQ`。
  当前线上只有 SPOT 和 PERP；适配器只留 `marketType == "PERP"` 且
  `orderBookState == "Open"`（枚举 `Open/Closed/CancelOnly/LimitOnly/PostOnly`）
  且 `visible == true` 的行。`rwaMarketType == "STOCK"` 是代币化股票（现货），顺手滤掉。
- 精度全在 `filters` 里（BTC_USDC_PERP 线上实测值）：
  - `filters.price.tickSize = "0.1"`，`minPrice = "0.1"`，`maxPrice = "1000000"`
  - `filters.quantity.stepSize = "0.00001"`，`minQuantity = "0.00001"`，`maxQuantity = null`
  - **没有 minNotional 一类的名义额下限字段**（最小约束就是 minQuantity）
- 每行还有 `fundingInterval`（毫秒）、`fundingRateUpperBound/LowerBound`（**基点**，见下）、
  `openInterestLimit`、`imfFunction/mmfFunction`（sqrt 保证金函数，见杠杆节）。

`/api/v1/market?symbol=BTC_USDC_PERP` 线上真实响应（截取关键字段）：

```json
{
  "symbol": "BTC_USDC_PERP", "baseSymbol": "BTC", "quoteSymbol": "USDC",
  "marketType": "PERP", "orderBookState": "Open", "visible": true,
  "filters": {
    "price":    {"tickSize": "0.1", "minPrice": "0.1", "maxPrice": "1000000"},
    "quantity": {"stepSize": "0.00001", "minQuantity": "0.00001", "maxQuantity": null}
  },
  "fundingInterval": 3600000,
  "fundingRateUpperBound": "100", "fundingRateLowerBound": "-100",
  "openInterestLimit": "4000",
  "imfFunction": {"type": "sqrt", "base": "0.01333333", "factor": "0.00002494"},
  "mmfFunction": {"type": "sqrt", "base": "0.00666667", "factor": "0.00001496"}
}
```

---

## 资金费（1 小时结算——年化别再乘 8 小时口径）

- **结算周期是逐合约字段 `fundingInterval`（毫秒）**。2026-08-05 实测 84 个 PERP
  **全部是 3600000 = 1 小时**。官方 learn 文章也写明每小时整点结算（"Rates Strike"）。
  年化 = 每期费率 × 24 × 365；**照币安 8h 肌肉记忆算会把年化低估 8 倍**。
  实现读字段，不写死。
- 符号约定：**正 = 多头付空头**（官方 learn/hourly-funding 文章原文
  "If positive, longs pay shorts, if negative, shorts pay longs"），与仓库约定一致，原样存。
- 当前费率：`GET /api/v1/markPrices`（公开；`symbol` 可选，`marketType` 默认 PERP）。
  **就算传了 symbol 也返回数组**。不带 symbol 一次拉回全部 84 个 PERP 的
  费率+标记价+下次结算时刻——`fetch_carry_rates` 批量扫描就用这一发，
  别逐符号打 84 次。线上真实响应：

```json
[{
  "fundingRate": "-0.0000117704808946259223127855",
  "indexPrice": "64620.4597227", "markPrice": "64575.6",
  "nextFundingTimestamp": 1785949200000, "symbol": "BTC_USDC_PERP"
}]
```

  `fundingRate` 是**当期的小数费率**（不是基点），`nextFundingTimestamp` 是毫秒、
  即当期结束/下期开始（整点）。spot 行没有这三个字段（schema 里非必填）。
- **费率上下限在 market 行里，单位是基点**：`fundingRateUpperBound: "100"` = 100bps
  = 每期 ±1%。**除以 10000** 才是小数费率。实测分布：3 个市场 ±100bps（BTC/ETH/SOL 档），
  其余 81 个 ±150bps。fetch_funding_limits 返回 `(upper/10000, lower/10000)` 即 (cap, floor)。
  注意这里跟 markPrices 的 fundingRate（小数）**单位不一致**，clamp 前必须换算。

### fundingRates 历史（翻页方向是这家最省心的地方，但要自己反转）

`GET /api/v1/fundingRates?symbol=&limit=&offset=`（公开，无需签名）：

- `limit` 默认 100，**单页上限 10000**；`offset` 默认 0。
- **返回按 `intervalEndTimestamp` 降序（最新在前）**，`offset` 从最新往回数。
  线上实测：offset=0 的第一条就是刚结算的那期，offset=5 精确接在第 5 条后面，
  无缝无重复。**天然满足"覆盖到窗口末端"的仓库契约**——第一页永远含最新一条，
  往回翻到 since_ms 之前即可停，最后整体 reverse 成升序返回。
  没有币安那种"从 since 往后数"的假序列坑。
- 一页 10000 条 ≈ 416 天（1h 结算），正常窗口一页就够，翻页逻辑仍要写（防御 limit 缩水）。
- `intervalEndTimestamp` 是**无时区的 UTC datetime 字符串**（`"2026-08-05T17:00:00"`，
  没有 Z 后缀），不是毫秒整数！解析：`datetime.fromisoformat(s).replace(tzinfo=timezone.utc)`
  再转毫秒。线上真实响应：

```json
[{"fundingRate": "-0.000011756", "intervalEndTimestamp": "2026-08-05T17:00:00", "symbol": "BTC_USDC_PERP"},
 {"fundingRate": "-0.000006562", "intervalEndTimestamp": "2026-08-05T16:00:00", "symbol": "BTC_USDC_PERP"}]
```

- 未知符号**不报错，返回 200 `[]`**（实测）。空列表≠没数据错误，适配器如实返回空。
- 结算时刻仍要按仓库契约去重（offset 翻页途中跨过一次整点结算，整个列表会下移一位，
  与 HTX/Bitget 的 pageNo 同病）。

账户自己的资金费**支付流水**（不是市场费率）在
`GET /wapi/v1/history/funding`（`fundingHistoryQueryAll`，签名）：
`symbol/limit(默认100,最大1000)/offset/sortDirection(Asc|Desc)`，
返回 `quantity`（正=收到，负=付出）、`intervalEndTimestamp`（同样是 naive datetime 串）、
`fundingRate`。

---

## 费率档位

- 挂牌基础档（合约）：**maker 0.02% / taker 0.05%**（=2bps/5bps）。
  全局 support 站的费率表是**图片**读不出字，数值取自 EU support 文本版费率页
  （eu.support.backpack.exchange/exchange/trading-fees，11 档：Tier1 0.020%/0.050%
  → Tier6 0.010%/0.028% → VIP5 0.000%/0.018%），与第三方站交叉一致。
  **public_feed.DEFAULT_TAKER = 0.0005 正确，可去掉 UNVERIFIED 标。**
  档位按 30 天量**每小时重算**。强平费 1%/笔（EU support）。
- 账户真实档位：`GET /api/v1/account`（`accountQuery`，签名）。响应（AccountSummary）里
  `futuresMakerFee` / `futuresTakerFee` / `spotMakerFee` / `spotTakerFee` 是
  **基点 decimal 字符串**（spec 原文 "Futures maker fee in basis points.
  Negative if a rebate exists."）。除以 10000 得小数费率；正=成本、maker 负=返佣，
  **符号约定与仓库一致，不用翻转**。账户级一档吃全市场 → FeeSchedule.symbol 填 `""`。
  同一响应还有 `leverageLimit`、`liquidating`、`limitOrders` 等（见杠杆节）。

---

## REST 端点

### 系统

- `GET /api/v1/ping` → 裸文本 `pong`。
- `GET /api/v1/time` → **裸文本毫秒串**（实测 body 就是 `1785948047107`，
  **不是 JSON**）。`fetch_server_time_ms` 用 `int(resp.text)`，别 `resp.json()`。
- `GET /api/v1/status` → `{"status":"Ok","message":null}`（枚举 Ok/Maintenance UNVERIFIED
  第二值，spec 里 Status 枚举被折叠）。

### 行情（全公开，无需签名）

- `GET /api/v1/markets?marketType[]=PERP` — 不传 marketType 默认返回 SPOT+PERP。
- `GET /api/v1/market?symbol=` — 单市场，同 schema。
- `GET /api/v1/depth?symbol=&limit=` — `limit` 是**枚举字符串** `5/10/20/50/100/500/1000`，
  不传默认给到每边 5000 档。响应：

```json
{"asks": [["64575.2","0.24612"], ["64576.8","0.77429"]],
 "bids": [["64574.3","0.77413"], ["64575.1","3.57196"]],
 "lastUpdateId": "5796499452", "timestamp": 1785948116439400}
```

  **大坑：bids 是价格升序——最优买价在数组最后一个元素**（实测 `bids[-1]` 才是 best bid，
  asks 升序 best ask 是 `asks[0]`）。照币安肌肉记忆取 `bids[0]` 会把买一价读成带宽
  最远端，盘口价差算出来大得离谱。`timestamp` 是**微秒**。`limit=N` 给的是最优 N 档
  （两侧各 N），只是 bids 的排列方向反直觉。
- `GET /api/v1/ticker?symbol=`（及 `/tickers`）— 24h 统计：`lastPrice/high/low/volume/
  quoteVolume/firstPrice/priceChange/priceChangePercent/trades`。
  **没有 bid/ask 字段！这家没有 REST bookTicker 端点**，`fetch_book_top` 只能用
  `depth?limit=5`（或走 WS bookTicker 流）。
- `GET /api/v1/markPrices?symbol=` — 见资金费节。
- `GET /api/v1/openInterest?symbol=` — `[{symbol, openInterest, timestamp(微秒)}]`，
  不传 symbol 返回全市场。
- `GET /api/v1/fundingRates` — 见资金费节。
- `GET /api/v1/klines?symbol=&interval=&startTime=&endTime=` — **startTime/endTime 单位是秒**
  （不是毫秒，这家自己都不统一）。interval 枚举 `1s..1month`。适配器用不到，防坑记一笔。

### 交易（全签名）

#### 下单 `POST /api/v1/order`（`orderExecute`）

请求 body（OrderExecutePayload，必填 `orderType/side/symbol`）关键字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `symbol` | string | `BTC_USDC_PERP` |
| `side` | enum | **`Bid`（买）/ `Ask`（卖）**，不是 buy/sell |
| `orderType` | enum | `Limit` / `Market` |
| `quantity` | decimal 串 | base 币数量；Market 单可改用 `quoteQuantity`（花多少 quote） |
| `price` | decimal 串 | Limit 必填 |
| `timeInForce` | enum | `GTC` / `IOC` / `FOK`（默认 GTC UNVERIFIED，spec 未写默认值） |
| `postOnly` | bool | 只挂不吃；**独立布尔，不是 TIF 值** |
| `reduceOnly` | bool | 仅期货 |
| `clientId` | **uint32 整数** | 自定义单号。**不是字符串！** 见下 |
| `selfTradePrevention` | enum | `RejectTaker`(默认)/`RejectMaker`/`RejectBoth` |
| 其余 | — | TP/SL 触发单、`slippageTolerance`、spot margin 的 autoLend/autoBorrow 族，本仓库不用 |

- **`clientId` 是 uint32**（0..4294967295 的 JSON 整数）。仓库的 client_order_id 是字符串，
  适配器要做确定性映射（如 `zlib.crc32(coid.encode())`），并在查单/撤单时用同一个数。
  **服务端是否拒绝重复 clientId 未文档化（UNVERIFIED）——不能指望它当幂等闸**，
  只能当查找键用；OrderUnknown 消解全靠 cancel→查开放单→查历史单这条链。
- 响应 200 = 撮合引擎的**同步终态**（架构文档：单线性命令流，HTTP 响应就是执行结果）。
  返回 LimitOrder / MarketOrder 对象（按 `orderType` 判别）：
  `id`(string)、`clientId`、`status`、`executedQuantity`、`executedQuoteQuantity`、
  `quantity`、`price`、`side`、`createdAt`(毫秒整数)、`postOnly`、`reduceOnly`、
  `timeInForce`、`selfTradePrevention`。
  **没有均价、没有手续费字段**：均价 = `executedQuoteQuantity / executedQuantity`
  （executedQuantity 为 0 时置 0），手续费只能去 `/wapi/v1/history/fills` 拿
  （OrderResult.fee 可先填 0 并在 note/文档写明）。
- `status` 枚举：`New / PartiallyFilled / Filled / Cancelled / Expired / TriggerPending /
  TriggerFailed`。映射：Filled→filled，PartiallyFilled→partial，New→new，
  Cancelled→canceled，Expired→canceled（IOC 未成交残量/FOK 杀单的终态；
  订单历史里另有 expiryReason 字段）。
- **Taker 速度带**：所有非 postOnly 订单（含 IOC 限价、市价）统一加 **100ms** 撮合延迟，
  postOnly 与撤单豁免。500ms 收敛循环里要把这 100ms 算进预算。

#### 批量下单 `POST /api/v1/orders`

body 是 OrderExecutePayload **数组**，一批最多 **50** 单、不能为空。校验阶段
all-or-nothing（任一单参数非法整批拒绝），过校验后各单独立执行、逐单返回结果数组。
签名串拼法见认证节。

#### 撤单 `DELETE /api/v1/order`（`orderCancel`）

body：`{"symbol": ..., "orderId": ...}` 或 `{"symbol": ..., "clientId": <uint32>}`。
**orderId 与 clientId 只能给一个**——文档原文：两个都给时 orderId 优先且
**签名会验不过**。响应 200 = 被撤订单对象（含撤前的 executedQuantity，撤单同时拿到
成交量，很好用）；**202 = 已受理但尚未执行**（body 无内容）——不能当撤成功，
要接着查单确认终态。

#### 查单 `GET /api/v1/order?symbol=&orderId=|clientId=`（`orderQuery`）

**只返回还挂在簿上的单**。已完全成交/已撤/已过期 → **404 `RESOURCE_NOT_FOUND`**。
所以 query_order 的实现必须是两段式：先查这里，404 再去
`GET /wapi/v1/history/orders?symbol=&limit=..`（`orderHistoryQueryAll`）按 `clientId`
字段过滤（历史端点不能按 clientId 服务端过滤，只有 `orderId` 参数，得拉回来自己配）。
**404 绝不能直接当"订单从未被接受"**——成交完的单在这也是 404，误判会让
resolve_unknown_order 把已成交仓位当成 0。

#### 挂单列表 `GET /api/v1/orders?symbol=&marketType=`（`orderQueryAll`）

返回 OrderType 数组（Limit/Market 混合，按 orderType 判别）。不传 symbol 给全市场。

#### 全撤 `DELETE /api/v1/orders`（`orderCancelAll`）

body `{"symbol": ..., "orderType": "RestingLimitOrder"|"ConditionalOrder"(可选)}`，
返回被撤订单数组；同样可能 202。

### 历史（/wapi/v1，全签名）

- `GET /wapi/v1/history/orders`（`orderHistoryQueryAll`）：
  `orderId/symbol/limit(默认100,最大1000)/offset/sortDirection(Asc|Desc)/marketType`。
  **没有 from/to 时间过滤**。响应 Order 数组：注意这里 `createdAt` 是
  **naive datetime 串**（挂单端点是毫秒整数，两处类型不同！），含
  `clientId/status/executedQuantity/executedQuoteQuantity/expiryReason`。
- `GET /wapi/v1/history/fills`（`fillHistoryQueryAll`）：
  `orderId/from/to(毫秒,from 含 to 不含)/symbol/limit(≤1000)/offset/fillType/sortDirection`。
  响应 OrderFill：`fee/feeSymbol/isMaker/orderId/price/quantity/side/symbol/
  timestamp(naive datetime 串)/tradeId`，**`clientId` 在这是 string 类型**（spec 原文，
  又一处类型漂移）。手续费唯一来源。默认含清算/ADL 等系统单，`fillType=User` 只看自己的。
- `GET /wapi/v1/history/funding`（`fundingHistoryQueryAll`）：见资金费节。

### 账户

- `GET /api/v1/account`（`accountQuery`）→ AccountSummary：费率 bps 四件套 +
  `leverageLimit`（decimal 串）+ `liquidating` + `limitOrders/triggerOrders` 计数 +
  autoLend/autoRepayBorrows 等布尔。
- `PATCH /api/v1/account`（`accountUpdate`）body：
  `{"leverageLimit": "10"}`（可选 autoLend/autoBorrowSettlements/autoRepayBorrows）。
  响应 200 **无 body**。
- `GET /api/v1/account/limits/order`（`maxOrderQuantity`）：算某市场当前可下的最大数量
  （参数 symbol/side/price...），风控预检可用，本仓库暂不用。

### 持仓 `GET /api/v1/position`（`positionQuery`）

参数 `symbol`（可选，单仓）、`marketType`（可选）。响应 FuturePositionWithMargin 数组：

| 字段 | 说明 |
| --- | --- |
| `netQuantity` | **正=多，负=空**（净持仓模型，无对冲双向仓） |
| `netCost` | 开仓成本（正多负空） |
| `entryPrice` / `markPrice` / `breakEvenPrice` | decimal 串 |
| `estLiquidationPrice` | 预估强平价 |
| `pnlUnrealized` / `pnlRealized` | 浮盈/已实现 |
| `cumulativeFundingPayment` | 该仓累计资金费 |
| `imf` / `mmf` + `imfFunction/mmfFunction` | 初始/维持保证金率及函数 |
| `positionId` / `userId` / `subaccountId` | id 族 |

无仓位时返回 `[]`（op 定义里 404 是给坏符号的，UNVERIFIED 具体触发条件）。

---

## 杠杆（账户级，没有逐合约杠杆）

- 杠杆是**账户级** `leverageLimit`：`PATCH /api/v1/account`（`accountUpdate`）设置，
  `GET /api/v1/account` 读回。**没有按符号设杠杆的端点**——set_leverage(symbol, lev)
  只能忽略 symbol 调 accountUpdate，文档注释写明这是全账户旋钮（两腿同所对冲时
  这反而省事，但要防止两条腿的引擎重复设置互相打架，设前先读）。
- **没有杠杆分层表端点**。保证金率由每个 market 的 `imfFunction` 给：
  `{type:"sqrt", base, factor}`，即 IMF = max(base, factor*sqrt(数量×价格)) 的形态
  （公式形状 UNVERIFIED，spec 只给参数不给公式）。BTC 的 base=0.01333≈1/75；
  账户 leverageLimit 上限官网标 50x（UNVERIFIED 边界值，API 文档未写）。
  fetch_leverage_tiers 建议：返回单档 `[(openInterestLimit×markPrice, 1/imf.base)]`
  或空表并在文档写明这家不适用档位模型，由主控定夺。

---

## 错误码与限频

错误响应统一 JSON（2024-10-15 起）：`{"code": "<ApiErrorCode>", "message": "..."}`，
HTTP 状态 400/401/403/404/429/500/503，撤单类还有 202（受理未执行）。

ApiErrorCode 全集（spec 枚举原文）：

```
ACCOUNT_DEACTIVATED  ACCOUNT_LIQUIDATING  BORROW_LIMIT  BORROW_REQUIRES_LEND_REDEEM
FORBIDDEN  INSUFFICIENT_FUNDS  INSUFFICIENT_MARGIN  INSUFFICIENT_SUPPLY
INVALID_ASSET  INVALID_CLIENT_REQUEST  INVALID_MARKET  INVALID_ORDER  INVALID_PRICE
INVALID_POSITION_ID  INVALID_QUANTITY  INVALID_RANGE  INVALID_SIGNATURE  INVALID_SOURCE
INVALID_SYMBOL  INVALID_TWO_FACTOR_CODE  LEND_LIMIT  LEND_REQUIRES_BORROW_REPAY
MAINTENANCE  MAX_LEVERAGE_REACHED  ORDER_LIMIT  POSITION_LIMIT  PRECONDITION_FAILED
RESOURCE_NOT_FOUND  SERVER_ERROR  TIMEOUT  TOO_EARLY  TOO_MANY_REQUESTS
TRADING_PAUSED  UNAUTHORIZED
```

分类建议：

- `TOO_MANY_REQUESTS`（HTTP 429）→ RateLimited。
- 明确拒绝（OrderRejected，可安全重试/换参）：`INVALID_ORDER / INVALID_PRICE /
  INVALID_QUANTITY / INVALID_SYMBOL / INVALID_MARKET / INVALID_CLIENT_REQUEST /
  INSUFFICIENT_FUNDS / INSUFFICIENT_MARGIN / MAX_LEVERAGE_REACHED / ORDER_LIMIT /
  POSITION_LIMIT / TRADING_PAUSED / ACCOUNT_LIQUIDATING / ACCOUNT_DEACTIVATED /
  PRECONDITION_FAILED / FORBIDDEN`。
- **必须当 OrderUnknown**：网络超时/连接中断（老规矩），加上下单响应里的
  `TIMEOUT`（引擎侧超时，单子可能已进撮合流）、以及 HTTP 500/503 的
  `SERVER_ERROR`——单线性命令流意味着 API 层报错不代表引擎没收到。
  撤单的 202 之后查单确认，不是 Unknown 但也不是终态。
- `RESOURCE_NOT_FOUND`：在 orderQuery 语境下**不是"从未接受"**（已成交也 404），
  走历史端点兜底后再定性；其他语境（坏 orderId）才是真不存在。
- `INVALID_SIGNATURE / UNAUTHORIZED`：配置错，直接抛致命，别重试。
- `MAINTENANCE`：retriable=True。

限频（官方 support FAQ，API 文档本体**没写**数字）：

- **按 subaccount 计，不按 IP**："up to **2000 requests per minute** across standard
  REST endpoints"。
- **历史类端点 30 requests per minute**（"REST routes that return time-range data
  such as candles/trades"——klines、trades/history 确定在内；
  `/api/v1/fundingRates` 和 `/wapi/v1/history/*` 是否算入 **UNVERIFIED**，
  按最坏情况处理：资金费历史批量补数据时限速 ≤0.5 req/s 并缓存）。
- 超限 HTTP 429。响应里有没有 Retry-After header UNVERIFIED。
- history.VENUE_LIMITS 建议值：fundingRates 单页上限 10000（页大小用 1000 保守值即可，
  一页就覆盖 41 天）。

---

## WebSocket

- `wss://ws.backpack.exchange`，订阅：
  `{"method":"SUBSCRIBE","params":["bookTicker.BTC_USDC_PERP"]}`，可一次多个流；
  退订 `UNSUBSCRIBE`。所有数据包一层 `{"stream": "...", "data": {...}}`。
- **时间戳微秒**（除 K 线起止）。服务器每 60s 发 WS Ping 帧，120s 没收到 Pong 就断
  （标准协议帧，httpx-ws/websockets 库会自动回，自己写要确认）。服务端下线会主动发
  Close，客户端收到就重连。2026-08-03 起支持 permessage-deflate。
- 盘口流 `bookTicker.<symbol>`（`stream_book_top` 用这个）：

```json
{"e": "bookTicker", "E": 1694687965941000, "s": "SOL_USDC",
 "a": "18.70", "A": "1.000", "b": "18.67", "B": "2.000",
 "u": "111063070525358080", "T": 1694687965940999}
```

  某侧簿空时 `a/A/b/B` 为 **null**，要判空。
- 增量深度 `depth.<symbol>`（另有 `depth.200ms/600ms/1000ms.<symbol>` 聚合档）：
  序列号规则"本条 `U` 必须 == 上条 `u`+1"，断序就重拉 REST 快照。
- 私有流 `account.orderUpdate.<symbol>` / `account.positionUpdate` /
  `account.balanceUpdate`：订阅消息带
  `"signature": ["<base64 公钥>", "<base64 签名>", "<timestamp>", "<window>"]`，
  签名串 `instruction=subscribe&timestamp=..&window=..`。

---

## 坑（汇总，实现时逐条对照）

1. `/api/v1/time` 返回**裸文本毫秒**不是 JSON；`/ping` 返回裸 `pong`。
2. **depth 的 bids 升序，best bid 是 `bids[-1]`**；asks 升序 best ask 是 `asks[0]`。
3. **没有 REST bookTicker**，ticker 端点无 bid/ask，fetch_book_top 用 `depth?limit=5`。
4. 资金费**1 小时结算**（fundingInterval=3600000，逐合约字段），年化 ×24×365。
5. `fundingRateUpperBound/LowerBound` 单位**基点**，markPrices 的 `fundingRate` 是
   **小数**——同一概念两处两种单位，clamp 前除以 10000。
6. `intervalEndTimestamp`、历史单 `createdAt`、fills `timestamp` 都是
   **naive UTC datetime 串**；挂单/下单响应的 `createdAt` 却是毫秒整数。逐端点核对。
7. fundingRates 历史**降序 + offset 从最新往回**，返回前要 reverse 成升序；
   未知符号 200 `[]` 不报错。
8. **`clientId` 是 uint32 整数**不是字符串；字符串 coid 需 crc32 一类确定性映射；
   幂等不能指望服务端去重。fills 历史里它又变 string 类型。
9. `GET /api/v1/order` 只认**在簿**订单，已成交/已撤 404 `RESOURCE_NOT_FOUND`——
   查单必须带历史端点兜底，404 ≠ 从未接受。
10. 撤单/全撤可能返回 **202**（受理未执行），要跟查单确认；orderId/clientId
    二选一，同时给会**签名验不过**。
11. side 是 `Bid/Ask`；postOnly 是独立布尔不进 TIF；TIF 只有 GTC/IOC/FOK。
12. 下单响应**无手续费无均价**，均价自己除，费用走 fills 历史。
13. 所有非 postOnly 单有 **100ms taker 速度带**，撤单豁免。
14. 签名串里 `window` 不发 header 也必须写 5000；布尔要小写；批量下单每单各带
    `instruction=orderExecute&` 前缀。
15. `X-BROKER-ID`/`X-BROKER-KEY` header 存在但**本仓库绝不发**（CCXT 会发 1400，别抄）。
16. 杠杆是账户级 `leverageLimit`，没有逐合约杠杆、没有档位表端点。
17. klines 的 startTime/endTime 是**秒**（其他处毫秒/微秒混用）。
18. 限频历史桶只有 30 req/min，资金费历史补数据要节流 + 缓存。

---

## 主控接线依赖（本次不许改共享文件，列在这里由主控统一落）

- `public_feed.DEFAULT_TAKER = 0.0005`：**已核实正确**（挂牌基础档 taker 0.05%），
  可去掉 UNVERIFIED 注记。
- `history.VENUE_LIMITS`：fundingRates 单页上限 10000；建议页大小 1000、
  节流 ≤0.5 req/s（历史桶 30/min 的防御值）。
- `session.py` 凭据语义确认：api_key = base64 ED25519 公钥（X-API-Key 原文），
  api_secret = base64 32 字节私钥种子（`Ed25519PrivateKey.from_private_bytes` 直接吃）。
  若用户从别处导出 64 字节（seed+pub 拼接）格式，取前 32 字节（CCXT 同款兼容）。
- `normalize_base`：`BTC_USDC_PERP → BTC` 已处理，无千倍前缀特例（kPEPE 之类不存在，
  Backpack 直接上小数 stepSize）。

---

## UNVERIFIED（文档查不到或没试到，不要当成已知）

- 重复 `clientId` 是否被服务端拒绝（幂等语义未文档化）。
- `/api/v1/fundingRates`、`/wapi/v1/history/*` 是否计入 30 req/min 历史桶。
- 429 响应有无 `Retry-After` header。
- `timeInForce` 缺省值（推测 GTC，spec 未写）。
- `Status` 枚举第二个值（Maintenance？spec 折叠了）。
- imfFunction 的精确公式与 leverageLimit 的上限（50x 来自营销页不是 API 文档）。
- `/api/v1/position` 404 的确切触发条件（坏符号 vs 无仓位；无仓位实测预期 `[]`）。
- 全局站费率表是图片，基础档数值取自 EU 文本页 + 第三方交叉，主站若有差异以账户
  `accountQuery` 实测为准（上线前先打一发 accountQuery 对数）。
- IOC 未成交残量的终态是否总是同步返回 `Expired`（架构上强烈暗示，未实测私有端点）。

---

## 参考文档

- https://docs.backpack.exchange/ （Redoc 单页；OpenAPI spec 内嵌于 HTML 的
  `__redoc_state`，本次已整份抽出核对）
- 线上实测（2026-08-05，公开端点）：/api/v1/time、/status、/ping、/markets、
  /market?symbol=BTC_USDC_PERP、/depth、/ticker、/markPrices、/fundingRates（含翻页）
- https://support.backpack.exchange/exchange/api-and-developer-docs/faqs （限频数字）
- https://eu.support.backpack.exchange/exchange/trading-fees （费率档位文本版）
- https://learn.backpack.exchange/articles/hourly-funding-and-real-time-yield
  （1 小时结算与符号约定）
- CCXT `ts/src/backpack.ts` sign()（签名拼法交叉验证；其 X-Broker-Id 注入不抄）
