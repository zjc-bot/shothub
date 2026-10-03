# -*- coding: utf-8 -*-
"""人形持枪 bot：7 部件程序化拼装 + FSM + 须线避障 + burst 射击。

拼装（GDD §4.1）：全部 box_mesh 纯视觉部件，bot 根实体携带唯一的
全身物理体（half=(0.32,0.91,0.32)，mass=65，承担移动碰撞/推挤），
功能等同"仅躯干挂物理体"；仅头/躯干 cast_shadow。bot 根实体位置 =
物理体中心（站立时 y≈0.91），base_pos() 给出脚底原点，命中盒与
部件偏移均以脚底原点为基准（与 GDD §3.2/§4.1 字面值一致）。

FSM（GDD §4.2）：SPAWN(0.8s 闪烁入场) → APPROACH(直线+须线避障)
→ ENGAGE(8~14m 巡航 + 侧向游走 + 3 发 burst，首發警告弹)
→ REPOSITION(失去 LOS>1.5s 或 HP<30% 且波次≥3，走 8 导航点)
→ DEAD(侧倒 90°/0.25s + y 压缩下沉，0.5s 后移除)。

精准度（GDD §4.3）：err = wave_err × (1 + dist/40) + move_penalty(疾跑+1.2°)，
bot 子弹同样先过 raycast_world 静态遮挡——躲掩体后自然安全。
"""
import math
import random

from engine.scene import Entity
from engine.physics import PhysicsBody
from engine.mesh import box_mesh, MeshRenderer
from engine.math_utils import (
    Vec3, dir_from_yaw_pitch, yaw_pitch_from_dir, wrap_angle,
)

from .combat import (raycast_world, HEAD_OFFSET, HEAD_HALF,
                     BODY_OFFSET, BODY_HALF)

# ---- 导航点（GDD §6.2 字面值，REPOSITION 用）----
# 原来定义在 `waves.py`，但**用它的 `BaseBot._begin_reposition` 没 import**
# —— 于是任何 bot 在 ENGAGE 里丢失视线超过 LOSE_LOS_TIME 就直接
# `NameError: name 'NAV_POINTS' is not defined` 崩掉整个游戏。
# （t_rush 把玩家瞬移 40m 才把它逼出来；波次关同样会踩到。）
#
# 为什么放 bots.py 而不是 import waves：`waves -> units.stormtrooper
# -> bots` 已经是一条链，bots 再 import waves 就成环。导航点是**AI**
# 的数据，天然属于 bots；waves.py 保留 `from .bots import NAV_POINTS`
# 转出，`world.py` 的既有 import 不受影响。
NAV_POINTS = (
    (-12.0, -10.0), (12.0, -10.0), (-12.0, 14.0), (12.0, 12.0),
    (0.0, -13.0), (0.0, 13.0), (-6.0, -16.0), (6.0, 16.0),
)

# ---- 部件表（GDD §4.1 字面值：(w,h,d), (x右,y上,z前), color, shin, spec, shadow） ----
_SKIN = (0.93, 0.80, 0.66)
_TORSO_NORMAL = (0.52, 0.62, 0.60)
_TORSO_ELITE = (0.90, 0.42, 0.38)
_LEG_COLOR = (0.58, 0.62, 0.70)
_GUN_COLOR = (0.40, 0.42, 0.46)
_FLASH_COLOR = (1.0, 0.82, 0.45)

BODY_HALF_Y = 0.91                # 物理体半高（脚底原点 ↔ 实体中心换算）

APPROACH_SPEED = 3.2
STRAFE_SPEED = 1.8
RETREAT_SPEED = 2.0
AIM_TIME = 0.5
BURST_SHOTS = 3
BURST_INTERVAL = 0.12
ENGAGE_MIN = 8.0
ENGAGE_MAX = 14.0
LOSE_LOS_TIME = 1.5


def _rot_local(off, yaw):
    """局部 (x右, y上, z前) 偏移 → 世界偏移（bot 前方 = dir_from_yaw_pitch(yaw,0)）。"""
    s, c = math.sin(yaw), math.cos(yaw)
    return Vec3(off.x * c - off.z * s, off.y, -off.x * s - off.z * c)


