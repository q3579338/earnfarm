# KuCoin Futures (合约 REST v1 / v2 / v3 混编)

调研时间 2026-08。全部结论来自官方文档 https://www.kucoin.com/docs-new/ 的实际页面
（旧域名 docs.kucoin.com/futures 已 301 到 docs-new），凡是文档没写死的一律标 UNVERIFIED，
不要脑补。文档页是 SPA，WebFetch 抓到的经常是被摘要过的残缺表格，本文里的字段表
是用浏览器渲染后逐字读出来的。

REST base：`https://api-futures.kucoin.com`
（**不是** `api.kucoin.com`。现货/统一账户才是 api.kucoin.com；合约的私有端点、
甚至合约的 trade-fees，全在 api-futures 这个域名上。）

---

## 认证签名

header 全必填。400001 的原文只点名了四个
（"Any of KC-API-KEY, KC-API-SIGN, KC-API-TIMESTAMP, KC-API-PASSPHRASE is missing
in your request header."），`KC-API-KEY-VERSION` 不在这句话里，但文档的 header 表把它列为必填，
所以照样要发。**缺它到底报哪个码 UNVERIFIED**（文档没写），别指望靠错误码定位到它。

| header | 值 | 缺了/错了的后果 |
| --- | --- | --- |
| `KC-API-KEY` | API key 明文 | 400001 缺失 / 400003 不存在 |
| `KC-API-SIGN` | `base64(HMAC_SHA256(secret, prehash))` —— **base64 不是 hex** | 400005 |
| `KC-API-TIMESTAMP` | 毫秒时间戳，十进制字符串 | 400001 / 400002 |
| `KC-API-PASSPHRASE` | `base64(HMAC_SHA256(secret, passphrase))` —— **不是明文** | 400004 |
| `KC-API-KEY-VERSION` | `"2"` 或 `"3"`，**字符串**（去 API 管理页看这把 key 是哪版） | 见上，UNVERIFIED |
| `Content-Type` | `application/json`，POST/DELETE 都要带 | 415000 |
| `X-SITE-TYPE` | 可选，默认 `global`，本仓库不传 | — |

**`KC-API-SIGN` 是 base64 不是 hex，是这家最容易被别家肌肉记忆带偏的一处**：
Bybit/HTX/Gate 都是 hex，抄过去写成 `.hexdigest()` 会稳定收 400005，
而 400005 的 msg 只有 "Signature error -- Please check your signature"，
不会告诉你是编码错了还是拼串错了。

prehash 串 = `timestamp + METHOD + endpoint + body`，**四段直接拼接，没有任何分隔符**：

```
1768982284539GET/api/v1/orders/byClientOid?clientOid=arb-1a2b
1768982284539POST/api/v1/orders{"clientOid":"arb-1a2b","symbol":"XBTUSDTM",...}
```

逐条契约（文档 https://www.kucoin.com/docs-new/authentication 原文）：

- `timestamp` 必须和 `KC-API-TIMESTAMP` header **同一个值**。签一个发另一个是最常见的 400005。
- METHOD 必须全大写。
- `endpoint` = 路径 **加上查询串**，含 `?`。GET/DELETE 的所有参数都要出现在 URL 里，
  body 段为空串 `""`。
- POST 的参数全部进 body（JSON），**"Do not include extra spaces in JSON strings"** ——
  也就是必须 `separators=(",", ":")`。文档还强调"待加密的 body 必须与 Request Body 内容一致"。
- **签名用的 URL 必须是未经 URL-encode 的原文。** 文档给的反例：
  实际发出去的是 `...&passphrase=abc%21%40%2311`，进签名的却要写 `...&passphrase=abc!@#11`。
  这条对我们影响不大（symbol/clientOid 字符集里没有需要转义的字符），但它意味着
  **不能把 dict 丢给 httpx 的 `params=` 让它自己拼**：httpx 会 percent-encode，
  而签名要的是原文，两边一旦不一致就 400005 且完全没有诊断信息。
  正确做法照抄 okx.py：在 `_prepare` 里自己拼查询串塞进 URL，返回空 params。
- 时间戳偏差 > 5 秒直接 400002。错误原文写死了这个数字：
  "KC-API-TIMESTAMP Invalid -- Time differs from server time by more than 5 seconds"。
  **没有 recvWindow 这种可调旋钮**（Bybit 有 recv_window、币安有 recvWindow，这家一个都没有），
  唯一的补救是 `sync_clock()` 顶着 `/api/v1/timestamp` 维护 offset。
  收到 400002 应该先触发一次 `sync_clock()` 再判死——它十有八九是本地时钟漂了。

可直接照抄的实现（这段就是 `_prepare` 的全部内容，注意"签什么发什么"）：

```python
import base64, hashlib, hmac, json, time

def sign(secret: str, ts: str, method: str, endpoint: str, body: str = "") -> str:
    # endpoint 必须含查询串（带 '?'），且是【未 URL-encode 的原文】
    prehash = ts + method.upper() + endpoint + body
    return base64.b64encode(
        hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
    ).decode()

ts = str(int(time.time() * 1000) + clock_offset_ms)   # 同一个字符串既签又发

# GET：参数全进 URL，body 段是空串
endpoint = "/api/v1/orders/byClientOid?clientOid=arb-1a2b"
sig = sign(secret, ts, "GET", endpoint)               # body="" 省略

# POST：参数全进 body，先 dumps 成 str，签它、再用 content= 发它
body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
sig  = sign(secret, ts, "POST", "/api/v1/orders", body)
# httpx: client.post(url, content=body, headers=hdr)   ← 不是 json=payload
```

curl（把上面算出来的值填进去就是线上真实请求）：

```bash
curl -X POST 'https://api-futures.kucoin.com/api/v1/orders' \
  -H 'KC-API-KEY: <key>' \
  -H 'KC-API-SIGN: <base64 hmac-sha256>' \
  -H 'KC-API-TIMESTAMP: 1768982284539' \
  -H 'KC-API-PASSPHRASE: <base64(hmac_sha256(secret, passphrase))>' \
  -H 'KC-API-KEY-VERSION: 3' \
  -H 'Content-Type: application/json' \
  -d '{"clientOid":"arb-1a2b","symbol":"XBTUSDTM","side":"buy","type":"limit","size":1,"price":"100000"}'
```

那一行 `-d` 的字节串，必须与 prehash 里 body 段的字符串**逐字节相同**。
基类 `ExchangeAdapter._request` 走的是 `json=send_body`，序列化交给 httpx——
httpx 的分隔符和键序跟我们签名时用的没有任何保证一致，所以本适配器**必须覆写 `_request`**。
okx.py 已经因为同样的理由覆写过一次，照抄那个模式即可（见文末"主控接线依赖"）。

passphrase 那一步单独说一句：v1 老 key 是明文 passphrase + `KC-API-KEY-VERSION: 1`，
v2/v3 要 HMAC 再 base64。**这个仓库只支持 v2/v3**，默认发 `"3"`（官方 authentication 页
最新示例就是 3，现在新建的 key 也都是 v3），允许用户在凭据里覆写成 `"2"`。
v1 老 key 现在是否还被服务端接受 UNVERIFIED——authentication 页已经不再区分版本、
统一要求 passphrase 做 HMAC+base64——总之不要为它写兼容分支：
明文 passphrase 在 header 里裸奔本身就不该继续支持。

响应信封统一是 `{"code": "200000", "data": ...}`；出错时是 `{"code": "3000xx", "msg": "..."}`。
HTTP 200 也可能带业务错误码，**必须解析 body 的 code，不能只看 status_code**。

---

## 符号模型（算错就是 1000 倍仓位）

命名：`XBTUSDTM` / `XBTUSDCM` / `ETHUSDTM` / `XBTUSDM`。

