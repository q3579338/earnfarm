# 跨所资金费套利 · 执行安全状态机设计

> 面向实现的设计文档。核心立场：**原工具的根本错误不是"没有止损"，而是把"两腿并发下单 + 无限重试补腿"当成了收敛逻辑。真正的收敛条件必须包含"放弃"这个终态。**

---

## 0. 三条设计公理

在展开之前先立三条，后面所有决策都从这里推出：

**公理 1：仓位是真值，订单是过程。**
任何时候本地记账与交易所 `position` 冲突，无条件以交易所为准。订单回执可以丢、可以迟到、可以撒谎（超时不等于失败），仓位快照不会。

**公理 2：不确定性要前置消化。**
先做不确定的事（难腿），再做确定的事（易腿）。这条同时决定了建仓顺序、平仓顺序，以及"超时后先 cancel 再查仓"的固定动作序列。

**公理 3：熔断停的是赚钱动作，不停保命动作。**
一刀切的"全局 halt"在有裸腿时等于自杀。熔断必须有白名单。

---

## 1. 单腿裸奔检测与撤退

### 1.1 敞口的定义（比"净敞口超阈值"更细）

先统一符号。两腿归一化到 **base 币计价的带符号数量**（币本位合约要先按张数×面值/mark 折算）：

```
net_qty   = pos_A + pos_B            # 理想为 0
gross_usd = (|pos_A| + |pos_B|)/2 * mark_ref
naked_usd = |net_qty| * mark_ref
```

**关键：dust 门槛必须由交易所过滤器推导，不能拍脑袋。** 两所的 `stepSize` / `minQty` / 合约面值 不同，会产生一个**结构性不可消除残差**。把它当裸奔会让状态机陷入"补 → 补不上（低于 minQty 被拒）→ 再补"的死循环——这是这类工具最常见的卡死原因。

```
step_notional = max(stepSize_A, stepSize_B, contractSize_A, contractSize_B) * mark_ref
DUST_USD      = max(5.0, 3 * step_notional, 1.5 * max(minNotional_A, minNotional_B))
```

四级分档（相对/绝对双阈值取更严者）：

| 级别 | 条件 | 含义 |
|---|---|---|
| `DUST` | `naked_usd < DUST_USD` | 结构性残差，**不触发任何动作**，只记账 |
| `SOFT` | `< min(0.4%·equity, 3%·gross_usd)` | 正常补腿窗口 |
| `HARD` | `< min(1.2%·equity, 10%·gross_usd)` | 缩短窗口，准备撤退 |
| `CRITICAL` | 超出上面 / 裸腿浮亏 > 0.5%·equity | 跳过重试，直接市价撤退 |

补充两个触发器，它们比金额更重要：

- **时间加权敞口**：`∫naked_usd dt` 超过 `EXPOSURE_BUDGET_USD_S`（建议 = `0.3%·equity × 30s`）也升级。防止"一直 SOFT 但裸了 5 分钟"。
- **波动率加权**：`risk = naked_usd × σ_1m × √(t/60)`。山寨币的 SOFT 和 BTC 的 SOFT 不是一回事，阈值必须除以 `σ_1m / σ_1m(BTC)`。

### 1.2 "补腿无望"判据（这是原工具完全缺失的部分）

分成**软判据**（累计计分，超时才判死）和**硬判据**（命中即刻判死，一次重试都不做）。硬判据是关键——很多情况下重试 100 次也不可能成功，浪费的每一秒都是纯风险。

**硬判据（命中 → 立即 `UNWIND`）：**

1. `symbol.status != TRADING`（下架 / 结算 / 交割 / 暂停）
2. 交易所把该合约置为 **reduce-only 模式**（Binance `-2010` 配合 symbol 状态、Bybit `Reduce-only order is required`）——这是"永远开不进去"的最强信号
3. 该账户 **可用保证金不足以开这一腿**，且同交易所内无可划转余额
4. 账户被限制开仓（风控冻结、KYC 限制、风险限额已满且无法提升）
5. 交易所进入维护窗口（从公告 API / `/systemStatus` 读到）
6. 私有 WS + REST **双通道对该交易所全部不可用** > `T_EXCHANGE_DEAD (10s)`

**软判据（任一命中 → 判死）：**

7. 重试预算耗尽：`T_repair` 超时 **或** 重试次数 `N_repair` 用完（先到先算）
8. 连续 `M_gate_fail = 8` 拍盘口不达标（深度 < 需求 × 1.5，或 spread > `SPREAD_MAX_BP`）
9. **经济判据**（最容易被忽略）：
   `预估补腿冲击成本 > K_econ × 预估平掉已成交腿的成本`，`K_econ = 2.0`
   补腿只有在"比撤退便宜"时才值得做。空腿市场流动性枯竭时这个判据会先于超时触发，正确。

**重试预算建议值：**

| 敞口级 | `T_repair` | `N_repair` | 退避序列 (ms) |
|---|---|---|---|
| SOFT | 20 s | 12 | 200, 300, 500, 800, 1200, 1500… |
| HARD | 6 s | 5 | 150, 300, 500, 800, 1000 |
| CRITICAL | 0 | 0 | — |

补腿单本身用 **IOC marketable limit**（对手价穿 `REPAIR_CROSS_BP = 8bp`），**不要用 FOK**（见 §2.4），也不要用纯市价（防插针）。

### 1.3 撤退阶梯（市价 vs 限价的正确答案：都不是，是升级阶梯）

单一选择必然错：小额裸奔用市价是浪费；大额裸奔一笔市价会打穿多档，滑点可能超过裸奔本身的风险。正确结构是**时间驱动的升级阶梯**，越晚越激进。

**Phase −1（前置，强制，不可跳过）：终结在途订单**

```
1. 对本单元所有 in-flight clientOrderId 执行 cancel（幂等）
2. 等待每个订单进入终态（FILLED / CANCELED / EXPIRED / REJECTED）
3. 用终态里的 executedQty 重算 net_qty
4. 拉一次 REST position 快照校准
```

**为什么这一步是硬性的**：你判定 leg_B 失败准备平 leg_A，但 leg_B 的成交回执只是延迟了 800ms。你不 cancel 就去平 leg_A，2 秒后 leg_B 成交回执到达 —— 你现在从"多头裸奔"变成"空头裸奔"，而且方向反了、量翻倍了。**必须先让在途订单不可能再成交，再决定平多少。** 这是整个撤退逻辑里最容易写错、代价最大的一步。

**Phase 0（t = 0–2 s）：Marketable IOC**
限价 = 对手价 ± `UNWIND_CROSS_BP_0 = 5bp`，`reduceOnly=True`，单次量 ≤ top-3 档累计可吃量 × 0.6。不成交就每 300ms 重下。
——效果接近市价，但有滑点上限保护，能挡住插针和深度真空。

**Phase 1（t = 2–6 s）：递进穿价 + 切片**
每 400ms 把穿价幅度按 `5 → 10 → 20 → 40 bp` 递增，单片仍受深度约束。适用于中大额，避免一次性砸穿。

**Phase 2（t > 6 s，或 CRITICAL 直接进入）：真市价 / 深穿价**
`MARKET` 单一次清剩余。
**注意 fallback**：部分交易所对小币禁用 MARKET 或有 `MARKET_LOT_SIZE` 上限。被拒 → 降级为穿价 `200bp` 的 IOC 限价，分 3 片。

**Phase 3（Phase 2 连续失败 2 次）：跨市场中和 (`CROSS_HEDGE`)**
这是"撤退失败怎么办"的真正答案：**中和敞口不必在原市场平仓**。按优先级找替代腿：

