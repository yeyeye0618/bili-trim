# -*- coding: utf-8 -*-
"""
bili_backup.py — 把一支緩存影片收成「一個檔案」的備份，以及還原回去。

為什麼不是直接留一個資料夾
--------------------------
資料夾很容易被誤刪一半、被同步工具拆開、看不出裡面是哪支影片
（目錄名是 cid，一串數字看不出是什麼）。所以備份是一個 .bilibak 檔案，
檔名就是影片標題，裡面附一份 manifest 記下它是哪支、原本在手機的哪裡、
每個檔案的大小與 md5 —— 還原的時候就能原地放回去並驗證。

.bilibak 其實就是一個未壓縮的 zip：

    manifest.json
    payload/entry.json
    payload/danmaku.xml
    payload/<type_tag>/index.json
    payload/<type_tag>/video.m4s
    payload/<type_tag>/audio.m4s

影音本身已經是壓縮過的，再壓一次只會慢而且省不到東西，所以用 ZIP_STORED。
"""

import hashlib
import json
import os
import time
import zipfile
from pathlib import Path

EXT = ".bilibak"
MAGIC = "bili_trim_backup/1"
MANIFEST = "manifest.json"
PAYLOAD = "payload/"
_CHUNK = 1 << 22          # 4 MB


class BackupError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 路徑
# --------------------------------------------------------------------------- #

def unique_path(backup_dir, stem, ext=EXT):
    """<備份目錄>/<標題>.bilibak，撞名就加 (2)、(3)。"""
    backup_dir = Path(backup_dir)
    cand = backup_dir / (stem + ext)
    n = 2
    while cand.exists():
        cand = backup_dir / ("%s (%d)%s" % (stem, n, ext))
        n += 1
    return cand


def _walk(root):
    """cid 目錄底下所有檔案，回傳相對路徑（用 / 當分隔，zip 要的）。"""
    root = Path(root)
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out.append(p.relative_to(root).as_posix())
    return out


# --------------------------------------------------------------------------- #
# 建立
# --------------------------------------------------------------------------- #

def create(cid_dir, dest, item=None, extra=None, progress=None,
           skip=(".bili_gui_stage.json",)):
    """
    把 cid_dir 整個收進 dest（一個 .bilibak 檔）。
    item 是 bili_device.CacheItem，用來填 manifest；沒有也能做，只是資訊少。
    回傳 manifest dict。
    """
    cid_dir = Path(cid_dir)
    dest = Path(dest)
    if not cid_dir.is_dir():
        raise BackupError("找不到要備份的目錄：%s" % cid_dir)
    rels = [r for r in _walk(cid_dir) if r not in skip]
    if not rels:
        raise BackupError("%s 裡面沒有檔案" % cid_dir)

    grand = sum((cid_dir / r).stat().st_size for r in rels) or 1
    done = [0]
    files = []

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        tmp.unlink()

    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED,
                             allowZip64=True) as zf:
            for rel in rels:
                src = cid_dir / rel
                size = src.stat().st_size
                info = zipfile.ZipInfo(PAYLOAD + rel,
                                       date_time=time.localtime(
                                           src.stat().st_mtime)[:6])
                info.compress_type = zipfile.ZIP_STORED
                info.file_size = size
                h = hashlib.md5()
                with src.open("rb") as fin, \
                        zf.open(info, "w", force_zip64=True) as fout:
                    while True:
                        buf = fin.read(_CHUNK)
                        if not buf:
                            break
                        h.update(buf)
                        fout.write(buf)
                        done[0] += len(buf)
                        if progress:
                            progress(done[0], grand, "備份 %s" % rel)
                files.append({"rel": rel, "size": size, "md5": h.hexdigest()})

            man = _manifest(item, files, extra)
            zf.writestr(MANIFEST, json.dumps(man, ensure_ascii=False, indent=1))
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise

    if dest.exists():
        dest.unlink()
    tmp.replace(dest)
    return man


def _manifest(item, files, extra):
    man = {
        "format": MAGIC,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "created_unix": int(time.time()),
        "total_bytes": sum(f["size"] for f in files),
        "files": files,
    }
    if item is not None:
        man.update({
            "title": item.title,
            "part": item.part,
            "display_name": item.display_name,
            "owner": item.owner,
            "bvid": item.bvid,
            "avid": item.avid,
            "cid": item.cid,
            "quality_dir": item.quality_dir,
            "quality_desc": item.quality_desc,
            "duration_ms": item.duration_ms,
            "package": item.root.package if item.root else "",
            "package_label": item.root.label if item.root else "",
            "device_path": item.device_path,
        })
    if extra:
        man.update(extra)
    return man


# --------------------------------------------------------------------------- #
# 讀取
# --------------------------------------------------------------------------- #

