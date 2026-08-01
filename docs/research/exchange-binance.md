# Binance (币安) — USDⓈ-M 永续 fapi / 统一账户 papi / 现货 api

## 返佣码机制

### mechanism

客户端订单ID前缀机制,不是 header 也不是独立请求体字段。把返佣码拼到下单请求的 newClientOrderId 参数最前面,格式固定为 `x-` + LinkID。官方原话(Binance Support FAQ a78a065d0c4846aaa1af474d8e712ab9):If your Link ID is "ABC123", the "newClientOrderId" of the order endpoint should start with "x-ABC123" when you place an order. The length of newClientOrderId should be under 36 characters.

三套 API 各自的落点:
- 现货 POST /api/v3/order → newClientOrderId
- USDⓈ-M 合约 POST /fapi/v1/order → newClientOrderId,官方 pattern 原文:`^[\.A-Z\:/a-z0-9_-]{1,36}$`
- 批量 POST /fapi/v1/batchOrders → batchOrders 数组里每个子订单都要各自带前缀(每单独立 newClientOrderId,同 36 字符规则),不是整批带一次
- 统一账户 POST /papi/v1/um/order → newClientOrderId。注意 papi 的规则更严:同页系的 /papi/v1/cm/order 文档明写 `^[\.A-Z\:/a-z0-9_-]{1,32}$`(32 不是 36)。我没能直接抓到 UM 那一页的 regex 原文,保守按 32 处理。另外:papi 路径下 broker/Link 返佣是否被统计,官方文档没有任何说明——查不到,别假设能返。

字符集限制(合约):只允许 A-Z a-z 0-9 以及 . : / _ -。空格、#、@、+、% 全部非法,拼接前必须过滤。大小写敏感,LinkID 原样照抄不要 upper()。

长度预算:实测主流客户端(CCXT ts/src/binance.ts 的 options.broker)用的 Binance broker id 都是 8 位大小写混合字母数字,例如 spot/margin = x-TKT5PX2F、swap/future(U本位)= x-cvBPrNm9、inverse/delivery/option = x-xcKtGhcu。也就是前缀本身占 10 字符,fapi 还剩 26 字符给你自己的 order tag,papi 只剩 22。工程上按 22 设计自己的 tag 编码最安全。

绑定式替代方案(非订单级):POST /fapi/v1/apiReferral/userCustomization(签名,X-MBX-APIKEY),参数 customerId(必填,须全局唯一)、brokerId(必填)、timestamp、recvWindow;GET 同路径可回查。这是把某个客户账号绑到某个 broker 做返佣归账,和逐单前缀是两条并行的归属通道。

### example

// USDⓈ-M 合约下单,brokerPrefix 由用户在配置里填(可留空)
// 配置示例: BROKER_PREFIX = "x-cvBPrNm9"

const CID_RE = /^[.A-Z:/a-z0-9_-]{1,36}$/;

function buildClientOrderId(brokerPrefix, tag) {
  // tag 例如 "L1a" + 毫秒时间戳36进制,自己保证唯一
  const raw = (brokerPrefix ? brokerPrefix : '') + tag;
  const s = raw.slice(0, 36);              // fapi 36;papi 换成 32
  if (!CID_RE.test(s)) throw new Error('bad newClientOrderId: ' + s);
  return s;
}

const cid = buildClientOrderId('x-cvBPrNm9', 'h' + Date.now().toString(36));
// => "x-cvBPrNm9h1m2n3o4p"  (19 字符,合法)

const params = new URLSearchParams({
  symbol: 'BTCUSDT',
  side: 'BUY',
  type: 'LIMIT',
  timeInForce: 'IOC',
  quantity: '0.010',
  price: '61234.50',
  newClientOrderId: cid,
  newOrderRespType: 'RESULT',
  recvWindow: '5000',
  timestamp: String(Date.now()),
});
const sig = crypto.createHmac('sha256', SECRET).update(params.toString()).digest('hex');
params.append('signature', sig);

await fetch('https://fapi.binance.com/fapi/v1/order', {
  method: 'POST',
  headers: {
    'X-MBX-APIKEY': API_KEY,
    'Content-Type': 'application/x-www-form-urlencoded',
  },
  body: params.toString(),
});

// 批量:每单各自带前缀
// batchOrders=[{...,"newClientOrderId":"x-cvBPrNm9L1..."},{...,"newClientOrderId":"x-cvBPrNm9S1..."}]

### howToGet

