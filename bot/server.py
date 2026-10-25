"""Runs the KILLDPI server *inside* the GitHub Actions runner.

    xray (killdpi inbound, WS, 127.0.0.1:8080, fallback -> decoy site :8081)
    cloudflared quick tunnel  -> https://<random>.trycloudflare.com -> 127.0.0.1:8080
    decoy http server          -> plain HTTPS GETs to the tunnel see a boring web page

The tunnel hostname changes on every run, so the bot re-publishes every
subscription with the new address each time it starts. Clients connect to a
Cloudflare clean IP with SNI/Host = tunnel hostname, which is proven to work
with the KILLDPI WS transport.

Nothing in the protocol is changed; this file only orchestrates the stock
Xray-core binary built from the KILLDPI repo.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from xray_api import XrayAPI

log = logging.getLogger("server")

TUNNEL_RE = re.compile(r"https://([a-z0-9-]+\.trycloudflare\.com)")

# Explicit list instead of geoip:private (the release binary ships without geoip.dat).
PRIVATE_CIDRS = ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
                 "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
                 "::1/128", "fc00::/7", "fe80::/10"]

DECOY_HTML = b"""<!doctype html><html><head><meta charset="utf-8"><title>Welcome to nginx!</title>
<style>body{width:35em;margin:0 auto;font-family:Tahoma,Verdana,Arial,sans-serif}</style></head>
<body><h1>Welcome to nginx!</h1><p>If you see this page, the nginx web server is successfully installed and
working. Further configuration is required.</p><p><em>Thank you for using nginx.</em></p></body></html>
"""


class _Decoy(BaseHTTPRequestHandler):
    server_version = "nginx"
    sys_version = ""

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(DECOY_HTML)))
        self.end_headers()
        self.wfile.write(DECOY_HTML)

    do_HEAD = do_GET  # noqa: N815

    def log_message(self, *a):  # silence
        pass


def _free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def server_config(private_key: str, clients: list[dict], port: int, api_port: int, ws_path: str,
                  fallback_port: int) -> dict:
    """Server-side Xray JSON. Same inbound shape as tests/e2e.sh in the KILLDPI repo plus the API."""
    return {
        "log": {"loglevel": "warning"},
        "api": {"tag": "api", "listen": f"127.0.0.1:{api_port}", "services": ["HandlerService", "StatsService"]},
        "stats": {},
        "policy": {"levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True, "statsUserOnline": True,
                                     "handshake": 8, "connIdle": 300}},
                   "system": {"statsInboundUplink": True, "statsInboundDownlink": True}},
        "inbounds": [{
            "tag": "killdpi-in", "listen": "127.0.0.1", "port": port, "protocol": "killdpi",
            "settings": {"clients": clients, "privateKey": private_key, "fallbacks": [{"dest": fallback_port}]},
            "streamSettings": {"network": "ws", "wsSettings": {"path": ws_path}},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
        }],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}, {"tag": "block", "protocol": "blackhole"}],
        "routing": {"domainStrategy": "IPIfNonMatch",
                    "rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
                              {"type": "field", "outboundTag": "block",
                               "ip": PRIVATE_CIDRS}]},
    }


class KillDPIServer:
    def __init__(self, xray_bin: str, cloudflared_bin: str, private_key: str, workdir: str = ".runtime",
                 port: int = 8080, api_port: int = 10085, fallback_port: int = 8081, ws_path: str = "/killdpi"):
        self.xray_bin = xray_bin
        self.cloudflared_bin = cloudflared_bin
        self.private_key = private_key
        self.workdir = workdir
        self.port, self.api_port, self.fallback_port, self.ws_path = port, api_port, fallback_port, ws_path
        self.tunnel_host: str = ""
        self.started_at: float = 0
        self._xray: subprocess.Popen | None = None
        self._cf: subprocess.Popen | None = None
        self._decoy: ThreadingHTTPServer | None = None
        self.api = XrayAPI(f"127.0.0.1:{api_port}")
        os.makedirs(workdir, exist_ok=True)

    # ------------------------------------------------------------ keys
    @staticmethod
    def keygen(xray_bin: str) -> tuple[str, str]:
        out = subprocess.run([xray_bin, "killdpi-keygen"], capture_output=True, text=True, check=True).stdout
        priv = re.search(r"PrivateKey:\s*(\S+)", out).group(1)
        pub = re.search(r"PublicKey:\s*(\S+)", out).group(1)
        return priv, pub

    @staticmethod
    def public_key_of(private_key: str) -> str:
        """X25519 public key for a base64url private key (no xray needed)."""
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import x25519
        raw = base64.urlsafe_b64decode(private_key + "=" * (-len(private_key) % 4))
        pub = x25519.X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.urlsafe_b64encode(pub).rstrip(b"=").decode()

    # ------------------------------------------------------------ lifecycle
    def start(self, clients: list[dict], tunnel_timeout: int = 60) -> str:
        """Start decoy + xray + tunnel. Returns the tunnel hostname."""
        if not clients:  # xray refuses an empty client list -> placeholder that nobody owns
            clients = [{"id": "00000000-0000-4000-8000-000000000000", "email": "placeholder@killdpi"}]
        for prt in (self.port, self.api_port, self.fallback_port):
            if not _free(prt):
                raise RuntimeError(f"port {prt} already in use (previous server still running?)")
        cfg_path = os.path.join(self.workdir, "server.json")
        with open(cfg_path, "w") as f:
            json.dump(server_config(self.private_key, clients, self.port, self.api_port, self.ws_path, self.fallback_port), f, indent=2)

        self._decoy = ThreadingHTTPServer(("127.0.0.1", self.fallback_port), _Decoy)
        threading.Thread(target=self._decoy.serve_forever, daemon=True).start()

        self._xray = subprocess.Popen([self.xray_bin, "run", "-c", cfg_path],
                                      stdout=open(os.path.join(self.workdir, "xray.log"), "wb"), stderr=subprocess.STDOUT)
        for _ in range(50):
            if not _free(self.port) and not _free(self.api_port):
                break
            if self._xray.poll() is not None:
                raise RuntimeError("xray exited: " + open(os.path.join(self.workdir, "xray.log")).read()[-800:])
            time.sleep(0.2)
        else:
            raise RuntimeError("xray did not open its ports")
        log.info("xray up on :%d (api :%d)", self.port, self.api_port)

        self.tunnel_host = self._start_tunnel(tunnel_timeout)
        self.started_at = time.time()
        if self.wait_dns():
            log.info("tunnel hostname resolves; probe=%s", self.probe(attempts=3))
        else:
            log.warning("tunnel hostname did not resolve within 90 s (edge DNS lag)")
        return self.tunnel_host

    def _start_tunnel(self, timeout: int) -> str:
        logpath = os.path.join(self.workdir, "cloudflared.log")
        open(logpath, "w").close()  # fresh log: never pick up a hostname from a previous instance
        self._cf = subprocess.Popen([self.cloudflared_bin, "tunnel", "--url", f"http://127.0.0.1:{self.port}",
                                     "--no-autoupdate", "--protocol", "http2", "--edge-ip-version", "4"],
                                    stdout=open(logpath, "ab"), stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            txt = open(logpath, errors="ignore").read()
            m = TUNNEL_RE.search(txt)
            if m:
                host = m.group(1)
                # wait until the edge actually routes to us
                for _ in range(20):
                    if "Registered tunnel connection" in open(logpath, errors="ignore").read():
                        break
                    time.sleep(0.5)
                log.info("tunnel: %s", host)
                return host
            if self._cf.poll() is not None:
                raise RuntimeError("cloudflared exited: " + txt[-800:])
            time.sleep(0.5)
        raise RuntimeError("cloudflared did not report a URL in time")

    def restart_tunnel(self) -> str:
        if self._cf:
            self._cf.terminate()
            try:
                self._cf.wait(5)
            except subprocess.TimeoutExpired:
                self._cf.kill()
        self.tunnel_host = self._start_tunnel(60)
        self.wait_dns()
        return self.tunnel_host

    def healthy(self) -> bool:
        return (self._xray is not None and self._xray.poll() is None and
                self._cf is not None and self._cf.poll() is None)

    def tunnel_alive(self) -> bool:
        return self._cf is not None and self._cf.poll() is None

    def probe(self, timeout: float = 8, attempts: int = 1) -> bool:
        """HTTPS GET to the tunnel must reach our origin (proves edge -> runner path works)."""
        if not self.tunnel_host:
            return False
        import httpx
        for i in range(attempts):
            try:
                r = httpx.get(f"https://{self.tunnel_host}/", timeout=timeout, follow_redirects=False)
                # 200 = decoy, 404 = xray's WS listener answered (non-WS path). Both prove edge -> origin.
                # 502/530 = cloudflared is up but the origin is dead; 1033 = tunnel gone.
                if r.status_code in (200, 404):
                    return True
                log.warning("probe: status %s", r.status_code)
            except Exception as e:  # noqa: BLE001
                log.warning("probe failed (%d/%d): %s", i + 1, attempts, e)
            if i + 1 < attempts:
                time.sleep(3)
        return False

    def wait_dns(self, timeout: int = 90) -> bool:
        """A brand-new trycloudflare hostname needs a few seconds to resolve; wait for it."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                socket.getaddrinfo(self.tunnel_host, 443)
                return True
            except socket.gaierror:
                time.sleep(2)
        return False

    def stop(self) -> None:
        for p in (self._cf, self._xray):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
        if self._decoy:
            self._decoy.shutdown()
        self.api.close()

    # ------------------------------------------------------------ users
    def add_client(self, uuid: str, email: str, cipher: str, padding: str) -> None:
        self.api.add_user("killdpi-in", email, uuid, cipher, padding)

    def remove_client(self, email: str) -> None:
        try:
            self.api.remove_user("killdpi-in", email)
        except Exception as e:  # noqa: BLE001
            log.warning("remove_user %s: %s", email, e)

    def client_count(self) -> int:
        return self.api.user_count("killdpi-in")

    def traffic(self, email: str) -> tuple[int, int]:
        return self.api.user_traffic(email)

    def online(self, email: str) -> int:
        try:
            return self.api.online(email)
        except Exception:  # noqa: BLE001
            return 0

    def total_traffic(self) -> tuple[int, int]:
        return self.api.inbound_traffic("killdpi-in")


def find_bin(name: str, search: tuple[str, ...] = ("bin", ".", "/usr/local/bin")) -> str:
    env = os.environ.get(name.upper() + "_BIN")
    if env and os.path.exists(env):
        return env
    for d in search:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return os.path.abspath(p)
    w = shutil.which(name)
    if w:
        return w
    raise FileNotFoundError(f"{name} binary not found (set {name.upper()}_BIN)")
