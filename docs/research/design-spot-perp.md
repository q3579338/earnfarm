# 同所现货+永续配对(spot-perp)设计 —— v1 以 Backpack 为首发

> 核心判断:同所 spot-perp 不是"多接一家交易所",而是给既有管道引入**第二种腿类型**。
> 现货腿的本质是"没有资金费、不能做空、仓位是余额不是仓位"三件事,每一件都恰好踩在
> 既有代码的一条隐含假设上(RawFunding 隐含全是永续、orient 隐含两腿都可做空、
> _reconcile 隐含 position_qty 是本对冲的真值)。设计的重心不是加功能,而是**把这三条
> 隐含假设显式化**,让现货腿在每一层都有明确的表达,而不是靠零值伪装成一个"费率恒 0 的永续"。

调研依据(两份,本文档为其整合):
- `docs/research/exchange-backpack.md` —— Backpack 现货+借贷侧 API 调研(2026-08,OpenAPI
  规范逐字段核对 + 公开端点线上实测;`backpack.py` 头注引用的同一份)。
- 仓内改造侦察报告(2026-08,只读)—— 本文档引用的全部 file:line 锚点已在写作时逐一复核。
  **勘误一处**:侦察报告称 `exchanges/backpack.py` 不存在,现已过时——perp 适配器已落地
  (1353 行),构造器在 `backpack.py:344-350` 显式拒绝 SPOT。因此适配器工作是**扩展**不是新建。

---

## 0. 功能边界:v1 只做「正费率 + 现货多头对冲」

### 0.1 v1 范围

| 维度 | v1 做 | v1 不做 |
|---|---|---|
| 方向 | 买现货 + 空永续,吃**正**资金费 | 多永续 + 借币卖现货,吃负资金费(→ v2) |
| 交易所 | Backpack(调研完整、统一账户) | OKX 现货(适配器已备,费率/账户口径未按本设计复核,列 v1.1) |
| 借贷 | 完全不碰:不带 autoBorrow,不手动 borrowLendExecute | 一切借贷路径 |
| 阶段 | v1a 榜单只读(feed+评分+UI)→ v1b 执行(executor/safety 四件套) | — |

**为什么砍掉负费率方向**:Backpack 原生支持(现货卖单带 `autoBorrow=true` 一单完成"借+卖"),
诱惑很大——负费率币恰恰是费差绝对值最大的那批。但成本模型多出三个活动部件:借币年化
(浮动,随 utilization 变化)、entry fee(±1% 带,`borrowEntryFeeMin/MaxMultiplier`)、还款
netting 语义,其中借贷利率的 APR/APY 口径本身还是 UNVERIFIED(见 §10)。把一个成本口径
没钉死的方向放进"把口径钉在明面上"的工具,是自我打脸。

**不这么砍会怎么坏**:`scoring.orient`(scoring.py:442-448)只比小时化费率,负费率永续会把
现货定向到**空腿**——现货 `shortable=False`(models.py:346),score_pair 照常打出漂亮分数,
用户点"建对冲"才在 trader.py:128 的 validate_pairing 撞 PairingError。负费率币因费差大
高居榜首,等于把最显眼的位置留给不可执行的行。

### 0.2 v2(借币做空)前置调研清单

1. 借款利率口径实测:lend 侧规范写明 APY,borrow 侧只写 "rate borrowers pay",配合
   "每小时整点计提复利、借款额×年利率÷365÷24"的公式更像 APR 名义值。用
   `/wapi/v1/history/interest` 跑一天实账反推,误差在几个 bp 但方向必须钉死。
2. entry fee 精确模型:官方说"相当于一部分小时利息",触发条件与 filters 的 ±1% 带的关系需实测。
3. 借款利率波动风险:`/api/v1/borrowLend/markets/history` 拉历史,utilization 冲高时
   borrow rate 能否吃掉资金费收入,需要和资金费同格式的稳定性评分。
