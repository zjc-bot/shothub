# -*- coding: utf-8 -*-
"""全局状态常量与「哪些状态算在战斗里」的**唯一真源**。

**为什么单独一个模块**：状态判断散落在 `app.py` / `weapon.py` /
`combat.py` 三处，而 `weapon.py` 被 `app.py` import —— 想让 weapon 也用
`ST_COMBAT` 常量就得把常量抽到两者都能 import 的地方。把它们连同
**状态集合**一起放这里，`app.py` 再 `from .states import *` 转出，
既不破坏现有 100+ 处 `ST_*` 引用，又只有一处需要改。

**踩过的坑（务必读）**：新增 Boss 关时漏改了硬编码的状态列表，出现
"Boss 战里左键右键都没反应"。原因是三处各自写了字面量：
- `app.py`     `if st in (ST_COMBAT, ST_INTERMISSION)` → 不跑
              `combat.update` / `weapon.update` / ADS FOV
- `weapon.py`  `app.state in ("COMBAT", "INTERMISSION")` → 扳机不响
- `weapon.py`  `_sync_view_model` 同款门 → **手上的枪不可见**
于是玩家看到的是"Boss 战完全不能射击"。以后加状态**只改这个文件**。

集合语义：
- `PLAYABLE_STATES` —— 可操作（相机跟鼠标 / 玩家能移动）的状态
- `GUN_STATES` —— 武器系统每帧工作（扳机、ADS、换弹、bloom）
- `COMBAT_HUD_STATES` —— 画战斗 HUD
- `SUPPLY_STATES` —— 间隙态（三选一 + 补给 + 倒计时横幅）
"""

# ---- 单个状态 ----
ST_TITLE = "TITLE"
ST_EXPLORE = "EXPLORE"
ST_COMBAT = "COMBAT"
ST_INTERMISSION = "INTERMISSION"
ST_WIN = "RESULT_WIN"
ST_LOSE = "RESULT_LOSE"
ST_QUIT = "QUIT"                # 退出确认页（ENG-006）
ST_BOSS = "BOSS"                # Boss 交战中
ST_BOSS_BREAK = "BOSS_BREAK"    # 层间间隙（三选一升级，同 INTERMISSION）

# ---- 状态集合（加状态时只改这里）----
#: 可操作：相机跟随鼠标、玩家响应输入
PLAYABLE_STATES = frozenset(
    (ST_EXPLORE, ST_COMBAT, ST_INTERMISSION, ST_BOSS, ST_BOSS_BREAK))

#: 武器系统工作：扳机开火 / 右键 ADS / 换弹 / bloom 收敛 / 视角模型可见
GUN_STATES = frozenset(
    (ST_COMBAT, ST_INTERMISSION, ST_BOSS, ST_BOSS_BREAK))

#: 战斗系统工作：脱战回血、Boss 掉落计时
COMBAT_STATES = GUN_STATES

#: 画战斗 HUD（血条 / 弹药 / 准星 / 飘字 / 顶部 Boss 条）
COMBAT_HUD_STATES = GUN_STATES

#: 间隙态：三选一升级 + 补给 + 倒计时横幅
SUPPLY_STATES = frozenset((ST_INTERMISSION, ST_BOSS_BREAK))

#: 可结算掉落的战斗状态（`CombatManager.on_bot_death` 用）
DROP_STATES = GUN_STATES