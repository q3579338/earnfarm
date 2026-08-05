# Hyperliquid (L1 永续 DEX)

调研日期 2026-08-05。所有结论以官方文档 https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
及官方 Python SDK（github.com/hyperliquid-dex/hyperliquid-python-sdk）为准，
拿不准的一律在正文里标 **UNVERIFIED**，文末有汇总清单。

Hyperliquid 和前六家 CEX 在结构上是两个物种，这一节先把差异说清楚，后面所有细节都由它派生：

- **没有 API key / secret**。鉴权是 secp256k1 钱包签名（EIP-712），"API key" 的等价物是
  一把 **API Wallet（官方文档叫 agent wallet）私钥**，由主账户在网页上授权。
- **只有两个 HTTP 端点**：`POST /info`（全部只读，公开，不签名）和 `POST /exchange`（全部写操作，签名）。
  没有 REST 路径参数，请求类型写在 body 的 `type` / `action.type` 字段里。
- **签名不覆盖 HTTP body 字节**。签的是 `action` 对象的 **msgpack** 编码。
  这和 Gate/Bybit "签什么就发什么字节" 的模式完全相反——JSON 怎么序列化、键顺序在 JSON 里如何都不影响签名，
  但 **msgpack 的键顺序影响签名**（msgpack 按 dict 插入顺序编码）。
- **符号是币名**：`BTC`、`ETH`、`kPEPE`、`HYPE`，不是 `BTCUSDT`。下单时还要再换成 **整数 asset index**。
- **资金费每小时结算一次**，不是 8 小时。年化归一化时 `interval_s = 3600`。

## Base URL

```
主网  REST : https://api.hyperliquid.xyz
主网  WS   : wss://api.hyperliquid.xyz/ws
测试网 REST : https://api.hyperliquid-testnet.xyz
测试网 WS   : wss://api.hyperliquid-testnet.xyz/ws
```

`rest_base` 取 `https://api.hyperliquid.xyz`，路径只有 `/info` 和 `/exchange` 两个。

**主网/测试网不是只换域名**：签名 payload 里的 `source` 字段主网是 `"a"`、测试网是 `"b"`，
用错这一个字符签出来的签名会恢复出另一个地址，服务端报的却是
`User or API Wallet 0x... does not exist`（那个地址是恢复出来的垃圾地址，不是你的）。

## 认证签名

### API Wallet（agent wallet）与主账户的关系

- 主账户（master account）在网页上 approve 一把或多把 API Wallet，之后这些 wallet **只能签名，不持有资产**。
- **查询时必须传主账户/子账户的真实地址，不能传 agent 地址**。文档原文：
  "to query the account data associated with a master or sub-account, you must pass in the actual address of that account."
  这意味着适配器必须**同时**持有两样东西：签名用的私钥 + 查询用的账户地址。这就是凭据映射要解决的问题。
- API Wallet 会被 prune 的三种情况：被新的 `ApproveAgent` 注销、自身过期、注册它的账户没钱了。
- **不要复用已注销过的 agent 地址**：文档警告 "once an agent is deregistered, its used nonce state may be pruned"，
  重放窗口会被打开。

### nonce 规则

nonce 就是**毫秒时间戳**（`int(time.time() * 1000)`），但服务端的校验规则不是"窗口内即可"：

1. 每个 signer 地址保存**最高的 100 个 nonce**；新请求的 nonce 必须**大于这个集合里最小的那个**，
   且**从未被用过**。
2. nonce 必须落在 `(T - 2天, T + 1天)`，`T` 是区块的 unix 毫秒时间戳。
3. nonce 按 **signer** 计——用主私钥签就按主账户地址计，用 API Wallet 签就按 agent 地址计。
   **同一把 API Wallet 给多个子账户签名会共享同一个 nonce 计数器**，官方建议一个子账户配一把 agent wallet。

对本项目的后果：

- 时钟窗口是 ±天级，`ClockOffset` 那套校时对 Hyperliquid 几乎无意义，
  但 `fetch_server_time_ms` 是基类抽象方法必须实现（用 `exchangeStatus` 的 `time`）。
- **真正的风险是并发**：500ms 收敛循环里两个协程同时取 `time.time()*1000` 会拿到同一个毫秒，
  第二个请求的 nonce "已被用过" 直接被拒。适配器必须持有一个**进程内单调递增的 nonce 发生器**
  （`max(now_ms, last + 1)`，加锁），不能裸用时间戳。
- 100 条历史窗口意味着：如果你短时间内发了 100 笔以上并且乱序到达，早到的小 nonce 会被挤出窗口下沿。
  单调递增发生器天然规避。

### L1 action 签名算法（照着这个写就对）

```python
# 1) action 是一个普通 dict，键顺序即 msgpack 编码顺序，顺序错 = 签名错
data  = msgpack.packb(action)                    # msgpack-python 默认参数即可
# 2) nonce，8 字节大端
data += nonce.to_bytes(8, "big")
# 3) vaultAddress：没有就是单字节 0x00；有就是 0x01 + 20 字节地址原文
if vault_address is None:
    data += b"\x00"
else:
    data += b"\x01"
    data += bytes.fromhex(vault_address[2:] if vault_address.startswith("0x") else vault_address)
# 4) expiresAfter（可选）：先补一个 0x00，再 8 字节大端。注意这个 0x00 不是"无"的标记，
#    而是这一段自己的前缀；没有 expiresAfter 时这两行整个不写。
if expires_after is not None:
    data += b"\x00"
    data += expires_after.to_bytes(8, "big")
# 5) keccak256
connection_id = keccak(data)                     # 32 字节

# 6) 套进 EIP-712 的 "phantom agent"
phantom = {"source": "a" if is_mainnet else "b", "connectionId": connection_id}
payload = {
    "domain": {
        "chainId": 1337,                                            # 固定 1337，不是 HL 的链 id
        "name": "Exchange",
        "verifyingContract": "0x0000000000000000000000000000000000000000",
        "version": "1",
    },
    "types": {
        "Agent": [
            {"name": "source",       "type": "string"},
            {"name": "connectionId", "type": "bytes32"},
        ],
        "EIP712Domain": [
            {"name": "name",              "type": "string"},
            {"name": "version",           "type": "string"},
            {"name": "chainId",           "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
    },
    "primaryType": "Agent",
    "message": phantom,
}
# 7) secp256k1 签
signed = account.sign_message(encode_typed_data(full_message=payload))
signature = {"r": to_hex(signed.r), "s": to_hex(signed.s), "v": signed.v}   # v = 27/28
```

`primaryType` 是 `"Agent"`，字段顺序 `source` 在前、`connectionId` 在后。
`chainId` 恒为 **1337**（这是历史遗留的假链 id，不要填 42161/HyperEVM 的 998/999）。

### HTTP 请求体

```json
{
  "action": { "...": "同上，逐字节 msgpack 过的那个对象" },
  "nonce": 1754450974231,
  "signature": {"r": "0x...", "s": "0x...", "v": 28},
  "vaultAddress": null,
  "expiresAfter": null
}
```

官方 SDK 的 `_post_action` **总是**把 `vaultAddress` / `expiresAfter` 两个键写进 JSON（值为 `null`），
服务端接受。适配器可以照抄，也可以省略——**因为签名不覆盖 HTTP body**，这两种写法都能过。

