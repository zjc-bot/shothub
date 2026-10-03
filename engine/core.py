# -*- coding: utf-8 -*-
"""引擎核心：窗口 / 主循环 / 固定步物理调度 / 渲染调度。

Engine 是与具体游戏解耦的骨架，游戏逻辑通过继承挂入：
    on_start()        场景构建（一次性）
    on_update(dt)     每帧游戏逻辑（快捷键、灯光同步、相机装配）
    get_hud_lines()   HUD 文本

主循环每帧顺序（顺序即正确性）：
    1. input.begin_frame()      清空本帧按键/鼠标位移
    2. 处理窗口/键盘/鼠标事件
    3. on_update(dt)            游戏逻辑（含相机装配更新）
    4. 固定步长跑物理（累加器模式，防帧率抖动引发穿透）
    5. scene.update(dt)         实体逻辑（炮台追踪、炮弹寿命…）
    6. flush_removed()          统一清理已删除实体
    7. render + flip            阴影→主渲染→HUD

headless 模式：不创建窗口/GL，用于 --selftest 自动化自检。
"""
import time

import pygame

from .scene import Scene
from .physics import PhysicsWorld
from .input import Input
from .camera import CameraRig

STEP = 1.0 / 120.0          # 物理固定步长（秒）
MAX_STEPS_PER_FRAME = 6     # 单帧最大物理步数（防死亡螺旋）


