# HTX (原火币 Huobi) — USDT本位永续 USDT-M Perpetual。合约域名 api.hbdm.com（AWS优选 api.hbdm.vn，备用 api.btcgateway.pro）；现货域名 api.huobi.pro。两套域名完全分离，但共用同一套 API Key。合约文档 https://huobiapi.github.io/docs/usdt_swap/v1/en/ 与 https://www.htx.com/en-us/opend/newApiPages/ 。注意文档有两套：经典单币种保证金走 /linear-swap-api/v1/*，多币种保证金(Multi-Assets Collateral, 2025-05-12上线)走 /v5/*，两者互斥。

## 返佣码机制

### mechanism

合约（USDT本位永续）：下单请求 JSON body 里加字段 channel_code（String）。不是 header，不是订单ID前缀。

关键证据：CCXT ts/src/htx.ts 第5500-5502行 —— const broker = this.safeValue(this.options,'broker',{}); const brokerId = this.safeString(broker,'id'); request['channel_code'] = brokerId; 另在 sign() 第7744-7752行做兜底注入：凡是 POST、路径不含 'cancel' 且以 'order' 结尾的合约接口，若调用方没传 channel_code 就自动填入默认 broker id。CCXT 默认值是 'AA03022abc'（8-10位字母数字混合）。该逻辑最早由 2022-03-26 提交 'channel code' 引入，至今仍在维护，且已随 CCXT 迁移到 v5 后继续对 /v5/trade/order 注入。

覆盖范围：v1 的 /linear-swap-api/v1/swap_cross_order 与 swap_order（GitHub issue ccxt#15250 里有真实抓包，body 含 "channel_code":""），以及 v5 的 /v5/trade/order。批量下单 swap_cross_batchorder 走数组参数，CCXT 明确跳过注入（isArrayParams 分支），批量单能否带码未证实。

重要诚实声明：channel_code 在 HTX 任何公开文档里都查不到。我完整 grep 过 2.1MB 英文 + 2.0MB 中文 USDT本位合约文档、币本位合约文档、官方 Postman collection（hbdmapi/huobi_futures_Postman）、以及 USDT本位 API 版本变更历史页，channel_code 出现次数均为 0。新版 /v5/trade/order 文档的参数表我逐字读过（22个参数），也没有 channel_code。所以它是只对签约经纪商私下开放的未公开字段。

现货是另一套机制：broker id 作为 client-order-id 的前缀（CCXT 第5269行 request['client-order-id'] = brokerId + uuid），上限 64 字符。合约不能这么干——v1 swap_cross_order 文档里 client_order_id 类型是 long，取值范围 [1, 9223372036854775807]，塞不下字母，这正是合约要单独开 channel_code 字段的原因。

### example

v1 cross 下单（推荐给单币种保证金账户）：

POST https://api.hbdm.com/linear-swap-api/v1/swap_cross_order?AccessKeyId=..&SignatureMethod=HmacSHA256&SignatureVersion=2&Timestamp=2026-08-01T01%3A40%3A55&Signature=..
Content-Type: application/json

{
  "contract_code": "BTC-USDT",
  "direction": "buy",
  "offset": "both",
  "lever_rate": 3,
  "volume": 50,
  "order_price_type": "limit",
  "price": "62963.4",
  "client_order_id": 1785548451822,
  "channel_code": "AA03022abc"
}

v5（仅多币种保证金账户）：POST https://api.hbdm.com/v5/trade/order，body {"contract_code":"BTC-USDT","margin_mode":"cross","position_side":"both","side":"buy","type":"limit","price":"62963.4","volume":"50","time_in_force":"gtc","channel_code":"AA03022abc"}

工具实现建议：把 channel_code 做成配置项，值为空字符串时直接从 body 里删掉该 key（而不是发空串），这样对未签约用户最安全。

### howToGet

走 HTX Broker Program（API Broker 类别），不是普通的注册邀请码，普通返佣码没有 API 通道。

申请入口：https://www.htx.com/en-us/broker
在线申请表：https://forms.office.com/r/tPCAUezYeE（官方支持页 https://www.htx.com/support/84914016804394 给出，称 1-2 个工作日内联系）
邮箱：broker@htx-inc.com
Telegram：@HB_BrokerProgram

