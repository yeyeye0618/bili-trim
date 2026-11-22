# -*- coding: utf-8 -*-
"""
bili_mdns.py — 自己在區網上找開著「無線偵錯」的手機（mDNS / DNS-SD），只用標準庫。

為什麼不直接用 adb mdns services：
  adb 自己的 mDNS 只綁一張網卡。電腦上有 VMware、Hyper-V、VPN 這類虛擬網卡時，
  它常常問錯網卡，什麼都看不到，也不會報錯。這裡對電腦上「每一張」IPv4 網卡
  各問一次。

問法有兩種，兩種都用：
  1. 從臨時埠送查詢。來源埠不是 5353 的查詢，RFC 6762 §6.7 規定回應方要用
     單播直接回到這個埠（legacy unicast），不必搶 5353。
  2. 自己也綁 5353（SO_REUSEADDR，跟 Windows 和 adb 共用）、加入群播，
     收群播的回應和手機自己發的公告。
  實測（vivo V2144）：「連線」服務兩種都會回，「配對」服務只用群播回 ——
  只做第 1 種的話永遠看不到配對視窗。

手機開著無線偵錯時會廣播兩種服務：
  _adb-tls-connect._tcp   連線用的那組 IP:埠號（無線偵錯主畫面上那組）
  _adb-tls-pairing._tcp   只有「配對視窗」開著的時候才有：配對碼視窗，
                          或掃了電腦上的 QR 圖碼之後（服務名稱 = QR 裡的 S）
"""

import random
import select
import socket
import struct
import time

MDNS_ADDR, MDNS_PORT = "224.0.0.251", 5353
CONNECT = "_adb-tls-connect._tcp.local"
PAIRING = "_adb-tls-pairing._tcp.local"
T_A, T_PTR, T_TXT, T_SRV, T_ANY = 1, 12, 16, 33, 255


class Service(object):
    """一個找到的服務。name 是完整的服務名稱（adb-XXXX-abc._adb-tls-connect._tcp.local）。"""

    def __init__(self, name, kind):
        self.name = name
        self.kind = kind            # "connect" / "pairing"
        self.target = ""            # SRV 指到的主機名稱
        self.port = 0
        self.ip = ""
        self.txt = {}
        self.via = ""               # 哪張網卡問到的（電腦這邊的 IP）
        self.seen = 0.0

    @property
    def instance(self):
        """
        服務名稱的第一段：adb-<序號>-<亂碼>，或 QR 配對時電腦自己取的名字。
        實測（vivo V2144）：QR 配對的名字會被手機包上一層雙引號 —— Android 把 QR
        裡的 S 當成 Wi-Fi 的 SSID 處理，SSID 習慣加引號 —— 所以這裡拿掉。
        """
        return self.name.split("._adb-tls-", 1)[0].strip('"')

    @property
    def serial_hint(self):
        """adb-<序號>-<亂碼> -> 序號（手機的 ro.serialno）；看不出來回空字串。"""
        inst = self.instance
        if inst.startswith("adb-") and inst.count("-") >= 2:
            return inst[4:].rsplit("-", 1)[0]
        return ""

    @property
    def addr(self):
        return "%s:%d" % (self.ip, self.port) if self.ip and self.port else ""

    def __repr__(self):
        return "<Service %s %s %s>" % (self.kind, self.instance, self.addr or "?")


# --------------------------------------------------------------------------- #
# DNS 封包
# --------------------------------------------------------------------------- #

def _enc_name(name):
    out = b""
    for label in name.rstrip(".").split("."):
        b = label.encode("utf-8")
        out += bytes([len(b)]) + b
    return out + b"\0"


def build_query(names, qtype=T_PTR):
    """一個標準查詢封包，可以一次問好幾個名稱。"""
    qid = random.randint(0, 0xFFFF)
    pkt = struct.pack(">6H", qid, 0, len(names), 0, 0, 0)
    for n in names:
        pkt += _enc_name(n) + struct.pack(">2H", qtype, 1)
    return pkt