1. 同交易所同标的的**另一合约**（U 本位 ↔ 币本位 ↔ 当季交割）
2. 同交易所的**现货**（做多裸腿 → 卖出等值现货；需要有现货余额或统一账户）
3. **第三个交易所**的同标的永续
4. **高相关替代品**（BTC/ETH 对山寨的 beta 对冲，仅作最后手段，`β` 用 30 天回归，标记为"不完全对冲"）

成功后进入 `BALANCED_SYNTHETIC`：敞口中和了，但腿分布在非计划市场，**禁止继续赚资金费，标记待人工整理**，并进入软熔断。

**Phase 4：全部失败 → `HALTED` + 保命模式**
不再尝试成交，转为**防爆仓**：向裸腿账户划入所有可用保证金、降低该账户其他仓位、持续告警（微信 / TG，每 60s 重发直到人工 ack）。

### 1.4 撤退过程本身的失败处理

| 失败模式 | 处理 |
|---|---|
| `reduceOnly` 单被拒（`-2022`，仓位已为 0） | 说明仓位已被别的路径平掉 → 强制 `RECONCILE`，不要重下 |
| **平仓单必须 `reduceOnly=True`** | 否则仓位若已被 ADL/强平清掉，普通单会**开出反向仓**。这是撤退里最阴的坑，硬性要求 |
| 部分成交 | 更新剩余量继续当前 Phase，不重置计时器 |
| 撤退中收到迟到的对腿成交 | 重算 `net_qty`；若符号翻转，**原地掉头**（撤退方向反转），不要走完旧计划 |
| 限频 429 | 撤退路径走**预留配额**（见 §4.6），且退避上限 500ms 封顶——风控路径不接受长退避 |
| 交易所整体不可读 | → `FROZEN`（**不下任何单**）。在看不见仓位时下单是最糟选择 |

### 1.5 撤退后的自适应

一次 `UNWIND` 后：
- 该 symbol 冷却 `T_COOLDOWN = 10 min`
- 下次建仓的**切片大小 × 0.5**（指数缩容），连续 3 次成功后才逐步恢复
- 24h 内同 symbol 撤退 ≥ 2 次 → 拉黑该 symbol 直到人工解除

---

## 2. 两腿原子性：把窗口压到最小

### 2.1 结论先行

**默认策略：串行 + 难腿优先 + 第二腿数量 = 第一腿实际成交量 + 分片建仓。**

这和"并发窗口更短"的直觉相反，理由在下面。

### 2.2 并发 vs 串行的真实权衡

| | 并发 | 串行（难腿先） |
|---|---|---|
| 敞口窗口 | `max(lat_A, lat_B)` ≈ 30–80 ms | `t_fill_A + lat_B` ≈ 150–800 ms |
| 结果空间 | 4 种（成成/成败/败成/败败） | 2 种有意义（难腿不成 → **零敞口**；难腿成 → 补易腿） |
| 裸奔概率 | `P_fail_A + P_fail_B` | `P_fail_easy \| A已成` ≈ 极小 |
| 裸奔时长期望 | 可能很长（若失败的是难腿） | 短（要补的一定是易腿） |
| 数量错配 | **天然存在**（两腿都按目标量下，一腿部分成交即错配） | **结构上不可能** |

关键论证：**并发是"窗口的最小值"，串行是"尾部风险的最小值"**。资金费套利的收益是 bp 级、可预测的；裸奔的损失是尾部、不可预测的。优化目标应该是**期望损失**而不是**平均窗口**。一次难腿裸奔 30 秒的损失，能吃掉几百次省下的 50ms 窗口。

并且串行有一个更重要的隐性收益：**它把"补腿失败"这个事件从"可能发生在难腿上"变成了"只可能发生在易腿上"**。§1.2 里那堆"补腿无望"的硬判据（流动性枯竭、reduce-only、深度不足），绝大多数只会命中难腿。串行等于把最坏情况从状态空间里删掉了。

**并发只在一种情况下可接受**：两腿都是 BTC/ETH 级深度、单片名义 < `PARALLEL_MAX_USD (≈ $5k)`、且历史拒单率 < 0.5%。做成配置项按 symbol 自动选择：

```
mode = PARALLEL if (min(depth_score_A, depth_score_B) > 0.9
                    and slice_usd < PARALLEL_MAX_USD
                    and max(reject_rate_A, reject_rate_B) < 0.005)
       else SEQUENTIAL_HARD_FIRST
```

### 2.3 "难腿"的判定公式

不是简单看流动性，用综合硬度分，每 60s 用滚动统计更新：

```
hardness = est_slippage_bp(slice_qty, orderbook)      # 按 top-N 档模拟吃单
         + spread_bp / 2
         + reject_rate_ewma * 200                      # 拒单率权重最高
         + latency_p95_ms / 20
         + (30 if funding_settle_within_60s else 0)    # 结算窗口附近更难
         + (50 if ws_degraded else 0)
先发 hardness 大的那一腿。
```

### 2.4 FOK 是错的，改用 IOC

原工具用 FOK 的动机是"避免部分成交"，但代价是：**FOK 拒单率高得多**，并发场景下"A 的 FOK 成了、B 的 FOK 因为深度差 0.3% 被拒"是高频事件——FOK 反而**制造**了更多裸奔。

正确方案：
- **难腿：IOC 分片**，接受部分成交。实际成交多少就是多少。
- **易腿：IOC，数量 = 难腿的 `executedQty`**（向下取整到公共 stepSize）。
- 数量错配问题因此在结构上消失。

取整规则很重要：
```
common_step = lcm_like(stepSize_A, stepSize_B)   # 实际用 max + 向下对齐
qty_leg2 = floor_to(executed_qty_leg1, common_step)
残差 = executed_qty_leg1 - qty_leg2   # 若 > DUST 则立刻把难腿多出的部分削掉（reduceOnly）
```
最后这一句是很多实现漏掉的：**与其留一个补不上的残差裸着，不如把第一腿多出来的部分削平**。削平走的是同一条 `UNWIND` 通道，只是量很小。

### 2.5 分片：最有效的一招

做不到原子，就把不原子的代价除以 N。

```
n_slices = ceil(target_notional / SLICE_MAX_USD)
SLICE_MAX_USD = min(
    NAKED_HARD_USD * 0.8,                 # 单片全裸也不超过 HARD 档
    0.15 * top5_depth_usd(难腿),           # 不超过难腿浅档深度的 15%
    5000 * liquidity_tier_multiplier
)
片间隔 = SLICE_INTERVAL_MS (300~1000, 加 ±30% 抖动防被识别)
```

代价：建仓总时长变长（basis 可能在建仓期间跑掉）、手续费档位、被抢跑。收益：**单次事故的最大敞口从"全仓"降到"一片"**。这个交换在资金费套利里绝对划算——资金费是持有期收益，晚 30 秒建完仓损失微乎其微。

### 2.6 窗口压缩的工程手段（把"拍内"只剩发单）

1. **预热连接**：TCP + TLS 长连接常驻，禁用 Nagle；优先用 **WS 下单通道**（Binance WS API / Bybit WS Trade），比 REST 省 20–40ms 且不吃 REST 权重
2. **预设杠杆**：`PREFLIGHT` 阶段完成，绝不在下单路径上调杠杆（原工具的"调杠杆失败作废加仓"说明它在热路径上做这件事）
3. **预取过滤器**：`exchangeInfo` / `instruments-info` 缓存，每 5 min 刷新（**必须刷新**，交易所会动态调整 `maxLeverage` / `priceFilter`）
4. **时钟同步**：NTP + 每 5 min 校准 `serverTime` 偏移，`recvWindow = 3000`（不要设 60000，那会让幽灵单在你以为超时后才被撮合）
5. **单元级 in-flight 锁**：**主循环每拍必须检查上一拍订单是否已终结**。原工具 500ms 一拍并发下单，如果订单 800ms 才有回执，第二拍会重复下单 —— 这本身就能造成双倍仓位。锁 + 幂等键 `clientOrderId` 两道保险。