4. `autoBorrow` 卖单的原子性与失败模式(借不到额度时整单拒还是部分成交)。
5. 平仓 netting:反向操作即还款("borrowing first redeems any existing lend"),部分平仓时
   利息计提与仓位的对应关系。
6. 借款仓位自身的 imf/mmf 对账户 IMR 的贡献、`estLiquidationPrice` 语义。
7. executor 错误码:借币额度不足(`maxBorrowQuantity` 触顶)的报错标记。

---

## 1. 现货腿的表达:缺席,不是零值(总纲决策)

两个候选方案:

**方案 A(侦察报告原案)**:现货合成 RawFunding 行(rate=0、interval_h=0)流入
fetch_funding 管道,给 RawFunding 加 `kind` 字段,bucket 循环里加槽位约束。

**方案 B(本设计采纳)**:现货**不进** RawFunding。bucket 循环选出空腿后,查"现货工具索引"
按需合成同所现货多腿;RawFunding 保持"每行都是永续"的既有语义不变。

选 B 的三个理由:

1. **既有约定就是缺席**。现行现货适配器不返回 CarryRate(okx.py:580-581 返回空元组),
   "零 carry 表现为缺席而非零值"是仓里已经付过学费的约定。方案 A 反其道而行,还要顺手
   拆掉 public_feed.py:284 的 `interval_h = ... if r.interval_s else 8.0` 兜底——这个兜底
   会把 interval=0 的现货行"好心"伪装成 8h 永续,settlement_offsets 随即给多腿生成一串
   虚构结算点,污染 n_long 计数、"持有期内零结算"判决(scoring.py:542-545)和 UI 周期过滤。
2. **不扰动既有 perp-perp 结果**。bucket 循环按小时化费率排序取两端(public_feed.py:492-493),
   方案 A 里现货 0 费率行会在"全 bucket 皆正费率"时**静默顶掉**原多腿——存量跨所行无声消失,
   这正是 normalize_base 注释里警告过的"整家静默消失"模式,只是这次消失的是行不是家。
   方案 B 让 spot-perp 行**并存**于 perp-perp 行,排序交给评分层,谁优谁上。
3. **约束是结构性的**。方案 B 里现货腿只可能出现在合成分支的多腿槽位,"现货只能做多、
   只配正费率永续"不需要靠散落的 if 维护——不存在现货进空腿的代码路径。方案 A 的槽位
   约束是防御性检查,漏一处就回到 §0.1 的老问题。

代价:public_feed 需要一个平行的现货工具/盘口通道(§2.1),数据模型出现轻微不对称。
**若未来有人要走回方案 A**(比如为了让现货腿吃 StaleMonitor 的统一打点),必须同时做齐
三道闸:RawFunding 加 kind、284 行兜底加 `kind is PERP` 条件、bucket 槽位约束,缺一即坏。

---

## 2. 数据层(public_feed.py)

### 2.1 现货工具与盘口通道

- 适配器持有:`_adapters: dict[Venue, ExchangeAdapter]`(public_feed.py:232)升级为按
  **market key** 索引(`"backpack:perp"` / `"backpack:spot"`),fetch_instruments /
  fetch_book / fetch_depth 的寻址随之从 Venue 换成 market key。
  open_public_adapters(public_feed.py:123-137)对 `SPOT_ENABLED` 白名单里的家多建一个
  `market_kind=SPOT` 实例(空凭据,公开端点不签名——backpack.py 已把私钥构造延迟到首次
  签名,正是为了这条路)。
- **不这么做会怎么坏**:同一家的现货盘口在现有结构里没有地址可用——执行器和评分器
  拿不到现货 bid/ask/深度,后面全部免谈。
- Backpack 特有便利:ticker/depth/klines 现货与永续**共用同一组端点**,现货实例的盘口
  实现是零新协议的。

### 2.2 配对分支(不动 494 行)

`public_feed.py:494` 的 `if long_raw.venue is short_raw.venue: continue` **保持原样**——
它对 perp-perp 仍然正确(同所同 base 两张永续没有套利空间)。spot-perp 行由新增分支合成:

