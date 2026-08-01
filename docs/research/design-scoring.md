# 跨交易所资金费套利:机会评分系统设计

> 核心判断:qqq.bot 的排序是 **"毛信号排序"**,而套利是 **"费用生意"**。正确的做法不是给毛指标加权重,而是**直接把每个机会折算成"在你的仓位、你的持有期下,扣完所有摩擦后的风险调整净年化"**,再按这个可比的实数排序。评分不是打分,是**估值**。

---

## 0. 符号与约定(必须先钉死,否则符号错误会静默产生反向排名)

| 符号 | 含义 | 单位 |
|---|---|---|
| `f` | 资金费率,**正 = 多头付给空头** | 小数 (0.0001 = 1bp) |
| `T` | 该腿结算间隔 | 小时 (1 / 4 / 8) |
| `L` / `S` | 多腿(买入的交易所)/ 空腿 | — |
| `N` | 单腿名义 | 计价币 (USDT) |
| `H` | 计划持有期 | 小时 |
| `Y = 8760` | 24×365 | 小时/年 |
| `κ` | 资金占用系数 = `m_L + m_S`(各腿保证金率之和),1x/1x 双边 = 2.0 | — |

**方向自动定向**:对任意交易所对 (X, Y),比较 **小时化费率** `f/T`,在 `f/T` 低的一边做多、高的一边做空。所有历史统计必须按**当前这个定向**取符号,不能取绝对值(否则翻转频率完全失去意义)。

所有中间量统一用"**占名义的比例**"(不是 bp、不是 %),只在展示层转换。

---

## 1. 净年化公式(含不同结算周期归一化)

### 1.1 毛 carry 的正确归一化

单期收益不可比(1h 的 1bp ≠ 8h 的 1bp)。先化成**小时化 carry**:

```
c_hour = f_S / T_S  −  f_L / T_L          [每小时,占名义比例]
APR_gross(名义) = c_hour × 8760
APR_gross(资金) = c_hour × 8760 / κ
```

推导:空腿每 `T_S` 小时收 `f_S`,多腿每 `T_L` 小时付 `f_L`;单位时间净收 `f_S/T_S − f_L/T_L`。资金费按名义线性计提、不复利,故用**单利年化**(用复利年化是常见错误,会把 8h 30bp 这类高费率放大到荒谬的量级)。

### 1.2 但是:收益是**离散**到账的,持有期短时必须数结算次数

`c_hour × H` 只在 `H >> T` 时成立。实盘决定成败的恰恰是 `H ≈ 几个 T` 的区间,必须精确计数:

给定当前时刻 `t0`、下次结算时刻 `t_next`、间隔 `T`,持有到 `t0+H`,结算发生在
```
t_k = t_next + (k−1)·T ,  k = 1,2,...  且  t_k ≤ t0 + H
n(H) = max(0, floor((H − (t_next − t0)) / T) + 1)
```
**若 `H < t_next − t0`,则 n = 0,一分钱资金费都收不到,但四笔手续费一分不少。** 这就是"看着 3bp 很香、实际先亏 20bp"的机制化表达。

### 1.3 费率不会永远维持:引入衰减预测路径

qqq 的第二宗罪是**默认当前费率永续**。用 AR(1) 收敛到长期均值:

```
f_k = f̄ + (f_now − f̄) · ρ^(k−1)          k=1 时恰为 f_now(交易所给的下期预测费率,近似已知)
ρ  = 该腿历史费率在结算频率上的一阶自相关,截断到 [0, 0.98],样本少时向 0.7 收缩
f̄  = 历史中位数(用中位数不用均值,抗尖峰)
```

### 1.4 完整净年化

```
FundingIncome(H) = Σ_{k=1..n_S(H)} clamp(f_S,k)  −  Σ_{k=1..n_L(H)} clamp(f_L,k)

Cost_rt = Fee_open + Slip_open + Fee_close + Slip_close + Transfer   (§4)

Net(H) = FundingIncome(H) − Cost_rt + BasisPnL(H)                    (§5)

────────────────────────────────────────────────
APR_net = Net(H) × 8760 / H / κ
────────────────────────────────────────────────
```

`clamp` = 交易所费率上下限(命中上限的币种常伴随结算周期被动缩短,见 §7)。

