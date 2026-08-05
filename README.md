# carryfarm

跨交易所资金费率套利工具。在币安、OKX、HTX、Gate、Bybit、Bitget、Hyperliquid、
KuCoin 八家之间找同一个币的资金费差，一边做多一边做空，赚两边费率的差额，
不吃价格波动。Hyperliquid 是每**1 小时**结算一次的链上永续——结算频率和
CEX（4/8 小时）不同步，它和 CEX 之间天然比 CEX 互相之间更容易出费差。

这份文档不是功能清单。它是**用这个工具之前你该知道的事**——尤其是它算出来的结论，
大概率和你想看到的不一样。

---

## 先说结论：绝大多数机会是负期望

2026-08-01 的一次实测。六家全部连通，名义仓位 5 万美元，假设持有 72 小时，
榜上 60 个候选，**120 条腿全部有真实历史数据**（不是先验、不是估算）：

| 判决 | 数量 |
|---|---|
| 可做（good / sized_down） | **0** |
| 负期望（negative） | 52 |
| 做不了（unexecutable，深度不够） | 8 |

榜首那几个长这样：

| 币 | 毛年化 | 净年化 | 往返成本 |
|---|---|---|---|
| BTC | +6.9% | **−8.2%** | 0.20% |
| ONDO | +46.7% | **−17.4%** | 0.34% |
| AERO | +34.2% | **−17.9%** | 0.37% |
| BNB | +24.9% | **−14.4%** | 0.31% |

ONDO 的毛年化 46.7% 是真的，它不是 bug。净年化 −17.4% 也是真的。
两个数字之间隔着这个工具存在的全部理由：

**1. 成本要按持有期摊销，而不是按年。**
0.34% 的往返成本听着像噪音。但你持有 72 小时就平掉，这 0.34% 就要摊在
3 天上——年化 41%。开平各两笔、共四笔 taker 手续费加滑点，是这门生意里
最大的一笔支出，通常比你赚的资金费还多。

**2. 当前费率不可外推。**
"现在费率是 0.05%，一年 8760 小时，所以年化 46.7%" —— 这句话是错的，
而几乎所有同类工具的榜单都是这么排的。资金费是均值回复的：一个飙到 46% 的
费率，最常见的下一步是掉回去，不是保持。本工具的净年化用的是**历史回测出来的
预期收益**（把这个币的资金费历史按你的持有期滚动，看它实际能收到多少），
而不是把此刻的快照乘以 8760。毛年化那一列只是拿来对照的，**不要看它做决定**。

所以：**看到满屏"负期望"是这个工具在正常工作，不是它坏了。**
资金费套利常常整段时间都没有正期望的机会，那时候不做才是对的。
真能做的机会稀少而且短暂，常常只活几个小时——这也是为什么有守护模式。

---

## 第二件事：成本是结论变量，不是精度问题

费率档位不是"算得准一点"的问题，是**判决正反的问题**。同一个机会、同一份行情、
同一份历史，只换费率档位（`tests/test_fees.py` 里有一个金标准测试钉着这组数）：

```
持有期收益 = 9 次结算 × 2.4bp        = 0.00216
VIP0 成本                            = 0.00263  → 净年化 −2.9%  判 negative
VIP3 成本                            = 0.00113  → 净年化 +6.3%  判 good
```

毛年化两边一字不差，变的只有成本，结论从"别做"翻成"能做"。

因此：

- **没连账户时，榜上所有成本都是按各家 VIP0 挂牌价估的**，界面会在成本列打黄色
  `*`、在摘要行直接写"成本按 VIP0 挂牌价估算"。看到星号就意味着这一行的判决
  可能因为你的真实档位而反过来。
- **连上账户后自动换成你的真实档位重算**。七家 CEX 走签名端点拿真实费率；
  Hyperliquid 用你的主钱包地址查公开的 userFees（质押折扣已折算在内，不再重复打折）。
  账户页会显示"费率档位：已按你的真实档位计算"。
- **查不到某个品种的档位时，工具返回"没查到"而不是零。** 这条听起来是废话，
  但零手续费会让四笔往返成本整个消失、负期望的机会全片翻成正期望，而且不报错。
  这是这类代码里最贵的一种静默 bug。

### 平台币抵扣（BNB / GT / OKB / BGB）

一句话：**状态会告诉你，但成本不打折。**

- **Gate GT**：不用管。`/futures/{settle}/fee` 返回的已经是 VIP 档 + GT 折扣叠加后的
  最终费率，再乘一次就是重复打折。