- **BTC 在 KuCoin 合约里叫 `XBT`**（BitMEX 血统），不是 `BTC`。
  `baseCurrency` 字段返回的就是 `"XBT"`。跟仓库里 `normalize_base()` 的冲突点在这里：
  跨所比价要把 `XBT → BTC` 映射掉，否则 KuCoin 的 BTC 永远配不上其他五家。
  这个映射只有 XBT 一个特例（其余币种正常），但**不能反向硬编码**——
  `XBTUSDTM` 里的 `XBT` 是符号前缀，`settleCurrency` 是 `USDT`。
  反向合约 `XBTUSDM` 的 `settleCurrency` 才是 `XBT`。
- 结尾的 `M` 是 KuCoin 对永续（perpetual）的标记，正向 U 本位是 `<BASE>USDTM`，
  U 本位 USDC 是 `<BASE>USDCM`，币本位反向是 `<BASE>USDM`。
- **筛永续必须看 `type` 字段**：`FFWCSX` = 永续，`FFICSX` = 有交割日的期货。
  只按符号后缀猜会把交割合约当永续算进 carry。另外 `expireDate` 非 null 的也要排除。
- `status` 枚举：`Init / Open / BeingSettled / Settled / Paused / Closed / CancelOnly`，
  只有 `Open` 能下单。
- 新增 `marketType` 字段：`CRYPTO / NASDAQ`。KuCoin 已经上了股票永续，
  这些合约的资金费逻辑和交易时段跟币不一样，carry 扫描默认应该只收 `CRYPTO`。
  同族的 `marketStage` 是 `NORMAL / PRE_MARKET`，盘前合约没有对手所的现货/永续可配，
  留着只会在机会榜上产生配不出对手腿的幽灵行。

筛永续的完整过滤链（四个条件缺一不可，顺序无所谓）：

```python
ok = (d["type"] == "FFWCSX"            # 永续；FFICSX 是有交割日的期货
      and d.get("expireDate") is None  # 双保险：type 对但带交割日的一律排除
      and d.get("marketType", "CRYPTO") == "CRYPTO"   # 排掉股票永续
      and d["status"] == "Open")       # 只有 Open 能下单
```

只按符号后缀 `M` 猜会把交割合约算进 carry —— 那种合约到期就没有资金费了，
年化算出来是纯幻觉。

### 张 vs 币

**下单数量单位是"张"（lot），`size` 是正整数。** 换算关系：

```
1 张 = multiplier 个 base 币          （正向合约，如 XBTUSDTM multiplier=0.001 → 1 张 = 0.001 BTC）
1 张 = 1 USD                          （所有币本位 coin-swap 合约，如 XBTUSDM）
```

文档原文（Get All Symbols → multiplier）："The basic unit of the contract API is lots.
For the number of coins in each lot, please refer to the param multiplier. For example,
for XBTUSDTM, multiplier=0.001 ... There is also a special case. All coin-swap contracts,
such as each XBTUSDM contract, correspond to 1 USD."

所以对外暴露 base 币数量、对内换算成张，走 okx.py 那套硬性约定：

```
lots = int(qty_base / multiplier)     # 正向
lots = int(qty_base * price)          # 反向（1 张 = 1 USD）—— 反向合约本仓库暂不接
```

`lotSize` 是张数的最小递增（XBTUSDTM 是 1），`tickSize` 是价格最小变动，
`maxOrderQty` / `marketMaxOrderQty` 是单笔张数上限。
**盘口 `bestBidSize`、深度数组里的 size、`openInterest`、`volumeOf24h` 全都是"张"**，
算深度容量时必须乘 multiplier 再乘价格才是名义额；漏乘就是把 BTC 深度低估 1000 倍。

映射到 `Instrument`：
`tick_size = tickSize`，`contract_size = multiplier`，
`lot_size = lotSize * multiplier`（对外统一成 base 币），
`funding_interval_s = currentFundingRateGranularity / 1000`。
`min_notional` KuCoin 没有直接给（`lotSize` 是张数不是名义额），只能用
`lotSize * multiplier * markPrice` 估。

---

## REST 端点

### instruments — `GET /api/v1/contracts/active`

Public，weight **3**，无参数，一次返回全部合约。

这是本适配器最划算的一个端点：**合约规格、当前资金费、资金费上下限、资金费周期、
挂牌费率、mark/index/last 价、24h 量，全在这一个响应里**。
`fetch_instruments` / `fetch_carry_rates` / `fetch_funding_limits` 三个抽象方法
都应该共用它的一次缓存结果，不要各拉一遍。

关键字段（逐字取自文档字段表 + 官方 example）：

```json
{
  "symbol": "XBTUSDTM", "rootSymbol": "USDT", "type": "FFWCSX",
  "firstOpenDate": 1585555200000, "expireDate": null, "settleDate": null,
  "baseCurrency": "XBT", "quoteCurrency": "USDT", "settleCurrency": "USDT",
  "maxOrderQty": 1000000, "marketMaxOrderQty": 1000000, "maxPrice": 1000000.0,
  "lotSize": 1, "tickSize": 0.1, "indexPriceTickSize": 0.01, "multiplier": 0.001,
  "initialMargin": 0.008, "maintainMargin": 0.004,
  "maxRiskLimit": 250000, "minRiskLimit": 250000, "riskStep": 125000,
  "makerFeeRate": 2.0E-4, "takerFeeRate": 6.0E-4,
  "isDeleverage": true, "isQuanto": true, "isInverse": false,
  "status": "Open",
  "fundingFeeRate": -1.5E-5, "predictedFundingFeeRate": null,
  "fundingRateGranularity": 28800000,
  "effectiveFundingRateCycleStartTime": 1750147200000,
  "currentFundingRateGranularity": 28800000,
  "fundingRateCap": 0.003, "fundingRateFloor": -0.003,
  "openInterest": "4955514", "volumeOf24h": 6788.072, "turnoverOf24h": 5.98E8,
  "markPrice": 89131.36, "indexPrice": 89148.12, "lastTradePrice": 89126.5,
  "nextFundingRateTime": 3929820, "nextFundingRateDateTime": 1766736000000,
  "maxLeverage": 125, "supportCross": true,
  "buyLimit": 93587.9, "sellLimit": 84674.8,
  "crossRiskLimit": 1.5E9, "orderPriceRange": 0.05,
  "marketStage": "NORMAL", "marketType": "CRYPTO"
}
```

数值字段全是 JSON number（会出现 `2.0E-4` 这种科学计数），**必须 `Decimal(str(v))` 转，
不能 `Decimal(float)`**，否则 `6.0E-4` 会变成一串 0.00059999999...。

`isQuanto` 和 `period` 文档已标 deprecated，不要用。
`takerFixFee` / `makerFixFee` 同样是废字段。

`maxRiskLimit` / `minRiskLimit` / `riskStep` 的字段描述写的是 "unit: XBT"，
但 XBTUSDTM 的取值是 250000 —— 显然是 USDT 计价，文档描述抄错了。
要真实的分档表用下面的 risk-limit 端点，别信这里的单位说明。

### fundingCurrent — `GET /api/v1/funding-rate/{symbol}/current`

Public，weight **2**，symbol 走路径参数。单符号，扫全市场不要用它（用 contracts/active）。

```json
{"code":"200000","data":{
  "symbol":".XBTUSDTMFPI8H","granularity":28800000,"timePoint":1748491200000,
  "value":6.1E-5,"predictedValue":1.09E-4,
  "fundingRateCap":0.003,"fundingRateFloor":-0.003,
  "period":28800000,"fundingTime":1748491200000}}
```