```python
# bucket 内选出 short_raw(小时化费率最高的永续)之后,追加:
if (short_hourly > 0                       # 现货收不到钱,只有"永续多头付费"方向成立
        and short_raw.venue in SPOT_ENABLED           # v1: {Venue.BACKPACK}
        and spot_index.has(short_raw.venue, base)):   # 该所有同 base 现货
    rows.append(make_pair(
        long=spot_leg(short_raw.venue, base),   # market = f"{venue}:spot"
        short=short_raw))                        # 与 perp-perp 行并存,评分层定序
```

现货 LegQuote 的构造口径:`funding_now=0`、`funding_interval_h=0.0`(settlement_offsets
对 interval<=0 返回空列表,scoring.py:127-128,退化路径天然正确)、盘口取现货簿、
taker_fee 取现货档(§2.3)。腿的 market key 生成处(public_feed.py:541 写死 `:perp`)
按腿的来源分支。

MIN_TOP_NOTIONAL(public_feed.py:71)对现货一档同样生效:现货簿一档吃不下几百刀的对,
连合成都不合成。

### 2.3 DEFAULT_TAKER 按 (venue, kind) 拆列

DEFAULT_TAKER(public_feed.py:51-66)现为按 Venue 单值,语义是**永续**挂牌价。现货腿
必须用现货挂牌档:Backpack 现货 VIP0 taker 是 **10bp**(0.0010),永续是 5bp。

**不拆会怎么坏**:现货腿成本被**低估一半**——而这张表自己的注释写着"宁可高估成本也别
低估——低估会让负期望的机会看起来能做"。四笔执行里两笔在现货侧,10bp vs 5bp 的差在
往返成本里是 10bp,以 24h 持有折年化约 36 个百分点的系统性乐观偏差。

实现:加 `DEFAULT_TAKER_SPOT: dict[Venue, float]`(首发只有 `Venue.BACKPACK: 0.0010`),
真实档位仍走 refresh_fees 覆盖(§4.3)。真实档位缺失时的口径标注(`_basis_of`)不变。

### 2.4 历史与稳定性:现货腿 = 常数 0 序列

`_history_for`(public_feed.py:345-382)与 hourly_carry 的对齐逻辑要求两腿历史有重叠
区间;现货没有资金费历史,照现状会恒得 None → stability 恒 is_prior。

正确语义:CarryKind.ZERO 的腿是**常数 0 序列**,配对的小时化 carry 序列就**等于永续腿
自己的历史**(short − long = perp − 0)。实现为:构建 hourly_carry 时若多腿 market 的
kind 是 SPOT,直接取空腿自己的小时序列,不走对齐。

**不这么做会怎么坏**:所有 spot-perp 行永远"历史数据不足"灰标、S_hist 吃 0.35 先验、
被压成 marginal——稳定性列对这批行形同虚设,而 spot-perp 恰恰是稳定性最有意义的形态
(单边费率序列,没有两腿相关性的噪声)。

---

## 3. 评分层(scoring.py)

### 3.1 已核对的免费退化路径(不改)

- funding_income(scoring.py:158-174):多腿 interval=0 → offsets 为空 → Σf_L=0,
  净收入 = Σf_S。前提是 interval **真的是 0**(§1 的兜底陷阱已由方案 B 绕开)。
- breakeven_hours(scoring.py:255-278):现货腿贡献零事件,事件驱动扫描自动正确。
- roundtrip_cost / slip_rate(scoring.py:181-234):模型只依赖 bid/ask/半价差/带宽深度/
  taker 费,对现货盘口同样适用。唯一前提:LegQuote.taker_fee 装的是现货费率(§2.3)。
- 容量:capacity 的二分搜索逐腿算滑点、参与率取 `min(L.depth, S.depth)`——现货簿接上后,
  **容量自动取现货簿和永续簿的薄边**,零新代码。Backpack 现货深度通常薄于永续,预期
  薄边多数在现货侧,这正是要如实呈现的。

