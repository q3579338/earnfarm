"""网页访问闸：首次设置、密码校验、令牌一次性。

这一层是公网部署的唯一防线（运行时状态所有客户端共享，闸一破全盘皆失），
所以三件事必须钉死：明文密码绝不落盘、令牌用完即废、环境变量应急通道有效。
"""

from __future__ import annotations

import json

import pytest

from earnfarm.ui import access


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(access.WEB_PASSWORD_ENV, raising=False)
    monkeypatch.delenv(access.WEB_AUTH_ENV, raising=False)


def test_gate_off_by_default():
    """什么都不设 = 本地模式，无登录闸。默认行为不能因为加了闸而改变。"""
    assert not access.gate_enabled()


def test_gate_on_by_auth_flag_or_password(monkeypatch):
    monkeypatch.setenv(access.WEB_AUTH_ENV, "1")
    assert access.gate_enabled()
    monkeypatch.delenv(access.WEB_AUTH_ENV)
    monkeypatch.setenv(access.WEB_PASSWORD_ENV, "s3cret-value")
    assert access.gate_enabled()


def test_set_then_verify_password(tmp_path):
    assert not access.password_configured(tmp_path)
    access.set_password(tmp_path, "hunter2-long")
    assert access.password_configured(tmp_path)
    assert access.verify_password(tmp_path, "hunter2-long")
    assert not access.verify_password(tmp_path, "hunter2-lonG")
    assert not access.verify_password(tmp_path, "")


def test_password_file_never_contains_plaintext(tmp_path):
    """落盘的只能是 salt+scrypt 哈希。明文进了文件，备份/快照就等于泄露。"""
    access.set_password(tmp_path, "my-secret-pw-123")
    blob = json.loads((tmp_path / "web_password.json").read_text(encoding="utf-8"))
    assert set(blob) == {"salt", "hash"}
    assert "my-secret-pw-123" not in json.dumps(blob)
    # 同一密码两次设置必须产出不同哈希（盐是随机的）
    first = blob["hash"]
    access.set_password(tmp_path, "my-secret-pw-123")
    second = json.loads((tmp_path / "web_password.json").read_text(encoding="utf-8"))["hash"]
    assert first != second


def test_short_password_rejected(tmp_path):
    with pytest.raises(ValueError, match="至少"):
        access.set_password(tmp_path, "short")
    assert not access.password_configured(tmp_path)


def test_setup_token_is_stable_then_consumed(tmp_path):
    """令牌重启复用（不然每次重启都作废用户手里那张），设完密码即作废。"""
    token = access.setup_token(tmp_path)
    assert token and access.setup_token(tmp_path) == token
    access.set_password(tmp_path, "brand-new-pass")
    assert not (tmp_path / "setup_token").exists()
    # 重新取会生成一张**新的**，旧令牌不能复活
    assert access.setup_token(tmp_path) != token


def test_env_password_overrides_file(tmp_path, monkeypatch):
    """环境变量是应急通道：忘了密码也能进。它必须压过文件里的哈希。"""
    access.set_password(tmp_path, "file-password")
    monkeypatch.setenv(access.WEB_PASSWORD_ENV, "env-password")
    assert access.verify_password(tmp_path, "env-password")
    assert not access.verify_password(tmp_path, "file-password")


def test_storage_secret_is_persistent(tmp_path):
    """换 secret 等于让所有已登录会话掉线，所以必须生成一次就固定。"""
    s = access.storage_secret(tmp_path)
    assert len(s) >= 32
    assert access.storage_secret(tmp_path) == s
