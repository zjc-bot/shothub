# -*- coding: utf-8 -*-
"""Boss 关管理器：两层 Boss 关（奶龙）。

设计文档：`docs/design/boss-level-design.md`

**与 `WaveManager` 的关系**：玩法流程**逐条对齐**，但刷怪逻辑不同。

| 环节 | 波次挑战（`WaveManager`） | Boss 关（本模块） |
|---|---|---|
| 进入 | TITLE [1] | TITLE [3] |
| 开场 | 3s 倒计时 | 3s 倒计时（同一 `PREWAVE_COUNTDOWN`） |
| 关卡数 | 5 波递增刷小兵 | 2 层，每层 1~4 只 Boss |
| 间隙 | 三选一升级 + 补给 | **同一套**（`_apply_upgrade` / `weapon.supply`） |
| 通关 | `wave_index >= 5` → WIN | 第 2 层**全部** Boss 死亡 → WIN |
| 战败 | 玩家 HP≤0 → LOSE | **同一套**（`on_player_dead`） |
| Esc | 交战不拦，退出页确认 | **同一套**（`App.on_escape`，本模块不参与） |
| 回标题 | 结算页 [R]/[T] | **同一套**（`back_to_title`） |

所以本模块**不含任何状态机**：它只回答两个问题 ——
「现在该刷哪几只 Boss？」「这一层打完没有？」。
状态流转、`_enter_state`、升级选择、结算页判定全在 `app.py`，
与波次关共用同一段代码路径（见 `App.on_update` 的 `BOSS_*` 分支）。

**每层的 Boss 数量**（用户指定）：

- 第 1 层「奶龙·单枪」：**4 只**，从战场**四个角**同时诞生
- 第 2 层「奶龙·双枪刺刀」：**2 只**（带极速刺刀冲击）

难度来自**空间压迫**（同时要盯 4 个方向）而不是血量线性叠加，
所以单只 HP 沿用用户给的字面值（1000 / 300），而**单发伤害按只数下调**，
让"总 incoming DPS"保持在可玩区间（推导见下方 `damage` 注释与设计文档 §8）。
"""
import random

from .units.boss_nailong import NaiLongBoss

# ----------------------------------------------------------------------
# 两层 Boss 定义
# ----------------------------------------------------------------------
# key: (显示名, 数量, HP, 双枪?, 移动速度, 交战距离, 瞄准预备, 连发数,
#       射速间隔, 弹匣间隔, 单发伤害, 误差角, 冲刺速度, 冲刺伤害)
# 数值来源与平衡理由见设计文档；改这里之前先读文档 §2/§3/§8。
BOSS_STAGES = (
    {
        "key": "single",
        "name": "奶龙·单枪",
        "count": 4,             # 用户指定：第 1 层四只，四个角诞生
        "hp": 1000.0,           # 用户指定：血量较厚（**每只** 1000）
        "dual_gun": False,
        # 单枪：慢而准。射速 1/0.55s ≈ 1.8 发/s，弹匣 4 发后停 1.8s
        # -> 实际约 0.93 发/s，给玩家躲避窗口。
        "speed": 2.2,
        "engage_min": 12.0,
        "engage_max": 22.0,
        "aim_time": 1.20,
        "burst": 4,
        "burst_interval": 0.55,
        "burst_gap": (1.8, 2.4),
        # 单发伤害按只数下调（1 只时是 14）。推导：
        #   每 cycle = 4×0.55 + avg(2.1) = 4.3s 发 4 发 -> 0.93 发/s
        #   1 只  : 14 × 0.93 = 13.0 dps
        #   4 只  : 8  × 0.93 × 4 = 29.8 dps   <- 目标区间 25~35
        # 100 HP 的玩家在四向火力下有约 3.4s 反应时间，且脱战 4s 后
        # 10 HP/s 回血。若沿用 14：67 dps，玩家 1.5s 就没了，不可玩。
        "damage": 8.0,
        "err": 3.0,            # 精度高于普通兵（体型大 = 威胁大）
        "rush_speed": 0.0,     # 无刺刀
        "rush_damage": 0.0,
    },
    {
        "key": "dual",
        "name": "奶龙·双枪刺刀",
        "count": 2,             # 用户指定：第 2 层两只
        "hp": 300.0,            # 用户指定：血量少一点（**每只** 300）
        "dual_gun": True,       # 双枪交替，火力密度翻倍
        "speed": 3.6,           # 更快
        "engage_min": 8.0,
        "engage_max": 16.0,
        "aim_time": 0.55,       # 预备更短
        "burst": 3,
        "burst_interval": 0.16,   # 双枪连射很快
        "burst_gap": (0.9, 1.3),
        # 每 cycle = 3×0.16 + avg(1.1) = 1.58s 发 3 发 -> 1.90 发/s
        #   2 只  : 7 × 1.90 × 2 = 26.6 dps（另有刺刀 18 × 2）
        "damage": 7.0,
        "err": 4.5,
        "rush_speed": 17.0,     # 极速刺刀冲击
        "rush_damage": 18.0,
    },
)

