"""钉住「hosted 模式下 refresh_opportunities 真的会调浏览器取数」这条接缝。

这正是上线第一版漏掉的地方：Python 侧每个零件单测都过，但整条链在生产上
一次都没跑起来——机会榜稳定少 binance/bybit 两条腿，状态栏零提示。
单元测试测不到"接线"，只有把 refresh_opportunities 整个跑一遍才测得到。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from earnfarm.config import Config
from earnfarm.models import Venue
from earnfarm.ui import app as appmod
from earnfarm.ui import public_feed_client as pf
from earnfarm.ui.app import AppState, refresh_opportunities


class FakeLabel:
    """够用的 ui.label 替身：只关心写进去的文案序列。"""

    def __init__(self) -> None:
        self.text = ""
        self.seen: list[str] = []

    def style(self, *_a, **_k):
        self.seen.append(self.text)
        return self


class FakeSpinner:
    def set_visibility(self, _v) -> None:
        pass


class BoomFeed:
    """PublicFeed 的替身：进 async with 就炸，把测试挡在联网之前。

    浏览器取数发生在它之前，所以照样测得到；而整轮 105 秒的真实拉取
    一秒都不用等。
    """

    def __init__(self, *_a, **_k) -> None:
        pass

    async def __aenter__(self):
        raise RuntimeError("挡住联网")

    async def __aexit__(self, *_a):
        return False


@pytest.fixture
def state(tmp_path):
    return AppState(replace(Config(), data_dir=tmp_path))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(appmod, "PublicFeed", BoomFeed)
    # 回填器要建九家适配器（实测 4.3 秒），而这组测试只关心浏览器取数那一段。
    # 不打掉的话五个用例白烧 20 秒
    monkeypatch.setattr(AppState, "ensure_backfiller", lambda self: None)


def _run(state, monkeypatch, *, hosted: bool):
    monkeypatch.setenv("EARNFARM_HOSTED", "1" if hosted else "")
    calls: list[tuple] = []

    async def spy(venues=None, timeout=45.0):
        calls.append(tuple(venues or ()))
        return {"binance": 3, "bybit": 2}, {}

    monkeypatch.setattr(pf, "feed_board_all", spy)
    label = FakeLabel()
    asyncio.run(refresh_opportunities(state, label, FakeSpinner()))
    return calls, label


def test_hosted_mode_calls_feed_board_all(state, monkeypatch):
    """hosted 模式必须真的调到浏览器取数，且把勾选的家原样传进去。"""
    calls, _ = _run(state, monkeypatch, hosted=True)
    assert len(calls) == 1, "hosted 模式下必须调且只调一次"
    assert set(pf.venue_name(v) for v in calls[0]) == {v.value for v in Venue}


def test_hosted_mode_announces_the_browser_fetch(state, monkeypatch):
    """状态栏要说人话：先说"正在用你的浏览器拉"，成功后说拉到了哪几家。

    第一版这两句一次都没出现过——那本身就是"整段没执行"的唯一外部症状。
    """
    _, label = _run(state, monkeypatch, hosted=True)
    joined = "\n".join(label.seen)
    assert "正在用你的浏览器拉取" in joined
    assert "binance" in joined and "bybit" in joined


def test_local_mode_never_touches_the_browser(state, monkeypatch):
    """本地模式行为一字不变：不调浏览器取数，也不提它。"""
    calls, label = _run(state, monkeypatch, hosted=False)
    assert calls == []
    assert "浏览器" not in "\n".join(label.seen)


def test_browser_failure_still_lets_the_board_refresh(state, monkeypatch):
    """浏览器整轮挂掉也不能中断刷新——榜上少一家好过一条都没有。"""
    monkeypatch.setenv("EARNFARM_HOSTED", "1")

    async def boom(venues=None, timeout=45.0):
        raise RuntimeError("页面断开了")

    monkeypatch.setattr(pf, "feed_board_all", boom)
    label = FakeLabel()
    # 不抛出去：整轮刷新照常走到 PublicFeed（这里被 BoomFeed 挡下）
    asyncio.run(refresh_opportunities(state, label, FakeSpinner()))
    assert state.refreshing is False, "finally 必须把标志放掉，否则整站卡死"


def test_refresh_flag_is_released_even_when_browser_feed_runs(state, monkeypatch):
    _run(state, monkeypatch, hosted=True)
    assert state.refreshing is False