### 3.2 orient 残余风险:shortable 闸

方案 B 已保证配对生成时现货在多腿、永续费率为正。但 rescore 路径(public_feed.py:403
注释:"score_pair 内部再 orient 一次会得到同样的顺序")依赖 orient 幂等——**费率在持有
排队期间翻负**时,orient(scoring.py:442-448)会把 0 费率的现货重新定向到空腿。

处置:**不改 orient**(它是纯函数,塞进市场感知会引入 MARKETS 依赖,且"交换后仍非法"
没有自然解),在 score_pair 的 orient 之后加一道闸:

```python
long, short = orient(leg_a, leg_b)
if not MARKETS[short.market].shortable:
    # 费率翻负把现货定向到了空腿:这不是机会变差,是方向非法。
    # 归入不可执行组,绝不交换回来硬算——硬算出的是一个做不了的方向的分数。
    return infeasible(reason="现货不能做空;该配对仅在永续费率为正时成立")
```

与配对生成处的"只配正费率"前置约束互为冗余的两道闸。**只留一道会怎么坏**:只留前置
约束,rescore 翻负后照常出分,用户看到的方向和实际可执行方向相反;只留 score_pair 闸,
配对循环合成一堆注定 infeasible 的行白白浪费盘口请求。

validate_pairing(models.py:362-378)**不用动**——现货多腿本来就放行,同所 spot+perp
不在禁令内。可选加一条更友好的报错:多腿为 SPOT 且用户试图反向时,明说"现货做空需要
借币,v2 才支持"。

### 3.3 kappa:同所统一账户下的资金占用口径

score_pair 的 `kappa=2.0`(scoring.py:454、477)隐含"两腿各占 1x 保证金"。同所 Backpack
统一账户下这是**双重错误口径**:现货多腿占全额名义(m=1.0,无杠杆),但官方明确单一跨
保证金钱包,**现货多头本身就是永续空头的保证金**(抵押权重 BTC/ETH/SOL 基础 0.95,
inverseSqrt 随规模递减;"100% of lent assets as collateral")。永续腿的增量占用
≈ max(0, IM − 0.95×N) ≈ 0。

v1 取 **kappa_spot_perp = 1.3**(可调参数,按配对类型选择):1.0(现货全额)+ 0.3
(缓冲:MMR 头寸距离 + 费率翻负间隙的 USDC 浮存——付资金费会触发 USDC 自动借款计息,
见 §7)。跨所与 perp-perp 一律维持 2.0 不变。

**取 2.0 会怎么坏**:统一账户的核心优势(资金效率对跨所翻倍)被记账抹掉,spot-perp 的
净年化被系统性腰斩,永远排不过 perp-perp,功能白做。**取 1.0 会怎么坏**:违背"套利要在
会计上悲观"——缓冲是真实占用,费率翻负的极端时段尤其如此。0.3 的缓冲值待实盘用
`GET /api/v1/capital/collateral` 的 netEquityAvailable 增量实测后校准,校准前不下调。

### 3.4 不入账的附加收益(记文档,不进公式)

Backpack 现货多腿带 autoLend 可边抵押边吃 lend 年化(实测 SOL 0.54%),未实现盈利也计入
权益生息。v1 **一律不入评分**:金额小(< 1% 年化)、口径未钉死(APY,且交易所抽
10-15% 利差)、且把低置信收益混进主数字违反 §5 基差项立下的"单列展示"先例。实盘核算时
从 `/wapi/v1/history/interest` 按 PaymentType 逐项对回来即可。

---

## 4. 适配器层(exchanges/backpack.py + base.py + session.py)

### 4.1 双实例模式(照 okx.py 抄)

基类语义是"一家交易所的一套市场一个适配器"(base.py:122-124);OKX 已验证范式:同一个类
两个实例,`market_kind` 构造参数,行为全在 `self.kind` 上分支(okx.py:143-186)。
AdapterRegistry 按任意 market key 索引(base.py:402-417),`"backpack:spot"` 双注册
机制上零成本。

