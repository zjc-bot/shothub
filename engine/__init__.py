# -*- coding: utf-8 -*-
"""engine 包：与具体玩法解耦的 3D 引擎骨架。

子模块：
- core       主循环 / 窗口 / 固定步物理调度
- scene      实体与场景管理
- transform  变换（位置 / 欧拉角 / 均匀缩放）
- math_utils 零依赖三维数学库（Vec3 / Mat4）
- camera     第一人称 / 平滑跟随双模式相机架
- physics    AABB / 球体碰撞与响应
- mesh       程序化网格生成 + 渲染组件
- input      键盘 / 相对鼠标输入
- hud        中文 HUD 文本层
"""