class BaseBot(Entity):
    """所有"敌人"角色的公共基类。

    抽出它是为了让 Boss（奶龙）与人形 bot 共用同一套物理/FSM/命中盒契约，
    同时把**外观**留给子类各自实现。

    与旧版的区别：原 `HumanoidBot` 身兼"外观 + 行为"两职，Boss 想换个体型
    就得整类复制。现在 `HumanoidBot` 退化为 `BaseBot` + 人形外观。
    """

    # ---- 命中盒：子类可覆写（Boss 体型差一个量级） ----
    HEAD_BOX = None            # None -> 用 combat 的模块常量
    HEAD_BOX_HALF = None
    BODY_BOX = None
    BODY_BOX_HALF = None

    # ---- 外观：子类填 (key, size, offset, color, shin, spec, shadow) ----
    SPEC = ()

    # ---- 尺寸派生 ----
    BODY_HALF_Y_LOCAL = 0.91    # 脚底原点 <-> 实体中心的半高

    @property
    def yaw(self):
        return self._yaw

    @yaw.setter
    def yaw(self, v):
        self._yaw = v

    # ---- 行为参数（Boss 会改） ----
    MOVE_SPEED = APPROACH_SPEED
    STRAFE_SPEED_ = STRAFE_SPEED
    RETREAT_SPEED_ = RETREAT_SPEED
    ENGAGE_MIN_ = ENGAGE_MIN
    ENGAGE_MAX_ = ENGAGE_MAX
    AIM_TIME_ = AIM_TIME
    BURST_SHOTS_ = BURST_SHOTS
    BURST_INTERVAL_ = BURST_INTERVAL
    BURST_GAP = (1.1, 1.6)

    def __init__(self, scene, physics, app, pos_xz, hp, elite,
                 wave_damage, wave_err, wave_index):
        super().__init__("bot", "bot")
        self.scene = scene
        self.app = app
        self.max_hp = float(hp)
        self.hp = float(hp)
        self.elite = elite
        self.armor = 60.0 if elite else 25.0    # 防护（需求 C8 击退用）
        self.damage = wave_damage
        self.err_base = wave_err
        self.wave_index = wave_index
        self.wave_cleared = False

        self.dead = False
        self._death_t = 0.0
        self.state = "SPAWN"
        # 出生朝向场心（GDD §6.3：刷怪点出生朝向场心）
        self._yaw = math.atan2(pos_xz[0], pos_xz[1])
        self._walk_phase = random.uniform(0.0, math.tau)
        self._state_t = 0.0
        self._aim_t = 0.0
        self._burst_left = 0
        self._burst_first = True
        self._burst_gap_t = 0.0
        self._shot_t = 0.0
        self._los_lost_t = 0.0
        self._strafe_t = 0.0
        self._strafe_dir = 1.0
        self._nav_target = None
        self._repositioned_low_hp = False
        self._flash_t = 0.0
        self._hit_flash_t = 0.0

        self.last_hit_part = None         # "head" / "body"
        self.hit_flash_at = 0.0

        # ---- 根实体：唯一物理体（全身 AABB，承担移动碰撞/推挤） ----
        half = Vec3(0.32, self.BODY_HALF_Y_LOCAL, 0.32)
        self.transform.position = Vec3(pos_xz[0], self.BODY_HALF_Y_LOCAL,
                                       pos_xz[1])
        self.body = PhysicsBody(self, "aabb", half=half,
                                mass=self._body_mass(), friction=0.0)
        physics.register(self.body)

        # ---- 视觉部件 ----
        self.parts = {}           # key -> (entity, local_offset, base_color)
        for key, size, off, color, shin, sp, shadow in self.SPEC:
            e = Entity("bot_" + key, "bot_part")
            e.renderer = MeshRenderer(box_mesh(*size), color=color,
                                      cast_shadow=shadow,
                                      shininess=shin, spec_strength=sp)
            scene.add(e)
            self.parts[key] = [e, Vec3(*off), color]

        self._muzzle = self.transform.position.clone()
        self._sync_parts(0.0)
        scene.add(self)

    def _sync_muzzle_from_parts(self):
        """把 `self._muzzle` 设为朝向玩家最近的枪口（多枪时用最近的那个）。

        单枪 / 无枪时 `self._muzzle` 原样保留 —— 与旧行为一致。
        """
        gun_keys = [k for k in self.parts if k.startswith("gun")]
        if not gun_keys:
            return
        eye = (self.app.player.transform.position
               + Vec3(0.0, self.app.player.eye_height, 0.0))
        best, best_d = self._muzzle, float("inf")
        for key in gun_keys:
            _e, off, _c = self.parts[key]
            w = self.base_pos() + _rot_local(off, self.yaw)
            d = (w - eye).length_sq()
            if d < best_d:
                best_d, best = d, w
        self._muzzle = best

    def _body_mass(self):
        return 65.0

    # ------------------------------------------------------------------
    def base_pos(self):
        """脚底原点（命中盒/部件偏移基准，GDD 中的"bot 位置"）。"""
        p = self.transform.position
        return Vec3(p.x, p.y - self.BODY_HALF_Y_LOCAL, p.z)

    def hitboxes(self):
        """本角色的命中盒：(HEAD_OFFSET, HEAD_HALF, BODY_OFFSET, BODY_HALF)。

        `combat.player_shoot` 逐角色调用本方法，**不再直接读模块常量**。
        默认返回 `combat.py` 的模块常量 —— 所以所有普通兵的行为逐位不变。

        存在的理由：Boss 体型与普通兵差一个量级（奶龙高 2.77m / 宽 1.5m，
        普通兵 1.83m / 0.6m），共用同一套盒子会让 Boss「打不中下半身」
        或者「隔空判定」。Boss 子类覆写本方法即可，**不需要改 combat 的数值**。
        """
        if self.HEAD_BOX is None:
            return (HEAD_OFFSET, HEAD_HALF, BODY_OFFSET, BODY_HALF)
        return (self.HEAD_BOX, self.HEAD_BOX_HALF,
                self.BODY_BOX, self.BODY_BOX_HALF)

    def head_world(self):
        """头盒中心（世界坐标）。走 hitboxes()，Boss 覆写盒子后自动跟随。"""
        ho, _hh, _bo, _bh = self.hitboxes()
        return self.base_pos() + ho

    # ------------------------------------------------------------------
    # 伤害
    # ------------------------------------------------------------------
    def take_damage(self, amount, headshot=False):
        """返回是否击杀。Sprint 2 反馈数据：last_hit_part / hit_flash_at。"""
        if self.dead:
            return False
        self.hp -= amount
        self.last_hit_part = "head" if headshot else "body"
        self.hit_flash_at = self.app.time
        self._hit_flash_t = 0.08
        if self.hp <= 0.0:
            self._die()
            return True
        return False

    def _die(self):
        self.dead = True
        self.state = "DEAD"
        self._death_t = 0.0
        # 尸体不再参与碰撞/推挤
        if self.body is not None:
            self.app.physics.unregister(self.body)
            self.body = None
        # 死亡掉落（需求 G19：普通 1 件 50/50，精英 2 件各一）
        if self.app.combat is not None:
            self.app.combat.on_bot_death(self)

    # ------------------------------------------------------------------
    # 每帧
    # ------------------------------------------------------------------
    def update(self, dt):
        if self.dead:
            self._update_death(dt)
            self._sync_parts(dt)
            return

        self._state_t += dt
        _p, p_pos, player_eye, base, dist, los = self._sense()

        if self.state == "SPAWN":
            self._stop(dt)
            # 入场：躯干 unlit 闪烁 2 次（0.2s 一格）
            torso = self.parts.get("torso")
            if torso is not None:
                torso[0].renderer.unlit = (self._state_t % 0.4) < 0.2
            if self._state_t >= 0.8:
                if torso is not None:
                    torso[0].renderer.unlit = False
                self._set_state("APPROACH")

        elif self.state == "APPROACH":
            self._move_towards(p_pos.x, p_pos.z, self.MOVE_SPEED, dt,
                               face_player=False)
            if dist <= self.ENGAGE_MAX_ and los:
                self._set_state("ENGAGE")

        elif self.state == "ENGAGE":
            # Boss 私有行为的钩子（`_boss_tick`）。基类是**空实现**，
            # 所以人形 bot / 暴风兵的行为逐字节不变；`NaiLongBoss`
            # 覆写它来插入「刺刀冲击」（RUSH_TELEGRAPH→RUSH→RUSH_STUN）。
            # 返回 True = 本帧已交给 Boss 私有状态，ENGAGE 逻辑整段跳过。
            if not self._boss_tick(dt, dist, player_eye, los):
                self._engage_move(base, p_pos, dist, dt)
                self._face(player_eye, dt)
                if los:
                    self._los_lost_t = 0.0
                    self._update_fire(dist, player_eye, dt)
                else:
                    self._los_lost_t += dt
                    self._stop(dt)
                    if self._los_lost_t > LOSE_LOS_TIME:
                        self._begin_reposition()
                if (not self._repositioned_low_hp
                        and self.wave_index >= 3
                        and self.hp < self.max_hp * 0.3):
                    self._repositioned_low_hp = True
                    self._begin_reposition()

        elif self.state == "REPOSITION":
            if los:
                self._set_state("ENGAGE")
            elif self._nav_target is not None:
                tx, tz = self._nav_target
                d = math.hypot(tx - base.x, tz - base.z)
                if d < 1.0:
                    self._set_state("ENGAGE")
                else:
                    self._move_towards(tx, tz, self.MOVE_SPEED, dt,
                                       face_player=False)
            else:
                self._set_state("APPROACH")

        self._sync_parts(dt)

    # ------------------------------------------------------------------
    # 移动与避障
    # ------------------------------------------------------------------
    def _sense(self):
        """每帧感知：返回 (player, 玩家位置, 玩家眼位, 自己的脚底, 距离, LOS)。

        抽出来是因为 `NaiLongBoss` 的冲刺子状态机也要用同一份感知结果，
        复制一遍六行常量会随参数调整而失同步。
        """
        player = self.app.player
        p_pos = player.transform.position
        player_eye = p_pos + Vec3(0.0, player.eye_height, 0.0)
        head = self.head_world()
        dist = (player_eye - head).length()
        los = self.app.combat.has_los(head, player_eye)
        return player, p_pos, player_eye, self.base_pos(), dist, los

    def _boss_tick(self, dt, dist, player_eye, los):
        """Boss 私有行为钩子（ENGAGE 内）。基类空实现 → 什么都不做。

        存在的意义是**不给基类加状态**：人形 bot 走原来的 ENGAGE 分支，
        数值与时序零改动；只有覆写了本方法的 `NaiLongBoss` 会多出
        「刺刀冲击」这一套状态。
        """
        return False

    def _set_state(self, s):
        self.state = s
        self._state_t = 0.0
        if s == "ENGAGE":
            self._aim_t = 0.0
            self._burst_left = 0
            self._burst_gap_t = 0.0
            self._los_lost_t = 0.0

    def _stop(self, dt):
        if self.body is not None:
            vy = self.body.velocity.y
            self.body.velocity = Vec3(0.0, vy, 0.0)

    def _move_towards(self, tx, tz, speed, dt, face_player):
        base = self.base_pos()
        d = Vec3(tx - base.x, 0.0, tz - base.z)
        if d.length_sq() < 1e-8:
            self._stop(dt)
            return
        d = d.normalized()
        d = self._avoid(d)
        vy = self.body.velocity.y
        self.body.velocity = Vec3(d.x * speed, vy, d.z * speed)
        self._walk_phase += speed * 3.2 * dt
        if not face_player:
            des = math.atan2(-d.x, -d.z)
            k = 1.0 - math.exp(-8.0 * dt)
            self.yaw += wrap_angle(des - self.yaw) * k

    def _avoid(self, direction):
        """须线避障（GDD §4.2）：前方 ±35°、长 3m 两根 raycast，命中静态体则绕行。

        raycast_world 返回 (t, body)（ENG-004 需求 C6），此处只关心
        是否命中，解包后判 t。
        """
        origin = self.base_pos() + Vec3(0.0, 0.9, 0.0)
        yaw = math.atan2(-direction.x, -direction.z)
        hit_l, _b = raycast_world(self.app.physics, origin,
                                  dir_from_yaw_pitch(yaw + math.radians(35), 0),
                                  3.0, include_dynamic=True)
        hit_r, _b = raycast_world(self.app.physics, origin,
                                  dir_from_yaw_pitch(yaw - math.radians(35), 0),
                                  3.0, include_dynamic=True)
        if hit_l is None and hit_r is None:
            return direction
        if hit_l is not None and hit_r is not None:
            steer = 90.0            # 两侧都堵：右转 90°
        elif hit_l is not None:
            steer = -55.0           # 左有障碍 → 向右绕
        else:
            steer = 55.0
        steer_yaw = yaw + math.radians(steer)
        return dir_from_yaw_pitch(steer_yaw, 0.0)

    def _face(self, target, dt):
        des, _ = yaw_pitch_from_dir(target - self.head_world())
        k = 1.0 - math.exp(-8.0 * dt)
        self.yaw += wrap_angle(des - self.yaw) * k

    def _engage_move(self, base, p_pos, dist, dt):
        """8~14m 巡航 + 侧向游走（2~3s 随机换向）。"""
        flat = Vec3(p_pos.x - base.x, 0.0, p_pos.z - base.z)
        if flat.length_sq() < 1e-8:
            self._stop(dt)
            return
        toward = flat.normalized()
        radial = 0.0
        if dist < self.ENGAGE_MIN_:
            radial = -self.RETREAT_SPEED_
        elif dist > self.ENGAGE_MAX_:
            radial = self.MOVE_SPEED
        self._strafe_t -= dt
        if self._strafe_t <= 0.0:
            self._strafe_dir = random.choice((-1.0, 1.0))
            self._strafe_t = random.uniform(2.0, 3.0)
        side = Vec3(-toward.z, 0.0, toward.x) * (self._strafe_dir
                                                 * self.STRAFE_SPEED_)
        wish = toward * radial + side
        if wish.length_sq() > 1e-8:
            wish = self._avoid(wish.normalized()) * min(
                wish.length(), self.MOVE_SPEED)
            vy = self.body.velocity.y
            self.body.velocity = Vec3(wish.x, vy, wish.z)
            self._walk_phase += (min(wish.length(), self.MOVE_SPEED)
                                 * 3.2 * dt)
        else:
            self._stop(dt)

    # ------------------------------------------------------------------
    # 射击
    # ------------------------------------------------------------------
    def _update_fire(self, dist, player_eye, dt):
        if self._aim_t < self.AIM_TIME_:
            self._aim_t += dt
            return
        if self._burst_gap_t > 0.0:
            self._burst_gap_t -= dt
            return
        if self._burst_left <= 0:
            self._burst_left = self.BURST_SHOTS_
            self._burst_first = True
            self._shot_t = 0.0
        self._shot_t -= dt
        if self._shot_t <= 0.0:
            self._fire_one(dist, player_eye, self._burst_first)
            self._burst_first = False
            self._burst_left -= 1
            self._shot_t = self.BURST_INTERVAL_
            if self._burst_left <= 0:
                self._burst_gap_t = random.uniform(*self.BURST_GAP)

    def _fire_one(self, dist, player_eye, warning):
        """单发 hitscan（GDD §4.3 精准度模型；首發警告弹强制偏移 ≥0.7·err）。"""
        player = self.app.player
        err = self.err_base * (1.0 + dist / 40.0)
        h_speed = math.hypot(player.body.velocity.x, player.body.velocity.z)
        if h_speed > 6.0:
            err += 1.2
        err_rad = math.radians(err)
        if warning:
            theta = math.radians(err * random.uniform(0.7, 1.0))
        else:
            theta = err_rad * math.sqrt(random.random())
        phi = random.uniform(0.0, math.tau)
        fwd = (player_eye - self._muzzle).normalized()
        right = fwd.cross(Vec3(0.0, 1.0, 0.0))
        if right.length_sq() < 1e-8:
            right = Vec3(1.0, 0.0, 0.0)
        right = right.normalized()
        up = right.cross(fwd).normalized()
        direction = (fwd * math.cos(theta)
                     + right * (math.sin(theta) * math.cos(phi))
                     + up * (math.sin(theta) * math.sin(phi))).normalized()
        if self.app.combat.bot_shoot_hits_player(self._muzzle, direction):
            self.app.combat.damage_player(self.damage)
        self._flash_t = 0.06

    # ------------------------------------------------------------------
    # 死亡侧倒
    # ------------------------------------------------------------------
    def _update_death(self, dt):
        self._death_t += dt

    def _begin_reposition(self):
        self._nav_target = random.choice(NAV_POINTS)
        self._set_state("REPOSITION")

    def _sync_parts(self, dt):
        base = self.base_pos()
        roll = 0.0
        squash = 1.0
        if self.dead:
            p = min(1.0, self._death_t / 0.25)
            roll = p * math.radians(90.0)
            squash = 1.0 - 0.85 * p
        leg_swing = math.radians(25.0) * math.sin(self._walk_phase)
        arm_swing = math.radians(8.0) * math.sin(self._walk_phase)
        if self._hit_flash_t > 0.0:
            self._hit_flash_t -= dt
            brighten = 1.25
        else:
            brighten = 1.0
        if self._flash_t > 0.0:
            self._flash_t -= dt
            gun_color = _FLASH_COLOR
        else:
            gun_color = _GUN_COLOR

        for key, (e, off, color) in self.parts.items():
            o = off
            if self.dead:
                cr, sr = math.cos(roll), math.sin(roll)
                o = Vec3(off.x * cr - off.y * sr,
                         (off.x * sr + off.y * cr) * squash,
                         off.z)
            e.transform.position = base + _rot_local(o, self.yaw)
            e.transform.yaw = self.yaw
            e.transform.roll = roll
            if key == "leg_l":
                e.transform.pitch = leg_swing
            elif key == "leg_r":
                e.transform.pitch = -leg_swing
            elif key == "arm_l":
                e.transform.pitch = -arm_swing
            elif key == "arm_r":
                e.transform.pitch = arm_swing
            else:
                e.transform.pitch = 0.0

            if key == "gun":
                e.renderer.color = gun_color
                m = _rot_local(Vec3(off.x, off.y, off.z + 0.45), self.yaw)
                self._muzzle = base + m
            elif key in ("head", "torso"):
                e.renderer.color = tuple(min(1.0, c * brighten)
                                         for c in color)