---

## 3. 爆仓距离 → 可执行动作

### 3.1 先修正指标本身

原工具"界面显示爆仓概率但执行器不读"只是接线问题；更深的问题是**指标选错了**。

**问题 A：对冲仓不该看组合爆仓距离，要看"亏损腿所在账户"的爆仓距离。**
两腿在不同交易所 = 两个独立的保证金池。组合永远 delta 中性、总权益几乎不动，但亏损那一腿的账户可以单独爆掉。所以：

```
risk_leg = argmin over legs of MR_account(leg)
```

**问题 B：价格距离在全仓下会骗人**，因为同账户其他仓位的盈亏会改变 `liq_price`。用双指标取更保守者：

```
d_liq = |mark - liq_price| / mark                        # 价格距离
MR    = account_equity / maintenance_margin_total        # 保证金率倍数（>1 存活）
```

**问题 C：必须给 basis spike 留缓冲。** 两所 mark price 短暂背离（单边插针、指数源故障）时，亏损腿会先爆。缓冲取该 symbol **90 天最大 5 分钟 basis 偏离** 的 1.5 倍。

### 3.2 阈值必须按波动率缩放

硬编码 25% 对山寨币是自杀（山寨 8h 波动可以 15%）。

```
σ_8h = max(realized_vol_8h_30d_p90, atr_8h / mark)
d_yellow  = clamp(6.0 * σ_8h + basis_buffer, 0.10, 0.45)
d_orange  = clamp(3.5 * σ_8h + basis_buffer, 0.06, 0.30)
d_red     = clamp(2.0 * σ_8h + basis_buffer, 0.04, 0.20)
d_black   = clamp(1.2 * σ_8h + basis_buffer, 0.025, 0.12)
```
BTC（σ_8h≈1.2%）→ yellow≈10%，red≈4%；某山寨（σ_8h≈6%）→ yellow≈40%，red≈14%。合理。

### 3.3 分档动作表

| 档 | 触发（`d_liq` 或 `MR` 任一命中） | 动作 |
|---|---|---|
| **GREEN** | `d > d_yellow` 且 `MR > 4.0` | 正常，允许加仓 |
| **YELLOW** | `d ≤ d_yellow` 或 `MR ≤ 4.0` | **停止加仓**（`PREFLIGHT` 直接拒），允许补腿/减仓；告警 |
| **ORANGE** | `d ≤ d_orange` 或 `MR ≤ 2.5` | ① 优先**同所内划转保证金**（spot/资金账户 → 合约，秒级）<br>② 划不动 → **两腿等比 reduceOnly 减仓 25%** |
| **RED** | `d ≤ d_red` 或 `MR ≤ 1.6` | 两腿等比减仓至 `MR ≥ 3.0`；**减仓走同一串行状态机**，且用 marketable IOC 不用挂单 |
| **BLACK** | `d ≤ d_black` 或 `MR ≤ 1.25` | 全平 + 硬熔断 + 人工告警 |

**三个必须写进代码的细节：**

1. **减仓必须两腿等比同时减**，否则减仓过程本身在制造裸奔。走 §1/§2 同一条串行通道，`reduceOnly=True`。
2. **RED/BLACK 时的顺序例外**：救火优先级高于敞口。此时**先减危险账户那一腿**（用市价），哪怕短暂制造敞口——爆仓是本金归零，短暂敞口是 bp 级。这是"难腿优先"原则唯一的例外，要在代码里显式注释。
3. **迟滞（hysteresis）**：升档立即，降档要求指标改善 20% 且持续 60s，否则会在阈值边缘反复减仓。

### 3.4 与"停止加仓"的接线

`PREFLIGHT` 闸门必须包含：`risk_tier == GREEN`。这就是原工具缺失的那根线——**风控档位是建仓的前置条件，不是一个显示器**。

---

## 4. 异常态处理与对账

### 4.1 WS 断连（行情 vs 私有，处理完全不同）

**行情 WS**（可降级）：

| 陈旧时长 | 动作 |
|---|---|
| > 1.5 s | 闸门关闭，**禁止新开仓**（补腿仍允许，用 REST 现拉盘口） |
| > 5 s | 切 REST 轮询降级模式（1 Hz），标记 `ws_degraded=True`（影响 hardness 评分） |
| > 20 s | 视作**该市场不可用**：若有裸奔 → 触发 `UNWIND` 硬判据（在瞎子状态下裸奔是最危险的组合） |

**私有 WS**（订单/持仓，不可降级，更严重）：
错过成交回执 = 本地仓位错误 = 所有决策错误。

| 时长 | 动作 |
|---|---|
| > 3 s | 进入 `DEGRADED`，立即触发一次全量 REST 对账 |
| > 15 s | **只允许 `reduceOnly` 操作**，禁止一切开仓，直到重连 + 对账通过 |
| 重连后 | 强制全量对账 + 拉 `userTrades` 补齐断连期间的成交，**不能只靠增量续传** |

**序列号缺口检测**：Binance 的 `u`/`U`、Bybit 的 `seq`。发现缺口 → 等同于断连，强制全量对账。这个检查很多实现漏掉，缺口比断连更隐蔽。

**listenKey 续期**：每 30 min 一次，失败重试 3 次后重建连接。

### 4.2 REST 超时 —— 最危险的误判

**铁律：超时 ≠ 失败。超时后订单可能已成交。**

处理流程（严禁"超时就重发"）：

```
1. 每个下单请求携带 clientOrderId = f"{unit_id}-{leg}-{seq}"   # 幂等键
2. 超时 (T_ORDER_ACK = 3s) → 不重发，进入 ORDER_UNKNOWN
3. ORDER_UNKNOWN 的固定动作序列：
   a. cancel_by_client_id(cid)    # 幂等；让订单不可能再成交
      - 返回 "unknown order" / "already filled" 都是有效信息
   b. query_order(cid)            # 拿终态 executedQty
   c. 若 a/b 都拿不到 → 拉 position + balance 快照，用差分推断实际成交
4. 20s (T_UNKNOWN_MAX) 内解决不了 → FROZEN
```

**为什么必须是 "先 cancel 再查"而不是"先查再定"**：直接查询有竞态——你查到 `NEW`，但下一毫秒它成交了。`cancel` 是一个**状态终结操作**，执行后订单只可能是 `CANCELED` 或 `FILLED`，竞态窗口关闭。

**为什么不能靠猜**：
- 猜"已成交"、实际未成交 → 你以为对冲好了，实际在裸奔且毫不知情（最坏）
- 猜"未成交"去重下 → 实际已成交则变双倍仓位（裸奔 + 超杠杆）

两个都不可接受，所以**不猜，用 position 快照把未知在 1–2 秒内变成已知**。这是公理 1 的直接应用。

**`FROZEN` 状态的语义**：交易所只读能力丧失 → **停止一切下单**（包括平仓），只保留看门狗和告警。在看不见仓位时下单，可能把一个小问题变成灾难。

### 4.3 持仓与本地记录不一致：三层对账

| 层 | 频率 | 数据源 | 用途 |
|---|---|---|---|
| **L1 轻对账** | 每次 WS position/order 推送 | WS | 实时校验，发现即标记 |
| **L2 全量对账** | 每 15 s，以及每次状态迁移到 `BALANCED` 前 | REST `positionRisk` + `openOrders` + `balance` | 权威快照，覆盖本地 |
| **L3 流水对账** | 每 120 s | REST `userTrades`（从 `lastTradeId` 增量）+ `income` | 重放计算仓位；**唯一能发现 ADL / 强平 / 手动干预 / 其他程序下单的层** |

**差异处理（永远以交易所为真）：**

