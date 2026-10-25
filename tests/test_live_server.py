"""Live test (needs internet + bin/xray + bin/cloudflared): starts the real server stack,
adds a client over gRPC, connects through Cloudflare (tunnel host AND a clean IP) with a
KILLDPI client built from the *subscription link*, and fetches a URL through it."""
import json, os, subprocess, sys, time, uuid, socket
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.join(HERE, "..", "bot"))
from server import KillDPIServer
from subscription import build_subscription, decode_subscription, INFO_UUID
from killdpi_link import KillDPIConfig
import httpx

XRAY = os.environ.get("XRAY_BIN", os.path.join(HERE, "..", "bin", "xray"))
CF = os.environ.get("CLOUDFLARED_BIN", os.path.join(HERE, "..", "bin", "cloudflared"))
priv, pub = KillDPIServer.keygen(XRAY)
srv = KillDPIServer(XRAY, CF, priv, workdir="/tmp/killdpi-live")
host = srv.start([])
print("tunnel:", host)
assert srv.probe(attempts=10), "decoy not reachable through the tunnel"
print("ok  decoy reachable via tunnel (active-probe fallback)")

uid = str(uuid.uuid4()); token = "livetest"
srv.add_client(uid, f"{token}@killdpi", "aes-256-gcm", "16-64")
assert srv.client_count() == 2  # placeholder + ours
print("ok  client added over gRPC")

rt = {"tunnel_host": host, "deadline_ts": time.time() + 3600, "started_ts": time.time()}
body, _ = build_subscription({"configs": [token]}, {token: {"uuid": uid, "profile": "gaming"}}, rt, pub, max_ips=2)
real = [l for l in decode_subscription(body) if INFO_UUID not in l]
print("subscription has", len(real), "real links")

def run_client(link, port):
    cfg = KillDPIConfig.from_link(link)
    js = json.loads(cfg.to_client_json(socks_port=port))
    p = f"/tmp/killdpi-live/client-{port}.json"; json.dump(js, open(p, "w"))
    proc = subprocess.Popen([XRAY, "run", "-c", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        if socket.socket().connect_ex(("127.0.0.1", port)) == 0: break
        time.sleep(0.2)
    return proc

results = []
for i, link in enumerate(real):
    cfg = KillDPIConfig.from_link(link)
    proc = run_client(link, 20800 + i)
    try:
        r = httpx.get("https://api.ipify.org", proxy=f"socks5://127.0.0.1:{20800 + i}", timeout=25)
        ok = r.status_code == 200
        results.append(ok)
        print(("ok " if ok else "FAIL"), f"{cfg.name!r} via {cfg.host} -> {r.text.strip() if ok else r.status_code}")
    except Exception as e:
        results.append(False); print("FAIL", cfg.name, cfg.host, e)
    finally:
        proc.terminate()

up, down = srv.traffic(f"{token}@killdpi")
print(f"traffic for client: up={up} down={down}")
assert up > 0 and down > 0, "stats not counted"
srv.remove_client(f"{token}@killdpi"); assert srv.client_count() == 1
print("ok  client removed")
srv.stop()
assert results[0], "tunnel-host link must work"
assert any(results[1:]), "at least one clean-IP link must work"
print("LIVE SERVER TEST PASSED")