```python
HOURS_PER_YEAR = 8760.0

def settlement_offsets(now_ts, next_ts, interval_h, horizon_h):
    """返回持有期内每次结算距今的小时数;自动修正过期的 next_funding_time"""
    step = interval_h * 3600.0
    t = next_ts
    if t < now_ts:                      # 数据陈旧,推进到未来第一个
        t += math.ceil((now_ts - t) / step) * step
    end, out = now_ts + horizon_h * 3600.0, []
    while t <= end + 1e-6:
        out.append((t - now_ts) / 3600.0)
        t += step
    return out

def forecast_path(f_now, f_bar, rho, n, cap):
    return [max(-cap, min(cap, f_bar + (f_now - f_bar) * rho ** k)) for k in range(n)]

def funding_income(long_leg, short_leg, horizon_h, now_ts):
    nS = len(settlement_offsets(now_ts, short_leg.next_ts, short_leg.T, horizon_h))
    nL = len(settlement_offsets(now_ts, long_leg.next_ts,  long_leg.T,  horizon_h))
    inc  = sum(forecast_path(short_leg.f_now, short_leg.f_bar, short_leg.rho, nS, short_leg.cap))
    inc -= sum(forecast_path(long_leg.f_now,  long_leg.f_bar,  long_leg.rho,  nL, long_leg.cap))
    return inc, nS, nL
```

---

## 2. 回本周期:必须解**时间**,不是解次数

两腿周期不同 + 费率衰减 ⇒ 累计收益是**分段阶跃且非线性**的,闭式解只在"同周期、无衰减"时成立:

```
无衰减近似:  n* = ceil( Cost_rt / (f_S − f_L) )        [同周期]
             H* = Cost_rt / c_hour                     [异周期,小时]
```

真实值用事件驱动扫描(把两腿所有结算点按时间合并,逐个累加,首次跨过 `Cost_rt` 的时刻就是 `H*`):

```python
def breakeven_hours(long_leg, short_leg, cost_rt, now_ts, max_h=24*30):
    ev = []
    for leg, sgn in ((short_leg, +1), (long_leg, -1)):
        offs = settlement_offsets(now_ts, leg.next_ts, leg.T, max_h)
        path = forecast_path(leg.f_now, leg.f_bar, leg.rho, len(offs), leg.cap)
        ev += [(h, sgn * f) for h, f in zip(offs, path)]
    cum = 0.0
    for h, amt in sorted(ev):
        cum += amt
        if cum >= cost_rt:
            return h                      # 精确到该次结算落地的小时
    return math.inf                        # 永不回本
```

### 怎么呈现给用户(这是产品的胜负手)

不要只给一个"7 期"。给**三元组 + 一句判决**:

```
回本:  56 小时(7 次结算)   |  你的计划持有期:24 小时
该费差历史同号中位存续:12 小时
判决:[红] 在回本前费率大概率已消失,期望为负
```

关键新增量是 **`P(活到回本)`**:用历史"同号连续存续时长"的经验分布,取 `1 − F(H*)`。回本 56h、而历史上这个费差符号中位只活 12h ⇒ 存活概率 ~15% ⇒ 无论毛年化多漂亮都要沉底。

---

## 3. 历史稳定性评分

### 3.1 先做时间轴对齐(否则一切统计都是错的)

把两腿各自的结算序列**摊成小时序列**(第 k 期的 `f` 均摊到它覆盖的 `T` 小时),再逐小时相减,得到**按当前定向取符号**的 carry 序列 `x_t`(每小时占名义比例):

```python
def to_hourly_series(fs, ts, interval_h, grid):      # 阶梯摊平,含缺失前向填充上限
    ...
x = short_hourly - long_hourly                       # 定向后的小时 carry,x>0 才是赚
```

### 3.2 主指标:滚动窗口"费后"回测(比矩统计诚实得多)

对每个长度 `W = round(H)` 的历史窗口计算**真实净收益**:

```
net_i = Σ_{t=i..i+W-1} x_t  −  Cost_rt
p_win = mean(net_i > 0)
APR_i = net_i × 8760 / H / κ      → 取中位数、25 分位(下行情形)
n_eff = len(x) / W                 # 窗口重叠,有效样本数不是窗口数
```
`p_win` 用 Beta(2,3) 先验收缩(先验均值 0.4,偏保守):
```
p̂ = (p_win·n_eff + 2) / (n_eff + 5)
```

### 3.3 辅助分量(全部映射到 [0,1])