```
diff = |exch_pos - local_pos| * mark
diff < DUST                    → 静默修正
diff < NAKED_SOFT              → 修正 + 告警 + 暂停开仓 60s
diff ≥ NAKED_SOFT              → 修正 + 硬熔断（有别的东西在动这个账户）
出现本地完全不知道的 symbol 持仓 → 硬熔断，必须人工
```

**ADL / 部分强平的特殊处理**（L3 能在 `income` 里看到 `type=ADL` / `LIQUIDATION`）：
交易所单方面砍掉你一腿。**正确反应是对另一腿做等量 reduceOnly 减仓，而不是把被砍的腿补回去。** ADL 发生说明该市场处于极端状态，补回去大概率再被砍一次，而且是在最差价位。补回去是在跟交易所的风控引擎对赌。

### 4.4 其他真实故障模式（都要写进错误分类器）

| 现象 | 处理 |
|---|---|
| HTTP 200 但 body 是错误码 | 按 body 判定，不看 HTTP status |
| `-1021` 时间戳失效 | 重新校准 serverTime，不计入失败计数 |
| `-1013` / `PERCENT_PRICE` 带式限价拒单 | 重算价格夹到 `[minPrice, maxPrice]`；连续命中 → 说明市场极端，计入软判据 |
| `-2022` reduceOnly rejected | 仓位已为 0 → 强制 `RECONCILE`，不重下 |
| `-4131` 对手方流动性不足（GTX/FOK） | 直接计入"补腿无望"软判据 |
| 双向持仓模式 (hedge mode) | `PREFLIGHT` 检查 `positionSide` 配置；模式错会开出反向仓而非平仓 |
| 保证金模式被外部改动（cross↔isolated） | L2 对账时校验，不一致 → 软熔断 |
| 合约下架 / 移仓公告 | 每 10 min 拉公告 API；**下架前 24h 停止该 symbol 新开仓** |
| 交易所计划维护（Bybit 有固定窗口） | 维护前 30 min 停开仓，前 10 min 主动平仓至 0 |
| 资金费结算时点 | 结算前后 ±30 s 禁止建仓（此时 mark 跳变 + 深度变差 + 有些所限制操作） |

### 4.5 限频：给风控路径留专用车道

**这条极其实战**：不能因为查行情把权重打满，导致平仓单被 429 挡在门外。

```
全局令牌桶按交易所权重配额分池：
  MARKET_DATA   : 40%
  TRADING_NORMAL: 30%
  RISK_RESERVED : 30%   ← 只有 UNWIND / DELEVER / cancel / reconcile 能用
RISK_RESERVED 池的退避上限 500ms（普通池可退避到 5s）
```

### 4.6 交易所侧死人开关（必装）

程序崩溃、机器断电、进程 hang —— 本地一切逻辑都失效。唯一还生效的保护是**交易所侧的自动撤单**：

- Binance Futures: `POST /fapi/v1/countdownCancelAll`，`countdownTime=30000`，**每 10 s 续期**
- Bybit: `set-dcp`（Disconnection Protect），10 s 窗口

程序死了 → 30 秒后交易所自动撤掉所有挂单。这不能平仓，但能防止"幽灵挂单在无人看管时成交"。

配套**本地看门狗**（独立线程 + 独立 API key）：主循环心跳停止 > 10 s → 看门狗接管，执行 cancel-all + 若有 `naked > HARD` 则市价平掉裸腿 + 告警。

---

## 5. 熔断

分两级，**软熔断停开仓，硬熔断停策略但保留保命白名单**（公理 3）。

### 5.1 软熔断（`no_new_position`，自动恢复）

| 触发 | 冷却 |
|---|---|
| 连续 3 次两腿建仓失败 | 5 min |
| 30 min 内 `UNWIND` 事件 ≥ 3 | 30 min |
| 单笔实际滑点 > 3× 预期 且 > 15 bp | 10 min（该 symbol） |
| 滚动 10 笔平均滑点 > 2× 预期 | 15 min |
| 行情降级 / 私有 WS `DEGRADED` | 恢复即解除 |
| 风控档位 ≤ YELLOW | 回到 GREEN 且持续 60s |
| L2 对账中等差异 | 60 s |
| 资金费率符号翻转（套利前提消失） | 转 `CLOSING` |

### 5.2 硬熔断（`HALTED`，必须人工 ack）

| 触发 | 说明 |
|---|---|
| **账户总权益 15 min 内下跌 > 1.5%，或 1h > 3%** | **最重要的一条**。对冲仓的权益本应几乎不动，权益骤降本身就是"对冲已失效"的最强信号，且**不依赖你识别出具体是哪种故障**。这是兜底总闸 |
| 撤退阶梯走完（Phase 3 后仍有 HARD 裸奔） | |
| `naked ≥ CRITICAL` 且持续 > 60 s | |
| L2/L3 对账出现无法解释的仓位 | 有别的东西在动这个账户 |
| 检测到 ADL / 强平事件 | |
| 5 min 内 API 错误率 > 25% | |
| 风控档位 = BLACK | |
| 同 symbol 24h 内熔断 2 次 | 该 symbol 永久拉黑至人工解除 |
| 两所 basis 偏离 > 90 天 p99.5 | 指数源故障 / 一边要下架 |

### 5.3 `HALTED` 之后做什么（关键：不是什么都不做）

```
if naked > DUST:          → 保命白名单：继续执行 UNWIND（平敞口）
if risk_tier <= ORANGE:   → 保命白名单：继续执行 DELEVER（防爆仓）
if 敞口=0 且 risk=GREEN:  → 真冻结：持仓保留（资金费还在赚），停止一切调仓
永远执行：cancel 所有挂单、续期 countdownCancelAll、告警循环（60s 重发直到 ack）
永远禁止：任何增加名义敞口的下单
```

---

## 6. 杠杆策略

### 6.1 原假设"全仓下杠杆不影响爆仓价"何时不成立

这个假设在**教科书条件下**（逐笔全仓、仓位大小固定、档位不变）近似成立，但实盘有五个破口：

1. **逐仓模式**：直接不成立。若保证金模式被外部改动或某 symbol 强制逐仓（新币常见），假设立刻失效。
2. **档位（tier）跳变**：MMR 由**名义价值档位**决定。名义规模跨过 tier 边界，MMR 阶跃上升，`liq_price` 突然跳一大截。贴着边界建仓 = 一个 tick 的价格变动就跳档。**建仓名义必须避开 tier 边界 ±5%。**
3. **交易所动态下调最大杠杆**（最要命）：新币上市初期高杠杆、之后调低；波动期交易所会突降某 symbol 的杠杆上限**并强制降低用户已设杠杆**。如果你贴着上限，一旦降档：你的名义可能超出新档位允许 → 无法加仓、部分交易所会强制减仓、且 MMR 上升导致 `liq_price` 逼近。**贴着最高杠杆 = 贴着悬崖。**
4. **仓位大小不是固定的**：高杠杆 → 初始保证金占用低 → 可用保证金看起来充裕 → 策略若按可用保证金自动放大仓位（绝大多数会），实际风险就是被杠杆放大的。作者的假设只在"仓位外生给定"时成立。
5. **Bybit 风险限额 / 统一账户 haircut**：风险限额分档决定 MMR 且需手动提升；统一账户里现货抵押品的折算率会被调整，权益凭空缩水。

### 6.2 反方向也有代价（不要一味调低）

低杠杆 = 同样仓位占用更多初始保证金 = `availableMargin` 更少 = **可能下不进补腿单和 reduceOnly 平仓单**。撤退时发现"没钱下单"是灾难性的。

所以：**杠杆不是风控手段，仓位规模才是。** 杠杆的正确角色是"给保证金留出操作空间"，风险由有效杠杆控制。

