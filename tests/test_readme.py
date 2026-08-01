"""README 里那份示例配置必须是真能加载的。

三个 agent 并行往 config.py 里加过段，文档漂移的代价很具体：用户照着 README
写一份 config.toml，启动时被 ConfigError 顶回来（"不认识的配置项"），
而他没有任何理由怀疑是文档错了。示例配置是接口的一部分，跟着测。
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

from carryfarm.config import MIN_MARKET_INTERVAL_S, load

README = Path(__file__).resolve().parent.parent / "README.md"


def _toml_blocks() -> list[str]:
    return re.findall(r"```toml\n(.*?)```", README.read_text(encoding="utf-8"), re.S)


def test_readme_exists():
    assert README.exists()


def test_every_toml_block_in_the_readme_actually_loads(tmp_path):
    blocks = _toml_blocks()
    assert blocks, "README 里应当有示例配置"
    for i, block in enumerate(blocks):
        path = tmp_path / f"config_{i}.toml"
        path.write_text(block, encoding="utf-8")
        cfg = load(path)          # 键名写错 / 段名过时都会在这里抛 ConfigError
        assert cfg.watch.market_interval_s >= MIN_MARKET_INTERVAL_S


def test_readme_documents_the_alert_threshold_split():
    """[alerts].min_net_apr 和 [scoring].min_net_apr 是两个不同的旋钮，
    同名不同义，是最容易被当成重复配置删掉的一处。"""
    text = README.read_text(encoding="utf-8")
    assert "alerts.min_net_apr" in text and "scoring.min_net_apr" in text


def test_readme_states_the_headline_finding():
    """README 的存在理由是那条实测结论：绝大多数机会是负期望、当前费率不可外推。
    这段被后来的人当成"营销文案"删掉的话，工具就会重新变得像在推荐机会。"""
    text = README.read_text(encoding="utf-8")
    assert "负期望" in text
    assert "不可外推" in text
