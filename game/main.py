# -*- coding: utf-8 -*-
"""命令行入口：解析参数并启动 App。

用法：
    python main.py                    # 直接游玩（默认 1280x720）
    python main.py --width 1600 --height 900
    python main.py --selftest         # 无窗口自检（CI / 无显卡环境）
    python main.py --selftest --frames 600
    python main.py --glcheck          # 真窗口 GL 合成检查（HUD 全屏 quad 铺屏）

说明：本文件同时兼容三种启动方式：
    python main.py / python -m game.main / python game/main.py
"""
import os
import sys

# 允许 python game/main.py 直接运行：把项目根目录放回 sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _arg_int(argv, name, default):
    if name in argv:
        try:
            return int(argv[argv.index(name) + 1])
        except (IndexError, ValueError):
            pass
    return default


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    width = _arg_int(argv, "--width", 1280)
    height = _arg_int(argv, "--height", 720)
    frames = _arg_int(argv, "--frames", 3000)
    headless = "--selftest" in argv

    if "--glcheck" in argv:
        # GL 合成层检查：headless 自检测不到贴屏步骤，单独走一条路径
        from engine.qa_gl import run_glcheck
        return run_glcheck(width=width, height=height)

    from game.app import App
    app = App(headless=headless, width=width, height=height)
    app.selftest = headless
    if headless:
        app.run(max_frames=frames)   # 结束时 on_shutdown 输出自检报告
        return 0
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