**这是和其它六家最本质的差别**：Gate 要求"签的 body 字符串和发的 body 字符串逐字节相同"，
Hyperliquid 不要求，`Content-Type: application/json` 之外没有任何 header 要求，没有 api-key header。
代价是换了一个更刁的约束：**msgpack 编码前的 dict 键顺序**。

### 官方点名的五个翻车点（verbatim 归纳）

1. 混淆 `sign_l1_action` 和 `sign_user_signed_action` 两套方案（后者用于提现/转账等用户签名动作，本项目用不到）。
2. **msgpack 的字段顺序**。
3. 数字的尾随零（trailing zeroes）。
4. **地址大小写**——签名前把地址一律 lowercase，有些字段按 bytes 解析并在全网被自动小写化。
5. "本地能恢复出正确 signer" 不代表 payload 构造正确——本地验签只验了你自己那套。

签名错的表征是 `L1 error: User or API Wallet 0x0123... does not exist.` 或
`Must deposit before performing actions.`，返回的地址跟你的地址对不上。
**看到这两条错误第一反应是签名错，不是没钱。**

## REST 端点

所有请求都是 `POST`，`Content-Type: application/json`，请求类型写在 body 里。

### serverTime

```
POST /info   {"type": "exchangeStatus"}
→ {"specialStatuses": [], "time": 1754450974231}
```

权重 2。返回的 `time` 是毫秒。
**UNVERIFIED**：`exchangeStatus` 出现在官方限频表的权重-2 列表里，但字段形状我是从 QuickNode 的镜像文档拿的，
gitbook 正文没给示例。退路：`{"type":"l2Book","coin":"BTC"}` 的 `time` 字段（官方有示例，权重同样是 2）。

### instruments

```
POST /info   {"type": "meta", "dex": ""}
→ {
    "universe": [
      {"name": "BTC", "szDecimals": 5, "maxLeverage": 50,
       "marginTableId": 50, "onlyIsolated": false, "isDelisted": false,
       "marginMode": "strictIsolated" | "noCross"}      // 后三个字段是可选的，多数币不带
    ],
    "marginTables": [
      [50, {"description": "", "marginTiers": [{"lowerBound": "0.0", "maxLeverage": 50}]}]
    ]
  }
```

- `dex` 传 `""`（或省略）= 第一个 perp dex，也就是官方主市场。传别的值是 HIP-3 builder 部署的市场，
  **本适配器只做 `dex=""`**。
- **`universe` 的下标就是下单用的 asset index**（`BTC = 0`）。
- **delisted 的币仍然留在 universe 里**（带 `isDelisted: true`），下标因此保持稳定。
  这条很重要：**不能过滤掉 delisted 之后再取 `enumerate` 的下标**，那样所有 index 都会左移，
  下单会打到完全不相干的币上。正确做法是先 `enumerate(universe)` 拿到 index，再过滤 delisted。
- `Instrument` 字段映射：
  - `tick_size`：**Hyperliquid 没有 tick size 字段**，价格合法性由"5 位有效数字 + 最多 `6 - szDecimals` 位小数"两条规则决定。
    `tick_size` 只能填一个保守的代理值 `Decimal(1).scaleb(-(6 - szDecimals))`（该币允许的最细小数位），
    实际下单前必须再跑一次 5 位有效数字的规整。见"符号与精度模型"。
  - `lot_size` = `Decimal(1).scaleb(-szDecimals)`
  - `min_notional` = `Decimal("10")`（错误码 `MinTradeNtl: "Order must have minimum value of $10."`）
  - `contract_size` = `1`（线性合约，1 张 = 1 个 underlying，文档原文 "1 unit of underlying spot asset"）
  - `max_leverage` = `maxLeverage`
  - `funding_interval_s` = `3600`

### leverageTiers

杠杆档位在同一个 `meta` 响应里：`universe[i].marginTableId` → `marginTables` 里对应那张表的 `marginTiers`。
每个 tier 是 `{"lowerBound": "<名义仓位下限 USDC>", "maxLeverage": N}`，按 lowerBound 升序。

基类要的是 `[(名义上限, 最大杠杆), ...]` 升序，所以要做一次错位换算：

```
tiers[i] 的名义上限 = tiers[i+1].lowerBound；最后一档没有上限，填一个哨兵（如 Decimal("Infinity") 或极大值）
```

文档给的 BTC 主网例子：`0-150M → 40x`，`>150M → 20x`。
`lowerBound` 的口径是 **Notional Position Value (USDC)**，不是账户净值。

### fundingCurrent

```
POST /info   {"type": "metaAndAssetCtxs", "dex": ""}
→ [
    {"universe": [...], "marginTables": [...], "collateralToken": 0},
    [
      {"dayNtlVlm": "1169046.29406", "funding": "0.0000125",
       "impactPxs": ["14.3047", "14.3444"], "markPx": "14.3161", "midPx": "14.314",
       "openInterest": "688.11", "oraclePx": "14.32", "premium": "0.00031774",
       "prevDayPx": "15.322"}
    ]
  ]
```

**这是一次调用拿全市场的资金费**，是 carry 榜单的主力端点。响应是**两元素数组**，
`[0]` 是 meta、`[1]` 是 ctx 数组，**两者按下标一一对应**（`ctxs[i]` 对应 `universe[i]`）。
长度不一致要直接抛错，不能 zip 截断——那会让全表符号错位。

- `funding`：**这一小时的资金费率**，符号是交易所原始约定（正 = 多头付空头），直接写进 `CarryRate.rate`，不取反。
- `interval_s = 3600`。
- `next_settle_ms`：**响应里没有这个字段**。资金费在整点结算，所以自己算下一个整点：
  `(now_ms // 3_600_000 + 1) * 3_600_000`。
  也可以用 `{"type":"predictedFundings"}`，它的 `HlPerp` 条目带 `nextFundingTime`，
  但那是一次额外的权重-20 请求且只覆盖第一个 perp dex，不值当。
- `premium`：溢价指数，用于判断费率的成因（见"资金费周期"）。
- `impactPxs`：`[impact_bid, impact_ask]`，用 20000 USDC（BTC/ETH）或 6000 USDC（其它）名义额吃出来的冲击价。
  **这是资金费的定价基础，不是盘口**，不要拿它当 BookTop。

### fundingHistory

```
POST /info   {"type": "fundingHistory", "coin": "ETH",
              "startTime": 1683800000000, "endTime": 1683900000000}
→ [{"coin": "ETH", "fundingRate": "-0.00022196", "premium": "-0.00052196", "time": 1683849600076}]
```

- `coin` 必填，`startTime`（毫秒，**包含**）必填，`endTime`（毫秒，包含）可选，缺省为当前时间。
- **单币一次一个调用**，没有批量模式。
- **翻页方向是向"新"翻**，和 Bybit（向旧翻）相反。文档通用规则：
  "Responses that take a time range will only return 500 elements ... use the last returned timestamp as the next `startTime`"。
- **每小时一条，所以 500 条只有约 20.8 天**。想要 30 天窗口必须翻至少两页——
  这不是可选优化，是必须实现的。
- **`startTime` 是闭区间，用上一页最后一条的 `time` 当下一页的 `startTime` 会把那一条再收一遍。**
  基类契约明确要求去重（重复值会抬高 autocorr、拖偏 median 且不报错），
  所以要么用 `last_time + 1` 当下一页起点，要么按 `time` 做集合去重。**两个都做最保险。**