class Engine:
    def __init__(self, title="MiniWorld", width=1280, height=720,
                 headless=False):
        self.title = title
        self.width = width
        self.height = height
        self.headless = headless

        self.scene = Scene()
        self.physics = PhysicsWorld()
        self.input = Input()
        self.camera_rig = CameraRig()
        self.running = True
        self.frame = 0
        self.time = 0.0
        self.fps_display = 0.0
        self.mouse_captured = False
        self.scene.entity_count = 0

        self._accumulator = 0.0
        self._fps_ema = 60.0
        self._last_ticks = time.perf_counter()

        # 渲染相关（headless 下为 None）
        self.renderer = None
        self.hud = None
        self.sun = None          # 由游戏侧创建：graphics.light.DirectionalLight
        self.spot_lights = []    # 由游戏侧创建：SpotLight 列表
        self.clear_color = (0.66, 0.80, 0.90)
        self.fog = None          # graphics.renderer.FogParams

        if headless:
            # 无显示环境（CI/容器）必须先选 dummy 视频驱动再 init，
            # 否则 pygame.init() 内部的 display.init 会先按默认驱动失败。
            import os
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        pygame.init()
        if headless:
            # headless：无窗口无 GL
            pygame.display.set_mode((64, 64))
            return

        # ---- 真窗口 + moderngl 上下文 ----
        pygame.display.set_caption(title)
        pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 3)
        pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 3)
        pygame.display.gl_set_attribute(
            pygame.GL_CONTEXT_PROFILE_MASK, pygame.GL_CONTEXT_PROFILE_CORE)
        pygame.display.gl_set_attribute(pygame.GL_DEPTH_SIZE, 24)
        pygame.display.gl_set_attribute(pygame.GL_DOUBLEBUFFER, 1)
        self._screen = pygame.display.set_mode(
            (width, height),
            pygame.DOUBLEBUF | pygame.OPENGL | pygame.RESIZABLE)

        import moderngl
        self.ctx = moderngl.create_context()
        from graphics.renderer import Renderer, FogParams
        from .hud import HUD
        self.fog = FogParams()
        self.renderer = Renderer(self.ctx, width, height)
        self.hud = HUD(width, height)
        self.clear_color = (0.66, 0.80, 0.90)   # 与雾色一致

    # ------------------------------------------------------------------
    # 鼠标捕获
    # ------------------------------------------------------------------
    def capture_mouse(self):
        if self.headless:
            return
        pygame.event.set_grab(True)
        pygame.mouse.set_relative_mode(True)   # pygame-ce API（原版 pygame 叫 set_relative_mouse_mode）
        pygame.mouse.set_visible(False)
        self.mouse_captured = True

    def release_mouse(self):
        if self.headless:
            return
        pygame.event.set_grab(False)
        pygame.mouse.set_relative_mode(False)
        pygame.mouse.set_visible(True)
        self.mouse_captured = False

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _process_events(self):
        import pygame as pg
        for event in pg.event.get():
            if event.type == pg.QUIT:
                self.running = False
                return
            if event.type == pg.KEYDOWN:
                if event.key == pg.K_ESCAPE:
                    if not self.on_escape():
                        if self.mouse_captured:
                            self.release_mouse()
                        else:
                            self.running = False
                    continue
                self.input.process_event(event)
            elif event.type == pg.KEYUP:
                self.input.process_event(event)
            elif event.type == pg.MOUSEMOTION and self.mouse_captured:
                self.input.process_mouse_motion(event.rel)
            elif event.type == pg.MOUSEBUTTONDOWN:
                if self.mouse_captured:
                    self.input.process_mouse_button(event.button, True)
                elif event.button == 1:    # 左键点击窗口 → 捕获鼠标
                    self.capture_mouse()
            elif event.type == pg.MOUSEBUTTONUP and self.mouse_captured:
                self.input.process_mouse_button(event.button, False)
            elif event.type == pg.VIDEORESIZE:
                self.handle_resize(event.w, event.h)
            elif event.type == pg.WINDOWFOCUSLOST:
                self.release_mouse()

    # ------------------------------------------------------------------
    # 窗口缩放
    # ------------------------------------------------------------------
    MIN_WIDTH = 480        # 再窄卡片就会贴边（GDD §7 HUD 最小可用宽度）
    MIN_HEIGHT = 320

    def handle_resize(self, w, h):
        """处理窗口缩放：重建与尺寸绑定的资源并更新宽高比。

        之前完全没有 VIDEORESIZE 分支——窗口一旦被拖拽/贴靠/最大化，
        HUD 画布、GL viewport 与相机宽高比都会停留在旧尺寸，导致
        HUD 被拉伸错位（正是"缩放导致布局错位"的成因）。
        """
        w = max(self.MIN_WIDTH, int(w))
        h = max(self.MIN_HEIGHT, int(h))
        if (w, h) == (self.width, self.height):
            return
        self.width, self.height = w, h
        if self.headless:
            return
        try:
            self._screen = pygame.display.set_mode(
                (w, h), pygame.DOUBLEBUF | pygame.OPENGL | pygame.RESIZABLE)
        except pygame.error:
            return                       # 驱动拒绝该尺寸：保留上一档
        if self.hud is not None:
            self.hud.resize(w, h)
        if self.renderer is not None:
            self.renderer.resize(w, h)
        self.on_resize(w, h)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self, max_frames=None):
        self.on_start()
        if not self.headless:
            self.capture_mouse()
        while self.running:
            if max_frames is not None and self.frame >= max_frames:
                break
            # --- 计时 ---
            now = time.perf_counter()
            dt = now - self._last_ticks
            self._last_ticks = now
            if self.headless:
                dt = STEP * 2.0            # 自检模式固定帧长
            dt = min(dt, 0.05)             # 防大步长穿墙
            if dt > 0:
                inst = 1.0 / dt
                self._fps_ema += (inst - self._fps_ema) * 0.05
                self.fps_display = int(self._fps_ema)

            self.input.begin_frame()
            self._process_events()
            if not self.running:
                break

            self.time += dt
            self.on_update(dt)

            if self.simulation_active():
                # --- 固定步物理 ---
                self._accumulator += dt
                steps = 0
                while (self._accumulator >= STEP
                       and steps < MAX_STEPS_PER_FRAME):
                    self.physics.step(STEP)
                    self._accumulator -= STEP
                    steps += 1
                if steps == MAX_STEPS_PER_FRAME:
                    self._accumulator = 0.0    # 丢帧保护

                # --- 实体逻辑与清理 ---
                self.scene.entity_count = len(self.scene.entities)
                self.scene.update(dt)
                self.scene.flush_removed(self.physics)
            else:
                self.scene.entity_count = len(self.scene.entities)

            # --- 渲染 ---
            self._render()
            self.frame += 1
        self.release_mouse()
        self.on_shutdown()

    def _render(self):
        if self.headless or self.renderer is None:
            return
        hud_surface = None
        if self.hud is not None:
            hud_surface = self.hud.render(self.get_hud_lines(),
                                          self.get_hud_cards())
        self.renderer.render(
            self.scene, self.camera_rig, self.sun, self.spot_lights,
            hud_surface, self.clear_color, self.fog)
        pygame.display.flip()

    def quit(self):
        self.running = False

    # ------------------------------------------------------------------
    # 游戏侧钩子（子类覆写）
    # ------------------------------------------------------------------
    def on_start(self):
        pass

    def on_update(self, dt):
        pass

    def on_escape(self):
        """Esc 拦截钩子：返回 True 表示已消费 Esc，不执行默认行为。

        默认行为：鼠标已捕获 → 释放；已释放 → 直接退出主循环。
        Esc 不会进入 Input（事件分支里 continue 掉了），所以游戏层想
        响应 Esc 只能靠这个钩子。子类典型用途：把"按一下就杀进程"
        改成"先进退出确认页"，并用 request_quit() 真正退出。
        """
        return False

    def request_quit(self):
        """请求退出主循环（供游戏层确认页调用）。"""
        self.running = False

    def on_resize(self, width, height):
        """窗口尺寸变化钩子（子类覆写；引擎已先重建 HUD/GL 资源）。"""
        pass

    def get_hud_lines(self):
        return []

    def get_hud_cards(self):
        """任意位置卡片指令列表（engine.hud.HUD.render 第二参数），默认无。"""
        return None

    def simulation_active(self):
        """世界模拟是否推进（物理 + 实体 update）；结算页等场景可冻结。"""
        return True

    def on_shutdown(self):
        pass
