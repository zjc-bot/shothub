# -*- coding: utf-8 -*-
"""迷你世界主程序：引擎组装 + FPS 波次生存模式状态机。

状态机（GDD fps-wave-mode §1.2）：
    TITLE ──[1]──► INTERMISSION(3s 倒计时) ──► COMBAT(第1波)
      │[2]            ▲   │清波（未到第5波）
      ▼               └───┘
    EXPLORE        COMBAT ──清完第5波──► RESULT_WIN
    （现有 demo）        └──玩家 HP≤0──► RESULT_LOSE
    两结算页 [R] 回 TITLE（reset_world 全量重建）/ [Esc] 退出

快捷键（EXPLORE 观光模式照旧）：
    Tab 切换第一/跟随视角   F 手电开关   [ ] 光锥角   - = 强度   , . 衰减
    H 帮助   R 重置世界   Esc 释放鼠标 / 退出
挑战模式：左键开火 / R 换弹 / WASD 移动 / Shift 疾跑 / 空格跳跃。
"""
import math

import pygame

from engine.core import Engine
from engine.math_utils import Vec3, lerp, project_to_screen
from engine.hud import HUD
from graphics.light import DirectionalLight, SpotLight

from . import world
from .waves import WAVES
from .player import POSTURE_STAND, POSTURE_CROUCH, POSTURE_PRONE
from .effects import ImpactFX

# 挑战模式状态 + 「哪些状态算在战斗里」的唯一真源。
# 常量与状态集合都住在 states.py；这里 re-export 是为了让既有 100+ 处
# `ST_*` 引用一个都不用改。**新增状态只改 game/states.py** ——
# 曾因漏改 weapon.py / app.py 里的硬编码状态列表，导致 Boss 战里
# 左键右键都打不出子弹（见 states.py 顶部说明）。
from .states import (
    ST_TITLE, ST_EXPLORE, ST_COMBAT, ST_INTERMISSION,
    ST_WIN, ST_LOSE, ST_QUIT, ST_BOSS, ST_BOSS_BREAK,
    PLAYABLE_STATES, GUN_STATES, COMBAT_HUD_STATES, SUPPLY_STATES,
)

TITLE_ORBIT_SPEED = 0.05        # TITLE 相机缓速环视（rad/s，GDD §1.3）
PREWAVE_COUNTDOWN = 3.0         # [1] 后 3s 倒计时刷第 1 波
INTERMISSION_TIME = 8.0         # 波间补给 8s（GDD §5.3）

# Boss 关层数（与 game/boss_rush.py 的 STAGE_COUNT 一致；HUD 文案要用）
BOSS_TOTAL_STAGES = 2

# Boss 顶部血条布局：第 i 条画在 BOSS_BAR_Y + i * BOSS_BAR_STEP。
# 第 1 层有 4 只 Boss，4 条 12px 高的条 + 4px 间隙 = 60px，仍在顶部安全区内。
BOSS_BAR_Y = 20
BOSS_BAR_STEP = 16


