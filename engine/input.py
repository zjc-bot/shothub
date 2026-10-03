# -*- coding: utf-8 -*-
"""输入管理：键盘状态 + 相对鼠标位移。

每帧调用 begin_frame() 清空「本帧刚按下」与鼠标位移，
之后由 Engine 处理事件累积，游戏逻辑读取：
- is_down(key)     持续按住（移动类）
- was_pressed(key) 本帧刚按下（开关类）
- mouse_delta()    本帧鼠标相对位移（仅鼠标捕获时有效）
- is_mouse_down(btn)    鼠标键持续按住（开火类）
- mouse_was_pressed(btn) 鼠标键本帧刚按下
"""
import pygame


class Input:
    def __init__(self):
        self.keys_down = set()      # 持续按住
        self.keys_pressed = set()   # 本帧刚按下
        self.mouse_down = set()     # 鼠标键持续按住
        self.mouse_pressed = set()  # 鼠标键本帧刚按下
        self._mouse_dx = 0
        self._mouse_dy = 0

    # ---------- 帧循环 ----------
    def begin_frame(self):
        self.keys_pressed.clear()
        self.mouse_pressed.clear()
        self._mouse_dx = 0
        self._mouse_dy = 0

    def process_event(self, event):
        if event.type == pygame.KEYDOWN:
            self.keys_down.add(event.key)
            self.keys_pressed.add(event.key)
        elif event.type == pygame.KEYUP:
            self.keys_down.discard(event.key)

    def process_mouse_motion(self, rel):
        self._mouse_dx += rel[0]
        self._mouse_dy += rel[1]

    def process_mouse_button(self, button, down):
        """鼠标键事件（Engine 仅在鼠标已捕获时转发，未捕获的左键=捕获请求）。"""
        if down:
            self.mouse_down.add(button)
            self.mouse_pressed.add(button)
        else:
            self.mouse_down.discard(button)

    # ---------- 查询 ----------
    def is_down(self, key):
        return key in self.keys_down

    def was_pressed(self, *keys):
        for k in keys:
            if k in self.keys_pressed:
                return True
        return False

    def mouse_delta(self):
        return self._mouse_dx, self._mouse_dy

    def is_mouse_down(self, button):
        return button in self.mouse_down

    def mouse_was_pressed(self, button):
        return button in self.mouse_pressed

    # ---------- 自检辅助（headless 模式模拟按键） ----------
    def simulate(self, key):
        self.keys_down.add(key)

    def tap(self, key):
        self.keys_pressed.add(key)
        self.keys_down.add(key)

    def release(self, key):
        self.keys_down.discard(key)

    def simulate_mouse(self, button):
        """headless 模拟鼠标键按住（每帧调用=持续按住，等价于开火扳机）。"""
        self.mouse_down.add(button)
        self.mouse_pressed.add(button)

    def release_mouse(self, button):
        self.mouse_down.discard(button)
