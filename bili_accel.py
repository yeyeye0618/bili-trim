"""
電腦代下載：手機 App 下載很慢的緩存，改由電腦把同一份檔案下載好、推回手機。

做法（2026-11 在國際版 6.9.0 上實測）：

1. 使用者在 App 裡按緩存。App 立刻把這支的下載網址、大小、md5 寫進
   <cid 目錄>/<type_tag>/index.json，網址大約兩小時後過期（deadline 參數）。
2. 這裡把 App 關掉（等於暫停下載），照 index.json 在電腦上下載同一份檔案，
   核對大小和 md5。
3. 刪掉手機上下載到一半的暫存檔，把檔案推成 video.m4s / audio.m4s。
4. 使用者在 App 裡按「繼續」，App 發現檔案已經在、md5 也對，直接判定完成。

App 只沿用「跟伺服器那份一模一樣」的檔案：它下載前會比 md5，下載後也會再驗。
所以這裡只能下載 index.json 指定的那份，不能拿別的來源（例如電腦版客戶端的
緩存，那是另一種編碼）代替。

整個過程沒有離開 App 的緩存：電腦上的檔案推完就刪，不另存。
"""

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import bili_device as DEV
import bili_env as ENV

# App 自己下載用的 User-Agent。CDN 對這個網址不看 Cookie，看簽名參數。
USER_AGENT = "Bilibili Freedoooooom/MarkII"

# 網址剩不到這麼多秒就不下了：下載到一半過期只會白跑。
MIN_URL_LIFE = 120

STREAM_NAMES = {"video": "video.m4s", "audio": "audio.m4s"}


class AccelError(RuntimeError):
    pass


class Stream(object):
    """index.json 裡的一條串流（視訊或音訊）。"""

    def __init__(self, kind, raw):
        self.kind = kind
        self.name = STREAM_NAMES[kind]
        self.id = raw.get("id")
        urls = [raw.get("base_url") or ""] + list(raw.get("backup_url") or [])
        self.urls = [u for u in urls if u]
        self.size = int(raw.get("size") or 0)
        self.md5 = (raw.get("md5") or "").lower()

    @property
    def deadline(self):
        """網址最早什麼時候過期（unix 秒）；看不出來就 None。"""
        best = None
        for u in self.urls:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
            for key in ("deadline", "e_deadline"):
                v = (q.get(key) or [""])[0]
                if v.isdigit():
                    best = int(v) if best is None else max(best, int(v))
        return best

    def __repr__(self):
        return "<Stream %s id=%s %d bytes>" % (self.kind, self.id, self.size)


# --------------------------------------------------------------------------- #
# 哪些可以代下載
# --------------------------------------------------------------------------- #

def is_pending(item):
    """這支是不是還沒下載完、而且是我們處理得了的 DASH 緩存。"""
    if item.is_completed:
        return False
    if not item.quality_dir:
        return False
    if item.media_type not in (None, 2):
        return False
    return True


def pending_items(items):
    return [it for it in items if is_pending(it)]


def parse_index(text):
    """index.json 的文字 → [Stream, ...]（視訊在前）。"""
    try:
        data = json.loads(text)
    except ValueError:
        raise AccelError("index.json 不是合法的 JSON —— App 可能還沒寫完，"
                         "在 App 裡按一下繼續、等幾秒再暫停。")
    out = []
    for kind in ("video", "audio"):
        rows = data.get(kind) or []
        if rows:
            out.append(Stream(kind, rows[0]))
    if not out or out[0].kind != "video":
        raise AccelError("index.json 裡沒有視訊串流（可能是舊版 FLV 格式，不支援）。")
    for s in out:
        if not s.urls:
            raise AccelError("index.json 的 %s 沒有下載網址。" % s.kind)
        if s.size <= 0:
            raise AccelError("index.json 的 %s 沒有檔案大小。" % s.kind)
        if not re.fullmatch(r"[0-9a-f]{32}", s.md5):
            raise AccelError("index.json 的 %s 沒有 md5，沒辦法確認下載的檔案"
                             "跟 App 要的一樣。" % s.kind)
    return out


def read_streams(device, item):
    text = device.shell("cat %s/index.json" % item.quality_path, timeout=60)
    if not text.strip() or "No such file" in text:
        raise AccelError("手機上還沒有 index.json —— 在 App 裡按緩存，"
                         "等它開始下載再回來。")
    return parse_index(text)


def check_fresh(streams, now=None):
    """網址快過期就丟錯。回傳還剩幾秒（看不出來就 None）。"""
    now = time.time() if now is None else now
    ds = [s.deadline for s in streams if s.deadline]
    if not ds:
        return None
    left = min(ds) - now
    if left < MIN_URL_LIFE:
        raise AccelError(
            "這支的下載網址已經過期（App 大約兩小時換一次）。在 App 的「下載中」"
            "按一下這支讓它繼續、看到開始下載就暫停，再回來按一次代下載。")
    return left


