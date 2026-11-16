# -*- coding: utf-8 -*-
"""
bili_media.py — 預覽用的影格來源與播放器（底層是 ffmpeg / ffplay）。

GUI 需要兩種東西：

  拖時間軸的時候     -> 單張影格，愈快愈好（Scrubber）
  按播放的時候       -> 連續影格 + 聲音（Player）

兩者都是把 ffmpeg 解出來的 rawvideo 餵進 Dear PyGui 的 raw texture。
實測（1280x720 HEVC 緩存）：
  單張影格 -ss 定位  約 70 ms，而且是影格精準的（用 -copyts 驗過）
  連續解碼到 854x480 約 650 fps，遠超過即時播放需要的 30 fps

聲音用 ffplay -nodisp 另外一個行程播。這樣不用額外的音訊套件，
代價是起播瞬間影音可能有一點點偏移，所以留了一個可調的 audio_offset。
"""

import json
import os
import queue
import shutil
import subprocess
import threading
import time

import numpy as np

import bili_env as ENV

_NO_WINDOW = 0
if os.name == "nt":
    _NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


class MediaError(RuntimeError):
    pass


def have_ffmpeg():
    return ENV.have("ffmpeg") and ENV.have("ffprobe")


def have_ffplay():
    return ENV.have("ffplay")


def _popen(args, **kw):
    return subprocess.Popen(args, creationflags=_NO_WINDOW, **kw)


# --------------------------------------------------------------------------- #
# 探測
# --------------------------------------------------------------------------- #

def probe(path):
    """回傳 {width, height, fps, duration, codec}。"""
    exe = ENV.find("ffprobe")
    if exe is None:
        raise MediaError(ENV.missing_hint("ffprobe"))
    exe = str(exe)
    r = subprocess.run(
        [exe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate,codec_name,duration",
         "-of", "json", str(path)],
        capture_output=True, creationflags=_NO_WINDOW)
    if r.returncode:
        raise MediaError("ffprobe 讀不動 %s：%s"
                         % (path, r.stderr.decode("utf-8", "replace")[:200]))
    try:
        st = json.loads(r.stdout.decode("utf-8", "replace"))["streams"][0]
    except (ValueError, KeyError, IndexError):
        raise MediaError("%s 裡找不到視訊串流。" % path)
    num, _, den = (st.get("r_frame_rate") or "30/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    return {"width": int(st.get("width") or 0),
            "height": int(st.get("height") or 0),
            "fps": fps if fps > 0 else 30.0,
            "duration": float(st.get("duration") or 0.0),
            "codec": st.get("codec_name") or ""}


# --------------------------------------------------------------------------- #
# 影格來源
# --------------------------------------------------------------------------- #

class FrameSource(object):
    """
    固定輸出尺寸的影格來源。畫面不是這個比例就加黑邊（ffmpeg 的 pad），
    這樣 texture 可以建一次用到底，不必隨影片換尺寸重建。
    """

    def __init__(self, video_path, out_w=854, out_h=480):
        self.path = str(video_path)
        self.out_w = int(out_w)
        self.out_h = int(out_h)
        self.info = probe(self.path)
        self.fps = self.info["fps"]
        self.frame_bytes = self.out_w * self.out_h * 4
        self._vf = ("scale=%d:%d:force_original_aspect_ratio=decrease,"
                    "pad=%d:%d:(ow-iw)/2:(oh-ih)/2"
                    % (self.out_w, self.out_h, self.out_w, self.out_h))

    def _args(self, t, duration=None, frames=None):
        a = [ENV.path("ffmpeg"), "-v", "error", "-nostdin"]
        if t and t > 0:
            a += ["-ss", "%.3f" % t]       # -i 之前 = 快速定位，而且是影格精準的
        a += ["-i", self.path]
        if duration is not None:
            a += ["-t", "%.3f" % max(duration, 0.04)]
        if frames is not None:
            a += ["-frames:v", str(frames)]
        a += ["-vf", self._vf, "-f", "rawvideo", "-pix_fmt", "rgba", "-"]
        return a

    def grab(self, t):
        """單張影格，回傳 uint8 的 ndarray（長度 = frame_bytes），失敗回 None。"""
        p = _popen(self._args(t, frames=1), stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL)
        try:
            buf = read_exact(p.stdout, self.frame_bytes)
            p.stdout.close()
            p.wait(timeout=20)
        except (subprocess.TimeoutExpired, OSError):
            p.kill()
            return None
        if buf is None:
            return None
        return np.frombuffer(buf, dtype=np.uint8)

    def open_stream(self, t, duration=None):
        """開一個連續解碼的管線，回傳 Popen。"""
        return _popen(self._args(t, duration), stdout=subprocess.PIPE,
                      stderr=subprocess.DEVNULL, bufsize=self.frame_bytes)


