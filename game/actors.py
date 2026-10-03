# -*- coding: utf-8 -*-
"""活动道具：追踪炮台 / 炮弹 / 漂浮眼球。

追踪算法（炮台与眼球共用思想）——指数阻尼收敛，帧率无关：
    yaw += wrap_angle(desired - yaw) * (1 - exp(-k * dt))
k 越大咬合越快；比线性 lerp(yaw, desired, k*dt) 更稳定。
"""
import math

from engine.scene import Entity
from engine.physics import PhysicsBody
from engine.mesh import box_mesh, sphere_mesh, MeshRenderer
from engine.math_utils import (
    Vec3, wrap_angle, yaw_pitch_from_dir, dir_from_yaw_pitch,
)


class Projectile(Entity):
    """炮弹：自发光小球 + 球体碰撞，命中反弹、寿命到期自动消亡。

    gravity_scale=0.35 让弹道带一点抛物线（重力有效值 18*0.35≈6.3）。
    """

    def __init__(self, scene, physics, pos, direction, speed=13.0):
        super().__init__("shell", "projectile")
        self.scene = scene
        self.transform.position = pos.clone()
        self.renderer = MeshRenderer(sphere_mesh(0.16, 10, 14),
                                     color=(1.0, 0.75, 0.35),
                                     cast_shadow=False, unlit=True)
        self.body = PhysicsBody(self, "sphere", radius=0.16, mass=0.6,
                                restitution=0.35, gravity_scale=0.35)
        self.body.velocity = direction.normalized() * speed
        self.life = 5.0
        physics.register(self.body)
        scene.add(self)

    def update(self, dt):
        self.life -= dt
        if self.life <= 0.0:
            self.scene.remove(self)      # 引擎帧末统一回收实体与碰撞体


class Turret(Entity):
    """追踪炮台：底座（静态碰撞）+ 炮头 + 炮管，平滑追踪玩家并周期开火。

    底座 / 炮头 / 炮管没有父子层级（引擎为 demo 规模刻意不做），
    由本实体每帧手动同步子部件变换——这正是模块注释「组合对象由游戏逻辑
    直接同步子部件」的用例。
    """

    TRACK_SPEED = 3.2          # 朝向收敛速率（rad/s 量级的阻尼系数）
    FIRE_RANGE = 26.0
    FIRE_ARC = 0.35            # 允许开火的最大瞄准误差（弧度）
    FIRE_INTERVAL = 1.9
    SHELL_SPEED = 13.0

    def __init__(self, scene, physics, player, base_pos):
        """base_pos：底座摆放点（平台顶面中心）。"""
        super().__init__("turret", "turret")
        self.scene = scene
        self.physics = physics
        self.player = player
        base_center = Vec3(base_pos.x, base_pos.y + 0.35, base_pos.z)
        head_y = base_center.y + 0.35 + 0.31           # 底座顶 + 炮头半高

        # 底座：静态碰撞体，玩家/炮弹都能撞上
        base = Entity("turret_base", "turret")
        base.transform.position = base_center.clone()
        base.renderer = MeshRenderer(box_mesh(1.4, 0.7, 1.4),
                                     color=(0.55, 0.54, 0.52),
                                     shininess=24.0, spec_strength=0.30)
        base.body = PhysicsBody(base, "aabb", half=Vec3(0.7, 0.35, 0.7),
                                is_static=True)
        physics.register(base.body)
        scene.add(base)

        # 炮头 / 炮管：纯视觉，本实体每帧同步
        self.head = Entity("turret_head", "turret")
        self.head.renderer = MeshRenderer(box_mesh(0.92, 0.62, 0.92),
                                          color=(0.80, 0.78, 0.75),
                                          shininess=48.0, spec_strength=0.50)
        scene.add(self.head)

        self._barrel_color = (0.40, 0.42, 0.46)
        self.barrel = Entity("turret_barrel", "turret")
        self.barrel.renderer = MeshRenderer(box_mesh(0.20, 0.20, 1.05),
                                            color=self._barrel_color,
                                            shininess=72.0, spec_strength=0.70)
        scene.add(self.barrel)

        # 本实体逻辑锚点 = 炮头枢轴
        self.transform.position = Vec3(base_pos.x, head_y, base_pos.z)
        self.yaw = 0.0
        self.pitch = 0.0
        self.cooldown = 1.2
        self.flash = 0.0
        self.shots = 0
        scene.add(self)

    # ------------------------------------------------------------------
    def update(self, dt):
        hp = self.transform.position
        target = self.player.transform.position + \
            Vec3(0.0, self.player.eye_height, 0.0)
        d = target - hp
        dist = d.length()

        des_yaw, des_pitch = yaw_pitch_from_dir(d)
        k = 1.0 - math.exp(-self.TRACK_SPEED * dt)
        self.yaw += wrap_angle(des_yaw - self.yaw) * k
        self.pitch += (des_pitch - self.pitch) * k
        self.pitch = max(-0.6, min(0.9, self.pitch))

        fwd = dir_from_yaw_pitch(self.yaw, self.pitch)
        self.head.transform.position = hp.clone()
        self.head.transform.yaw = self.yaw
        self.head.transform.pitch = self.pitch
        self.barrel.transform.position = hp + fwd * 0.72
        self.barrel.transform.yaw = self.yaw
        self.barrel.transform.pitch = self.pitch

        # 炮口火光：短暂提亮炮管
        if self.flash > 0.0:
            self.flash -= dt
            self.barrel.renderer.color = (1.0, 0.82, 0.45)
        else:
            self.barrel.renderer.color = self._barrel_color

        self.cooldown -= dt
        if (self.cooldown <= 0.0 and dist < self.FIRE_RANGE
                and abs(wrap_angle(des_yaw - self.yaw)) < self.FIRE_ARC):
            self._fire(fwd)
            self.cooldown = self.FIRE_INTERVAL

    def _fire(self, fwd):
        muzzle = self.transform.position + fwd * 1.45
        Projectile(self.scene, self.physics, muzzle, fwd, self.SHELL_SPEED)
        self.shots += 1
        self.flash = 0.09


