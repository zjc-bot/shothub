# -*- coding: utf-8 -*-
"""玩家：第一人称移动 + 姿态系统（下蹲/趴下）+ 重力 + 跳跃 + 疾跑。

姿态（ENG-007 模块一）
---------------------
三态：STAND / CROUCH / PRONE。碰撞体半高、视点高度、移速同步变化：

    姿态    碰撞体半高   眼高偏移   移速系数   总高    眼高
    STAND     0.90       0.72      1.00      1.80m   1.62m
    CROUCH    0.55       0.37      0.52      1.10m   0.92m
    PRONE     0.26       0.08      0.20      0.52m   0.34m

关键设计：
- **平滑过渡不瞬移**：碰撞体半高与眼高都走 `k = 1-exp(-rate·dt)` 指数逼近，
  摄像机因此平滑升降（需求明确要求"不要瞬移"）。物理上碰撞体半高是可变
  字段（`PhysicsBody.half`），逐帧微调不会破坏固定步求解。
- **趴下更稳**：PRONE 时武器晃动的 bob 振幅与 sway 上限都压到 0.25 倍，
  且禁用疾跑 —— 提供"更高的瞄准稳定性"（需求）。
- **起身受阻判定**：趴/蹲状态下若头顶被遮挡（矮墙/箱堆/桥）则**保持原
  姿态**，不会把玩家卡进几何体里。`_can_stand()` 用引擎 ray_aabb 向上探。
- **空中不切姿态**：滞空时拒绝切换，避免半空变矮导致穿地/卡顶。

其余沿用原实现要点：
- 玩家是普通 Entity：AABB 碰撞体（半宽 0.31），质量远大于木箱（70:3），
  碰撞求解按质量反比分摊位移 → 玩家推箱轻而易举。
- 重力由 engine.physics 统一积分（g=18），这里只设置水平期望速度：
  着地直接赋值（响应干脆），空中指数逼近（保留操控感）。
- 相机（yaw）驱动移动方向；eye_height 为身体中心到眼睛的偏移。
"""
import math

import pygame

from engine.scene import Entity
from engine.physics import PhysicsBody, aabb_vs_aabb
from engine.mesh import box_mesh, MeshRenderer
from engine.math_utils import Vec3, dir_from_yaw_pitch

# 出生点（世界中心偏南，面向 -Z 可直视木箱区）
SPAWN = Vec3(0.0, 1.6, 8.0)

# ---- 姿态常量 -------------------------------------------------------
POSTURE_STAND = "STAND"
POSTURE_CROUCH = "CROUCH"
POSTURE_PRONE = "PRONE"
POSTURES = (POSTURE_STAND, POSTURE_CROUCH, POSTURE_PRONE)

# 姿态参数表：半高 / 眼高偏移 / 移速系数（ENG-007 可调参数集中在此）
POSTURE_HALF_Y = {POSTURE_STAND: 0.90, POSTURE_CROUCH: 0.55,
                  POSTURE_PRONE: 0.26}
POSTURE_EYE = {POSTURE_STAND: 0.72, POSTURE_CROUCH: 0.37,
               POSTURE_PRONE: 0.08}
POSTURE_SPEED = {POSTURE_STAND: 1.00, POSTURE_CROUCH: 0.52,
                 POSTURE_PRONE: 0.20}

POSTURE_BLEND_RATE = 11.0        # 姿态过渡指数系数（越大越快，1/秒量纲）
POSTURE_MAX_BLEND_PER_FRAME = 1.8  # 半高变化速率上限（米/秒），防大 dt 跳变
STAND_CLEARANCE = 0.06           # 起身时头顶留出的安全间隙（米）
PRONE_STABILITY = 0.25           # 趴下时武器晃动衰减到 25%