`value` = 当期已确定的费率，`predictedValue` = 下一期预测值。
`fundingTime` 是绝对毫秒时间戳（下一次结算时刻）。
注意 `data.symbol` 返回的可能是 **指数符号**（`.XBTUSDTMFPI8H`）而不是合约符号，
别拿它当 key 回写进以 `XBTUSDTM` 为索引的 dict —— 用请求时的 symbol。（UNVERIFIED：
文档字段表标的是 "Symbol of the contract"，但 example 给的是指数符号，两者矛盾，
实现上按请求 symbol 走最安全。）

### fundingLimits — 同上两个端点任取

`fundingRateCap` / `fundingRateFloor` 在 `contracts/active` 和
`funding-rate/{symbol}/current` **两个端点上都有**，取哪个都行。
基类要求返回 `(cap, floor)` 且 `cap >= floor`，KuCoin 的命名跟这个顺序天然一致
（XBTUSDTM 是 0.003 / -0.003），照抄即可，**不要**自作聪明排序。

### fundingHistory — `GET /api/v1/contract/funding-rates`

Public，weight **5**。参数 `symbol` / `from` / `to`，**三个都是 required**（文档标 required，
不给 from/to 是未定义行为，不要赌它默认返回最近 N 条）。

```json
{"code":"200000","data":[
  {"symbol":"XBTUSDTM","fundingRate":2.1E-4,"timepoint":1702296000000},
  {"symbol":"XBTUSDTM","fundingRate":3.47E-4,"timepoint":1702267200000},
  ...
  {"symbol":"XBTUSDTM","fundingRate":1.32E-4,"timepoint":1700337600000}]}
```

**排序是倒序（新 → 旧）。** 官方 example 首条 1702296000000、末条 1700337600000，
中间严格按 28800000 递减 68 步、共 69 条，正好铺满 `[from, to]` 区间内的每个 8 小时结算点。
这跟基类 `fetch_funding_history` 要的"升序"是反的，适配器必须 `reversed()` 之后再返回。

跟币安的关键区别：**KuCoin 是按 `to` 端对齐的**，只要把 `to` 设成"现在"，
返回的第一条就是最新一次结算，天然满足基类"必须覆盖到窗口末端"的契约；
而币安是从 `startTime` 往后数 N 条，会给你一段停在两周前的假序列。
所以这家不用做"掰回来"的补救，但**翻页方向必须顺着它**：

```
to = now
while to > since_ms:
    page = GET(symbol, from=max(since_ms, to - WINDOW), to=to)
    if not page: break
    to = min(p.timepoint for p in page) - 1     # 减 1ms 防重复取到边界那条
```

字段名注意大小写：**是 `timepoint` 全小写**（私有资金费端点里叫 `timePoint`，
两个端点拼法不一样，抄错就是 KeyError）。

- 单页上限：文档没写。实测样例一次拿到 69 条没被截。UNVERIFIED 是否有硬上限；
  按上面的窗口翻页写就不受影响，窗口取 7~14 天比较稳。
- 是否有历史深度限制（比如只留 6 个月）：文档没写。UNVERIFIED。
  （私有的 `/api/v1/funding-history` 明确写了"仅保留 6 个月、单次范围 ≤ 3 个月"，
  公开这个没有对应说明。）
- 去重：这个端点按时间窗口取、不按页码翻，不会出现 HTX/Bitget 那种"翻页途中跨过结算
  导致整体下移一位"的重复。但上面的循环里 `to = min(timepoint) - 1` 那一步不能省，
  否则每页首尾会重叠一条。

私有版（自己实际付/收了多少钱）是 `GET /api/v1/funding-history`，weight 5，
参数 `symbol/startAt/endAt/reverse/offset/forward/maxCount`，
响应 `{dataList: [...], hasMore: bool}`，字段里 `timePoint` 是驼峰、
`funding` 是实际结算金额（负数=付出）。历史保留 6 个月，单次 startAt~endAt ≤ 3 个月，
只给一端时另一端按 3 个月自动补齐。文档明说**推荐用时间翻页而不是 offset 翻页**。

### bookTop — `GET /api/v1/ticker`

Public，weight **2**。单符号。

```json
{"code":"200000","data":{
  "sequence":1697895100310,"symbol":"XBTUSDM","side":"sell","size":2936,
  "tradeId":"1697901180000","price":"67158.4",
  "bestBidPrice":"67169.6","bestBidSize":32345,
  "bestAskPrice":"67169.7","bestAskSize":7251,
  "ts":1729163001780000000}}
```

- `ts` 是**纳秒**，不是毫秒。除以 1e6 才是 `BookTop.ts_ms`。（全站只有这几个行情端点
  用纳秒，`/api/v1/timestamp` 和订单 `createdAt` 是毫秒，混着用必然出事。）
- `bestBidSize` / `bestAskSize` 是**张**，要乘 multiplier 才是币。
- 全市场一次拉：`GET /api/v1/allTickers`，weight **5**，无参数，返回同结构数组。
  扫 100 个符号时 allTickers（5）比 100 次 ticker（200）便宜 40 倍，必须用它。

### bookDepth — `GET /api/v1/level2/depth20` / `depth100`

Public，weight **5**（两个档位同价）。参数 `symbol`。

```json
{"code":"200000","data":{
  "sequence":1697895963339,"symbol":"XBTUSDM",
  "bids":[[66968,2],[66964.8,25596]],
  "asks":[[66968.1,13501],[66968.7,2032]],
  "ts":1729168101216000000}}
```

`[price, size]`，**price 是 number 不是 string**（跟 ticker 相反，ticker 的价格是 string），
size 是张。`ts` 同样纳秒。

全量快照是 `GET /api/v1/level2/snapshot`，weight **3**（比 depth20 还便宜，
但返回整本盘口、流量大得多）。文档原话是它"uses more server resources and traffic,
and we have strict access rate limit controls"，并且建议用 WS 增量维护。
10bp 带宽的容量估计用 depth100 就够，不要用 snapshot 进轮询。

名义额 = `size × multiplier × price`。**漏乘 multiplier 就把 BTC 的 10bp 带宽低估 1000 倍**，
然后容量闸门会放行一个盘口根本吃不下的规模，滑点惩罚全部失真。

`depth20` 和 `depth100` 是同一个 weight（都是 5），所以**直接用 depth100**，
没有理由为省不存在的配额去要更浅的一档。文档把路径写成 `/api/v1/level2/depth{size}`
但只举了 20 和 100 两个例子，**是否还支持 depth5 之类的档位 UNVERIFIED**——
不要去试探未文档化的 size，拿不到会是 404000 而不是降级返回。

响应里 `bids` / `asks` **缺字段或为空时必须抛错**，不能退化成"深度 0"往下走：
深度 0 会让容量估计得出"这个符号一分钱都吃不下"，机会被静默丢弃；
而真实原因通常是限频或合约暂停，那是要报出来的状态，不是一个合法的深度值。

对照六家的限频差异：ticker(2) < snapshot(3) < depth20/100(5) = allTickers(5)。
weight 差得不大，但 public 池是**按 IP 计**且所有 VIP 等级都只有 2000/30s，
这才是真正的瓶颈——一个符号一次 depth100 就是 5，400 次就打满 30 秒配额。

### placeOrder — `POST /api/v1/orders`

Private，权限 Futures，Futures 池，weight **2**。

必填：`clientOid` / `side` / `symbol`；数量三选一 `size` | `qty` | `valueQty`。

