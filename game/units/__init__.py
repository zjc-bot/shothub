# -*- coding: utf-8 -*-
"""角色兵种：程序化外观变体。

设计依据：
- `docs/design/stormtrooper-design.md`（暴风兵）
- `docs/design/boss-level-design.md`（奶龙 Boss）
构建规则：`docs/design/procedural-humanoid-rules.md`

继承结构（`game/bots.py`）：

    BaseBot          物理体 + FSM AI + 命中盒契约 + 伤害/死亡
    └── HumanoidBot  人形外观（普通兵/精英兵）
        └── Stormtrooper   层叠装甲外观 + 摆动分组
    NaiLongBoss      直接继承 BaseBot（非人形，自带外观与体型）

**只换外观，不改逻辑**：物理体、FSM AI、行走摆动、死亡侧倒、受击提亮、
掉落、爆头判定全部继承。Boss 额外覆写 `hitboxes()`（体型差一个量级，
共用盒子会打不到下半身 / 打不出爆头），但 **combat 的数值一个没动**。
"""
from .stormtrooper import Stormtrooper
from .boss_nailong import NaiLongBoss

__all__ = ["Stormtrooper", "NaiLongBoss"]