class Player(Entity):
    def __init__(self):
        super().__init__("player", "player")
        self.transform.position = SPAWN.clone()
        self.eye_height = 0.72          # 身体中心 → 眼睛（眼高 1.62m）
        self.walk_speed = 5.0
        self.run_speed = 8.2
        self.jump_speed = 7.6           # g=18 时跳高约 1.6m，可上 1m 台阶
        self.air_control = 0.30         # 空中操控系数 0~1

        # 可见身体（跟随视角 / 其他实体可见；不投射阴影：眼睛在体内会污染阴影贴图）
        self.renderer = MeshRenderer(
            box_mesh(0.62, 1.8, 0.62),
            color=(0.98, 0.66, 0.30),
            cast_shadow=False, shininess=24.0, spec_strength=0.25)

        self.body = PhysicsBody(self, "aabb", half=Vec3(0.31, 0.9, 0.31),
                                mass=70.0, friction=0.0)

        # 战斗属性（挑战模式由 CombatManager 读写；观光模式闲置）
        self.hp = 100.0
        self.max_hp = 100.0
        self.armor = 55.0             # 防护（需求 C8：击退/升级用）
        self.damage_reduction = 0.0   # 减伤比例（需求 F 升级 [3]，上限 0.40）

        # ---- 姿态状态（ENG-007）----
        self.posture = POSTURE_STAND
        self._target_posture = POSTURE_STAND
        # 物理世界引用：起身受阻判定要遍历静态体（world.build_world 注入）。
        # 为 None 时 _can_reach 退化为"总是可起身"，即姿态永远不会被卡住。
        self._physics = None
        # 姿态切换失败（头顶被挡）时置 True，供 selftest / HUD 查询
        self.posture_blocked = False

    # ------------------------------------------------------------------
    # 姿态
    # ------------------------------------------------------------------
    def set_posture(self, posture):
        """请求切换姿态。返回是否受理（空中或受阻会拒绝）。

        受理后不立即改碰撞体——由 update() 每帧插值，保证摄像机平滑。
        """
        if posture not in POSTURES or posture == self._target_posture:
            return False
        if self.body is not None and not self.body.grounded:
            return False            # 滞空禁止切姿态（防穿地/卡顶）
        if not self._can_reach(posture, self._physics):
            self.posture_blocked = True
            return False
        self.posture_blocked = False
        self._target_posture = posture
        return True

    def toggle_posture(self, posture):
        """切到指定姿态；已处于该姿态则回 STAND（供按键切换用）。"""
        if self._target_posture == posture:
            return self.set_posture(POSTURE_STAND)
        return self.set_posture(posture)

    def _can_reach(self, posture, physics=None):
        """目标姿态是否可达：变高时**新增占据的体积**不能与静态体重叠。

        判据不是"头顶往上有没有东西"，而是"目标姿态相对当前姿态多出来的
        那截身体会不会插进静态体"。做法：把新增体积写成盒（底 = 当前头顶，
        顶 = 目标头顶），逐个静态体做 AABB 相交测试。矩形相交用
        `aabb_vs_aabb` 即可，无需射线（射线的 hitscan 语义会让"起点在盒内"
        恒真，误判受阻）。
        """
        target_half = POSTURE_HALF_Y[posture]
        gain = target_half - self.body.half.y
        if gain <= 1e-6:
            return True             # 变矮永远可行
        if self.body is None or not self.body.grounded or physics is None:
            return True
        p = self.transform.position
        feet = p.y - self.body.half.y      # 脚底（站地面时为 0）
        # 探针体：**脚底 → 目标姿态头顶 + 安全间隙**，横向略窄于碰撞体。
        # lo 抬 1mm 避免与地面碰撞板（顶面 y=0）贴面即判重叠。
        lo = feet + 0.001
        hi = feet + target_half * 2.0 + STAND_CLEARANCE
        new_center = Vec3(p.x, (lo + hi) * 0.5, p.z)
        new_half = Vec3(self.body.half.x * 0.92, (hi - lo) * 0.5,
                        self.body.half.z * 0.92)
        for b in physics.bodies:
            if b.entity is self or not b.is_static:
                continue           # 排除自身与动态体（会被碰撞解算推开）
            if b.kind != "aabb":
                continue
            # 只看**探针新增的那截**（当前头顶以上）：若把整具身体都算进去，
            # 玩家本来就贴着掩体站立时，脚边那截重叠会让判定永远为"受阻"，
            # 哪怕掩体在身后、与起身无关。
            top = p.y + self.body.half.y
            if b.center.y + b.half.y <= top + 1e-6:
                continue           # 该静态体完全在当前头顶以下 → 无关
            if aabb_vs_aabb(new_center, new_half, b.center, b.half
                            ) is not None:
                return False
        return True

    def _update_posture(self, dt):
        """把碰撞体半高 / 眼高按指数逼近目标姿态（不瞬移）。"""
        tgt = self._target_posture
        t_half = POSTURE_HALF_Y[tgt]
        t_eye = POSTURE_EYE[tgt]
        # 限速：单帧变化不超过 MAX_BLEND_PER_FRAME，避免大姿态差（0.90→0.26）
        # 在大 dt（掉帧/切后台回来）下一步跳完，视觉上就是"瞬移"。
        step = (t_half - self.body.half.y)
        max_step = POSTURE_MAX_BLEND_PER_FRAME * dt
        if step > max_step:
            step = max_step
        elif step < -max_step:
            step = -max_step
        b = self.body
        b.half.y += step
        k = 1.0 - math.exp(-POSTURE_BLEND_RATE * dt)
        self.eye_height += (t_eye - self.eye_height) * k
        # 姿态名滞后于实际半高：全部到位才更新（供逻辑判定用）。
        # 用比例判据而非绝对 eps——0.002 对半高 0.26 的趴姿过宽，
        # 会让姿态在过渡早期就被判定为"已到位"。
        if abs(b.half.y - t_half) < max(0.002, t_half * 0.02) and \
                abs(self.eye_height - t_eye) < max(0.004, t_eye * 0.02):
            self.posture = tgt

    def speed_factor(self):
        """当前姿态的移速系数（供 weapon 扩散 / AI 判定复用）。"""
        return POSTURE_SPEED[self._target_posture]

    def stability(self):
        """瞄准稳定度：趴下最低（武器晃动衰减倍率）。"""
        return PRONE_STABILITY if self._target_posture == POSTURE_PRONE \
            else 1.0

    def can_sprint(self):
        """只有站立可疾跑（下蹲/趴下强制走）。"""
        return self._target_posture == POSTURE_STAND

    # ------------------------------------------------------------------
    def control(self, inp, camera_rig, dt):
        """每帧根据输入与相机朝向设置速度（引擎随后跑固定步物理）。"""
        self._update_posture(dt)
        b = self.body
        fwd = dir_from_yaw_pitch(camera_rig.yaw, 0.0)      # 水平前方
        right = fwd.cross(Vec3(0.0, 1.0, 0.0)).normalized()

        wish = Vec3()
        if inp.is_down(pygame.K_w):
            wish = wish + fwd
        if inp.is_down(pygame.K_s):
            wish = wish - fwd
        if inp.is_down(pygame.K_d):
            wish = wish + right
        if inp.is_down(pygame.K_a):
            wish = wish - right

        sprint = (inp.is_down(pygame.K_LSHIFT)
                  or inp.is_down(pygame.K_RSHIFT)) and self.can_sprint()
        base = self.run_speed if sprint else self.walk_speed
        speed = base * self.speed_factor()
        if wish.length_sq() > 1e-9:
            wish = wish.normalized() * speed

        if b.grounded:
            # 着地：直接给定期望水平速度（急停急走），保留竖直分量
            vy = b.velocity.y
            # 姿态非站立时禁止起跳（趴着跳不成立，且会穿低掩体）
            if inp.is_down(pygame.K_SPACE) and self.can_sprint():
                vy = self.jump_speed
            b.velocity = Vec3(wish.x, vy, wish.z)
        else:
            # 空中：水平速度向期望值指数逼近，保留惯性跳跃弧线
            k = min(1.0, self.air_control * dt * 10.0)
            vx = b.velocity.x + (wish.x - b.velocity.x) * k
            vz = b.velocity.z + (wish.z - b.velocity.z) * k
            b.velocity = Vec3(vx, b.velocity.y, vz)

    def respawn(self):
        self.transform.position = SPAWN.clone()
        self.body.velocity = Vec3()
        self.posture = POSTURE_STAND
        self._target_posture = POSTURE_STAND
        self.body.half.y = POSTURE_HALF_Y[POSTURE_STAND]
        self.eye_height = POSTURE_EYE[POSTURE_STAND]
        self.posture_blocked = False