class HumanoidBot(BaseBot):
    """人形 bot（普通兵 / 精英兵 / 暴风兵 Stormtrooper 的共同外观基类）。"""

    HEAD_BOX = None            # -> 用 combat 模块常量
    HEAD_BOX_HALF = None
    BODY_BOX = None
    BODY_BOX_HALF = None
    BODY_HALF_Y_LOCAL = BODY_HALF_Y

    def __init__(self, scene, physics, app, pos_xz, hp, elite,
                 wave_damage, wave_err, wave_index):
        torso_color = _TORSO_ELITE if elite else _TORSO_NORMAL
        self.SPEC = (
            # key,    size,               offset,               color,       shin,  spec, shadow
            ("head",  (0.34, 0.32, 0.34), (0.0, 1.66, 0.0),     _SKIN,       24.0, 0.20, True),
            ("torso", (0.60, 0.70, 0.34), (0.0, 1.15, 0.0),     torso_color, 20.0, 0.15, True),
            ("leg_l", (0.22, 0.80, 0.26), (-0.14, 0.40, 0.0),   _LEG_COLOR,  20.0, 0.15, False),
            ("leg_r", (0.22, 0.80, 0.26), (0.14, 0.40, 0.0),    _LEG_COLOR,  20.0, 0.15, False),
            ("arm_l", (0.16, 0.58, 0.20), (-0.38, 1.18, 0.06),  torso_color, 20.0, 0.15, False),
            ("arm_r", (0.16, 0.58, 0.20), (0.38, 1.18, 0.06),   torso_color, 20.0, 0.15, False),
            ("gun",   (0.11, 0.15, 0.80), (0.30, 1.22, 0.42),   _GUN_COLOR,  72.0, 0.70, False),
        )
        super().__init__(scene, physics, app, pos_xz, hp, elite,
                         wave_damage, wave_err, wave_index)