### 6.3 推荐做法

```python
L_eff_target = 3.0          # 总名义 / 总权益，两腿合计（即每腿 1.5x）
SAFETY_K     = 3.0

lev_set = clamp(
    ceil(L_eff_per_leg * SAFETY_K),           # 够用：占用少，留出下单空间
    3,
    min(
        max_lev_current(symbol),
        max_lev_hist_min_90d(symbol),          # 防交易所降档
        20                                      # 硬上限
    )
)
# 且校验：设定后的名义不落在任何 tier 边界 ±5% 内，否则下调 slice 规模避开
```

配套硬规则：

1. **有效杠杆 `L_eff = Σ|notional| / total_equity ≤ 3.0`**，这是真正的风控变量，每拍计算，超限即停止加仓。
2. **每个交易所账户权益缓冲 ≥ 40%**（`MR ≥ 4` 对应）。
3. **救援金必须放在每个交易所内部**（spot / 资金账户），不能只在一边。**跨所提币在需要它的时候大概率不可用**（拥堵、暂停、风控审核），分钟级延迟对防爆仓毫无意义。同所内部划转是秒级，这是唯一可靠的紧急补给通道。
4. **每 5 min 刷新 `maxLeverage`**，检测到交易所下调 → 立即评估当前名义是否超出新档位 → 超出则主动等比减仓，不等交易所来强制。
5. **设杠杆失败的处理**：保留原工具"设杠杆失败 → 两腿加仓都作废"的正确做法，但要区分错误原因——"有持仓时不能降杠杆"是可预期的，跳过即可，不要进入死循环重试。

---

## 7. 完整状态机图

```
                         ┌─────────┐
                         │  IDLE   │◄────────────────────────┐
                         └────┬────┘                          │
              信号 & 熔断未触发 │                               │ 冷却结束
                              ▼                                │
                       ┌──────────────┐   任一闸门失败          │
                       │  PREFLIGHT   │─────────────────────────┤
                       └──────┬───────┘                         │
   闸门: 双边行情<1.5s / 深度≥1.5x / 保证金充足 / 杠杆已设       │
        / risk_tier==GREEN / L_eff<3 / 无软熔断 / symbol正常     │
                              │ 通过                            │
                              ▼                                 │
                     ┌────────────────┐  未成交/拒单(零敞口)     │
                     │ LEG1_PENDING   │─────────────────────────┤
                     │ (难腿 IOC 分片) │                         │
                     └───┬────────┬───┘                         │
             成交 qty>dust│        │ 超时/无回执                  │
                         ▼        ▼                             │
                  ┌──────────┐  ┌──────────────┐                │
                  │ EXPOSED  │  │ORDER_UNKNOWN │                │
                  │  t0 计时  │  │ cancel→查仓   │                │
                  └────┬─────┘  └──┬────────┬──┘                │
                       │           │推断成功  │ >20s             │
                       │           └────►    ▼                  │
                       ▼                 ┌────────┐             │
              ┌────────────────┐         │ FROZEN │             │
              │  LEG2_PENDING  │         │ 停止下单│             │
              │ qty=leg1实际成交│         └───┬────┘             │
              └──┬──────┬──────┘             │恢复              │
        完全成交  │      │ 未成交/部分/拒单     ▼                 │
                 │      ▼                 ┌───────────┐         │
                 │  ┌────────┐            │ RECONCILE │         │
                 │  │ REPAIR │            └─────┬─────┘         │
                 │  │退避重试 │                  │               │
                 │  └─┬───┬──┘                  │               │
                 │补齐│   │ 无望判据/超时/HARD    │               │
                 ▼    ▼   ▼                     │               │
            ┌──────────────┐              ┌─────┴──────┐        │
            │   BALANCED   │◄─────────────┤  修正本地   │        │
            │ 收资金费/对账 │              └────────────┘        │
            └──┬───┬───┬───┘                                    │
   risk<=ORANGE│   │   │ 退出信号/资金费翻转                      │
               ▼   │   ▼                                        │
        ┌─────────┐│┌──────────┐                                │
        │ DELEVER ││ │ CLOSING  │──── 平完 ──────────────────────┤
        │两腿等比减││ │难腿先, IOC│                                │
        └────┬────┘│ └──────────┘                               │
             │     │                                            │
             │     │ 对账不一致 → RECONCILE                       │
             ▼     ▼
        ┌──────────────────────────────────────┐
        │              UNWIND                   │
        │ Phase-1: cancel在途 + 确认终态(强制)   │
        │ Phase 0 (0-2s): IOC 对手价+5bp 切片    │
        │ Phase 1 (2-6s): 穿价 5→10→20→40bp     │
        │ Phase 2 (>6s) : MARKET / 深穿价200bp   │
        │ Phase 3       : CROSS_HEDGE 跨市场中和 │
        │ Phase 4       : HALTED + 保命          │
        └───┬──────────────┬────────────┬───────┘
     敞口<=dust│        跨市场成功│      全部失败│
            ▼             ▼            ▼
      ┌─────────┐  ┌──────────────────┐  ┌────────┐
      │  FLAT   │  │BALANCED_SYNTHETIC│  │ HALTED │
      │ →冷却    │  │ 敞口中和,待人工   │  │保命白名单│
      └────┬────┘  └──────────────────┘  └────────┘
           └────────────────────────────────────────┘
```

**统一原则**：`LEG1` 永远是难腿（建仓、平仓都是），因为要先消化不确定性。唯一例外是 `DELEVER` 在 RED/BLACK 档时先减危险账户腿（救火优先于敞口）。

---

## 8. 参数速查表（建议初值）

```python
# ---- 节拍 ----
TICK_MS                 = 250      # 评估拍；下单为事件驱动，禁止定时轮转下单
ORDER_INFLIGHT_LOCK     = True     # 上一拍订单未终结禁止新单（原工具缺失）

# ---- 敞口 ----
DUST_USD                = max(5, 3*step_notional, 1.5*min_notional)
NAKED_SOFT_PCT_EQ       = 0.004    # 且 <= 3% gross
NAKED_HARD_PCT_EQ       = 0.012    # 且 <= 10% gross
NAKED_CRIT_PCT_EQ       = 0.030    # 且 <= 25% gross
EXPOSURE_BUDGET_USD_S   = 0.003 * equity * 30   # 时间加权敞口预算

# ---- 补腿 ----
T_REPAIR_SOFT_S         = 20 ;  N_REPAIR_SOFT = 12
T_REPAIR_HARD_S         = 6  ;  N_REPAIR_HARD = 5
REPAIR_BACKOFF_MS       = [200,300,500,800,1200,1500]
REPAIR_CROSS_BP         = 8
M_GATE_FAIL             = 8
K_ECON                  = 2.0      # 补腿成本 > 2x 撤退成本 则判死

# ---- 撤退 ----
UNWIND_P0_S             = 2  ; UNWIND_P0_CROSS_BP = 5
UNWIND_P1_S             = 6  ; UNWIND_P1_CROSS_BP = [10,20,40]
UNWIND_SLICE_DEPTH_FRAC = 0.6      # 单片 <= top3 可吃量 * 0.6
UNWIND_P2_FALLBACK_BP   = 200      # MARKET 被拒时的深穿价
T_COOLDOWN_S            = 600
SLICE_SHRINK_ON_UNWIND  = 0.5

# ---- 建仓 ----
ATOMICITY_MODE          = 'SEQUENTIAL_HARD_FIRST'
PARALLEL_MAX_USD        = 5000
SLICE_MAX_USD           = min(0.8*NAKED_HARD_USD, 0.15*top5_depth_usd)
SLICE_INTERVAL_MS       = 500 (±30% jitter)
ORDER_TIF               = 'IOC'    # 不用 FOK

# ---- 连接 ----
T_QUOTE_STALE_S         = 1.5 / 5 / 20        # 禁开仓 / 降级 / 市场死亡
T_WS_PRIV_DEGRADED_S    = 3
T_WS_PRIV_REDUCEONLY_S  = 15
T_ORDER_ACK_S           = 3
T_UNKNOWN_MAX_S         = 20
T_EXCHANGE_DEAD_S       = 10
RECV_WINDOW_MS          = 3000

# ---- 对账 ----
RECON_L2_S              = 15
RECON_L3_S              = 120

# ---- 风控档 (按 σ_8h 缩放, 见 §3.2) ----
MR_YELLOW/ORANGE/RED/BLACK = 4.0 / 2.5 / 1.6 / 1.25
HYSTERESIS_IMPROVE      = 0.20 ; HYSTERESIS_HOLD_S = 60

# ---- 熔断 ----
EQ_DROP_15M             = 0.015    # 总闸
EQ_DROP_1H              = 0.030
SLIPPAGE_ALERT_MULT     = 3.0 ; SLIPPAGE_ALERT_BP = 15
API_ERR_RATE_5M         = 0.25
UNWIND_COUNT_30M        = 3
SYMBOL_BLACKLIST_24H    = 2

# ---- 限频 ----
QUOTA_SPLIT             = {'market':0.4, 'trade':0.3, 'risk':0.3}
RISK_BACKOFF_MAX_MS     = 500

# ---- 杠杆 ----
L_EFF_TARGET            = 3.0      # 两腿名义合计/总权益
LEV_SAFETY_K            = 3.0
LEV_HARD_CAP            = 20
TIER_EDGE_AVOID         = 0.05
MARGIN_BUFFER_PER_EXCH  = 0.40
DEADMAN_COUNTDOWN_MS    = 30000 ; DEADMAN_REFRESH_MS = 10000
WATCHDOG_HEARTBEAT_S    = 10
```