| 分量 | 公式 | 含义 |
|---|---|---|
| 一致性 `A` | `clip(2·p_same_sign − 1, 0.02, 1)` | 抛硬币(50%)= 0 |
| 存续力 `P` | `clip(median_run_hours / H*, 0, 1)` | 同号中位存续 vs 回本时间 |
| 衰减 `D` | `clip(mean(x[-25%:]) / mean(x), 0, 1)` | **只罚不奖**:上升趋势封顶 1 |
| 幅度稳定 `V` | `\|μ\| / (\|μ\| + MAD)` | 用 MAD 不用 σ,抗尖峰 |

`median_run_hours` = 历史上 `sign(x_t)` 与当前定向一致的连续段长度的中位数。

### 3.4 综合(用加权几何均值,不用加权和)

```
S_hist = p̂^0.35 · A^0.20 · P^0.25 · D^0.10 · V^0.10
```
**为什么是乘性**:任一分量趋近 0 应当直接否决整个机会(符号乱翻的费差,再高的均值也没用)。加权和会让一个 80 分的均值把 5 分的翻转率救回来——这正是要修的病。

```python
def stability(x, H, cost_rt, kappa):
    W = max(1, int(round(H)))
    if len(x) < W + 12:
        return dict(S=0.35, is_prior=True, n_eff=0)          # §7 新合约
    cum   = np.convolve(x, np.ones(W), 'valid')
    net   = cum - cost_rt
    n_eff = max(1.0, len(x) / W)
    p_hat = (net > 0).mean() * n_eff
    p_hat = (p_hat + 2) / (n_eff + 5)
    A = np.clip(2 * (np.sign(x) == np.sign(x.mean())).mean() - 1, 0.02, 1)
    mu, mad = x.mean(), np.median(np.abs(x - np.median(x))) + 1e-12
    V = abs(mu) / (abs(mu) + mad)
    D = np.clip(x[-len(x)//4:].mean() / mu, 0, 1) if mu > 0 else 0.02
    run = median_run_hours(x, np.sign(mu))
    return dict(
        S = p_hat**0.35 * A**0.20 * np.clip(run / max(H,1e-9),0,1)**0.25 * D**0.10 * V**0.10,
        p_win=p_hat, run_h=run, n_eff=n_eff,
        apr_med = np.median(net) * HOURS_PER_YEAR / H / kappa,
        apr_q25 = np.percentile(net, 25) * HOURS_PER_YEAR / H / kappa,
        sigma_apr = net.std() * HOURS_PER_YEAR / H / kappa,       # 已含自相关,优于 σ√H 解析式
        is_prior=False)
```

---

## 4. 滑点惩罚(只有一档盘口 + 深度)

### 4.1 分解

taker 成本 = **半价差**(任何大小都要付) + **穿透冲击**(超出一档的部分才付)。

以中价 `m=(bid+ask)/2` 为基准:
```
半价差   s = (ask − bid) / (2m)
一档名义 q1 = ask_sz × ask   (买方向) / bid_sz × bid (卖方向)
```
若 `Q ≤ q1`:`slip = s`
若 `Q > q1`:设带宽 `b`(相对价格,如 ±10bp = 0.001)内可见深度名义 `Dep`,**均匀簿密度** `ρ = Dep / b`(名义/单位相对价格)。吃掉超出部分 `E = Q − q1` 需要下探
```
δ = E / ρ,  超出部分的平均额外成本 = δ/2
slip = η · [ s + (E/Q) · (E / (2ρ)) ]        η = 保守系数(默认 1.5,含延迟与两腿异步)
```
无盘口深度时退化到平方根律:`slip = s + k·σ_daily·sqrt(Q/ADV)`,`k ≈ 0.7`,并把数据置信度打折。

### 4.2 四笔执行

```
Cost_rt = (fee_L + slip_L^buy) + (fee_S + slip_S^sell)          # 开
        + (fee_L + γ·slip_L^sell) + (fee_S + γ·slip_S^buy)      # 平,γ=1.2 紧急度
        + transfer_amort                                         # 跨所调拨/提现摊销
```
紧急度系数只乘滑点、不乘手续费(常见实现 bug)。若允许挂单离场,把平仓换成 `maker_fee` 并单独标注"依赖挂单成交"。

### 4.3 副产品:**容量**(qqq 完全没有的维度)

评分是**仓位相关**的。二分搜索满足 `APR_net(Q) ≥ 阈值` 的最大 `Q`,展示为"该机会容量 $X"。同时硬过滤:参与率 `u = Q / (α·Dep)`(α=0.2)超过 1 直接判不可执行。