class BackupInfo(object):
    def __init__(self, path, manifest):
        self.path = Path(path)
        self.m = manifest or {}

    # 下面這些都容許 manifest 缺欄位，舊檔或手改過的檔也看得懂
    @property
    def display_name(self):
        return self.m.get("display_name") or self.m.get("title") or self.path.stem

    @property
    def cid(self):
        return self.m.get("cid") or ""

    @property
    def device_path(self):
        return self.m.get("device_path") or ""

    @property
    def quality_dir(self):
        return str(self.m.get("quality_dir") or "")

    @property
    def duration_ms(self):
        return self.m.get("duration_ms") or 0

    @property
    def created_at(self):
        return self.m.get("created_at") or ""

    @property
    def total_bytes(self):
        return self.m.get("total_bytes") or self.path.stat().st_size

    @property
    def files(self):
        return self.m.get("files") or []

    @property
    def package(self):
        return self.m.get("package") or ""

    def __repr__(self):
        return "<BackupInfo %s>" % self.path.name


def read_manifest(path):
    """只讀 manifest，不解開內容（很快，zip 的中央目錄就有）。"""
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as zf:
            raw = zf.read(MANIFEST).decode("utf-8")
    except KeyError:
        raise BackupError("%s 裡沒有 manifest.json，不是本工具做的備份。"
                          % path.name)
    except (zipfile.BadZipFile, OSError) as e:
        raise BackupError("讀不開 %s：%s" % (path.name, e))
    try:
        man = json.loads(raw)
    except ValueError:
        raise BackupError("%s 的 manifest.json 壞了。" % path.name)
    if man.get("format") != MAGIC:
        raise BackupError("%s 的備份格式是 %s，這個版本看不懂。"
                          % (path.name, man.get("format")))
    return man


def info(path):
    return BackupInfo(path, read_manifest(path))


def list_backups(backup_dir):
    """掃一個目錄裡所有 .bilibak，新的在前。壞檔會被跳過。"""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return [], []
    good, bad = [], []
    for p in sorted(backup_dir.glob("*" + EXT)):
        try:
            good.append(BackupInfo(p, read_manifest(p)))
        except BackupError as e:
            bad.append((p, str(e)))
    good.sort(key=lambda b: -(b.m.get("created_unix") or 0))
    return good, bad


# --------------------------------------------------------------------------- #
# 還原
# --------------------------------------------------------------------------- #

def extract(path, dest_dir, progress=None, verify=True):
    """
    把備份解到 dest_dir（會建立 dest_dir 本身，裡面直接是 entry.json 等）。
    verify=True 時逐檔核對 manifest 裡的 md5。回傳 (BackupInfo, 本機目錄)。
    """
    path = Path(path)
    dest_dir = Path(dest_dir)
    bi = info(path)
    want = {f["rel"]: f for f in bi.files}
    grand = sum(f["size"] for f in bi.files) or 1
    done = 0

    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.startswith(PAYLOAD)
                 and not n.endswith("/")]
        if not names:
            raise BackupError("%s 裡沒有 payload，備份是空的。" % path.name)
        for name in names:
            rel = name[len(PAYLOAD):]
            out = dest_dir / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.md5()
            with zf.open(name) as fin, out.open("wb") as fout:
                while True:
                    buf = fin.read(_CHUNK)
                    if not buf:
                        break
                    h.update(buf)
                    fout.write(buf)
                    done += len(buf)
                    if progress:
                        progress(done, grand, "還原 %s" % rel)
            if verify and rel in want:
                exp = want[rel]
                got = out.stat().st_size
                if got != exp["size"]:
                    raise BackupError("%s 還原後大小不符（應 %d，得 %d），"
                                      "備份可能損壞。" % (rel, exp["size"], got))
                if h.hexdigest() != exp["md5"]:
                    raise BackupError("%s 還原後 md5 不符，備份可能損壞。" % rel)
    return bi, dest_dir


def verify(path, progress=None):
    """只驗不解：把內容讀過一遍，核對 md5。回傳 [(rel, ok, 說明)]。"""
    bi = info(path)
    want = {f["rel"]: f for f in bi.files}
    grand = sum(f["size"] for f in bi.files) or 1
    done = 0
    report = []
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if not name.startswith(PAYLOAD) or name.endswith("/"):
                continue
            rel = name[len(PAYLOAD):]
            h = hashlib.md5()
            size = 0
            with zf.open(name) as fin:
                while True:
                    buf = fin.read(_CHUNK)
                    if not buf:
                        break
                    h.update(buf)
                    size += len(buf)
                    done += len(buf)
                    if progress:
                        progress(done, grand, "核對 %s" % rel)
            exp = want.get(rel)
            if exp is None:
                report.append((rel, True, "manifest 沒記，略過核對"))
            elif size != exp["size"]:
                report.append((rel, False, "大小不符"))
            elif h.hexdigest() != exp["md5"]:
                report.append((rel, False, "md5 不符"))
            else:
                report.append((rel, True, "相符"))
    return bi, report