只能走 Binance Link(原 Link & Trade / API Link ID)项目申请,申请入口是 binance.com/en/link 页面的 contact-us 表单,官方说法:Fill out the form here and you will receive an email with the unique Link ID and rebate rate。提交后客户经理 72 小时内联系。

关键现实:这是机构项目,不是散户能自助开的。官方门槛写明 Link partner 需为机构、用户数不少于 20,000(聚合交易平台、券商/债券商、股票交易平台等),且月交易量不低于 1000 万。散户个人拿不到 Link ID。

所以对这个工具的产品设计:brokerCode 应当做成「可选配置项,默认空」。真正会填的是把工具二次分发给自己客户的机构用户,或工具作者自己拿到 Link ID 后作为默认值内置。普通用户手上有的通常是注册时用的 referral code(推荐码)——那是账户级、注册时一次性绑定的,和 API 下单完全无关,不能塞进 newClientOrderId。这两个概念要在 UI 上区分清楚,否则用户填错。

### canBeEmpty

不填完全不影响交易:newClientOrderId 本身是可选参数,不传时 Binance 自动生成;传了但没有 x-LinkID 前缀,订单一样正常提交、撮合、成交,费率也不变。唯一后果是这笔成交不会被归属到任何 Link partner,返佣链路断掉。

反过来,填了也不一定有返佣。官方返佣判定是双条件:rebateWorking && ifNewUser 同时为 true 才有。rebateWorking=true 表示该用户没有绑定其他推荐码且 VIP 等级低于 VIP 3(不含 VIP3);ifNewUser=true 表示该用户是在你成为 Binance Link 之后才在 binance.com 注册的。老用户、已绑他人推荐码的用户、VIP3 及以上,前缀写对了也不返。归属状态可用 GET /sapi/v1/broker/rebate/futures/recentRecord 和 GET /sapi/v1/broker/rebate/recentRecord 查(响应里带 rebate status 字段)。

验证前缀是否生效的土办法:合约的 income history(GET /fapi/v1/income)里出现 API rebate 类型的流水,就说明 Link ID 在工作;合约返佣打到合约钱包,现货返佣打到现货钱包。

还有一个必须防的坑:newClientOrderId 在未成交订单里必须唯一——「Orders with the same newClientOrderID can be accepted only when the previous one is filled, otherwise the order will be rejected」。500ms 收敛循环高频改单时,前缀吃掉字符预算后剩下的唯一性位数不够会直接撞 ID 被拒。

## 认证签名