```python
def slip_rate(leg, side, notional, eta=1.5):
    m  = (leg.bid + leg.ask) / 2
    s  = (leg.ask - leg.bid) / (2 * m)
    q1 = leg.ask_sz * leg.ask if side == 'buy' else leg.bid_sz * leg.bid
    if notional <= q1 or leg.depth <= 0:
        return eta * s
    rho = leg.depth / (leg.depth_band_bp / 1e4)
    E   = notional - q1
    return eta * (s + (E / notional) * (E / (2 * rho)))

def capacity(f_score, lo=100, hi=5_000_000, target_apr=0.10):
    for _ in range(24):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f_score(mid) >= target_apr else (lo, mid)
    return lo
```

---

## 5. 价差回归收益

多 A 空 B、**等数量** `Q_base`。定义相对基差 `D = (P_A − P_B) / P_ref`(用两所**中价**,不用买卖价——买卖价的部分已经在滑点里算过了,否则重复计费)。

```
BasisPnL = D_exit − D_entry              (长 A 短 B:A 相对变贵则赚)
```
你通常在费率低(=价格便宜)的一侧做多,故 `D_entry < 0`,回归到均衡 `D̄` 是**顺风收益**。

### 期望回归:OU 部分回归,不是全额回归

```
E[D_exit] = D_entry + θ·(D̄ − D_entry),   θ = 1 − exp(−H/τ)
τ = 历史基差的均值回归时间常数(对 D_t 拟合 AR(1):τ = −1/ln(ρ_D),小时)
BasisPnL_exp = θ·(D̄ − D_entry)
APR_basis = BasisPnL_exp × 8760 / H / κ
```

**三条必须写进代码的护栏**(否则这一项会用噪声霸占榜首):
1. **年化爆炸**:5bp 基差 / 1 小时 = 438% 年化。必须 `clip(APR_basis, ±0.5·|APR_funding|, 硬上限 1.0)`。
2. **不对称计提**:顺风只计 `κ_fav = 0.5` 折扣,逆风(`D_entry` 不利)**全额计入成本**。套利要在会计上悲观。
3. **单列展示**,不与资金费收益混在一个数字里 —— 它是低置信项,且退出基差本身有方差 `σ_D`,要进 §6 的风险项。

```python
def basis_terms(D_entry, D_bar, tau_h, H, kappa, apr_funding):
    theta = 1 - math.exp(-H / max(tau_h, 1e-6))
    raw   = theta * (D_bar - D_entry)
    raw   = raw * (0.5 if raw > 0 else 1.0)               # 顺风打折,逆风全额
    apr   = raw * HOURS_PER_YEAR / H / kappa
    lim   = min(1.0, 0.5 * abs(apr_funding) + 0.05)
    return max(-lim, min(lim, apr)), raw
```

---

## 6. 最终排序

### 6.1 合成:先算实数,再打置信折扣

```
APR_net = (FundingIncome(H) − Cost_rt) × 8760/H/κ  +  APR_basis        [期望净年化]

σ_APR   = sqrt( σ_hist²  +  (σ_D × 8760/H/κ)² )                        [不确定性]

RA      = APR_net − λ·σ_APR                     λ 默认 0.5(均值-方差确定性等价)

C       = S_hist · S_liq · S_data · S_exch      置信度 ∈ (0,1]
          S_liq  = exp(−max(0, u − 0.25))        u = Q/(α·Dep)
          S_data = Π(每个降级回退 ×0.85),下限 0.3
          S_exch = 交易所信用系数(用户配置 0.5~1.0)

────────────────────────────────────────────
Score = C · RA        若 RA > 0
Score =     RA        若 RA ≤ 0        ← 关键的不对称
────────────────────────────────────────────
```

**为什么负值不打折**:`Score = C·RA` 在 `RA<0` 时,低置信度会让亏损**看起来更小**,把"数据缺失的烂机会"排到"数据完整的烂机会"前面——这是一个会静默产生错误排名的符号陷阱。收益打折、亏损全额。

### 6.2 分组与硬门(排序之外的判决层)

先分组,组内再按 Score 排:

| 组 | 触发条件 |
|---|---|
| 不可执行(不参与排序) | `u > 1`;数据过期 > 60s;合约即将下架;单腿停盘 |
| **负期望 [红]**(单独沉底分组) | `APR_net ≤ 0` **或** `H* = ∞` **或** `H* > H_target` |
| 高风险 [橙] | `H* > median_run_hours`(回本前费率大概率消失)/ 容量 < 目标仓位 / `apr_q25 < 0` |
| 无评级 [灰] | 新合约,`n_eff` 不足,`S_hist` 用先验 |
| 正常 | 其余 |