class Eye(Entity):
    """漂浮眼球：悬浮巡航 + 瞳孔始终凝视玩家，可被玩家推动（球体动力学）。

    - 悬浮：gravity_scale=0 的球体动态体，直接速度控制飞向巡航锚点；
      玩家撞上会把它推离轨道，速度控制又会把它拉回来 —— 免费的物理演示。
    - 瞳孔：无碰撞体的纯视觉小球，贴在眼球表面朝向玩家的一侧。
    """

    ORBIT_R = 2.6
    MAX_STEER = 2.2

    def __init__(self, scene, physics, player, anchor):
        super().__init__("eye", "eye")
        self.scene = scene
        self.player = player
        self.anchor = anchor.clone()
        self.t = 1.7                                    # 巡航相位
        self.transform.position = anchor.clone()

        self.renderer = MeshRenderer(sphere_mesh(0.6, 24, 32),
                                     color=(0.97, 0.97, 1.00),
                                     shininess=96.0, spec_strength=0.9)
        self.body = PhysicsBody(self, "sphere", radius=0.6, mass=2.5,
                                restitution=0.5, gravity_scale=0.0)
        physics.register(self.body)

        self.pupil = Entity("eye_pupil", "eye")
        self.pupil.transform.position = anchor.clone()
        self.pupil.renderer = MeshRenderer(sphere_mesh(0.24, 14, 18),
                                           color=(0.15, 0.18, 0.28),
                                           shininess=160.0, spec_strength=1.2,
                                           cast_shadow=False)
        scene.add(self.pupil)
        scene.add(self)

    def update(self, dt):
        self.t += dt
        a = self.anchor
        target = Vec3(a.x + math.cos(self.t * 0.55) * self.ORBIT_R,
                      a.y + math.sin(self.t * 1.6) * 0.45,
                      a.z + math.sin(self.t * 0.55) * self.ORBIT_R)
        steer = (target - self.transform.position) * 1.4
        if steer.length() > self.MAX_STEER:
            steer = steer * (self.MAX_STEER / steer.length())
        self.body.velocity = steer

        # 瞳孔凝视玩家（本引擎网格以局部原点为中心，直接位移即可）
        look = (self.player.transform.position
                + Vec3(0.0, self.player.eye_height, 0.0)) \
            - self.transform.position
        if look.length_sq() > 1e-6:
            self.pupil.transform.position = \
                self.transform.position + look.normalized() * 0.40