def read_exact(stream, n):
    """
    管線上的 read(n) 不保證給滿 n 個 byte（尤其未緩衝的時候），
    而影格必須整張才能用，所以自己湊滿。回傳 None 表示串流結束。
    """
    chunks, got = [], 0
    while got < n:
        try:
            b = stream.read(n - got)
        except (OSError, ValueError):
            return None
        if not b:
            return None
        chunks.append(b)
        got += len(b)
    return b"".join(chunks) if len(chunks) > 1 else chunks[0]


# --------------------------------------------------------------------------- #
# 拖曳用：單張影格，最後一次請求優先
# --------------------------------------------------------------------------- #

class Scrubber(object):
    """
    背景執行緒抓單張影格。拖時間軸會瘋狂發請求，所以只留最後一個，
    中間的直接丟掉 —— 使用者要看的是放手的那一格。
    """

    def __init__(self):
        self._src = None
        self._want = None
        self._result = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def set_source(self, src):
        with self._lock:
            self._src = src
            self._want = None
            self._result = None

    def request(self, t):
        with self._lock:
            self._want = float(t)
        self._wake.set()

    def take(self):
        """拿走已完成的結果 (t, frame)；沒有就回 None。"""
        with self._lock:
            r, self._result = self._result, None
        return r

    def shutdown(self):
        self._stop = True
        self._wake.set()

    def _loop(self):
        while not self._stop:
            self._wake.wait(0.2)
            self._wake.clear()
            while not self._stop:
                with self._lock:
                    src, t = self._src, self._want
                    self._want = None
                if src is None or t is None:
                    break
                frame = src.grab(t)
                if frame is not None:
                    with self._lock:
                        # 這段時間內又有新請求，就讓下一輪蓋掉它
                        self._result = (t, frame)


# --------------------------------------------------------------------------- #
# 播放
# --------------------------------------------------------------------------- #