class App(Engine):
    def __init__(self, headless=False, width=1280, height=720):
        super().__init__(title="迷你世界 MiniWorld",
                         width=width, height=height, headless=headless)
        self._aspect = width / max(1, height)

        # 玩法对象（on_start 构建世界时回填）
        self.player = None
        self.turret = None
        self.eye = None
        self.lighthouse = None
        self.flashlight = None      # HUD 直接读取此属性

        # 命中特效（ENG-007：火花 + 弹痕）。全模式共用，explore 下不产生。
        self.fx = ImpactFX(self)

        # 挑战模式对象（challenge 世界构建时回填，explore 下为 None）
        self.combat = None
        self.weapon = None          # 当前武器（需求 E14）
        self.weapons = []           # [Rifle(MW-1), Sniper(SR-1)]
        self.waves = None
        self.bosses = None         # BossManager（仅 boss 模式构建世界时回填）
        self.nav_points = []

        # Boss 关进度（与波次关的 _next_wave / _lose_wave 并列）
        self._boss_stage = 0       # 当前层（0 起）
        self._lose_boss_stage = 0  # 战败时记在第几层（结算页文案用）

        # 波次三选一升级（需求 F15/F17）
        self._upgrade_pending = False      # INTERMISSION 且未选择
        self.upgrades_taken = []           # 如 ["生命", "火力(自动)", …]

        # 状态机
        self.state = ST_TITLE
        self.mode = "challenge"     # 当前世界构建模式（challenge/explore）
        self._intermission_t = 0.0
        self._next_wave = 1
        self._lose_wave = 0
        self._quit_return = ST_TITLE     # 退出确认页按 Esc 要退回的状态
        self._combat_time = 0.0     # 挑战模式累计用时（结算页数据源）

        # Sprint 2 HUD 动画状态（仅在有窗口的 HUD 组装中推进，headless 不消耗）
        self._banner_t = 0.0        # COMBAT 波次横幅剩余时间（2.5s 渐隐）
        self._hurt_t = 0.0          # 受击 vignette 剩余时间（250ms 线性渐隐）
        self._prev_hp = 100.0       # 受击沿检测（hp 下降沿）
        self._hud_t = 0.0           # HUD 动画计时基准（上次组装的 app.time）

        # --selftest 自检状态
        self.selftest = False
        self._shots_before_reset = 0
        self._st = None

    # ------------------------------------------------------------------
    # 构建钩子
    # ------------------------------------------------------------------
    def on_start(self):
        # 太阳（平行光，无阴影；阴影由聚光灯负责）
        self.sun = DirectionalLight(Vec3(-0.42, -1.0, -0.28),
                                    color=(1.0, 0.98, 0.94), intensity=1.05)

        # 手电筒：cast_shadow → 箱子/墙会在光锥里投出实时阴影
        self.flashlight = SpotLight(
            position=Vec3(0.0, 1.7, 8.0), direction=Vec3(0.0, 0.0, -1.0),
            color=(1.0, 0.94, 0.82),
            inner_angle_deg=12.0, outer_angle_deg=22.0,
            intensity=3.2, attenuation=0.035,
            cast_shadow=True, shadow_res=1024, shadow_far=60.0)
        self.spot_lights = [self.flashlight]

        if self.selftest:
            self._st = _new_selftest_state()
            import random
            random.seed(20250913)      # 自检确定性（扩散/AI 随机源）

        self._build_world()

    def _build_world(self):
        world.build_world(self, self.mode)

    def reset_world(self):
        """R 键：完全重建世界（保留手电与相机状态），按当前 mode 重建。"""
        if self.selftest and self.turret is not None:
            self._shots_before_reset += self.turret.shots
        # 释放旧聚光灯的 GL 阴影资源（手电保留但 FBO 会自动重建）
        if self.renderer is not None:
            for light in self.spot_lights:
                self.renderer.release_light_resource(light)
        self.spot_lights = [self.flashlight]
        self.physics.bodies.clear()
        self.scene.clear()
        self.fx.clear()               # 弹痕/火花池引用随世界一起清掉
        self._build_world()

    # ------------------------------------------------------------------
    # 状态机转入
    # ------------------------------------------------------------------
    def _enter_state(self, s):
        self.state = s
        if s == ST_COMBAT:
            self._banner_t = 2.5        # 波次横幅：停留 2.0s + 渐隐 0.5s
        if s == ST_BOSS:
            self._banner_t = 2.5        # Boss 入场横幅，节奏与波次横幅一致
        if s in PLAYABLE_STATES:
            self.capture_mouse()
        elif s in (ST_TITLE, ST_WIN, ST_LOSE, ST_QUIT):
            self.release_mouse()

    # ------------------------------------------------------------------
    # 退出流程（ENG-006）：Esc 一律先进确认页，不再直接杀进程
    # ------------------------------------------------------------------
    def on_escape(self):
        """拦截 Esc（Engine 钩子）。返回 True = 已消费。

        - 已在退出页：退回进入前的状态（取消退出）
        - 标题页 / 两个结算页：弹出退出确认页
        - 交战 / 间隙 / 观光：交回引擎默认行为（鼠标已捕获 → 仅释放）
        """
        if self.state == ST_QUIT:
            self._enter_state(self._quit_return)
            return True
        if self.state in (ST_TITLE, ST_WIN, ST_LOSE):
            self._quit_return = self.state
            self._enter_state(ST_QUIT)
            return True
        return False

    def back_to_title(self, from_state):
        """从结算页/观光模式回标题页：世界按标题需要重建，避免残留。"""
        self.mode = "challenge"
        self.reset_world()
        self._enter_state(ST_TITLE)

    def start_challenge(self):
        """TITLE [1]：进入 3s 倒计时，随后刷第 1 波。"""
        if self.mode != "challenge":
            self.mode = "challenge"
            self.reset_world()
        self._combat_time = 0.0
        self._intermission_t = PREWAVE_COUNTDOWN
        self._next_wave = 1
        self._upgrade_pending = False       # 开局无升级（需求 F15）
        self.upgrades_taken = []
        self._enter_state(ST_INTERMISSION)

    def start_explore(self):
        """TITLE [2]：观光模式（现有 demo 原样，世界重建为 explore）。"""
        if self.mode != "explore":
            self.mode = "explore"
            self.reset_world()
        self._enter_state(ST_EXPLORE)

    def start_boss(self):
        """TITLE [3]：奶龙 Boss 关两层。

        与 `start_challenge` 逐条对齐 —— 同一个 3s 倒计时、同一套升级三选一、
        同一个结算页与退出流程，区别只是敌人由 `WaveManager` 换成 `BossManager`。
        """
        if self.mode != "boss":
            self.mode = "boss"
            self.reset_world()
        self._combat_time = 0.0
        self._intermission_t = PREWAVE_COUNTDOWN
        self._boss_stage = 0
        self._upgrade_pending = False       # 开局无升级（同波次关需求 F15）
        self.upgrades_taken = []
        if self.bosses is not None:
            self.bosses.start_stage(0)
            self.combat.clear_boss_drop_state()   # 换关卡时清掉上一局的节流状态
        self._enter_state(ST_BOSS_BREAK)   # 复用间隙态：先跑 3s 倒计时

    def on_player_dead(self):
        """玩家 HP≤0 → RESULT_LOSE（CombatManager 回调）。"""
        if self.state in (ST_WIN, ST_LOSE):
            return
        if self.state in (ST_BOSS, ST_BOSS_BREAK):
            self._lose_boss_stage = self.bosses.stage_number() \
                if self.bosses else 0
            self._enter_state(ST_LOSE)
            return
        self._lose_wave = self.waves.wave_index if self.waves else 0
        self._enter_state(ST_LOSE)

    def simulation_active(self):
        """结算页/退出页冻结世界模拟（GDD §1.3：实体不 update，渲染继续）。

        退出页也冻结：读确认框时不该被 bot 打死，也不该继续跑物理。
        """
        return self.state not in (ST_WIN, ST_LOSE, ST_QUIT)
    # ------------------------------------------------------------------
    # 每帧逻辑
    # ------------------------------------------------------------------
    def on_resize(self, width, height):
        """窗口缩放：更新相机宽高比，否则 FOV 会按旧比例拉伸。

        投影矩阵靠 _aspect；HUD 画布与 GL 资源由引擎基类重建。
        HUD 各元素用 W/2 / H/2 相对锚点，故无需逐个调整。
        """
        self._aspect = width / max(1, height)

    def on_update(self, dt):
        if self.selftest:
            self._selftest_script()      # 先注入，再处理，保证同帧生效

        st = self.state
        inp = self.input

        # ---- H 键：按键总览开关（所有状态，含 TITLE / 结算页）----
        # 放在状态分发之前：_handle_shortcuts 只在 EXPLORE/COMBAT/INTERMISSION
        # 被调用，若把 H 留在那里，进入页与结算页按 H 就没有反应。
        if self.hud is not None and inp.was_pressed(pygame.K_h):
            self.hud.show_help = not self.hud.show_help

        # ---- 姿态：Ctrl 按住下蹲 / C 切换下蹲 / Z 切换趴下（ENG-007 模块一）----
        # Ctrl 走"按住"（FPS 惯例）；C/Z 走"切换"，两者可并存互不冲突。
        if st in PLAYABLE_STATES and self.player is not None:
            p = self.player
            ctrl = inp.is_down(pygame.K_LCTRL) or \
                inp.is_down(pygame.K_RCTRL)
            if inp.was_pressed(pygame.K_c):
                p.toggle_posture(POSTURE_CROUCH)
            if inp.was_pressed(pygame.K_z):
                p.toggle_posture(POSTURE_PRONE)
            if ctrl:
                p.set_posture(POSTURE_CROUCH)
            elif p._target_posture == POSTURE_CROUCH \
                    and not inp.was_pressed(pygame.K_c):
                p.set_posture(POSTURE_STAND)     # 松开 Ctrl 自动回站

        if st == ST_TITLE:
            if self.mouse_captured:
                self.release_mouse()     # TITLE 始终释放鼠标
            self.camera_rig.yaw = (self.camera_rig.yaw
                                   + TITLE_ORBIT_SPEED * dt) % 6.2831853
            if inp.was_pressed(pygame.K_1):
                self.start_challenge()
            elif inp.was_pressed(pygame.K_2):
                self.start_explore()
            elif inp.was_pressed(pygame.K_3):
                self.start_boss()

        elif st == ST_EXPLORE:
            self._handle_shortcuts(allow_reset=True)
            # 观光模式原本是死胡同：R 只重置世界，没有任何键能回标题页
            if inp.was_pressed(pygame.K_t):
                self.back_to_title(ST_EXPLORE)

        elif st in GUN_STATES:
            self._handle_shortcuts(allow_reset=False)
            if inp.was_pressed(pygame.K_r) and self.weapon is not None:
                self.weapon.start_reload()
            # 升级三选一（需求 F17：间隙态未选时 K_1/2/3 归升级，
            # 此期间不切枪；选定后恢复 K_1/K_2 直选武器）。
            # INTERMISSION 与 BOSS_BREAK 共用同一份 `_apply_upgrade`，
            # 所以这里用 SUPPLY_STATES 一次覆盖两个间隙态。
            if st in SUPPLY_STATES and self._upgrade_pending:
                if inp.was_pressed(pygame.K_1):
                    self._apply_upgrade(0)
                elif inp.was_pressed(pygame.K_2):
                    self._apply_upgrade(1)
                elif inp.was_pressed(pygame.K_3):
                    self._apply_upgrade(2)
            elif self.weapons:
                if inp.was_pressed(pygame.K_q):        # 循环切换（需求 E14）
                    self._cycle_weapon()
                elif inp.was_pressed(pygame.K_1):      # COMBAT 内直选（E14）
                    self._select_weapon(0)
                elif inp.was_pressed(pygame.K_2):
                    self._select_weapon(1)
            self._combat_time += dt
            if st == ST_INTERMISSION:
                self._intermission_t -= dt
                if self._intermission_t <= 0.0:
                    if self._upgrade_pending:          # 超时自动 [1]（需求 F17）
                        self._apply_upgrade(0, auto=True)
                    self.waves.start_wave(self._next_wave)
                    self._enter_state(ST_COMBAT)
            elif st == ST_BOSS_BREAK:
                self._intermission_t -= dt
                if self._intermission_t <= 0.0:
                    if self._upgrade_pending:      # 超时自动 [1]（同波次关）
                        self._apply_upgrade(0, auto=True)
                    self.bosses.spawn_stage()      # 倒计时结束才刷 Boss（本层全部）
                    self._enter_state(ST_BOSS)
            elif st == ST_BOSS:
                # Boss 关的进度推进（升段 / 通关判定）。
                # **必须写在这个 if/elif 链内部**：若另开一个
                # `elif st == ST_BOSS:`，它永远不会被执行 ——
                # 上面的 `elif st in (ST_COMBAT, ST_INTERMISSION,
                # ST_BOSS, ST_BOSS_BREAK)` 已经先把 ST_BOSS 吃掉了。
                # （踩过一次：分支写了但永远不跑，Boss 打死不升段。）
                if self.bosses.stage_cleared():
                    if self.bosses.all_cleared():
                        self._enter_state(ST_WIN)
                    else:
                        # 层间间隙：补给 + 三选一（同 INTERMISSION 的全部内容）
                        self._boss_stage = self.bosses.stage_index + 1
                        self.bosses.start_stage(self._boss_stage)
                        self.combat.clear_boss_drop_state()
                        self._intermission_t = INTERMISSION_TIME
                        self.weapon.supply()
                        self.combat.supply()
                        self._upgrade_pending = True
                        self._enter_state(ST_BOSS_BREAK)
            else:
                self.waves.update(dt)
                if self.waves.wave_cleared():
                    if self.waves.wave_index >= 5:
                        self._enter_state(ST_WIN)
                    else:
                        self._next_wave = self.waves.wave_index + 1
                        self._intermission_t = INTERMISSION_TIME
                        self.weapon.supply()       # 仅取消换弹（需求 G18）
                        self.combat.supply()       # HP +30（≤max_hp）
                        self._upgrade_pending = True
                        self._enter_state(ST_INTERMISSION)

        elif st in (ST_WIN, ST_LOSE):
            if inp.was_pressed(pygame.K_r):
                self.back_to_title(st)
            elif inp.was_pressed(pygame.K_t):
                self.back_to_title(st)

        elif st == ST_QUIT:
            # Esc 已在 on_escape 里处理（取消退出）；这里只管确认键
            if (inp.was_pressed(pygame.K_RETURN)
                    or inp.was_pressed(pygame.K_KP_ENTER)
                    or inp.was_pressed(pygame.K_SPACE)):
                self.request_quit()       # 唯一真正结束进程的入口
            elif inp.was_pressed(pygame.K_t):
                self.back_to_title(ST_TITLE)

        # 玩家控制（TITLE/RESULT 冻结）
        if self.player is not None and st in PLAYABLE_STATES:
            self.player.control(self.input, self.camera_rig, dt)
            if self.player.transform.position.y < -12.0:   # 坠落保险丝
                self.player.respawn()

        # 相机装配（fps 立即跟随鼠标；follow 指数平滑逼近）
        # sens_scale：狙击开镜时降到 35%（ENG-007 模块二"瞄准降灵敏度"）
        w = self.weapon
        sens = 1.0
        if w is not None and getattr(w, "has_ads", False):
            sens = 1.0 - (1.0 - w.ads_hip_sens_scale) * w.ads_t
        self.camera_rig.update(self.input, self.player, dt,
                               self.mouse_captured, self._aspect,
                               sens_scale=sens)

        # 战斗系统每帧 + ADS FOV（需求 E12：FOV = lerp(62°, 18°, ads_t)）
        # 用 GUN_STATES 而不是 (ST_COMBAT, ST_INTERMISSION)：**漏一个状态
        # 武器就整帧不更新**，表现为"Boss 战里扣扳机没反应"。
        if st in GUN_STATES:
            self.combat.update(dt)
            self.weapon.update(dt)
            if getattr(self.weapon, "has_ads", False):
                self.camera_rig.fov = math.radians(
                    lerp(62.0, 18.0, self.weapon.ads_t))
            else:
                self.camera_rig.fov = math.radians(62.0)
        else:
            self.camera_rig.fov = math.radians(62.0)

        self._sync_flashlight()

    # ------------------------------------------------------------------
    # 武器切换（需求 E14）
    # ------------------------------------------------------------------
    def _select_weapon(self, idx):
        if not self.weapons or idx >= len(self.weapons):
            return
        if self.weapon is self.weapons[idx]:
            return
        if self.weapon is not None:
            self.weapon.set_visible(False)
        self.weapon = self.weapons[idx]
        self.weapon.raise_weapon()         # 0.4s raise + 取消换弹

    def _cycle_weapon(self):
        if not self.weapons:
            return
        try:
            cur = self.weapons.index(self.weapon)
        except ValueError:
            cur = 0
        self._select_weapon((cur + 1) % len(self.weapons))

    # ------------------------------------------------------------------
    # 波次三选一升级（需求 F15/F16/F17）
    # ------------------------------------------------------------------
    def _apply_upgrade(self, idx, auto=False):
        """应用第 idx 项升级；auto=True 为 8s 超时自动选择（记录后缀）。"""
        c, p = self.combat, self.player
        if idx == 0:       # 生命强化：max_hp+25 且 HP+25
            p.max_hp += 25.0
            p.hp = min(p.max_hp, p.hp + 25.0)
            name = "生命"
        elif idx == 1:     # 火力强化：damage_mult ×1.25
            c.damage_mult *= 1.25
            name = "火力"
        else:              # 防护强化：armor+30 且 damage_reduction+0.08（≤0.40）
            p.armor += 30.0
            p.damage_reduction = min(0.40, p.damage_reduction + 0.08)
            name = "防护"
        if auto:
            name += "(自动)"
        self.upgrades_taken.append(name)
        self._upgrade_pending = False

    def _handle_shortcuts(self, allow_reset):
        inp = self.input
        if inp.was_pressed(pygame.K_TAB):
            self.camera_rig.toggle_mode()

        fl = self.flashlight
        if fl is not None:
            if inp.was_pressed(pygame.K_f):
                fl.enabled = not fl.enabled
            if inp.was_pressed(pygame.K_LEFTBRACKET):
                deg = fl.outer_angle * 57.29577951 - 2.5
                fl.set_outer_angle_deg(deg)
                fl.set_inner_angle_deg(deg * 0.55)
            if inp.was_pressed(pygame.K_RIGHTBRACKET):
                deg = fl.outer_angle * 57.29577951 + 2.5
                fl.set_outer_angle_deg(deg)
                fl.set_inner_angle_deg(deg * 0.55)
            if inp.was_pressed(pygame.K_MINUS):
                fl.add_intensity(-0.4)
            if inp.was_pressed(pygame.K_EQUALS):
                fl.add_intensity(0.4)
            if inp.was_pressed(pygame.K_COMMA):
                fl.add_attenuation(-0.005)
            if inp.was_pressed(pygame.K_PERIOD):
                fl.add_attenuation(0.005)

        if allow_reset and inp.was_pressed(pygame.K_r):
            self.reset_world()

    def _sync_flashlight(self):
        """手电始终贴着相机：fps 模式=第一人称光锥；follow 模式=镜头同轴。"""
        if self.flashlight is None:
            return
        pos, fwd = self.camera_rig.eye_and_forward()
        self.flashlight.position = pos
        self.flashlight.direction = fwd

    # ------------------------------------------------------------------
    # HUD
    # ------------------------------------------------------------------
    def get_hud_lines(self):
        if self.state == ST_EXPLORE:
            return HUD.compose_lines(self)
        cjk = self.hud.using_cjk if self.hud is not None else False

        def t(zh, en):
            return zh if cjk else en

        if self.state == ST_TITLE:
            return [("%s | FPS %d | %s: %d" % (
                t("迷你世界", "MiniWorld"), self.fps_display,
                t("实体", "ents"), self.scene.entity_count), True)]
        if self.state == ST_INTERMISSION:
            return [("%s %ds → %s %d | HP %d | %s %d/%d | FPS %d" % (
                t("间隙补给", "Supply"), max(0, int(self._intermission_t) + 1),
                t("下一波 第", "Next wave"), self._next_wave,
                int(self.player.hp),
                t("弹药", "Ammo"), self.weapon.mag, self.weapon.reserve,
                self.fps_display), True)]
        if self.state == ST_COMBAT:
            alive = self.combat.alive_bot_count()
            return [("%s %d | %s %d | HP %d | %s %d/%d | %s %d | FPS %d" % (
                t("第", "Wave"), self.waves.wave_index,
                t("波 · 余敌", " left"), alive,
                int(self.player.hp),
                t("弹药", "Ammo"), self.weapon.mag, self.weapon.reserve,
                t("击杀", "Kills"), self.combat.kills,
                self.fps_display), True)]
        if self.state == ST_BOSS_BREAK:
            nxt = (self.bosses.stage_index + 1
                   if self.bosses and self.bosses.stage_index == 0
                   else self.bosses.stage_number() if self.bosses else 1)
            return [("%s %ds → %s %d/%d | HP %d | %s %d/%d | FPS %d" % (
                t("Boss 关准备", "Boss Ready"), max(0, int(self._intermission_t) + 1),
                t("第", "Stage"), nxt, BOSS_TOTAL_STAGES,
                int(self.player.hp),
                t("弹药", "Ammo"), self.weapon.mag, self.weapon.reserve,
                self.fps_display), True)]
        if self.state == ST_BOSS:
            # 多 Boss：报**总血量 + 剩余只数**。用"第一只的血"会误导
            # 玩家以为打空了（4 只时尤其明显）。
            mgr = self.bosses
            alive = mgr.alive() if mgr else []
            hp = int(mgr.total_hp()) if mgr else 0
            mx = int(mgr.max_total_hp()) if mgr else 0
            return [("%s %s ×%d | %s %d/%d | %s %d/%d | %s %d | FPS %d" % (
                t("Boss", "Boss"),
                mgr.name() if mgr else "?", len(alive),
                t("总血", "HP"), hp, mx,
                t("弹药", "Ammo"), self.weapon.mag, self.weapon.reserve,
                t("击杀", "Kills"), self.combat.kills,
                self.fps_display), True)]
        # RESULT 页：一行最小信息（数据在居中卡片）
        return [("%s | FPS %d" % (t("迷你世界", "MiniWorld"),
                                  self.fps_display), True)]

    def get_hud_cards(self):
        """HUD 绘制指令：TITLE / RESULT 居中卡片 + COMBAT/INTERMISSION
        战斗 HUD（GDD §7 七个元素，Sprint 2）。EXPLORE 恒为 None（不变）。"""
        if self.hud is None:
            return None
        cjk = self.hud.using_cjk

        def t(zh, en):
            return zh if cjk else en

        cx, cy = self.width / 2, self.height / 2
        cards = None
        if self.state == ST_TITLE:
            lines = [
                (t("迷你世界 · 选择模式", "MiniWorld - Choose a Mode"), True),
                (t("[1] 波次挑战    5 波生存战",
                   "[1] Wave Challenge    5-wave survival"), True),
                (t("[2] 观光模式    自由探索",
                   "[2] Explore Mode    Free roam"), True),
                (t("[3] 奶龙 Boss 关    2 层超级 Boss",
                   "[3] NaiLong Boss    2 super bosses"), True),
                (t("移动 [WASD]   瞄准 [鼠标]   射击 [左键]",
                   "Move [WASD]   Aim [Mouse]   Fire [LMB]"), "dim"),
                (t("切枪 [Q]（或 [1] 步枪 / [2] 狙击）   开镜 [右键]",
                   "Swap [Q] (or [1] rifle / [2] sniper)   Scope [RMB]"), "dim"),
                (t("换弹 [R]   疾跑 [Shift]   跳跃 [空格]   说明 [H]",
                   "Reload [R]   Sprint [Shift]   Jump [Space]   Help [H]"),
                 "dim"),
            ]
            cards = [{"x": cx, "y": cy, "lines": lines,
                      "token": True, "center": True}]
        if self.state in (ST_WIN, ST_LOSE):
            in_boss = self.mode == "boss"
            if self.state == ST_WIN:
                headline = (t("Boss 击破 · 两层全部肃清！",
                              "Boss Defeated - both stages!") if in_boss
                            else t("挑战成功 · 全部 5 波肃清！",
                                   "Victory - all 5 waves cleared!"))
            else:
                headline = (t("战斗失败 · 止步第 %d 层"
                              % self._lose_boss_stage,
                              "Defeated at stage %d" % self._lose_boss_stage)
                            if in_boss else
                            t("战斗失败 · 止步第 %d 波" % self._lose_wave,
                              "Defeated at wave %d" % self._lose_wave))
            c = self.combat
            acc = (100.0 * c.shots_hit / c.shots_fired) if c and \
                c.shots_fired else 0.0
            kills = c.kills if c else 0
            heads = c.headshots if c else 0
            secs = int(self._combat_time)
            lines = [
                (headline, True),
                (t("击杀 %d · 爆头 %d · 命中率 %d%% · 用时 %d:%02d"
                   % (kills, heads, int(acc), secs // 60, secs % 60),
                   "Kills %d · Headshots %d · Acc %d%% · Time %d:%02d"
                   % (kills, heads, int(acc), secs // 60, secs % 60)),
                 False),
                (t("[R] 再来一局    [Esc] 退出",
                   "[R] Play Again    [Esc] Quit"), "dim"),
            ]
            # 需求 F17：结算页数据行追加本次升级选择列表
            if self.upgrades_taken:
                lines.insert(2, (
                    t("强化：%s" % "，".join(self.upgrades_taken),
                      "Upgrades: %s" % ", ".join(self.upgrades_taken)),
                    "dim"))
            cards = [{"x": cx, "y": cy, "lines": lines,
                     "token": True, "center": True}]
        elif self.state == ST_QUIT:
            lines = [
                (t("要退出游戏吗？", "Quit the game?"), True),
                (t("[Enter] 确认退出      [Esc] 返回上一页",
                   "[Enter] Confirm Quit      [Esc] Go Back"), "dim"),
                (t("[T] 回到标题页（继续玩）",
                   "[T] Back to Title (keep playing)"), "dim"),
            ]
            cards = [{"x": cx, "y": cy, "lines": lines,
                      "token": True, "center": True}]
        elif self.state in COMBAT_HUD_STATES:
            cards = self._combat_hud_commands(t, cx, cy)

        # H 键：按键总览居中卡片（任何状态可用，画在最上层）
        if self.hud.show_help:
            cards = (cards or []) + [
                {"x": cx, "y": cy,
                 "lines": self._help_card_lines(t),
                 "token": True, "center": True}]
        return cards

    # ------------------------------------------------------------------
    # H 键：按键总览（所有状态通用，居中卡片）
    # ------------------------------------------------------------------
    def _help_card_lines(self, t):
        """完整按键表。须与 README「🎮 操作按键」节保持一致。

        与 HUD 左上角常驻卡片的分工：那里只放视角/手电/一行 [H] 提示，
        全量键位放这里。修复前 help 内容只存在于 HUD.compose_lines，而它
        仅在 EXPLORE 状态被调用——其余状态 get_hud_lines 只返回 1 行状态条，
        HUD.render 切不出第 2 张卡片，于是 TITLE/COMBAT/INTERMISSION/RESULT
        下按 H 完全没有反应。
        """
        return [
            (t("按键说明", "Controls"), True),
            ("", False),      # 空行占位，作分组间距
            (t("移动 [W][A][S][D]    转向 [鼠标]    疾跑 [Shift]    跳跃 [空格]",
               "Move [W][A][S][D]   Look [Mouse]   Sprint [Shift]   Jump [Space]"),
             False),
            (t("射击 [左键]（按住）    换弹 [R]    狙击开镜 [右键]（按住）",
               "Fire [LMB] (hold)   Reload [R]   Scope [RMB] (hold)"), False),
            (t("切枪 [Q] 循环    直选 [1] 步枪 / [2] 狙击",
               "Swap [Q] cycle   Direct [1] rifle / [2] sniper"), False),
            ("", False),
            (t("视角 [Tab]    手电 [F]    锥角 [ ] 键    强度 [-] [=] 键    衰减 [,] [.] 键",
               "View [Tab]   Torch [F]   Cone [ ]   Power [-] [=]   Falloff [,] [.]"),
             False),
            (t("[Esc] 释放鼠标（再按退出）    左键点击窗口 = 重新捕获",
               "[Esc] release mouse (again to quit)   LMB click = recapture"),
             False),
            ("", False),
            (t("姿态 [Ctrl] 按住下蹲   [C] 切换下蹲   [Z] 切换趴下（趴下更稳）",
               "Posture [Ctrl] hold crouch  [C] toggle crouch  [Z] toggle prone"),
             False),
            (t("间隙三选一： [1] 生命  [2] 火力  [3] 防护（8s 不选自动取 1）",
               "Intermission: [1] HP  [2] Damage  [3] Armor (auto [1] after 8s)"),
             False),
            (t("结算页 [R] 再来一局    观光模式 [T] 回标题页",
               "Result [R] retry    Explore [T] back to title"), False),
            ("", False),
            (t("退出：任何页面按 [Esc] 弹确认页， [Enter] 确认退出",
               "Quit: [Esc] anywhere for the confirm page, [Enter] to quit"), "dim"),
            (t("[T] 回到标题页（继续玩）",
               "[T] back to title (keep playing)"), "dim"),
            (t("[H] 关闭本页", "[H] close"), "dim"),
        ]

    # ------------------------------------------------------------------
    # 顶部横幅纵向定位（ENG-005）
    # ------------------------------------------------------------------
    # 与 HUD.render 里左上常驻卡的落点保持一致
    STATUS_CARD_POS = (16, 14)
    STATUS_CARD_GAP = 8              # 沿用 render 里两张卡片之间的间距

    def _banner_center_y(self, banner_lines):
        """横幅卡片的中心 y：落在左上状态卡下沿 + 间距处，保证不遮盖。

        状态卡高度按当帧实际文本实测（COMBAT/INTERMISSION 为 1 行、
        EXPLORE 为 3 行），因此换文案或换字号都会自动跟随，不写死像素。
        横向仍交给调用方用 W/2 作中心锚点，窗口缩放不会错位。
        """
        status_h = self.hud.measure_card(
            self.get_hud_lines()[:3], False)[1]
        banner_h = self.hud.measure_card(banner_lines, True)[1]
        top = self.STATUS_CARD_POS[1] + status_h + self.STATUS_CARD_GAP
        return top + banner_h / 2.0

    # ------------------------------------------------------------------
    # Sprint 2：战斗 HUD 指令组装（GDD §7 七个元素）
    # ------------------------------------------------------------------
    def _combat_hud_commands(self, t, cx, cy):
        """每帧组装战斗 HUD 指令列表（engine.hud 按 kind 分发）。

        时间语义：本方法只在有窗口渲染时逐帧调用，动画计时（横幅渐隐 /
        vignette 渐隐 / 低血脉冲）在此用 app.time 差分推进，headless
        selftest 不消耗任何 HUD 开销。
        """
        W, H = self.width, self.height
        now = self.time
        dt = max(0.0, now - self._hud_t)
        self._hud_t = now
        self._banner_t = max(0.0, self._banner_t - dt)
        self._hurt_t = max(0.0, self._hurt_t - dt)

        # 受击沿：hp 下降沿同时驱动 vignette 与血条闪白 1 帧
        hp = self.player.hp
        hurt_edge = hp < self._prev_hp - 1e-6
        if hurt_edge:
            self._hurt_t = 0.25
        self._prev_hp = hp

        cmds = []
        # 「间隙态」标记：波次关的 INTERMISSION 与 Boss 关的 BOSS_BREAK
        # 共用同一套横幅 + 三选一逻辑，只是文案不同。
        st_intermission = self.state in SUPPLY_STATES
        in_boss = self.mode == "boss"

        # 7) 受击 vignette：四边红条 α=90，250ms 线性渐隐（最底层先画）
        if self._hurt_t > 0.0:
            cmds.append({"kind": "vignette",
                         "alpha": int(90 * self._hurt_t / 0.25)})

        # 1) 血量条：左下卡片 (16, H-78)，HP<25% 珊瑚红 + 1s 周期 α 脉冲
        ratio = max(0.0, hp) / 100.0
        pulse = 1.0
        if ratio < 0.25:
            pulse = 0.6 + 0.4 * math.sin(now * math.tau)
        cmds.append({"kind": "hp", "x": 16, "y": H - 78, "label": "HP",
                     "ratio": ratio, "flash": hurt_edge, "pulse": pulse})

        # 10) 姿态指示（ENG-007 模块一）：左下 HP 卡右侧一行小字，
        # 显示当前姿态 + 移速惩罚；被掩体挡住时给 dim 提示。
        p = self.player
        if p is not None and p._target_posture != POSTURE_STAND:
            label = {POSTURE_CROUCH: "下蹲", POSTURE_PRONE: "趴下"}.get(
                p._target_posture, "")
            txt = t("%s · 移速 %d%%" % (label, int(p.speed_factor() * 100)),
                    "%s · Speed %d%%" % (label.upper(), int(
                        p.speed_factor() * 100)))
            cmds.append({"kind": "card", "x": 16, "y": H - 92, "lines":
                         [(txt, "dim" if p.posture_blocked else False)],
                         "token": True})

        # 2) 弹药计数：右下卡片 (W-196, H-78)；主行上方武器名小字行（需求 E14）
        w = self.weapon
        if w.reloading:
            ammo_lines = [(w.name, "dim"),
                          (t("换弹中…", "Reloading..."), True),
                          (t("弹匣｜备弹", "MAG | RESERVE"), "dim")]
        else:
            ammo_lines = [(w.name, "dim"),
                          ("%d ｜ %d" % (w.mag, w.reserve), "big"),
                          (t("弹匣｜备弹", "MAG | RESERVE"), "dim")]
        cmds.append({"kind": "card", "x": W - 196, "y": H - 78,
                     "lines": ammo_lines})

        # 4) 波次横幅：顶部居中。纵向位置由左上状态卡的**实测高度**推出
        #    （ENG-005），不再写死 y=64——1280×720（默认分辨率）下
        #    状态卡宽 637px、横幅中心恰在 x=640，两者重叠 120×27px，
        #    横幅会把状态卡尾部"击杀 / FPS"压掉一半。横向仍是 W/2 中心锚点。
        if in_boss:
            # ---- Boss 关横幅 ----
            if self.state == ST_BOSS:
                if self._banner_t > 0.0:
                    alpha = 255 if self._banner_t > 0.5 \
                        else int(510 * self._banner_t)
                    n = self.bosses.stage_number()
                    k = self.bosses.count()
                    b_lines = [(t("第 %d 层 · %s ×%d"
                                   % (n, self.bosses.name(), k),
                                   "Stage %d - %s x%d"
                                   % (n, self.bosses.name(), k)), True)]
                    cmds.append({"kind": "card", "x": cx,
                                 "y": self._banner_center_y(b_lines),
                                 "lines": b_lines,
                                 "center": True, "alpha": alpha})
            else:
                secs = max(0, int(self._intermission_t) + 1)
                if self.bosses.stage_index == 0 and not self._upgrade_pending:
                    text = t("Boss 关准备 · 第 1 层 %ds" % secs,
                             "Boss ready - Stage 1 in %ds" % secs)
                elif self._upgrade_pending:
                    text = t("选强化后迎战 · 第 %d 层 %ds"
                             % (self.bosses.stage_index + 1, secs),
                             "Pick upgrade - Stage %d in %ds"
                             % (self.bosses.stage_index + 1, secs))
                else:
                    text = t("第 %d 层已肃清 · 下一层 %ds"
                             % (self.bosses.stage_index, secs),
                             "Stage %d cleared - Next in %ds"
                             % (self.bosses.stage_index, secs))
                b_lines = [(text, True)]
                cmds.append({"kind": "card", "x": cx,
                             "y": self._banner_center_y(b_lines),
                             "lines": b_lines, "center": True})
        elif self.state == ST_COMBAT:
            if self._banner_t > 0.0:
                n = self.waves.wave_index
                m = WAVES[n - 1][0]         # 本波敌人总数
                alpha = 255 if self._banner_t > 0.5 \
                    else int(510 * self._banner_t)
                b_lines = [(t("第 %d 波 · 敌人 %d" % (n, m),
                              "Wave %d · Enemies %d" % (n, m)), True)]
                cmds.append({"kind": "card", "x": cx,
                             "y": self._banner_center_y(b_lines),
                             "lines": b_lines,
                             "center": True, "alpha": alpha})
        else:
            secs = max(0, int(self._intermission_t) + 1)
            if self.waves.wave_index == 0:      # 开局 3s 倒计时
                text = t("准备迎战 · 第 1 波 %ds" % secs,
                         "Get ready · Wave 1 in %ds" % secs)
            else:
                text = t("第 %d 波已肃清 · 下一波 %ds"
                         % (self.waves.wave_index, secs),
                         "Wave %d cleared · Next in %ds"
                         % (self.waves.wave_index, secs))
            b_lines = [(text, True)]
            cmds.append({"kind": "card", "x": cx,
                         "y": self._banner_center_y(b_lines),
                         "lines": b_lines, "center": True})

        # 3) 动态准星：线隙 = spread 映射（0.6°≈8px，3°≈40px 线性外推）；
        #    狙击开镜（ads_t>0.5）改狙击样式圆环十字（需求 E12）
        if getattr(self.weapon, "has_ads", False) and \
                self.weapon.ads_t > 0.5:
            cmds.append({"kind": "scope", "cx": cx, "cy": cy})
        else:
            gap = 8.0 + (self.weapon.spread_deg - 0.6) * (32.0 / 2.4)
            gap = max(8.0, min(56.0, gap))
            ch_color = self.hud.dim_color if self.weapon.reloading \
                else (244, 248, 252, 220)
            cmds.append({"kind": "crosshair", "cx": cx, "cy": cy,
                         "gap": gap, "color": ch_color})

        # 6) 命中标记：最近一次 hit_event 的三态短时显示
        if self.combat.hit_events:
            t0, part, killed, _hs = self.combat.hit_events[-1]
            age = now - t0
            if killed:
                mark, dur = "kill", 0.20
            elif part == "head":
                mark, dur = "head", 0.15
            else:
                mark, dur = "body", 0.12
            if age < dur:
                cmds.append({"kind": "hitmark", "cx": cx, "cy": cy,
                             "mark": mark})

        # 5) 击杀飘字：中下 (W/2, H×0.62)，上浮 24px 0.8s 渐隐，同屏 ≤3
        items = []
        for t0, _part, killed, headshot in self.combat.hit_events:
            age = now - t0
            if killed and age < 0.8:
                text = (t("爆头击杀 +1", "Headshot Kill +1") if headshot
                        else t("+1 击杀", "+1 Kill"))
                color = self.hud.hl_color if headshot \
                    else self.hud.text_color
                alpha = int(255 * (1.0 - age / 0.8))
                items.append((text, color, alpha, 24.0 * age / 0.8))
        if items:
            cmds.append({"kind": "floats", "cx": cx, "y": H * 0.62,
                         "items": items[-3:]})

        # 8) 升级三选一卡片（需求 F15）：INTERMISSION / BOSS_BREAK 未选择时居中显示
        if st_intermission and self._upgrade_pending:
            p, c = self.player, self.combat
            param_line = t(
                "HP上限 %d · 火力 ×%.2f · 防护 %d · 减伤 %d%%"
                % (int(p.max_hp), c.damage_mult, int(p.armor),
                   int(p.damage_reduction * 100)),
                "MaxHP %d · Dmg x%.2f · Armor %d · DR %d%%"
                % (int(p.max_hp), c.damage_mult, int(p.armor),
                   int(p.damage_reduction * 100)))
            up_lines = [
                (t("选择强化", "Choose Upgrade"), True),
                (t("[1] 生命强化：HP 上限 +25 且 HP +25",
                   "[1] Vitality: Max HP +25 & HP +25"), "dim"),
                (t("[2] 火力强化：伤害 ×1.25",
                   "[2] Firepower: Damage x1.25"), "dim"),
                (t("[3] 防护强化：护甲 +30 且减伤 +8%",
                   "[3] Guard: Armor +30 & DR +8%"), "dim"),
                (param_line, "dim"),
            ]
            cmds.append({"kind": "card", "x": cx, "y": cy, "lines": up_lines,
                         "token": True, "center": True})

        # 9) bot 头顶血条（需求 A）：投影 + LOS + 距离裁剪后合并
        cmds.extend(self._mob_bar_commands())

        # 11) Boss 顶部固定血条（TITLE [3] 专属，**每只一条**）
        cmds.extend(self._boss_bar_cmds(t, cx))
        return cmds

    def _boss_bar_cmds(self, t, cx):
        """Boss 关顶部血条指令列表；非 Boss 交战态返回空列表。

        **每只 Boss 一条**，而不是一条聚合条 —— 第 1 层有 4 只，
        玩家要能一眼看出"我打的是哪一只、还剩哪几只没打"。
        聚合总血量在左上角状态行里（`get_hud_lines`），两处不重复。

        为什么不复用头顶 mobbar（`_mob_bar_commands` 里跳过 Boss）：
        - Boss 血量 1000/300，追一个在 3D 里移动的头顶会让血条乱晃；
        - Boss 2.86m 高、离得远时头几乎不可见，血条会突然消失；
        - 玩家需要随时知道还剩多少血，才好决定继续输出还是绕后。

        布局：第 i 条画在 `BOSS_BAR_Y + i * BOSS_BAR_STEP`，
        名字右对齐在血条左侧（Boss 名含中文，宽度不定，右对齐才整齐），
        层数只在第一条上标一次。
        """
        out = []
        if self.state != ST_BOSS or self.bosses is None:
            return out
        n = self.bosses.stage_number()
        w = min(460, max(240, int(self.width * 0.46)))
        for i, b in enumerate(self.bosses.alive()):
            out.append({
                "kind": "bossbar",
                "cx": cx, "y": BOSS_BAR_Y + i * BOSS_BAR_STEP,
                "w": w, "h": 12,
                "ratio": max(0.0, b.hp) / b.max_hp,
                "name": b.boss_name,
                "stage": (t("\u7b2c %d / %d \u5c42" % (n, BOSS_TOTAL_STAGES),
                           "Stage %d / %d" % (n, BOSS_TOTAL_STAGES))
                          if i == 0 else ""),
            })
        return out

    # ------------------------------------------------------------------
    # bot 头顶血条指令（需求 A3）：独立方法，headless 断言可直接调用
    # ------------------------------------------------------------------
    def _mob_bar_commands(self):
        """对每只存活 bot：头顶点 = base_pos+(0,2.06,0) 投影到屏幕；
        |ndc| ≤ 1.1（屏幕 5% 余量）、距离 ≤ 40m、eye→head 有 LOS 才生成。
        不引用 self.hud（headless 可用）。"""
        cmds = []
        if self.combat is None or self.player is None:
            return cmds
        rig = self.camera_rig
        eye, _fwd = rig.eye_and_forward()
        proj = rig.projection_matrix()
        view = rig.view_matrix()
        W, H = self.width, self.height
        for bot in self.combat._alive_bots():
            # Boss 不画头顶小血条：改用顶部固定大血条（见 `_boss_bar_cmd`）。
            # 原因：Boss 2.86m 高、离远时头几乎不可见，头顶条会乱晃并突然消失。
            if bool(getattr(bot, "is_boss", False)):
                continue
            dist = (bot.head_world() - eye).length()
            if dist > 40.0:
                continue
            if not self.combat.has_los(eye, bot.head_world()):
                continue
            top = bot.base_pos() + Vec3(0.0, 2.06, 0.0)
            r = project_to_screen(proj, view, top, W, H)
            if r is None:
                continue
            sx, sy, _depth = r
            if not (-0.05 * W <= sx <= 1.05 * W
                    and -0.05 * H <= sy <= 1.05 * H):
                continue
            cmds.append({"kind": "mobbar", "cx": sx, "y": sy,
                         "w": 56 if bot.elite else 44, "h": 5,
                         "ratio": max(0.0, bot.hp) / bot.max_hp,
                         "elite": bot.elite})
        return cmds

    # ------------------------------------------------------------------
    # 自检（--selftest，GDD §8 十项断言 + 原 5 项观光分支）
    # ------------------------------------------------------------------
    def on_shutdown(self):
        if self.selftest:
            self._report()

    def _selftest_script(self):
        """headless 自检脚本：按帧注入按键/鼠标，覆盖挑战全链路与观光分支。

        Sprint 3 时间线（ENG-004 需求 H）：
        - f280~520   狙击窗口（趁真波 bot ~f660 才近身）：K_2 切 SR-1 →
                     ADS 1.2s → 单发狙杀普通兵 / 精英减伤存活 → K_1 切回。
        - f560~950   步枪腰射窗口（原 300~950 后移，语义不变）。
        - f1000      debug_kill_all → INTERMISSION + 升级三选一 + 掉落。
        - f1010~1055 mobbar / 击退 / tracer / 升级选择 / 拾取循环断言。
        - f1600~     压低 HP 等 bot 命中 → RESULT_LOSE（原 1100 后移）。
        """
        f, inp = self.frame, self.input
        st = self._st
        TAP = inp.tap

        # ---------- 阶段二：观光分支（原 5 项断言的注入脚本） ----------
        if st["phase2"]:
            self._selftest_explore(f, inp, st)
            return

        # ---------- 阶段一：挑战模式（mode 恒为 challenge，combat 必存在） ----------
        if f == 5:
            TAP(pygame.K_1)                      # TITLE → INTERMISSION(3s)
        if self.state == ST_TITLE and f < 300:
            st["saw_title"] = True
        if self.state == ST_INTERMISSION and not st["saw_combat"]:
            st["saw_prewave"] = True
        if self.state == ST_COMBAT:
            st["saw_combat"] = True

        # 出生点正北被既有石阶群遮挡（GDD 俯视图的"中央开阔交火区"不含
        # 既有 step2/tower），先向右横移 ~5m 打开两条北向枪线再交战
        if 200 <= f < 270:
            inp.simulate(pygame.K_d)
        elif f == 270:
            inp.release(pygame.K_d)

        # 腰射窗口（原 300~950 后移至 560~950，为狙击窗口让路；bot 需
        # ~f660 才进入 17m 有效射程，语义不变）：瞄最近 bot、
        # 距离 ≤17m 且有 LOS 才开火；首杀后改瞄头，只在 bloom ≤0.7° 的
        # 低扩散窗口点射（爆头率保障）。
        firing = 560 <= f < 950
        if firing and self.state in GUN_STATES:
            if self.combat.kills >= 1:
                st["aim_head"] = True
            self.combat.aim_at_nearest_bot(head=st["aim_head"])
            info = self.combat.nearest_bot_info()
            low_bloom = (not st["aim_head"]
                         or self.weapon.bloom_deg <= 0.7)
            if (info is not None and info[1] <= 17.0 and info[2]
                    and low_bloom):
                inp.simulate_mouse(1)
            else:
                inp.release_mouse(1)        # 停火让 bloom 收敛
        else:
            inp.release_mouse(1)

        # ---- 狙击窗口（需求 E/H22：debug_spawn 近距兵，一枪狙杀/精英减伤）----
        if f == 280:
            TAP(pygame.K_2)                  # COMBAT 内 K_2 直选 SR-1
            st["sniper_kills_before"] = self.combat.kills
        if f == 310:
            st["sniper_bot"] = self.waves.debug_spawn(elite=False)
        if 320 <= f < 500:
            inp.simulate_mouse(3)            # 按住右键开镜（等 1.2s 收敛）
        if f == 400 and not st["sniper_one_shot"]:
            self.combat.aim_at_nearest_bot(head=True)
            inp.simulate_mouse(1)            # 单发狙杀（260/650 > 40HP）
        if f == 401:
            inp.release_mouse(1)
        if f == 410 and not st["sniper_one_shot"]:
            st["sniper_one_shot"] = (self.combat.kills
                                     > st["sniper_kills_before"])
        if f == 415:
            st["elite_bot"] = self.waves.debug_spawn(elite=True)
        if f == 495 and not st["elite_resist_done"]:
            self.combat.aim_at_nearest_bot(head=False)
            inp.simulate_mouse(1)            # 精英：260×0.5=130 < 160HP
        if f == 496:
            inp.release_mouse(1)
        if f == 500 and not st["elite_resist_done"]:
            b = st.get("elite_bot")
            st["sniper_elite_resist"] = (b is not None and not b.dead
                                         and abs(b.hp - 30.0) < 1.0)
            st["elite_resist_done"] = True
            # 清理测试用 bot（take_damage 直杀：正常掉落、不计 kills）
            for key in ("sniper_bot", "elite_bot"):
                b = st.get(key)
                if b is not None and not b.dead:
                    b.take_damage(999999.0, headshot=False)
        if f == 500:
            inp.release_mouse(3)             # 松开右键出镜
        if f == 520:
            TAP(pygame.K_1)                  # 切回步枪 MW-1

        if f == 330:
            # GDD 为 f≥260；3s 倒计时(180f)+刷怪间隔 2.2s(132f) 使第 2 只
            # 最早 f317 出现，确定性检查点取 f330（见交接说明）
            st["spawned2"] = self.waves.total_spawned >= 2
        if f == 900:
            st["kill_by_900"] = self.combat.kills >= 1
        if self.combat.headshots >= 1:
            st["headshot"] = True                # 累计爆头数 ≥ 1（GDD 语义）
        if f == 999:
            st["wave_at_clear"] = self.waves.wave_index
        if self.combat.min_hp < 100.0:
            st["damaged"] = True

        if f == 950:
            inp.release_mouse(1)
            TAP(pygame.K_r)                      # 手动换弹（1.6s）
            st["shots_at_reload"] = self.combat.shots_fired
        if f == 960:
            self.player.hp = 50.0                # 创造脱战回血观察条件
        if f == 1000:
            st["hp_at_clear"] = self.player.hp
            self.combat.debug_kill_all()         # 秒清当前波（GDD f1000）
        if f == 1005:
            # 语义改（需求 H21）：supply 不再补弹药 → 验证升级 offer 出现、
            # 波次推进、HP+30（≤max_hp 封顶）生效
            st["wave_transition"] = (
                self.state == ST_INTERMISSION
                and self._next_wave == st["wave_at_clear"] + 1
                and self._upgrade_pending
                and self.player.hp >= min(
                    self.player.max_hp,
                    st["hp_at_clear"] + 30.0) - 0.01)
        if f == 1055:
            st["reload_mag_ok"] = (self.weapon.mag == 30)
            st["reload_ok"] = (self.combat.shots_fired
                               == st["shots_at_reload"])
        if self.combat.regen_observed:
            st["regen"] = True

        # ---- mobbar（需求 H22，INTERMISSION 内，kb 兵静止便于投影）----
        if f == 1010:
            st["kb_bot"] = self.waves.debug_spawn(elite=False)
        if f in (1014, 1015):
            # 连续两帧瞄准（SPAWN 兵静止，保证 view 矩阵已对准）
            self.combat.aim_at_nearest_bot(head=False)
        if f == 1015:
            cmds = self._mob_bar_commands()
            st["mob_hp_bar"] = (len(cmds) > 0
                                and all(0.0 < c["ratio"] <= 1.0
                                        for c in cmds))
        if f == 1030:
            b = st.get("kb_bot")
            if b is not None and not b.dead:
                b.take_damage(999999.0, headshot=False)   # 清理（掉 pickup）

        # ---- 升级选择（需求 F/H22：tap K_2 → damage_mult×1.25）----
        if f == 1035:
            TAP(pygame.K_2)                  # INTERMISSION 未选 → 升级[2]
        if f == 1040:
            st["upgrade_choice"] = abs(
                self.combat.damage_mult - 1.25) < 1e-9

        # ---- 拾取循环（需求 G/H22：pickup 移近 → 计数+1）----
        if f == 1045:
            pickups = self.scene.query("pickup")
            st["pickup_seen"] = len(pickups) >= 1
            st["pick_before"] = (self.combat.pickups_hp
                                 + self.combat.pickups_ammo)
            p = self.player.transform.position
            if pickups:
                pickups[0].teleport(Vec3(p.x + 0.6, p.y - 0.35, p.z))
        if f == 1055:
            st["pickup_cycle"] = (
                st.get("pickup_seen", False)
                and (self.combat.pickups_hp
                     + self.combat.pickups_ammo)
                > st.get("pick_before", 0))

        # ---- 击退 / tracer（需求 H22：f1055 reload 断言之后才可直裁开火，
        #      否则 shots_fired 计数会污染 reload_ok）----
        if f == 1060:
            st["kb_bot"] = self.waves.debug_spawn(elite=False)
        if f in (1064, 1065):
            self.combat.aim_at_nearest_bot(head=False)
        if f == 1065:
            bot = st.get("kb_bot")
            if bot is not None and bot.body is not None:
                pre = math.hypot(bot.body.velocity.x,
                                 bot.body.velocity.z)
                eye, _fwd = self.camera_rig.eye_and_forward()
                target = bot.base_pos() + Vec3(0.0, 0.9, 0.0)
                self.combat.player_shoot(
                    eye, (target - eye).normalized(),
                    damage_body=20.0, headshot_mult=1.0)
                post = math.hypot(bot.body.velocity.x,
                                  bot.body.velocity.z)
                st["knockback_applied"] = (post - pre) > 0.3
                st["tracer_spawned"] = len(
                    self.scene.query("tracer")) >= 1
        if f == 1080:
            b = st.get("kb_bot")
            if b is not None and not b.dead:
                b.take_damage(999999.0, headshot=False)   # 清理（掉 pickup）

        # 压低 HP 等 bot 命中 → RESULT_LOSE（原 f1100 后移，为新测试让路）
        if f >= 1600 and not st["lose"] and self.state not in (
                ST_WIN, ST_LOSE) and self.state != ST_TITLE:
            self.player.hp = 1.0
            if f == 1600:
                # 近距兵加速终结（远波 bot 需 ~30s 才摸到玩家）
                self.waves.debug_spawn(elite=False)
                self.waves.debug_spawn(elite=False)
        if f == 2500 and not st["lose"] and self.state not in (
                ST_WIN, ST_LOSE, ST_TITLE):
            self.combat.damage_player(99999.0)    # 兜底：确保进入结算页
        if self.state == ST_LOSE:
            if not st["lose"]:
                st["lose"] = True
                # 战败前快照统计（reset 后 combat 对象重建）
                st["snap"] = dict(
                    waves=self.waves.max_wave_reached,
                    kills=self.combat.kills,
                    headshots=self.combat.headshots,
                    min_hp=self.combat.min_hp,
                    shots_fired=self.combat.shots_fired,
                    pickups=(self.combat.pickups_hp
                             + self.combat.pickups_ammo),
                    entities=len(self.scene.entities))
            if not st["lose_tapped"]:
                TAP(pygame.K_r)                  # 结算页 R → TITLE + 重建
                st["lose_tapped"] = True

        # 重启校验 → 进入观光分支
        if st["lose_tapped"] and not st["phase2"] \
                and self.state == ST_TITLE:
            st["restart_hp100"] = (self.player.hp == 100.0)
            st["restart_no_bots"] = (len(self.scene.query("bot")) == 0)
            st["restart_fewer"] = (len(self.scene.entities)
                                   < st["snap"]["entities"])
            st["restarted"] = True
            TAP(pygame.K_2)                      # TITLE → EXPLORE
            st["phase2"] = True
            st["p2_frame"] = f

    def _selftest_explore(self, f, inp, st):
        """阶段二注入：原 5 项断言对应脚本（explore 世界，combat 为 None）。"""
        TAP = inp.tap
        r = f - st["p2_frame"]
        if r == 2:
            TAP(pygame.K_f)                  # 手电关
        elif r == 4:
            TAP(pygame.K_f)                  # 手电开
        elif r == 6:
            TAP(pygame.K_RIGHTBRACKET)       # 光锥＋
        elif r == 8:
            TAP(pygame.K_LEFTBRACKET)        # 光锥－
        elif r == 10:
            TAP(pygame.K_EQUALS)             # 强度＋
        elif r == 12:
            TAP(pygame.K_MINUS)              # 强度－
        elif r == 14:
            TAP(pygame.K_COMMA)              # 衰减－
        elif r == 16:
            TAP(pygame.K_PERIOD)             # 衰减＋
        elif r == 18:
            TAP(pygame.K_h)                  # 帮助开
        elif r == 20:
            TAP(pygame.K_h)                  # 帮助关
        elif r == 22:
            inp.simulate(pygame.K_w)         # 前进
        elif r == 24:
            TAP(pygame.K_TAB)                # 切平滑跟随视角
        elif r == 26:
            TAP(pygame.K_SPACE)              # 跳跃
        elif r == 27:
            inp.release(pygame.K_SPACE)
        elif r == 40:
            TAP(pygame.K_TAB)                # 切回第一人称
        # 等炮台首发 → R 重建（覆盖重置链路）→ 再等首发
        if not st["p2_reset"]:
            if self.turret is not None and self.turret.shots >= 1:
                TAP(pygame.K_r)
                st["p2_reset"] = True
        elif not st["explore_done"]:
            if self.turret is not None and self.turret.shots >= 1:
                inp.release(pygame.K_w)
                st["explore_done"] = True

    def _report(self):
        import math as _math
        st = self._st
        snap = st.get("snap") or dict(
            waves=self.waves.max_wave_reached if self.waves else 0,
            kills=self.combat.kills if self.combat else 0,
            headshots=self.combat.headshots if self.combat else 0,
            min_hp=self.combat.min_hp if self.combat else 100.0,
            shots_fired=self.combat.shots_fired if self.combat else 0,
            pickups=(self.combat.pickups_hp + self.combat.pickups_ammo)
            if self.combat else 0,
            entities=len(self.scene.entities))

        def finite(v):
            return (_math.isfinite(v.x) and _math.isfinite(v.y)
                    and _math.isfinite(v.z))

        # ---- 挑战模式九项（GDD §8 #1~#9） ----
        checks = [
            ("title_to_combat",
             st["saw_title"] and st["saw_prewave"] and st["saw_combat"]),
            ("bots_spawned", st["spawned2"]),
            ("hitscan_kill", st["kill_by_900"]),
            ("headshot_bonus", st["headshot"]),
            ("player_damaged", st["damaged"]),
            ("regen_works", st["regen"]),
            ("reload_cycle", st["reload_mag_ok"] and st["reload_ok"]),
            ("wave_transition", st["wave_transition"]),
            ("lose_and_restart",
             st["lose"] and st["restarted"] and st["restart_hp100"]
             and st["restart_no_bots"] and st["restart_fewer"]),
        ]

        # ---- Sprint 3 新增七项（ENG-004 需求 H22） ----
        checks += [
            ("mob_hp_bar", st["mob_hp_bar"]),
            ("tracer_spawned", st["tracer_spawned"]),
            ("knockback_applied", st["knockback_applied"]),
            ("sniper_one_shot", st["sniper_one_shot"]),
            ("sniper_elite_resist", st["sniper_elite_resist"]),
            ("upgrade_choice", st["upgrade_choice"]),
            ("pickup_cycle", st["pickup_cycle"]),
        ]

        # ---- 观光分支五项（原断言全部保留，GDD §8 #10） ----
        p = self.player.transform.position
        checks.append(("player_in_world",
                       -29.0 < p.x < 29.0 and -29.0 < p.z < 29.0
                       and 0.4 < p.y < 3.2))
        checks.append(("turret_fired",
                       self.turret is not None and self.turret.shots >= 1
                       and self._shots_before_reset >= 1))
        checks.append(("flashlight_alive", self.flashlight.enabled))
        finite_ok = all(
            finite(e.transform.position)
            for e in self.scene.entities if e.body is not None)
        checks.append(("positions_finite", finite_ok))
        checks.append(("entities_alive", len(self.scene.entities) > 20))

        ok = True
        for name, passed in checks:
            print("[selftest] %-18s %s" % (name, "PASS" if passed else "FAIL"))
            ok = ok and passed
        print("[selftest] frames=%d entities=%d contacts=%d "
              "cone=%.1f° power=%.1f"
              % (self.frame, len(self.scene.entities),
                 self.physics.contact_count,
                 self.flashlight.outer_angle * 57.29577951,
                 self.flashlight.intensity))
        print("[selftest] waves=%d kills=%d headshots=%d min_hp=%d "
              "shots_fired=%d"
              % (snap["waves"], snap["kills"], snap["headshots"],
                 int(snap["min_hp"]), snap["shots_fired"]))
        print("[selftest] pickups=%d upgrades=%s"
              % (snap["pickups"],
                 ",".join(self.upgrades_taken) if self.upgrades_taken
                 else "-"))
        print("[selftest] RESULT: %s" % ("PASS" if ok else "FAIL"))
        if not ok:
            raise SystemExit(1)


def _new_selftest_state():
    return dict(
        saw_title=False, saw_prewave=False, saw_combat=False,
        spawned2=False, kill_by_900=False, headshot=False,
        aim_head=False, damaged=False, regen=False,
        reload_mag_ok=False, reload_ok=False, shots_at_reload=0,
        wave_transition=False, wave_at_clear=0, hp_at_clear=100.0,
        lose=False, lose_tapped=False, snap=None,
        restarted=False, restart_hp100=False, restart_no_bots=False,
        restart_fewer=False,
        # ---- Sprint 3 新增（需求 H22） ----
        sniper_kills_before=0, sniper_bot=None, elite_bot=None,
        elite_resist_done=False, sniper_one_shot=False,
        sniper_elite_resist=False, kb_bot=None,
        mob_hp_bar=False, tracer_spawned=False, knockback_applied=False,
        upgrade_choice=False,
        pickup_seen=False, pick_before=0, pickup_cycle=False,
        phase2=False, p2_frame=0, p2_reset=False, explore_done=False,
    )