- 返回天然是升序，且最后一页就是最新那端——正好满足基类"必须覆盖到窗口末端"的要求，
  只要循环条件写成"翻到返回条数 < 500 或 last_time 不再前进为止"，不要写成"取前 N 条"。
- 停机保护：给一个最大页数硬闸（比如 32 页 ≈ 666 天），防止服务端不推进 cursor 时无限翻页。

### userFunding（对账用，本期不接）

```
POST /info   {"type": "userFunding", "user": "0x<账户地址>",
              "startTime": 1681222254710, "endTime": <now>}
→ [{"delta": {"coin":"ETH","fundingRate":"0.0000417","szi":"49.1477",
              "type":"funding","usdc":"-3.625312","nSamples":null},
    "hash": "0x...", "time": 1681222254710}]
```

`usdc` 是**实际现金流，负 = 你付出**，这是"我到底收了多少资金费"的唯一权威口径。
基类当前没有对应的抽象方法，所以本期不接；但真要核对策略收益时，
**用这个而不是拿 `fundingHistory` 的费率乘仓位自己推**——后者算不出结算时刻的实际仓位，
也漏掉了结算瞬间的仓位变动。翻页规则同 `fundingHistory`（500 条上限、向新翻、`startTime` 闭区间）。

### bookTop / bookDepth

```
POST /info   {"type": "l2Book", "coin": "BTC", "nSigFigs": null}
→ {"coin": "BTC", "time": 1754450974231,
   "levels": [ [ {"px":"113377.0","sz":"7.6699","n":17}, ... ],     // [0] = bids，价降序
               [ {"px":"113397.0","sz":"0.11543","n":3}, ... ] ]}   // [1] = asks，价升序
```

- 权重 2，是全部 info 请求里最便宜的一档，可以进较快的轮询。
- `levels` 是**长度为 2 的数组**：`[0]` 买盘、`[1]` 卖盘。`n` 是该档的订单笔数。
- `sz` 是 **base 币数量**（不是张、不是名义额），`contract_size = 1` 所以名义额 = `px * sz`，无需换算。
- `nSigFigs` 可选，合法值 `2/3/4/5/null`，用于把档位按有效数字聚合；`mantissa`（1/2/5）只有在 `nSigFigs=5` 时允许。
  **算带内深度必须传 `null`（全精度）**，聚合过的簿会把带宽边界上的量算错。
- **UNVERIFIED**：单侧返回多少档文档没写（实测常见 20 档/侧）。10bp 带宽在主流币上肯定吃不完，
  小币簿本来就稀。按基类契约，带宽内档位不够时如实返回 `levels_used`，**绝不外推**。
- 响应缺 `levels` 或长度不为 2 要直接抛错，不能退化成深度 0（scoring 里 `depth_cap<=0` 会跳过容量判断，
  等于把"没数据"读成"深度无限"）。

### feeRate

```
POST /info   {"type": "userFees", "user": "0x<主账户地址>"}
→ {
    "dailyUserVlm": [{"date":"2025-05-23","userCross":"0.0","userAdd":"0.0","exchange":"2852367.077"}],
    "feeSchedule": {"cross":"0.00045","add":"0.00015","spotCross":"0.0007","spotAdd":"0.0004",
                    "tiers":{"vip":[],"mm":[]}, "referralDiscount":"0.04", "stakingDiscountTiers":[]},
    "userCrossRate": "0.000315",
    "userAddRate":   "0.000105",
    "userSpotCrossRate": "0.00049",
    "userSpotAddRate":   "0.00028",
    "activeReferralDiscount": "0.0",
    "trial": null,
    "feeTrialReward": "0.0",
    "nextTrialAvailableTimestamp": null,
    "stakingLink": {"type":"tradingUser","stakingUser":"0x54c0..."},
    "activeStakingDiscount": {"bpsOfMaxSupply":"4.7577998927","discount":"0.3"}
  }
```

- **这是公开的 info 请求，不需要签名**，只要知道地址就能查——和其它六家"只有签名请求才拿得到真实档位"不同。
  但语义一样：`userCrossRate` / `userAddRate` 才是这个账户的真实费率，`feeSchedule.cross/add` 是挂牌基础档。
- 术语对照：**cross = taker（吃单）**，**add = maker（挂单）**。别被 "cross" 误导成全仓保证金。
- **折扣已经算进去了**。上面的例子：`0.00045 * (1 - 0.3) = 0.000315`、`0.00015 * (1 - 0.3) = 0.000105`，
  `activeStakingDiscount.discount = "0.3"` 是 HYPE 质押折扣。**不要再手动打一次折**。
- 符号约定：**正数 = 成本**，和 `FeeSchedule` 的约定一致，直接填，不用翻转（不像 OKX）。
- **账户级档位，一档吃全市场** → `FeeSchedule.symbol` 填空串 `""`。
- `note` 里可以写 `activeStakingDiscount.discount` 和 `activeReferralDiscount`，方便界面显示折扣来源。
- 必须缓存。权重 20，档位一天变不了一次。
- **UNVERIFIED**：做市商返佣档（`feeSchedule.tiers.mm`，文档另一处给的是 -0.001% / -0.002% / -0.003%）
  是否已经体现在 `userAddRate` 上、以及 `userAddRate` 能否为负，没有文档明说。
  按基类契约"查不到就不返回这一条"，但这里能查到，正常填即可；负值天然被当成返佣（收入），逻辑自洽。

基础档位（14 天成交量，Base Rate，供参考/兜底）：

| 14d 量 | taker (cross) | maker (add) |
|---|---|---|
| Tier 0 | 0.045% | 0.015% |
| >$5M | 0.040% | 0.012% |
| >$25M | 0.035% | 0.008% |
| >$100M | 0.030% | 0.004% |
| >$500M | 0.028% | 0.000% |
| >$2B | 0.026% | 0.000% |
| >$7B | 0.024% | 0.000% |

HYPE 质押折扣：>10 → 5%，>100 → 10%，>1000 → 15%，>10000 → 20%，>100000 → 30%，>500000 → 40%。

### positions

```
POST /info   {"type": "clearinghouseState", "user": "0x<主账户或子账户地址>", "dex": ""}
→ {
    "assetPositions": [
      {"type": "oneWay",
       "position": {
          "coin": "ETH", "szi": "0.0335", "entryPx": "2986.3",
          "positionValue": "100.02765", "unrealizedPnl": "-0.0134",
          "returnOnEquity": "-0.0026789", "liquidationPx": "2866.26936529",
          "marginUsed": "4.967826", "maxLeverage": 50,
          "leverage": {"type": "isolated", "value": 20, "rawUsd": "-95.059824"},
          "cumFunding": {"allTime": "514.085417", "sinceOpen": "0.0", "sinceChange": "0.0"}
       }}
    ],
    "crossMaintenanceMarginUsed": "0.0",
    "crossMarginSummary": {"accountValue":"13104.51","totalMarginUsed":"0.0","totalNtlPos":"0.0","totalRawUsd":"13104.51"},
    "marginSummary":      {"accountValue":"13109.48","totalMarginUsed":"4.97","totalNtlPos":"100.03","totalRawUsd":"13009.45"},
    "time": 1708622398623,
    "withdrawable": "13104.514502"
  }
```