### 6.3 UI:一眼看到"为什么它不是好机会"

对每一行给**费用穿透瀑布**,并把 qqq 口径的毛值划掉放在旁边:

```
ASTER  币安(空) × Bybit(多)                      净年化  −40.2%  [红 负期望]
────────────────────────────────────────────────────────────────
毛年化(qqq 口径)                                        +32.9%
  − 往返手续费 20.0bp                                    −73.0%
  − 滑点 3.4bp                                           −12.4%
  ± 价差回归 +1.1bp                                       +4.0%
  ────────────────────────────────────────────────
  = 净年化(24h 持有 / $10,000 仓位)                     −40.2%
回本 56h(7 次结算) > 计划持有 24h;历史同号中位存续 12h
费后历史胜率 18%(n_eff=41) · 容量 $23k
```

三个默认排序键:**风险调整净年化**(默认)、**净年化**、**容量**。绝不提供"按毛费差排序"作为默认。

---

## 7. 边界情况(每一条都要有测试)

| 情形 | 处理 |
|---|---|
| `f = 0` 或两腿费率相等 | `c_hour = 0` → `H* = ∞`,显示"永不回本",归入负期望组。禁止 `cost/c` 直除 |
| `c_hour < 0` | 定向反了 → 由 `orient()` 交换两腿;若交换后仍 ≤0 说明无机会 |
| 下期预测费率缺失 | 回退到历史中位数,`S_data ×0.85`,并标注"预测费率不可得" |
| 深度缺失 | 平方根律或"3× 中位价差"的惩罚性默认值,`S_data ×0.85`,置信度封顶 0.5 |
| **新上市无历史** | 不排除(新币恰恰费率最高),但 `S_hist = 0.35` 先验 + 灰标 + **不允许进入首屏前三**;展示"需持有 ≥ H* 才回本,无历史可验证" |
| 极端值 | 历史序列 1%/99% winsorize;费率 clamp 到交易所上限;主用中位数/MAD,均值/σ 仅作诊断 |
| **结算周期动态变化** | 部分交易所在费率触顶时把 8h 自动切 4h/1h。`T` 必须每轮从 API 读取,**禁止硬编码**;历史序列要按当时的 `T` 摊平,不能用当前 `T` 回溯 |
| `next_funding_time` 已过期 | `settlement_offsets` 内自动向前推进(已实现);同时若过期 > 1 个周期则判数据源故障 |
| 币本位/反向合约 | 资金费以币计价、名义非线性 → 不与 USDT 线性合约同池比较,单独通道或直接排除 |
| 合约乘数/最小下单量不同 | 两腿数量取 `lcm` 对齐后的可行量,残余暴露计入 delta 风险;`Q` 向下取整到可执行档 |
| 两所标记价偏离 > 2% | 疑似停盘/清算/下架 → 不可执行组 |
| 历史样本 < W+12 | 走先验分支,不要用 3 个样本算出 100% 胜率 |
| 资金无法调拨(提现关闭/链拥堵) | `S_exch` 归零,或直接不可执行 |
| 除零/NaN 传播 | 所有除法带 `1e-12`;最终 Score 为 NaN 一律降到不可执行组,**不能默默当 0**(0 会排在负期望之上) |

---

## 8. 装配骨架