流程：先注册 HTX 账号 → 提交申请 → Broker 团队分配 broker ID → 做测试单 → 在 broker 后台看返佣。
合格申请人类型：算法交易平台、专业交易平台、交易所聚合器、钱包服务商等。
返佣比例：官方英文页写 up to 60%，2024 年规则升级后中文渠道口径为最高 65%，按下单用户 Prime 等级分档（Prime7 以下可参与），T 日结算 T+1 发放，默认 USDT 结算。

注意：HTX 的 API Broker 还有另一种模式——主账号邀请用户以子账号形式交易来拿返佣。channel_code 属于哪种模式下生效、格式和长度上限是多少，官方没公开，必须在申请时直接问 broker@htx-inc.com 要 channel_code 的字段规范。

### canBeEmpty

不填完全不影响下单，订单正常成交，只是没有返佣归属。CCXT 抓包里就有 "channel_code":"" 空串照常下单成功的真实案例（ccxt issue #15250）。

风险提示：因为该字段未公开文档化，无法确认 HTX 对非法/未注册的 channel_code 值是拒单还是静默忽略。CCXT 多年来对所有用户无条件注入 'AA03022abc'，从未见报错，说明至少对未注册值是静默忽略的，不会导致下单失败。建议工具默认不填（或留空即删 key），用户配置了自己的码再带上。

## 认证签名

HMAC-SHA256 + Base64。认证参数走 URL query，业务参数走 body。没有任何自定义认证 header。

待签字符串 = 4 行，用换行符 \n 连接（末行后无换行）：
第1行 METHOD 大写 GET 或 POST
第2行 小写主机名，不带协议不带端口，如 api.hbdm.com
第3行 路径，如 /linear-swap-api/v1/swap_cross_order
第4行 排序后的 query string

第4行构造规则（最大的坑）：
- POST：只有 4 个参数 AccessKeyId、SignatureMethod、SignatureVersion、Timestamp。业务参数一律不进签名，全放 JSON body。
- GET：上述 4 个 + 该接口全部业务参数。
- 按参数名 ASCII 升序（A<S<S<T 天然有序）。
- value 做 URI 编码，十六进制必须大写（":"→"%3A"，空格→"%20"）。CCXT 还额外 replace('%2c','%2C')，逗号也要大写编码。
- 用 & 连接。

固定值：SignatureMethod=HmacSHA256，SignatureVersion=2。
Timestamp 格式 YYYY-MM-DDTHH:MM:SS，UTC，秒级，无毫秒无 Z 后缀，例 2017-05-11T15:19:30；进签名串时 URI 编码为 2017-05-11T15%3A19%3A30。

Signature = Base64(HMAC_SHA256(待签串, SecretKey))，再做一次 URL 编码后作为 Signature 参数追加到 query 末尾。

最终请求示例：
POST https://api.hbdm.com/linear-swap-api/v1/swap_cross_order?AccessKeyId=xxx&SignatureMethod=HmacSHA256&SignatureVersion=2&Timestamp=2017-05-11T15%3A19%3A30&Signature=4F65x5A2bLyMWVQj3Aqp%2BB4w%2BivaA7n5Oi2SuYtCJ9o%3D
Header: Content-Type: application/json（GET 时用 application/x-www-form-urlencoded）
Body: {"contract_code":"BTC-USDT","direction":"buy",...,"channel_code":"你的码"}

时间戳容忍窗口：查不到。英文和中文全量文档我都 grep 过，只说时间戳用于"防止第三方截取请求"，没给具体秒数。建议 NTP 同步。签名错误返回 err_code 1003 invalid signature。

WebSocket 鉴权用同一算法，但方法固定按 GET 签，path 用 WS 路径（如 /linear-swap-notification），然后发 {"op":"auth","type":"api","AccessKeyId":...,"SignatureMethod":"HmacSHA256","SignatureVersion":"2","Timestamp":"...","Signature":"..."}。注意 op/type/cid/Signature 四个字段本身不参与签名运算。

API Key 若未绑定 IP，有效期只有 90 天。

## REST 端点

### fundingCurrent

三个选择，delta中性套利建议组合使用：

1) GET /linear-swap-api/v1/swap_batch_funding_rate （公开，无参数）—— 一次返回全部 272 个合约的当期资金费。实测可用。返回 estimated_rate=null、next_funding_time=null（已废弃字段），funding_time 是本次结算时间戳(ms)。扫描全市场用这个，1 次请求搞定。