# --------------------------------------------------------------------------- #
# 下載
# --------------------------------------------------------------------------- #

def file_md5(path, progress=None):
    h = hashlib.md5()
    total = os.path.getsize(str(path)) or 1
    done = 0
    with open(str(path), "rb") as f:
        while True:
            b = f.read(4 << 20)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress:
                progress(done, total)
    return h.hexdigest()


_SIZE = re.compile(r"\[#\w+\s+([\d.]+)([KMG]i)?B/([\d.]+)([KMG]i)?B")
_UNIT = {None: 1, "Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30}


def _aria2_download(exe, stream, dest, progress):
    """用 aria2 多連線下載。網址都給它，它會把備用網址當鏡像一起用。"""
    cmd = [str(exe), "-x", "16", "-s", "16", "-k", "1M",
           "--file-allocation=none", "--allow-overwrite=true",
           "--auto-file-renaming=false", "--continue=false",
           "--max-tries=5", "--retry-wait=2",
           "--summary-interval=1", "--console-log-level=warn",
           "--download-result=hide", "--enable-color=false",
           "-U", USER_AGENT, "-d", str(dest.parent), "-o", dest.name] \
        + stream.urls
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            creationflags=DEV._NO_WINDOW)
    tail = []
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode("utf-8", "replace").strip()
        if line:
            tail.append(line)
            del tail[:-8]
        m = _SIZE.search(line)
        if m and progress:
            done = float(m.group(1)) * _UNIT[m.group(2)]
            progress(min(int(done), stream.size), stream.size)
    proc.wait()
    if proc.returncode:
        raise AccelError("aria2 下載 %s 失敗（代碼 %d）：\n  %s"
                         % (stream.name, proc.returncode, "\n  ".join(tail)))