backpack.py 的改动:拆掉构造器的 SPOT 拒绝(backpack.py:344-350),按 kind 分支:

| 关注点 | 现货分支行为 |
|---|---|
| 下单 | 同端点同 instruction(orderExecute),**不带 reduceOnly**(规范注明 Futures only,带了会拒单);不带 autoBorrow/autoBorrowRepay/autoLend(v1 不碰借贷);卖单带 `autoLendRedeem=true`(防账户级 autoLend 开着时余额锁在 lend 池卖不动;对未 lend 余额是否无害 UNVERIFIED,实测确认) |
| 仓位 | 无仓位概念,`position_qty` 语义换成**余额相对基线的增量**(§5.1);资产余额端点口径以 lend 中余额是否计入为准(UNVERIFIED,实测) |
| instruments | `GET /api/v1/markets?marketType[]=SPOT`;**按 marketType 分支解析,不按字段有无**——实测 SPOT 行会带无意义的 fundingInterval 残留(BTC_USDC 返回 28800000);`funding_interval_s` 显式填 0,别让 models.py:95 的 28800 默认值漏进去 |
| filters | 只有 price/quantity 两组,**没有 minNotional**;LegFilters.min_notional 合成为 `minQuantity × 抓取时价格`,供 safety 的 dust 门槛消费(撮合引擎是否另有隐性名义额拒单 UNVERIFIED) |
| 资金费 | fetch_carry_rates 返回空元组(照 okx.py:580-581 约定,缺席不是零值) |
| 杠杆 | set_leverage 直接 NotImplementedError(照 okx.py:973-974) |
| 费率 | `GET /api/v1/account` 的 spotMakerFee/spotTakerFee,**单位是基点、负数是返佣**——必须 ÷10000 换算,并加 sanity clamp(taker 换算后 >0.5% 或 <−0.1% 视为解析错误,拒用退默认档并告警)。现货与永续是四个独立字段,两腿分别取 |
| 端点前缀 | 历史类全在 `/wapi/v1/`(history/fills、history/orders、history/interest……),交易与公开在 `/api/v1/`——perp 实现已踩过,现货侧照规矩 |
| speed bump | 全站 100ms taker 延迟入簿(非 post-only 都算,现货+合约),两腿风险窗计算计入(§7) |

签名(ED25519、instruction 前缀、window)、时钟同步、错误消解链(404 → history 自配)
全部复用 perp 实现,现货分支零新协议。

### 4.2 session 双注册 + 共享限频(成对改,不可拆)

- 适配器类加 `supported_kinds` 类属性(基类默认 `(MarketKind.PERP,)`,backpack/okx 覆盖);
  connect_all 对每个支持的 kind 各建实例、按 `f"{venue}:{kind}"` 注册(session.py:288
  现写死 `:perp`——不改则执行器打现货腿时 `registry.get("backpack:spot")` 直接 KeyError,
  base.py:410-414)。
- **限频配额必须同 venue 共享**:每个实例各有独立的 Semaphore 配额池(base.py:146-150),
  而交易所真实限频按 UID/IP 共享。双实例各自为政时,"风控路径预留 30% 配额"的保证静默
  失效——撤退风暴时两个实例互相把对方限到死,恰好击穿这个预留想防的那个场景。实现:
  quota 字典构造时可注入,connect_all 对同 venue 的第二个实例传入第一个的 quota。
- refresh_fees 无需改:它遍历 registry.markets()(session.py:309),现货市场自动被覆盖。

---

## 5. 执行与安全层(v1b;executor.py / safety.py)

裸腿检测(safety.py:213-252)与撤退阶梯(safety.py:336-359)是纯数学,对"同所"没有假设,
同所配对反而让 CROSS_HEDGE 阶段(safety.py:60)第一次有了自然的对手市场。要处理的是
**现货腿语义**,按危险程度排序:

### 5.1 对账基线(最危险,v1b 的硬门)