2) GET /linear-swap-api/v1/swap_funding_rate?contract_code=BTC-USDT （公开，单个）—— 同样 estimated_rate 和 next_funding_time 都是 null。

3) GET /v5/market/funding_rate?contract_code=BTC-USDT （公开）—— 唯一能拿到 next_funding_time 的端点，还额外给 min_funding_rate / max_funding_rate 上下限。实测返回：{"contract_code":"BTC-USDT","funding_rate":"0.0001","funding_time":"1785571200000","next_funding_time":"1785600000000","min_funding_rate":"-0.00375","max_funding_rate":"0.00375"}。缺点：不支持批量，不传 contract_code 报错 code 1066。

实战建议：用 swap_batch_funding_rate 扫全市场 + swap_contract_info 的 settlement_period 自己算下一次结算时间，避免 272 次单独请求。

### fundingHistory

GET /linear-swap-api/v1/swap_historical_funding_rate?contract_code=BTC-USDT&page_index=1&page_size=100 （公开）

实测：page_size 上限 100，传 200/1000 都被静默截断为 100。返回 {total_page, current_page, total_size} 和 data 数组。BTC-USDT 实测 total_size=6329 条、total_page=64（page_size=100），6329 × 8h ≈ 5.8 年，一直回溯到合约上线日 2020-10-21。也就是说全历史都能取，没有时间窗限制，只受分页限制。

字段：funding_rate（挂牌费率）、realized_rate（实际扣收费率，通常等于 funding_rate；仅当扣资金费会导致爆仓时才少收或不收）、avg_premium_index、funding_time。做回测要用 realized_rate 而不是 funding_rate，但实测当前 realized_rate 返回 null，需要自行校验。

v5 版本：GET /v5/market/funding_rate_history?contract_code=BTC-USDT&limit=100，limit 上限也是 100，传 200 报 code 1067 "The limit field is invalid"。

### placeOrder

单币种保证金账户（默认，绝大多数用户）：
POST /linear-swap-api/v1/swap_cross_order （全仓）
POST /linear-swap-api/v1/swap_order （逐仓）
批量：swap_cross_batchorder / swap_batchorder

多币种保证金账户（Multi-Assets Collateral，2025-05-12 上线）：
POST /v5/trade/order，批量 /v5/trade/batch_orders

v1 swap_cross_order 完整参数（逐字来自官方文档）：
contract_code String 选填 如 BTC-USDT
pair + contract_type 可替代 contract_code，同时填时 contract_code 优先
direction String 必填 buy/sell
offset String 对冲模式必填 open/close；单向模式选填且只能填 both
lever_rate int 必填 杠杆倍数
volume long 必填 张数（整数！）
price decimal 选填 限价单必填
order_price_type String 必填 见下
reduce_only int 选填 0/1，对冲模式无效
client_order_id long 选填 范围 [1, 9223372036854775807]
tp_trigger_price / tp_order_price / tp_order_price_type / sl_trigger_price / sl_order_price / sl_order_price_type 选填
channel_code String 选填 未公开的经纪商码

order_price_type 取值：limit, market, opponent(对手价), lightning(闪电平仓), post_only, optimal_5/10/20, ioc, fok, opponent_ioc, lightning_ioc, optimal_5_ioc/optimal_10_ioc/optimal_20_ioc, opponent_fok, lightning_fok, optimal_5_fok/optimal_10_fok/optimal_20_fok。其中 limit / post_only / ioc / fok 必须传 price，其余不用传。

方向映射：开多 direction=buy offset=open；平多 sell+close；开空 sell+open；平空 buy+close。单向模式下 offset 填 both 或不填。

重要：若已有持仓，下单的 lever_rate 必须与当前持仓杠杆一致，否则直接拒单。要改杠杆得先调 switch_lever_rate。

### positions

POST /linear-swap-api/v1/swap_cross_position_info （全仓）
POST /linear-swap-api/v1/swap_position_info （逐仓）
POST /linear-swap-api/v1/swap_cross_account_position_info （资产+持仓一次拿全，省一次请求，推荐给 500ms 收敛循环用）
v5：GET /v5/trade/position/opens
参数 contract_code 选填，不填返回全部持仓。

### balance