HMAC SHA256(也支持 RSA PKCS#8;现货另支持 Ed25519)。

待签字符串构造(官方定义 totalParams):totalParams = query string 拼接 request body,即把 URL 上的 query 部分和 body 部分按出现顺序原样字符串相连,对这个结果做 HMAC-SHA256,key = secretKey。原文:The HMAC SHA256 signature is a keyed HMAC SHA256 operation. Use your secretKey as the key and totalParams as the value for the HMAC operation.

实操要点:
1. 参数顺序无所谓,但签名用的字符串必须和最终真正发出去的字节完全一致(URL 编码后的形式)。最稳的做法是用同一个 URLSearchParams/字典序列化一次,拿它 sign,再拿它发。不要签一份、发另一份。
2. signature 必须放在 query string 或 body 的最末尾。
3. GET 参数放 query string;POST/PUT/DELETE 参数可放 query string 或 body(Content-Type: application/x-www-form-urlencoded)。同名参数两边都出现时,官方原文:the query string parameter will be used。混放容易签错,建议 POST 全部走 body。
4. 签名结果 hex,大小写不敏感;API key 大小写敏感。

Header:X-MBX-APIKEY: <apiKey>。只有这一个认证 header,签名不走 header 而是走参数。

时间戳:参数名 timestamp,int64 毫秒。recvWindow 可选,毫秒,默认 5000,最大 60000。
服务端校验逻辑(官方原文的两条):请求被拒当 timestamp < serverTime + 1000 不成立(即客户端时间领先服务器超 1000ms)或 serverTime - timestamp > recvWindow。换句话说:允许最多领先 1 秒,允许最多落后 recvWindow 毫秒。
错误码:-1021 INVALID_TIMESTAMP,消息两种 "Timestamp for this request is outside of the recvWindow." / "Timestamp for this request was 1000ms ahead of the server's time.";-1022 INVALID_SIGNATURE "Signature for this request is not valid."

建议:启动时和周期性(每几分钟)用 GET /fapi/v1/time 校准本地与服务器偏移量,下单时 timestamp = Date.now() + offset。500ms 收敛循环不要把 recvWindow 调到 60000 掩盖时钟漂移——那会让延迟到达的撤改单在你以为超时之后才生效。recvWindow 保持 5000 并主动校时更安全。

papi(https://papi.binance.com)认证规则完全相同:X-MBX-APIKEY + HMAC SHA256(totalParams),timestamp 毫秒,recvWindow 默认 5000 最大 60000,同样支持 RSA PKCS#8(上传公钥换 API key)。

## REST 端点

### fundingCurrent

GET https://fapi.binance.com/fapi/v1/premiumIndex —— 权重:带 symbol 为 1,不带 symbol 为 10(返回全市场,套利扫描用这个)。参数:symbol(可选)。响应字段:symbol, markPrice, indexPrice, estimatedSettlePrice, lastFundingRate(注意语义是「最近一次/当前预测」的资金费率,字符串), interestRate, nextFundingTime(int64 毫秒,下次结算时刻), time。
注:lastFundingRate 是实时滚动的预测值,不是已结算值;要拿已结算的历史值用 /fapi/v1/fundingRate。做 carry 决策时用 premiumIndex 的 lastFundingRate + nextFundingTime 判断距结算还有多久、当前预测费率多少。

### fundingHistory

GET https://fapi.binance.com/fapi/v1/fundingRate —— 参数:symbol(可选,不传返回全市场)、startTime(int64 ms)、endTime(int64 ms)、limit(int64,默认 100,最大 1000)。
一次最多 1000 条。按 8 小时周期算,单次 1000 条 ≈ 333 天;4 小时周期的品种 ≈ 166 天。
回溯多久:官方文档没有写明历史上限,实测可回溯到该合约上线之日(exchangeInfo 的 onboardDate)。「能回溯多久」这一点文档查不到硬性说明,不要当成保证——按 onboardDate 做边界、分页拉取。
分页语义(官方注):不传 startTime/endTime 时返回最近的记录(文档在不同版本里写作「最近 200 条」和「最近 limit 条」,存在不一致,以实际返回为准,建议显式传 limit);若 startTime~endTime 之间的数据条数超过 limit,返回从 startTime 起、截到 limit 为止的数据。所以正确分页是:固定 limit=1000,每轮把 startTime 推进到上一批最后一条 fundingTime+1,向前滚动。结果按时间升序返回。
限频:官方注明与 GET /fapi/v1/fundingInfo 共享 500 次/5 分钟/IP 的独立配额。文档另一处又标 Request Weight = 0,两处说法不一致(dev.binance.vision 上有开发者提出这个矛盾且官方未澄清)。按 500/5min/IP 保守节流。
响应字段:symbol, fundingRate(字符串), fundingTime(int64 毫秒,结算时刻), markPrice。

实际到账的资金费(带手续费口径,做 PnL 归因用):GET /fapi/v1/income?incomeType=FUNDING_FEE —— 权重 30。参数 symbol / incomeType / startTime / endTime / page / limit(最大 1000)/ timestamp。不传 startTime、endTime 时返回最近 7 天;整个接口只保留最近 3 个月数据。要长期归因必须自己落库。

### placeOrder

POST https://fapi.binance.com/fapi/v1/order —— SIGNED,权重:10 秒下单限额计 1、1 分钟下单限额计 1、IP 权重 0(下单不吃 IP 权重,这点对 500ms 收敛循环很关键)。
必填:symbol, side(BUY/SELL), type(LIMIT/MARKET/STOP/STOP_MARKET/TAKE_PROFIT/TAKE_PROFIT_MARKET/TRAILING_STOP_MARKET), timestamp。
条件必填:LIMIT 需 timeInForce+quantity+price;MARKET 需 quantity。
可选关键项:positionSide(BOTH 默认 / LONG / SHORT,Hedge 模式必填)、timeInForce(GTC/IOC/FOK/GTX/GTD/RPI,默认 GTC)、reduceOnly(true/false,Hedge 模式下不可用)、newClientOrderId(pattern ^[\.A-Z\:/a-z0-9_-]{1,36}$,返佣码就塞这里)、newOrderRespType(ACK 默认 / RESULT——对冲腿建议 RESULT,少一次查单往返)、priceMatch(OPPONENT / OPPONENT_5 / OPPONENT_10 / OPPONENT_20 / QUEUE / QUEUE_5 / QUEUE_10 / QUEUE_20,不能与 price 同时传)、selfTradePreventionMode(EXPIRE_TAKER/EXPIRE_BOTH/EXPIRE_MAKER,默认 EXPIRE_MAKER,仅在 IOC/GTC/GTD 下生效)、goodTillDate(GTD 必填,须 > 当前+600s 且 < 253402300799000)、recvWindow(最大 60000)。
响应:orderId, clientOrderId, symbol, side, positionSide, status, type, origType, price, avgPrice, origQty, executedQty, cumQty, cumQuote, timeInForce, updateTime, reduceOnly, closePosition, priceProtect, workingType, priceMatch, selfTradePreventionMode, goodTillDate。

批量下单(两腿同时打的场景):POST /fapi/v1/batchOrders —— 单批最多 5 单,权重:10 秒下单限额计 5、1 分钟下单限额计 1、IP 权重 5。batchOrders 为对象数组,放在请求体。官方注:批量单并发处理,撮合顺序不保证;返回内容顺序与传入顺序一致。跨腿对冲不要依赖批量的顺序性。
改单:PUT /fapi/v1/order(仅 LIMIT,改价改量,保留队列位置的能力有限)。撤单:DELETE /fapi/v1/order,DELETE /fapi/v1/allOpenOrders,DELETE /fapi/v1/batchOrders。断线保护:POST /fapi/v1/countdownCancelAll(死人开关,delta 中性工具强烈建议开)。

### positions

GET https://fapi.binance.com/fapi/v3/positionRisk —— SIGNED,权重 5。参数:symbol(可选)、timestamp、recvWindow(最大 60000)。
响应字段:symbol, positionSide, positionAmt(带符号,空头为负), entryPrice, breakEvenPrice, markPrice, unRealizedProfit, liquidationPrice, leverage, marginType(cross/isolated), isolatedMargin / isolatedWallet, isAutoAddMargin, notional, maxNotionalValue, updateTime, isolatedCreatedAt, adl。
旧版仍在:GET /fapi/v2/positionRisk。新代码用 v3。
注意:v3 默认只返回有持仓/有风险敞口的条目,不传 symbol 时不会把全部合约都吐出来——不要靠它来枚举合约清单。
统一账户:GET /papi/v1/um/positionRisk。
每 500ms 的收敛循环不要轮询这个接口(权重 5,2400/min 的 IP 配额撑不住 500ms 全量轮询)。正确做法是走 user data stream 的 ACCOUNT_UPDATE 事件维护本地持仓,REST 只做定期对账(比如每 30s 一次)。

### balance

GET https://fapi.binance.com/fapi/v3/balance —— SIGNED,权重 5。参数:timestamp, recvWindow。
响应(数组):accountAlias, asset, balance, crossWalletBalance, crossUnPnl, availableBalance, maxWithdrawAmount, marginAvailable(bool,多资产模式下能否作为保证金), updateTime。
更全的账户视图:GET /fapi/v3/account(含 totalWalletBalance / totalUnrealizedProfit / totalMarginBalance / totalInitialMargin / totalMaintMargin / availableBalance / maxWithdrawAmount 以及 assets[] 和 positions[])。
旧版 GET /fapi/v2/balance 仍可用(权重 5,同字段)。
统一账户:GET /papi/v1/balance,GET /papi/v1/account。

### setLeverage

POST https://fapi.binance.com/fapi/v1/leverage —— SIGNED,IP 权重 1。参数:symbol(必填)、leverage(必填,int,1~125,实际上限取决于该 symbol 的 leverageBracket 和当前名义仓位)、timestamp、recvWindow。响应:leverage, maxNotionalValue, symbol。
配套:
- POST /fapi/v1/marginType —— 参数 symbol、marginType(ISOLATED / CROSSED),IP 权重 1,响应 {code, msg}。有持仓或有挂单时改会失败。
- POST /fapi/v1/positionSide/dual —— 参数 dualSidePosition(true=Hedge 双向 / false=One-way 单向)。整个账户级别的开关,不是按 symbol。delta 中性两腿如果在同一个 Binance 账户内跑同一 symbol 的多空,必须开 Hedge 模式,否则会互相抵消。
- POST /fapi/v1/multiAssetsMargin —— 多资产保证金模式开关。
- GET /fapi/v1/leverageBracket —— 查每个 symbol 的杠杆分层(bracket / initialLeverage / notionalCap / notionalFloor / maintMarginRatio / cum),算强平价和最大可开必须用它。
- GET /fapi/v1/symbolConfig —— 查每个 symbol 当前生效的 marginType / leverage / maxNotionalValue,启动时用它核对而不是盲目重设。
- GET /fapi/v1/accountConfig —— 查账户级配置(feeTier / dualSidePosition / multiAssetsMargin 等)。
统一账户:POST /papi/v1/um/leverage。

### instruments

GET https://fapi.binance.com/fapi/v1/exchangeInfo —— 公开接口,IP 权重 1。无参数(可选 symbol)。
顶层:timezone, serverTime, rateLimits[](含 REQUEST_WEIGHT / ORDERS 的 intervalNum / interval / limit,启动时读它拿到真实限频而不是写死), exchangeFilters[], assets[], symbols[]。
symbols[] 每项:symbol(如 BTCUSDT)、pair(如 BTCUSDT)、contractType(PERPETUAL / CURRENT_QUARTER / NEXT_QUARTER / 空)、deliveryDate、onboardDate(上线时间,毫秒,用来定资金费历史回溯边界)、status(TRADING / PENDING_TRADING / SETTLING / CLOSE 等)、baseAsset、quoteAsset、marginAsset、pricePrecision、quantityPrecision、baseAssetPrecision、quotePrecision、underlyingType、underlyingSubType[]、settlePlan、triggerProtect、liquidationFee、marketTakeBound、maxMoveOrderLimit、orderTypes[]、timeInForce[]、filters[]。
filters[] 关键项(全部是字符串,必须用 Decimal 解析):
- PRICE_FILTER: minPrice, maxPrice, tickSize —— 挂单价必须是 tickSize 的整数倍
- LOT_SIZE: minQty, maxQty, stepSize —— 限价单数量必须是 stepSize 的整数倍
- MARKET_LOT_SIZE: minQty, maxQty, stepSize —— 市价单单独一套,通常 maxQty 比 LOT_SIZE 小得多,市价平仓大单会被这条拦住
- MIN_NOTIONAL: notional —— 最小名义价值,USDⓈ-M 上普遍是 5(USDT)
- PERCENT_PRICE: multiplierUp, multiplierDown, multiplierDecimal —— 相对 markPrice 的挂单价上下界
- MAX_NUM_ORDERS: limit —— 单 symbol 最大挂单数
- MAX_NUM_ALGO_ORDERS: limit —— 单 symbol 最大条件单数
重要:pricePrecision / quantityPrecision 是「小数位数上限」,tickSize / stepSize 是「步长」,两者都要满足,不能只看其中一个。

### feeRate

GET https://fapi.binance.com/fapi/v1/commissionRate —— SIGNED(需 X-MBX-APIKEY),IP 权重 20。参数:symbol(可选,不传则按账户默认)、timestamp、recvWindow(最大 60000)。响应:symbol, makerCommissionRate, takerCommissionRate(均为小数字符串,如 "0.000200")。
权重 20 很贵,启动时查一次缓存住,不要放进循环。
账户等级/费率档位另可从 GET /fapi/v3/account 的 feeTier 字段拿,或 GET /fapi/v1/accountConfig。
BNB 抵扣开关会影响实际扣费:GET /fapi/v1/feeBurn 查、POST /fapi/v1/feeBurn 开关。算套利净收益时如果开了 BNB 抵扣,实际费率比 commissionRate 返回值低一档,别忘了这一项。

## WebSocket

【重大变更,务必注意】Binance 在 2026-03-06 做了 USDⓈ-M 合约 WebSocket 系统升级,引入 /public /market /private 三条分流入口。旧版 URL 已于 2026-04-23 永久下线。现在(2026-08)必须用带路由路径的地址,老代码直连 wss://fstream.binance.com/ws/xxx 会拿不到 /market 类数据。官方原文:After the upgrade, any connections not migrated will ONLY be able to receive data from wss://fstream.binance.com/public。

Base:wss://fstream.binance.com(测试网 wss://fstream.binancefuture.com,另有 wss://demo-fstream.binance.com 的说法,两者我看到文档里都出现过,上测试网前自己 ping 一下确认)

三条路由:
- /public —— 高频公开行情:<symbol>@bookTicker、!bookTicker、<symbol>@depth、<symbol>@depth<levels>(支持 @500ms / @100ms)
- /market —— 常规行情:<symbol>@aggTrade、<symbol>@markPrice、<symbol>@markPrice@1s、!markPrice@arr、!markPrice@arr@1s、<symbol>@kline_<interval>、<symbol>@ticker、!ticker@arr、<symbol>@forceOrder、!forceOrder@arr
- /private —— 用户数据流(listenKey)

★ 对这个套利工具的直接后果:盘口最优价(bookTicker)在 /public,资金费推送(markPrice)在 /market,不在同一条路由上 —— 必须开至少两条 WebSocket 连接,不能合并订阅。

URL 两种形态:
- 单流:wss://fstream.binance.com/public/ws/btcusdt@bookTicker
- 组合流:wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker
  组合流的消息外层包一层:{"stream":"<streamName>","data":<原始payload>}
- 也可以连基础地址后发订阅报文:{"method":"SUBSCRIBE","params":["btcusdt@bookTicker"],"id":1},回包 {"result":null,"id":1};对应 UNSUBSCRIBE、LIST_SUBSCRIPTIONS。

盘口最优价 —— <symbol>@bookTicker(单 symbol,实时逐笔推送,无固定节流)/ !bookTicker(全市场)。payload:
{"e":"bookTicker","u":订单簿更新ID,"E":事件时间ms,"T":撮合引擎时间ms,"s":"BTCUSDT","b":"最优买价","B":"最优买量","a":"最优卖价","A":"最优卖量"}
用 u 做序号单调性校验丢包;用 T(撮合时间)而不是 E 做延迟估计。500ms 收敛循环的报价源就用这个,别去轮询 REST /fapi/v1/ticker/bookTicker。

资金费推送 —— <symbol>@markPrice(默认 3 秒一次)或 <symbol>@markPrice@1s(1 秒一次);全市场 !markPrice@arr / !markPrice@arr@1s。payload:
{"e":"markPriceUpdate","E":事件时间ms,"s":"BTCUSDT","p":"标记价格","i":"指数价格","P":"预估结算价","r":"资金费率","T":下次资金费时间ms}
另有 "ap"(标记价移动均值)和 "st"(symbol type,1=UM 2=CM)字段。r 和 T 就是实时资金费率和下次结算时刻 —— 跨所 carry 的核心信号从这里取,不用轮询 premiumIndex。

用户数据流(持仓/成交回报,delta 收敛必需):POST /fapi/v1/listenKey 取 key,PUT 每 30 分钟续期(60 分钟不续会失效),连 wss://fstream.binance.com/private/ws/<listenKey>。关键事件:ORDER_TRADE_UPDATE(订单/成交状态)、ACCOUNT_UPDATE(余额与持仓变动,含 wallet balance 和 position)、ACCOUNT_CONFIG_UPDATE(杠杆变更)、MARGIN_CALL。

心跳与重连(官方原文):
- The websocket server will send a ping frame every 3 minutes. —— 服务端每 3 分钟发 ping
- If the websocket server does not receive a pong frame back from the connection within a 10 minute period, the connection will be disconnected. —— 10 分钟内没回 pong 就断
- 允许主动发 unsolicited pong 保活
- A single connection is only valid for 24 hours; expect to be disconnected at the 24 hour mark —— 单连接 24 小时强制断,必须实现无缝重连(建议提前到 23h 主动重建新连接、双连接叠加数秒再切,避免断档)

限额:
- 单连接最多监听 1024 个 stream
- 单连接入站消息(客户端发给服务端,如 SUBSCRIBE)不超过 10 条/秒,超了直接断连,反复被断的 IP 会被封
- 连接被反复断开会触发 IP ban

下单也可以走 WebSocket API(独立于行情流)—— 见 Websocket API General Info,限频与 REST 共享(2400 weight/min、1200 orders/min、300 orders/10s),延迟低于 REST,500ms 收敛循环建议用它下单。

## 资金费周期

标准结算周期是 8 小时,UTC 00:00 / 08:00 / 16:00 三个时点。

非 8 小时的品种确实存在。Binance 对部分高波动/新上线合约采用 4 小时结算(社区与 freqtrade 侧已确认存在跨周期导致历史数据出现「缺口」的问题,见 freqtrade issue #12583:Binance Futures Funding - No longer always 8h candles)。官方 FAQ 的说法是结算频率「typically occurring every eight hours, but during periods of high market volatility, the interval can be shorter」。1 小时周期我没有查到官方在 USDⓈ-M 上启用的确证 —— 这一点查不到,别按有 1h 写死逻辑,但代码要按「周期是变量」设计。

怎么通过 API 查某个合约的结算周期(权威做法):
GET https://fapi.binance.com/fapi/v1/fundingInfo —— 权重 0(与 /fapi/v1/fundingRate 共享 500 次/5 分钟/IP),无参数。
响应数组每项:symbol, adjustedFundingRateCap, adjustedFundingRateFloor, fundingIntervalHours(整数,文档示例值为 8), disclaimer(忽略此字段)。

★ 最关键的坑:这个接口只返回「有调整过」的 symbol —— 即那些 FundingRateCap / FundingRateFloor / fundingIntervalHours 被改过默认值的合约。没有出现在返回列表里的 symbol,就是默认配置,即 8 小时周期 + 默认 cap/floor。所以正确实现是:
  intervalHours = fundingInfoMap[symbol]?.fundingIntervalHours ?? 8
千万不要写成「fundingInfo 里查不到就报错/跳过」,那会把绝大多数合约全过滤掉。

两个交叉校验手段(建议同时做,防止 fundingInfo 缓存过期):
1. GET /fapi/v1/premiumIndex 的 nextFundingTime,减去当前时间,配合已知周期反推是否对得上;
2. GET /fapi/v1/fundingRate 拉最近 3~5 条,对 fundingTime 做差分,差值 / 3600000 就是实际生效的小时数。这是最可靠的事实来源,因为它反映的是链上已发生的结算节奏。

工程建议:fundingInfo 每小时刷一次并缓存;年化换算必须用各自的 intervalHours 而不是一律 ×3 —— 4 小时品种一天结算 6 次,按 8 小时口径算年化会低估一半,跨所比价时会系统性选错腿。

## 坑

- 数量/价格精度:必须按 stepSize / tickSize 做「向下取整到步长整数倍」(floor,不是 round),再按 quantityPrecision / pricePrecision 截断小数位,两个条件都要满足。stepSize 是字符串如 "0.001",用 Decimal/BigDecimal 解析,绝不能用 float 做取模(0.1+0.2 问题会让你在边界上稳定触发 -1111)。违反报 -1111 BAD_PRECISION 'Precision is over the maximum defined for this asset.'。另外发送时要去掉科学计数法和尾随零,"1E-3" 会被拒。
- 最小名义价值:USDⓈ-M 上 MIN_NOTIONAL.notional 普遍为 5 USDT,报错 -4164 MIN_NOTIONAL "Order's notional must be no smaller than 5.0 (unless you choose reduce only)"。注意括号里的豁免 —— reduceOnly 单不受此限,所以「减仓收敛到目标」时可以打很小的尾单,但「加仓」不行。delta 收敛器必须区分这两个方向,否则残差小于 5U 时会陷入「一直想补但一直被拒」的死循环。
- 市价单有独立的 MARKET_LOT_SIZE 过滤器,maxQty 通常远小于 LOT_SIZE 的 maxQty。大额平仓用市价会被这条拦住,必须自己切片,或改用 LIMIT + priceMatch=OPPONENT(吃对手价,等效市价但走限价通道)。
- 永续符号命名与现货的对应关系有两个大坑。(a) 千倍前缀:1000PEPEUSDT、1000SHIBUSDT、1000BONKUSDT、1000FLOKIUSDT 等,合约一张的标的是 1000 个币,现货符号是 PEPEUSDT。跨腿对冲和跨所比价必须解析这个乘数并做数量换算,否则对冲比例差 1000 倍。(b) 结算币种:USDⓈ-M 下同时有 USDT 保证金(BTCUSDT)和 USDC 保证金(BTCUSDC)两套独立永续,资金费率完全不同,别混。另外交割合约符号形如 BTCUSDT_250926,枚举永续时必须用 contractType == 'PERPETUAL' 过滤,只看符号后缀不可靠。币本位在另一套 dapi(BTCUSD_PERP,pair=BTCUSD)。
- 限频三套独立配额,不要混为一谈:(1) IP 维度 REQUEST_WEIGHT 2400/分钟;(2) 账户维度 ORDERS 1200/分钟 且 300/10 秒;(3) /fapi/v1/fundingRate + /fapi/v1/fundingInfo 共享的 500 次/5 分钟/IP 专用桶。响应头 X-MBX-USED-WEIGHT-1M 和 X-MBX-ORDER-COUNT-1M / X-MBX-ORDER-COUNT-10S 实时回报用量,必须读并做主动退避。429 = 超限,418 = IP 已被封禁(Retry-After 头给解封时间)。错误码 -1003 TOO_MANY_REQUESTS、-1015 TOO_MANY_ORDERS。注意 POST /fapi/v1/order 的 IP 权重是 0,只吃下单配额 —— 所以 500ms 高频收敛的瓶颈在 300/10s 的下单数,不在 IP 权重;而轮询 positionRisk(权重 5)/ commissionRate(权重 20)才是 IP 权重杀手,这些必须缓存或改用 WS。启动时读 exchangeInfo 的 rateLimits[] 拿真实数值,别写死。
- FOK 完全支持。timeInForce 全集:GTC / IOC / FOK / GTX / GTD / RPI。GTX 是 post-only(会成为 taker 就撤单),做 maker 腿必用;GTD 需配 goodTillDate(必须 > 当前时间+600 秒,即最短 10 分钟,想做短时效单只能自己撤,GTD 顶不上)。两腿对冲建议:主动腿 IOC 或 FOK,被动腿 GTX。papi 的 CM 单只支持 GTC/IOC/FOK/GTX(无 GTD/RPI)。
- selfTradePreventionMode 默认值是 EXPIRE_MAKER,且只在 timeInForce 为 IOC/GTC/GTD 时生效。delta 中性工具如果在同账户对同一 symbol 同时挂买卖两侧(比如做市式收敛),默认设置会把你自己的 maker 单撤掉,造成「挂了但莫名消失」。要么显式设 EXPIRE_TAKER,要么保证同 symbol 同时只有单侧挂单。
- 两腿在同一 Binance 账户内跑同一 symbol 的多空:必须先 POST /fapi/v1/positionSide/dual 开 Hedge 模式(账户级开关,不是按 symbol),否则 One-way 模式下两腿互相抵消,净仓归零。而 Hedge 模式下 reduceOnly 参数不可用,平仓要靠 positionSide + 反向单来表达,收敛器逻辑要为两种模式分别写。切换 dual 模式要求账户无持仓无挂单。
- newClientOrderId 唯一性约束:官方原文 'Orders with the same newClientOrderID can be accepted only when the previous one is filled, otherwise the order will be rejected.' 500ms 循环反复改单时,加上 10 字符的 broker 前缀后只剩 26 字符(papi 22)做唯一性编码,用毫秒时间戳 36 进制约占 9 字符,还要留计数器防同毫秒碰撞。设计不当会在高频撤改时稳定撞 ID 被拒。
- 时钟:允许客户端最多领先服务器 1000ms,落后不超过 recvWindow(默认 5000,最大 60000)。不要靠调大 recvWindow 掩盖漂移 —— 那会让网络抖动中延迟到达的撤单在你以为它超时之后才生效,对两腿对冲是致命的。启动 + 周期性用 GET /fapi/v1/time 校准 offset。错误 -1021。
- 签名字符串必须与实际发出的字节完全一致(含 URL 编码形式)。POST 参数同时出现在 query 和 body 时官方取 query 的值,但签名却是 query+body 拼接 —— 混放极易签出对不上的串,报 -1022。规范做法:POST 参数全放 body,一次序列化,签它、发它,signature 追加在最末。
- 统一账户(papi)不是 fapi 的简单换前缀:base 是 https://papi.binance.com,限频是 IP 6000/分钟、下单 1200/分钟(与 fapi 数值不同),newClientOrderId 长度上限是 32 而非 36(CM 单已确证,UM 单我未抓到原文,按 32 保守处理),且 papi 下 broker/Link 返佣是否被统计官方无任何说明 —— 查不到,别假设。跑返佣的话优先走 fapi。
- /fapi/v3/positionRisk 不返回零仓位条目,不能用它枚举全部合约;合约清单只能来自 exchangeInfo。同理 fundingInfo 只返回「非默认」symbol,查不到 = 默认 8 小时,不是 = 不存在。这两个「稀疏返回」语义是最容易写反的地方。
- GET /fapi/v1/income 只保留最近 3 个月,且不传时间范围时默认只给最近 7 天(权重 30)。资金费实际到账的长期归因必须自建落库,不能指望随时回查。
- WebSocket 三路由分流是 2026-03-06 的破坏性变更,旧 URL 已于 2026-04-23 下线。任何 2026 年 3 月之前写的示例代码、以及大部分第三方库的默认地址都可能已失效。bookTicker 在 /public、markPrice 在 /market,这套工具至少要两条连接;单连接 24 小时强制断开,必须实现提前重建的无缝切换。
- BNB 手续费抵扣(GET/POST /fapi/v1/feeBurn)开启后实际扣费低于 commissionRate 返回值。资金费套利的净收益对手续费极度敏感(手续费往往比资金费差还大),算 PnL 时这一项不能漏。
