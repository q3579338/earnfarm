"""OKX v5 适配器（统一交易账户）。

依据 `docs/research/exchange-okx.md`（2026-08-01 实测）实现，不凭记忆写端点。

三个必须一直记着的 OKX 特性：

1. **数量单位是"张"，不是币**。coins = sz × ctVal × ctMult。BTC-USDT-SWAP 的
   ctVal=0.01 BTC，sz=1 只有 0.01 BTC（约 $1.1k）；PEPE-USDT-SWAP 一张是一千万个
   PEPE。OKX 也不像币安那样搞 1000PEPE 前缀，倍数全藏在 ctVal 里。所以本适配器在
   边界上做了硬性约定：**对外（OrderRequest.qty / Position.qty / BookTop 各档量）
   一律是 base 币数量，对内换算成张**。引擎永远不需要知道"张"这回事。
2. **签名签的是发出去的那串字节**。json.dumps 两次用不同 separators 就是两个签名。
   所以这里覆写了 `_request`：基类用 `json=` 交给 httpx 自己序列化，序列化结果和
   签名用的字符串没有任何保证能一致。
3. **资金费周期不是固定 8 小时**，且会动态升降（费率打到上下限就 8h→4h→2h→1h）。
   OKX 没有 interval 字段，只能用 nextFundingTime - fundingTime 现算，每次都要重算。

实现范围：linear 永续（`okx:perp`）+ 现货（`okx:spot`）。
反向永续（BTC-USD-SWAP）和全仓杠杆（`okx:margin`）未实现，原因见 `__init__` 与
`fetch_carry_rates` 里的注释。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, AsyncIterator, Mapping, Sequence
from urllib.parse import urlencode

import httpx

from ..models import (
    ZERO,
    BookDepth,
    BookTop,
    CarryRate,
    FeeSchedule,
    Instrument,
    MarketKind,
    Position,
    Venue,
)
from .base import (
    Credential,
    ExchangeAdapter,
    ExchangeError,
    OrderRejected,
    OrderRequest,
    OrderResult,
    OrderUnknown,
    RateLimited,
)

REST_BASE = "https://openapi.okx.com"
WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"

# tag / clOrdId 的字符集：纯字母数字，大小写敏感，长度分别 ≤16 / ≤32。
# 带横杠的 UUID 会被直接拒单，这是最容易踩的一脚。
TAG_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")

# 服务端超过 30s 没有推送就断链。计时器要小于 30s，且每收到一条消息就重置。
WS_IDLE_TIMEOUT_S = 20.0
WS_MAX_BACKOFF_S = 30.0

DEFAULT_FUNDING_INTERVAL_S = 28800

# 深度端点一次取多少档。50 档在主流品种上足够盖住 10bp 带宽；薄盘小币可能不够，
# 那时如实报 levels_used，绝不外推补足（宁可低估容量也不编数）。
# 不设更大是因为 /market/books 的响应体随档数线性变沉，而它本来就比 bbo 贵。
DEPTH_LEVELS = 50

# 调研文档点名的错误码。50011=通用限频，50061=子账户 1000 笔/2s 的下单总闸。
_RATE_LIMIT_CODES = frozenset({"50011", "50061"})
# 50102=时间戳过期（容忍窗口 30s）。这个必须重新校时后重试，不是永久失败。
_CLOCK_CODES = frozenset({"50102"})
# 50013=系统繁忙。请求已经打到 OKX 了，结果不明 —— 下单路径必须按"未知"处理。
_BUSY_CODES = frozenset({"50013"})
# 这些是**进撮合之前**就被挡下的（时间戳超窗），订单确定不存在，
# 不能因为 retriable=True 就升级成 OrderUnknown 白跑一轮 cancel+查单。
_PRE_MATCH_CODES = _CLOCK_CODES

_TIF_TO_ORD_TYPE = {
    "IOC": "ioc",
    "FOK": "fok",
    "GTC": "limit",
    "GTX": "post_only",
    "POST_ONLY": "post_only",
}

_STATE_TO_STATUS = {
    "live": "new",
    "partially_filled": "partial",
    "filled": "filled",
    "canceled": "canceled",
    "mmp_canceled": "canceled",
}


def _dumps(obj: Any) -> str:
    """全局唯一的 JSON 序列化入口。

    签名和发送必须用同一个函数、同一个 dict —— 只要中间换了 separators 或者 key
    顺序，签名就对不上（OKX 报 50113）。所有 POST body 都从这里走。
    """
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _dec(raw: Any, default: Decimal = ZERO) -> Decimal:
    """OKX 的空值是空字符串（nextFundingRate 经常是 ""），不能 Decimal("")。"""
    if raw is None or raw == "":
        return default
    return Decimal(str(raw))


def _dec_str(value: Decimal) -> str:
    """序列化成 OKX 认的字符串。

    绝不能让科学计数法漏出去：PEPE 的 tickSz 是 1e-9，body 里写 "1E-9" 直接被拒。
    """
    return format(value, "f")


def _round_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    """按步长规整，并把小数位数对齐到步长（OKX 对多余精度是硬拒绝）。"""
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=rounding)
    return (units * step).quantize(step)


class OkxAdapter(ExchangeAdapter):
    """OKX v5。返佣走下单 body 的 `tag` 字段，不是 header 也不是订单 ID 前缀。"""

    venue = Venue.OKX

    def __init__(self, credential: Credential, *, broker_code: str = "",
                 client: httpx.AsyncClient | None = None,
                 market_kind: MarketKind = MarketKind.PERP,
                 rest_base: str = REST_BASE,
                 ws_public_url: str = WS_PUBLIC,
                 td_mode: str = "cross",
                 simulated: bool = False) -> None:
        if market_kind is MarketKind.MARGIN:
            # 调研文档对全仓杠杆只字未提：借币利率端点（interest-rate-loan-quota /
            # account/interest-rate）、autoLoan 自动借币语义、计息周期、还币顺序全缺。
            # 没有利率就算不出 CarryKind.INTEREST 的现金流，硬写等于编数据。
            raise NotImplementedError(
                "okx:margin 未实现：调研文档缺借币利率端点、autoLoan 语义和计息周期，"
                "补齐 docs/research/exchange-okx.md 的杠杆部分后再开"
            )
        if market_kind is MarketKind.PERP_COIN:
            # 反向合约的 ctVal 是 USD（BTC-USD-SWAP 一张 100 USD），"数量→币"的换算
            # 语义和 linear 完全不同，混在一个类里迟早算错方向。
            raise NotImplementedError("okx:perp_coin（反向永续）未实现")
        # 用户从网页复制返佣码常带尾随空白/换行，六家统一在构造期剥掉
        broker_code = broker_code.strip()
        if broker_code and not TAG_RE.match(broker_code):
            # 配置期就炸掉，别等到下单时才发现返佣码根本发不出去
            raise ValueError(
                f"OKX 返佣码不合法: {broker_code!r}；tag 必须是 1~16 位纯字母数字"
                "（大小写敏感，不能有横杠/下划线/空格）"
            )
        super().__init__(credential, broker_code=broker_code, client=client)
        self.rest_base = rest_base.rstrip("/")
        self.ws_public_url = ws_public_url
        self.kind = market_kind
        self.market = f"{self.venue.value}:{market_kind.value}"
        self.td_mode = "cash" if market_kind is MarketKind.SPOT else td_mode
        self.simulated = simulated

        self._instruments: dict[str, Instrument] = {}
        self._min_size: dict[str, Decimal] = {}      # 最小下单量（张），OKX 没有名义额下限
        self._inst_family: dict[str, str] = {}       # position-tiers 要用
        self._funding_interval: dict[str, int] = {}  # 由 fetch_carry_rates 反哺
        # method=current_period|next_period 决定 fundingRate 对应哪一次结算，
        # CarryRate 没有这个字段，先挂在这里供上层排查用
        self._funding_method: dict[str, str] = {}

    @property
    def inst_type(self) -> str:
        return "SPOT" if self.kind is MarketKind.SPOT else "SWAP"

    # ---- 签名 -----------------------------------------------------------

    def _iso_timestamp(self) -> str:
        """OK-ACCESS-TIMESTAMP：ISO8601 UTC 带毫秒带 Z。

        WS 登录用的是 Unix 秒字符串，两者串了就是 50102/60006，别混。
        走 timestamp_ms() 拿已校准的时钟——容忍窗口只有 30s。
        """
        sec, msec = divmod(self.timestamp_ms(), 1000)
        dt = datetime.fromtimestamp(sec, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{msec:03d}Z"

    def _prepare(self, method: str, path: str, params: dict, body: dict,
                 signed: bool) -> tuple[dict[str, str], str, dict, dict]:
        method = method.upper()
        # 查询串必须原样进 prehash，所以在这里就拼进 URL，返回空 params 防止 httpx 再拼一遍。
        # safe="," 是因为 instId 支持逗号分隔列表，转义成 %2C 后签名和实际请求还是一致的，
        # 但可读性差且部分网关会二次解码，直接留原样最稳。
        query = urlencode(params, safe=",") if params else ""
        request_path = f"{path}?{query}" if query else path
        body_str = _dumps(body) if body else ""

        headers = {"Content-Type": "application/json"}
        if self.simulated:
            headers["x-simulated-trading"] = "1"
        if signed:
            ts = self._iso_timestamp()
            headers["OK-ACCESS-KEY"] = self._cred.api_key
            headers["OK-ACCESS-SIGN"] = self._hmac_sha256_b64(
                ts + method + request_path + body_str
            )
            headers["OK-ACCESS-TIMESTAMP"] = ts
            headers["OK-ACCESS-PASSPHRASE"] = self._cred.passphrase
        return headers, self.rest_base + request_path, {}, dict(body)

    async def _request(self, method: str, path: str, *, quota: str = "market",
                       signed: bool = False, params: Mapping[str, Any] | None = None,
                       body: Mapping[str, Any] | None = None,
                       extra_headers: Mapping[str, str] | None = None) -> Any:
        """覆写基类：OKX 必须签名"发出去的那串字节"。

        基类走 `json=body` 让 httpx 自己序列化，序列化结果和 `_prepare` 里签名用的
        字符串没有任何保证一致（separators、ensure_ascii、key 顺序任一不同就废）。
        这里改成 `content=`，且两处都调用同一个 `_dumps` 作用在同一个 dict 上。
        """
        if self._clock.is_stale:
            try:
                await self.sync_clock()
            except Exception:
                pass          # 校时失败不阻塞业务，drift 会体现在 clock_drift_ms 上

        async with self._quota.get(quota, self._quota["market"]):
            headers, url, _, send_body = self._prepare(
                method, path, dict(params or {}), dict(body or {}), signed
            )
            if extra_headers:
                headers = {**headers, **extra_headers}
            content = (_dumps(send_body).encode()
                       if send_body and method.upper() != "GET" else None)
            try:
                resp = await self._client.request(method, url, content=content,
                                                  headers=headers)
            except httpx.HTTPError as exc:
                # 超时、连接中断、服务端半途断流全在这里。code 固定 NETWORK，
                # place_order 靠它翻译成 OrderUnknown。
                raise ExchangeError(self.venue.value, "NETWORK", str(exc),
                                    retriable=True) from exc
            return self._parse(resp)

    def _parse(self, resp: httpx.Response) -> Any:
        if resp.status_code == 429:
            raise RateLimited(self.venue.value, "HTTP_429", resp.text, retriable=True)
        if resp.status_code >= 500:
            # 5xx 说明请求打到了服务端但结果不明，下单路径要按"未知"处理
            raise ExchangeError(self.venue.value, f"HTTP_{resp.status_code}",
                                resp.text[:200], retriable=True)
        try:
            # parse_float=Decimal：OKX v5 目前所有数值都是字符串，但这道闸不能靠
            # 上游习惯撑着 —— 任何一个裸数字漏进来就是一次静默的精度损失
            payload = json.loads(resp.text, parse_float=Decimal) if resp.text else {}
        except ValueError as exc:
            # 响应体读不出来（WAF/代理注入 HTML）= 这单有没有进撮合完全不知道。
            # 统一成 BAD_JSON + retriable，由 place_order 翻成 OrderUnknown。
            raise ExchangeError(self.venue.value, "BAD_JSON",
                                f"响应不是 JSON: {resp.text[:200]}", retriable=True) from exc

        code = str(payload.get("code", ""))
        data = payload.get("data")
        if code != "0":
            detail_code, detail_msg = code, str(payload.get("msg", ""))
            # 批量/单笔下单失败时顶层 code 是 "1"，真正的原因在 data[0].sCode，
            # 只看顶层会把"余额不足"和"参数错误"混成同一个错。
            if isinstance(data, list) and data and isinstance(data[0], dict):
                s_code = str(data[0].get("sCode", "") or "")
                if s_code and s_code != "0":
                    detail_code = s_code
                    detail_msg = str(data[0].get("sMsg") or detail_msg)
            raise self._translate(detail_code, detail_msg)
        return data if data is not None else []

    def _translate(self, code: str, message: str) -> ExchangeError:
        if code in _RATE_LIMIT_CODES:
            return RateLimited(self.venue.value, code, message, retriable=True)
        if code in _CLOCK_CODES:
            self._clock.synced_at = 0.0     # 逼下一次请求重新校时
            return ExchangeError(self.venue.value, code, message, retriable=True)
        if code in _BUSY_CODES:
            # 系统繁忙：请求到了服务端但结果不明，下单路径靠 retriable 升级成 OrderUnknown
            return ExchangeError(self.venue.value, code, message, retriable=True)
        # OKX 的 5xxxx 里 51xxx 是交易域（下单/撤单/查单被拒，含"订单不存在"51603）。
        # 调研文档只逐个点名了 50011/50061/50102/60012，这条前缀规则是结构性推断。
        if code.startswith("51"):
            return OrderRejected(self.venue.value, code, message, retriable=False)
        return ExchangeError(self.venue.value, code, message, retriable=False)

    # ---- 元数据 ----------------------------------------------------------

    async def fetch_server_time_ms(self) -> int:
        """不走 `_request`：那里会先检查时钟是否过期并回调 sync_clock，
        而 sync_clock 又调本方法，直接无限递归。这里手动占配额、直连。"""
        async with self._quota["market"]:
            try:
                resp = await self._client.get(self.rest_base + "/api/v5/public/time")
            except httpx.HTTPError as exc:
                raise ExchangeError(self.venue.value, "NETWORK", str(exc),
                                    retriable=True) from exc
            data = self._parse(resp)
        return int(data[0]["ts"])

    async def fetch_instruments(self) -> Sequence[Instrument]:
        data = await self._request("GET", "/api/v5/public/instruments",
                                   quota="market",
                                   params={"instType": self.inst_type})
        out: list[Instrument] = []
        for d in data:
            if d.get("state") != "live":
                continue
            if d.get("ruleType") == "pre_market":
                continue        # 盘前合约撮合规则不同，不参与套利
            if self.kind is MarketKind.PERP and d.get("ctType") != "linear":
                continue        # 反向合约的 ctVal 是 USD，单位语义不同，单独适配器再说
            inst = self._parse_instrument(d)
            out.append(inst)
            self._instruments[inst.symbol] = inst
            # minSz 和 lotSz 都是**张**，_min_size 是 place_order 内部用的，保持张口径
            self._min_size[inst.symbol] = _dec(d.get("minSz"),
                                               _dec(d.get("lotSz"), Decimal("1")))
            family = d.get("instFamily") or d.get("uly") or ""
            if family:
                self._inst_family[inst.symbol] = family
        return tuple(out)

    def _parse_instrument(self, d: Mapping[str, Any]) -> Instrument:
        symbol = d["instId"]
        if self.kind is MarketKind.SPOT:
            base, quote = d.get("baseCcy", ""), d.get("quoteCcy", "")
            contract_size = Decimal("1")
        else:
            # instFamily/uly 形如 "BTC-USDT"，比拿 ctValCcy/settleCcy 拼稳
            family = d.get("instFamily") or d.get("uly") or ""
            parts = family.split("-")
            base = parts[0] if parts else ""
            quote = parts[1] if len(parts) > 1 else d.get("settleCcy", "")
            contract_size = _dec(d.get("ctVal"), Decimal("1")) * _dec(d.get("ctMult"), Decimal("1"))
        return Instrument(
            market=self.market,
            symbol=symbol,
            base=base,
            quote=quote,
            tick_size=_dec(d.get("tickSz"), Decimal("1")),
            # lotSz 是**张**，而对外的数量口径是币 —— 必须乘 ctVal×ctMult 折成币，
            # 否则上层拿"张粒度"去给"币数量"取整：BTC 的 0.05 币缺口会被 floor 成 0
            # （报出来的粒度 0.1 其实是 0.001 BTC），这条腿永远补不平。
            lot_size=_dec(d.get("lotSz"), Decimal("1")) * contract_size,
            # OKX 不提供 quote 计价的最小名义额，下限是"张数"（minSz），
            # 折成 USD 需要标记价，而 /public/instruments 不返回标记价。
            # 这里如实填 0，最小张数校验在 place_order 里用 _min_size 本地拦截。
            min_notional=ZERO,
            contract_size=contract_size,
            max_leverage=_dec(d.get("lever"), Decimal("1")),
            # 没有 interval 字段，只能由 fetch_carry_rates 用
            # nextFundingTime-fundingTime 现算后回填；这里给默认 8h。
            funding_interval_s=self._funding_interval.get(symbol, DEFAULT_FUNDING_INTERVAL_S),
        )

    async def _instrument(self, symbol: str) -> Instrument:
        if symbol not in self._instruments:
            await self.fetch_instruments()
        inst = self._instruments.get(symbol)
        if inst is None:
            raise ExchangeError(self.venue.value, "UNKNOWN_INSTRUMENT",
                                f"{symbol} 不在 {self.inst_type} 的可交易列表里")
        return inst

    async def _contract_size(self, symbol: str) -> Decimal:
        """仓位路径专用的 ctVal×ctMult。

        合约下架/暂停后 state 就不是 live，会被 fetch_instruments 过滤掉，但**仓位
        还在**。持仓是真值，不能因为查不到规格就让整个 fetch_positions 报错，
        所以这里对缺失的品种单独按 instId 再查一次。
        """
        inst = self._instruments.get(symbol)
        if inst is not None:
            return inst.contract_size
        data = await self._request("GET", "/api/v5/public/instruments", quota="risk",
                                   params={"instType": "SWAP", "instId": symbol})
        if not data:
            raise ExchangeError(self.venue.value, "UNKNOWN_INSTRUMENT",
                                f"{symbol} 查不到 ctVal，无法把张数折成币")
        return (_dec(data[0].get("ctVal"), Decimal("1"))
                * _dec(data[0].get("ctMult"), Decimal("1")))

    async def fetch_fee_schedule(self, symbols: Sequence[str]) -> Sequence[FeeSchedule]:
        """按品种返回实际费率档位，每条都带 symbol（调用方按 symbol 匹配）。

        OKX 一次调用返回整个 instType 的档位（**账户级**，不分品种），但 linear 和
        inverse 用的是不同字段（takerU/makerU vs taker/maker），所以仍要逐个 symbol
        判断该读哪一对。不传 symbols 时返回一条 symbol="" 的账户级档位。

        符号约定：OKX 用负数表示"你付"（"-0.0005" = 5bp taker），这里统一翻转成
        **正数=成本**，与 scoring.py 把 taker_fee 直接加进成本的用法一致；
        maker 返佣会因此变成负数，正好当作收入。

        OKB 抵扣：OKX 早就把它并进了 level（响应里的 "Lv1"/"VIP2"），没有独立的
        折扣开关可读，所以这个返回值已经是最终档位，不需要也无法再打折。
        """
        data = await self._request("GET", "/api/v5/account/trade-fee", quota="market",
                                   signed=True, params={"instType": self.inst_type})
        row = data[0] if data else {}
        level = str(row.get("level") or "")
        note = f"OKX 档位 {level}" if level else ""
        out: list[FeeSchedule] = []
        for symbol in (list(symbols) or [""]):
            if self.kind is MarketKind.SPOT:
                maker_raw, taker_raw = row.get("maker"), row.get("taker")
            elif self._is_linear(symbol):
                maker_raw, taker_raw = row.get("makerU"), row.get("takerU")
            else:
                maker_raw, taker_raw = row.get("maker"), row.get("taker")
            # 响应空/字段缺失时**跳过**，不补零：零手续费会让四笔 taker 成本
            # 凭空消失，把负期望的机会翻成正期望，还一声不吭
            if maker_raw in (None, "") or taker_raw in (None, ""):
                continue
            out.append(FeeSchedule(
                market=self.market, symbol=symbol,
                maker=-_dec(maker_raw),
                taker=-_dec(taker_raw),
                note=note,
            ))
        return tuple(out)

    def _is_linear(self, symbol: str) -> bool:
        inst = self._instruments.get(symbol)
        if inst is not None:
            return inst.quote in ("USDT", "USDC")
        return "-USDT-" in symbol or "-USDC-" in symbol

    async def fetch_funding_limits(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        """返回 **(cap, floor)** —— 按基类 docstring 的 "cap / floor" 顺序。

        OKX 的上下限是逐品种的，不是全所统一：BTC ±0.375%、DOGE ±0.75%、TRB ±1%。
        看到 -2% 的"机会"先拿这个夹一遍，多半是脏数据。
        """
        if self.kind is not MarketKind.PERP:
            return None
        data = await self._request("GET", "/api/v5/public/funding-rate", quota="market",
                                   params={"instId": symbol})
        if not data:
            return None
        d = data[0]
        cap, floor = d.get("maxFundingRate"), d.get("minFundingRate")
        if cap in (None, "") or floor in (None, ""):
            return None
        return _dec(cap), _dec(floor)

    # ---- 行情 ------------------------------------------------------------

    async def fetch_book_top(self, symbol: str) -> BookTop:
        """一档盘口。

        TODO(调研缺口)：调研文档只写了 WS 的 bbo-tbt 通道，没有覆盖 REST 的
        /api/v5/market/books。这里按同样的档位结构 [价, 量, nonRpiQty, 订单数] 解析
        （该结构在文档的 WS 段是明确写了的），实盘接入前建议再核一次响应字段。
        """
        inst = await self._instrument(symbol)
        data = await self._request("GET", "/api/v5/market/books", quota="market",
                                   params={"instId": symbol, "sz": "1"})
        if not data:
            raise ExchangeError(self.venue.value, "EMPTY_BOOK", symbol, retriable=True)
        book = data[0]
        bids, asks = book.get("bids") or [], book.get("asks") or []
        if not bids or not asks:
            raise ExchangeError(self.venue.value, "EMPTY_BOOK", symbol, retriable=True)
        return BookTop(
            bid_price=_dec(bids[0][0]),
            bid_qty=_dec(bids[0][1]) * inst.contract_size,   # 张 → 币
            ask_price=_dec(asks[0][0]),
            ask_qty=_dec(asks[0][1]) * inst.contract_size,
            ts_ms=int(book.get("ts") or self.timestamp_ms()),
        )

    async def fetch_book_depth(self, symbol: str, band_bp: float = 10.0) -> BookDepth:
        """band_bp 基点带宽内的可见深度（名义额，quote 计价）。

        以中价为基准，买卖两侧各统计 [mid*(1-band), mid] 和 [mid, mid*(1+band)]
        内的累计名义额。用一档当深度代理会把容量低估一两个数量级——小币一档常常
        只有几百刀，10bp 内却有几万，机会因此被误判成"做不了"。

        端点：GET /api/v5/market/books?instId=..&sz=50（公开，无需签名），和
        fetch_book_top 是同一个，只把 sz 从 1 提到 DEPTH_LEVELS。

        限频权重：走 quota="market"。深度接口比 bbo/bookTicker 贵得多，OKX v5 文档
        给 /market/books 的是 **40 次/2s（按 IP）**，档数更深的 /market/books-full
        只有 10 次/2s —— 所以扫全市场时别一轮全打，配额要和行情轮询共用那个信号量，
        绝不能挤占 risk/trade 的池子。
        TODO(调研缺口)：docs/research/exchange-okx.md 只写了 WS 的 bbo-tbt，整个
        REST /market/books（含 fetch_book_top 用的那次）都没覆盖，上面的限频数字和
        sz 上限 400 来自 OKX v5 文档而非本项目实测，接实盘前核一次。

        档位不够时不外推：只统计真的返回了的档，levels_used 如实报两侧合计档数，
        等于 2×DEPTH_LEVELS 就说明带宽还没走完档位就用光了，读数是**下界**。
        """
        if band_bp <= 0:
            # 带宽非正 → 带内一档都统计不到，返回 0 会被 scoring.slippage_rate 当成
            # "没有深度数据"而静默退回半价差模型：配置错了却看不出来。入口直接炸。
            raise ValueError(f"band_bp 必须为正基点数，收到 {band_bp}")
        inst = await self._instrument(symbol)
        data = await self._request("GET", "/api/v5/market/books", quota="market",
                                   params={"instId": symbol, "sz": str(DEPTH_LEVELS)})
        if not data:
            raise ExchangeError(self.venue.value, "EMPTY_BOOK", symbol, retriable=True)
        book = data[0]
        bids, asks = book.get("bids") or [], book.get("asks") or []
        if not bids or not asks:
            # 单边空盘无法定中价，宁可让上层重试也不要拿一侧价格冒充 mid
            raise ExchangeError(self.venue.value, "EMPTY_BOOK", symbol, retriable=True)

        bid_price, ask_price = _dec(bids[0][0]), _dec(asks[0][0])
        mid = (bid_price + ask_price) / 2
        # band_bp 是 float，经字符串再进 Decimal：直接 Decimal(0.1) 会把二进制浮点的
        # 尾巴带进边界价，恰好压在边界上的那一档算不算就成了掷骰子。
        band = Decimal(str(band_bp)) / Decimal("10000")
        bid_notional, bid_used = self._walk_depth(bids, inst.contract_size,
                                                  mid * (1 - band), is_bid=True)
        ask_notional, ask_used = self._walk_depth(asks, inst.contract_size,
                                                  mid * (1 + band), is_bid=False)
        return BookDepth(
            symbol=symbol,
            bid_notional=bid_notional,
            ask_notional=ask_notional,
            bid_price=bid_price,
            bid_qty=_dec(bids[0][1]) * inst.contract_size,   # 张 → 币
            ask_price=ask_price,
            ask_qty=_dec(asks[0][1]) * inst.contract_size,
            levels_used=bid_used + ask_used,
            band_bp=band_bp,
            ts_ms=int(book.get("ts") or self.timestamp_ms()),
        )

    @staticmethod
    def _walk_depth(levels: Sequence[Sequence[Any]], contract_size: Decimal,
                    bound: Decimal, *, is_bid: bool) -> tuple[Decimal, int]:
        """沿一侧累加名义额到边界为止，返回 (累计名义额, 计入档数)。

        档位本身有序（bids 高→低、asks 低→高），所以第一个越界的档之后不可能再有
        带内的档，直接 break —— 顺带保证脏数据里的乱序档不会被"捡回来"多算容量。
        边界闭区间：恰好等于边界价的那一档算带内。

        每档是 [价, 量(**张**), nonRpiQty, 订单数]。量取 index 1（可吃的总量），
        index 2 是剔掉 RPI 之后的量，拿它当可吃量会系统性低估深度。
        名义额 = 价 × 张 × ctVal×ctMult：张数当币数用，PEPE 会差七个数量级。
        """
        total = ZERO
        used = 0
        for level in levels:
            price = _dec(level[0])
            if (price < bound) if is_bid else (price > bound):
                break
            total += price * _dec(level[1]) * contract_size
            used += 1
        return total, used

    async def fetch_carry_rates(self, symbols: Sequence[str] | None = None) -> Sequence[CarryRate]:
        """当前资金费。

        符号：**原样存交易所约定**（正 = 多头付给空头），不取负。
        注意基类那句"适配器要取负"的 docstring 与 models.CarryRate 的约定相反，
        以 models.CarryRate 为准——上层统一用 `short_rate - long_rate` 算净收益。

        OKX 没有批量端点（instType=SWAP 单独用、instId 逗号列表都是 HTTP 400），
        只能一个品种一个请求地扇出；扫全市场时应该改用 WS 的 funding-rate 通道。
        """
        if self.kind is not MarketKind.PERP:
            return ()       # 现货没有 carry（CarryKind.ZERO）
        if symbols is None:
            if not self._instruments:
                await self.fetch_instruments()
            symbols = tuple(self._instruments)
        # 单个品种失败（限频/下架/脏数据）只丢那一个，不拖垮整轮扫描；
        # 并发上限由基类的 market 配额信号量兜住。
        results = await asyncio.gather(
            *(self._fetch_one_carry(s) for s in symbols), return_exceptions=True
        )
        return tuple(r for r in results if isinstance(r, CarryRate))

    async def _fetch_one_carry(self, symbol: str) -> CarryRate:
        data = await self._request("GET", "/api/v5/public/funding-rate", quota="market",
                                   params={"instId": symbol})
        d = data[0]
        funding_ms = int(d.get("fundingTime") or 0)
        next_ms = int(d.get("nextFundingTime") or 0)
        # 周期只能现算，而且会动态变（打到上下限就 8h→4h→2h→1h，回落后再变回去），
        # 所以每次都重算并回填，绝不能缓存成常量。
        interval_s = ((next_ms - funding_ms) // 1000
                      if next_ms > funding_ms
                      else self._funding_interval.get(symbol, DEFAULT_FUNDING_INTERVAL_S))
        self._funding_interval[symbol] = interval_s
        self._funding_method[symbol] = str(d.get("method") or "")
        return CarryRate(
            market=self.market,
            symbol=symbol,
            rate=_dec(d.get("fundingRate")),
            interval_s=interval_s,
            next_settle_ms=funding_ms,      # fundingTime 是"下一次"结算（> ts）
            ts_ms=int(d.get("ts") or self.timestamp_ms()),
        )

    async def fetch_funding_history(self, symbol: str, since_ms: int,
                                    limit: int = 1000) -> Sequence[tuple[int, Decimal]]:
        """历史资金费，升序返回。

        OKX 单页上限 100 条、倒序（新→旧），用 after=上一页最旧的 fundingTime 翻页。
        实测**只有约 3 个月**深度（BTC 2026-08-01 最老回到 2026-04-30），
        再往前一律返回空数组——任何超过 3 个月的回测必须自己落库。
        用 realizedRate（实际结算的）而不是 fundingRate（公告的）。
        """
        if self.kind is not MarketKind.PERP:
            return ()
        out: list[tuple[int, Decimal]] = []
        cursor: int | None = None
        while len(out) < limit:
            params: dict[str, Any] = {"instId": symbol, "limit": "100"}
            if cursor is not None:
                params["after"] = str(cursor)
            page = await self._request("GET", "/api/v5/public/funding-rate-history",
                                       quota="market", params=params)
            if not page:
                break
            stop = False
            for d in page:
                ts = int(d["fundingTime"])
                if ts < since_ms:
                    stop = True
                    continue
                out.append((ts, _dec(d.get("realizedRate") or d.get("fundingRate"))))
            cursor = int(page[-1]["fundingTime"])
            if stop or len(page) < 100:
                break
        out.sort(key=lambda x: x[0])
        return tuple(out[-limit:])

    async def stream_book_top(self, symbols: Sequence[str]) -> AsyncIterator[BookTop]:
        """bbo-tbt 逐笔一档流（最小可用版：订阅 + 心跳 + 重连 + 序号断链检测）。

        心跳必须发**字面文本帧 "ping"**，协议级 WebSocket ping 不算数（服务端 30s
        没推送就断）。订阅操作每连接每小时只有 480 次配额，所以一次性批量订阅、
        长期保持，绝不能在循环里反复订退。

        TODO: BookTop 模型里没有 symbol 字段，多品种订阅时无法区分来源。
              现阶段建议一个 symbol 一条流；模型加上 symbol 后改成单连接多路复用。
        TODO: seqId 断链目前整体重连（会重新消耗一次订阅配额），更省的做法是只对
              出问题的 instId 发一次 unsubscribe+subscribe。
        TODO: 私有频道（orders/positions）和 WS 下单未实现，走 REST。
        """
        import websockets   # 懒加载：只跑 REST 的部署不该被这个依赖绑住

        for symbol in symbols:
            await self._instrument(symbol)      # 预热 ctVal，张→币要用
        args = [{"channel": "bbo-tbt", "instId": s} for s in symbols]
        backoff = 1.0
        while True:
            try:
                # ping_interval=None：OKX 要的是文本帧 "ping"，让库自己发协议 ping 没用
                async with websockets.connect(self.ws_public_url,
                                              ping_interval=None) as ws:
                    await ws.send(_dumps({"op": "subscribe", "args": args}))
                    backoff = 1.0
                    last_seq: dict[str, int] = {}
                    while True:
                        raw = await self._ws_recv(ws)
                        if raw is None:
                            break                    # 心跳没等到 pong，重连
                        if raw == "pong":
                            continue
                        msg = json.loads(raw, parse_float=Decimal)
                        if msg.get("event") == "error":
                            raise ExchangeError(self.venue.value,
                                                str(msg.get("code", "WS")),
                                                str(msg.get("msg", "")), retriable=True)
                        if msg.get("arg", {}).get("channel") != "bbo-tbt":
                            continue
                        inst_id = msg["arg"]["instId"]
                        gap = False
                        for d in msg.get("data") or []:
                            prev = d.get("prevSeqId")
                            seq = d.get("seqId")
                            if (prev is not None and inst_id in last_seq
                                    and last_seq[inst_id] != prev):
                                gap = True           # checksum 已废弃恒为 0，只能靠序号链
                                break
                            if seq is not None:
                                last_seq[inst_id] = seq
                            top = self._book_from_ws(inst_id, d)
                            if top is not None:
                                yield top
                        if gap:
                            break
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, WS_MAX_BACKOFF_S)

    async def _ws_recv(self, ws: Any) -> str | None:
        """收一条；空闲超时就发字面 "ping"，再等不到就判死。"""
        try:
            return await asyncio.wait_for(ws.recv(), timeout=WS_IDLE_TIMEOUT_S)
        except asyncio.TimeoutError:
            pass
        await ws.send("ping")
        try:
            return await asyncio.wait_for(ws.recv(), timeout=WS_IDLE_TIMEOUT_S)
        except asyncio.TimeoutError:
            return None

    def _book_from_ws(self, symbol: str, d: Mapping[str, Any]) -> BookTop | None:
        inst = self._instruments.get(symbol)
        if inst is None:
            return None
        bids, asks = d.get("bids") or [], d.get("asks") or []
        if not bids or not asks:
            return None
        return BookTop(
            bid_price=_dec(bids[0][0]),
            bid_qty=_dec(bids[0][1]) * inst.contract_size,
            ask_price=_dec(asks[0][0]),
            ask_qty=_dec(asks[0][1]) * inst.contract_size,
            ts_ms=int(d.get("ts") or self.timestamp_ms()),
        )

    # ---- 交易 ------------------------------------------------------------

    def _client_id(self, raw: str) -> str:
        """规整 clOrdId：纯字母数字、≤32 位。

        uuid4() 带横杠会被直接拒单，所以只剥掉 `-` 和 `_`（uuid4().hex 正好 32 位）。
        其他非法字符不静默改写——改了之后调用方再拿原 ID 查单就查不到了，宁可炸。
        """
        candidate = raw.replace("-", "").replace("_", "")
        if not candidate:
            return uuid.uuid4().hex
        if not CLIENT_ID_RE.match(candidate):
            raise ExchangeError(
                self.venue.value, "BAD_CLIENT_ID",
                f"clOrdId {raw!r} 不合法：必须是 ≤32 位纯字母数字（去掉 -/_ 后仍不合规）"
            )
        return candidate

    async def place_order(self, req: OrderRequest) -> OrderResult:
        """下单。

        - qty 是 **base 币数量**，这里换算成张（coins / ctVal / ctMult）再按 lotSz 向零取整
        - 价格按 tickSz 规整：买单 ROUND_DOWN、卖单 ROUND_UP（宁可少一个 tick 的价格
          优势，也不要因为多余精度被拒单）
        - 返佣码走 body 的 `tag`，为空时**整个字段都不出现**（不发 tag:""）
        - 超时/断流/5xx 一律抛 OrderUnknown，绝不当失败返回
        """
        inst = await self._instrument(req.symbol)
        cl_ord_id = self._client_id(req.client_order_id)

        # Instrument.lot_size 是**币**口径，这里取整的是**张**，要先折回 lotSz
        lot_contracts = inst.lot_size / inst.contract_size
        contracts = _round_step(req.qty / inst.contract_size, lot_contracts, ROUND_DOWN)
        if contracts <= 0:
            raise OrderRejected(self.venue.value, "PINCHED",
                                f"{req.symbol} 数量 {req.qty} 币按 lotSz={lot_contracts} "
                                "取整后为零")
        min_sz = self._min_size.get(req.symbol)
        if min_sz is not None and contracts < min_sz:
            raise OrderRejected(self.venue.value, "BELOW_MIN_SIZE",
                                f"{req.symbol} {contracts} 张 < minSz {min_sz} 张")

        body: dict[str, Any] = {
            "instId": req.symbol,
            "tdMode": self.td_mode,
            "side": req.side.lower(),
            "ordType": self._ord_type(req),
            "sz": _dec_str(contracts),
        }
        if req.price is not None:
            # 方向与 executor.cross_price 一致：买单向上、卖单向下。保护限价是故意
            # 穿透对手价的，反向取整会把穿透削回来 —— PEPE 那种 tick 相对价格很粗的
            # 品种上，那不是少赚一个 tick，而是 IOC 根本挂不上、这条腿永远补不平。
            rounding = ROUND_UP if req.side.lower() == "buy" else ROUND_DOWN
            body["px"] = _dec_str(_round_step(req.price, inst.tick_size, rounding))
        if self.kind is MarketKind.PERP:
            # 强制 net_mode：双腿对冲的收敛算术只有在"一个有符号数"下才成立。
            # long_short_mode 下 posSide 必填且 reduceOnly 不是平仓机制。
            body["posSide"] = "net"
            if req.reduce_only:
                body["reduceOnly"] = True
        elif self.kind is MarketKind.SPOT and body["ordType"] == "market":
            # 现货市价单默认 sz 是计价币数量，必须显式声明按 base 计
            body["tgtCcy"] = "base_ccy"
        body["clOrdId"] = cl_ord_id
        if self.broker_code:
            body["tag"] = self.broker_code      # 空码时连 key 都不加

        try:
            data = await self._request("POST", "/api/v5/trade/order", quota="trade",
                                       signed=True, body=body)
        except OrderRejected:
            raise                 # 明确被拒 = 撮合肯定没收到，不必白跑一轮消解
        except RateLimited:
            raise                 # 被限频 = 明确没进撮合，安全重试
        except ExchangeError as exc:
            if (exc.code == "NETWORK" or exc.code == "BAD_JSON"
                    or exc.code.startswith("HTTP_5")
                    or (exc.retriable and exc.code not in _PRE_MATCH_CODES)):
                # 请求可能已经落到撮合了。直接重发 = 双倍仓位，必须交给
                # resolve_unknown_order 走 cancel→查单→仓位差分。
                raise OrderUnknown(self.venue.value, req.client_order_id or cl_ord_id) from exc
            raise

        row = data[0] if data else {}
        # Ack ≠ 成交：下单响应只表示 OKX 收下了请求，终态要靠 query_order
        # 或私有 orders 频道确认。
        return OrderResult(
            client_order_id=req.client_order_id or cl_ord_id,
            exchange_order_id=str(row.get("ordId", "")),
            status="new",
            filled_qty=ZERO,
            avg_price=ZERO,
        )

    def _ord_type(self, req: OrderRequest) -> str:
        if req.price is None:
            # 薄盘上 optimal_limit_ioc（按最优价 IOC）比 market 更安全，
            # 需要时在调用方显式传 price=None + tif 扩展，这里保持最朴素的 market。
            return "market"
        return _TIF_TO_ORD_TYPE.get(req.time_in_force.upper(), "limit")

    async def cancel_order(self, symbol: str, client_order_id: str) -> None:
        """撤单——状态终结操作。撤完订单只可能是 canceled 或 filled。

        "订单不存在/已终结"（51xxx）会被翻译成 OrderRejected，
        基类的 resolve_unknown_order 正是靠它继续往下查单的。
        """
        try:
            await self._request("POST", "/api/v5/trade/cancel-order", quota="risk",
                                signed=True,
                                body={"instId": symbol,
                                      "clOrdId": self._client_id(client_order_id)})
        except ExchangeError as exc:
            raise self._risk_path_error(exc) from exc

    def _risk_path_error(self, exc: ExchangeError) -> ExchangeError:
        """风控路径（撤单/查单）上的错误重分类。

        本适配器的 _request 把**所有** httpx 异常收敛成 ExchangeError('NETWORK')，
        而 base.resolve_unknown_order 只认 (OrderRejected, httpx.HTTPError, RateLimited)
        —— 不转一手的话，网络抖动或 5xx 会让消解流程在最危险的时刻带着未捕获异常崩掉。
        可重试的挂到 RateLimited（code 保留，日志仍看得出真实原因）；不可重试的原样
        上抛，绝不降级成 OrderRejected —— 那等于谎报"成交量为 0"。
        """
        if isinstance(exc, (OrderRejected, RateLimited)) or not exc.retriable:
            return exc
        return RateLimited(self.venue.value, exc.code, exc.message, retriable=True)

    async def query_order(self, symbol: str, client_order_id: str) -> OrderResult:
        """单笔订单详情。

        TODO(调研缺口)：文档写了下单/撤单/改单，没有单独写 GET /api/v5/trade/order
        的字段表。这里按 v5 通用订单对象解析（state/accFillSz/avgPx/fee/feeCcy），
        实盘前建议核一次。
        """
        inst = await self._instrument(symbol)
        try:
            data = await self._request("GET", "/api/v5/trade/order", quota="risk",
                                       signed=True,
                                       params={"instId": symbol,
                                               "clOrdId": self._client_id(client_order_id)})
        except ExchangeError as exc:
            raise self._risk_path_error(exc) from exc
        row = data[0] if data else {}
        return self._order_result(row, inst, fallback_client_id=client_order_id)

    def _order_result(self, row: Mapping[str, Any], inst: Instrument,
                      fallback_client_id: str = "") -> OrderResult:
        return OrderResult(
            client_order_id=fallback_client_id or str(row.get("clOrdId", "")),
            exchange_order_id=str(row.get("ordId", "")),
            status=_STATE_TO_STATUS.get(str(row.get("state", "")), "new"),
            filled_qty=_dec(row.get("accFillSz")) * inst.contract_size,   # 张 → 币
            avg_price=_dec(row.get("avgPx")),
            # 与 FeeSchedule 同一约定：OKX 用负数表示"你付"，翻正成本
            fee=-_dec(row.get("fee")),
            fee_asset=str(row.get("feeCcy", "")),
        )

    async def fetch_open_orders(self, symbol: str) -> Sequence[OrderResult]:
        """未成交挂单。"A 腿成交、B 腿还挂着"是限价模式下最隐蔽的单腿。

        TODO(调研缺口)：/api/v5/trade/orders-pending 未在调研文档中列出，
        字段按通用订单对象解析。
        """
        inst = await self._instrument(symbol)
        data = await self._request("GET", "/api/v5/trade/orders-pending", quota="risk",
                                   signed=True,
                                   params={"instType": self.inst_type, "instId": symbol})
        return tuple(self._order_result(row, inst) for row in data)

    async def fetch_positions(self, symbols: Sequence[str] | None = None) -> Sequence[Position]:
        """持仓 —— 真值。本地记账与它冲突时无条件以它为准。"""
        if self.kind is MarketKind.SPOT:
            return await self._spot_positions(symbols)
        params: dict[str, Any] = {"instType": "SWAP"}
        if symbols:
            params["instId"] = ",".join(symbols)     # 这个端点允许逗号列表
        data = await self._request("GET", "/api/v5/account/positions", quota="risk",
                                   signed=True, params=params)
        if data and not self._instruments:
            await self.fetch_instruments()
        out: list[Position] = []
        for row in data:
            symbol = row.get("instId", "")
            ct_val = await self._contract_size(symbol)
            contracts = _dec(row.get("pos"))
            # net_mode 下 pos 自带符号；long_short_mode 会给两行正数，靠 posSide 定符号
            if row.get("posSide") == "short" and contracts > 0:
                contracts = -contracts
            liq = row.get("liqPx")
            out.append(Position(
                market=self.market,
                symbol=symbol,
                qty=contracts * ct_val,                  # 张 → 币
                entry_price=_dec(row.get("avgPx")),
                mark_price=_dec(row.get("markPx")),
                liquidation_price=_dec(liq) if liq not in (None, "") else None,
                leverage=_dec(row.get("lever"), Decimal("1")),
            ))
        return tuple(out)

    async def _spot_positions(self, symbols: Sequence[str] | None) -> Sequence[Position]:
        """现货没有"仓位"，用币种余额折算。

        mark/entry 填 0：/account/balance 不返回价格，而调研文档没有给出现货标记价的
        来源端点。**依赖 mark_price 的风控（爆仓距离等）不能用在现货腿上**；
        qty 是准的，delta 收敛只看它。
        """
        data = await self._request("GET", "/api/v5/account/balance", quota="risk",
                                   signed=True)
        details = (data[0].get("details") or []) if data else []
        by_ccy = {d.get("ccy"): d for d in details}
        # 传了 symbols 就按 "BTC-USDT" 取 base 币，没传就把每个有余额的币都当一条
        wanted = ({s: s.split("-")[0] for s in symbols} if symbols
                  else {str(d.get("ccy")): str(d.get("ccy")) for d in details})
        out: list[Position] = []
        for symbol, ccy in wanted.items():
            row = by_ccy.get(ccy)
            if row is None:
                continue
            out.append(Position(
                market=self.market,
                symbol=symbol,
                qty=_dec(row.get("cashBal") or row.get("eq")),
                entry_price=ZERO,
                mark_price=ZERO,
                liquidation_price=None,
                leverage=Decimal("1"),
            ))
        return tuple(out)

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        if self.kind is not MarketKind.PERP:
            raise NotImplementedError("okx:spot 没有杠杆设置")
        await self._request("POST", "/api/v5/account/set-leverage", quota="risk",
                            signed=True,
                            body={"instId": symbol,
                                  "lever": _dec_str(leverage),
                                  "mgnMode": self.td_mode})

    async def fetch_leverage_tiers(self, symbol: str) -> Sequence[tuple[Decimal, Decimal]]:
        """杠杆档位 [(**quote 名义额**上限, 最大杠杆), ...] 升序。

        单位说明：OKX 的档位上限 maxSz 是**张数**，而 base.py 定的契约、以及币安
        (notionalCap) / Bybit (riskLimitValue) / Gate (risk_limit) / Bitget (endUnit)
        给的都是 quote 名义额。直接报 base 币数量会让 safety.choose_leverage 拿美元
        名义额去比"1000（BTC）"，判定早已越顶档，直接掐到最低杠杆。
        /public/position-tiers 不返回价格，只能用盘口中价折算；拿不到价格时退化成
        单档（名义上限未知），和 HTX/Bybit 的退化口径一致，绝不返回单位错误的数。
        """
        if self.kind is not MarketKind.PERP:
            return ()
        inst = await self._instrument(symbol)
        params = {"instType": "SWAP", "tdMode": self.td_mode,
                  "instFamily": self._inst_family.get(symbol, "")}
        data = await self._request("GET", "/api/v5/public/position-tiers",
                                   quota="market", params=params)
        try:
            price = (await self.fetch_book_top(symbol)).mid
        except (ExchangeError, httpx.HTTPError):
            price = ZERO
        if price <= 0:
            return ((Decimal("Infinity"), inst.max_leverage),)
        tiers = [(_dec(d.get("maxSz")) * inst.contract_size * price,
                  _dec(d.get("maxLever"))) for d in data]
        tiers.sort(key=lambda t: t[0])
        return tuple(tiers)