`_reconcile` 无条件 `unit.hard_filled = abs(hard_pos)`(executor.py:445-467)。公理 1
"仓位是真值"对永续成立,对现货**不成立**——position_qty 对现货只能是币的余额,余额里混着
用户与本对冲无关的存货。一次对账就会把无关余额吸进对冲记账,制造虚假敞口,然后撤退逻辑
去**卖用户不相干的币**。这是全设计唯一可能直接亏用户本金的路径。

处置:现货腿的"仓位"定义为**相对建仓基线的增量**——unit 创建时快照该币余额为 baseline,
`position := balance_now − baseline`,baseline 随 unit 持久化。附加防线:对账发现余额
变动与本对冲的成交记录对不上(用户手动动了同币)时,**冻结该 unit(FROZEN)并告警**,
绝不自动"修正"。更干净的隔离(独立子账户)Backpack 是否支持 UNVERIFIED,支持则列为
推荐部署方式。

### 5.2 其余三件套

- **reduce_only 恒真假设**:_on_unwind / _trim 一律 reduce_only=True(executor.py:421-423、
  509),现货适配器静默忽略该参数(不是报错——报错会把撤退打断在最不该断的地方)。
  `_NO_POSITION_MARKERS`(executor.py:540-546)全是永续码,现货"余额不足"不在表里 →
  不走对账、计入 phase2_failures → 两次后 CROSS_HEDGE/HALTED。加 Backpack 现货余额不足
  标记(具体错误码 UNVERIFIED,实现期用小额实单捕获,照表内"按关键词认"的既有风格)。
- **保证金闸门映射**:check_gates / repair_verdict 用 available_margin/required_margin
  (executor.py:527、safety.py:291),LegHealth 默认 available_margin=inf(safety.py:264)
  → 现货腿闸门恒过,余额不足要到下单才发现——违背"绝不在热路径上补救"。现货适配器把
  可用 USDC(统一账户取 netEquityAvailable)映射进 available_margin,本单成本映射进
  required_margin。
- **杠杆路径**:对现货腿跳过 set_leverage / leverage tiers;liquidation_distance 传 None
  已被 risk_tier 正确处理(safety.py:485)。

### 5.3 同所带来的简化

Backpack 是单一跨保证金钱包,现货与永续**没有内部划转**这回事——跨所路径里的资金调拨
机制对 backpack 同所对是天然 no-op,can_transfer_in(safety.py:265)对这类腿恒 False 且
无害(同池不需要转)。MarketSpec.reduce_min_notional 至今无消费者(现货减仓尾单靠 dust
门槛兜着,safety.py:188-200 已含 1.5×min_notional,配合 §4.1 的合成 min_notional 够用),
记录在案不动它。

---

## 6. UI 与监控

- **现货腿显示**(ui/opportunities.py:349-359):现行 `费率 / 周期` 无条件渲染,现货腿会
  打出 "+0.0000% / 0h"——用户读成"零费率的永续"。按 market key 后缀 `:spot` 分支(显式
  判断,不用 interval==0 当哨兵——哨兵值会把"数据坏了"和"这是现货"混成一种显示),现货
  行改打 **"现货 · 无资金费"**。摘要/理由文案不动(本来就按实际数字生成)。
  **不改会怎么坏**:不炸,但每一行现货腿都在向用户撒谎(假费率假周期),而这个工具的
  卖点就是口径钉在明面上——费率、挂单口径都有标注体系,现货腿反而无标注,自我打脸。
- theme.leg_label(ui/theme.py:127-134)已会把 kind 打出来("BP·spot SOL_USDC"),免费
  拿到一半标识;abbrev 表补 backpack/hyperliquid/kucoin 缩写(缺了只丑不坏)。
- StaleMonitor 打点(watch.py:511、721)写死 `f"{venue}:perp"`,现货通道上线后按 market
  key 打点——否则现货行情源停更**永远不会被喊出来**,评分拿着陈尸盘口继续出行。
- ui/app.py:60 附近"跨所配对至少勾两家"的交互下限提示,同所配对上线后不再成立,文案改成
  "勾一家(需支持现货)或两家"。