- **币安 BNB**：开关状态读得到（`/fapi/v1/feeBurn`），**折扣幅度读不到**——它不在
  任何 API 里，只挂在帮助中心文案上，随时可改。所以工具只把"BNB 抵扣已开启"写进
  附注给你看，不擅自削成本。
- **OKX OKB**：早已并进 `level` 字段，返回值即最终档位。
- **Bybit / Bitget BGB / HTX 点卡**：衍生品没有可读的抵扣开关，接口返回的就是最终扣费率。

方向是安全的：实际扣费只会比工具显示的更低，不会更高。宁可低估收益，不可低估成本。

---

## 怎么跑

需要 Python 3.11+（用了 `tomllib`）。依赖只有三个：`httpx`、`nicegui`、`cryptography`。

### 界面模式

```bash
python run.py                # 浏览器打开 http://127.0.0.1:8777
python run.py --port 9000
python run.py --offline      # 用离线演示数据，不联网
```

必须走 `run.py` 这个顶层脚本，不能 `python -m carryfarm.ui.app`：NiceGUI 的
`ui.run()` 会用 `runpy.run_path` 重跑入口文件，那条路径不带包上下文，包内的
相对导入会全部炸掉。

默认**只监听回环地址**。要对外提供访问，必须在配置里显式打开 `allow_public`
并配好 TLS 证书——会话 cookie 和配对码在明文 HTTP 上会被中间人截获。

### 守护模式（无界面，7×24）

```bash
python run.py --watch                  # 跑起来，有机会才推送
python run.py --watch --once           # 只跑一轮就退出（cron / 排错）
python run.py --watch --status         # 问一句"它还活着吗"
python run.py --watch --log-file /var/log/carryfarm.log   # 自动轮转 5MB×3
python run.py --watch --config /etc/carryfarm.toml
python run.py --watch -v               # 连 httpx 的每个请求都打出来
```

这条路径**一行 nicegui 都不导入**，服务器上不用装它。

启动时会打一段自检，把最容易配错的事直接说出来：

```
行情源：6 家公开端点（binance、okx、htx、gate、bybit、bitget），实际连通以首轮行情为准
账户：6 个；推进已有仓位：否（凭据库没解锁，要解锁请设环境变量 CARRYFARM_MASTER_PASSWORD）
告警通道：log　（只有本地日志通道：手机上收不到任何东西，去 [alerts] 配一个通道）
节奏：行情 300s／历史 1800s／仓位 500ms
数据库：C:\Users\YJQ\.carryfarm\carryfarm.db
```

`--status` 的存在是为了分清"没机会"和"进程死了"——这两件事在推送上长得一模一样：

```
● 已停止（优雅退出，26 秒前）——不是崩溃，是有人让它停的。
  行情：1 轮（最近一轮 9 秒前），已连 binance、okx、htx、gate、bybit、bitget
  榜单：40 行，可执行 0 行；累计告警 0 条
  仓位：0 个（纸上，未接管）
  告警通道：log
```

主密码只从环境变量读，绝不落配置文件：`CARRYFARM_MASTER_PASSWORD`
（`CARRYFARM_PASSWORD` 也认，向后兼容）。不给密码就跑"只看不做"模式——
行情和告警照常，仓位一律不碰。

---

## 配置

配置文件 `~/.carryfarm/config.toml`，**不存在也能直接跑**（全默认值，只是没有账户）。
数据库固定在 `~/.carryfarm/carryfarm.db`。所有可调项都集中在 `carryfarm/config.py`，
没有第二处配置来源。写了不认识的段或键会直接报错，不会被静默忽略。