POST /linear-swap-api/v1/swap_cross_account_info （全仓账户）
POST /linear-swap-api/v1/swap_account_info （逐仓账户）
POST /linear-swap-api/v1/swap_balance_valuation （账户估值）
v5：GET /v5/account/balance
注意都是 POST，不是 GET。

### setLeverage

POST /linear-swap-api/v1/swap_cross_switch_lever_rate （全仓）
POST /linear-swap-api/v1/swap_switch_lever_rate （逐仓）
v5：POST /v5/position/lever

参数：contract_code（或 pair+contract_type）+ lever_rate(int 必填)。

两个硬限制，文档原文：
1) 该接口限频 1 次 / 3 秒（比常规接口严得多，CCXT 给的权重是 30）。
2) 只有在该币种无持仓且无挂单时才能切换杠杆。
所以杠杆必须在开仓前设好，跑起来之后基本改不动。查可用杠杆档位用 POST /linear-swap-api/v1/swap_cross_available_level_rate。

### instruments

GET /linear-swap-api/v1/swap_contract_info （公开，不传参返回全部）
实测 268 个 USDT 本位永续，字段：
  contract_code "BTC-USDT"
  symbol "BTC"（基础币）
  contract_size 0.001 —— 每张合约代表的币数量，lotSize 的真正来源
  price_tick 0.1 —— tickSize
  settlement_period "8" —— 资金费周期，单位小时，字符串
  contract_status 1=正常（实测还存在状态 3）
  support_margin_mode "all"
  business_type / contract_type 均为 "swap"

GET /linear-swap-api/v1/swap_query_elements?contract_code=BTC-USDT （公开，更细）
补充给出：funding_rate_cap / funding_rate_floor（BTC 为 ±0.00375）、settle_period(int 8)、min_level 1 / max_level 200（杠杆区间）、price_ticks、instrument_values、order_limits（单笔最大张数，BTC 限价 500000 张 / 市价 170000 张）、normal_limits、open_limits。

minNotional：HTX 没有独立的 minNotional 字段。最小下单量就是 1 张，所以最小名义价值 = contract_size × 标记价格。BTC 即 0.001 × 价格 ≈ 63 USDT，SHIB 为 1000 × 价格。这点要自己算，别去找 minNotional 字段。

### feeRate

POST /linear-swap-api/v1/swap_fee
参数：contract_code（或 pair + contract_type），business_type 默认 swap，查交割合约时必须传 futures 或 all。
返回按合约逐个给出用户实际档位：open_maker_fee, open_taker_fee, close_maker_fee, close_taker_fee, delivery_fee, fee_asset。
示例返回 open_maker_fee 0.0002 / open_taker_fee 0.0004。
注意 HTX 开仓和平仓费率是分开给的，做套利成本模型时开平都要算。
没有单独的 swap_cross_fee，这一个接口同时覆盖全仓和逐仓。

## WebSocket

HTX 有两个 WS 端点，协议、订阅报文格式、心跳格式全都不一样，这是最容易踩的坑。

端点一 —— 行情：wss://api.hbdm.com/linear-swap-ws
备用 wss://api.btcgateway.pro/linear-swap-ws
所有 market.* 频道走这里，无需鉴权。
订阅报文：{"sub": "market.BTC-USDT.bbo", "id": "客户端自定id"}
心跳：双向。服务端每 5 秒发 {"ping": 18212558000}，客户端必须回 {"pong": 18212558000}。连续忽略 5 次心跳即被断开；最近 2 次心跳内回过一次即保持连接。

最优盘口频道：market.$contract_code.bbo
推送格式：
{"ch":"market.BTC-USDT.bbo","ts":1603707934525,"tick":{"mrid":131599726,"id":1603707934,"bid":[13064,38],"ask":[13072.3,205],"ts":1603707934525,"version":131599726,"ch":"market.BTC-USDT.bbo"}}
bid/ask 都是 [价格, 数量]，数量单位是张(Cont) 不是币。
文档明确警告：bbo 的买一卖一不是实时更新，约有 500ms 延迟。对 500ms 收敛的工具这是致命的——要真正实时的盘口必须改用增量深度 market.$contract_code.depth.size_${size}.high_freq（size 可取 20 等），自行维护订单簿。

