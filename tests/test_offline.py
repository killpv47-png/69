"""Offline tests (no network, no Telegram): link format, profiles, storage,
subscription content/stability, protobuf encoding for the Xray API, key derivation."""
import base64, json, os, shutil, sys, time, uuid
from urllib.parse import unquote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bot"))

from killdpi_link import KillDPIConfig, LinkError
from profiles import PROFILES, PROFILE_ORDER
from storage import LocalStorage
from subscription import DEFAULT_CLEAN_IPS, build_subscription, decode_subscription, INFO_UUID
import xray_api
from server import KillDPIServer, server_config

PBK = "SsRefZP5HZif8QcvncmOHqw23zTrBrjbnPcDYjBzsy0"
TUNNEL = "abc-def-ghi.trycloudflare.com"


def test_link_roundtrip_all_profiles():
    for k in PROFILE_ORDER:
        p = PROFILES[k]
        c = KillDPIConfig(uuid=str(uuid.uuid4()), host=TUNNEL, port=443, pbk=PBK, cipher=p.cipher, network="ws",
                          security="tls", sni=TUNNEL, ws_host=TUNNEL, path="/killdpi", padding=p.padding,
                          keepalive=p.keepalive, name=p.title)
        link = c.to_link()
        assert KillDPIConfig.from_link(link) == c, k
        ob = json.loads(c.to_client_json())["outbounds"][0]
        assert ob["protocol"] == "killdpi" and ob["settings"]["publicKey"] == PBK
        assert ob["streamSettings"]["network"] == "ws" and ob["streamSettings"]["tlsSettings"]["serverName"] == TUNNEL
        assert ob["streamSettings"]["wsSettings"]["host"] == TUNNEL
    print("ok  link roundtrip for", len(PROFILE_ORDER), "profiles")


def test_bad_links():
    u = uuid.uuid4()
    bad = ["vless://a@b:1", f"killdpi://not-uuid@h:1?pbk={PBK}", f"killdpi://{u}@h:1",
           f"killdpi://{u}@h:1?pbk=short", f"killdpi://{u}@h:1?pbk={PBK}&cipher=rc4", f"killdpi://{u}@h:1?pbk={PBK}&type=kcp"]
    for b in bad:
        try:
            KillDPIConfig.from_link(b); raise AssertionError("accepted: " + b)
        except LinkError:
            pass
    print("ok  rejected", len(bad), "malformed links with readable errors")


def test_subscription_content_and_countdown():
    from datetime import datetime, timezone
    now = time.time() + 30  # fixed "now" slightly ahead so rounding can't bite
    fixed = datetime.fromtimestamp(now, timezone.utc)
    user = {"configs": ["t1", "t2"]}
    configs = {"t1": {"uuid": str(uuid.uuid4()), "profile": "gaming"},
               "t2": {"uuid": str(uuid.uuid4()), "profile": "strong_dpi"}}
    rt = {"tunnel_host": TUNNEL, "deadline_ts": now + 5 * 3600 + 7 * 60, "started_ts": now}
    body, links = build_subscription(user, configs, rt, PBK, DEFAULT_CLEAN_IPS, max_ips=3)
    decoded = decode_subscription(body)
    assert decoded == links
    names = [unquote(l.split("#", 1)[1]) for l in decoded]
    # first entry is the live countdown with hours & minutes
    assert names[0].startswith("⏳") and "05h 07m" in names[0], names[0]
    # each config: 1 tunnel link + 3 clean-ip links, same protocol params, different names/hosts
    real = [KillDPIConfig.from_link(l) for l in decoded if INFO_UUID not in l]
    assert len(real) == 2 * (1 + 3), len(real)
    g = [c for c in real if c.uuid == configs["t1"]["uuid"]]
    assert {c.host for c in g} == {TUNNEL, *DEFAULT_CLEAN_IPS[:3]}
    assert all(c.sni == TUNNEL and c.ws_host == TUNNEL and c.cipher == "aes-256-gcm" and c.padding == "16-64" for c in g)
    assert len({c.name for c in g}) == 4
    # info entries are syntactically valid links (clients won't choke)
    for l in decoded:
        KillDPIConfig.from_link(l)
    # countdown updates when time passes
    rt["deadline_ts"] = now + 65
    body2, _ = build_subscription(user, configs, rt, PBK, max_ips=3, now=fixed)
    assert "00h 01m" in unquote(decode_subscription(body2)[0].split("#", 1)[1])
    # offline server -> no real links, only info
    body3, _ = build_subscription(user, configs, {"tunnel_host": "", "deadline_ts": 0}, PBK)
    assert all(INFO_UUID in l for l in decode_subscription(body3))
    print("ok  subscription: countdown name, 4 addresses per config, clean IPs, offline mode")