| 参数 | 说明（逐字/摘自文档） |
| --- | --- |
| `clientOid` | 用户自定义订单 ID。**最长 40 字符，只允许数字、字母、下划线 `_`、连字符 `-`**。文档写 "UUID"，但字符集约束才是硬性的。重复报 300018 |
| `side` | `buy` / `sell` |
| `symbol` | `XBTUSDTM` |
| `type` | `limit` / `market`，默认 `limit` |
| `size` | **张数，正整数**。币本位合约只支持 size |
| `qty` | base 币数量，必须是 multiplier 的整数倍；币本位不支持 |
| `valueQty` | quote 币金额（USDT/USDC）；币本位不支持 |
| `price` | limit 必填，string |
| `leverage` | integer，**只在逐仓开仓时需要**；平仓或全仓不用传 |
| `marginMode` | `ISOLATED` / `CROSS`，默认 `ISOLATED` |
| `positionSide` | `BOTH` / `LONG` / `SHORT`。单向模式可省（默认 BOTH），**对冲模式必填** |
| `timeInForce` | `GTC` / `IOC` / `RPI`，默认 `GTC` |
| `postOnly` | bool，`timeInForce=IOC` 时无效 |
| `reduceOnly` | bool，默认 false |
| `closeOrder` | bool，全平；设了它 side/size/leverage 都留空，且**优先级高于 reduceOnly** |
| `forceHold` | bool，强制冻结资金防止减仓单被撮合引擎撤掉 |
| `stp` | `CN` / `CO` / `CB`（`DC` 暂不支持） |
| `remark` | ≤100 utf8 字符 |

响应只给 ID，**不给成交信息**：

```json
{"code":"200000","data":{"orderId":"234125150956625920","clientOid":"5c52e11203aa677f33e493fb"}}
```

所以 `place_order` 拿不到 filled_qty/avg_price，必须紧跟一次 `query_order`
（IOC 单尤其如此）。这跟币安下单直接回成交明细不一样。

`reduce_only` 的语义比其他家啰嗦，文档明确列了四条后果，值得抄进注释：
数量要自己算（不保证一单平完）；会加仓则直接拒；数量超过持仓则该单被撤；
所有活跃减仓单数量之和超过持仓时，价差较劣的那些会被撤或部分撤。

限额：单合约单账户最多 100 个限价单、200 个条件单（超了 300001 / 300004）。

### cancelOrder — `DELETE /api/v1/orders/client-order/{clientOid}?symbol=...`

Private，Futures 池，weight **1**。clientOid 走路径，**symbol 是必填查询参数**。

```json
{"code":"200000","data":{"clientOid":"017485b0-2957-4681-8a14-5d46b35aee0d"}}
```

按交易所单号撤是 `DELETE /api/v1/orders/{orderId}`（weight 1），
返回 `{"cancelledOrderIds":["235303670076489728"]}`。
全撤是 `DELETE /api/v3/orders?symbol=...`（weight **10**，注意是 v3 路径）。

**"撤单成功"只代表请求被接收，不代表已撤。** 文档原话是请求进撮合引擎排队，
真实状态要回查或看推送。所以基类 `resolve_unknown_order` 里
"cancel 是状态终结操作"这条在 KuCoin 上有几毫秒的窗口——
cancel 返回后立刻 query，可能读到还是 open，需要短暂重试而不是当成"没成交"。

### queryOrder — `GET /api/v1/orders/byClientOid?clientOid=...`

Private，权限 General，Futures 池，weight **5**。

```json
{"code":"200000","data":{
  "id":"250444645610336256","symbol":"XRPUSDTM","type":"limit","side":"buy",
  "price":"0.1","size":1,"value":"1","dealValue":"0","dealSize":0,
  "timeInForce":"GTC","postOnly":false,"leverage":"3",
  "clientOid":"5c52e11203aa677f33e493fb",
  "isActive":true,"cancelExist":false,
  "createdAt":1732523858568,"updatedAt":1732523858568,"endAt":null,
  "orderTime":1732523858550892322,
  "settleCurrency":"USDT","marginMode":"ISOLATED","positionSide":"BOTH",
  "avgDealPrice":"0","filledSize":0,"filledValue":"0",
  "status":"open","reduceOnly":false}}
```

三个必须写进注释的坑：

1. **`status` 只有 `open` / `done` 两个值，没有 `canceled`。**
   已撤和已成都是 `done`。区分只能靠 `cancelExist`（true=被撤过）和 `filledSize`：
   ```
   open                              -> "new" / "partial"（filledSize>0 时算 partial）
   done & filledSize == size         -> "filled"
   done & cancelExist & filledSize=0 -> "canceled"
   done & cancelExist & 0<filled<size-> "canceled"（部分成交后撤，成交量以 filledSize 为准）
   ```
   直接把 `status` 映射成 `OrderResult.status` 会让"被拒绝的单"和"全成交的单"
   长得一模一样。
2. **文档把 `filledSize` 和 `filledValue` 的描述写反了**：
   `filledSize` 写的是 "Value of the executed orders"、`filledValue` 写的是
   "Executed order quantity"。看类型就知道谁是谁——`filledSize` 是 integer（张数），
   `filledValue` 是 string（名义额）。`dealSize`/`dealValue` 同理。以类型为准。
3. **响应里没有手续费字段。** `OrderResult.fee` 只能另查
   `GET /api/v1/fills?orderId=...`（weight 5，Futures 池），
   字段 `fee`（正数=成本）、`feeCurrency`、`liquidity`（taker/maker）、
   `feeRate`、`openFeePay` / `closeFeePay`（开/平仓分开记）。
   多花一次 weight-5 请求，所以只在需要精确成本核算时调，别每单都调。

`avgDealPrice` 对反向合约是 `sum(qty)/sum(value)` 反着算的（文档明说），
接反向合约时不能直接当价格用。

按交易所单号查是 `GET /api/v1/orders/{orderId}`。
历史单查询窗口：Classic 账户下 canceled/filled 只能回查 7×24 小时——
超窗的单子查不到不代表"没这单"，对账逻辑不能把"查不到"当成"没成交"。

同一个响应里还混了两种时间单位：`orderTime` 是**纳秒**（`1732523858550892322`），
`createdAt` / `updatedAt` 是**毫秒**（`1732523858568`）。名字都不带单位后缀，
写 `OrderResult.ts_ms` 时挑错字段，时间戳会差 6 个数量级。

### fills（成交明细）— `GET /api/v1/fills?orderId=...`

Private，Futures 池，weight **5**。这是拿到 `OrderResult.fee` / `fee_asset` 的**唯一**途径
（订单查询响应里根本没有手续费字段）。

参数：`orderId`（给了它其余参数可忽略）/ `symbol` / `side` / `type` /
`tradeTypes`（`trade,adl,liquid,settlement`）/ `startAt` / `endAt` /
`currentPage`（默认 1）/ `pageSize`（默认 50，≤1000）。

```json
{"code":"200000","data":{"currentPage":1,"pageSize":50,"totalNum":2,"totalPage":1,
 "items":[{"symbol":"XBTUSDTM","tradeId":"1828954878212","orderId":"284486580251463680",
   "side":"buy","liquidity":"taker","forceTaker":false,
   "price":"86275.1","size":1,"value":"86.2751",
   "openFeePay":"0.05176506","closeFeePay":"0","feeRate":"0.00060","fixFee":"0",
   "feeCurrency":"USDT","fee":"0.05176506","settleCurrency":"USDT",
   "orderType":"market","tradeType":"trade",
   "tradeTime":1740640088244000000,"createdAt":1740640088427}]}}
```

- `fee` **正数 = 成本**，跟本仓库约定一致；`liquidity` 区分 taker/maker；
  `openFeePay` / `closeFeePay` 把开仓费和平仓费分开记。
- 时间又是两种单位：`tradeTime` 纳秒、`createdAt` 毫秒。
- **数据保留 3 个月，但单次查询范围 ≤ 7×24 小时**；只给 `startAt` 时 `endAt` 自动补
  `startAt+24h`，只给 `endAt` 时 `startAt` 自动补 `endAt-24h`。想拉一个月的成交必须自己切窗口。