---

## 9. Python 伪代码骨架

```python
# ============================================================
# hedge_fsm.py  —  跨所资金费套利执行安全状态机（骨架）
# 设计公理: 1) 仓位是真值,订单是过程  2) 不确定性前置消化
#           3) 熔断停赚钱动作,不停保命动作
# ============================================================
import asyncio, time, math, uuid
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, Literal

class S(Enum):
    IDLE=auto(); PREFLIGHT=auto(); LEG1_PENDING=auto(); EXPOSED=auto()
    LEG2_PENDING=auto(); REPAIR=auto(); UNWIND=auto(); CROSS_HEDGE=auto()
    BALANCED=auto(); BALANCED_SYNTHETIC=auto(); DELEVER=auto(); CLOSING=auto()
    RECONCILE=auto(); ORDER_UNKNOWN=auto(); FROZEN=auto(); HALTED=auto(); FLAT=auto()

class Exposure(Enum): DUST=0; SOFT=1; HARD=2; CRITICAL=3
class RiskTier(Enum): GREEN=0; YELLOW=1; ORANGE=2; RED=3; BLACK=4

@dataclass
class Unit:                       # 一个建仓分片 = 一个状态机实例
    uid: str
    symbol: str
    state: S = S.IDLE
    t_state: float = field(default_factory=time.time)
    t_exposed: Optional[float] = None
    hard_leg: str = 'A'           # 每片开始时重新评估
    leg1_filled: float = 0.0
    leg2_filled: float = 0.0
    target_qty: float = 0.0
    repair_n: int = 0
    unwind_phase: int = -1
    inflight: dict = field(default_factory=dict)   # cid -> leg
    exposure_integral: float = 0.0

# ------------------------------------------------------------
# 敞口计算
# ------------------------------------------------------------
def dust_usd(f_a, f_b, mark) -> float:
    step_notional = max(f_a.step, f_b.step, f_a.ctv, f_b.ctv) * mark
    return max(5.0, 3*step_notional, 1.5*max(f_a.min_notional, f_b.min_notional))

def classify_exposure(pos_a, pos_b, mark, equity, filters) -> tuple[Exposure, float]:
    net   = pos_a + pos_b                      # 已符号对齐
    naked = abs(net) * mark
    gross = (abs(pos_a) + abs(pos_b)) / 2 * mark
    d     = dust_usd(filters.a, filters.b, mark)
    if naked < d:                                            return Exposure.DUST, naked
    # 按波动率归一化(BTC 为基准)
    scale = max(0.3, min(3.0, VOL_1M[SYM] / VOL_1M['BTCUSDT']))
    soft = min(P.NAKED_SOFT_PCT_EQ*equity, 0.03*gross) * scale
    hard = min(P.NAKED_HARD_PCT_EQ*equity, 0.10*gross) * scale
    crit = min(P.NAKED_CRIT_PCT_EQ*equity, 0.25*gross) * scale
    if naked < soft: return Exposure.SOFT, naked
    if naked < hard: return Exposure.HARD, naked
    if naked < crit: return Exposure.HARD, naked
    return Exposure.CRITICAL, naked

# ------------------------------------------------------------
# 幂等下单：唯一入口，任何地方都不许直接调 REST 下单
# ------------------------------------------------------------
async def place_idempotent(ex, unit: Unit, leg, side, qty, px, *,
                           reduce_only=False, tif='IOC', quota='trade'):
    cid = f"{unit.uid}-{leg}-{unit.repair_n}-{uuid.uuid4().hex[:6]}"
    unit.inflight[cid] = leg
    try:
        r = await ex.new_order(symbol=unit.symbol, side=side, qty=qty, price=px,
                               tif=tif, reduce_only=reduce_only,
                               client_id=cid, quota=quota,
                               timeout=P.T_ORDER_ACK_S)
        unit.inflight.pop(cid, None)
        return OrderResult(cid=cid, status=r.status, filled=r.executed_qty,
                           avg=r.avg_price, err=None)
    except (TimeoutError, TransportError):
        # 关键: 超时 != 失败。绝不重发,交给 ORDER_UNKNOWN 解决
        return OrderResult(cid=cid, status='UNKNOWN', filled=0.0, avg=0.0,
                           err='TIMEOUT')
    except ExchangeError as e:
        unit.inflight.pop(cid, None)
        return OrderResult(cid=cid, status='REJECTED', filled=0.0, avg=0.0,
                           err=classify_error(e))   # 见 §4.4 错误分类器

# ------------------------------------------------------------
# 未知订单消解: cancel(终结) → query → position 差分。禁止靠猜。
# ------------------------------------------------------------
async def resolve_unknown(ex, unit: Unit, cid) -> Optional[float]:
    t0 = time.time()
    backoff = 0.2
    pos_before = STATE.pos_snapshot(ex.name, unit.symbol)
    while time.time() - t0 < P.T_UNKNOWN_MAX_S:
        try:
            # a) cancel 是状态终结操作,执行后订单只可能 CANCELED / FILLED
            await ex.cancel_by_client_id(unit.symbol, cid, quota='risk')
        except OrderNotFound:
            pass                    # 已终结,继续查
        except TransportError:
            await asyncio.sleep(backoff); backoff = min(backoff*2, 1.0); continue
        try:
            o = await ex.query_order(unit.symbol, client_id=cid, quota='risk')
            unit.inflight.pop(cid, None)
            return o.executed_qty
        except OrderNotFound:
            # 交易所连订单都不认识 → 用仓位差分推断(公理1)
            pos_now = await ex.position(unit.symbol, quota='risk')
            unit.inflight.pop(cid, None)
            return abs(pos_now - pos_before)
        except TransportError:
            await asyncio.sleep(backoff); backoff = min(backoff*2, 1.0)
    return None                     # 交易所不可读 → FROZEN

# ------------------------------------------------------------
# PREFLIGHT 闸门（原工具缺 risk_tier / L_eff 这两根线）
# ------------------------------------------------------------
def preflight(unit: Unit) -> tuple[bool, str]:
    checks = [
        (BREAKER.level == 0,                          'breaker'),
        (RISK.tier == RiskTier.GREEN,                 'risk_tier'),      # ← §3.4
        (RISK.l_eff < P.L_EFF_TARGET,                 'l_eff'),
        (MD.age(A) < P.T_QUOTE_STALE_S and
         MD.age(B) < P.T_QUOTE_STALE_S,               'quote_stale'),
        (MD.depth_ok(A, unit.target_qty, 1.5) and
         MD.depth_ok(B, unit.target_qty, 1.5),        'depth'),
        (MD.spread_bp(A) < P.SPREAD_MAX_BP and
         MD.spread_bp(B) < P.SPREAD_MAX_BP,           'spread'),
        (ACCT.avail(A) > need_margin(A)*1.4 and
         ACCT.avail(B) > need_margin(B)*1.4,          'margin'),
        (LEV.is_set(A) and LEV.is_set(B),             'leverage'),       # 预热,非热路径
        (not SYM.near_tier_edge(unit.symbol, 0.05),   'tier_edge'),
        (SYM.status_ok(A) and SYM.status_ok(B),       'symbol_status'),
        (not FUNDING.within_settle_window(30),        'funding_window'),
        (not MAINT.within(30*60),                     'maintenance'),
        (unit.symbol not in BLACKLIST,                'blacklist'),
        (COOLDOWN.ready(unit.symbol),                 'cooldown'),
    ]
    for ok, name in checks:
        if not ok: return False, name
    return True, ''

# ------------------------------------------------------------
# 补腿无望判据
# ------------------------------------------------------------
def repair_hopeless(unit: Unit, empty_ex, level: Exposure) -> Optional[str]:
    # 硬判据: 命中即刻判死,一次重试都不做
    if not SYM.status_ok(empty_ex):              return 'symbol_not_trading'
    if SYM.reduce_only_mode(empty_ex):           return 'exchange_reduce_only'
    if ACCT.avail(empty_ex) < need_margin(empty_ex) and \
       not ACCT.can_transfer_in(empty_ex):       return 'insufficient_margin'
    if ACCT.open_restricted(empty_ex):           return 'account_restricted'
    if MAINT.active(empty_ex):                   return 'maintenance'
    if CONN.dead_for(empty_ex) > P.T_EXCHANGE_DEAD_S: return 'exchange_dead'
    # 软判据
    T = P.T_REPAIR_SOFT_S if level == Exposure.SOFT else P.T_REPAIR_HARD_S
    N = P.N_REPAIR_SOFT   if level == Exposure.SOFT else P.N_REPAIR_HARD
    if time.time() - unit.t_exposed > T:         return 'timeout'
    if unit.repair_n >= N:                       return 'budget'
    if GATE.consecutive_fail(empty_ex) >= P.M_GATE_FAIL: return 'book_unusable'
    if est_repair_cost(unit) > P.K_ECON * est_unwind_cost(unit): return 'econ'
    if unit.exposure_integral > P.EXPOSURE_BUDGET_USD_S:  return 'time_weighted'
    return None

# ------------------------------------------------------------
# 撤退阶梯
# ------------------------------------------------------------
async def unwind(unit: Unit, force_market=False):
    # ---- Phase -1: 终结在途订单(强制,不可跳过) ----
    for cid in list(unit.inflight):
        ex = EX[unit.inflight[cid]]
        filled = await resolve_unknown(ex, unit, cid)
        if filled is None:
            return goto(unit, S.FROZEN)
        apply_fill(unit, unit.inflight.get(cid), filled)
    await reconcile(unit, full=True)      # 公理1: 以交易所仓位为真

    t0 = time.time()
    while True:
        net, mark = current_net(unit)
        if abs(net)*mark < DUST[unit.symbol]:
            return goto(unit, S.FLAT)

        # 撤退中对腿迟到成交导致符号翻转 → 原地掉头
        side = 'SELL' if net > 0 else 'BUY'
        ex   = EX[leg_of(net)]
        el   = time.time() - t0
        depth_cap = MD.eatable(ex, side, 3) * P.UNWIND_SLICE_DEPTH_FRAC
        qty  = min(abs(net), depth_cap)

        if force_market or el > P.UNWIND_P1_S:              # Phase 2
            r = await place_idempotent(ex, unit, ex.leg, side, abs(net), None,
                                       reduce_only=True, tif='MARKET', quota='risk')
            if r.err in ('MARKET_DISABLED','MARKET_LOT_SIZE'):
                px = cross_price(ex, side, P.UNWIND_P2_FALLBACK_BP)
                for _ in range(3):
                    await place_idempotent(ex, unit, ex.leg, side, qty, px,
                                           reduce_only=True, quota='risk')
            if r.err and unit.unwind_phase >= 2:            # Phase 2 连败 → Phase 3
                if await cross_hedge(unit):
                    return goto(unit, S.BALANCED_SYNTHETIC)
                return halt(unit, 'unwind_failed')
            unit.unwind_phase = 2
        elif el > P.UNWIND_P0_S:                           # Phase 1: 递进穿价
            bp = P.UNWIND_P1_CROSS_BP[min(int((el-P.UNWIND_P0_S)//1.3), 2)]
            await place_idempotent(ex, unit, ex.leg, side, qty,
                                   cross_price(ex, side, bp),
                                   reduce_only=True, quota='risk')
            unit.unwind_phase = 1
        else:                                              # Phase 0
            await place_idempotent(ex, unit, ex.leg, side, qty,
                                   cross_price(ex, side, P.UNWIND_P0_CROSS_BP),
                                   reduce_only=True, quota='risk')
            unit.unwind_phase = 0

        await asyncio.sleep(0.3)
        if time.time() - t0 > 60:
            return halt(unit, 'unwind_timeout')

# ------------------------------------------------------------
# 主状态机（事件驱动 + 1s 兜底 tick；禁止定时轮转下单）
# ------------------------------------------------------------
async def step(unit: Unit):
    if unit.t_exposed:
        _, naked = current_exposure(unit)
        unit.exposure_integral += naked * P.TICK_MS/1000

    if unit.state is S.IDLE:
        if SIGNAL.wants(unit.symbol): goto(unit, S.PREFLIGHT)

    elif unit.state is S.PREFLIGHT:
        ok, why = preflight(unit)
        if not ok:
            GATE.note_fail(why); return goto(unit, S.IDLE)
        unit.hard_leg = pick_hard_leg(unit)          # §2.3 硬度评分
        unit.target_qty = slice_qty(unit)            # §2.5 分片
        goto(unit, S.LEG1_PENDING)
        r = await place_idempotent(EX[unit.hard_leg], unit, 'L1',
                                   side_for(unit,'L1'), unit.target_qty,
                                   cross_price(EX[unit.hard_leg], side_for(unit,'L1'), 3))
        if r.status == 'UNKNOWN': return goto(unit, S.ORDER_UNKNOWN, cid=r.cid)
        if r.filled * MARK <= DUST[unit.symbol]:
            return goto(unit, S.IDLE)                # 零敞口,安全退出
        unit.leg1_filled = r.filled
        unit.t_exposed = time.time()
        goto(unit, S.EXPOSED)

    elif unit.state is S.EXPOSED:
        goto(unit, S.LEG2_PENDING)
        # 关键: 第二腿数量 = 第一腿实际成交量,数量错配在结构上消失
        step_c = common_step(unit.symbol)
        q2 = floor_to(unit.leg1_filled, step_c)
        resid = unit.leg1_filled - q2
        if resid * MARK > DUST[unit.symbol]:
            await trim_leg1(unit, resid)             # 削平补不上的残差
        r = await place_idempotent(EX[easy_leg(unit)], unit, 'L2',
                                   side_for(unit,'L2'), q2,
                                   cross_price(EX[easy_leg(unit)], side_for(unit,'L2'), 5))
        if r.status == 'UNKNOWN': return goto(unit, S.ORDER_UNKNOWN, cid=r.cid)
        unit.leg2_filled += r.filled
        lvl, _ = current_exposure(unit)
        goto(unit, S.BALANCED if lvl is Exposure.DUST else S.REPAIR)

    elif unit.state is S.REPAIR:
        lvl, _ = current_exposure(unit)
        if lvl is Exposure.DUST:     return goto(unit, S.BALANCED)
        if lvl is Exposure.CRITICAL: return await unwind(unit, force_market=True)
        why = repair_hopeless(unit, EX[easy_leg(unit)], lvl)
        if why:
            LOG.warn('repair_hopeless', why=why); return await unwind(unit)
        await asyncio.sleep(P.REPAIR_BACKOFF_MS[min(unit.repair_n, 5)]/1000)
        unit.repair_n += 1
        r = await place_idempotent(EX[easy_leg(unit)], unit, 'L2',
                                   side_for(unit,'L2'), remaining_qty(unit),
                                   cross_price(EX[easy_leg(unit)], side_for(unit,'L2'),
                                               P.REPAIR_CROSS_BP))
        if r.status == 'UNKNOWN': return goto(unit, S.ORDER_UNKNOWN, cid=r.cid)
        unit.leg2_filled += r.filled

    elif unit.state is S.ORDER_UNKNOWN:
        filled = await resolve_unknown(EX[unit.ctx_leg], unit, unit.ctx_cid)
        if filled is None: return goto(unit, S.FROZEN)
        apply_fill(unit, unit.ctx_leg, filled)
        lvl, _ = current_exposure(unit)
        goto(unit, S.BALANCED if lvl is Exposure.DUST else S.REPAIR)

    elif unit.state is S.BALANCED:
        unit.t_exposed = None; unit.exposure_integral = 0
        if RISK.tier.value >= RiskTier.ORANGE.value: return goto(unit, S.DELEVER)
        if RECON.dirty(unit):                        return goto(unit, S.RECONCILE)
        if SIGNAL.wants_exit(unit.symbol):           return goto(unit, S.CLOSING)

    elif unit.state is S.DELEVER:
        # 注意: RED/BLACK 时先减危险账户那一腿(救火 > 敞口),这是难腿优先的唯一例外
        await delever_proportional(unit, target_mr=3.0,
                                   danger_first=RISK.tier.value >= RiskTier.RED.value)
        goto(unit, S.BALANCED)

    elif unit.state is S.FROZEN:
        # 只读能力丧失 → 停止一切下单,只保留看门狗与告警
        if await CONN.recovered(): goto(unit, S.RECONCILE)

    elif unit.state is S.HALTED:
        # 公理3: 保命白名单仍执行
        if current_exposure(unit)[0].value >= Exposure.HARD.value:
            await unwind(unit, force_market=True)
        if RISK.tier.value >= RiskTier.ORANGE.value:
            await delever_proportional(unit, target_mr=3.0, danger_first=True)
        await ALERT.repeat_until_ack(unit)

# ------------------------------------------------------------
# 熔断评估（独立于单元，全局；权益骤降是总闸）
# ------------------------------------------------------------
def evaluate_breaker():
    if EQ.drop(15*60) > P.EQ_DROP_15M or EQ.drop(3600) > P.EQ_DROP_1H:
        return BREAKER.hard('equity_drop')          # 对冲失效的最强信号
    if RECON.unexplained_position():   return BREAKER.hard('unexplained_pos')
    if EVENTS.adl_or_liquidation():    return BREAKER.hard('adl')
    if API.err_rate(5*60) > P.API_ERR_RATE_5M: return BREAKER.hard('api_errors')
    if RISK.tier is RiskTier.BLACK:    return BREAKER.hard('liq_black')
    if BASIS.dev() > BASIS.p995_90d(): return BREAKER.hard('basis_anomaly')
    if STATS.unwinds(30*60) >= P.UNWIND_COUNT_30M: return BREAKER.soft('unwind_freq', 1800)
    if STATS.consecutive_entry_fail() >= 3:        return BREAKER.soft('entry_fail', 300)
    if SLIP.last() > P.SLIPPAGE_ALERT_MULT*SLIP.expected() and \
       SLIP.last_bp() > P.SLIPPAGE_ALERT_BP:       return BREAKER.soft('slippage', 600)
    if CONN.degraded():                            return BREAKER.soft('conn', 0)
    if RISK.tier.value >= RiskTier.YELLOW.value:   return BREAKER.soft('risk_tier', 0)
    BREAKER.clear_if_cooled()

# ------------------------------------------------------------
# 后台任务
# ------------------------------------------------------------
async def bg_tasks():
    await asyncio.gather(
        loop_every(P.RECON_L2_S,  lambda: reconcile_all(full=True)),
        loop_every(P.RECON_L3_S,  reconcile_trades),          # 唯一能发现 ADL/手动干预
        loop_every(P.DEADMAN_REFRESH_MS/1000, refresh_deadman_switch),
        loop_every(1.0,           evaluate_breaker),
        loop_every(1.0,           update_risk_tier_with_hysteresis),
        loop_every(300,           refresh_symbol_filters_and_maxlev),  # 防交易所降档
        loop_every(600,           poll_maintenance_and_delisting),
        watchdog_heartbeat_monitor(),                          # 独立 API key
    )
```