def test_storage_and_stable_sub_url():
    shutil.rmtree("data_local", ignore_errors=True)
    st = LocalStorage()
    user = {"configs": []}
    u1 = st.publish_subscription(user, "v1")
    u2 = st.publish_subscription(user, "v2")
    assert u1 == u2, "subscription URL must be stable across updates"
    assert open(u2[len("file://"):]).read() == "v2"
    state = st.empty_state(); state["users"]["1"] = {"configs": []}
    st.save_state(state); assert st.load_state() == state
    assert st.save_state(state) is False, "unchanged content must not be rewritten"
    assert st.dispatch_workflow("bot.yml", {"reason": "t"}) and st.dispatched[0]["inputs"]["reason"] == "t"
    assert st.set_secret("K", "v") and st.secrets["K"] == "v"
    shutil.rmtree("data_local", ignore_errors=True)
    print("ok  storage: stable sub URL, no-op writes, dispatch, secrets")


def test_protobuf_encoding():
    u = str(uuid.uuid4())
    msg = xray_api.user_message("a@b", u, "aes-256-gcm", "16-64", level=0)
    fields = xray_api._decode(msg)
    assert (2, 2, b"a@b") in fields
    acct = [v for n, w, v in fields if n == 3][0]
    tm = dict((n, v) for n, w, v in xray_api._decode(acct))
    assert tm[1] == b"xray.proxy.killdpi.Account"
    inner = dict((n, v) for n, w, v in xray_api._decode(tm[2]))
    assert inner == {1: u.encode(), 2: b"aes-256-gcm", 3: b"16-64"}
    assert xray_api._varint(300) == b"\xac\x02"
    print("ok  protobuf hand-encoding (User/TypedMessage/Account)")


def test_key_derivation_and_server_config():
    # public_key_of must match `xray killdpi-keygen` when the binary is available
    xr = next((p for p in (os.environ.get("XRAY_BIN", ""), os.path.join(HERE, "..", "bin", "xray")) if p and os.path.exists(p)), None)
    if xr:
        priv, pub = KillDPIServer.keygen(xr)
        assert KillDPIServer.public_key_of(priv) == pub, "pubkey derivation mismatch"
        print("ok  public key derivation matches xray killdpi-keygen")
    else:
        assert len(KillDPIServer.public_key_of("L16_xGyhSmcLJ9EtMSpijnbm5UaKU9xaD2luHzZzp_A")) == 43
        print("ok  public key derivation (no xray binary; length check only)")
    cfg = server_config("priv", [{"id": str(uuid.uuid4()), "email": "x@k"}], 8080, 10085, "/killdpi", 8081)
    ib = cfg["inbounds"][0]
    assert ib["protocol"] == "killdpi" and ib["settings"]["fallbacks"][0]["dest"] == 8081
    assert ib["streamSettings"]["network"] == "ws" and cfg["api"]["listen"].endswith(":10085")
    print("ok  server json (killdpi inbound, fallback, api)")


if __name__ == "__main__":
    test_link_roundtrip_all_profiles(); test_bad_links(); test_subscription_content_and_countdown()
    test_storage_and_stable_sub_url(); test_protobuf_encoding(); test_key_derivation_and_server_config()
    print("ALL OFFLINE TESTS PASSED")
