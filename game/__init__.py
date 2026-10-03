# -*- coding: utf-8 -*-
"""game 包：迷你世界 demo 的玩法层。

模块：
- main     命令行入口（正常游玩 / --selftest 自检）
- app      App：引擎钩子装配、快捷键、手电同步、世界重置
- player   玩家控制（WASD + 重力 + 跳跃 + 疾跑）
- actors   追踪炮台 / 炮弹 / 漂浮眼球
- world    世界构建（地形 / 围墙 / 平台 / 箱球道具 / 灯塔）
"""
from .app import App

__all__ = ["App"]