class Player(object):
    """
    照著一串「要播的區段」依序播。想預覽剪完的成品就把保留區段餵進來，
    播到區段尾端會自己跳到下一段開頭 —— 那個接點就是剪輯的接縫。

    呼叫端每一幀叫一次 tick()，拿回 (來源時間, 影格或 None)。
    """

    QUEUE = 90          # 約 3 秒的緩衝（30fps）

    def __init__(self, src, audio_path=None):
        self.src = src
        self.audio_path = str(audio_path) if audio_path else None
        self.segments = [(0.0, src.info["duration"] or 0.0)]
        self.volume = 70
        self.muted = False
        self.audio_offset = 0.0      # 正值 = 聲音晚一點開始
        self.audio_error = None

        self._proc = None
        self._aproc = None
        self._thread = None
        self._q = queue.Queue(maxsize=self.QUEUE)
        self._gen = 0                # 換段／停止時讓舊執行緒自己退場
        self._seg = 0
        self._seg_t0 = 0.0
        self._wall0 = 0.0
        self._served = 0
        self._last = None
        self._eof = False
        self.playing = False
        self.pos = 0.0

    # ---- 設定 ----

    def set_segments(self, segments):
        self.segments = [(float(a), float(b)) for a, b in segments if b > a] \
                        or [(0.0, self.src.info["duration"] or 0.0)]
        # 播放中改區段（使用者邊播邊拖邊緣）時，索引可能指到不存在的段
        self._seg = max(0, min(self._seg, len(self.segments) - 1))

    def total_playable(self):
        return sum(b - a for a, b in self.segments)

    def source_to_output(self, t):
        """來源時間 -> 成品時間軸上的位置。"""
        out = 0.0
        for a, b in self.segments:
            if t < a:
                return out
            if t <= b:
                return out + (t - a)
            out += b - a
        return out

    def output_to_source(self, t):
        """成品時間 -> 來源時間。"""
        left = max(t, 0.0)
        for a, b in self.segments:
            span = b - a
            if left <= span:
                return a + left
            left -= span
        return self.segments[-1][1] if self.segments else 0.0

    # ---- 控制 ----

    def play(self, t=None):
        t = self.pos if t is None else float(t)
        idx = 0
        for i, (a, b) in enumerate(self.segments):
            if t < b:
                idx = i
                t = max(t, a)
                break
        else:
            idx, t = 0, self.segments[0][0]
        self._start_segment(idx, t)
        self.playing = True

    def pause(self):
        self.playing = False
        self._teardown()

    def stop(self):
        self.pause()
        self.pos = self.segments[0][0] if self.segments else 0.0
        self._last = None

    def seek(self, t):
        was = self.playing
        self._teardown()
        self.pos = float(t)
        if was:
            self.play(self.pos)
        else:
            self.playing = False

    def shutdown(self):
        self._teardown()

    # ---- 內部 ----

    def _teardown(self):
        self._gen += 1
        p, self._proc = self._proc, None
        a, self._aproc = self._aproc, None
        for proc in (p, a):
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        if p is not None and p.stdout is not None:
            try:
                p.stdout.close()
            except OSError:
                pass
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._eof = False

    def _start_segment(self, idx, t):
        self._teardown()
        self._seg = idx
        a, b = self.segments[idx]
        t = min(max(t, a), b)
        self._seg_t0 = t
        self._served = 0
        self.pos = t

        self._proc = self.src.open_stream(t, duration=b - t)
        gen = self._gen
        self._thread = threading.Thread(target=self._read_loop,
                                        args=(self._proc, gen), daemon=True)
        self._thread.start()

        if self.audio_path and not self.muted and ENV.have("ffplay"):
            at = t + self.audio_offset
            # 注意：這裡不能加 -nostdin。那是 ffmpeg 的選項，ffplay 沒有，
            # 它會把 -nostdin 當成「要吃一個值的選項」而吞掉後面的 -volume，
            # 然後直接結束 —— 配上 -loglevel quiet 就是無聲無息地沒有聲音。
            self._aproc = _popen(
                [ENV.path("ffplay"), "-nodisp", "-autoexit",
                 "-loglevel", "error",
                 "-volume", str(int(self.volume)),
                 "-ss", "%.3f" % max(at, 0), "-t", "%.3f" % max(b - t, 0.1),
                 "-i", self.audio_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL)
            threading.Thread(target=self._audio_watch,
                             args=(self._aproc, self._gen), daemon=True).start()
        self._wall0 = time.perf_counter()

    def _audio_watch(self, proc, gen):
        """
        ffplay 一開就死的話把原因記下來 —— 不然聲音沒出來完全沒有線索。
        活過 1.5 秒就當它正常在播，不去打擾它。
        """
        try:
            _out, err = proc.communicate(timeout=1.5)
        except subprocess.TimeoutExpired:
            return
        if gen == self._gen and proc.returncode:
            self.audio_error = (err or b"").decode("utf-8", "replace").strip()[:300] \
                or ("ffplay 以 %s 結束" % proc.returncode)

    def take_audio_error(self):
        """拿走並清掉上一次的音訊錯誤（GUI 用來只報一次）。"""
        err, self.audio_error = self.audio_error, None
        return err

    def _read_loop(self, proc, gen):
        n = self.src.frame_bytes
        stdout = proc.stdout
        while gen == self._gen:
            buf = read_exact(stdout, n)
            if buf is None:
                break
            frame = np.frombuffer(buf, dtype=np.uint8)
            while gen == self._gen:
                try:
                    self._q.put(frame, timeout=0.2)
                    break
                except queue.Full:
                    continue
        if gen == self._gen:
            self._eof = True

    def tick(self):
        """
        每一幀叫一次。回傳 (來源時間, 影格或 None)；
        影格是 None 表示畫面不用換（還沒到下一格的時間）。
        """
        if not self.playing:
            return self.pos, None

        self._seg = max(0, min(self._seg, len(self.segments) - 1))
        a, b = self.segments[self._seg]
        elapsed = time.perf_counter() - self._wall0
        want = int(elapsed * self.src.fps)
        self.pos = min(self._seg_t0 + elapsed, b)

        frame = None
        while self._served <= want:
            try:
                frame = self._q.get_nowait()
            except queue.Empty:
                break
            self._served += 1

        if frame is not None:
            self._last = frame

        # 這一段播完了：跳到下一段（接縫就在這裡）
        done = self.pos >= b - 1e-3 or (self._eof and self._q.empty())
        if done:
            if self._seg + 1 < len(self.segments):
                nxt = self._seg + 1
                self._start_segment(nxt, self.segments[nxt][0])
            else:
                self.playing = False
                self._teardown()
                self.pos = b
        return self.pos, frame
