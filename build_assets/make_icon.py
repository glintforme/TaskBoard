# -*- coding: utf-8 -*-
"""生成应用图标 icon.ico（64x64 蓝色圆角方块 + 白色对勾，纯标准库手写 PNG/ICO）。"""
import struct
import zlib
import os


def make_png(size=64):
    # 圆角半径
    rad = size * 0.22
    cx, cy = size * 0.30, size * 0.52   # 对勾中心
    raw = b""
    for y in range(size):
        row = bytearray()
        for x in range(size):
            # 圆角矩形判断
            rx = min(x, size - 1 - x)
            ry = min(y, size - 1 - y)
            inside = True
            if rx < rad and ry < rad:
                dx, dy = rad - rx, rad - ry
                inside = dx * dx + dy * dy <= rad * rad
            if inside:
                r, g, b = 0x5B, 0x8C, 0xFF
            else:
                r = g = b = 0
            # 白色对勾：两条线段
            if inside:
                # 线段1：(0.18,0.52)->(0.38,0.70)；线段2：(0.38,0.70)->(0.82,0.34)
                for (x1, y1, x2, y2) in ((0.18, 0.52, 0.38, 0.70), (0.38, 0.70, 0.82, 0.34)):
                    px, py = x / size, y / size
                    # 点到线段距离
                    vx, vy = x2 - x1, y2 - y1
                    wx, wy = px - x1, py - y1
                    t = max(0.0, min(1.0, (wx * vx + wy * vy) / (vx * vx + vy * vy)))
                    dx = wx - t * vx
                    dy = wy - t * vy
                    if dx * dx + dy * dy <= (0.05) ** 2:
                        r = g = b = 255
                        break
            row += bytes((r, g, b))
        raw += b"\x00" + bytes(row)

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def make_ico(png, path):
    # ICO 头 + 一个 PNG 条目（Vista+ 支持 PNG 压缩条目）
    with open(path, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, 1))
        f.write(struct.pack("<BBBBHHII", 64, 64, 0, 0, 1, 32, len(png), 22))
        f.write(png)


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build_assets", "icon.ico")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    make_ico(make_png(), out)
    print("ICON_OK", os.path.abspath(out), os.path.getsize(out), "bytes")