```python
@dataclass
class Leg:
    ex: str; sym: str
    bid: float; ask: float; bid_sz: float; ask_sz: float
    depth: float; depth_band_bp: float                    # ±band 内可见名义
    f_now: float; T: float; next_ts: float; cap: float
    taker_fee: float; maker_fee: float
    hist_f: np.ndarray; hist_ts: np.ndarray
    f_bar: float = 0.0; rho: float = 0.7

def orient(a: Leg, b: Leg):
    return (a, b) if a.f_now / a.T <= b.f_now / b.T else (b, a)   # (多腿, 空腿)

def score_opportunity(a, b, notional, H, kappa=2.0, lam=0.5, now_ts=None):
    L, S = orient(a, b)
    now_ts = now_ts or time.time()

    cost = (L.taker_fee + slip_rate(L,'buy', notional)) + (S.taker_fee + slip_rate(S,'sell',notional)) \
         + (L.taker_fee + 1.2*slip_rate(L,'sell',notional)) + (S.taker_fee + 1.2*slip_rate(S,'buy',notional)) \
         + TRANSFER_AMORT

    inc, nS, nL = funding_income(L, S, H, now_ts)
    Hstar       = breakeven_hours(L, S, cost, now_ts)

    x   = aligned_hourly_spread(L, S)                       # §3.1,按当前定向取符号
    st  = stability(x, H, cost, kappa)

    apr_fund  = (inc - cost) * HOURS_PER_YEAR / H / kappa
    apr_basis, _ = basis_terms(D_entry(L,S), D_bar(L,S), tau_h(L,S), H, kappa, apr_fund)
    apr_net   = apr_fund + apr_basis

    sigma = math.hypot(st['sigma_apr'], sigma_D(L,S) * HOURS_PER_YEAR / H / kappa)
    ra    = apr_net - lam * sigma

    u      = notional / (0.2 * min(L.depth, S.depth) + 1e-12)
    C      = st['S'] * math.exp(-max(0, u-0.25)) * data_quality(L,S) * exch_credit(L,S)
    score  = C * ra if ra > 0 else ra                        # 不对称:亏损不打折

    flags = []
    if u > 1 or stale(L,S):                       flags.append('INFEASIBLE')
    if apr_net <= 0 or math.isinf(Hstar) or Hstar > H: flags.append('NEGATIVE_EV')
    if Hstar > st.get('run_h', 0):                flags.append('DECAY_RISK')
    if st.get('is_prior'):                        flags.append('NO_HISTORY')
    if not math.isfinite(score):        score, flags = -math.inf, flags + ['INFEASIBLE']

    return dict(score=score, apr_net=apr_net, apr_gross=(S.f_now/S.T - L.f_now/L.T)*HOURS_PER_YEAR,
                cost=cost, breakeven_h=Hstar, n_settle=(nL, nS), sigma=sigma, conf=C,
                capacity=capacity(lambda q: score_opportunity(a,b,q,H,kappa)['apr_net']),
                hist=st, flags=flags)
```

---

## 9. 自检用例(用你给的那个场景做金标准)

**输入**:两腿均 8h;`Δf = 3bp/期`;taker 5bp × 4 = 20bp;κ=2;`f̄=1bp`,`ρ=0.85`。

| 口径 | 结果 |
|---|---|
| qqq 显示(毛年化,名义) | **+32.85%** ← 榜首 |
| 无衰减回本 | 20/3 = 6.67 → **7 期 = 56h** |
| **含衰减回本** | 累计 `n + 13.33(1−0.85ⁿ)` 首次 ≥20bp → **10 期 = 80h** |
| H=24h(3 期) | 9−20 = **−11bp** → 名义 −40.2%,**资金 −20.1%** |
| H=56h(恰好"回本") | 21−20 = 1bp → 名义 +1.56%,**资金 +0.78%**(扛 2.3 天赚 0.78% 年化) |
| H=168h 含衰减(21 期) | 33.9−20 = 13.9bp → 名义 +7.2%,**资金 +3.6%** |

**结论行**:`32.85% → 3.6%`,九倍缩水,且需扛满 7 天;而若该费差历史同号中位只活 12h,`P(活到 80h)` ≈ 0.1 ⇒ 判 `NEGATIVE_EV`。

必须写的断言:
```python
assert score(f_L=f_S)['breakeven_h'] == inf                 # 零费差不崩、不排前
assert score(H=1)['n_settle'] == (0,0) and score(H=1)['apr_net'] < 0   # 短持有=纯付费
assert score(...)['apr_net'] == pytest.approx(-0.4015, abs=1e-3)       # 上表 H=24 场景
assert score(no_hist=True)['flags'].count('NO_HISTORY')                # 新合约有灰标
assert rank([lowconf_loser, highconf_loser])[0] is not lowconf_loser   # 不对称置信生效
assert score(T_L=1, T_S=8) 的 c_hour == f_S/8 - f_L/1                  # 异周期归一化
```

---

## 附:与既有结论的一致性

这套模型的三个核心修正,和你 2026-07 因子绞肉机跑出来的结论完全同源——**手续费才是杀手、降换手 > 压费率、开仓挂单省一半成本**。因此实现上有两个高优先级的产品化推论:
1. 把 `exit_is_taker=False`(挂单离场)做成开关,它直接把 `Cost_rt` 砍掉近一半,是排名变化最大的单一参数;
2. 默认 `H_target` 不要给 8h 这种短值,短持有期在这个成本结构下几乎必然负期望——默认值建议 3 天,并让用户看到 `APR_net(H)` 的曲线而不是单点。