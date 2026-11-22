# -*- coding: utf-8 -*-
"""
bili_qr.py — 最小的 QR 圖碼產生器，只用標準庫。

只為了一件事：在視窗裡畫出「無線偵錯 → 使用 QR 圖碼配對裝置」要掃的那張圖
（WIFI:T:ADB;S:<名稱>;P:<密碼>;; 大約 40 個字元）。所以只做位元組模式、
錯誤更正等級 M、版本 1–6（最多 106 個位元組），不做版本資訊區塊（版本 7 起才有）。

照 ISO/IEC 18004 的步驟寫，結構參考 Project Nayuki 的 qrcodegen（MIT）。

    matrix(text) -> [[bool, ...], ...]   True = 黑格，不含四周留白
"""

# 版本 -> (總碼字數, 每塊的錯誤更正碼字數, 塊數)，等級 M
_M = {1: (26, 10, 1), 2: (44, 16, 1), 3: (70, 26, 1),
      4: (100, 18, 2), 5: (134, 24, 2), 6: (172, 16, 4)}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}
_FORMAT_M = 0       # 格式資訊裡 M 的兩個位元


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _codewords(data, version):
    total, ec_len, blocks = _M[version]
    n_data = total - ec_len * blocks
    bits = []

    def put(val, n):
        bits.extend((val >> i) & 1 for i in reversed(range(n)))
    put(0b0100, 4)                      # 位元組模式
    put(len(data), 8)                   # 版本 1–9 的字數欄位是 8 位元
    for b in data:
        put(b, 8)
    cap = n_data * 8
    put(0, min(4, cap - len(bits)))     # 結束符
    put(0, (-len(bits)) % 8)
    out = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(out) < n_data:
        out.append(pad)
        pad ^= 0xEC ^ 0x11
    per = n_data // blocks              # 版本 1–6 的 M 每塊一樣長
    div = _rs_divisor(ec_len)
    dblocks = [out[i * per:(i + 1) * per] for i in range(blocks)]
    eblocks = [_rs_remainder(b, div) for b in dblocks]
    res = []
    for i in range(per):
        res.extend(b[i] for b in dblocks)
    for i in range(ec_len):
        res.extend(b[i] for b in eblocks)
    return res


class _Grid(object):
    def __init__(self, version):
        self.v = version
        self.n = 17 + 4 * version
        self.mod = [[False] * self.n for _ in range(self.n)]
        self.fn = [[False] * self.n for _ in range(self.n)]

    def set_fn(self, x, y, dark):
        self.mod[y][x] = dark
        self.fn[y][x] = True

    def draw_function(self):
        n = self.n
        for i in range(n):
            self.set_fn(6, i, i % 2 == 0)
            self.set_fn(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.set_fn(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _ALIGN[self.v]
        last = len(pos) - 1
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_fn(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)             # 先佔位，選好遮罩再畫一次

    def draw_format(self, mask):
        data = _FORMAT_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 != 0
        n = self.n
        for i in range(6):
            self.set_fn(8, i, bit(i))
        self.set_fn(8, 7, bit(6))
        self.set_fn(8, 8, bit(7))
        self.set_fn(7, 8, bit(8))
        for i in range(9, 15):
            self.set_fn(14 - i, 8, bit(i))
        for i in range(8):
            self.set_fn(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_fn(8, n - 15 + i, bit(i))
        self.set_fn(8, n - 8, True)     # 固定的那一格黑格

    def draw_codewords(self, data):
        n, i, total = self.n, 0, len(data) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.fn[y][x] and i < total:
                        self.mod[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 != 0
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        f = (lambda x, y: (x + y) % 2 == 0,
             lambda x, y: y % 2 == 0,
             lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0,
             lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0,
             lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)[mask]
        for y in range(self.n):
            for x in range(self.n):
                if not self.fn[y][x] and f(x, y):
                    self.mod[y][x] = not self.mod[y][x]

    def penalty(self):
        """ISO 的四條扣分規則，用來挑最好掃的遮罩。"""
        n, m, score = self.n, self.mod, 0
        lines = [m[y] for y in range(n)] + [[m[y][x] for y in range(n)] for x in range(n)]
        pat1 = [True, False, True, True, True, False, True, False, False, False, False]
        pat2 = pat1[::-1]
        for line in lines:
            run, prev = 0, None
            for c in line:
                if c == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, c
            if run >= 5:
                score += run - 2
            for i in range(n - 10):
                seg = line[i:i + 11]
                if seg == pat1 or seg == pat2:
                    score += 40
        for y in range(n - 1):
            for x in range(n - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(r) for r in m)
        k = abs(dark * 20 - n * n * 10) // (n * n)
        return score + k * 10


def matrix(text):
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    for version in sorted(_M):
        total, ec_len, blocks = _M[version]
        if 12 + len(data) * 8 <= (total - ec_len * blocks) * 8:
            break
    else:
        raise ValueError("QR 內容太長（%d 位元組）" % len(data))
    words = _codewords(data, version)
    best, best_score = None, None
    for mask in range(8):
        g = _Grid(version)
        g.draw_function()
        g.draw_codewords(words)
        g.apply_mask(mask)
        g.draw_format(mask)
        s = g.penalty()
        if best_score is None or s < best_score:
            best, best_score = g, s
    return best.mod


def adb_pairing_text(name, password):
    """Android「使用 QR 圖碼配對裝置」看得懂的內容（跟 Android Studio 的格式一樣）。"""
    return "WIFI:T:ADB;S:%s;P:%s;;" % (name, password)