```toml
[scoring]
assumed_holding_days = 7      # 开平成本按几天摊销
history_days = 30             # 回看多少天历史做稳定性评分
min_net_apr = 0.05            # 净年化低于此值直接标负期望
stability_weight = 0.35       # 排序时历史一致性的权重
fallback_taker_fee = 0.0005   # 查不到真实档位时的兜底（就是那个会骗人的挂牌价）

[server]
host = "127.0.0.1"            # 非回环地址必须同时开 allow_public 且配 TLS
port = 8777

[safety]
naked_retry_seconds = 30      # 单腿裸奔多久后强制撤退
circuit_breaker_failures = 20 # 连续下单失败多少次整体停机等人工
leverage_mode = "conservative"

[watch]
market_interval_s = 300       # 硬下限 180 秒，见下
history_interval_s = 1800
notional = 50000
horizon_h = 72
alert_max_per_round = 5       # 一轮推 20 条等于一条都没推

[alerts]
enabled = true
min_net_apr = 0.30            # 注意：远高于 scoring.min_net_apr
min_capacity_usd = 5000
silence_opportunity_s = 14400 # 同一个机会 4 小时内不重复推
silence_risk_s = 900

[alerts.wechat]               # PushPlus
enabled = true
token_env = "CARRYFARM_PUSHPLUS_TOKEN"

[alerts.telegram]
enabled = true
bot_token_env = "CARRYFARM_TELEGRAM_BOT_TOKEN"
chat_id_env = "CARRYFARM_TELEGRAM_CHAT_ID"

[alerts.webhook]              # style: raw / feishu / dingtalk / bark
enabled = true
style = "feishu"
url_env = "CARRYFARM_ALERT_WEBHOOK_URL"
```

几个容易踩的点：

- **`alerts.min_net_apr` 默认 30%，`scoring.min_net_apr` 默认 5%。** 两个不是一回事：
  前者是"值不值得把人从别的事上拉过来"，后者是"值不值得列在榜上"。前者标准必须更高。
- **`watch.market_interval_s` 有 180 秒硬下限。** 一轮完整刷新实测约 105 秒，
  配得更短的后果是程序永远在刷新，并且把六家的限频配额全花在自己身上。
- **所有密钥走 `*_env` 指向的环境变量，不要填字面量。** config.toml 是明文文件，
  它会被备份、被同步到网盘、被贴进聊天框。飞书/钉钉的 webhook URL 本身就是密钥。
- **告警门槛只在 `[alerts]` 里配，`[watch]` 不重复一份。** 同一个阈值放两处，
  早晚会出现"调高了却还在响"的鬼故事。

一个通道都没配也能正常跑：降级成只写本地事件表，不报错、不崩，但**手机上收不到
任何东西**——自检那行会明说这件事。静默地不发告警比配错更危险。

---

## 它不会做的事

- **不会自己开仓。** 守护模式只看榜、推历史、发告警、推进**本进程内**已有的仓位。
  开仓必须是人在界面上做的决定。
- **目前只有纸上撮合网关**（行情真实、成交模拟）。界面和守护模式都不会自作主张切实盘。
- **守护模式的仓位循环实际上是空转的。** `Trader` 还没有从数据库重建运行时状态的入口
  （`hedges` 表存的是目标和状态，逐分片的成交进度在内存里），而守护模式又从不自己开仓，
  所以它能推进的仓位数恒为 0。要补这一块需要 `Trader.load_open_hedges()`，
  属于执行层改动。自检和文档都照实说，没有假装覆盖了。
- **去重状态在内存里。** 重启后可能把一个仍然存在的机会重推一次。
- **`taskkill /F`（和 SIGKILL）仍会丢内存状态**，这个没有任何程序能兜。
- **没有进程守护。** 日志轮转有了，崩溃重启是操作系统的活（NSSM / 计划任务 / systemd）。

## 安全

API key 用主密码派生的密钥加密后存本地 SQLite，明文不落盘。配 key 时**只开
「读取」+「交易」权限，务必关掉提现，并绑定 IP 白名单**——这个工具永远不需要提现权限。

## 测试

```bash
python -m pytest tests/ -q      # 全绿为准，具体条数随功能增长
```

测试全部离线（不发任何网络请求）。其中有一条源码扫描，禁止仓库里出现任何
≥24 位十六进制串和长字面量 token。

## 代码地图

| 文件 | 干什么 |
|---|---|
| `carryfarm/public_feed.py` | 拉八家全市场资金费、配对、算成本 |
| `carryfarm/scoring.py` | 净年化 / 回本 / 稳定性 / 容量 / 判决 |
| `carryfarm/history.py` | 历史资金费回填，落 SQLite |
| `carryfarm/session.py` | 凭据库、账户连接、真实费率档位缓存 |
| `carryfarm/safety.py` | 裸腿检测、撤退状态机、熔断 |
| `carryfarm/executor.py` `trader.py` | 分片下单与收敛 |
| `carryfarm/alerts.py` | 告警：6 类事件 × 4 个通道 + 去重 |
| `carryfarm/watch.py` | 守护模式：4 条独立监督的循环 |
| `carryfarm/ui/` | NiceGUI 界面 |
| `docs/research/` | 八家交易所的接口调研 + 设计文档 |
