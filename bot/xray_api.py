"""Minimal gRPC client for the Xray API (HandlerService / StatsService).

`xray api adu` cannot build KILLDPI inbounds, so the bot talks to the gRPC
API directly. Messages are hand-encoded protobuf (only varints and
length-delimited fields are needed), so no generated stubs are required.

    AlterInbound(tag, AddUserOperation{User{level, email, account=TypedMessage{
        type="xray.proxy.killdpi.Account", value=Account{id, cipher, padding}}}})
"""
from __future__ import annotations

import grpc


# ------------------------------------------------------------------ protobuf helpers
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field_varint(num: int, value: int) -> bytes:
    return _varint((num << 3) | 0) + _varint(value)


def _field_bytes(num: int, value: bytes | str) -> bytes:
    if isinstance(value, str):
        value = value.encode()
    if not value:
        return b""
    return _varint((num << 3) | 2) + _varint(len(value)) + value


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    n = 0
    while True:
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7


def _decode(buf: bytes) -> list[tuple[int, int, int | bytes]]:
    """Return [(field_number, wire_type, value)] for a flat message."""
    out = []
    i = 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _read_varint(buf, i)
        elif wt == 2:
            ln, i = _read_varint(buf, i)
            v = buf[i:i + ln]
            i += ln
        elif wt == 1:
            v = int.from_bytes(buf[i:i + 8], "little"); i += 8
        elif wt == 5:
            v = int.from_bytes(buf[i:i + 4], "little"); i += 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        out.append((num, wt, v))
    return out


def typed_message(type_name: str, value: bytes) -> bytes:
    return _field_bytes(1, type_name) + _field_bytes(2, value)


def killdpi_account(uuid: str, cipher: str = "", padding: str = "") -> bytes:
    return _field_bytes(1, uuid) + _field_bytes(2, cipher) + _field_bytes(3, padding)


def user_message(email: str, uuid: str, cipher: str = "", padding: str = "", level: int = 0) -> bytes:
    acct = typed_message("xray.proxy.killdpi.Account", killdpi_account(uuid, cipher, padding))
    return _field_varint(1, level) + _field_bytes(2, email) + _field_bytes(3, acct)


# ------------------------------------------------------------------ client
class XrayAPI:
    def __init__(self, address: str = "127.0.0.1:10085", timeout: float = 5.0):
        self.address = address
        self.timeout = timeout
        self._chan = grpc.insecure_channel(address)

    def _call(self, method: str, payload: bytes) -> bytes:
        fn = self._chan.unary_unary(method, request_serializer=lambda b: b, response_deserializer=lambda b: b)
        return fn(payload, timeout=self.timeout)

    def add_user(self, tag: str, email: str, uuid: str, cipher: str = "", padding: str = "") -> None:
        op = typed_message("xray.app.proxyman.command.AddUserOperation", _field_bytes(1, user_message(email, uuid, cipher, padding)))
        req = _field_bytes(1, tag) + _field_bytes(2, op)
        self._call("/xray.app.proxyman.command.HandlerService/AlterInbound", req)

    def remove_user(self, tag: str, email: str) -> None:
        op = typed_message("xray.app.proxyman.command.RemoveUserOperation", _field_bytes(1, email))
        req = _field_bytes(1, tag) + _field_bytes(2, op)
        self._call("/xray.app.proxyman.command.HandlerService/AlterInbound", req)

    def user_count(self, tag: str) -> int:
        resp = self._call("/xray.app.proxyman.command.HandlerService/GetInboundUsersCount", _field_bytes(1, tag))
        for num, wt, v in _decode(resp):
            if num == 1 and wt == 0:
                return int(v)
        return 0

    def query_stats(self, pattern: str = "", reset: bool = False) -> dict[str, int]:
        req = _field_bytes(1, pattern) + (_field_varint(2, 1) if reset else b"")
        resp = self._call("/xray.app.stats.command.StatsService/QueryStats", req)
        out: dict[str, int] = {}
        for num, wt, v in _decode(resp):
            if num == 1 and wt == 2:
                name, value = "", 0
                for n2, w2, v2 in _decode(v):
                    if n2 == 1:
                        name = v2.decode()
                    elif n2 == 2:
                        value = int(v2)
                out[name] = value
        return out

    def online(self, email: str) -> int:
        resp = self._call("/xray.app.stats.command.StatsService/GetStatsOnline", _field_bytes(1, f"user>>>{email}>>>online"))
        for num, wt, v in _decode(resp):
            if num == 1 and wt == 2:
                for n2, w2, v2 in _decode(v):
                    if n2 == 2:
                        return int(v2)
        return 0

    def sys_stats(self) -> dict[str, int]:
        resp = self._call("/xray.app.stats.command.StatsService/GetSysStats", b"")
        names = {1: "goroutines", 2: "gc", 3: "alloc", 4: "total_alloc", 5: "sys", 6: "mallocs", 7: "frees",
                 8: "live_objects", 9: "pause_total_ns", 10: "uptime"}
        return {names.get(n, str(n)): int(v) for n, wt, v in _decode(resp) if wt == 0}

    def user_traffic(self, email: str) -> tuple[int, int]:
        st = self.query_stats(f"user>>>{email}>>>")
        return st.get(f"user>>>{email}>>>traffic>>>uplink", 0), st.get(f"user>>>{email}>>>traffic>>>downlink", 0)

    def inbound_traffic(self, tag: str) -> tuple[int, int]:
        st = self.query_stats(f"inbound>>>{tag}>>>")
        return st.get(f"inbound>>>{tag}>>>traffic>>>uplink", 0), st.get(f"inbound>>>{tag}>>>traffic>>>downlink", 0)

    def close(self) -> None:
        self._chan.close()