---

## 10. 与原工具的差异清单（实现时逐项核对）

| # | 原工具 | 本设计 |
|---|---|---|
| 1 | 两腿并发按目标量下单 | 串行、难腿先、第二腿量 = 第一腿实际成交量 |
| 2 | FOK | IOC + 分片 |
| 3 | 无限重试补腿，无放弃条件 | 6 条硬判据 + 5 条软判据 + 撤退阶梯 + `CROSS_HEDGE` 兜底 |
| 4 | 500ms 定时轮转下单 | 事件驱动 + in-flight 锁 + 幂等 `clientOrderId`（原设计可重复下单） |
| 5 | 超时视作失败 | `ORDER_UNKNOWN`：cancel 终结 → 查单 → 仓位差分 |
| 6 | 无对账 | L1/L2/L3 三层，永远以交易所为真 |
| 7 | 爆仓概率只显示 | `risk_tier` 接入 `PREFLIGHT` 闸门 + 5 档自动动作 |
| 8 | 无止损 | 敞口四级 + 权益骤降总闸 + 撤退阶梯 |
| 9 | 无熔断 | 软/硬两级 + 保命白名单 + 交易所侧 deadman + 本地看门狗 |
| 10 | 取最高杠杆 | 中档杠杆 + `L_eff ≤ 3` + 避 tier 边界 + 防交易所降档 + 同所内预留救援金 |
| 11 | 平仓单未强制 `reduceOnly` | 全部风控路径强制 `reduceOnly`（否则可能开出反向仓） |
| 12 | 无限频分池 | 风控路径预留 30% 配额，退避封顶 500ms |

**实现优先级**（按"每小时能挡住多少灾难"排序）：
1. 幂等 `clientOrderId` + in-flight 锁 + `ORDER_UNKNOWN` 消解（挡双倍仓位/幽灵单）
2. 串行难腿优先 + 第二腿按实际成交量（挡结构性数量错配）
3. `UNWIND` 阶梯 + Phase −1 强制 cancel（挡永久裸奔）
4. 权益骤降总闸 + 交易所侧 deadman switch（兜底）
5. L2/L3 对账（挡 ADL / 静默漂移）
6. `risk_tier` 接线 + 杠杆重构