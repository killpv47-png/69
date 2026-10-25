"""KILLDPI share-link format (used by the bot, docs and the Android client).

    killdpi://<uuid>@<host>:<port>?pbk=<server-public-key>&cipher=<c>&type=<tcp|ws|grpc>
             &security=<none|tls>&sni=<sni>&host=<ws-host>&path=<ws-path>&fp=<fingerprint>
             &serviceName=<grpc-service>&padding=<min-max>&keepalive=<sec>&pcs=<cert-sha256>#<name>

It mirrors the VLESS share-link convention so existing parsers can be adapted
with minimal changes. Only `uuid`, `host`, `port` and `pbk` are mandatory.
"""
from __future__ import annotations

import json
import uuid as _uuid
from dataclasses import dataclass, asdict
from urllib.parse import quote, unquote, urlencode, urlparse, parse_qs

SCHEME = "killdpi"
CIPHERS = ("chacha20-poly1305", "aes-256-gcm")
NETWORKS = ("tcp", "ws", "grpc")


class LinkError(ValueError):
    """Raised with a human-readable message when a link cannot be parsed."""


@dataclass
class KillDPIConfig:
    uuid: str
    host: str
    port: int
    pbk: str
    cipher: str = "chacha20-poly1305"
    network: str = "tcp"
    security: str = "none"
    sni: str = ""
    ws_host: str = ""
    path: str = ""
    fp: str = "chrome"
    service_name: str = ""
    padding: str = ""
    keepalive: int = 0
    pin_sha256: str = ""
    name: str = "KILLDPI"

    def validate(self) -> None:
        try:
            _uuid.UUID(self.uuid)
        except Exception:
            raise LinkError("UUID نامعتبر است")
        if not self.host:
            raise LinkError("آدرس سرور خالی است")
        if not (0 < int(self.port) < 65536):
            raise LinkError("پورت باید بین 1 تا 65535 باشد")
        if len(self.pbk) < 40:
            raise LinkError("کلید عمومی سرور (pbk) نامعتبر است")
        if self.cipher not in CIPHERS:
            raise LinkError(f"cipher نامعتبر: {self.cipher} (مجاز: {', '.join(CIPHERS)})")
        if self.network not in NETWORKS:
            raise LinkError(f"نوع شبکه نامعتبر: {self.network} (مجاز: {', '.join(NETWORKS)})")
        if self.security not in ("none", "tls"):
            raise LinkError("security باید none یا tls باشد")

    def to_link(self) -> str:
        self.validate()
        q = {"pbk": self.pbk, "cipher": self.cipher, "type": self.network, "security": self.security}
        if self.sni:
            q["sni"] = self.sni
        if self.network == "ws":
            if self.ws_host:
                q["host"] = self.ws_host
            if self.path:
                q["path"] = self.path
        if self.network == "grpc" and self.service_name:
            q["serviceName"] = self.service_name
        if self.security == "tls":
            q["fp"] = self.fp or "chrome"
            if self.pin_sha256:
                q["pcs"] = self.pin_sha256
        if self.padding:
            q["padding"] = self.padding
        if self.keepalive:
            q["keepalive"] = str(self.keepalive)
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{SCHEME}://{self.uuid}@{host}:{self.port}?{urlencode(q, quote_via=quote)}#{quote(self.name)}"

    @classmethod
    def from_link(cls, link: str) -> "KillDPIConfig":
        link = link.strip()
        if not link.lower().startswith(SCHEME + "://"):
            raise LinkError("لینک باید با killdpi:// شروع شود")
        u = urlparse(link)
        if not u.username or not u.hostname or not u.port:
            raise LinkError("ساختار لینک ناقص است (uuid@host:port)")
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if "pbk" not in q:
            raise LinkError("پارامتر pbk (کلید عمومی سرور) وجود ندارد")
        cfg = cls(
            uuid=u.username, host=u.hostname, port=u.port, pbk=q["pbk"],
            cipher=q.get("cipher", "chacha20-poly1305"), network=q.get("type", "tcp"),
            security=q.get("security", "none"), sni=q.get("sni", ""), ws_host=q.get("host", ""),
            path=q.get("path", ""), fp=q.get("fp", "chrome"), service_name=q.get("serviceName", ""),
            padding=q.get("padding", ""), keepalive=int(q.get("keepalive", "0") or 0),
            pin_sha256=q.get("pcs", ""), name=unquote(u.fragment) if u.fragment else "KILLDPI",
        )
        cfg.validate()
        return cfg

    def stream_settings(self) -> dict:
        ss: dict = {"network": self.network, "security": self.security}
        if self.network == "ws":
            ws = {"path": self.path or "/"}
            if self.ws_host:
                ws["host"] = self.ws_host
            ss["wsSettings"] = ws
        elif self.network == "grpc":
            ss["grpcSettings"] = {"serviceName": self.service_name or "killdpi"}
        if self.security == "tls":
            tls = {"serverName": self.sni or self.ws_host or self.host, "fingerprint": self.fp or "chrome"}
            if self.pin_sha256:
                tls["pinnedPeerCertSha256"] = self.pin_sha256
            ss["tlsSettings"] = tls
        return ss

    def to_outbound(self, tag: str = "proxy") -> dict:
        settings = {"address": self.host, "port": self.port, "id": self.uuid, "publicKey": self.pbk, "cipher": self.cipher}
        if self.padding:
            settings["padding"] = self.padding
        if self.keepalive:
            settings["keepalive"] = self.keepalive
        return {"tag": tag, "protocol": "killdpi", "settings": settings, "streamSettings": self.stream_settings()}

    def to_client_json(self, socks_port: int = 10808) -> str:
        cfg = {
            "log": {"loglevel": "warning"},
            "inbounds": [{"tag": "socks", "listen": "127.0.0.1", "port": socks_port, "protocol": "socks",
                          "settings": {"udp": True}, "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]}}],
            "outbounds": [self.to_outbound(), {"tag": "direct", "protocol": "freedom"}, {"tag": "block", "protocol": "blackhole"}],
            "routing": {"domainStrategy": "IPIfNonMatch",
                        "rules": [{"type": "field", "outboundTag": "direct",
                                   "ip": ["10.0.0.0/8", "127.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7"]}]},
        }
        return json.dumps(cfg, indent=2, ensure_ascii=False)

    def to_dict(self) -> dict:
        return asdict(self)


def server_inbound_json(private_key: str, clients: list[dict], port: int, network: str = "tcp",
                        path: str = "/killdpi", cert_file: str = "", key_file: str = "",
                        fallback_port: int | None = None, service_name: str = "killdpi") -> str:
    """Generate a server-side Xray config for a KILLDPI inbound."""
    settings: dict = {"clients": clients, "privateKey": private_key}
    if fallback_port:
        settings["fallbacks"] = [{"dest": fallback_port}]
    ss: dict = {"network": network}
    if network == "ws":
        ss["wsSettings"] = {"path": path}
    elif network == "grpc":
        ss["grpcSettings"] = {"serviceName": service_name}
    if cert_file:
        ss["security"] = "tls"
        ss["tlsSettings"] = {"certificates": [{"certificateFile": cert_file, "keyFile": key_file}]}
    cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": [{"tag": "killdpi-in", "listen": "0.0.0.0", "port": port, "protocol": "killdpi",
                      "settings": settings, "streamSettings": ss}],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}, {"tag": "block", "protocol": "blackhole"}],
        "routing": {"rules": [{"type": "field", "outboundTag": "block",
                               "ip": ["10.0.0.0/8", "127.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7"]}]},
    }
    return json.dumps(cfg, indent=2)