- 权重 2，可以放进风控轮询。
- **`user` 必须是主账户/子账户地址，不是 agent 地址**。传 agent 地址会返回一个空仓位的账户，
  而基类明确说仓位是"真值"——读到空仓会让引擎认为裸奔并触发撤退。这是最危险的接错。
- `szi` 是**有符号的 base 币数量**（正=多，负=空），正好对应 `Position.qty`，不用换算。
- **`assetPositions` 里只有非零仓位**，平掉的币不出现。`fetch_positions(symbols)` 要给未出现的符号补零仓位，
  不能只返回 API 给的那几条。
- `markPx` 不在这个响应里 → `Position.mark_price` 要从 `metaAndAssetCtxs` 或 `allMids` 取，
  或者用 `positionValue / abs(szi)` 反算（后者是 HL 自己按 mark 算的名义额，等价且省一次请求）。
- `liquidationPx` 可能是 `null`（全仓无风险时），映射成 `Position.liquidation_price = None`。
- `leverage.value` 是整数，`leverage.type` 是 `"cross"` / `"isolated"`。

### openOrders

```
POST /info   {"type": "frontendOpenOrders", "user": "0x<账户地址>", "dex": ""}
→ [{"coin":"BTC","side":"A","limitPx":"29792.0","sz":"5.0","origSz":"5.0","oid":91490942,
    "timestamp":1681247412573,"orderType":"Limit","reduceOnly":false,
    "isTrigger":false,"triggerPx":"0.0","triggerCondition":"N/A","isPositionTpsl":false,
    "cloid": null}]
```

**必须用 `frontendOpenOrders` 而不是 `openOrders`**：后者只有 `{coin, limitPx, oid, side, sz, timestamp}`，
**没有 `cloid`**，而本项目一切按 client_order_id 索引，拿不到 cloid 的挂单等于查不到。
两者权重都是 20，没理由用阉割版。

- `side`：**`"B"` = bid = 买/多，`"A"` = ask = 卖/空**。这个缩写在 fills、orderStatus 里也一样。
- `sz` 是**剩余未成交量**，`origSz` 是原始下单量 → `filled_qty = origSz - sz`。
- 判断两腿是否平衡必须把这些挂单计入（基类注释里说的"A 已成交、B 还挂着"）。

### placeOrder

```
POST /exchange
{
  "action": {
    "type": "order",
    "orders": [{
      "a": 0,                                   // asset index（整数！不是币名）
      "b": true,                                // isBuy
      "p": "50000",                             // 限价，字符串
      "s": "0.1",                               // 数量，base 币，字符串
      "r": false,                               // reduceOnly
      "t": {"limit": {"tif": "Ioc"}},           // "Alo" | "Ioc" | "Gtc"
      "c": "0x1234567890abcdef1234567890abcdef" // cloid，可选，128 bit hex
    }],
    "grouping": "na"                            // "na" | "normalTpsl" | "positionTpsl"
  },
  "nonce": 1754450974231,
  "signature": {"r":"0x...","s":"0x...","v":28}
}
```

**键顺序就是上面这个顺序，一个字都不能挪**（msgpack 按插入顺序编码）：

- action：`type` → `orders` → `grouping`（→ `builder`，本项目不发）
- order wire：`a` → `b` → `p` → `s` → `r` → `t` → `c`（`c` 可选，没有 cloid 时整个键不出现）
- order type：`{"limit": {"tif": ...}}`

响应（HTTP 恒为 200，成败看 body）：

```json
{"status":"ok","response":{"type":"order","data":{"statuses":[{"resting":{"oid":77738308}}]}}}
{"status":"ok","response":{"type":"order","data":{"statuses":[{"filled":{"totalSz":"0.02","avgPx":"1891.4","oid":77747314}}]}}}
{"status":"ok","response":{"type":"order","data":{"statuses":[{"error":"Order must have minimum value of $10."}]}}}
{"status":"err","response":"<整批被拒的错误串>"}
```

- `statuses` 与 `orders` **按下标一一对应**。单笔下单取 `statuses[0]`。
- **`{"error": ...}` 出现在 `status: "ok"` 的响应里**。只看 HTTP 状态码或只看外层 `status` 会把
  "订单被拒"当成"下单成功"，然后引擎按一个不存在的仓位继续加仓。
- `filled` 分支直接给了 `totalSz` / `avgPx` / `oid`，IOC 路径一次就拿全 `OrderResult`；
  但 **`filled` 里没有手续费**，`OrderResult.fee` 要么留 0 要么另查 `userFillsByTime`（见 queryOrder）。
- `resting` 只有 `oid`，`filled_qty = 0`、`status = "new"`。
- 市价单：Hyperliquid **没有市价单类型**，"market" 是前端行为——用 `Ioc` + 一个足够穿透的限价来实现。
  这正好和本项目 `OrderRequest.price=None` 时的 FOK 保护限价语义对得上：
  `price is None` 时必须自己从盘口算一个保护价，**不能发空价**。
- `expiresAfter`（毫秒时间戳）是 HL 版的"迟到即拒"，对 500ms 收敛循环有价值，
  但它会进 action hash（多两段字节），实现时要么全程带、要么全程不带，不能只在部分路径带。

### cancelOrder

按 oid 撤：

```json
{"action": {"type": "cancel", "cancels": [{"a": 0, "o": 77738308}]},
 "nonce": ..., "signature": {...}}
```

按 cloid 撤（**本项目用这个**，因为上层只有 client_order_id）：

```json
{"action": {"type": "cancelByCloid",
            "cancels": [{"asset": 0, "cloid": "0x1234567890abcdef1234567890abcdef"}]},
 "nonce": ..., "signature": {...}}
```

注意两者的键名**故意不一样**：`cancel` 用缩写 `a`/`o`，`cancelByCloid` 用全名 `asset`/`cloid`。
这是官方 SDK 的实际写法（`hyperliquid/exchange.py` 的 `bulk_cancel` / `bulk_cancel_by_cloid`），
照抄，别自作主张统一。

响应：

```json
{"status":"ok","response":{"type":"cancel","data":{"statuses":["success"]}}}
{"status":"ok","response":{"type":"cancel","data":{"statuses":[{"error":"Order was never placed, already canceled, or filled."}]}}}
```

`statuses[i]` 要么是字面量字符串 `"success"`，要么是 `{"error": "..."}` 对象——**类型不统一，解析要分支判断**。

`MissingOrder`（"Order was never placed, already canceled, or filled."）必须映射成 `OrderRejected`，
因为 `base.resolve_unknown_order` 靠 `OrderRejected` 判定"这单已终结、继续查"。

**UNVERIFIED**：gitbook 的 cancel 示例里在 action 层多了一个 `"f": false`（快速撤单标记），
另一处又说它是 cancel 条目里的字段且"false 时省略"。官方 Python SDK **两处都不发**。
按 SDK 来（不发 `f`）——多发一个键会改变 msgpack 字节从而改变签名，宁可少发。

### queryOrder

