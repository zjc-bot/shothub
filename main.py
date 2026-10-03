#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""迷你世界 MiniWorld —— 一键启动入口。

    python main.py            # 游玩
    python main.py --selftest # 无窗口自检
    python main.py --glcheck  # 真窗口 GL 合成检查（HUD 贴屏）

项目结构 / 按键说明见 README.md。
"""
from game.main import main

if __name__ == "__main__":
    raise SystemExit(main())
