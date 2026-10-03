# -*- coding: utf-8 -*-
"""波次管理：§5.1 五波表 + §5.2 刷怪点轮换 + §5.3 间隙补给。

刷怪点轮换（GDD §5.2）：
- 6 个刷怪点 SP1~SP6（四角 (±24,±24) + (0,∓26)）。
- 每波开始：按距玩家从远到近排序取最远 N 个（N=波的刷怪点数），
  且与上一波集合不得完全相同（相同时把最远点换成次远未用点）。
- 同点不连刷：单次刷怪选择时跳过上一次所用点。
"""
from .units.stormtrooper import Stormtrooper

# ---- 兵种外观：波次挑战统一用暴风兵 ----
# Stormtrooper 继承 HumanoidBot，只覆写外观（30 部件层叠白甲）与摆动分组，
# 物理体/命中盒/AI 状态机/伤害/死亡全部不变，因此换过去**不改变任何手感**，
# selftest 的 HP/伤害/爆头断言也照常成立。
# 同屏上限 5 → 5 × 30 = 150 + 阴影投影，实测仍在预算内（见 stormtrooper-design.md §4）。
BOT_CLASS = Stormtrooper

# ---- 波次表（GDD §5.1 字面值） ----
# (总数, 单HP, 精英数, 单发伤害, 基准误差角°, 刷出间隔s, 同屏上限, 刷怪点数)
WAVES = (
    (4, 40, 0, 6, 7.0, 2.2, 3, 2),
    (6, 55, 0, 7, 6.0, 2.0, 4, 2),
    (8, 70, 1, 8, 5.0, 1.8, 4, 3),
    (10, 85, 2, 9, 4.2, 1.5, 5, 3),
    (12, 100, 3, 10, 3.5, 1.3, 5, 3),
)
WAVE_COUNT = len(WAVES)

# ---- 刷怪点（GDD §5.2/§6.3 字面值） ----
SPAWN_POINTS = (
    (-24.0, -24.0),     # SP1 西北
    (24.0, -24.0),      # SP2 东北
    (-24.0, 24.0),      # SP3 西南
    (24.0, 24.0),       # SP4 东南
    (0.0, -26.0),       # SP5 北中
    (0.0, 26.0),        # SP6 南中
)

# ---- 导航点（GDD §6.2 字面值）----
# 数据搬到 `bots.py` 了：`BaseBot._begin_reposition` 要用它，但 bots
# 不能 import waves（会成环，见 bots.py 里的说明）。这里转出以保持
# `from .waves import NAV_POINTS` 的既有引用可用。
from .bots import NAV_POINTS  # noqa: F401  (re-export)


class WaveManager:
    def __init__(self, app):
        self.app = app
        self.wave_index = 0         # 当前/即将开始的波次（1~5；0=未开始）
        self.max_wave_reached = 0
        self.total_spawned = 0
        self._spawned = 0           # 本波已刷
        self._spawn_t = 0.0
        self._points = []           # 本波刷怪点集
        self._prev_points = None
        self._last_point = None
        self._active = False        # COMBAT 刷怪中

    # ------------------------------------------------------------------
    def start_wave(self, index):
        """开始第 index 波（1 起）：选定刷怪点集，立即刷第一只。"""
        self.wave_index = index
        self.max_wave_reached = max(self.max_wave_reached, index)
        self._spawned = 0
        self._spawn_t = 0.0
        self._active = True
        self._last_point = None

        (count, hp, elites, dmg, err, interval, cap, n_points) = \
            WAVES[index - 1]
        player_pos = self.app.player.transform.position

        # 距玩家从远到近，取最远 N 个
        ordered = sorted(
            SPAWN_POINTS,
            key=lambda p: -((p[0] - player_pos.x) ** 2
                            + (p[1] - player_pos.z) ** 2))
        points = list(ordered[:n_points])
        # 与上一波集合不得完全相同：把最远点换成次远未用点
        if (self._prev_points is not None
                and set(points) == set(self._prev_points)):
            for cand in ordered[n_points:]:
                if cand not in points:
                    points[0] = cand
                    break
        self._points = points
        self._prev_points = list(points)

    # ------------------------------------------------------------------
    def update(self, dt):
        if not self._active:
            return
        (count, hp, elites, dmg, err, interval, cap, n_points) = \
            WAVES[self.wave_index - 1]
        if self._spawned >= count:
            return
        self._spawn_t -= dt
        if self._spawn_t > 0.0:
            return
        if self.app.combat.alive_bot_count() >= cap:
            return                        # 同屏上限：计时保持 ≤0，可刷即刷
        self._spawn_one()
        self._spawn_t = interval

    def _spawn_one(self):
        (count, hp, elites, dmg, err, interval, cap, n_points) = \
            WAVES[self.wave_index - 1]
        # 同点不连刷：候选点排除上一次所用点
        candidates = [p for p in self._points if p != self._last_point]
        if not candidates:
            candidates = list(self._points)
        point = candidates[self._spawned % len(candidates)]
        self._last_point = point
        # 精英：本波最后 elites 只（压轴登场），HP ×1.6
        elite = self._spawned >= count - elites
        bot_hp = round(hp * 1.6) if elite else hp
        BOT_CLASS(self.app.scene, self.app.physics, self.app,
                  point, bot_hp, elite, dmg, err, self.wave_index)
        self._spawned += 1
        self.total_spawned += 1

    # ------------------------------------------------------------------
    def wave_cleared(self):
        """本波刷怪完毕且场上无存活 bot。"""
        (count, *_rest) = WAVES[self.wave_index - 1]
        return (self._spawned >= count
                and self.app.combat.alive_bot_count() == 0)

    def stop(self):
        self._active = False

    # ------------------------------------------------------------------
    # 自检钩子（--selftest / 冒烟专用）
    # ------------------------------------------------------------------
    def debug_spawn(self, elite=False):
        """玩家前方 ~6m 直接刷一只测试 bot（不计入波次刷怪数）。

        数值为定案测试值：普通兵 40HP/伤 6/误差 7°，精英 160HP/伤 10/
        误差 3.5°（狙击一枪 260 秒杀普通兵；精英减半 130 → 存活 30，
        需求 H22 断言口径）。
        """
        p = self.app.player.transform.position
        pos = (p.x + 1.5, p.z - 6.0)
        if elite:
            hp, dmg, err = 160.0, 10.0, 3.5
        else:
            hp, dmg, err = 40.0, 6.0, 7.0
        return BOT_CLASS(self.app.scene, self.app.physics, self.app,
                         pos, hp, elite, dmg, err,
                         max(1, self.wave_index))