```
POST /info   {"type": "orderStatus", "user": "0x<账户地址>", "oid": 90542681}
POST /info   {"type": "orderStatus", "user": "0x<账户地址>", "oid": "0x1234...cdef"}   // 传 cloid 也走 oid 这个键
→ {"status": "order",
   "order": {"order": {"coin":"ETH","side":"A","limitPx":"2412.7","sz":"0.0","origSz":"0.0076",
                       "oid":1,"timestamp":1724361546645,"orderType":"Market","tif":"FrontendMarket",
                       "reduceOnly":true,"isTrigger":false,"triggerPx":"0.0","triggerCondition":"N/A",
                       "children":[],"isPositionTpsl":false,"cloid":null},
             "status": "filled",
             "statusTimestamp": 1724361546645}}
```

- **cloid 直接塞进 `oid` 字段**（官方 SDK 的 `query_order_by_cloid` 就是 `{"oid": cloid.to_raw()}`），
  不存在 `cloid` 这个请求键。
- 内层 `status` 的取值包含 `open` / `filled` / `canceled` / `triggered` / `rejected` / `marginCanceled`
  以及另外十几个（文档说 "15+ other status values"，没有给全表 → **UNVERIFIED**）。
  映射到 `OrderResult.status` 时用白名单 + 兜底：未知值一律当作非终结的 `new`，
  **绝不能把不认识的状态当成 canceled**——那会让引擎以为没成交然后重下。
- **`orderStatus` 不返回成交均价**。`sz` 是剩余量、`origSz` 是原始量，
  `filled_qty = origSz - sz` 能算出来，但 `avg_price` 和 `fee` 算不出来。
  要拿它们必须再查 `userFillsByTime` 按 `oid` 过滤：

```
POST /info   {"type": "userFillsByTime", "user": "0x...", "startTime": <下单时刻-60s>,
              "endTime": <now>, "aggregateByTime": false}
→ [{"coin":"AVAX","px":"18.435","sz":"93.53","side":"B","time":1681222254710,
    "startPosition":"26.86","dir":"Open Long","closedPnl":"0.0",
    "hash":"0x...","oid":90542681,"crossed":false,
    "fee":"0.01","feeToken":"USDC","builderFee":"0.01","tid":118906512037719}]
```

`fee` 单位是 USDC（`feeToken`），**正数 = 你付**。`crossed=true` 表示这笔是吃单。
`avg_price = Σ(px*sz) / Σsz`，`fee = Σfee`。
**UNVERIFIED**：做市返佣时 `fee` 是否为负数，文档没有明说（"Maker rebates are paid out continuously
on each trade directly to the trading wallet"，暗示是负 fee，但没有示例）。

- **UNVERIFIED**：订单完全不存在时 `orderStatus` 的外层 `status` 返回什么（大概率是 `"unknownOid"`）。
  防御写法：外层 `status != "order"` 一律当"订单不存在" → `OrderRejected`，
  这正好让 `resolve_unknown_order` 返回 `Decimal("0")`。

### setLeverage

```json
{"action": {"type": "updateLeverage", "asset": 0, "isCross": true, "leverage": 5},
 "nonce": ..., "signature": {...}}
→ {"status":"ok","response":{"type":"default"}}
```

键顺序 `type` → `asset` → `isCross` → `leverage`。`leverage` 是**整数**（不是字符串、不是小数），
所以基类的 `Decimal` 入参要 `int()` 一下，且非整数杠杆要拒绝而不是静默取整。

## WebSocket

```
wss://api.hyperliquid.xyz/ws
{"method": "subscribe", "subscription": {"type": "bbo", "coin": "BTC"}}
```

- 最优价流用 `bbo`：`{"coin", "time", "bbo": [WsLevel | null, WsLevel | null]}`，
  `bbo[0]` = 最优买、`bbo[1]` = 最优卖，`WsLevel` 同 l2Book 的 `{px, sz, n}`。
  **任一侧可能是 `null`**（单边空簿），必须处理，不能直接下标解引用。
- 全簿流用 `l2Book`：`{"type":"l2Book","coin":"BTC","nSigFigs":null,"mantissa":null,"fast":false}`，
  推 `{coin, levels, time}`，形状和 REST 的 l2Book 一致。
- 每个 coin 一条 subscription，**没有批量订阅一个数组的写法**（和 Gate 的原子批订阅不同）。
- 限额：单 IP 最多 10 条连接、每分钟最多 30 条新连接、最多 1000 条订阅、
  用户维度订阅最多 10 个不同地址、每分钟最多 2000 条上行消息、最多 100 条 in-flight post。
- **UNVERIFIED**：ping/pong 心跳的具体要求和超时时长文档没写。
  文档只说 "all automated users should handle disconnects from the server side and gracefully reconnect"，
  且断线"可能不预告地周期性发生"。实现按"自己定时发心跳 + 无数据超时即重连"来写。
- 私有流：`orderUpdates`（`{type, user}`，推 `{order, status, statusTimestamp}[]`）、
  `userFills`（`{type, user, aggregateByTime}`，推 `{isSnapshot?, user, fills[]}`）。
  **这两个都用地址订阅，不需要签名**——Hyperliquid 的账户数据全是公开的。

## 资金费周期

- **每小时结算一次**，整点结算，`interval_s = 3600`。
  年化时 periods/year = 8760，**不是 1095**。拿一个 8 小时口径的数直接比会差 8 倍。
- 公式：`F = 平均溢价指数 P + clamp(interest_rate − P, −0.0005, +0.0005)`
  - `interest_rate` 固定 0.01%/8h = **0.00125%/h**（`0.0000125`）。这就是为什么文档示例里
    `funding` 常常正好是 `"0.0000125"`——那是溢价为 0 时的地板值。
  - 溢价 `premium = impact_price_difference / oracle_price`，
    **每 5 秒采样一次、在整点前的一小时内取平均**。
- **上下限：±4%/小时**（"Funding on Hyperliquid is capped at 4%/hour"）。
  `fetch_funding_limits` 返回 `(Decimal("0.04"), Decimal("-0.04"))`，顺序是 `(cap, floor)`。
  **UNVERIFIED**：文档只说 "capped at 4%/hour"，没有单独写下限；这里按对称绝对值上限理解。
  文档明说 "The funding cap and funding interval do not depend on the asset"，所以全市场同一个数，
  不需要逐币查。
- 符号：**正 = 多头付空头**（"the long position will pay the short position"），
  与 `CarryRate` 约定一致，**原样存，不取反**。
- 结算是**在整点一次性划转**，不是连续累计。
- 4%/h 折年化是 350 倍——**这个 cap 高得几乎不是 cap**。别指望像 Bybit 的 ±0.75%/8h 那样
  用 cap 去否决脏数据；Hyperliquid 这边极端费率是真的会发生。

## 符号与精度模型

### 币名

- 永续的符号就是**币名**：`BTC`、`ETH`、`SOL`、`HYPE`。没有 `USDT` 后缀，没有分隔符。
  与 `normalize_base` 的冲突点：其它六家要从 `BTCUSDT` / `BTC_USDT` / `BTC-USDT-SWAP` 里剥出 base，
  Hyperliquid 的 symbol **本身就是 base**，剥离逻辑要整个跳过（否则 `HYPE` 之类会被误伤）。
- **HIP-3 builder 市场的币名带 dex 前缀**：`{dex}:{coin}`，如 `xyz:XYZ100`。
  本适配器只做 `dex=""`，遇到带冒号的名字直接跳过。
