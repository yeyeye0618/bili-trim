# -*- coding: utf-8 -*-
"""
bili_device.py — 手機端的 B站 緩存：找得到、列得出、拉得下來、推得回去。

只依賴 adb 與標準庫。所有對外函式都丟例外（AdbError）而不是 sys.exit，
這樣 GUI 才能把錯誤顯示在畫面上而不是整個關掉。

緩存目錄長相（三種 B站 都一樣）：

    <root>/<avid>/<cid>/entry.json          ← 這支影片的中介資料
    <root>/<avid>/<cid>/danmaku.xml
    <root>/<avid>/<cid>/<type_tag>/index.json
    <root>/<avid>/<cid>/<type_tag>/video.m4s
    <root>/<avid>/<cid>/<type_tag>/audio.m4s
"""

import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import unicodedata
from pathlib import Path

import bili_env as ENV
from bili_font import clean_text


class AdbError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 已知的 B站 版本與緩存位置
# --------------------------------------------------------------------------- #

PACKAGES = (
    ("com.bilibili.app.in", "國際版 Bilibili"),
    ("tv.danmaku.bili", "大陸版 哔哩哔哩"),
    ("com.bilibili.app.blue", "概念版 哔哩哔哩"),
)

# {pkg} 會被套上套件名。順序有意義：先找到的優先。
# /sdcard 與 /storage/emulated/0 是同一個地方，所以同一個套件只留第一個找到的。
ROOT_TEMPLATES = (
    "/storage/emulated/0/Android/data/{pkg}/download",
    "/sdcard/Android/data/{pkg}/download",
    "/storage/emulated/0/Android/data/{pkg}/files/download",
    "/sdcard/Android/data/{pkg}/files/download",
)


# --------------------------------------------------------------------------- #
# 檔名
# --------------------------------------------------------------------------- #

_WIN_BAD = '<>:"/\\|?*'
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | \
                {"COM%d" % i for i in range(1, 10)} | \
                {"LPT%d" % i for i in range(1, 10)}


def safe_name(text, limit=80):
    """把影片標題變成可以當檔名的字串（Windows 規則最嚴，照它來）。"""
    text = unicodedata.normalize("NFC", text or "")
    text = "".join("_" if (c in _WIN_BAD or ord(c) < 32) else c for c in text)
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    if not text:
        text = "untitled"
    if len(text) > limit:
        text = text[:limit].rstrip().rstrip(".") + "~"
    if text.split(".")[0].upper() in _WIN_RESERVED:
        text = "_" + text
    return text


# --------------------------------------------------------------------------- #
# adb
# --------------------------------------------------------------------------- #

def adb_exe():
    return ENV.find("adb")


def _adb_env():
    """
    不讓 Git Bash / MSYS 把 /sdcard/... 這種參數改寫成 Windows 路徑。
    （實測不設這兩個，adb 會收到 C:/Program Files/Git/sdcard/...）
    """
    env = dict(os.environ)
    env["MSYS_NO_PATHCONV"] = "1"
    env["MSYS2_ARG_CONV_EXCL"] = "*"
    return env