端点二 —— 订单/资金费推送：wss://api.hbdm.com/linear-swap-notification
备用 wss://api.btcgateway.pro/linear-swap-notification
所有 public.* 和私有 orders_cross.* 频道走这里。
订阅报文：{"op":"sub","topic":"public.BTC-USDT.funding_rate","cid":"40sG903yz80oDFWr"}
心跳：单向。服务端发 {"op":"ping","ts":"1492420473058"}，客户端回 {"op":"pong","ts":"1492420473058"}，ts 必须原样回填。同样 5 次不回即断开。

资金费推送频道：public.$contract_code.funding_rate（无需鉴权）
contract_code 支持通配符 "*"，即 public.*.funding_rate 一次订阅全市场资金费——做套利扫描器强烈推荐。
推送：{"op":"notify","topic":"public.BTC-USDT.funding_rate","ts":...,"data":[{"symbol":"BTC","contract_code":"BTC-USDT","fee_asset":"USDT","funding_time":"1603778700000","funding_rate":"-0.00022","estimated_rate":"-0.000684","settlement_time":"1603785600000"}]}
注意 WS 推送里 estimated_rate 是有值的，而 REST v1 的同名字段已废弃返回 null。

私有频道（需先发 op:auth 鉴权）：
orders_cross.$contract_code 订单更新
positions_cross.$contract_code 持仓更新
accounts_cross.$margin_account 账户更新（如 accounts_cross.USDT）
matchOrders_cross.$contract_code 成交明细
均支持 "*" 通配符，但同一连接内通配符订阅和具体合约订阅不能混用（文档明确列为 Not Allowed）。

数据压缩：两个端点的所有服务端响应都是 GZIP 压缩的，必须解压后才能解析。这是 HTX 独有的，很多人第一次接会踩。

订阅数上限：
- 单个 UID 同时最多 30 条私有订单推送 WS 连接。同一标的的多个合约共用一条连接即可。
- 单连接每秒最多发 40 条订阅请求。
- req 类请求限 50 次/秒；sub 类订阅无频率限制（服务端主动推）。
- 文档另有一句"WS 请求连接通常不应超过 30 条"。

异常处理：鉴权阶段出错服务端会先发 {"op":"close","ts":...} 再断开；鉴权成功后如果发了非法 op，服务端回 {"op":"error","ts":...} 但保持连接。

## 资金费周期

标准 8 小时（UTC 00:00 / 08:00 / 16:00），但 HTX 确实有 4 小时和 1 小时的特殊品种，比例还不低。

实测（2026-08-01 拉取全量 swap_contract_info，268 个 USDT 本位永续）：
- 8 小时：193 个。BTC-USDT、ETH-USDT、LTC、DOGE、SHIB、XRP、LINK、TRX、DOT、ADA、BCH、ICP 等主流全部是 8h。
- 4 小时：67 个。XAU-USDT、PAXG-USDT、JST-USDT、STEEM-USDT、SUN-USDT、LIT、AKE、GRAM、RE、CAP、ARX、SYN 等。
- 1 小时：8 个。SPX500-USDT、NASDAQ100-USDT、CXMT-USDT、GIGADEVICE-USDT、KIOXIA-USDT、SHAZ-USDT、BANK-USDT、H-USDT。基本是股指/新股类和高波动新币。

怎么查某个合约的结算周期（两个都可以）：
1) GET /linear-swap-api/v1/swap_contract_info?contract_code=BTC-USDT → 字段 settlement_period，单位小时，类型是字符串，值 "8" / "4" / "1"。不传 contract_code 可一次拿全市场，做全量映射表。
2) GET /linear-swap-api/v1/swap_query_elements?contract_code=BTC-USDT → 字段 settle_period，同样是小时，但类型是 int。同时还给出 funding_rate_cap / funding_rate_floor。

实测校验通过：BTC-USDT settlement_period=8，当前 funding_time 落在 08:00 UTC；XAU/JST settlement_period=4，funding_time 落在 04:00 UTC；SPX500/NASDAQ100 settlement_period=1，funding_time 落在 02:00 UTC（当时是 01:40 UTC）。周期与结算时刻完全对得上。

对套利工具的直接影响：跨所比较资金费必须先按周期年化归一。HTX 的 4h 品种同样费率下年化是 8h 的 2 倍，1h 品种是 8 倍。绝不能直接拿 funding_rate 数值跟 Binance/Bybit 的 8h 费率对比。

