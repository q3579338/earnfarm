"""AppState 的交易所勾选：存库、读回、下限拒绝。

这是**数据源**选择不是过滤器：改选会重拉行情，没勾的家连请求都不发。
最危险的错误形态是"剩一家"——跨所配对没有对手腿，榜单必然全空，
而界面上完全看不出为什么，所以 MIN_VENUES 是硬闸不是建议。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from earnfarm.config import Config
from earnfarm.models import Venue
from earnfarm.ui.app import MIN_VENUES, META_VENUES, AppState


@pytest.fixture
def state(tmp_path):
    return AppState(replace(Config(), data_dir=tmp_path))


def test_default_is_all_venues(state):
    assert state.enabled_venues == tuple(Venue)


def test_selection_round_trips_through_the_db(state, tmp_path):
    assert state.set_venues([Venue.BINANCE, Venue.HYPERLIQUID, Venue.KUCOIN])
    # 新建一个 AppState 模拟重启：选择必须从库里读回来
    reborn = AppState(replace(Config(), data_dir=tmp_path))
    assert reborn.enabled_venues == (Venue.BINANCE, Venue.HYPERLIQUID, Venue.KUCOIN)


def test_fewer_than_min_venues_is_rejected_and_state_untouched(state):
    before = state.enabled_venues
    assert not state.set_venues([Venue.BINANCE])
    assert not state.set_venues([])
    assert state.enabled_venues == before, "拒绝时状态一个字都不能动"
    assert MIN_VENUES == 2


def test_unknown_venue_tokens_in_db_are_ignored(state, tmp_path):
    """回滚陷阱：存过 8 家的库被退回少一家的版本读，不认识的名字要**跳过**
    而不是把整个选择废掉——Venue(\"gone\") 抛 ValueError 的那种废法会让
    用户的其余选择全部丢失。"""
    state.session.storage.set_meta(META_VENUES, "binance,ghost_exchange,kucoin")
    reborn = AppState(replace(Config(), data_dir=tmp_path))
    assert reborn.enabled_venues == (Venue.BINANCE, Venue.KUCOIN)


def test_db_with_too_few_valid_venues_falls_back_to_all(state, tmp_path):
    """库里有效名单剩一家时回全家：榜单空着却不知道为什么，比忘掉选择更糟。"""
    state.session.storage.set_meta(META_VENUES, "binance,ghost_exchange")
    reborn = AppState(replace(Config(), data_dir=tmp_path))
    assert reborn.enabled_venues == tuple(Venue)


def test_duplicates_are_collapsed_but_order_kept(state):
    assert state.set_venues([Venue.KUCOIN, Venue.BINANCE, Venue.KUCOIN])
    assert state.enabled_venues == (Venue.KUCOIN, Venue.BINANCE)


def test_changing_selection_bumps_the_generation(state):
    """代际计数是"改选后尽快重拉"的触发信号。它必须**只**在选择真变时递增：
    上一轮刷新完成会盖掉 last_refresh，拿时间戳当信号会被覆盖；
    而重复提交同一份选择也不该白烧一轮 2 分钟的全市场拉取。"""
    rev = state.venues_rev
    assert state.set_venues([Venue.BINANCE, Venue.KUCOIN])
    assert state.venues_rev == rev + 1
    assert state.set_venues([Venue.BINANCE, Venue.KUCOIN])   # 没变
    assert state.venues_rev == rev + 1, "同样的选择不触发重拉"
    assert not state.set_venues([Venue.BINANCE])             # 被拒
    assert state.venues_rev == rev + 1, "被拒的修改不触发重拉"
    assert state.refreshed_rev != state.venues_rev, "此刻应该欠一轮重拉"
