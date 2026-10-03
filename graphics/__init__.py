# -*- coding: utf-8 -*-
"""graphics 包：基于 moderngl（OpenGL 3.3 Core）的渲染层。

子模块：
- renderer  阴影 pass + 主光照 pass + HUD 覆盖 pass
- shaders   全部 GLSL 着色器源码
- light     平行光 / 聚光灯（光锥三参数可实时调节）
- texture   程序化纹理（零外部资源）
"""