- 每单都调它就是每单多花 5 weight。纪律：**只在需要精确成本核算时调**，
  日常两腿平衡判断用不着手续费。

### openOrders — `GET /api/v1/orders?status=active`

Private，General，Futures 池，weight **2**。

**`status` 不传默认是 `done`。** 查挂单必须显式传 `status=active`，
漏传会拿到一堆历史成交单、然后 `fetch_open_orders` 报告"有 50 个未成交挂单"，
两腿平衡判断直接崩掉。这是本家最容易踩的一个默认值。

分页信封 `{currentPage, pageSize, totalNum, totalPage, items[]}`，
`pageSize` 默认 50、**上限 1000**（不是 100，某些二手资料写的 100 是错的）。
`status=active` 没有时间范围限制；`status=done` 的 startAt~endAt 不得超过 168 小时。
`items` 里的 `size` / `filledSize` 跟别处一样是**张**，要还原成 base 币仍然得乘 multiplier。
其余参数 `symbol` / `side` / `type` / `startAt` / `endAt` 都是可选过滤器。

### positions — `GET /api/v1/positions`

Private，General，Futures 池，weight **2**。可选 `currency`（按 rootSymbol 过滤，如 USDT）。
单符号是 `GET /api/v1/position?symbol=...`。

```json
{"code":"200000","data":[{
  "id":"500000000001478576","symbol":"XBTUSDTM",
  "crossMode":false,"maintMarginReq":0.004,"riskLimit":500000,"realLeverage":6.0,
  "delevPercentage":0.05,"openingTimestamp":1759458359537,"currentTimestamp":1759458415086,
  "currentQty":1,"currentCost":120.0164,"currentComm":0.07200984,
  "isOpen":true,"markPrice":120009.57,"markValue":120.00957,
  "posInit":20.0027333373,"posMargin":20.0027333373,"posMaint":0.552044022,
  "realisedPnl":-0.07200984,"unrealisedPnl":-0.00683,
  "avgEntryPrice":120016.4,"liquidationPrice":100475.86,"bankruptPrice":100013.67,
  "settleCurrency":"USDT","isInverse":false,"maintainMargin":0.004,
  "marginMode":"ISOLATED","positionSide":"LONG","leverage":6.0,
  "dealComm":-0.07200984,"fundingFee":0,"tax":0}]}
```

- **`currentQty` 是"张"、integer、带符号**（单向模式下负数=空头）。
  映射到 `Position.qty` 要乘 multiplier 变成 base 币。UNVERIFIED：文档没有明文写
  "负数表示空头"，但 `positionSide` 在单向模式恒为 `BOTH`、方向只能靠符号，
  从字段类型（integer 而非 unsigned）反推是这样。对冲模式下有 LONG/SHORT 两条记录，
  这时应以 `positionSide` 为准。
- 有些字段标了 "Only applicable to Isolated Margin"（`riskLimit` / `realLeverage` /
  `posCross` / `posComm` / `posFunding` / `maintMargin` / `withdrawPnl`），
  全仓模式下它们缺失或为 0，**不能无条件 `d["realLeverage"]`**。
- `fundingFee` 是累计资金费（可正可负），`dealComm` 是累计手续费（例子里是负数，
  即"扣掉的"，跟 fills 端点的 `fee` 正数约定相反——两处符号不一致，别混用）。

### setLeverage — `POST /api/v2/changeCrossUserLeverage`

Private，Futures，Futures 池，weight **2**。body `{"symbol":"XBTUSDTM","leverage":"10"}`，
返回 `{"code":"200000","data":true}`。查询是 `GET /api/v2/getCrossUserLeverage`。

`leverage` 在 body 里是 **string**（`"10"`），不是数字——这家的 `leverage` 字段在下单接口里
是 integer、在这个接口里是 string，两处类型不一致，统一按各自端点的文档来。

**注意这只改全仓杠杆。逐仓模式下 KuCoin 没有独立的"设杠杆"接口——
杠杆是下单时 `leverage` 参数带进去的。** 所以 `set_leverage` 的实现要分叉：

```
CROSS    -> POST /api/v2/changeCrossUserLeverage        （真的发一次请求）
ISOLATED -> 只把值存进适配器实例，place_order 时作为 leverage 参数带上
```

这跟其他五家"先 setLeverage 再下单"的模型不一样，别按惯例写：
在逐仓下按惯例去找一个"设杠杆"端点，找到的会是全仓那个，
调用它返回 200、`data:true`，但你的逐仓单杠杆一点没变——**静默失效**，
直到爆仓距离算出来跟实际对不上才会发现。

配套的持仓模式端点：`POST /api/v2/position/switchPositionMode` 切单向/对冲，
`GET /api/v2/position/getPositionMode` 读当前模式。
**没设过模式就下单会报 330011**，所以启动自检里应该先读一次模式，
而不是等第一单被拒。

### leverageTiers — `GET /api/v1/contracts/risk-limit/{symbol}`

Public（不需要签名），Public 池，weight **5**。

```json
{"code":"200000","data":[
 {"symbol":"XBTUSDTM","level":1,"maxRiskLimit":100000,"minRiskLimit":0,
  "maxLeverage":125,"initialMargin":0.008,"maintainMargin":0.004},
 {"symbol":"XBTUSDTM","level":2,"maxRiskLimit":500000,"minRiskLimit":100000,
  "maxLeverage":100,"initialMargin":0.01,"maintainMargin":0.005},
 ... level 12: maxRiskLimit 50000000, maxLeverage 1, initialMargin 1.0}]}
```

天然升序，直接 `[(Decimal(maxRiskLimit), Decimal(maxLeverage)), ...]` 就是基类要的格式。
`maxRiskLimit` 单位是 USDT（文档字段描述写的是 "Upper limit USDT (included)"，
上边界含等号）。

**这个表只对逐仓有效**（文档标题就是 Get Isolated Margin Risk Limit）。
全仓的名义上限在 `contracts/active` 的 `crossRiskLimit` 字段里，
另有 `Get Cross Margin Requirement` 端点给全仓的分档保证金率。
用逐仓表去判全仓的档位边界会得出偏保守的结论——偏保守可以接受，但注释要写明。

### feeRate — `GET /api/v1/trade-fees?symbol=...`

Private，权限 General，Futures 池，weight **3**。**域名是 api-futures**，
不是账户类接口常在的 api.kucoin.com。

```json
{"code":"200000","data":{"symbol":"XBTUSDTM","takerFeeRate":"0.0006","makerFeeRate":"0.0002"}}
```

- **一次只能查一个 symbol**（没有批量参数）。要给 N 个符号取档位就是 N×3 的 weight，
  必须缓存，绝不能进轮询。基类注释已经写死了这条纪律。
- **符号约定：正数 = 成本**，跟本仓库的统一约定一致，**不需要取反**（OKX 才要翻）。
  高 VIP 档的 maker 可能是负数（返佣），此时保持负号即可，正好当收入。
- `fetch_fee_schedule` 每条必须填 `symbol` 字段（KuCoin 是按合约给的，不是账户级一档吃全市场，
  这点跟 OKX / Bitget / 币安合约都不同，调用方按 symbol 匹配）。
  查不到就不要返回这一条——**绝不补 `FeeSchedule(maker=0, taker=0)` 占位**。
  零手续费会让四笔 taker 成本凭空消失、负期望的机会整批翻成正期望，而且**不报任何错**。
  宁可少返回一条，让上层退回默认挂牌价并在界面标"按默认费率估算"。
- 兜底来源就在手边：`contracts/active` 的 `makerFeeRate` / `takerFeeRate`
  就是挂牌基础档（XBTUSDTM 为 `2.0E-4` / `6.0E-4`），公开可得、零额外 weight。
  私有档位拿不到时用它，并在 `note` 里标明这是基础档而非本账户实际档位。