- 现货是 `PURR/USDC` 或 `@107` 这种形式，与永续无关，不会出现在 `meta.universe` 里。
- **千倍前缀 `k`**：`kPEPE` / `kBONK` / `kSHIB` / `kFLOKI` / `kLUNC` / `kNEIRO` 之类。
  **UNVERIFIED**——我在官方文档里**没有找到**对这个前缀的定义，contract-specifications 页只说
  "1 unit of underlying spot asset"，perpetual-assets 页什么都没说。
  按行业惯例它等价于币安的 `1000PEPEUSDT`（1 单位 = 1000 个底层币），
  跨所配对时 `kPEPE ↔ 1000PEPE` 的 multiplier 应当是 1，但**在没有官方确认之前，
  这类币要么在 `fetch_instruments` 里显式标注、要么直接从可配对集合里排除**，
  乘数猜错就是 1000 倍的单腿裸奔。

### 数量精度

`sz` 一律是 **base 币数量**，`contract_size = 1`，没有"张"的概念。
按 `szDecimals` 取整：`lot_size = 10^-szDecimals`（BTC 的 `szDecimals=5` → 0.00001 BTC）。

### 价格精度（两条规则同时成立，缺一不可）

1. **最多 5 位有效数字**；
2. **小数位不超过 `MAX_DECIMALS − szDecimals`**，永续 `MAX_DECIMALS = 6`（现货是 8）；
3. **整数价永远合法**，不受有效数字限制（`123456` 合法）。

官方例子（永续）：`1234.5` ✓、`0.001234` ✓；`1234.56` ✗（6 位有效数字）、`0.0012345` ✗（7 位小数）。

实现顺序**必须是先规整有效数字、再夹小数位**（反过来会在夹完之后又超出有效数字）：

```python
def round_px(px: Decimal, sz_decimals: int, *, up: bool) -> Decimal:
    if px == px.to_integral_value():          # 整数价直接放行
        return px.to_integral_value()
    exp = px.adjusted()                       # 10^exp 量级
    q5  = Decimal(1).scaleb(exp - 4)          # 5 位有效数字的步长
    qdp = Decimal(1).scaleb(-(6 - sz_decimals))
    step = max(q5, qdp)                       # 两条规则取更粗的那个
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    return px.quantize(step, rounding=rounding)
```

`up` 的方向必须跟着单边走：**买单向下取（更保守）、卖单向上取**，
否则保护限价会被取整推到比意图更激进的一侧。

### 字符串格式化

价格和数量进 msgpack 的是**字符串**，且格式必须和官方 SDK 一致：

```python
def to_wire(x: Decimal) -> str:
    s = format(x.normalize(), "f")     # normalize 去掉尾随零，'f' 禁掉科学计数法
    return "0" if s == "-0" else s
```

- **科学计数法在两头都会咬人**，不只是小数那头。实测（Python 3.13）：
  `str(Decimal("0.00001").normalize())` → `'1E-5'`，
  `str(Decimal("50000.0").normalize())` → **`'5E+4'`**。
  后者尤其阴险——`50000` 正好是官方下单示例里的价格，一个整数价被 `str()` 变成 `5E+4` 发出去。
  所以 `format(..., "f")` 不是"顺手加的"，是必须的；任何地方用 `str(Decimal)` 拼 wire 值都是 bug。
- **尾随零要去掉**（官方点名的翻车点之一：`"50000.0"` vs `"50000"`），`normalize()` 负责这件事。
- **`-0` 的判断必须放在 format 之后，不能照抄 SDK**。官方 SDK 的 `float_to_wire` 是这么写的：

  ```python
  rounded = f"{x:.8f}"          # -0.0 → "-0.00000000"
  if rounded == "-0":           # ← 永远为 False，这一行是死代码
      rounded = "0"
  normalized = Decimal(rounded).normalize()
  return f"{normalized:f}"      # → "-0"
  ```

  实测确认：`f"{-0.0:.8f}"` 得到的是 `"-0.00000000"` 而不是 `"-0"`，所以那个 guard 从不触发，
  SDK 最终真的会把 `"-0"` 发上线。我们把判断放在格式化之后才真正拦得住。
- 顺带一提，SDK 的 `float_to_wire` 走 `float` 并在 8 位小数上做 `1e-12` 的回读校验，
  超差直接 `raise ValueError`。本适配器全程用 `Decimal`，不经过 float，天然没有这个失败模式——
  **不要为了"和 SDK 一致"把 Decimal 转回 float**。

### asset index

下单/撤单/改杠杆用的是 `a`/`asset`，值是 **`meta.universe` 里的下标整数**，不是币名。

- 映射表必须从 `fetch_instruments` 里建，并且 **`enumerate` 要在过滤 delisted 之前做**。
- **UNVERIFIED**：文档没写下标是否会被新上币复用。防御做法：
  **绝不把 asset index 持久化到磁盘或跨进程缓存**，每次启动重建，运行期定时刷新。
- HIP-3 市场是 `100000 + perp_dex_index * 10000 + index_in_meta`，现货是 `10000 + spot_index`——
  本适配器都用不到，但解析别的响应时看到 >= 10000 的 asset 要知道那不是永续。

### cloid（幂等 client order id）