_NO_WINDOW = 0
if os.name == "nt":
    _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _run(args, timeout=None):
    exe = adb_exe()
    if exe is None:
        raise AdbError(ENV.missing_hint("adb"))
    try:
        r = subprocess.run([exe] + list(args), capture_output=True,
                           env=_adb_env(), timeout=timeout,
                           creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise AdbError("adb %s 逾時。" % " ".join(args[:2]))
    out = r.stdout.decode("utf-8", "replace")
    err = r.stderr.decode("utf-8", "replace").strip()
    if r.returncode:
        raise AdbError("adb %s 失敗：%s" % (" ".join(args[:2]), err[:400] or "(無訊息)"))
    return out


def check_adb():
    """回傳 (可用, 說明文字)。給 GUI 開場時顯示。"""
    exe = adb_exe()
    if exe is None:
        return False, ENV.missing_hint("adb")
    try:
        ver = _run(["version"], timeout=20).splitlines()[0].strip()
    except AdbError as e:
        return False, str(e)
    return True, "%s（%s）" % (ver, exe)


def list_devices():
    """回傳 [Device, ...]，只含狀態為 device 的。"""
    out = _run(["devices", "-l"], timeout=30)
    found = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state != "device":
            continue
        model = ""
        for p in parts[2:]:
            if p.startswith("model:"):
                model = p[6:].replace("_", " ")
        found.append(Device(serial, model))
    return found


# --------------------------------------------------------------------------- #
# 無線偵錯（Wi-Fi）
# --------------------------------------------------------------------------- #
#
# Android 11 以上的「無線偵錯」讓 adb 改走 Wi-Fi，USB 能做的它都能做。
# 流程有兩段，各用一組不同的埠號，這是使用者最容易填錯的地方：
#
#   配對（只有第一次）：手機上「使用配對碼配對裝置」的視窗給一組 IP:埠號
#                       和 6 位數配對碼 -> adb pair
#   連線（之後每次）：  「無線偵錯」主畫面上的「IP 位址和通訊埠」 -> adb connect
#
# 手機重開機或換 Wi-Fi 之後無線偵錯會關掉、埠號會變；配對不用重做。
# 埠號可以不必手打：手機開著無線偵錯時會在區網廣播（mDNS），adb mdns services
# 看得到 —— 配對視窗開著的時候連配對用的那組也看得到。
#
# 藍芽沒有這條路：adb 只能走 USB 或網路。

_ADDR = re.compile(r"^\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})\s*:\s*([0-9]{1,5})\s*$")
_ADDR_ANY = re.compile(r"([0-9]{1,3}(?:\.[0-9]{1,3}){3}):([0-9]{1,5})")


def parse_addr(text):
    """「192.168.1.23:37165」-> 同一個字串（容許空白和全形冒號）；看不懂丟 ValueError。"""
    t = (text or "").strip().replace("：", ":").replace(" ", "")
    m = _ADDR.match(t)
    if not m:
        raise ValueError("要填「IP:埠號」，像 192.168.1.23:37165")
    if any(int(p) > 255 for p in m.group(1).split(".")):
        raise ValueError("IP 位址不對：%s" % m.group(1))
    port = int(m.group(2))
    if not 0 < port < 65536:
        raise ValueError("埠號不對：%s" % port)
    return "%s:%d" % (m.group(1), port)


def host_of(addr):
    return (addr or "").rsplit(":", 1)[0]


def _run_any(args, timeout):
    """跑 adb，不管結束碼，回傳 (結束碼, stdout+stderr)。"""
    exe = adb_exe()
    if exe is None:
        raise AdbError(ENV.missing_hint("adb"))
    try:
        r = subprocess.run([exe] + list(args), capture_output=True,
                           env=_adb_env(), timeout=timeout,
                           creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    text = (r.stdout + b"\n" + r.stderr).decode("utf-8", "replace").strip()
    return r.returncode, text


def pair(addr, code, timeout=30):
    """配對。成功就安靜回來，失敗丟 AdbError（訊息講下一步怎麼做）。"""
    addr = parse_addr(addr)
    code = re.sub(r"\D", "", code or "")
    if len(code) != 6:
        raise ValueError("配對碼是手機上顯示的 6 位數字")
    rc, out = _run_any(["pair", addr, code], timeout)
    if "successfully paired" in out.lower():
        return
    low = out.lower()
    if "wrong password" in low or "connection was dropped" in low:
        why = "配對碼不對，或手機上的配對視窗已經關了 —— 重新點一次「使用配對碼配對裝置」再試。"
    elif (rc == -1 or "unable to start pairing" in low or "failed to connect" in low
          or "protocol fault" in low or "cannot connect" in low):
        why = ("連不到手機的配對埠。確認：IP:埠號是「使用配對碼配對裝置」那個視窗裡的"
               "（不是無線偵錯主畫面那組）、視窗還開著、手機和電腦連同一個 Wi-Fi。")
    else:
        why = "adb 說：%s" % (out.splitlines()[-1] if out else "（沒有訊息）")
    raise AdbError("配對失敗。" + why)


def connect(addr, timeout=20):
    """連線。成功（或本來就連著）安靜回來，失敗丟 AdbError。"""
    addr = parse_addr(addr)
    rc, out = _run_any(["connect", addr], timeout)
    low = out.lower()
    if ("connected to" in low and "failed" not in low and "cannot" not in low
            and "unable" not in low):
        return addr
    if "authenticate" in low or "unauthorized" in low:
        why = "這台電腦還沒跟手機配對 —— 先做上面的「配對」。"
    # Windows 上 adb 直接轉述系統訊息（中文）加 socket 錯誤碼：10061 拒絕、10060 逾時
    elif "refused" in low or "10061" in low:
        why = ("手機拒絕連線。確認無線偵錯還開著，而且填的是「無線偵錯」主畫面上的"
               "「IP 位址和通訊埠」（埠號跟配對用的不一樣，而且手機重開後會變）。")
    elif (rc == -1 or "timed out" in low or "timeout" in low or "no route" in low
          or "10060" in low or "10065" in low or "10051" in low):
        why = "等不到手機回應。手機和電腦要連同一個 Wi-Fi，而且手機螢幕上無線偵錯要開著。"
    else:
        why = "adb 說：%s" % (out.splitlines()[-1] if out else "（沒有訊息）")
    raise AdbError("連線失敗。" + why)


def disconnect(serial):
    _run_any(["disconnect", serial], 15)


def parse_mdns(text):
    """adb mdns services 的輸出 -> {"pair": [(名稱, 位址)], "connect": [...]}。"""
    found = {"pair": [], "connect": []}
    for line in (text or "").splitlines():
        m = _ADDR_ANY.search(line)
        if not m:
            continue
        addr = "%s:%s" % (m.group(1), m.group(2))
        name = line.split()[0] if line.split() else ""
        if "_adb-tls-pairing." in line:
            found["pair"].append((name, addr))
        elif "_adb-tls-connect." in line:
            found["connect"].append((name, addr))
    return found


def discover(timeout=10):
    """區網上開著無線偵錯的手機。adb 太舊或 mDNS 不能用就回空的，不丟例外。"""
    try:
        rc, out = _run_any(["mdns", "services"], timeout)
    except AdbError:
        return {"pair": [], "connect": []}
    return parse_mdns(out)


# ---- 自己找埠號 ----
#
# 有些手機（實測：vivo）不在區網廣播無線偵錯的服務，有些網路（學校、公司）
# 也會擋廣播，上面的 discover() 就什麼都找不到。但手機的 IP 通常不變，只有
# 埠號會變（手機重開、無線偵錯關掉再開、在手機上點「重新連線」都會變）。
#
# 所以直接試那支手機的高位埠號（Android 的臨時埠範圍 32768–60999）。開著的
# 埠不一定是 adb，所以每個開著的埠都送一個 adb 的開頭封包（CNXN）：無線偵錯
# 會回 STLS（要求改走 TLS），舊式的 adb tcpip 會回 AUTH 或 CNXN，別的程式不會。
# 這個握手不會配對、不會授權任何東西，送完就斷。
#
# 速度（實測，一支 vivo 手機）：Windows 碰到「拒絕連線」會自己重試，一個關著的埠
# 要將近 2 秒才回報失敗；但開著的埠在區網裡幾十毫秒就連上。所以每個埠最多
# 等 0.3 秒、同時試 3000 個，整段 28k 個埠約 3 秒，找到 adb 就提早停（通常 2 秒）。
# 一定要快：掃得比手機螢幕自動熄滅還久的話，下面這個問題會讓它白掃。
#
# 實測到的陷阱：手機螢幕鎖過之後，有些手機（vivo）會悄悄停掉無線偵錯，
# 設定畫面卻還寫著開著、還顯示舊的埠號 —— 那個埠其實拒絕連線。這時候怎麼找
# 都找不到，只能在手機上把無線偵錯關掉再打開（會換一個新的埠號）。

A_CNXN, A_STLS, A_AUTH = 0x4E584E43, 0x534C5453, 0x48545541
PORT_LO, PORT_HI = 32768, 60999


def _cnxn_packet():
    data = b"host::\0"
    return struct.pack("<6I", A_CNXN, 0x01000001, 256 * 1024, len(data), 0,
                       A_CNXN ^ 0xFFFFFFFF) + data


def find_adb_ports(host, lo=PORT_LO, hi=PORT_HI, extra=(5555,), concurrency=3000,
                   timeout=0.3, stop_at_first=True, progress=None):
    """
    回傳這台主機上有 adb 在聽的埠號 [(埠號, "STLS"/"AUTH"/"CNXN"), ...]。
    progress(已試, 總數) 會被節流呼叫。在背景執行緒呼叫（會跑一陣子）。
    """
    import asyncio
    ports = list(extra) + [p for p in range(lo, hi + 1) if p not in extra]
    total = len(ports)
    found, state = [], {"next": 0, "done": 0, "stop": False, "t": 0.0}
    names = {A_STLS: "STLS", A_AUTH: "AUTH", A_CNXN: "CNXN"}
    packet = _cnxn_packet()

    async def probe(port):
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except Exception:
            return
        try:
            w.write(packet)
            await w.drain()
            hdr = await asyncio.wait_for(r.readexactly(24), 2.0)
            cmd = struct.unpack("<I", hdr[:4])[0]
            if cmd in names:
                found.append((port, names[cmd]))
                if stop_at_first:
                    state["stop"] = True
        except Exception:
            pass
        finally:
            try:
                w.close()
            except Exception:
                pass

    async def worker():
        while not state["stop"] and state["next"] < total:
            port = ports[state["next"]]
            state["next"] += 1
            await probe(port)
            state["done"] += 1
            now = time.time()
            if progress and now - state["t"] > 0.25:
                state["t"] = now
                progress(state["done"], total)

    async def main():
        await asyncio.gather(*(worker() for _ in range(min(concurrency, total))))

    asyncio.run(main())
    if progress:
        progress(total, total)
    return sorted(found)


def drop_stale(host=None):
    """
    adb 還掛著、但已經連不到的 Wi-Fi 連線（狀態 offline）先拿掉。不清的話，
    手機換了埠號之後 adb devices 會一直列著那條死掉的，看起來像連著。
    """
    try:
        out = _run(["devices"], timeout=15)
    except AdbError:
        return
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[1] == "device":
            continue
        serial = parts[0]
        if not (_ADDR.match(serial) or "._adb-tls-connect." in serial):
            continue
        if host is None or host_of(serial) == host:
            disconnect(serial)


def host_reachable(host, timeout_ms=800):
    """ping 一下。手機不在網路上就別花十幾秒掃它的埠。"""
    flag = ["-n", "1", "-w", str(timeout_ms)] if os.name == "nt" \
        else ["-c", "1", "-W", str(max(timeout_ms // 1000, 1))]
    try:
        r = subprocess.run(["ping"] + flag + [host], capture_output=True,
                           timeout=timeout_ms / 1000.0 + 3, creationflags=_NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 緩存項目
# --------------------------------------------------------------------------- #

class CacheRoot(object):
    def __init__(self, package, label, path):
        self.package = package
        self.label = label
        self.path = path

    def __repr__(self):
        return "<CacheRoot %s %s>" % (self.label, self.path)


class CacheItem(object):
    """一支緩存影片。entry 是 entry.json 原封不動的內容。"""

    def __init__(self, root, device_path, entry):
        self.root = root
        self.device_path = device_path.rstrip("/")
        self.entry = entry or {}
        e = self.entry
        page = e.get("page_data") or {}
        ep = e.get("ep") or {}

        self.avid = e.get("avid") or e.get("spid") or 0
        self.bvid = e.get("bvid") or ""
        self.cid = page.get("cid") or ep.get("cid") or 0
        # 標題裡常有變化選擇器之類的隱形字元（❤︎ 後面那個），字型沒有字形，
        # 畫出來就是個「?」。它們本來就看不見，清掉不會改變標題的樣子。
        self.title = clean_text((e.get("title") or "").strip())
        part = clean_text((page.get("part") or ep.get("index_title") or "").strip())
        self.part = part if part and part != self.title else ""
        self.owner = clean_text((e.get("owner_name") or "").strip())
        self.duration_ms = e.get("total_time_milli") or 0
        self.total_bytes = e.get("total_bytes") or 0
        self.downloaded_bytes = e.get("downloaded_bytes")
        self.quality_dir = str(e.get("type_tag") or "").strip()
        self.quality_desc = (e.get("quality_pithy_description") or "").strip()
        self.media_type = e.get("media_type")
        self.has_dash_audio = e.get("has_dash_audio")
        self.is_completed = e.get("is_completed") is not False
        self.cover = e.get("cover") or ""

    # ---- 顯示 ----

    @property
    def display_name(self):
        if self.part:
            return "%s - %s" % (self.title, self.part)
        return self.title or ("cid %s" % self.cid)

    @property
    def duration_s(self):
        return (self.duration_ms or 0) / 1000.0

    @property
    def quality_path(self):
        return "%s/%s" % (self.device_path, self.quality_dir)

    def remote_files(self):
        """這支影片要處理的檔案，相對於 cid 目錄。"""
        q = self.quality_dir
        return ["entry.json", "%s/index.json" % q,
                "%s/video.m4s" % q, "%s/audio.m4s" % q]

    @property
    def backup_stem(self):
        """備份檔名（不含副檔名）：用標題，不用 cid。"""
        stem = safe_name(self.display_name)
        return stem

    def unsupported_reason(self):
        """不是本工具能處理的格式時回傳原因，否則 None。"""
        if not self.quality_dir:
            return "entry.json 沒有 type_tag，找不到畫質目錄"
        if self.media_type not in (None, 2):
            return "media_type=%s，不是 DASH 緩存（舊版 FLV 分段格式不支援）" \
                   % self.media_type
        if self.has_dash_audio is False:
            return "這支沒有獨立音軌（has_dash_audio=false）"
        if not self.is_completed:
            return "還沒下載完（is_completed=false）"
        db, tb = self.downloaded_bytes, self.total_bytes
        if isinstance(db, int) and isinstance(tb, int) and 0 < db < tb:
            return "還沒下載完（%d / %d bytes）" % (db, tb)
        return None

    def __repr__(self):
        return "<CacheItem %s %s>" % (self.cid, self.display_name[:30])


# --------------------------------------------------------------------------- #
# 裝置
# --------------------------------------------------------------------------- #

class Device(object):
    def __init__(self, serial, model=""):
        self.serial = serial
        self.model = model

    def __repr__(self):
        return "<Device %s %s>" % (self.serial, self.model)

    @property
    def wireless(self):
        """用 Wi-Fi 連著的：序號是 IP:埠號，或 adb 自動連上時用的 mDNS 服務名稱。"""
        return bool(_ADDR.match(self.serial)) or "._adb-tls-connect." in self.serial

    @property
    def label(self):
        if self.wireless:
            where = "Wi-Fi %s" % host_of(self.serial) if _ADDR.match(self.serial) \
                else "Wi-Fi"
            return "%s（%s）" % (self.model, where) if self.model else where
        return "%s（%s）" % (self.serial, self.model) if self.model else self.serial

    # ---- 低階 ----

    def _args(self, *rest):
        return ["-s", self.serial] + list(rest)

    def shell(self, cmd, timeout=180):
        return _run(self._args("shell", cmd), timeout=timeout)

    def force_stop(self, package):
        """把 App 關掉，讓它下次開啟重新讀緩存目錄。"""
        self.shell("am force-stop %s" % package, timeout=30)

    # ---- 探測 ----

    def find_roots(self):
        """掃描三種 B站 的緩存目錄，回傳實際存在的那些。"""
        probes = []
        for pkg, label in PACKAGES:
            for tpl in ROOT_TEMPLATES:
                probes.append((pkg, label, tpl.format(pkg=pkg)))
        script = "".join('[ -d %s ] && echo OK %s;' % (p, p)
                         for _, _, p in probes)
        out = self.shell(script + " echo DONE", timeout=60)
        alive = set()
        for line in out.splitlines():
            if line.startswith("OK "):
                alive.add(line[3:].strip())
        roots, seen = [], set()
        for pkg, label, path in probes:
            if path in alive and pkg not in seen:
                seen.add(pkg)
                roots.append(CacheRoot(pkg, label, path))
        return roots

    def list_caches(self, roots, progress=None):
        """列出所有 root 底下的緩存影片。"""
        paths = []
        for root in roots:
            out = self.shell("find %s -maxdepth 3 -name entry.json 2>/dev/null; "
                             "echo DONE" % root.path, timeout=120)
            for line in out.splitlines():
                line = line.strip()
                if line.endswith("/entry.json"):
                    paths.append((root, line))
        if progress:
            progress(0, len(paths), "讀取 %d 支影片的中介資料" % len(paths))

        items, done = [], 0
        by_root = {}
        for root, p in paths:
            by_root.setdefault(id(root), (root, []))[1].append(p)
        for root, plist in by_root.values():
            texts = self._cat_many(plist)
            for p in plist:
                done += 1
                if progress:
                    progress(done, len(paths), "讀取中介資料 %d/%d" % (done, len(paths)))
                raw = texts.get(p)
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                items.append(CacheItem(root, p[:-len("/entry.json")], entry))

        items.sort(key=lambda x: -(x.entry.get("time_update_stamp") or 0))
        return items

    def _cat_many(self, paths, chunk=20):
        """
        一次 cat 多個檔案，用 @@@ 當分隔。路徑都是純數字與 c_ 開頭，
        不會有空白，所以不需要引號（避開 Windows -> adb -> sh 的三重跳脫）。
        """
        out = {}
        for i in range(0, len(paths), chunk):
            part = paths[i:i + chunk]
            cmd = "for f in %s; do echo @@@$f; cat $f; echo; done" % " ".join(part)
            txt = self.shell(cmd, timeout=120)
            cur, buf = None, []
            for line in txt.splitlines():
                if line.startswith("@@@"):
                    if cur is not None:
                        out[cur] = "\n".join(buf)
                    cur, buf = line[3:].strip(), []
                elif cur is not None:
                    buf.append(line)
            if cur is not None:
                out[cur] = "\n".join(buf)
        return out

    # ---- 查詢 ----

    def sizes(self, remote_paths):
        """{路徑: 位元組數}，讀不到的不會出現在結果裡。"""
        if not remote_paths:
            return {}
        # 注意：這裡是字串串接而不是 % 格式化，所以 %s/%n 只能寫一個百分號
        out = self.shell("stat -c '%s %n' " + " ".join(remote_paths), timeout=60)
        got = {}
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                got[parts[1].strip()] = int(parts[0])
        return got

    def md5s(self, remote_paths):
        """{路徑: md5}。在裝置上算，大檔會花幾十秒。"""
        if not remote_paths:
            return {}
        out = self.shell("md5sum " + " ".join(remote_paths), timeout=900)
        got = {}
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and len(parts[0]) == 32:
                got[parts[1].strip()] = parts[0].lower()
        return got

    def head_bytes(self, remote_path, n=262144):
        """
        讀裝置上某個檔案的前 n 個位元組（原始位元組，exec-out 不會動 CRLF）。
        用來在決定要不要拉整支影片之前，先看它的 ftyp/moov/sidx。
        """
        exe = adb_exe()
        if exe is None:
            raise AdbError("找不到 adb。")
        try:
            r = subprocess.run(
                [exe] + self._args("exec-out",
                                   "dd if=%s bs=%d count=1 2>/dev/null"
                                   % (remote_path, n)),
                capture_output=True, env=_adb_env(), timeout=120,
                creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise AdbError("讀取 %s 開頭逾時。" % remote_path)
        if r.returncode or not r.stdout:
            raise AdbError("讀不到 %s 的開頭：%s"
                           % (remote_path,
                              r.stderr.decode("utf-8", "replace").strip()[:200]))
        return r.stdout

    def exists(self, remote_path):
        out = self.shell("[ -e %s ] && echo YES || echo NO" % remote_path,
                         timeout=30)
        return "YES" in out

    # ---- 傳輸 ----

    def pull_file(self, remote, local, expect_size=None, progress=None,
                  label=""):
        """
        拉單一檔案。進度用「本機檔案長多大」來算 —— 比解析 adb 的輸出可靠，
        adb 的進度是用 \\r 刷同一行的，跨平台解析很脆。
        """
        local = Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.parent / (local.name + ".part")
        if tmp.exists():
            tmp.unlink()
        exe = adb_exe()
        if exe is None:
            raise AdbError("找不到 adb。")
        proc = subprocess.Popen(
            [exe] + self._args("pull", remote, str(tmp)),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_adb_env(), creationflags=_NO_WINDOW)
        self._watch(proc, lambda: tmp.stat().st_size if tmp.exists() else 0,
                    expect_size, progress, label)
        out, err = proc.communicate()
        if proc.returncode:
            if tmp.exists():
                tmp.unlink()
            raise AdbError("拉取 %s 失敗：%s"
                           % (remote, err.decode("utf-8", "replace").strip()[:300]))
        if expect_size is not None and tmp.stat().st_size != expect_size:
            got = tmp.stat().st_size
            tmp.unlink()
            raise AdbError("拉取 %s 不完整（裝置 %d bytes，拿到 %d）"
                           % (remote, expect_size, got))
        if local.exists():
            local.unlink()
        tmp.replace(local)
        if progress:
            progress(local.stat().st_size, expect_size or local.stat().st_size, label)

    def push_file(self, local, remote, progress=None, label=""):
        """推單一檔案。進度用裝置上的檔案大小來算。"""
        local = Path(local)
        total = local.stat().st_size
        exe = adb_exe()
        if exe is None:
            raise AdbError("找不到 adb。")
        proc = subprocess.Popen(
            [exe] + self._args("push", str(local), remote),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_adb_env(), creationflags=_NO_WINDOW)

        def on_device():
            try:
                return self.sizes([remote]).get(remote, 0)
            except AdbError:
                return 0

        # 推的時候每問一次 stat 就是一次 adb 往返，所以問得稀疏一點
        self._watch(proc, on_device, total, progress, label, interval=1.0)
        out, err = proc.communicate()
        if proc.returncode:
            raise AdbError("推送 %s 失敗：%s"
                           % (remote, err.decode("utf-8", "replace").strip()[:300]))
        if progress:
            progress(total, total, label)

    @staticmethod
    def _watch(proc, measure, total, progress, label, interval=0.35):
        """proc 跑的時候定期回報進度。"""
        if not progress or not total:
            return
        while proc.poll() is None:
            try:
                progress(min(measure(), total), total, label)
            except OSError:
                pass
            time.sleep(interval)

    # ---- 整支影片 ----

    def pull_cache(self, item, dest_dir, progress=None):
        """
        把一支緩存影片拉到 dest_dir/<cid 目錄名>，回傳本機路徑。
        只拉需要的四個檔案，外加 cid 目錄下的彈幕等小檔。
        """
        dest_dir = Path(dest_dir)
        local = dest_dir / item.device_path.rsplit("/", 1)[-1]
        rels = list(item.remote_files())
        # 彈幕與其他小檔（剪完要跟著平移）
        try:
            out = self.shell("ls -p %s" % item.device_path, timeout=30)
            for name in out.split():
                if not name.endswith("/") and name not in rels:
                    rels.append(name)
        except AdbError:
            pass

        remote_abs = ["%s/%s" % (item.device_path, r) for r in rels]
        sizes = self.sizes(remote_abs)
        grand = sum(sizes.get(p, 0) for p in remote_abs) or 1
        base = [0]

        for rel in rels:
            rp = "%s/%s" % (item.device_path, rel)
            if rp not in sizes:
                continue
            size = sizes[rp]
            start = base[0]

            def sub(done, _total, _label, start=start, rel=rel):
                if progress:
                    progress(start + done, grand, "拉取 %s" % rel)

            self.pull_file(rp, local / rel, expect_size=size, progress=sub,
                           label=rel)
            base[0] += size
            if progress:
                progress(base[0], grand, "拉取 %s" % rel)
        return local

    def push_cache(self, local, item, rels=None, progress=None):
        """
        把本機改好的檔案推回裝置，並在裝置上重算 md5 核對。
        回傳 [(rel, 本機 md5, 裝置 md5, 是否相符), ...]。
        """
        import hashlib
        local = Path(local)
        rels = list(rels or item.remote_files())
        rels = [r for r in rels if (local / r).is_file()]
        grand = sum((local / r).stat().st_size for r in rels) or 1
        base = [0]

        for rel in rels:
            size = (local / rel).stat().st_size
            start = base[0]

            def sub(done, _total, _label, start=start, rel=rel):
                if progress:
                    progress(start + done, grand, "推送 %s" % rel)

            self.push_file(local / rel, "%s/%s" % (item.device_path, rel),
                           progress=sub, label=rel)
            base[0] += size
            if progress:
                progress(base[0], grand, "推送 %s" % rel)

        big = [r for r in rels if r.endswith(".m4s")]
        if not big:
            return []
        if progress:
            progress(grand, grand, "在裝置上重算 md5 核對")
        dev = self.md5s(["%s/%s" % (item.device_path, r) for r in big])
        report = []
        for rel in big:
            h = hashlib.md5()
            with open(local / rel, "rb") as f:
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    h.update(b)
            mine = h.hexdigest()
            theirs = dev.get("%s/%s" % (item.device_path, rel), "")
            report.append((rel, mine, theirs, mine == theirs))
        return report


# --------------------------------------------------------------------------- #
# 本機暫存
# --------------------------------------------------------------------------- #

STAGE_SIDECAR = ".bili_gui_stage.json"


def stage_state(local):
    """讀暫存目錄旁邊記的狀態（原始 md5、剪過沒有）。"""
    p = Path(local) / STAGE_SIDECAR
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def write_stage_state(local, data):
    p = Path(local) / STAGE_SIDECAR
    merged = stage_state(local)
    merged.update(data)
    p.write_text(json.dumps(merged, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    return merged