费率上下限也是按合约给的，不是全局统一：BTC 是 ±0.375%，SPX500 是 ±2%。从 /v5/market/funding_rate 的 min_funding_rate / max_funding_rate，或 swap_query_elements 的 funding_rate_floor / funding_rate_cap 取。

## 坑

- 【数量单位是张不是币，且必须是整数】这是 HTX 最大的坑。swap_cross_order 的 volume 字段类型是 long，文档写 Numbers of orders (volume)，单位 Cont（张）。要下 0.05 BTC，必须先从 swap_contract_info 拿 contract_size=0.001，再算 volume = 0.05 / 0.001 = 50。换算后要向下取整，余数直接丢弃——这意味着 HTX 腿的数量粒度是离散的，delta 中性对冲时两腿数量对不齐是常态，收敛逻辑必须容忍 contract_size 级别的残差，不能死循环追平。SHIB contract_size=1000，PEPE=1000000，DOGE=100，XRP=10，粒度差异极大。
- 【没有 1000SHIB 这种符号，倍数藏在 contract_size 里】HTX 合约代码永远是 基础币-USDT，不做 1000x 前缀。实测 268 个合约里零个数字前缀符号。而 Binance/Bybit 用 1000SHIBUSDT、1000PEPEUSDT。跨所映射时必须做特殊处理：HTX 的 SHIB-USDT（contract_size=1000）对应 Binance 的 1000SHIBUSDT，价格差 1000 倍。直接按符号名匹配一定会错配，价格和数量都要按 contract_size 归一到币本位再比。
- 【v5 和 v1 是账户模式互斥的两套 API，选错直接全盘不通】/v5/* 只服务 Multi-Assets Collateral（多币种保证金）模式账户，2025-05-12 上线；单币种保证金账户必须用 /linear-swap-api/v1/*。CCXT 当前版本对 linear 无条件路由到 contractPrivatePostV5TradeOrder（htx.ts 第5577行），单币种账户用 CCXT 下单会失败，这就是 ccxt issue #27612。工具应先调 GET /linear-swap-api/v3/swap_unified_account_type 或 GET /v5/account/asset_mode 判断账户类型再选端点，默认走 v1 最稳。切换账户类型用 POST /linear-swap-api/v3/swap_switch_account_type，前提是无持仓无挂单。
- 【撤单率风控会直接封单，对 500ms 收敛循环是致命的】文档原文：10 分钟内通过特定订单类型下单总数 ≥ 3000 笔，且撤单率 > 99%，则禁止该 API 用户使用相关订单类型下单 5 分钟，返回 err_code 1084。被封的类型是 limit、post_only、FOK、IOC 四种——恰好是套利工具最常用的。未被封的仍可用：opponent(对手价)、lightning、optimal_5/10/20 以及它们的 _ioc/_fok 变体。按 UID 统计，主账号和子账号分开算，每 10 分钟一个周期（00:00-00:10 这样对齐）。每 500ms 挂撤一次 = 每 10 分钟 1200 笔，单合约还安全，但多合约并行很容易破 3000。设计上必须：控制挂撤频率、只在价格真正偏离时改单、或改用 opponent/optimal_N 这类不受限的类型。触发后响应 header 会带 recovery-time 字段告知解禁时间戳。
- 【限频三套体系，且 header 会实时回传余量】私有接口 144 次/3秒 每 UID（其中交易类 72 次/3秒、只读类 72 次/3秒，两者独立计数）。公开非行情接口 240 次/3秒 每 IP。公开行情类 REST 800 次/秒 每 IP。交割合约、币本位、期权、USDT本位各自独立计限额，互不占用。所有读和交易接口都会在响应 header 返回 ratelimit-limit / ratelimit-interval / ratelimit-remaining / ratelimit-reset，实测公开接口 header 为 ratelimit-limit: 240、ratelimit-interval: 3000。工具应该读这几个 header 做自适应退避，别靠本地计数猜。
- 【切杠杆 1 次/3 秒，且有持仓或挂单时根本切不了】POST swap_cross_switch_lever_rate 文档明确限频 1 次/3秒。更关键的是：只有该币种无持仓且无挂单时才允许切换。同时下单接口要求 lever_rate 必须与当前持仓杠杆一致，不一致直接拒单。所以杠杆必须在建仓前一次性设定好，运行中不可调。这对两腿对冲工具意味着杠杆是启动参数不是运行时参数。
- 【FOK 支持完整，且有增强变体】v1 的 order_price_type 直接支持 fok，另外还有 opponent_fok（对手价 FOK）、lightning_fok、optimal_5_fok / optimal_10_fok / optimal_20_fok。IOC 同理有 ioc、opponent_ioc、optimal_N_ioc。fok 和 ioc 必须同时传 price，opponent_fok / optimal_N_fok 不需要传 price（系统自动取盘口价），后者更适合快速对冲成交。v5 的写法不同：type 只有 market/limit/post_only，FOK 通过独立的 time_in_force 字段表达（fok/ioc/gtc，默认 gtc）。两套 API 迁移时这块要重写。
- 【POST 请求 body 不参与签名】这与 Binance/Bybit/OKX 全都不同。HTX 的 POST 只签 AccessKeyId、SignatureMethod、SignatureVersion、Timestamp 四个 query 参数，JSON body 完全不进签名串。很多人套用其他所的签名模板会一直 1003 invalid signature。另外 URI 编码的十六进制必须大写，冒号编码成 %3A 不能是 %3a。
- 【WebSocket 全程 GZIP，且两个端点心跳格式不同】行情端点 linear-swap-ws 用 {"ping":ts} / {"pong":ts}，订阅用 {"sub":"market.X.bbo","id":..}；通知端点 linear-swap-notification 用 {"op":"ping","ts":".."} / {"op":"pong","ts":".."}，订阅用 {"op":"sub","topic":"public.X.funding_rate"}。两套不能混。所有响应都是 GZIP 压缩必须解压。连续 5 次不回心跳即断开。
- 【bbo 频道有 500ms 延迟，对高频收敛不可用】文档原文警告买一卖一不实时、约 500ms 延迟。工具每 500ms 收敛一次持仓，用 bbo 等于永远在看上一拍的价格。必须改订 market.$contract_code.depth.size_20.high_freq 增量深度自行维护订单簿，才能拿到真实盘口。
- 【价格精度看 price_tick，别用小数位数推】price_tick 是绝对值不是位数：BTC 0.1、ETH 0.01、SHIB 1e-09、PEPE 1e-10、XRP 1e-05。下单价必须是 price_tick 的整数倍。用 Decimal 做量化，浮点数在 1e-10 这个量级会出精度问题导致拒单。
- 【历史资金费 page_size 上限 100，静默截断不报错】传 200 或 1000 不会报错，只是返回 100 条。必须靠 total_page 循环翻页。BTC-USDT 实测 6329 条覆盖 5.8 年全历史。而 v5 的 /v5/market/funding_rate_history 传 limit=200 会明确报 code 1067。同一个功能两套 API 的错误行为都不一致。
- 【v1 的 estimated_rate 和 next_funding_time 已废弃返回 null】REST v1 的 swap_funding_rate / swap_batch_funding_rate 这两个字段恒为 null，但字段还在，容易误以为拿到了值。要 next_funding_time 只能用 /v5/market/funding_rate（不支持批量），要预测费率只能订 WS public.*.funding_rate（推送里 estimated_rate 有值）。
- 【API Key 不绑 IP 只有 90 天有效期】文档明确：未绑定 IP 的 API Key 有效期仅 90 天。长跑的套利工具必须绑 IP，否则三个月后无声失效。
- 【现货和合约符号命名完全不同】合约 BTC-USDT（大写带横杠），现货 btcusdt（全小写无分隔符）。合约的 symbol 字段是基础币 BTC。做现货-合约对冲时映射规则：合约 contract_code.split('-')[0].lower() + 'usdt'。另注意部分合约无现货对应物——XAU-USDT、SPX500-USDT、NASDAQ100-USDT 是商品和股指类，只有合约没有现货。
- 【下单响应的错误码在 body 里，HTTP 状态码仍是 200】失败返回 {"status":"error","err_code":1047,"err_msg":"Insufficient margin available.","ts":..}。常用码：1003 签名无效、1013 合约不存在、1017 订单不存在、1034 订单价格类型错误、1039 价格超出限价、1041 数量超限、1047/1048 保证金或可平量不足、1061 订单不存在、1067 client_order_id 非法、1084 撤单率封禁、1094 杠杆为空需先切换杠杆、1220 未开通合约交易。批量下单时错误在 data.errors 数组里逐笔返回，status 仍是 ok，必须逐笔检查。
