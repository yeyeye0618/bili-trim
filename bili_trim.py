# -*- coding: utf-8 -*-
"""
bili_trim.py — 剪掉 B站 Android 緩存影片裡的工商片段，產物仍能在 B站 App 播放。

為什麼不是用 ffmpeg 重新封裝
---------------------------
實測（逐項二分搜尋）B站 App 對緩存檔的容忍度：

    改內容位元組（翻轉 1 byte）............ 可以播
    檔尾附加位元組（檔案變大）............. 可以播
    改 entry.json 的 total_bytes / 時長 ... 可以播
    改檔案內的 mvhd / tkhd / mehd ........ 可以播
    改 moof 裡的時間數值（tfdt / trun）... 可以播
    減少 sidx 的參考項（大小不變）......... 可以播
    ffmpeg 重新封裝（即使完全不剪）........ 卡在「正在緩衝」
    移除 fragment 讓檔案變小 .............. 卡在「正在緩衝」

結論有兩點：
  1. ffmpeg 重封裝出來的容器會被拒絕（它的 +empty_moov 不寫 mehd/elst、
     mvhd/tkhd 時長留 0、sidx 是 version 1 ……整體就是跟 B站 不同的檔案）。
     所以本工具完全不呼叫 ffmpeg 改容器，而是直接在原檔上動刀。
  2. 檔案不能比原本小。推測是下載管理器拿它資料庫裡記的大小比對，
     發現比較小就判定下載不完整、去續傳，而 CDN URL 早就過期 → 永遠緩衝。
     所以移除 fragment 之後，尾端補一個 free box 把大小補回原值。

    => 廣告是真的被移除（時間軸連續接起來），但**不會省到手機容量**。

做法
----
  video : 按 fragment 邊界整塊移除（每個 fragment 開頭都是關鍵影格，
          所以切點精度等於 GOP 長度，B站 通常 5 秒）
  audio : 精確切在視訊的切點上。AAC 每格獨立、trun 裡逐格記大小，
          所以邊界那個 fragment 重建即可，精度約 23ms，音畫不會飄
  兩軌  : 移除後把後面所有 fragment 的 tfdt 往前平移，時間軸連續
  收尾  : sidx 重建、mvhd/tkhd/mehd 改時長、尾端補回原本大小、
          index.json 的 md5 重算、entry.json 的時長更新

用法
----
  # 乾跑，只看會改什麼
  python bili_trim.py <cid 目錄> --cut 195-247 --cut 612-660

  # 確認後實際寫入（會先把整個 cid 目錄備份成 .bak）
  python bili_trim.py <cid 目錄> --cut 195-247 --apply

依賴：無（純標準庫；不需要 ffmpeg）
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import bili_env as ENV

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


# --------------------------------------------------------------------------- #
# 基本工具
# --------------------------------------------------------------------------- #

def human(n):
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        return str(n)
    return "{:,}".format(n)


def hhmmss(sec):
    sec = max(sec, 0)
    h, rem = divmod(int(round(sec)), 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s)


def to_seconds(text):
    total = 0.0
    for part in text.split(":"):
        total = total * 60 + float(part)
    return total


def parse_interval(text):
    m = re.match(r"^\s*([\d:.]+)\s*-\s*([\d:.]+)\s*$", text)
    if not m:
        raise argparse.ArgumentTypeError(
            "區間格式應為 起-迄，例如 195-247 或 3:15-4:07")
    a, b = to_seconds(m.group(1)), to_seconds(m.group(2))
    if b <= a:
        raise argparse.ArgumentTypeError("區間迄點必須大於起點：%s" % text)
    return (a, b)


def merge_intervals(ranges):
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + 1e-9:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def md5_file(path, skip=0):
    h = hashlib.md5()
    with open(path, "rb") as f:
        if skip:
            f.seek(skip)
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# MP4 box
# --------------------------------------------------------------------------- #

def parse_boxes(buf, start, end):
    """走訪 [start,end) 的 box，回傳 [(off, size, hdr, type_bytes), ...]。"""
    out = []
    off = start
    while off + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[off:off + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", buf[off + 8:off + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr or off + size > end:
            break
        out.append((off, size, hdr, typ))
        off += size
    return out


def find_box(buf, name, start, end):
    """在 [start,end) 內遞迴找 box（只進會含子 box 的容器）。"""
    for off, size, hdr, typ in parse_boxes(buf, start, end):
        if typ == name:
            return off, size, hdr
        if typ in (b"moof", b"traf", b"moov", b"trak", b"mvex"):
            r = find_box(buf, name, off + hdr, off + size)
            if r:
                return r
    return None


def mk_box(typ, body):
    return struct.pack(">I", len(body) + 8) + typ + body


# mvhd / tkhd / mehd 的 duration 位置（以 box 起點為基準）
_DUR_AT = {(b"mvhd", 0): (24, 4), (b"mvhd", 1): (32, 8),
           (b"tkhd", 0): (28, 4), (b"tkhd", 1): (36, 8),
           (b"mehd", 0): (12, 4), (b"mehd", 1): (12, 8)}


def _box_start(buf, name):
    i = buf.find(name)
    while i >= 4:
        size = struct.unpack(">I", buf[i - 4:i])[0]
        if 8 <= size <= len(buf):
            return i - 4
        i = buf.find(name, i + 1)
    return None


def read_duration(moov, name=b"mvhd"):
    o = _box_start(moov, name)
    if o is None:
        return None
    at, w = _DUR_AT[(name, 1 if moov[o + 8] == 1 else 0)]
    return struct.unpack(">Q" if w == 8 else ">I", moov[o + at:o + at + w])[0]


def patch_durations(moov, new_ms):
    """就地改 mvhd / tkhd / mehd 的 duration；box 與 moov 大小都不變。"""
    buf = bytearray(moov)
    done = []
    for name in (b"mvhd", b"tkhd", b"mehd"):
        o = _box_start(buf, name)
        if o is None:
            continue
        at, w = _DUR_AT[(name, 1 if buf[o + 8] == 1 else 0)]
        buf[o + at:o + at + w] = struct.pack(">Q" if w == 8 else ">I", new_ms)
        done.append(name.decode())
    return bytes(buf), done


# --------------------------------------------------------------------------- #
# 前綴（某些版本的 m4s 開頭有填充位元組）
# --------------------------------------------------------------------------- #

def inspect_head(head, total):
    """
    只靠檔案開頭（ftyp + moov + sidx）與總位元組數，判斷這是原檔還是剪過的。

    原檔的 sidx 會剛好蓋滿到檔尾；剪過的會短一截，後面補 free box 把大小
    補回原值。好處是只要讀檔案前面幾十 KB —— 手機上那支動輒幾百 MB，
    可以在決定要不要整個拉下來之前就先問清楚。

    回傳 dict：trimmed / frag_end / tail / duration_ms / timescale / nrefs。
    看不懂這個檔案就丟 RuntimeError。
    """
    i = head.find(b"ftyp")
    if i < 4:
        raise RuntimeError("開頭找不到 ftyp，不是預期的 B站 緩存格式")
    prefix = i - 4
    buf = head[prefix:]

    boxes = parse_boxes(buf, 0, len(buf))
    pos = {}
    for off, size, hdr, typ in boxes:
        t = typ.decode("latin1", "replace")
        if t in ("moov", "sidx") and t not in pos:
            pos[t] = (off, size, hdr)
    if "sidx" not in pos:
        raise RuntimeError("前 %s bytes 裡沒有完整的 sidx，讀多一點再試"
                           % human(len(head)))

    duration_ms = None
    if "moov" in pos:
        mo, ms, _ = pos["moov"]
        if mo + ms <= len(buf):
            duration_ms = read_duration(buf[mo:mo + ms])

    so, ssize, _ = pos["sidx"]
    if so + ssize > len(buf):
        raise RuntimeError("sidx 被截斷了（需要 %s bytes，只讀到 %s）"
                           % (human(ssize), human(len(buf))))
    p = so + 8
    ver = buf[p]
    p += 4
    _ref_id, timescale = struct.unpack(">II", buf[p:p + 8])
    p += 8
    if ver == 0:
        _ept, first_offset = struct.unpack(">II", buf[p:p + 8])
        p += 8
    else:
        _ept, first_offset = struct.unpack(">QQ", buf[p:p + 16])
        p += 16
    p += 2
    cnt = struct.unpack(">H", buf[p:p + 2])[0]
    p += 2
    span = 0
    for _ in range(cnt):
        w1 = struct.unpack(">I", buf[p:p + 4])[0]
        span += w1 & 0x7FFFFFFF
        p += 12

    frag_end = prefix + so + ssize + first_offset + span
    tail = total - frag_end
    return {"trimmed": tail > 0, "frag_end": frag_end, "tail": tail,
            "duration_ms": duration_ms, "timescale": timescale,
            "nrefs": cnt, "prefix": prefix}


def detect_prefix(path, scan=256):
    with open(path, "rb") as f:
        head = f.read(scan)
    i = head.find(b"ftyp")
    if i < 0:
        raise RuntimeError("%s 前 %d bytes 找不到 ftyp" % (path, scan))
    if i < 4:
        raise RuntimeError("%s 的 ftyp 位移異常（%d）" % (path, i))
    return i - 4


# --------------------------------------------------------------------------- #
# 一條串流
# --------------------------------------------------------------------------- #

class Track(object):
    """一個 m4s：ftyp + moov + sidx + 一連串 moof/mdat。"""

    def __init__(self, path):
        self.path = Path(path)
        self.prefix_len = detect_prefix(path)
        with open(path, "rb") as f:
            self.prefix = f.read(self.prefix_len)
            self.data = f.read()
        self.total = len(self.data)

        tops = parse_boxes(self.data, 0, self.total)
        kinds = {}
        for off, size, hdr, typ in tops:
            t = typ.decode("latin1", "replace")
            if t in ("ftyp", "moov", "sidx") and t not in kinds:
                kinds[t] = (off, size, hdr)
        for need in ("ftyp", "moov", "sidx"):
            if need not in kinds:
                raise RuntimeError("%s 裡找不到 %s box，這支不是預期的 B站 緩存格式"
                                   % (path, need))
        self.ftyp = self.data[kinds["ftyp"][0]:kinds["ftyp"][0] + kinds["ftyp"][1]]
        mo, ms, _ = kinds["moov"]
        self.moov = self.data[mo:mo + ms]
        self._read_sidx(kinds["sidx"][0])

        # fragment 的 (起點, 位元組大小, 時長單位)
        self.frags = []
        cur = self.sidx_end + self.first_offset
        for w1, dur, w3 in self.refs:
            size = w1 & 0x7FFFFFFF
            self.frags.append([cur, size, dur, w1, w3])
            cur += size
        # 剪過的檔案尾端會有一塊「補回原始大小」用的 free box，所以 sidx 不會
        # 蓋到檔尾。那不是損壞，而是這個工具自己留下的 —— 認出它、記下來，
        # 然後照常處理。剪過的檔案要能再剪，不然使用者一旦弄丟備份就卡死了。
        self.tail_pad = self.total - cur
        self.trimmed = False
        if self.tail_pad:
            rest = parse_boxes(self.data, cur, self.total)
            padded = (rest and all(t == b"free" for _, _, _, t in rest)
                      and sum(s for _, s, _, _ in rest) == self.tail_pad)
            if not padded:
                raise RuntimeError(
                    "%s 的 sidx 沒有剛好蓋滿 fragment 區域（算到 %d，檔案 %d），"
                    "而且尾端那 %s bytes 也不是補大小用的 free box —— "
                    "這個檔案的結構不是本工具認得的樣子。"
                    % (path, cur, self.total, human(self.tail_pad)))
            self.trimmed = True

    # ---- sidx ----

    def _read_sidx(self, off):
        d = self.data
        size = struct.unpack(">I", d[off:off + 4])[0]
        p = off + 8
        ver = d[p]
        p += 4
        self.ref_id, self.timescale = struct.unpack(">II", d[p:p + 8])
        p += 8
        if ver == 0:
            self.ept, self.first_offset = struct.unpack(">II", d[p:p + 8])
            p += 8
        else:
            self.ept, self.first_offset = struct.unpack(">QQ", d[p:p + 16])
            p += 16
        p += 2
        cnt = struct.unpack(">H", d[p:p + 2])[0]
        p += 2
        self.refs = []
        for _ in range(cnt):
            self.refs.append(list(struct.unpack(">III", d[p:p + 12])))
            p += 12
        self.sidx_off = off
        self.sidx_size = size
        self.sidx_end = off + size
        self.sidx_ver = ver

    def _build_sidx(self, refs, first_offset):
        body = (b"\x00\x00\x00\x00"
                + struct.pack(">II", self.ref_id, self.timescale)
                + struct.pack(">II", self.ept, first_offset)
                + struct.pack(">HH", 0, len(refs)))
        for w1, dur, w3 in refs:
            body += struct.pack(">III", w1, dur, w3)
        return mk_box(b"sidx", body)

    # ---- 時間軸 ----

    def starts(self):
        """每個 fragment 的起始時間（單位：sidx timescale）。"""
        out = []
        acc = 0
        for _, _, dur, _, _ in self.frags:
            out.append(acc)
            acc += dur
        return out, acc

    def duration_s(self):
        return self.starts()[1] / float(self.timescale)

    def fragment_seconds(self):
        return [f[2] / float(self.timescale) for f in self.frags]

    def boundaries(self):
        """所有 fragment 邊界的時間（秒），含結尾。"""
        st, total = self.starts()
        return [x / float(self.timescale) for x in st] + \
               [total / float(self.timescale)]

    # ---- moof 內部 ----

    def _tfhd_defaults(self, frag_bytes):
        """
        取 tfhd 的 default_sample_duration / default_sample_size。
        B站 的音訊只有第一個 fragment 在 trun 裡逐格記 duration，
        之後都靠 tfhd 的預設值，所以非讀不可。
        """
        r = find_box(frag_bytes, b"tfhd", 0, len(frag_bytes))
        if not r:
            raise RuntimeError("fragment 裡找不到 tfhd")
        o, size, hdr = r
        flags = struct.unpack(">I", frag_bytes[o + 8:o + 12])[0] & 0xFFFFFF
        p = o + 16                      # 跳過 version/flags 與 track_ID
        if flags & 0x000001:
            p += 8                      # base_data_offset
        if flags & 0x000002:
            p += 4                      # sample_description_index
        dur = sz = None
        if flags & 0x000008:
            dur = struct.unpack(">I", frag_bytes[p:p + 4])[0]
            p += 4
        if flags & 0x000010:
            sz = struct.unpack(">I", frag_bytes[p:p + 4])[0]
            p += 4
        return dur, sz

    def _trun_info(self, frag_bytes):
        r = find_box(frag_bytes, b"trun", 0, len(frag_bytes))
        if not r:
            raise RuntimeError("fragment 裡找不到 trun")
        o, size, hdr = r
        word = struct.unpack(">I", frag_bytes[o + 8:o + 12])[0]
        flags = word & 0xFFFFFF
        cnt = struct.unpack(">I", frag_bytes[o + 12:o + 16])[0]
        p = o + 16
        data_off_at = None
        if flags & 0x000001:
            data_off_at = p
            p += 4
        first_flags_at = None
        if flags & 0x000004:
            first_flags_at = p
            p += 4
        fields = []
        if flags & 0x000100:
            fields.append("dur")
        if flags & 0x000200:
            fields.append("size")
        if flags & 0x000400:
            fields.append("flags")
        if flags & 0x000800:
            fields.append("cto")
        stride = 4 * len(fields)
        samples = []
        for k in range(cnt):
            q = p + k * stride
            vals = {}
            for j, name in enumerate(fields):
                vals[name] = struct.unpack(">I", frag_bytes[q + 4 * j:q + 4 * j + 4])[0]
            samples.append(vals)
        # trun 沒逐格記的欄位，用 tfhd 的預設值補上，讓 samples 一律可用
        if "dur" not in fields or "size" not in fields:
            d_dur, d_size = self._tfhd_defaults(frag_bytes)
            for vals in samples:
                if "dur" not in vals:
                    if d_dur is None:
                        raise RuntimeError("trun 與 tfhd 都沒有 sample duration")
                    vals["dur"] = d_dur
                if "size" not in vals:
                    if d_size is None:
                        raise RuntimeError("trun 與 tfhd 都沒有 sample size")
                    vals["size"] = d_size
        return dict(off=o, size=size, flags=flags, count=cnt, fields=fields,
                    stride=stride, samples_at=p, samples=samples,
                    data_off_at=data_off_at, first_flags_at=first_flags_at,
                    word=word)

    def _tfdt_of(self, frag_bytes):
        r = find_box(frag_bytes, b"tfdt", 0, len(frag_bytes))
        if not r:
            raise RuntimeError("fragment 裡找不到 tfdt")
        o, size, hdr = r
        ver = frag_bytes[o + 8]
        if ver == 1:
            return o, 8, struct.unpack(">Q", frag_bytes[o + 12:o + 20])[0]
        return o, 4, struct.unpack(">I", frag_bytes[o + 12:o + 16])[0]

    def _set_tfdt(self, frag_bytes, value):
        b = bytearray(frag_bytes)
        o, w, _ = self._tfdt_of(b)
        b[o + 12:o + 12 + w] = struct.pack(">Q" if w == 8 else ">I", value)
        return bytes(b)

    def frag_bytes(self, i):
        off, size = self.frags[i][0], self.frags[i][1]
        return self.data[off:off + size]

    def rebuild_fragment(self, i, keep_slice):
        """
        重建第 i 個 fragment，只留 keep_slice 指定的 sample。
        回傳 (新的位元組, 新的時長單位)。只有音訊會用到。
        """
        frag = self.frag_bytes(i)
        tops = parse_boxes(frag, 0, len(frag))
        moof = next(x for x in tops if x[3] == b"moof")
        mdat = next(x for x in tops if x[3] == b"mdat")
        info = self._trun_info(frag)
        if not all("size" in x and "dur" in x for x in info["samples"]):
            raise RuntimeError("這條音訊解析不出逐 sample 的大小／時長，無法精修")

        keep = info["samples"][keep_slice]
        if not keep:
            raise RuntimeError("重建後沒有任何 sample")

        # mdat 內每個 sample 的位移
        payload_start = mdat[0] + mdat[2]
        offs = []
        cur = payload_start
        for s in info["samples"]:
            offs.append((cur, s["size"]))
            cur += s["size"]
        kept = offs[keep_slice]
        new_payload = b"".join(frag[o:o + n] for o, n in kept)

        # 新 trun：欄位組合不變，只是 sample 數變少
        ent = b""
        for s in keep:
            for name in info["fields"]:
                ent += struct.pack(">I", s[name])
        head = struct.pack(">I", info["word"]) + struct.pack(">I", len(keep))
        extra = b""
        if info["data_off_at"] is not None:
            extra += b"\x00\x00\x00\x00"       # 稍後回填
        if info["first_flags_at"] is not None:
            extra += frag[info["first_flags_at"]:info["first_flags_at"] + 4]
        new_trun = mk_box(b"trun", head + extra + ent)

        # 重組 moof：除了 trun 以外原封不動
        def rebuild(buf, off, size, hdr, typ):
            if typ == b"trun":
                return new_trun
            if typ in (b"moof", b"traf"):
                kids = b"".join(rebuild(buf, o, s, h, t)
                                for o, s, h, t in parse_boxes(buf, off + hdr, off + size))
                return mk_box(typ, kids)
            return buf[off:off + size]

        new_moof = rebuild(frag, moof[0], moof[1], moof[2], b"moof")
        new_mdat = mk_box(b"mdat", new_payload)

        # data_offset 以 moof 起點為基準（default_base_is_moof）
        if info["data_off_at"] is not None:
            nb = bytearray(new_moof)
            r = find_box(nb, b"trun", 0, len(nb))
            o = r[0]
            nb[o + 16:o + 20] = struct.pack(">i", len(new_moof) + 8)
            new_moof = bytes(nb)

        new_dur = sum(s["dur"] for s in keep)
        return new_moof + new_mdat, new_dur


# --------------------------------------------------------------------------- #
# 剪輯計畫
# --------------------------------------------------------------------------- #

class Plan(object):
    def __init__(self, track, label):
        self.t = track
        self.label = label
        self.ops = []        # 每個 fragment: ('keep',) / ('drop',) / ('trim', slice)
        for _ in track.frags:
            self.ops.append(("keep",))
        self.removed_units = 0

    def cut_aligned(self, ranges_units):
        """視訊用：區間必須剛好落在 fragment 邊界，整塊丟掉。"""
        starts, total = self.t.starts()
        for a, b in ranges_units:
            for i, (st, f) in enumerate(zip(starts, self.t.frags)):
                en = st + f[2]
                if st >= a - 1 and en <= b + 1:
                    if self.ops[i][0] == "keep":
                        self.ops[i] = ("drop",)
                        self.removed_units += f[2]

    def cut_exact(self, ranges_units):
        """音訊用：精確切在指定時間，邊界 fragment 重建。"""
        starts, total = self.t.starts()
        for a, b in ranges_units:
            for i, (st, f) in enumerate(zip(starts, self.t.frags)):
                dur = f[2]
                en = st + dur
                if en <= a or st >= b:
                    continue
                if st >= a and en <= b:
                    if self.ops[i][0] == "keep":
                        self.ops[i] = ("drop",)
                        self.removed_units += dur
                    continue
                # 部分重疊：算出要保留哪些 sample
                info = self.t._trun_info(self.t.frag_bytes(i))
                acc = st
                keep_idx = []
                for k, s in enumerate(info["samples"]):
                    s_end = acc + s["dur"]
                    mid = (acc + s_end) / 2.0
                    if not (a <= mid < b):
                        keep_idx.append(k)
                    acc = s_end
                if not keep_idx:
                    self.ops[i] = ("drop",)
                    self.removed_units += dur
                elif len(keep_idx) < len(info["samples"]):
                    lo, hi = keep_idx[0], keep_idx[-1] + 1
                    if keep_idx != list(range(lo, hi)):
                        # 中間被挖空（區間落在單一 fragment 內），拆不了，
                        # 退而保留前半段
                        hi = keep_idx[0] + 1
                        for k in keep_idx[1:]:
                            if k == hi:
                                hi += 1
                            else:
                                break
                        lo = keep_idx[0]
                    dropped = sum(info["samples"][k]["dur"]
                                  for k in range(len(info["samples"]))
                                  if not (lo <= k < hi))
                    self.ops[i] = ("trim", slice(lo, hi))
                    self.removed_units += dropped

    def render(self, pad_to):
        """產生新的檔案位元組（含原本的前綴），尾端補到 pad_to 大小。"""
        t = self.t
        new_refs = []
        pieces = []
        removed_before = 0
        running_tfdt = None

        for i, op in enumerate(self.ops):
            w1, dur, w3 = t.refs[i]
            if op[0] == "drop":
                removed_before += dur
                continue
            if op[0] == "keep":
                frag = t.frag_bytes(i)
                new_dur = dur
            else:
                frag, new_dur = t.rebuild_fragment(i, op[1])
                removed_before += dur - new_dur
            # tfdt 往前平移，讓時間軸連續，而且一定從 0 開始。
            #
            # 這裡本來是沿用「第一個保留的 fragment 原本的 tfdt」，結果只要切掉
            # 的是片頭，整條時間軸就會從被切掉的長度開始（例如切掉前 660 秒，
            # 第一格的 pts 就是 660.0）。B站 App 容忍這種偏移照播，但這些檔案
            # 沒有 elst 在做補償，所以 ffmpeg/ffprobe 會把時長算成「原始長度」，
            # 而且用 -ss 定位會整個對不上 —— 預覽功能就是這樣壞的。
            if running_tfdt is None:
                running_tfdt = 0
            frag = t._set_tfdt(frag, running_tfdt)
            running_tfdt += new_dur
            pieces.append(frag)
            new_refs.append([(w1 & 0x80000000) | len(frag), new_dur, w3])

        if not new_refs:
            raise RuntimeError("%s：全部被剪掉了" % self.label)

        probe = t._build_sidx(new_refs, 0)
        # sidx 變短多少，就在它後面補多少 free box，讓第一個 fragment 落在跟
        # 輸入檔一模一樣的位元組位移上。重複剪的時候這點特別重要：不補的話
        # 每剪一次檔案前段就往前擠一截，愈剪愈偏離當初驗證過的排版。
        target = t.sidx_end + t.first_offset      # 輸入檔第一個 fragment 的位移
        head_pad = target - (len(t.ftyp) + len(t.moov) + len(probe))
        if head_pad < 0:
            raise RuntimeError("新的 sidx 比原本大，不該發生")
        if 0 < head_pad < 8:
            head_pad = 0        # 放不下 free box，就讓它自然縮短
        new_sidx = t._build_sidx(new_refs, head_pad)

        new_units = sum(r[1] for r in new_refs)
        ts = t.timescale
        old_ms = read_duration(t.moov)
        new_ms = max(int(round(new_units / float(ts) * 1000)), 1)
        new_moov, touched = patch_durations(t.moov, new_ms)

        out = bytearray()
        out += t.ftyp
        out += new_moov
        out += new_sidx
        if head_pad:
            out += mk_box(b"free", b"\x00" * (head_pad - 8))
        first_frag_at = len(out) + len(t.prefix)
        for p in pieces:
            out += p

        body = len(out)
        tail_pad = pad_to - len(t.prefix) - body
        if tail_pad < 0:
            raise RuntimeError("%s：新檔比原檔大了 %d bytes" % (self.label, -tail_pad))
        if 0 < tail_pad < 8:
            raise RuntimeError("%s：尾端只差 %d bytes，放不下 free box"
                               % (self.label, tail_pad))
        if tail_pad:
            out += mk_box(b"free", b"\x00" * (tail_pad - 8))

        info = dict(nkeep=len(new_refs), ndrop=sum(1 for o in self.ops if o[0] == "drop"),
                    ntrim=sum(1 for o in self.ops if o[0] == "trim"),
                    removed_s=self.removed_units / float(ts),
                    old_ms=old_ms, new_ms=new_ms, touched=touched,
                    head_pad=head_pad, tail_pad=tail_pad,
                    sidx_old=t.sidx_size, sidx_new=len(new_sidx),
                    first_frag_at=first_frag_at,
                    first_frag_orig=t.sidx_end + t.first_offset + t.prefix_len,
                    new_dur_s=new_units / float(ts))
        return t.prefix + bytes(out), info


# --------------------------------------------------------------------------- #
# 彈幕
# --------------------------------------------------------------------------- #

def shift_danmaku(xml_path, cuts, out_path):
    tree = ET.parse(xml_path)
    root = tree.getroot()
    removed = shifted = 0
    for d in list(root.findall("d")):
        p = d.get("p")
        if not p:
            continue
        fields = p.split(",")
        try:
            t = float(fields[0])
        except (ValueError, IndexError):
            continue
        delta = 0.0
        drop = False
        for a, b in cuts:
            if a <= t < b:
                drop = True
                break
            if t >= b:
                delta += b - a
        if drop:
            root.remove(d)
            removed += 1
        elif delta > 0:
            fields[0] = "%.3f" % (t - delta)
            d.set("p", ",".join(fields))
            shifted += 1
    tree.write(out_path, encoding="utf-8", xml_declaration=True)
    return removed, shifted


def make_previews(v_bytes, a_bytes, v_src, a_src, snapped, seam_window,
                  out_dir, tag):
    """
    產生對照片段：剪完的接縫處 + 原檔被移除的那一段。
    用重編碼擷取，切點才會精準到影格（片段很短，所以很快）。
    """
    if not ENV.have("ffmpeg"):
        print("\n找不到 ffmpeg，跳過預覽（工具本身不需要它，只有預覽需要）")
        return
    import subprocess
    import tempfile

    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="bilitrim_"))
    made = []
    try:
        nv, na = work / "v.m4s", work / "a.m4s"
        nv.write_bytes(v_bytes)
        na.write_bytes(a_bytes)
        new_mp4 = work / "new.mp4"
        r = subprocess.run([ENV.path("ffmpeg"), "-v", "error", "-y", "-i", str(nv),
                            "-i", str(na), "-c", "copy", str(new_mp4)],
                           capture_output=True, text=True)
        if r.returncode:
            print("\n預覽失敗（新檔合併不起來）：%s" % r.stderr.strip()[:300])
            return
        orig_mp4 = work / "orig.mp4"
        r = subprocess.run([ENV.path("ffmpeg"), "-v", "error", "-y", "-i", str(v_src),
                            "-i", str(a_src), "-c", "copy", str(orig_mp4)],
                           capture_output=True, text=True)
        have_orig = r.returncode == 0

        def grab(src, start, length, dst, label):
            base = [ENV.path("ffmpeg"), "-v", "error", "-y",
                    "-ss", "%.3f" % max(start, 0),
                    "-i", str(src), "-t", "%.3f" % length]
            rr = subprocess.run(base + ["-c:v", "libx264", "-preset", "veryfast",
                                        "-crf", "22", "-c:a", "aac", "-b:a",
                                        "128k", str(dst)],
                                capture_output=True, text=True)
            if rr.returncode:
                rr = subprocess.run(base + ["-c", "copy", str(dst)],
                                    capture_output=True, text=True)
            if rr.returncode:
                print("  %s 擷取失敗：%s" % (label, rr.stderr.strip()[:200]))
                return False
            made.append(dst)
            return True

        print("\n預覽片段：")
        shift = 0.0
        for k, (a, b) in enumerate(snapped, 1):
            seam = a - shift
            shift += b - a
            dst = out_dir / ("%s_seam%02d_after.mp4" % (tag, k))
            if grab(new_mp4, seam - seam_window, seam_window * 2, dst,
                    "接縫 %d" % k):
                print("  接縫%d 剪完 : %s（接縫在正中間 %.1f s 處；新時間軸 %s）"
                      % (k, dst.name, seam_window, hhmmss(seam)))
            if have_orig:
                # 被移除的區間可能很長（例如整個片頭），全部重編碼會跑很久，
                # 所以只取開頭這一段，足夠確認「切掉的是廣告」就好
                cap = seam_window * 2 + 24.0
                want = (b - a) + seam_window * 2
                length = min(want, cap)
                dst = out_dir / ("%s_seam%02d_before.mp4" % (tag, k))
                if grab(orig_mp4, a - seam_window, length, dst, "原檔 %d" % k):
                    note = ""
                    if length < want:
                        note = ("；移除區間共 %.0f s，這裡只取開頭 %.0f s"
                                % (b - a, length))
                    print("  接縫%d 原檔 : %s（原時間軸 %s-%s，中間 %.1f s 就是"
                          "被移除的部分%s）"
                          % (k, dst.name, hhmmss(a), hhmmss(b), b - a, note))
        if made:
            print("  位置：%s" % out_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 切點吸附與寫回（CLI 與 GUI 共用）
# --------------------------------------------------------------------------- #

def snap_to_boundaries(bounds, cuts, mode="outer"):
    """
    把要刪除的區間吸附到 fragment 邊界 —— 視訊只能整塊移除，所以切點精度
    就是 GOP 長度。outer 外擴（寧可多切一點正片），inner 內縮（寧可殘留）。
    回傳 (吸附後的區間, [(原起, 原迄, 新起, 新迄), ...])。
    """
    rows, snapped = [], []
    for a, b in merge_intervals(cuts):
        if mode == "outer":
            sa = max([x for x in bounds if x <= a + 1e-6], default=bounds[0])
            sb = min([x for x in bounds if x >= b - 1e-6], default=bounds[-1])
        else:
            sa = min([x for x in bounds if x >= a - 1e-6], default=bounds[-1])
            sb = max([x for x in bounds if x <= b + 1e-6], default=bounds[0])
        if sb <= sa:
            raise ValueError(
                "區間 %.2f-%.2f 吸附到 fragment 邊界後塌掉了（這段比一個 "
                "fragment 還短）。改用外擴吸附，或把區間拉長。" % (a, b))
        rows.append((a, b, sa, sb))
        snapped.append((sa, sb))
    return merge_intervals(snapped), rows


def write_back(cid_dir, q_dir, v_new, a_new, v_info, a_info, snapped,
               log=print):
    """
    把剪好的位元組與對應的 JSON／彈幕寫進 cid 目錄。
    回傳實際改動過的檔案（相對 cid 目錄的路徑），呼叫端用它決定要推哪些回手機。
    備份不在這裡做 —— 由呼叫端自己決定備份策略。
    """
    cid_dir, q_dir = Path(cid_dir), Path(q_dir)
    v_src, a_src = q_dir / "video.m4s", q_dir / "audio.m4s"
    index_path, entry_path = q_dir / "index.json", cid_dir / "entry.json"
    changed = []

    v_src.write_bytes(v_new)
    a_src.write_bytes(a_new)
    changed += ["%s/video.m4s" % q_dir.name, "%s/audio.m4s" % q_dir.name]

    # index.json：大小沒變，只有 md5 要重算
    if index_path.is_file():
        index = json.loads(index_path.read_text(encoding="utf-8"))
        touched = []
        for label, p in (("video", v_src), ("audio", a_src)):
            reps = index.get(label)
            if not (isinstance(reps, list) and reps and isinstance(reps[0], dict)):
                continue
            rep = reps[0]
            if "md5" in rep:
                rep["md5"] = md5_file(p)
                touched.append("%s.md5" % label)
            if "size" in rep:
                rep["size"] = os.path.getsize(p)
        index_path.write_text(json.dumps(index, ensure_ascii=False,
                                         separators=(",", ":")),
                              encoding="utf-8")
        changed.append("%s/index.json" % q_dir.name)
        log("index.json: 更新 %s" % "、".join(touched))

    new_ms = min(v_info["new_ms"], a_info["new_ms"])
    if entry_path.is_file():
        entry = json.loads(entry_path.read_text(encoding="utf-8"))
        if "total_time_milli" in entry:
            entry["total_time_milli"] = new_ms
        total = os.path.getsize(v_src) + os.path.getsize(a_src)
        for k in ("total_bytes", "downloaded_bytes"):
            if k in entry:
                entry[k] = total
        entry_path.write_text(json.dumps(entry, ensure_ascii=False,
                                         separators=(",", ":")),
                              encoding="utf-8")
        changed.append("entry.json")
        log("entry.json: total_time_milli=%d, total_bytes=%d" % (new_ms, total))

    for dm in sorted(cid_dir.rglob("*.xml")):
        try:
            if not ET.parse(dm).getroot().findall("d"):
                log("彈幕 %s：裡面一條彈幕都沒有，不動它。" % dm.name)
                continue
            r, s = shift_danmaku(dm, snapped, dm)
            changed.append(dm.relative_to(cid_dir).as_posix())
            log("彈幕 %s：刪除 %d 條，平移 %d 條" % (dm.name, r, s))
        except ET.ParseError:
            log("彈幕 %s 不是 XML（可能是 protobuf），剪完後彈幕會錯位。" % dm.name)
    return changed


# --------------------------------------------------------------------------- #
# adb（--device 模式）
# --------------------------------------------------------------------------- #

def _adb_env():
    """
    避免 Git Bash / MSYS 把 /sdcard/... 這種路徑改寫成 Windows 路徑。
    （實測在 Git Bash 裡不設這個，adb 會收到 C:/Program Files/Git/sdcard/...）
    """
    env = dict(os.environ)
    env["MSYS_NO_PATHCONV"] = "1"
    env["MSYS2_ARG_CONV_EXCL"] = "*"
    return env


def adb(*args, **kw):
    import subprocess
    binary = kw.pop("binary", False)
    r = subprocess.run([ENV.path("adb")] + list(args),
                       capture_output=True, env=_adb_env())
    if r.returncode:
        raise RuntimeError("adb %s 失敗：%s"
                           % (" ".join(args),
                              r.stderr.decode("utf-8", "replace").strip()[:400]))
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def adb_require_device():
    if not ENV.have("adb"):
        sys.exit(ENV.missing_hint("adb"))
    rows = []
    for line in adb("devices").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            rows.append(parts[0])
    if not rows:
        sys.exit("adb 看不到裝置。確認 USB 已連接、已開啟 USB 偵錯，"
                 "並在手機上允許這台電腦。")
    if len(rows) > 1:
        sys.exit("接了多台裝置：%s。請只留一台。" % "、".join(rows))
    return rows[0]


ANDROID_ROOTS = ("/sdcard/", "/storage/emulated/", "/data/", "/mnt/")


def normalize_device_path(p):
    """
    Git Bash / MSYS 會把 /sdcard/... 這種參數改寫成 C:/Program Files/Git/sdcard/...
    （在 Python 看到之前就發生了，所以只能在這裡救）。
    認得出被改寫的樣子就切回原本的 Android 路徑。
    """
    p = p.replace("\\", "/")
    if p.startswith("/"):
        return p.rstrip("/")
    for root in ANDROID_ROOTS:
        i = p.find(root)
        if i > 0:
            fixed = p[i:].rstrip("/")
            print("注意     : 參數被 shell 改寫成 %s" % p)
            print("           已還原為 %s" % fixed)
            print("           （在 PowerShell 裡不會發生；Git Bash 可用 "
                  "MSYS_NO_PATHCONV=1 避免）")
            return fixed
    sys.exit("--device 要給手機上的絕對路徑（開頭是 /sdcard/... 之類），"
             "收到的是：%s" % p)


def _device_quality_dir(remote):
    """問裝置：cid 目錄底下的畫質子目錄叫什麼。"""
    out = adb("shell", "ls -p %s" % remote)
    dirs = [x.rstrip("/") for x in out.split() if x.endswith("/")]
    return dirs[0] if len(dirs) == 1 else None


def _staged_matches_device(local, remote):
    """暫存裡那份是不是跟裝置上的一模一樣（大小 + md5）。"""
    q = [d for d in local.iterdir()
         if d.is_dir() and (d / "video.m4s").is_file()]
    if not q or not (local / "entry.json").is_file():
        return False, "暫存裡的檔案不完整"
    q = q[0]
    for n in ("index.json", "video.m4s", "audio.m4s"):
        if not (q / n).is_file():
            return False, "暫存裡缺少 %s" % n

    rq = "%s/%s" % (remote, q.name)
    try:
        out = adb("shell", "stat -c '%s %n' {0}/video.m4s {0}/audio.m4s".format(rq))
    except RuntimeError as e:
        return False, "讀不到裝置上的檔案大小（%s）" % e
    sizes = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            sizes[parts[1].rsplit("/", 1)[-1]] = int(parts[0])
    for n in ("video.m4s", "audio.m4s"):
        if n not in sizes:
            return False, "裝置上讀不到 %s" % n
        if sizes[n] != (q / n).stat().st_size:
            return False, ("%s 大小不符（裝置 %s / 暫存 %s）"
                           % (n, human(sizes[n]), human((q / n).stat().st_size)))

    print("           大小相符，核對 md5（在裝置上算，稍等）...")
    out = adb("shell", "md5sum {0}/video.m4s {0}/audio.m4s".format(rq))
    dev = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            dev[parts[1].rsplit("/", 1)[-1]] = parts[0].lower()
    for n in ("video.m4s", "audio.m4s"):
        if dev.get(n) != md5_file(q / n):
            return False, "%s 的 md5 與裝置不符" % n
    return True, "與裝置完全一致"


def adb_pull_cid(remote, stage, refresh=False):
    """
    把手機上的 cid 目錄拉到 stage 底下，回傳本機路徑。
    暫存裡已經有一份乾淨且與裝置一致的副本時就重用，省掉重拉。
    """
    serial = adb_require_device()
    remote = remote.rstrip("/")
    name = remote.split("/")[-1]
    stage.mkdir(parents=True, exist_ok=True)
    local = stage / name
    backup = stage / (name + ".bak")

    print("裝置     : %s" % serial)

    if local.exists() and refresh:
        print("暫存     : --refresh，刪掉舊的 %s 重新拉" % local)
        shutil.rmtree(local)
        if backup.exists():
            shutil.rmtree(backup)

    if local.exists():
        if backup.exists():
            # 之前 --apply 過了。暫存裡那份是剪過的版本 —— 繼續剪它沒問題
            # （下面會用 md5 核對它跟裝置上是不是同一份），只要記得 --cut
            # 的時間是以那一版為準。原檔在 .bak 裡。
            print("暫存     : 旁邊有 %s，表示之前 --apply 過；暫存裡是剪過的那"
                  "一版。" % backup.name)
            print("           繼續剪沒問題，但 --cut 的時間要以那一版為準。"
                  "原檔在 %s。" % backup.name)
            print("           想從原檔重來請加 --refresh（會刪掉暫存重拉）。")
        ok, why = _staged_matches_device(local, remote)
        if ok:
            print("暫存     : 重用 %s（%s，不用重拉）" % (local, why))
            return local
        sys.exit("暫存目錄 %s 已存在但%s。\n"
                 "加 --refresh 刪掉重拉，或自己檢查後處理掉它。" % (local, why))

    print("拉取     : %s" % remote)
    print("           -> %s（整個檔案都要傳，會花一點時間）" % local)
    adb("pull", remote, str(stage))
    if not (local / "entry.json").is_file():
        sys.exit("拉下來的 %s 裡沒有 entry.json，確認手機上的路徑是 cid 層目錄。"
                 % local)
    return local


def adb_push_cid(local, remote, q_name):
    """把改好的檔案推回手機，並在裝置上重算 md5 核對。"""
    remote = remote.rstrip("/")
    rels = ["entry.json"]
    for n in ("index.json", "video.m4s", "audio.m4s"):
        rels.append("%s/%s" % (q_name, n))
    print("\n推回裝置 : %s" % remote)
    for rel in rels:
        p = local / rel
        if not p.is_file():
            continue
        adb("push", str(p), "%s/%s" % (remote, rel))
        print("  %s" % rel)

    print("在裝置上重算 md5 核對：")
    out = adb("shell", "md5sum %s/%s/video.m4s %s/%s/audio.m4s"
              % (remote, q_name, remote, q_name))
    on_device = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            on_device[parts[1].rsplit("/", 1)[-1]] = parts[0].lower()
    ok = True
    for n in ("video.m4s", "audio.m4s"):
        local_md5 = md5_file(local / q_name / n)
        dev = on_device.get(n, "(讀不到)")
        same = dev == local_md5
        ok = ok and same
        print("  %-10s 本機 %s / 裝置 %s  %s"
              % (n, local_md5[:12], dev[:12], "相符" if same else "不符！"))
    if not ok:
        sys.exit("\n裝置上的檔案與本機不一致，傳輸可能中斷了。"
                 "請用下面的還原路徑復原後重試。")
    print("  -> 兩個檔案都與本機一致")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        description="剪掉 B站 緩存影片的工商片段，產物仍能在 B站 App 播放",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cid_dir", type=Path, nargs="?",
                    help="PC 上的 cid 目錄（裡面有 entry.json）。"
                         "用 --device 時不需要給")
    ap.add_argument("--device", metavar="手機上的路徑",
                    help="直接對手機上的 cid 目錄操作，例如 "
                         "/sdcard/Android/data/com.bilibili.app.in/download/"
                         "<avid>/<cid>。會自動拉到本機、處理、推回去、"
                         "並在裝置上重算 md5 核對。需要 adb")
    ap.add_argument("--refresh", action="store_true",
                    help="--device 模式：忽略暫存裡現有的副本，從裝置重新拉一次")
    ap.add_argument("--work-dir", type=Path, default=None,
                    help="--device 模式的暫存目錄（預設：當前目錄下的 "
                         "bili_trim_stage）。原檔會留在這裡當備份")
    ap.add_argument("--cut", action="append", required=True, type=parse_interval,
                    metavar="起-迄", help="要刪除的區間，可重複指定")
    ap.add_argument("--snap", choices=["outer", "inner"], default="outer",
                    help="視訊切點吸附方向。outer（預設）外擴到 fragment 邊界，"
                         "寧可多切一點正片；inner 內縮，寧可殘留一點廣告")
    ap.add_argument("--apply", action="store_true",
                    help="實際寫回；不加就只做乾跑")
    ap.add_argument("--preview-seams", nargs="?", const=6.0, type=float,
                    default=None, metavar="秒數",
                    help="擷取每個接縫前後各 N 秒（預設 6）的小片段，另外再從原檔"
                         "擷取被移除的那一段，用來對照確認廣告切乾淨了。"
                         "需要 ffmpeg；乾跑時也會產生")
    ap.add_argument("--preview-dir", type=Path, default=None,
                    help="預覽片段的輸出目錄（預設：當前目錄）")
    ap.add_argument("--allow-incomplete", action="store_true",
                    help="即使緩存還沒下載完也照樣剪（不建議）")
    args = ap.parse_args()

    if args.device and args.cid_dir:
        sys.exit("--device 和 cid 目錄只能給一個。")
    if not args.device and not args.cid_dir:
        sys.exit("要給 PC 上的 cid 目錄，或用 --device 指定手機上的路徑。")

    if args.device:
        args.device = normalize_device_path(args.device)
        stage = (args.work_dir or Path.cwd() / "bili_trim_stage").resolve()
        cid_dir = adb_pull_cid(args.device, stage, refresh=args.refresh)
    else:
        cid_dir = args.cid_dir.resolve()

    entry_path = cid_dir / "entry.json"
    if not entry_path.is_file():
        sys.exit("在 %s 找不到 entry.json，確認這是 cid 層目錄。" % cid_dir)

    cand = [p for p in cid_dir.iterdir()
            if p.is_dir() and (p / "video.m4s").is_file()]
    if not cand:
        sys.exit("找不到含 video.m4s 的畫質子目錄。")
    if len(cand) > 1:
        sys.exit("有多個畫質目錄：%s，請先自行保留一個。" % [p.name for p in cand])
    q_dir = cand[0]
    v_src, a_src = q_dir / "video.m4s", q_dir / "audio.m4s"
    index_path = q_dir / "index.json"
    if not a_src.is_file():
        sys.exit("找不到 audio.m4s，這支可能是舊格式，本工具不處理。")

    entry = json.loads(entry_path.read_text(encoding="utf-8"))
    index = json.loads(index_path.read_text(encoding="utf-8")) \
        if index_path.is_file() else {}

    print("緩存目錄 : %s" % cid_dir)
    print("畫質目錄 : %s" % q_dir.name)

    # 完整性：沒下載完就不要碰
    problems = []
    for label, p in (("video", v_src), ("audio", a_src)):
        actual = os.path.getsize(p)
        reps = index.get(label)
        declared = reps[0].get("size") if isinstance(reps, list) and reps else None
        if isinstance(declared, int) and declared > 0 and actual != declared:
            problems.append("%s.m4s 實際 %s，index.json 宣告 %s（差 %+d）"
                            % (label, human(actual), human(declared), actual - declared))
    if entry.get("is_completed") is False:
        problems.append("entry.json 的 is_completed = false")
    tb, db = entry.get("total_bytes"), entry.get("downloaded_bytes")
    if isinstance(tb, int) and isinstance(db, int) and 0 < db < tb:
        problems.append("downloaded_bytes %s < total_bytes %s" % (human(db), human(tb)))
    if problems:
        print("\n" + "=" * 68)
        print("這支緩存還沒下載完：")
        for x in problems:
            print("  - %s" % x)
        print("\n沒下載完的不能剪：尾端有一段還不存在，而且大小對不上。"
              "請先在 App 裡下載完再回來。")
        print("=" * 68)
        if not args.allow_incomplete:
            sys.exit("\n中止：緩存不完整。")
        print("\n已指定 --allow-incomplete，繼續。")

    vt, at = Track(v_src), Track(a_src)
    print("前綴長度 : video=%d, audio=%d bytes" % (vt.prefix_len, at.prefix_len))
    if vt.trimmed or at.trimmed:
        print("")
        print("注意     : 這支已經被本工具剪過了（尾端有 %s bytes 補大小用的 "
              "free box）。" % human(vt.tail_pad))
        print("           還是可以繼續剪，但下面的 --cut 時間是以**目前這一版**"
              "的時間軸為準，")
        print("           不是原片。把上次用過的時間再貼一次會切到正片。")
        print("")
    print("原始時長 : video %.3f s (%s) / audio %.3f s"
          % (vt.duration_s(), hhmmss(vt.duration_s()), at.duration_s()))
    vsec = vt.fragment_seconds()
    asec = at.fragment_seconds()
    print("fragment : video %d 個，平均 %.3f s / audio %d 個，平均 %.3f s"
          % (len(vsec), sum(vsec) / len(vsec), len(asec), sum(asec) / len(asec)))

    # 視訊切點吸附到 fragment 邊界
    try:
        snapped, rows = snap_to_boundaries(vt.boundaries(), args.cut, args.snap)
    except ValueError as e:
        sys.exit(str(e))
    for a, b, sa, sb in rows:
        print("  刪除 %8.2f-%8.2f -> 實際 %8.2f-%8.2f  (偏移 %+.2f / %+.2f)"
              % (a, b, sa, sb, sa - a, sb - b))

    # 兩軌各自換算成自己的 timescale
    vplan = Plan(vt, "video")
    vplan.cut_aligned([(int(round(a * vt.timescale)), int(round(b * vt.timescale)))
                       for a, b in snapped])
    aplan = Plan(at, "audio")
    aplan.cut_exact([(int(round(a * at.timescale)), int(round(b * at.timescale)))
                     for a, b in snapped])

    v_new, v_info = vplan.render(os.path.getsize(v_src))
    a_new, a_info = aplan.render(os.path.getsize(a_src))

    for label, info in (("video", v_info), ("audio", a_info)):
        print("\n%s：" % label)
        print("  移除 %d 個 fragment，精修 %d 個，保留 %d 個"
              % (info["ndrop"], info["ntrim"], info["nkeep"]))
        print("  移除時長 %.3f s；新時長 %.3f s (%s)"
              % (info["removed_s"], info["new_dur_s"], hhmmss(info["new_dur_s"])))
        print("  時長欄位 %d -> %d ms（改到 %s）"
              % (info["old_ms"], info["new_ms"], "/".join(info["touched"])))
        print("  sidx %d -> %d bytes，後補 %d；尾端補 %s bytes 回原大小"
              % (info["sidx_old"], info["sidx_new"], info["head_pad"],
                 human(info["tail_pad"])))
        print("  第一個 fragment 位移：原檔 %d，新檔 %d  -> %s"
              % (info["first_frag_orig"], info["first_frag_at"],
                 "相同" if info["first_frag_orig"] == info["first_frag_at"] else "不同"))

    drift = abs(v_info["new_dur_s"] - a_info["new_dur_s"]) - \
        abs(vt.duration_s() - at.duration_s())
    print("\n音畫同步 : 移除量 video %.3f s / audio %.3f s，相差 %.3f s"
          % (v_info["removed_s"], a_info["removed_s"],
             abs(v_info["removed_s"] - a_info["removed_s"])))
    if abs(v_info["removed_s"] - a_info["removed_s"]) > 0.05:
        print("           ⚠ 相差超過 50ms，音畫可能可感覺到不同步")

    print("\n檔案大小 : video %s（原 %s）/ audio %s（原 %s）"
          % (human(len(v_new)), human(os.path.getsize(v_src)),
             human(len(a_new)), human(os.path.getsize(a_src))))
    print("           兩者都與原檔相同 —— B站 App 不接受變小的檔案，"
             "所以省不了容量")

    if args.preview_seams is not None:
        make_previews(v_new, a_new, v_src, a_src, snapped,
                      max(args.preview_seams, 1.0),
                      (args.preview_dir or Path.cwd()).resolve(),
                      cid_dir.name)

    if not args.apply:
        print("\n[乾跑] 沒有改動任何檔案。確認上面數字無誤後加 --apply。")
        return

    backup = cid_dir.parent / (cid_dir.name + ".bak")
    if backup.exists():
        sys.exit("備份 %s 已存在，先處理掉再跑。" % backup)
    shutil.copytree(cid_dir, backup)
    print("\n已備份   : %s" % backup)

    write_back(cid_dir, q_dir, v_new, a_new, v_info, a_info, snapped)

    if args.device:
        adb_push_cid(cid_dir, args.device, q_dir.name)
        print("\n已寫回手機 %s" % args.device)
        print("原檔備份留在 %s" % backup)
        print("要還原手機上的版本，把備份推回去：")
        print("  adb push \"%s/entry.json\" \"%s/entry.json\""
              % (backup, args.device.rstrip("/")))
        for n in ("index.json", "audio.m4s", "video.m4s"):
            print("  adb push \"%s/%s/%s\" \"%s/%s/%s\""
                  % (backup, q_dir.name, n, args.device.rstrip("/"),
                     q_dir.name, n))
        return

    print("\n已寫回 %s" % q_dir)
    if os.name == "nt":
        print("還原指令（PowerShell）：")
        print("  Remove-Item -LiteralPath '%s' -Recurse -Force; "
              "Move-Item -LiteralPath '%s' '%s'" % (cid_dir, backup, cid_dir))
    else:
        print("還原指令：rm -rf '%s' && mv '%s' '%s'" % (cid_dir, backup, cid_dir))


if __name__ == "__main__":
    main()