STAGE_COUNT = len(BOSS_STAGES)

# 刷怪点：**战场四个角**（用户指定）。
# 战场是 60×60（x/z ∈ [-30, 30]），取 ±24 留 6m 边距给 Boss 体型
# （宽 1.56m、高 2.86m），避免贴墙卡住或半个身子在墙里。
# 四点对称保证玩家到哪一侧都被至少两只夹击。
BOSS_SPAWN_POINTS = (
    (-24.0, -24.0),     # 西南
    (24.0, -24.0),      # 东南
    (-24.0, 24.0),      # 西北
    (24.0, 24.0),       # 东北
)


class BossManager:
    """两层 Boss 关的刷怪与进度。**不含状态机**（见模块 docstring）。

    **多 Boss 设计**：一层可以有 1~4 只（`stage()["count"]`）。
    本类只持**本层**的 Boss 列表 `mobs`；换层时整个清空。
    """

    def __init__(self, app):
        self.app = app
        self.stage_index = 0          # 当前层（0 起）
        self.mobs = []                # 本层已刷出的 Boss 列表
        self._spawned = False         # 本层是否已刷
        self._active = False

    # ------------------------------------------------------------------
    # 只读查询
    # ------------------------------------------------------------------
    def stage(self):
        """当前层配置（BossManager 与 app 都读它，避免两处重复取值）。"""
        return BOSS_STAGES[min(self.stage_index, STAGE_COUNT - 1)]

    def stage_number(self):
        """1 起；用于 HUD「第 N / 2 层」。"""
        return min(self.stage_index + 1, STAGE_COUNT)

    def name(self):
        """本层的 Boss 名（不带数量）。"""
        return self.stage()["name"]

    def count(self):
        """本层配置的数量（刷之前也知道，用于 HUD 预告）。"""
        return int(self.stage()["count"])

    def alive(self):
        """本层还活着的 Boss（死亡的不算）。"""
        return [m for m in self.mobs if not m.dead]

    def total_hp(self):
        """本层存活 Boss 的总血量（给聚合血条用）。"""
        return sum(max(0.0, m.hp) for m in self.alive())

    def max_total_hp(self):
        """本层全部 Boss 的满血总量（聚合血条的分母）。"""
        return sum(m.max_hp for m in self.mobs)

    def boss(self):
        """兼容旧调用：返回第一只活着的 Boss（或 None）。

        新代码请直接用 `alive()` —— 多 Boss 场景下"那一只"没有意义。
        """
        a = self.alive()
        return a[0] if a else None

    # ------------------------------------------------------------------
    # 刷怪
    # ------------------------------------------------------------------
    def start_stage(self, index):
        """开始第 index 层（0 起）：重置进度、选定本层的刷怪点。

        **不立即刷 Boss** —— 由 `spawn_stage()` 在倒计时结束时调用，
        这样入场动画（SPAWN 态闪烁 0.8s）才有意义。
        """
        self.stage_index = max(0, min(index, STAGE_COUNT - 1))
        self.mobs = []
        self._spawned = False
        self._active = True
        self._points = self._pick_points()

    def _pick_points(self):
        """按"离玩家由远到近"排序的刷怪点序列，取前 `count` 个。

        为什么按距离排序：玩家可能已经跑到某个角附近，先刷最远的，
        保证**每只 Boss 都不会贴脸出现**；同时四点对称，
        玩家站在任意位置都会被两只以上夹击。
        """
        p = self.app.player.transform.position
        ordered = sorted(BOSS_SPAWN_POINTS,
                         key=lambda q: -((q[0] - p.x) ** 2
                                         + (q[1] - p.z) ** 2))
        n = int(self.stage()["count"])
        if n >= len(ordered):
            # 要的比点多：循环取，绝不返回比 count 少的点
            return [ordered[i % len(ordered)] for i in range(n)]
        return ordered[:n]

    def spawn_stage(self):
        """按当前层配置刷出**全部** Boss。同一层只刷一次。"""
        if self._spawned:
            return self.mobs
        cfg = self.stage()
        for pt in self._points:
            self.mobs.append(self._make_boss(cfg, pt))
        self._spawned = True
        return self.mobs

    def _make_boss(self, cfg, point):
        """建一只 Boss 并按层配置覆盖行为参数。

        单枪 / 双枪、冲刺参数**都在构造时给**：NaiLongBoss 的 `__init__`
        会先按 dual_gun 组好 SPEC 再建部件，事后换枪就得重建部件树。
        """
        b = NaiLongBoss(self.app.scene, self.app.physics, self.app,
                        point, cfg["hp"], True,       # elite=True
                        cfg["damage"], cfg["err"], self.stage_number(),
                        dual_gun=cfg["dual_gun"],
                        rush_speed=cfg["rush_speed"],
                        rush_damage=cfg["rush_damage"])
        # 行为参数逐条按层配置覆盖（BaseBot 已把它们做成类属性，
        # 这里按实例改，故同层的 Boss 之间也互不干扰）
        b.MOVE_SPEED = cfg["speed"]
        b.ENGAGE_MIN_ = cfg["engage_min"]
        b.ENGAGE_MAX_ = cfg["engage_max"]
        b.AIM_TIME_ = cfg["aim_time"]
        b.BURST_SHOTS_ = cfg["burst"]
        b.BURST_INTERVAL_ = cfg["burst_interval"]
        b.BURST_GAP = cfg["burst_gap"]
        return b

    # ------------------------------------------------------------------
    # 进度判定
    # ------------------------------------------------------------------
    def stage_cleared(self):
        """本层打完没有：**刷过**且**一只活的都不剩**。

        必须判 `len(self.mobs) > 0` 而不是只看 `_spawned`：
        否则"还没刷"（mobs 为空）会被误判成"已清空"。
        """
        return self._spawned and len(self.mobs) > 0 \
            and not any(not m.dead for m in self.mobs)

    def all_cleared(self):
        """两层都打完 -> 通关。"""
        return self.stage_cleared() and self.stage_index >= STAGE_COUNT - 1

    def has_next_stage(self):
        return self.stage_index < STAGE_COUNT - 1

    def stop(self):
        self._active = False

    # ------------------------------------------------------------------
    def update(self, dt):
        """本模块不需要每帧驱动 —— Boss 自己由 Scene.update 驱动。

        保留空实现是为了与 `WaveManager.update(dt)` 接口一致，
        让 `App.on_update` 的两套流程写法对称（可读性）。
        """
        return

    # ------------------------------------------------------------------
    # 自检 / 调试
    # ------------------------------------------------------------------
    def debug_spawn(self, stage=0):
        """直接刷某层的全部 Boss（不经过倒计时）。自检与手动测试用。"""
        self.start_stage(stage)
        return self.spawn_stage()