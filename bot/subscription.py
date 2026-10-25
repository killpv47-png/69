"""Builds the subscription body for one user.

Every user gets, for each config they created:
  * several `killdpi://` links with the SAME protocol parameters but different
    *addresses* (tunnel hostname + a set of Cloudflare clean IPs) and different
    names, so a client can switch if one IP is throttled;
  * one "info" entry whose name is updated continuously and shows how many
    hours/minutes are left before the server (and therefore the subscription)
    must be refreshed;
  * a few static info entries (server start time, refresh hint, ...).

Clients ignore unreachable info entries; they are there to carry text only.
The whole file is plain base64 (standard subscription format).
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

from killdpi_link import KillDPIConfig
from profiles import PROFILES

# Cloudflare anycast ranges that are commonly reachable from Iran. The first
# entry (tunnel hostname) is always present; admin can override the IP list.
DEFAULT_CLEAN_IPS = [
    "188.114.96.1", "188.114.97.1", "162.159.192.1", "162.159.195.1",
    "104.17.148.22", "104.18.0.1", "104.19.0.1", "172.67.0.1",
]

INFO_UUID = "00000000-0000-4000-8000-000000000001"
INFO_PBK = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # 43 chars, syntactically valid, never used
TEHRAN = timezone(timedelta(hours=3, minutes=30))


def _fmt_remaining(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, m = divmod(seconds // 60, 60)
    return f"{h:02d}h {m:02d}m"


def info_link(name: str) -> str:
    """A syntactically valid killdpi:// link that only carries a name."""
    return KillDPIConfig(uuid=INFO_UUID, host="127.0.0.1", port=1, pbk=INFO_PBK, name=name).to_link()


def config_link(cfg: dict, host: str, tunnel_host: str, pbk: str, name: str, ws_path: str) -> str:
    p = PROFILES[cfg["profile"]]
    return KillDPIConfig(
        uuid=cfg["uuid"], host=host, port=443, pbk=pbk, cipher=p.cipher, network="ws", security="tls",
        sni=tunnel_host, ws_host=tunnel_host, path=ws_path, fp="chrome",
        padding=p.padding, keepalive=p.keepalive, name=name).to_link()


def build_subscription(user: dict, configs: dict, runtime: dict, pbk: str, clean_ips: list[str] | None = None,
                       ws_path: str = "/killdpi", now: datetime | None = None, max_ips: int = 4) -> tuple[str, list[str]]:
    """Return (base64 body, plain list of links)."""
    now = now or datetime.now(timezone.utc)
    tunnel = runtime.get("tunnel_host", "")
    deadline = runtime.get("deadline_ts", 0)
    remaining = deadline - now.timestamp() if deadline else 0
    clean_ips = (clean_ips or DEFAULT_CLEAN_IPS)[:max_ips]

    links: list[str] = []
    # --- live countdown (name is refreshed every publish) ---
    links.append(info_link(f"⏳ تا بروزرسانی ساب: {_fmt_remaining(remaining)}"))
    if tunnel:
        links.append(info_link(f"🟢 سرور فعال | شروع {datetime.fromtimestamp(runtime.get('started_ts', now.timestamp()), TEHRAN):%H:%M} تهران"))
    else:
        links.append(info_link("🔴 سرور خاموش است — بعداً ساب را بروزرسانی کنید"))
    links.append(info_link(f"🔄 آخرین بروزرسانی: {now.astimezone(TEHRAN):%m-%d %H:%M}"))

    user_cfgs = [configs[t] for t in user.get("configs", []) if t in configs]
    if tunnel:
        for cfg in user_cfgs:
            p = PROFILES[cfg["profile"]]
            base = f"{p.emoji} {p.title}"
            links.append(config_link(cfg, tunnel, tunnel, pbk, f"{base} | CF-Auto", ws_path))
            for i, ip in enumerate(clean_ips, 1):
                links.append(config_link(cfg, ip, tunnel, pbk, f"{base} | Clean-{i} {ip}", ws_path))
    links.append(info_link("ℹ️ اگر وصل نشد، فقط «بروزرسانی ساب» بزنید"))

    # Pure base64 body: the most widely supported subscription format
    # (v2rayNG, Hiddify, Streisand, Happ, NekoBox ... all accept it).
    body = base64.b64encode("\n".join(links).encode()).decode()
    return body + "\n", links


def decode_subscription(text: str) -> list[str]:
    return base64.b64decode(text.strip()).decode().split("\n")