def _read_name(buf, off, depth=0):
    """回傳 (名稱, 下一個位置)。處理壓縮指標。"""
    labels, jumped, end = [], False, off
    while True:
        if off >= len(buf) or depth > 20:
            raise ValueError("bad name")
        n = buf[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            ptr = ((n & 0x3F) << 8) | buf[off + 1]
            if not jumped:
                end = off + 2
            jumped = True
            off = ptr
            depth += 1
            continue
        labels.append(buf[off + 1:off + 1 + n].decode("utf-8", "replace"))
        off += 1 + n
    return ".".join(labels), (end if jumped else off)


def parse_packet(buf):
    """回傳 [(名稱, 型別, 資料), ...]，資料依型別解好：PTR/SRV/TXT/A。"""
    if len(buf) < 12:
        return []
    _, flags, qd, an, ns, ar = struct.unpack(">6H", buf[:12])
    if not flags & 0x8000:          # 不是回應
        return []
    off = 12
    for _ in range(qd):
        _, off = _read_name(buf, off)
        off += 4
    records = []
    for _ in range(an + ns + ar):
        name, off = _read_name(buf, off)
        rtype, _, _, rdlen = struct.unpack(">HHIH", buf[off:off + 10])
        off += 10
        rd = buf[off:off + rdlen]
        data = None
        try:
            if rtype == T_PTR:
                data, _ = _read_name(buf, off)
            elif rtype == T_SRV:
                _, _, port = struct.unpack(">3H", rd[:6])
                target, _ = _read_name(buf, off + 6)
                data = (target, port)
            elif rtype == T_A and rdlen == 4:
                data = socket.inet_ntoa(rd)
            elif rtype == T_TXT:
                txt, i = {}, 0
                while i < len(rd):
                    n = rd[i]
                    s = rd[i + 1:i + 1 + n].decode("utf-8", "replace")
                    k, _, v = s.partition("=")
                    if k:
                        txt[k] = v
                    i += 1 + n
                data = txt
        except Exception:
            data = None
        if data is not None:
            records.append((name, rtype, data))
        off += rdlen
    return records


# --------------------------------------------------------------------------- #
# 網卡
# --------------------------------------------------------------------------- #

def local_ipv4():
    """電腦上每一張網卡的 IPv4（不含 127.x 和 169.254.x 自動配的）。"""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except socket.gaierror:
        pass
    # 往外連一下看系統預設走哪張（不會真的送出封包）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ip for ip in ips
                  if not ip.startswith("127.") and not ip.startswith("169.254."))


def _same_subnet(ip, mine):
    """群播收到的封包看不出是哪張網卡，用前三段猜（家用網路幾乎都是 /24）。"""
    head = ip.rsplit(".", 1)[0] + "."
    return next((m for m in mine if m.startswith(head)), "")


# --------------------------------------------------------------------------- #
# 找手機
# --------------------------------------------------------------------------- #