- 格式：**`"0x" + 32 个十六进制字符**（128 bit / 16 字节）。官方 SDK 的校验就两条：
  前缀是 `0x`、去掉前缀后长度正好 32。
- 本项目的 `OrderRequest.client_order_id` 是任意字符串，必须做一次确定性映射：

```python
cloid = "0x" + hashlib.sha256(client_order_id.encode()).hexdigest()[:32]
```

  确定性是关键——`cancel_order(symbol, client_order_id)` 和 `query_order(...)`
  要能从同一个字符串再算出同一个 cloid。
- **UNVERIFIED**：重复 cloid 会不会被服务端拒绝（从而提供天然幂等）、以及 cloid 的唯一性窗口有多长，
  官方文档**没有任何说明**。第三方资料说会拒，但那不是官方来源。
  **所以绝不能把 cloid 当成防重放的护身符**：`/exchange` 请求超时必须抛 `OrderUnknown`，
  走 `cancelByCloid` → `orderStatus(cloid)` 的标准消解流程。这条路径不依赖服务端去重，两种行为下都正确。

## 错误码

Hyperliquid 没有数字错误码，全是**英文字符串**。分类如下。

### 逐单错误（HTTP 200，`status:"ok"`，藏在 `data.statuses[i].error`）→ `OrderRejected`

| 内部名 | 错误串 |
|---|---|
| Tick | `Price must be divisible by tick size.` |
| MinTradeNtl | `Order must have minimum value of $10.` |
| MinTradeSpotNtl | `Order must have minimum value of 10 {quote_token}.` |
| PerpMargin | `Insufficient margin to place order.` |
| ReduceOnly | `Reduce only order would increase position.` |
| BadAloPx | `Post only order would have immediately matched, bbo was {bbo}.` |
| IocCancel | `Order could not immediately match against any resting orders.` |
| BadTriggerPx | `Invalid TP/SL price.` |
| MarketOrderNoLiquidity | `No liquidity available for market order.` |
| PositionIncreaseAtOpenInterestCap | `Order would increase open interest while open interest is capped` |
| PositionFlipAtOpenInterestCap | 同上 |
| TooAggressiveAtOpenInterestCap | `Order rejected due to price more aggressive than oracle while at open interest cap` |
| OpenInterestIncrease | `Order would increase open interest too quickly` |
| InsufficientSpotBalance | `(Spot-only) Order has insufficient spot balance to trade` |
| Oracle | `Order price too far from oracle` |
| PerpMaxPosition | `Order would cause position to exceed margin tier limit at current leverage` |
| MissingOrder（撤单） | `Order was never placed, already canceled, or filled.` |

这些全是**明确拒绝、订单确定不存在**（`MissingOrder` 是"确定已终结"），可以安全重试或跳过。

注意 `IocCancel` 的语义：IOC 单一点都没吃到 → 这是**正常结果不是故障**，
在 500ms 收敛循环里会频繁出现，映射成 `OrderRejected` 后上层要当成"这一拍没成交"而不是报警。

### 整批预校验错误（`status:"err"`，`response` 是个字符串）

文档说某些错误是 payload 的确定性函数，会在逐单校验之前**整批拒掉**（例：空的 orders 数组、
非 reduce-only 的 TP/SL、价格离参考价太远）。这些同样是**进撮合之前**被挡下的 → 订单不存在 → `OrderRejected`。

签名/凭据类错误也走这条：

- `L1 error: User or API Wallet 0x0123... does not exist.`
- `Must deposit before performing actions.`

**这两条 99% 是签名构造错了**（msgpack 键顺序、source a/b、地址大小写、数字尾随零），
只有 1% 是真的没入金。**必须映射成不可重试的 `ExchangeError`，绝不能升级成 `OrderUnknown`**——
签名错的请求根本没进过撮合，白跑一轮 cancel+查单纯属浪费配额。

### HTTP 层

| 状态码 | 含义 | 映射 |
|---|---|---|
| 200 | 业务成败看 body 的 `status` 字段 | 见上 |
| 422 | `Failed to deserialize the JSON body into the target type` —— body 结构不对 | `ExchangeError(retriable=False)`，属于代码 bug |
| 429 | 限频 | `RateLimited(retriable=True)` |
| 5xx | 服务端错误 | `/info` 上可重试；**`/exchange` 上必须 `OrderUnknown`** |
| 连接超时 / 中断 | | **`/exchange` 上必须 `OrderUnknown`** |

**UNVERIFIED**：429 是否带 `Retry-After` header、以及 429 body 的具体形状，官方文档没写。

### 必须当 OrderUnknown 的情况

`/exchange` 上的：连接超时、读超时、连接被中断、5xx、以及任何**没能读到 `statuses` 数组**的 200 响应。
nonce 不是去重键，**重发一定会变成第二张单**。

## 限频

### IP 维度（对 `/info` 和 `/exchange` 都生效）

**总配额 1200 权重 / 分钟 / IP**。

| 请求 | 权重 |
|---|---|
| `/exchange` 任意 action | `1 + floor(batch_length / 40)`（单笔 = 1） |
| `l2Book` / `allMids` / `clearinghouseState` / `orderStatus` / `spotClearinghouseState` / `exchangeStatus` | 2 |
| `userRole` | 60 |
| 其余有文档的 info 请求（`meta` / `metaAndAssetCtxs` / `fundingHistory` / `userFees` / `frontendOpenOrders` / `userFillsByTime` …） | 20 |
| `recentTrades` / `historicalOrders` / `userFills` / `userFillsByTime` / `fundingHistory` / `userFunding` / `twapHistory` 等 | **额外**按每 20 条返回项再加权重 |
| `candleSnapshot` | 额外按每 60 条加权重 |
| Explorer API | 40，且每区块额外限 1 次 |

配额规划的直接后果：

- **榜单主力 `metaAndAssetCtxs` 权重 20 但一次拿全市场**，1 分钟跑 60 次也才 1200——单靠它撑不满。
- **真正吃配额的是 `fundingHistory`**：单币一次调用、权重 20 + 每 20 条再加权。
  200 个币 × 每币 2 页 = 400 次 × (20 + 25) ≈ 18000 权重 ≈ **15 分钟的全部配额**。
  历史回补必须限流 + 缓存 + 分批跨分钟摊开，绝对不能进任何轮询循环。
- `l2Book` / `clearinghouseState` 权重只有 2，风控轮询和盘口收敛应当优先用它们。
- 基类的三池分配（market 8 / trade 6 / risk 6）在这里要按权重而不是按并发来理解：
  建议把 `fundingHistory` 的回补单独放到 `market` 池并额外加一个令牌桶。

### 地址维度（**只对 action 生效，info 请求不受限**）

文档原文："Note that this rate limit only applies to actions, not info requests."

- 额度 = **累计成交 1 USDC 换 1 次请求**，初始白送 **10000 次**。
- 触顶后降为**每 10 秒 1 次请求**。
- 撤单有单独的更宽额度：`min(limit + 100000, limit * 2)`。
- 挂单数上限：默认 1000 张，每 5M USDC 成交量 +1，最多 5000 张；
  已有 1000+ 挂单时，reduce-only 单和触发单会被直接拒。
- 批量请求：**IP 维度算 1 次，地址维度算 n 次**。

**这是低频对冲策略最容易踩的坑**：10000 次初始额度看着多，但一个 500ms 收敛循环
每次调整都要 place + cancel，几天就能烧完；一旦触顶，每 10 秒才能发一个 action，
等于失去平仓能力。**必须监控 `{"type":"userRateLimit","user":...}` 的
`nRequestsUsed` / `nRequestsCap` / `nRequestsSurplus`，接近上限时主动降频甚至进 REDUCE_ONLY。**

## 凭据映射与 import 策略

### Credential 四个位怎么放

```
api_key    → 账户地址（主账户或子账户的 0x 地址）。所有 /info 查询用它。
api_secret → API Wallet（agent wallet）私钥，"0x" + 64 hex。只用于签名。
passphrase → vaultAddress（可选）。给金库/子账户下单时填，普通账户留空串。
uid        → 不使用，留空。
```

理由：

- `api_secret` 放私钥符合"secret 是要保密的那个"的直觉，也让现有的凭据脱敏/日志过滤逻辑天然覆盖它。
- `api_key` 放**账户地址**而不是 agent 地址——因为 agent 地址可以从私钥推出来，账户地址推不出来，
  而账户地址是 `clearinghouseState` / `userFees` / `frontendOpenOrders` 全都必需的。
  **这是最容易配错的一格**：填成 agent 地址会让 `fetch_positions` 返回空仓，
  而基类明确说仓位是真值 —— 引擎会认为裸奔并触发撤退平仓。
- 构造时应当做一次**一致性校验**：如果 `eth_account` 可用，从 `api_secret` 推出 agent 地址，
  当它**等于** `api_key` 时给一条 warning（"你填的是主钱包私钥而不是 API Wallet，
  权限过大且和网页端共享 nonce"）；当 `api_key` 为空时可以退化成"私钥即主钥"并自动填地址，
  但要在 `note` 里说清楚。
- `passphrase` 放 vaultAddress 是因为它进 action hash（`b"\x01" + 20 字节`），
  是一个"每次签名都要用、且属于凭据"的值，正好占住这个空位。

### import 策略（关键约束：没有 eth_account / msgpack 也要能跑公开端点）

`/info` 全部是**公开、不签名**的：榜单（`metaAndAssetCtxs`）、深度（`l2Book`）、
历史资金费（`fundingHistory`）、费率（`userFees`）、持仓（`clearinghouseState`）、
挂单（`frontendOpenOrders`）、查单（`orderStatus`）——**一个都不需要私钥**。
真正需要签名的只有 `/exchange` 的三条路径：`place_order` / `cancel_order` / `set_leverage`。

所以模块级采用"软导入 + 首次使用时报错"：

```python
try:
    import msgpack as _msgpack
    from eth_account import Account as _Account
    from eth_account.messages import encode_typed_data as _encode_typed_data
    from eth_utils import keccak as _keccak, to_hex as _to_hex
    _SIGN_IMPORT_ERROR: Exception | None = None