---

## 7. 边界情况(每一条都要有测试)

| 情形 | 处理 |
|---|---|
| 持有中费率翻负 | score_pair 的 shortable 闸(§3.2)把行判 infeasible;已持仓的对走守护模式既有的"费差消失"判决。翻负间隙付资金费会触发 **USDC 自动借款计息**(autoBorrowSettlements,UnrealizedNegativePnl 计息)——实盘核算必须把 `/wapi/v1/history/interest` 相应项计入,评分不预估(金额小、依赖账户余额结构) |
| 现货簿一档 < MIN_TOP_NOTIONAL | 不合成该行(§2.2) |
| 现货/永续 stepSize 不同 | 两腿数量按**较粗步长**对齐,残余 delta 记入(照跨所乘数对齐的既有规则);实测 SOL_USDC 现货与永续 tick/step 相同,BTC 不同(现货 step 0.00001) |
| SPOT 行残留 fundingInterval 字段 | 按 marketType 分支解析,禁止按字段有无判断(§4.1) |
| 账户 autoLend=true,余额在 lend 池 | 卖单带 autoLendRedeem=true;余额读数是否含 lend 中部分 UNVERIFIED,实测后钉死 balance 口径 |
| 用户手动动了同币余额 | 对账突变与成交记录对不上 → FROZEN + 告警,不自动修正(§5.1) |
| 费率字段单位 | account 端点费率是**基点**且可为负(返佣),÷10000 + sanity clamp,clamp 触发即告警退默认档(§4.1) |
| 100ms speed bump | 两腿都是 taker 时同延迟,腿间风险窗 += 100ms;批量端点 POST /api/v1/orders 能否跨现货/永续混批、是否原子 UNVERIFIED——**设计不依赖它**,两腿仍单单下发 |
| minNotional 缺失 | filters 无名义额过滤器,合成 min_notional = minQuantity×价格;撮合隐性拒单 UNVERIFIED,首笔实单验证 |
| 计价资产 | Backpack 现货计价 USDC、永续结算 USDC,同池无兑换;未来接 USDT 计价所需在 normalize 层对齐,v1 不涉及 |
| 资金费结算周期 | 实测全部 PERP fundingInterval=3600000(1h),逐合约读字段不写死(perp 适配器已遵守);单期上下限 ±100bp |

---

## 8. 改动文件清单(file:line;主控落 vs 可派)

"可派" = 自包含、有参照实现、错了不污染共享路径,可交给实现代理;"主控" = 共享热文件,
改动牵动全管道口径,主会话亲自落。

| # | 文件(绝对路径前缀 `C:\Users\QUBIC\skills\earnfarm\earnfarm\`) | 锚点 | 改动 | 归属 | 估行数 |
|---|---|---|---|---|---|
| 1 | `exchanges\backpack.py` | :338-358 构造器;各抽象方法 | 拆 SPOT 拒绝,按 §4.1 表加现货分支(参照 okx.py 全套) | **可派** | +350~450 |
| 2 | `exchanges\base.py` | :146-150;类属性区 | quota 可注入 + supported_kinds 默认值 | 主控 | +25~40 |
| 3 | `session.py` | :288 | 按 supported_kinds 双注册 + 同 venue 共享 quota | 主控 | +30~50 |
| 4 | `public_feed.py` | :51-66、:123-137、:232、:306-341、:461-541 | DEFAULT_TAKER_SPOT;现货实例与 market key 寻址;配对分支;现货 LegQuote;常数 0 历史 | 主控 | +150~200 |
| 5 | `scoring.py` | :451-463、:477 | shortable 闸;kappa 按配对类型 | 主控 | +30~50 |
| 6 | `models.py` | :362-378 | (可选)现货反向的友好报错 | 主控 | +5~10 |
| 7 | `executor.py` | :445-467、:540-546 | 现货基线对账 + 余额不足标记 | 主控 | +80~120 |
| 8 | `safety.py` | :264-265 及消费点 | 现货 LegHealth 语义注记(填充在适配器侧) | 主控 | +15~30 |
| 9 | `ui\opportunities.py` | :349-359 | 现货腿"现货 · 无资金费"分支 | 可派 | +15~25 |
| 10 | `ui\theme.py` | :127-134 | abbrev 补缩写 | 可派 | +5 |
| 11 | `watch.py` | :511、:721 | StaleMonitor 按 market key 打点 | 主控 | +10~20 |
| 12 | `ui\app.py` | :60 附近 | 勾选下限文案 | 可派 | +5 |
| 13 | 测试(新文件,按 §7 表逐行出用例 + §5.1 基线对账专项) | — | — | 可派(用例表主控出) | +300~400 |