class Browser(object):
    """
    在每張網卡上反覆問，把回應拼成服務清單。用法：

        b = Browser()
        b.poll(3.0)                 # 問 + 收 3 秒
        b.services("pairing")       # [Service, ...]
        b.close()

    執行緒不安全：同一時間只給一條執行緒用。
    """

    def __init__(self, names=(CONNECT, PAIRING)):
        self.names = list(names)
        self.socks = []
        self._svc = {}              # 服務名稱 -> Service
        self._srv = {}              # 服務名稱 -> (目標主機, 埠)
        self._addr = {}             # 主機名稱 -> IP
        self._txt = {}
        self.ips = local_ipv4()
        self.mcast = None
        try:
            m = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            m.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            m.bind(("", MDNS_PORT))
            m.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            joined = 0
            for ip in self.ips:
                try:
                    m.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                 socket.inet_aton(MDNS_ADDR) + socket.inet_aton(ip))
                    joined += 1
                except OSError:
                    pass
            if joined:
                m.setblocking(False)
                self.mcast = m
            else:
                m.close()
        except OSError:
            self.mcast = None
        for ip in self.ips:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                             socket.inet_aton(ip))
                s.bind((ip, 0))
                s.setblocking(False)
                self.socks.append((ip, s))
            except OSError:
                pass

    def _all_socks(self):
        return [s for _, s in self.socks] + ([self.mcast] if self.mcast else [])

    def close(self):
        if self.mcast:
            try:
                self.mcast.close()
            except OSError:
                pass
            self.mcast = None
        for _, s in self.socks:
            try:
                s.close()
            except OSError:
                pass
        self.socks = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def query(self):
        pkt = build_query(self.names)
        for _, s in self.socks:
            try:
                s.sendto(pkt, (MDNS_ADDR, MDNS_PORT))
            except OSError:
                pass
        if self.mcast:                  # 從 5353 問，回應會用群播（每張網卡各送一次）
            for ip in self.ips:
                try:
                    self.mcast.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                          socket.inet_aton(ip))
                    self.mcast.sendto(pkt, (MDNS_ADDR, MDNS_PORT))
                except OSError:
                    pass

    def _resolve_more(self, s):
        """有 PTR 但還缺 SRV / A 的，補問一次。"""
        need = [n for n, sv in self._svc.items() if not sv.port]
        hosts = [t for t, _ in self._srv.values() if t not in self._addr]
        try:
            if need:
                s.sendto(build_query(need, T_ANY), (MDNS_ADDR, MDNS_PORT))
            if hosts:
                s.sendto(build_query(hosts, T_A), (MDNS_ADDR, MDNS_PORT))
        except OSError:
            pass

    def _absorb(self, buf, via):
        now = time.time()
        for name, rtype, data in parse_packet(buf):
            low = name.lower()
            if rtype == T_PTR:
                for svc_type, kind in ((CONNECT, "connect"), (PAIRING, "pairing")):
                    if low == svc_type.lower():
                        sv = self._svc.get(data)
                        if sv is None:
                            sv = self._svc[data] = Service(data, kind)
                        sv.via, sv.seen = via, now
            elif rtype == T_SRV:
                self._srv[name] = data
            elif rtype == T_A:
                self._addr[name.lower()] = data
            elif rtype == T_TXT:
                self._txt[name] = data
        for name, sv in self._svc.items():
            if name in self._srv:
                sv.target, sv.port = self._srv[name]
            if sv.target and sv.target.lower() in self._addr:
                sv.ip = self._addr[sv.target.lower()]
            if name in self._txt:
                sv.txt = self._txt[name]

    def poll(self, seconds=2.0, every=0.7):
        """問 + 收 seconds 秒，每 every 秒重問一次（UDP 會掉）。"""
        end = time.time() + seconds
        next_q = 0.0
        while True:
            now = time.time()
            if now >= end:
                break
            if now >= next_q:
                self.query()
                for _, s in self.socks:
                    self._resolve_more(s)
                next_q = now + every
            socks = self._all_socks()
            if not socks:
                time.sleep(min(end, next_q) - now)
                continue
            r, _, _ = select.select(socks, [], [], max(0.0, min(end, next_q) - now))
            for s in r:
                via = next((ip for ip, x in self.socks if x is s), "")
                while True:
                    try:
                        buf, src = s.recvfrom(9000)
                    except (BlockingIOError, OSError):
                        break
                    try:
                        self._absorb(buf, via or _same_subnet(src[0], self.ips))
                    except Exception:
                        pass
        return self.services()

    def services(self, kind=None, max_age=None):
        now = time.time()
        return [sv for sv in self._svc.values()
                if sv.addr and (kind is None or sv.kind == kind)
                and (max_age is None or now - sv.seen <= max_age)]

    def forget(self, kind=None):
        """把舊的結果清掉（例如配對視窗關了之後，那個服務就不在了）。"""
        for name in [n for n, sv in self._svc.items() if kind is None or sv.kind == kind]:
            del self._svc[name]
            self._srv.pop(name, None)


def browse(seconds=3.0):
    with Browser() as b:
        return b.poll(seconds)


if __name__ == "__main__":
    print("網卡：", local_ipv4())
    for sv in browse(4.0):
        print(sv.kind, sv.instance, sv.addr, "via", sv.via, sv.txt)
