# -*- coding: utf-8 -*-
"""程序化纹理生成（零外部资源文件）。

- checker_bytes : 黑白棋盘格（地面），RGB bytes 供 moderngl 上传
- solid_bytes   : 纯色 1x1 纹理（占位/默认白）
- decal_bytes   : 弹痕贴图，RGBA（弹孔 + 放射裂纹 + 边缘羽化）
"""
import math
import struct


def checker_bytes(size=256, cells=4, c1=(170, 222, 155), c2=(146, 202, 133)):
    """生成 size x size 棋盘格 RGB 数据。cells：单边格子数。"""
    cell = size // cells
    rows = []
    row_even = bytearray()
    row_odd = bytearray()
    for x in range(size):
        idx = (x // cell) % 2
        c_even = c1 if idx == 0 else c2
        c_odd = c2 if idx == 0 else c1
        row_even.extend(struct.pack("3B", *c_even))
        row_odd.extend(struct.pack("3B", *c_odd))
    for y in range(size):
        rows.append(bytes(row_even if (y // cell) % 2 == 0 else row_odd))
    return b"".join(rows)


def solid_bytes(rgb=(255, 255, 255)):
    """1x1 纯色纹理。"""
    return struct.pack("3B", *rgb)


def decal_bytes(size=64, hole_r=0.30, core=(26, 22, 30), rim=(64, 58, 68),
                ring_r=0.46, fade_r=0.50, cracks=7, seed=12345):
    """弹痕贴图：RGBA，中央弹孔 + 放射裂纹，外缘 alpha 羽化到 0。

    为什么需要 alpha：主 pass 不开混合（只有 HUD pass 开），弹痕必须走
    独立的 decal pass（BLEND + DEPTH_TEST）才能淡出；所以这张图是 RGBA。

    参数（单位为贴图边长比例，0~0.5）：
      hole_r 弹孔半径 · core 弹孔色 · rim 裂纹色
      ring_r 裂纹终止半径 · fade_r alpha 归零半径 · cracks 裂纹条数
    """
    rows = []
    # 裂纹角度：等分 + 固定抖动，保证同一 seed 下每次生成完全一致
    step = math.tau / cracks
    angles = [(i * step + (seed % 7) * 0.11) % math.tau for i in range(cracks)]

    for y in range(size):
        row = bytearray()
        fy = (y + 0.5) / size - 0.5          # -> [-0.5, 0.5]
        for x in range(size):
            fx = (x + 0.5) / size - 0.5
            d = math.hypot(fx, fy)

            # 边缘 4 像素做 0.5texel 的软化，避免采样时出现硬锯齿边
            edge = min(fx, fy, 0.5 - fx, 0.5 - fy)
            if edge < 0.5 / size:
                row.extend(b"\x00\x00\x00\x00")
                continue

            if d <= hole_r:
                col, a = core, 255                       # 弹孔
            elif d <= ring_r:
                # 放射裂纹：到裂纹角距离足够近则上色，并随半径衰减
                ang = math.atan2(fy, fx)
                a_ang = min(abs(((ang - a0 + math.pi) % math.tau) - math.pi)
                            for a0 in angles)
                w = 0.030 * (1.0 - (d - hole_r) / (ring_r - hole_r))
                if a_ang < w:
                    col, a = rim, 255
                else:
                    col, a = rim, 0
            else:
                col, a = rim, 0

            # 羽化：ring_r 之后线性淡出到 fade_r，之外全透明
            if a and d > ring_r * 0.92:
                k = 1.0 - (d - ring_r * 0.92) / (fade_r - ring_r * 0.92)
                a = int(max(0, min(255, a * max(0.0, k))))
            row.extend(struct.pack("4B", col[0], col[1], col[2], a))
        rows.append(bytes(row))
    return b"".join(rows)