except ImportError as exc:          # 只在签名路径致命，公开端点不受影响
    _msgpack = _Account = _encode_typed_data = _keccak = _to_hex = None
    _SIGN_IMPORT_ERROR = exc


def _require_signing() -> None:
    if _SIGN_IMPORT_ERROR is not None:
        raise ExchangeError(
            Venue.HYPERLIQUID.value, "SIGNING_UNAVAILABLE",
            f"下单需要 eth_account + msgpack（pip install eth-account msgpack）：{_SIGN_IMPORT_ERROR}",
        )
```

约束：

- **`__init__` 里绝不调 `_require_signing()`**，也绝不用私钥去构造 `Account`——
  否则缺包时连"只看榜单"的用户都构造不出适配器。
- 私钥 → `Account` 对象**惰性构造并缓存**（第一次签名时构造），
  这样"配了空凭据只跑公开端点"也能正常工作。
- `_require_signing()` 放在 `place_order` / `cancel_order` / `set_leverage` 的第一行，
  以及任何走 `/exchange` 的私有方法开头。
- `keccak` 来自 `eth_utils`（`eth_account` 的传递依赖，装了 eth_account 就一定有），
  不要额外引入 `pycryptodome` / `sha3`。
- 暴露一个 `signing_available: bool` 属性给 UI，让界面能显示"只读模式"。

## 坑

1. **`metaAndAssetCtxs` 的两个数组按下标对齐**。长度不等要抛错，不能 zip 静默截断——
   截断之后全表符号错位，`BTC` 的费率会挂到别的币上，而且不报任何错。

2. **delisted 币仍占着 universe 的下标**。先 `enumerate` 再过滤，反了就是全表 index 左移，
   下单打到错误的币上。

3. **`clearinghouseState` 的 `user` 必须是账户地址不是 agent 地址**。
   填错返回的是一个合法的空账户，不报错，仓位读成 0 → 引擎判定裸奔 → 触发撤退。

4. **msgpack 键顺序 = 签名**。任何"顺手整理一下字段顺序"的重构都会让下单全线 401 等价错误。
   建议 action 用显式的字面量 dict 构造并加注释锁死顺序，不要用 `**kwargs` 或字典推导。

5. **`builder` 字段会进 msgpack**。它是 HL 的抽成机制（`{"b": 地址, "f": 十分之一基点}`），
   **本项目不使用，action 里绝不出现这个键**——一旦出现，msgpack 字节改变，
   连带签名和已有的测试全部失效。

6. **`status:"ok"` 不等于下单成功**。真正的成败在 `response.data.statuses[i]`，
   而且 `{"error": ...}` 就藏在 `"ok"` 里面。

7. **cancel 的 `statuses[i]` 类型不统一**：成功是裸字符串 `"success"`，失败是对象 `{"error": ...}`。
   直接 `st["error"]` 会在成功路径上抛 `TypeError`。

8. **`fundingHistory` 向"新"翻页且 `startTime` 闭区间**，用上一页末条时间戳当起点会重复收一条。
   基类明确要求去重，这里必须 `last_time + 1` 或按时间戳集合去重。

9. **每小时结算**。所有和"8 小时"绑死的年化/展示逻辑都要走 `interval_s`，
   硬编码 1095 会把 Hyperliquid 的年化算成实际值的 1/8。

10. **价格规整是两条规则的交集，且必须先有效数字后小数位**。
    只做 `quantize(tick)` 会在 `1234.56` 这种价上被 `Tick: Price must be divisible by tick size.` 拒。

11. **地址一律小写后再进签名**。`vaultAddress` 是按 bytes 解析的，大小写不一致直接签出另一个哈希。

12. **nonce 并发**。同毫秒的两个请求会撞车，必须有进程内单调递增发生器 + 锁。

13. **地址维度限频只对 action 生效，且初始只有 10000 次**。低频策略也会烧完；
    触顶后每 10 秒 1 个 action，等于失去及时平仓能力。要主动监控 `userRateLimit`。

14. **`orderStatus` 拿不到成交均价和手续费**，必须配 `userFillsByTime` 按 `oid` 聚合。

15. **HTTP body 的 JSON 序列化不影响签名**。这和 Gate/Bybit 完全相反——
    可以放心用 httpx 的 `json=` 参数，不需要手动序列化。但测试里要验的东西也变了：
    要验的是"从上线的 JSON body 里解析出的 action 重新 msgpack 之后，恢复出的 signer == agent 地址"。

## UNVERIFIED 汇总

1. `exchangeStatus` 的响应字段（`{specialStatuses, time}`）来自 QuickNode 镜像文档，官方 gitbook 正文没给示例。
2. `l2Book` 单侧返回多少档（未文档化，实测常见 20 档/侧）。
3. `k` 前缀（`kPEPE` / `kBONK` …）的确切倍数含义——官方文档**完全没提**。按行业惯例是 1000×，未经确认。
4. cancel 的 `f`（fast）字段到底在 action 层还是 cancel 条目层、以及 false 时是否必须省略。
   官方 SDK 两处都不发，本文档按 SDK 来。
5. 重复 cloid 是否被服务端拒绝、cloid 的唯一性保留窗口有多长——官方文档无任何说明。
6. `orderStatus` 在订单不存在时外层 `status` 的取值（推测 `"unknownOid"`）。
7. `orderStatus` 内层 `status` 的完整枚举（文档只列了 6 个，说还有 "15+ other status values"）。
8. `userFills.fee` 在做市返佣时是否为负数。
9. `userFees.userAddRate` 是否已含做市商返佣档（`feeSchedule.tiers.mm`）、能否为负。
10. 资金费下限是否严格对称为 −4%/h（文档只写了 "capped at 4%/hour"）。
11. 429 是否带 `Retry-After`、其 body 形状。
12. `asset index` 会不会被新上币复用（文档未说明；按"不复用"用，但绝不持久化）。
13. WebSocket 的 ping/pong 心跳要求与超时时长。
14. `$10` 最小名义额对 reduce-only 平仓单是否豁免（未找到官方说明；
    `MarketSpec(PERP).reduce_min_notional=False` 的默认行为与之无冲突，但残仓可能平不掉）。
15. `r`/`s` 用 `to_hex(int)` 生成的**不补零**十六进制串是官方 SDK 的写法；
    补齐 32 字节的写法是否同样被接受，未验证——照抄 SDK。

## 来源

- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/signing
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/error-responses
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications
- https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margin-tiers
- https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/master/hyperliquid/utils/signing.py
- https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/master/hyperliquid/utils/types.py
- https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/master/hyperliquid/exchange.py
- https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/master/hyperliquid/info.py
- https://www.quicknode.com/docs/hyperliquid/info-endpoints/exchangeStatus （仅用于 exchangeStatus 形状，非官方）