- 平台币（KCS）抵扣：KuCoin 合约的 KCS 折扣状态在合约 API 里读不到。
  UNVERIFIED 是否有端点可查。既然读不到折扣幅度，就绝不擅自打折——
  低估成本比高估危险得多，最多在 `note` 里写一句"未计入 KCS 抵扣"。
- 保证金预冻结（影响可用余额判断，不影响费率本身）：文档说明开仓时系统会预冻结
  "维持保证金 + 开仓费 + 预估平仓费"，减仓时不预冻结；成交后按实际方向扣开仓费或平仓费。
  所以按 `available` 算能开多大仓时，实际能开的比"名义 / 杠杆"要小一截。
  另外**市价单、冰山单、隐藏单恒按 taker 收费**，不管最终是不是提供了流动性。

### serverTime — `GET /api/v1/timestamp`

Public 池，weight **2**。`{"code":"200000","data":1729260030774}` —— 毫秒。
`data` 直接是数字，不是对象。

---

## WebSocket

两步：先 `POST /api/v1/bullet-public`（Public 池，weight **10**，无 body、无签名）
拿 token 和服务器列表，再连 `wss://{endpoint}?token={token}&connectId={uuid}`。

```json
{"code":"200000","data":{
  "token":"2neAiuYvAU61ZDXANAGAsiL4-...",
  "instanceServers":[{"endpoint":"wss://ws-api-futures.kucoin.com/","encrypt":true,
    "protocol":"websocket","pingInterval":18000,"pingTimeout":10000}]}}
```

- **心跳参数来自服务器**：`pingInterval` 18000ms、`pingTimeout` 10000ms。
  不要硬编码 30 秒，要按响应里的值发 `{"id":...,"type":"ping"}`。
- token 有有效期（文档未给具体秒数，UNVERIFIED），重连必须**重新取 token**，
  不能缓存复用上一次的。
- 私有频道用 `POST /api/v1/bullet-private`（需签名）。

BBO 频道 `/contractMarket/tickerV2:{symbol}`，推送样例：

```json
{"topic":"/contractMarket/tickerV2:XBTUSDTM","subject":"tickerV2",
 "data":{"symbol":"XBTUSDTM","sequence":1713516609293,
   "bestBidSize":5044,"bestBidPrice":"86454.5",
   "bestAskPrice":"86454.6","bestAskSize":73,
   "ts":1740641976241000000}}
```

跟 REST `/api/v1/ticker` 一样的两个单位坑照旧成立：`ts` 是**纳秒**、size 是**张**。
`sequence` 用来判断乱序/丢包。

限频：Classic API 单连接 ≤800 并发（private 按 UID、public 按 IP），
单次订阅最多 100 个 topic，合约的单连接 topic 总数无上限（现货是 ≤400），
客户端消息 100 条/10 秒（超了服务端可能直接断连）。
**断线重连前必须 sleep 3~5 秒**——这条是文档因为一次 DDoS 事故专门补上去的，
崩溃循环里的无退避重连会被当成攻击流量。

---

## 资金费周期

- 周期长度看 **`currentFundingRateGranularity`**（毫秒），不是 `fundingRateGranularity`。
  文档明说后者是"配置值"，改了之后要到下一周期才生效：
  "If the current settlement cycle is 8 hours and the next settlement time is 16:00,
  at 11:10 the system changes fundingRateGranularity to 2 hours, then the next
  settlement time will become 12:00. It is recommended to refer to
  effectiveFundingRateCycleStartTime to determine the exact next settlement time."
  也就是说**周期可以在一个周期中途被改短**，缓存下来的 `funding_interval_s` 会过期。
- 下一次结算时刻用 **`nextFundingRateDateTime`**（绝对毫秒时间戳）。
  **`nextFundingRateTime` 是"还剩多少毫秒"的倒计时，不是时间戳**——
  官方 example 里它是 `3929820`（约 65 分钟），而 `nextFundingRateDateTime`
  是 `1766736000000`。字段描述写的是 "Next funding rate time (milliseconds)"，
  望文生义会把 1970-01-01 00:01:05 当成下次结算时间。
  （Get Symbol 页的描述才写对："Milliseconds until next rate settlement"。）
- 符号约定：`fundingFeeRate` 正数 = 多头付空头，跟本仓库统一约定一致，
  **原样存，不要取反**。
- 上下限：`fundingRateCap` / `fundingRateFloor`，XBTUSDTM 是 ±0.003（±0.3%）。
  各合约不同，必须逐个读，不能拿 BTC 的值套全市场。
- KuCoin 在 2026 年多次批量调整过永续的资金费算法和结算间隔（公告有 140 个、
  227 个合约的批量变更），所以**周期不能只在启动时读一次**，
  至少跟着 `contracts/active` 的缓存一起刷新。

---

## 错误码与限频

### 限频体系