新文件:仅测试。合计约 **1000~1500 行**(其中约半数在可派项)。
依赖顺序:1 →(2+3)→ 4 →(5+6)→ v1a 可用 →(7+8)→ v1b →(9-12)随行。

---

## 9. 风险清单(按危险程度排)

1. **对账吸入用户存货**(§5.1):唯一直接亏本金的路径。v1b 上线硬门 = 基线对账 + 突变
   FROZEN 的专项测试通过;推荐部署在无闲杂余额的账户。
2. **费率单位换算错**:bps 忘 ÷10000 = 成本高估 100 倍(行全灭,显性);换算后符号/字段
   张冠李戴 = 低估(隐性,更危险)。sanity clamp + 上线首日与网页端账单人工对一次。
3. **rescore 翻负定向翻转**(§3.2):两道闸缺一即出"不可执行方向的漂亮分数"。
4. **限频分池击穿风控预留**(§4.2):必须与双注册同一提交落地,不可拆。
5. **现货费率默认档低估**(§2.3):v1a 就要落,不等 v1b。
6. **balance 口径含糊**(lend 池、冻结额):UNVERIFIED 项里唯一影响对账正确性的,实测优先级最高。
7. **费率翻负间隙的 USDC 借息**:不进评分,但必须进实盘核算,否则长期收益虚高几个 bp。
8. **kappa=1.3 的缓冲拍脑袋**:方向正确(介于 1.0 与 2.0),数值待 netEquityAvailable 实测校准;校准前禁止下调。
9. **深度长期薄于跨所对**:容量薄边如实呈现即可,不是风险,是事实——但要防"容量小 → 行被 MAJORS 保底顶上来 → 用户误读"的组合,MAJORS 保底行同样吃容量列。

---

## 10. UNVERIFIED 汇总(全部显式,上线前逐项清零或明确接受)

| # | 事项 | 来源 | 消解方式 |
|---|---|---|---|
| 1 | VIP 费率表中间档数值(机器转录) | Backpack 调研 | 上线前人工比对官方 VIP 程序页 |
| 2 | 批量下单跨现货/永续混批与原子性 | Backpack 调研 | 设计不依赖;若做腿间压缩再实测 |
| 3 | borrowInterestRate APR/APY 口径 | Backpack 调研 | v2 前置,history/interest 实测一天反推 |
| 4 | 现货隐性最小名义额拒单 | Backpack 调研 | 首笔小额实单验证 |
| 5 | 资金费 8h→1h 切换公告的年份 | Backpack 调研 | 实测已全 1h,仅存档意义 |
| 6 | autoLendRedeem 对未 lend 余额是否无害 | 本设计 §4.1 | 实测一笔带参卖单 |
| 7 | 余额端点是否计入 lend 中资产 | 本设计 §4.1/§5.1 | 实测:lend 前后各读一次 |
| 8 | 现货余额不足的错误码/文案 | 本设计 §5.2 | 实现期小额实单捕获入表 |
| 9 | Backpack 子账户支持与 API 隔离 | 本设计 §5.1 | 查文档 + 实测;支持则写进部署建议 |
| 10 | clientId 重复是否拒单(perp 遗留) | backpack.py 头注 | 不当幂等闸的现状延续,现货同 |
