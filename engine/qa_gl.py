# -*- coding: utf-8 -*-
"""GL 合成层回归检查（`python main.py --glcheck`）。

为什么需要单独一层：HUD 的链路是「pygame 画一张全屏 Surface → 上传成
纹理 → 用一个全屏 TRIANGLE_STRIP quad 贴回屏幕」。`--selftest` 全程
headless、不建 GL 上下文，所以它断言得了布局与玩法逻辑，但断言不了
**最后这一步贴屏**。

已知真 bug（2026-09-30 修，ENG-007）：quad 顶点顺序写成
左下→右下→右上→左上 时，TRIANGLE_STRIP 的两个三角形
（{v0,v1,v2} 与 {v1,v2,v3}）都落在屏幕右半边，左侧留下一个以画面中心
为顶点的楔形空洞——HUD 只覆盖 75% 屏幕，观感是「蝴蝶结 / 两个梯形
拼接 / 中间接缝 / 文字被斜边切断」。正确顺序：左下→右下→左上→右上。
该缺陷只在真窗口 GL 合成里可见，任何「只渲染 HUD Surface」的截图都
必然正常，所以布局层的实测数据与它无关。

检查方式（真窗口 + 真 GL 链路，窗口放到屏幕外不打扰用户）：
把 HUD 换成一张已知图案（纯色底 + 四角各一块标记色），渲染一帧后读回
默认帧缓冲，断言两件事：
  1. 覆盖：每个像素都必须是图案里的颜色（旧顶点顺序会漏掉左侧楔形）；
  2. 方向：四角标记色必须落在正确的屏幕角（防顶点/UV 配对错位导致翻转）。
"""
import os

# ---- 检查图案：底色 + 四角标记色（顺序即 左上/右上/左下/右下）----
_FILL = (255, 0, 255)
_CORNER_COLORS = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0))
_CORNER_NAMES = ("左上", "右上", "左下", "右下")
_MARK_PX = 16          # 四角标记块边长（像素）


def _make_pattern(width, height):
    """生成检查图案，返回 (Surface, 标记块边长)。"""
    import pygame
    m = max(2, min(_MARK_PX, width // 4, height // 4))
    s = pygame.Surface((width, height), pygame.SRCALPHA)
    s.fill(_FILL + (255,))
    s.fill(_CORNER_COLORS[0] + (255,), (0, 0, m, m))
    s.fill(_CORNER_COLORS[1] + (255,), (width - m, 0, m, m))
    s.fill(_CORNER_COLORS[2] + (255,), (0, height - m, m, m))
    s.fill(_CORNER_COLORS[3] + (255,), (width - m, height - m, m, m))
    return s, m


def run_glcheck(width=1280, height=720, frames=4):
    """跑一次 GL 合成检查；返回退出码：0 = 通过或跳过，1 = 失败。"""
    # 窗口放屏幕外：检查过程不该在用户桌面上弹一个游戏窗口
    os.environ.setdefault("SDL_VIDEO_WINDOW_POS", "-32000,-32000")

    import pygame

    from engine.core import Engine
    from game.app import App

    # 检查不抢用户鼠标（Engine.run 启动时会捕获鼠标）
    saved_capture = Engine.capture_mouse
    saved_release = Engine.release_mouse
    Engine.capture_mouse = lambda self: None
    Engine.release_mouse = lambda self: None
    try:
        try:
            app = App(headless=False, width=width, height=height)
        except Exception as exc:              # 无 GL / dummy 视频驱动
            print("[glcheck] SKIP: 创建窗口或 GL 上下文失败：%s" % exc)
            print("[glcheck] 本检查需要 OpenGL 3.3 上下文；无显卡环境请用 --selftest。")
            return 0

        ctx = app.renderer.ctx
        print("[glcheck] GL_RENDERER = %s | 窗口 %dx%d"
              % (ctx.info.get("GL_RENDERER", "?"), width, height))

        pattern, mark = _make_pattern(width, height)
        app.state = "TITLE"
        app.simulation_active = lambda: False      # 冻结场景：帧与帧完全一致
        app.on_update = lambda dt: None
        app.hud.render = lambda lines, cards: pattern

        shot = {}
        orig_flip = pygame.display.flip

        def flip():
            if not shot:                           # 读回必须在 flip 之前
                shot["fb"] = bytes(ctx.screen.read(components=4))
            orig_flip()

        pygame.display.flip = flip
        try:
            app.run(max_frames=frames)
        finally:
            pygame.display.flip = orig_flip
    finally:
        Engine.capture_mouse = saved_capture
        Engine.release_mouse = saved_release

    fb = shot.get("fb")
    if fb is None or len(fb) != width * height * 4:
        print("[glcheck] FAIL: 帧缓冲读回失败（得到 %s 字节，期望 %d）"
              % (len(fb) if fb is not None else None, width * height * 4))
        return 1

    def px(x, y):
        """按屏幕坐标取像素（y=0 是屏幕顶行）；GL 读回行序自下而上，这里换算。"""
        i = ((height - 1 - y) * width + x) * 4
        return fb[i], fb[i + 1], fb[i + 2]

    wanted = {_FILL} | set(_CORNER_COLORS)
    total = width * height
    missing = [(x, y) for y in range(height) for x in range(width)
               if px(x, y) not in wanted]
    ok = not missing

    print("[glcheck] HUD 铺屏覆盖率 = %.2f%%（要求 100.00%%）"
          % (100.0 * (total - len(missing)) / total))
    if missing:
        mx, my = missing[len(missing) // 2]
        n_left = sum(1 for x, _ in missing if x < width // 2)
        print("[glcheck] FAIL: %d 个像素没被 HUD 覆盖，例如屏幕 (%d, %d) 读回 %s"
              % (len(missing), mx, my, px(mx, my)))
        print("[glcheck]   其中 %d 个落在左半屏。若漏画区是以画面中心为顶点"
              % n_left)
        print("[glcheck]   的楔形/蝴蝶结 → 查 graphics/renderer.py 里 hud_verts "
              "的顶点顺序；")
        print("[glcheck]   TRIANGLE_STRIP 必须是 左下→右下→左上→右上 走 Z 字。")

    for name, color, (sx, sy) in zip(
            _CORNER_NAMES, _CORNER_COLORS,
            ((mark // 2, mark // 2),
             (width - mark // 2, mark // 2),
             (mark // 2, height - mark // 2),
             (width - mark // 2, height - mark // 2))):
        got = px(sx, sy)
        good = got == color
        ok = ok and good
        print("[glcheck] %s 标记 @(%d,%d) 期望 %s 实得 %s %s"
              % (name, sx, sy, color, got, "OK" if good else "MISMATCH"))

    print("[glcheck] RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1