按**资源池 + 每接口 weight** 计。文档原文："the weight of this interface will be
deducted and updated every 30s (starting from the arrival time of the user's first request)"
—— 注意是**固定 30 秒窗口**（从该用户第一个请求到达时刻起算），不是滑动窗口。
这意味着窗口边界可以被"攒"：29 秒时打满、跨过重置点再打满，瞬时速率是标称的两倍，
但也意味着重试退避必须等到真正的重置点，早了照样撞。

一共八个池：Unified Account / Spot(含 Margin) / Futures / Management /
Earn / CopyTrading / Broker / Public。本适配器只碰 Futures 和 Public 两个。

| 池 | VIP0 | VIP5 | VIP10 | VIP12 | 计量维度 |
| --- | --- | --- | --- | --- | --- |
| Futures | 2000/30s | 7000/30s | 16000/30s | 20000/30s | UID |
| Spot（含 Margin） | 4000/30s | 16000/30s | 33000/30s | 40000/30s | UID |
| Management | 2000/30s | 7000/30s | 16000/30s | 20000/30s | UID |
| **Public** | **2000/30s** | **2000/30s** | **2000/30s** | **2000/30s** | **IP，完全不随 VIP 提升** |
| Earn / CopyTrading / Broker | 2000/30s | 2000/30s | 2000/30s | 2000/30s | UID |

统一账户（UTA）走另一套口径：VIP0 200/s、VIP5 700/s、VIP12 3000/s。

本适配器用到的每个端点的 weight（实现限频器时按这张表扣，别按请求数）：

| Public 池 | w | Futures 池 | w |
| --- | --- | --- | --- |
| `contracts/active` | 3 | `POST /api/v1/orders`（下单） | 2 |
| `ticker` | 2 | `GET /api/v1/orders`（挂单列表） | 2 |
| `allTickers` | 5 | `DELETE /orders/{orderId}` | 1 |
| `level2/snapshot` | 3 | `DELETE /orders/client-order/{clientOid}` | 1 |
| `level2/depth20` / `depth100` | 5 | `DELETE /api/v3/orders`（全撤） | 10 |
| `funding-rate/{symbol}/current` | 2 | `orders/byClientOid`（查单） | 5 |
| `contract/funding-rates` | 5 | `fills` | 5 |
| `contracts/risk-limit/{symbol}` | 5 | `positions` | 2 |
| `timestamp` | 2 | `trade-fees` | 3 |
| `bullet-public` | **10** | `funding-history`（私有） | 5 |
|  |  | `changeCrossUserLeverage` | 2 |

响应头三件套：`gw-ratelimit-limit` / `gw-ratelimit-remaining` / `gw-ratelimit-reset`
（reset 是**毫秒**倒计时）。适配器应该读 `remaining` 做主动退避，而不是等 429 才反应——
等到 429 时已经被要求"停止访问直到配额重置"，那是整整一个 30 秒窗口的行情空窗。

关键推论：**Public 池按 IP 计、全 VIP 统一 2000/30s**，
所以行情轮询才是唯一真瓶颈，交易配额反而宽裕（这跟大多数所正好相反，
别把"VIP 高所以能猛刷行情"的直觉带过来）。基类那个
`market=8 / trade=6 / risk=6` 的信号量分配对 KuCoin 方向正确，
但 market 那一路必须按 **weight** 而不是按请求数算：
一次 `depth100`(5) 等于两次半 `ticker`(2)，400 次 `depth100` 就打满整个 30 秒配额。
扫多符号必须用 `allTickers`(5) 而不是 N×`ticker`(2)——扫 100 个符号是 5 对 200，便宜 40 倍。

提配额的两条路：Classic 账户下**子账户与主账户的限频互相独立**，
配额不够可以开子账户分摊；机构可发邮件 api@kucoin.com 申请提额。

WebSocket 侧另算：Classic API 单连接 ≤800 并发（private 按 UID、public 按 IP），
单次订阅 ≤100 个 topic，合约单连接的 topic 总数无上限（现货是 ≤400），
客户端消息 100 条/10 秒（超了服务端可能直接断连）。

### 错误码分类（决定异常类型）

必须当 **RateLimited**（可重试）：
- `429000` 总流量限频（HTTP 429），响应头会给重试等待时间。
  服务端过载也会返 429000 但不带个人限频信息，同样重试。
- `200002` "Too many requests in a short period of time, please retry later" ——
  **业务层限频，封 10 秒**。这个码不是 429、HTTP 可能还是 200，光看 status_code 会漏判。
  注意 `200002` **一码两义**：另一义是 "The query scope for Level 3 cannot exceed xxx"，
  那是参数错不是限频。必须看 `msg` 区分，否则会把一个永远不会成功的参数错
  当成限频无限重试下去。
- `1015` Cloudflare 按 IP 限频，封 30 秒。这一层在 KuCoin 网关之前，
  返回的可能不是 JSON，`_parse` 要能容忍非 JSON body。

必须当 **OrderRejected**（明确拒绝，未成交，可安全重试/重发新单）：
- `100004` 订单不在可撤状态（已成/已撤）—— 撤单路径上遇到它就是"已终结"，
  基类 `resolve_unknown_order` 靠这个往下走查单
- `300018` clientOid 重复 —— **这个码是幂等保护生效的证据**，
  说明上一发已经进去了，绝不能换个 ID 重发，必须转去查单
- `300001` / `300004` 活跃单（上限 100）/ 条件单（上限 200）数量超限
- `300005` 超过最大风险限额
- `300006` 平仓价须高于破产价、`300007` 价格劣于强平价
- `300008` / `300013` 市场无对手单
- `300009` 持仓为 0 无法平仓、`300010` 平仓失败
- `300011` 价格不能高于 xxx / `300012` 价格不能低于 xxx（对应 `buyLimit` / `sellLimit`）
- `300016` 杠杆超上限
- `300017` Futures Brawl 持仓不可操作
- `330005` 需要切到全仓、`330011` 需要先用 Switch Position Mode 设持仓模式
- `300003` 余额不足（至少 2 USDT）、`200005` 余额不足、`400100` `account.available.amount`
- 参数类（改对了才能重发，重试同样的参数没有意义）：
  `100001` 参数非法 / `100003` 合约参数非法 / `200003` symbol 非法 /
  `300000` 请求参数非法 / `400100` 参数错误

必须当 **不可重试的 ExchangeError**（配置/权限问题，重试无意义）：
- `400001` 缺 header、`400002` 时间戳偏差 >5s、`400003` key 不存在、
  `400004` passphrase 错、`400005` 签名错、`400006` IP 不在白名单、
  `400007` 权限不足、`415000` Content-Type 不对、`411100` 账号冻结、
  `40010` 地区或身份限制、`404000` URL 不存在
- `100002` systemConfigError、`100005` contractRiskLimitNotExist
- `200001` "The query scope for Level 2 cannot exceed xxx"（深度查询范围超限，
  是参数错不是限频，别跟同族的 `200002` 混起来）
- 其中 `400002` 是唯一一个**应该先补救再判死**的：收到它先触发一次 `sync_clock()`
  重算 offset 再重发一次，因为它十有八九是本地时钟漂了而不是 key 配错了。
  只重试一次就够——如果对完时还错，那就真是配置问题，继续重试只是在刷限频。

必须当 **OrderUnknown**（下单路径专用，绝不能简单重发）：
- 任何 `httpx.TimeoutException` / `NetworkError`
- `500000` Internal Server Error —— 服务端已经收到请求，
  订单可能已进撮合。**当成 OrderUnknown 而不是失败**
- `300002` "Order placement/cancellation suspended" 和
  `300015` "Contract/Funding is undergoing the settlement process" —— 
  UNVERIFIED 是拒绝还是已受理。结算窗口期的下单请求状态不明，
  保守起见按 OrderUnknown 走消解流程，代价只是多一次查单
- `300014` 仓位正在被强平

判定入口：HTTP 200 也可能带业务错误码，**必须先解析 body 的 `code` 字段**，
`"200000"` 才算成功；失败时读 `msg`。

---

## 坑（汇总，实现时逐条对照）

1. **`size` 是张不是币。** multiplier=0.001 的 XBTUSDTM，传 `size=1` 是 0.001 BTC；
   把 0.001 当 size 传会被拒（必须正整数），把 1 当"1 个 BTC"算就是 1000 倍错配。
2. **XBT ≠ BTC**，跨所归一化必须映射。
3. **`nextFundingRateTime` 是倒计时，`nextFundingRateDateTime` 才是时间戳。**
4. **资金费历史是倒序**，且字段是小写 `timepoint`；私有那个是驼峰 `timePoint`。
5. **`GET /api/v1/orders` 不传 status 默认返回 done**，查挂单必须显式 `status=active`。
6. **订单 `status` 只有 open/done**，canceled 要靠 `cancelExist` 推。
7. **文档把 `filledSize`/`filledValue` 的描述写反了**，按类型判断（integer=张数）。
8. **行情端点的 `ts` 是纳秒**，`/api/v1/timestamp` 和订单时间是毫秒。
9. **签名的 URL 要未编码的原文**，所以查询串必须自己拼进 URL，
   不能交给 httpx 的 `params=`；body 必须自己 `json.dumps(separators=(",",":"))`
   然后用 `content=` 发出去，签名和发送共用同一个字符串。
   （okx.py 已经因为同样的理由覆写了 `_request`，照抄那个模式。）
10. **passphrase 要 HMAC 再 base64**，不是明文。
11. **时钟偏差 >5 秒直接 400002，没有 recvWindow 可以放宽。**
12. **public 池按 IP、2000/30s、不随 VIP 提升**，行情轮询是唯一真瓶颈。
13. **逐仓没有独立的设杠杆接口**，杠杆随下单参数走；只有全仓能用
    `/api/v2/changeCrossUserLeverage`。
14. **下单响应不带成交信息**，IOC 单必须跟一次查单。
15. **手续费不在订单里**，要 `GET /api/v1/fills?orderId=`；
    而且 positions 的 `dealComm` 是负数、fills 的 `fee` 是正数，符号不一致。
16. **`type=FFICSX` 是交割合约**，扫 carry 时要按 `type == "FFWCSX"` 且
    `expireDate is None` 过滤；`marketType` 还要排除 `NASDAQ` 股票永续。
17. **数值字段是 JSON number 且带科学计数**（`6.0E-4`），
    必须 `Decimal(str(v))`，不能 `Decimal(float(v))`。
18. **撤单只是"已受理"**，不是"已撤"，消解未知订单时 cancel 后要给几十毫秒再查。
19. **`200002` 是业务层限频但可能不是 HTTP 429**，只看 status_code 会漏判。
20. **`clientOid` 字符集受限**（数字/字母/`_`/`-`，≤40）。
    如果仓库的 client_order_id 生成器带了 `.` 或 `:`，在这家会被拒。
21. **`KC-API-SIGN` 是 base64，不是 hex。** 另外五家里 Bybit/HTX/Gate 都是 hex，
    肌肉记忆写成 `.hexdigest()` 会稳定收 400005，而 400005 的 msg 不区分
    "编码错"和"拼串错"，能查半天。
22. **深度响应缺 `bids`/`asks` 要抛错，不能退化成深度 0。**
    深度 0 会让容量闸门静默丢弃这个机会，而真实原因通常是限频或合约暂停——
    那是要报出来的状态，不是一个合法的深度值。
23. **`fills` 单次查询范围 ≤ 7×24 小时**（数据保留 3 个月）。
    想拉一个月的成交必须自己切窗口；只给一端时另一端按 24h 自动补齐，
    以为"给个 startAt 就能拉到今天"会静默少一大截数据。
24. **`orderTime` 纳秒 vs `createdAt` 毫秒在同一个订单对象里并存**，
    字段名都不带单位后缀。挑错字段时间戳差 6 个数量级。
25. **`200002` 一码两义**（限频 / Level3 查询范围超限），必须看 `msg` 区分，
    否则会把一个永远不会成功的参数错当限频无限重试。

---

## 主控接线依赖（本次不许改共享文件，列在这里由主控统一接）

1. **`Venue.KUCOIN` 枚举成员此刻不存在。**
   需要在 `carryfarm/models.py` 的 `Venue` 枚举里加 `KUCOIN = "kucoin"`
   （值取小写 venue 名，与现有六家一致），否则 `carryfarm/exchanges/kucoin.py` 里的
   `venue = Venue.KUCOIN` 直接 ImportError，模块都导不进来。
2. **`ExchangeAdapter._request` 必须被本适配器覆写。**
   基类走 `json=send_body` 把序列化交给 httpx，与 KuCoin"被签的 body 字符串必须与
   实际发出的字节完全一致"的要求冲突（见"认证签名"节）。okx.py 已因同样理由覆写过，
   照抄那个模式即可——**这是适配器自己的事，不需要改 base.py**。
   如果主控希望收敛这个模式（比如在 base.py 提供一条 `_dumps` + `content=` 的可选路径），
   那属于 base.py 的改动，由主控决定，本文不做假设。
3. **返佣码：不接。** `config.py` 的 `BrokerCodes` 与 `ui/app.py` 的返佣码输入框都硬编码六家，
   KuCoin 天然不出现在里面，这正是想要的结果。`KuCoinAdapter.__init__` 照常接下
   `broker_code` 参数但**什么都不做**（用户明确要求不加返佣码），
   两个共享文件一行都不用动。
4. **`clientOid` 字符集要跟仓库的 ID 生成器对表。** KuCoin 只收数字/字母/`_`/`-`，≤40 字符。
   如果现有生成器会产出 `.` 或 `:`（常见于时间戳拼接），在这家会直接 300018 之外的参数错。
   这条要么由主控统一收紧生成器，要么由本适配器在 `_prepare` 里做一次字符替换——
   建议前者，因为字符替换会破坏 client id 的全局唯一性假设。

---

## UNVERIFIED（文档查不到或自相矛盾，不要当成已知）

- `/api/v1/contract/funding-rates` 的单页返回条数上限。样例 69 条未被截断，
  上限值文档未给。实现按时间窗口翻页规避。
- 该端点的历史深度（能回溯多久）。私有的 funding-history 明写 6 个月，公开的没写。
- `funding-rate/{symbol}/current` 响应里 `data.symbol` 到底是合约符号还是指数符号
  —— 字段表说是合约符号，example 给的是 `.XBTUSDTMFPI8H`。
- `currentQty` 负数表示空头：从字段类型和 `positionSide=BOTH` 反推，文档无明文。
- 下单 `timeInForce` 是否接受 `FOK`：下单页只列 `GTC/IOC/RPI`，
  查单页却列 `GTC/GTT/IOC/FOK`。以下单页为准，不要发 FOK。
- `RPI` 这个 TIF 的确切语义（Retail Price Improvement？）文档没展开。
- WebSocket token 的有效期秒数。
- KCS 手续费抵扣的状态/幅度能否从 API 读到。
- `300002`（下单撤单暂停）与 `300015`（结算中）是"已拒绝"还是"已受理"。
- v1 版 API key（明文 passphrase、`KC-API-KEY-VERSION: 1`）现在是否还被接受。
- `maxRiskLimit` 在 `contracts/active` 里标 "unit: XBT" 但取值明显是 USDT
  —— 判定为文档笔误，未找到勘误说明。
- 合约 `trade-fees` 是否支持一次传多个 symbol（文档只给单个示例，参数表也只有一个 `symbol`，
  无批量说明）。按单符号实现并缓存。
- `Get Part OrderBook` 除了 `depth20` / `depth100` 是否还支持别的档位——
  路径写成 `/api/v1/level2/depth{size}`，但文档只举了这两个例子（已复核原页面，确认只有这两例）。
- 全仓模式下的杠杆分档表（与逐仓 `risk-limit` 表的差异）：只知道 `contracts/active` 有
  `crossRiskLimit`（全仓名义硬上限），另有 `Get Cross Margin Requirement` 端点给全仓分档保证金率，
  该端点的字段结构本次未逐字读取。`fetch_leverage_tiers` 先用逐仓表（偏保守）。

---

## 参考文档

- https://www.kucoin.com/docs-new/introduction
- https://www.kucoin.com/docs-new/authentication
- https://www.kucoin.com/docs-new/rate-limit
- https://www.kucoin.com/docs-new/error-code/futures
- https://www.kucoin.com/docs-new/rest/futures-trading/introduction
- https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-all-symbols
- https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-symbol
- https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-ticker
- https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-all-tickers
- https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-full-orderbook
- https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-part-orderbook
- https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-server-time
- https://www.kucoin.com/docs-new/rest/futures-trading/funding-fees/get-current-funding-rate
- https://www.kucoin.com/docs-new/rest/futures-trading/funding-fees/get-public-funding-history
- https://www.kucoin.com/docs-new/rest/futures-trading/funding-fees/get-private-funding-history
- https://www.kucoin.com/docs-new/rest/futures-trading/orders/add-order
- https://www.kucoin.com/docs-new/rest/futures-trading/orders/cancel-order-by-clientoid
- https://www.kucoin.com/docs-new/rest/futures-trading/orders/cancel-order-by-orderld
- https://www.kucoin.com/docs-new/rest/futures-trading/orders/cancel-all-orders
- https://www.kucoin.com/docs-new/rest/futures-trading/get-stop-order-by-clientoid
- https://www.kucoin.com/docs-new/rest/futures-trading/orders/get-order-list
- https://www.kucoin.com/docs-new/rest/futures-trading/orders/get-trade-history
- https://www.kucoin.com/docs-new/rest/futures-trading/positions/get-position-list
- https://www.kucoin.com/docs-new/rest/futures-trading/positions/get-isolated-margin-risk-limit
- https://www.kucoin.com/docs-new/rest/futures-trading/positions/modify-cross-margin-leverage
- https://www.kucoin.com/docs-new/rest/futures-trading/positions/switch-position-mode
- https://www.kucoin.com/docs-new/rest/account-info/trade-fee/get-actual-fee-futures
- https://www.kucoin.com/docs-new/websocket-api/base-info/get-public-token-futures
