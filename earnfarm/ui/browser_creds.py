"""浏览器端凭据保管：密钥加密后存在**访客自己的 localStorage**，服务器不留。

为什么不存服务器：这个工具要给别人用。别人的交易所密钥存在你的服务器上
是负债不是功能——一次入侵、一次备份泄露，赔的是别人的钱。所以：

- 加密和解密**全在浏览器里**做（Web Crypto：PBKDF2-SHA256 200k 轮派生密钥，
  AES-GCM 加密），密文写进访客自己的 localStorage；
- 服务器只在**跑分析那一刻**拿到明文——币安的 HMAC 签名必须在服务端算，
  这一步物理上绕不开——用完即弃，不写任何文件、不进任何数据库；
- 换台电脑/换浏览器就没有了，这是设计如此：凭据跟着人走，不跟着服务器走。

口径必须对用户说清楚（页面上有）：这不是端到端加密，是「不托管」。
"""

from __future__ import annotations

import json
from typing import Any

from nicegui import ui

_STORE_KEY = "earnfarm_creds_v1"

# 全在浏览器里跑。iterations 20 万：手机上约 200ms，离线爆破则代价高昂
_JS = """
window.ef = {
  _b64(buf) { return btoa(String.fromCharCode(...new Uint8Array(buf))); },
  _u8(s) { return Uint8Array.from(atob(s), c => c.charCodeAt(0)); },
  _store() {
    try { return JSON.parse(localStorage.getItem('%(store)s') || '{}'); }
    catch (e) { return {}; }
  },
  async _key(pass, salt) {
    const base = await crypto.subtle.importKey(
      'raw', new TextEncoder().encode(pass), 'PBKDF2', false, ['deriveKey']);
    return crypto.subtle.deriveKey(
      { name: 'PBKDF2', salt: salt, iterations: 200000, hash: 'SHA-256' },
      base, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
  },
  list() { return Object.keys(this._store()); },
  del(alias) {
    const s = this._store(); delete s[alias];
    localStorage.setItem('%(store)s', JSON.stringify(s));
    return true;
  },
  async save(alias, obj, pass) {
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const key = await this._key(pass, salt);
    const ct = await crypto.subtle.encrypt(
      { name: 'AES-GCM', iv: iv }, key,
      new TextEncoder().encode(JSON.stringify(obj)));
    const s = this._store();
    s[alias] = { salt: this._b64(salt), iv: this._b64(iv), ct: this._b64(ct) };
    localStorage.setItem('%(store)s', JSON.stringify(s));
    return true;
  },
  async load(alias, pass) {
    const rec = this._store()[alias];
    if (!rec) return { err: 'missing' };
    try {
      const key = await this._key(pass, this._u8(rec.salt));
      const pt = await crypto.subtle.decrypt(
        { name: 'AES-GCM', iv: this._u8(rec.iv) }, key, this._u8(rec.ct));
      return { ok: JSON.parse(new TextDecoder().decode(pt)) };
    } catch (e) { return { err: 'badpass' }; }
  },
};
""" % {"store": _STORE_KEY}


def install() -> None:
    """把客户端加密模块注入页面。每个用到它的页面调用一次。"""
    ui.add_head_html(f"<script>{_JS}</script>")


class BrowserCredError(Exception):
    """给用户看的错误：密码不对、条目不存在、浏览器不支持等。"""


async def list_aliases() -> list[str]:
    try:
        return list(await ui.run_javascript("window.ef.list()", timeout=5.0) or [])
    except Exception:
        return []          # 老浏览器/禁用 localStorage：当作一个都没存


async def save(alias: str, credential: dict[str, str], passphrase: str) -> None:
    args = f"{json.dumps(alias)}, {json.dumps(credential)}, {json.dumps(passphrase)}"
    try:
        await ui.run_javascript(f"window.ef.save({args})", timeout=15.0)
    except Exception as exc:
        raise BrowserCredError(f"保存到浏览器失败：{exc}") from exc


async def load(alias: str, passphrase: str) -> dict[str, Any]:
    args = f"{json.dumps(alias)}, {json.dumps(passphrase)}"
    try:
        res = await ui.run_javascript(f"window.ef.load({args})", timeout=15.0)
    except Exception as exc:
        raise BrowserCredError(f"读取浏览器凭据失败：{exc}") from exc
    if not isinstance(res, dict) or "ok" not in res:
        err = (res or {}).get("err") if isinstance(res, dict) else None
        if err == "missing":
            raise BrowserCredError("这台浏览器里没有这条凭据")
        raise BrowserCredError("解锁密码不对（凭据在你本机加密，密码错了解不开）")
    return res["ok"]


async def delete(alias: str) -> None:
    try:
        await ui.run_javascript(f"window.ef.del({json.dumps(alias)})", timeout=5.0)
    except Exception as exc:
        raise BrowserCredError(f"删除失败：{exc}") from exc