def _open(url, start, end, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Range": "bytes=%d-%d" % (start, end)})
    return urllib.request.urlopen(req, timeout=timeout)


def _py_download(stream, dest, progress, threads=8, piece=8 << 20):
    """沒有 aria2 時的備案：標準庫，分段平行下載。"""
    size = stream.size
    with open(str(dest), "wb") as f:
        f.truncate(size)
    pieces = [(a, min(a + piece, size) - 1) for a in range(0, size, piece)]
    lock = threading.Lock()
    state = {"done": 0, "next": 0, "error": None}

    def take():
        with lock:
            if state["error"] is not None or state["next"] >= len(pieces):
                return None
            p = pieces[state["next"]]
            state["next"] += 1
            return p

    def worker():
        with open(str(dest), "r+b") as out:
            while True:
                p = take()
                if p is None:
                    return
                start, end = p
                last = None
                for attempt in range(6):
                    url = stream.urls[attempt % len(stream.urls)]
                    pos = start
                    try:
                        with _open(url, pos, end) as r:
                            if r.status != 206:
                                raise AccelError("伺服器不支援分段下載（HTTP %d）"
                                                 % r.status)
                            out.seek(pos)
                            while pos <= end:
                                b = r.read(min(1 << 18, end - pos + 1))
                                if not b:
                                    break
                                out.write(b)
                                pos += len(b)
                                with lock:
                                    state["done"] += len(b)
                        if pos > end:
                            break
                        last = AccelError("連線中斷（%d / %d）" % (pos - start,
                                                                  end - start + 1))
                    except (urllib.error.URLError, OSError, AccelError) as e:
                        last = e
                    with lock:
                        state["done"] -= pos - start   # 這段重來
                    time.sleep(1.5 * (attempt + 1))
                else:
                    with lock:
                        state["error"] = AccelError(
                            "下載 %s 的 %d-%d 失敗：%s"
                            % (stream.name, start, end, last))
                    return

    ts = [threading.Thread(target=worker, daemon=True)
          for _ in range(min(threads, len(pieces)))]
    for t in ts:
        t.start()
    while any(t.is_alive() for t in ts):
        if progress:
            progress(state["done"], size)
        time.sleep(0.3)
    if state["error"] is not None:
        raise state["error"]


def download(stream, dest, progress=None, log=None):
    """下載一條串流到 dest，核對大小和 md5。回傳下載花的秒數。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if part.exists():
        part.unlink()
    exe = ENV.find("aria2c")
    t0 = time.time()
    if exe is not None:
        _aria2_download(exe, stream, part, progress)
    else:
        if log:
            log("    沒有 aria2，用內建的下載器（8 條連線）。")
        _py_download(stream, part, progress)
    took = time.time() - t0
    got = part.stat().st_size if part.exists() else 0
    if progress:
        progress(min(got, stream.size), stream.size)
    if got != stream.size:
        if part.exists():
            part.unlink()
        raise AccelError("%s 下載不完整：應該 %d bytes，拿到 %d。"
                         % (stream.name, stream.size, got))
    md5 = file_md5(part)
    if md5 != stream.md5:
        part.unlink()
        raise AccelError("%s 的 md5 跟 App 記的不一樣（%s ≠ %s），不推過去。"
                         % (stream.name, md5, stream.md5))
    if dest.exists():
        dest.unlink()
    part.replace(dest)
    return took


# --------------------------------------------------------------------------- #
# 整支影片
# --------------------------------------------------------------------------- #

def _check_path(item):
    """只動 B站 緩存目錄底下這支影片的畫質目錄，路徑不對就不做。"""
    q = item.quality_path
    root = (item.root.path if item.root else "").rstrip("/")
    if not root or not q.startswith(root + "/") or " " in q or ".." in q:
        raise AccelError("這支的路徑看起來不對，不動它：%s" % q)
    return q


def device_leftovers(device, qdir):
    """畫質目錄裡 App 下載到一半留下的暫存檔（video.m4s.temp 之類）。"""
    out = device.shell("ls %s" % qdir, timeout=30)
    names = []
    for name in out.split():
        for base in STREAM_NAMES.values():
            if name.startswith(base + ".") and "/" not in name:
                names.append(name)
    return names


def accelerate(device, item, work_dir, progress=None, log=None):
    """
    幫一支沒下載完的緩存把檔案下載好、推回手機。
    回傳 {"bytes": 總位元組, "download_s": 秒, "push_s": 秒}。
    progress(done, total, label)；log(text)。
    """
    log = log or (lambda s: None)
    package = item.root.package
    qdir = _check_path(item)

    streams = read_streams(device, item)
    left = check_fresh(streams)
    total = sum(s.size for s in streams)
    log("  要下載：%s" % "、".join(
        "%s %s" % ("視訊" if s.kind == "video" else "音訊", _fmt(s.size))
        for s in streams))
    if left is not None:
        log("  下載網址還有 %d 分鐘過期。" % (left // 60))

    # 先關 App：手機就不會跟電腦搶著下載，也不會再寫暫存檔
    device.force_stop(package)
    log("  已經關掉 B站 App（等於暫停手機上的下載）。")

    local_dir = Path(work_dir) / ("accel_%s" % item.cid)
    done_before = [0]
    dl_s = 0.0
    try:
        for s in streams:
            start = done_before[0]

            def sub(done, _t, start=start, s=s):
                if progress:
                    progress(start + done, total, "電腦下載 %s" % s.name)

            took = download(s, local_dir / s.name, progress=sub, log=log)
            dl_s += took
            done_before[0] += s.size
            log("  %s 下載完（%.0f 秒，%s/s），md5 核對相符。"
                % (s.name, took, _fmt(s.size / max(took, 0.1))))

        # 下載期間使用者可能又打開了 App：推之前再關一次
        device.force_stop(package)
        junk = device_leftovers(device, qdir)
        if junk:
            device.shell("rm -f %s" % " ".join(
                "%s/%s" % (qdir, n) for n in junk), timeout=60)
            log("  刪掉手機上下載到一半的暫存檔：%s" % "、".join(junk))

        t0 = time.time()
        pushed = [0]
        for s in streams:
            start = pushed[0]

            def sub(done, _t, _label, start=start, s=s):
                if progress:
                    progress(start + done, total, "推到手機 %s" % s.name)

            device.push_file(local_dir / s.name, "%s/%s" % (qdir, s.name),
                             progress=sub, label=s.name)
            pushed[0] += s.size
        remote = ["%s/%s" % (qdir, s.name) for s in streams]
        device.shell("chmod 666 %s" % " ".join(remote), timeout=30)
        push_s = time.time() - t0

        if progress:
            progress(total, total, "在手機上核對 md5")
        got = device.md5s(remote)
        for s, r in zip(streams, remote):
            if got.get(r) != s.md5:
                raise AccelError("手機上的 %s md5 不符（%s），App 會重新下載它。"
                                 % (s.name, got.get(r) or "讀不到"))
        log("  推到手機（%.0f 秒），手機上重算 md5 也相符。" % push_s)
    finally:
        for s in streams:
            for p in (local_dir / s.name, local_dir / (s.name + ".part")):
                try:
                    p.unlink()
                except OSError:
                    pass
        try:
            local_dir.rmdir()
        except OSError:
            pass
    return {"bytes": total, "download_s": dl_s, "push_s": push_s}


def open_app(device, package):
    """
    把 App 打開，讓使用者去「下載中」按繼續。
    離線緩存頁的路由（bilibili://offline/cache/...）只在 App 內部用，
    從外面 am start 會 unable to resolve，所以只能開主畫面。
    """
    device.shell("monkey -p %s -c android.intent.category.LAUNCHER 1"
                 % package, timeout=30)


def _fmt(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1024.0
