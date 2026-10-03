# -*- coding: utf-8 -*-
"""世界构建：地形、围墙、平台、可推箱、弹力球、炮台、眼球、灯塔。

build_world(app, mode) 同时服务两个时机：
- 首次启动（App.on_start）
- R 键重置（App.reset_world）——完全重建，保证可重复游玩

模式（GDD fps-wave-mode §0）：
- "explore"   观光模式：现有 demo 原样（炮台/眼球/调试快捷键照旧）。
- "challenge" 挑战模式：不生成炮台与眼球（避免敌我火力混淆），
  在基础世界上叠加战斗层——§6.2 掩体 + 导航点 + 刷怪点 +
  CombatManager / Weapon / WaveManager。

坐标约定：+Y 上；地面顶面 y=0；地图范围 x/z ∈ [-30, 30]。
"""
import math

from engine.scene import Entity
from engine.physics import PhysicsBody
from engine.mesh import box_mesh, plane_mesh, sphere_mesh, MeshRenderer
from engine.math_utils import Vec3
from graphics.light import SpotLight

from .player import Player
from .actors import Turret, Eye
from .combat import CombatManager
from .weapon import Rifle, Sniper
from .waves import WaveManager, NAV_POINTS
from .boss_rush import BossManager


# ----------------------------------------------------------------------
# 通用构建器
# ----------------------------------------------------------------------
def _block(scene, physics, center, size, color, *, static=True, mass=1.0,
           friction=0.0, restitution=0.0, texture=None, shininess=16.0,
           spec_strength=0.15, cast_shadow=True, unlit=False, name="block"):
    """立方体：网格 + AABB 碰撞体（静态墙/平台 或 动态可推箱）。"""
    e = Entity(name)
    e.transform.position = center.clone()
    e.renderer = MeshRenderer(box_mesh(size[0], size[1], size[2]),
                              color=color, texture=texture,
                              cast_shadow=cast_shadow, unlit=unlit,
                              shininess=shininess, spec_strength=spec_strength)
    half = Vec3(size[0] * 0.5, size[1] * 0.5, size[2] * 0.5)
    if static:
        e.body = PhysicsBody(e, "aabb", half=half, is_static=True)
    else:
        e.body = PhysicsBody(e, "aabb", half=half, mass=mass,
                             friction=friction, restitution=restitution)
    physics.register(e.body)
    scene.add(e)
    return e


def _ball(scene, physics, center, radius, color, *, mass=1.2,
          restitution=0.55, friction=0.4, name="ball"):
    """球体：网格 + 球碰撞（弹性反弹）。"""
    e = Entity(name)
    e.transform.position = center.clone()
    e.renderer = MeshRenderer(sphere_mesh(radius), color=color,
                              shininess=64.0, spec_strength=0.85)
    e.body = PhysicsBody(e, "sphere", radius=radius, mass=mass,
                         restitution=restitution, friction=friction)
    physics.register(e.body)
    scene.add(e)
    return e


# ----------------------------------------------------------------------
# 灯塔：塔身 + 旋转扫射聚光灯（实时阴影贴图）
# ----------------------------------------------------------------------
class Lighthouse(Entity):
    """方向随时间旋转的聚光灯；灯室不投影（否则灯光被自己的墙挡住）。"""

    def __init__(self, scene, physics, app, center):
        super().__init__("lighthouse", "landmark")
        x, z = center.x, center.z
        stone_a = (0.95, 0.92, 0.88)
        stone_b = (0.90, 0.42, 0.38)

        _block(scene, physics, Vec3(x, 1.70, z), (2.4, 3.4, 2.4), stone_a,
               shininess=24.0, spec_strength=0.20, name="lighthouse_body")
        _block(scene, physics, Vec3(x, 4.10, z), (1.8, 1.4, 1.8), stone_b,
               shininess=24.0, spec_strength=0.20, name="lighthouse_mid")
        _block(scene, physics, Vec3(x, 5.30, z), (1.1, 0.95, 1.1),
               (0.72, 0.86, 0.94), cast_shadow=False, shininess=90.0,
               spec_strength=1.0, name="lighthouse_glass")
        _block(scene, physics, Vec3(x, 5.95, z), (1.4, 0.30, 1.4),
               (0.85, 0.38, 0.35), name="lighthouse_roof")

        # 灯室内自发光灯芯
        glow = Entity("lighthouse_glow", "landmark")
        glow.transform.position = Vec3(x, 5.30, z)
        glow.renderer = MeshRenderer(box_mesh(0.5, 0.5, 0.5),
                                     color=(1.0, 0.92, 0.62),
                                     cast_shadow=False, unlit=True)
        scene.add(glow)

        self.light_pos = Vec3(x, 5.30, z)
        self.light = SpotLight(
            position=self.light_pos, direction=Vec3(1.0, -0.38, 0.0),
            color=(1.0, 0.88, 0.60),
            inner_angle_deg=9.0, outer_angle_deg=16.0,
            intensity=5.0, attenuation=0.02,
            cast_shadow=True, shadow_res=1024, shadow_far=55.0)
        app.spot_lights.append(self.light)

        self.ang = 0.0
        scene.add(self)

    def update(self, dt):
        self.ang += dt * 0.55
        self.light.direction = Vec3(math.cos(self.ang), -0.38,
                                    math.sin(self.ang)).normalized()
        self.light.position = self.light_pos


# ----------------------------------------------------------------------
# 挑战模式战斗层（GDD §6.2 掩体坐标表，全部静态 AABB，只填空白区不拆既有物）
# ----------------------------------------------------------------------
_STONE = (0.80, 0.77, 0.74)     # 暖灰石
_CREAM = (0.88, 0.84, 0.77)     # 奶油墙
_WOOD_A = (0.82, 0.58, 0.34)    # 木箱双色
_WOOD_B = (0.86, 0.63, 0.38)


def _build_combat_layer(app):
    scene, physics = app.scene, app.physics

    # 矮墙 A~D（高 1.1：站后面只露头）
    _block(scene, physics, Vec3(-15, 0.55, -12), (5.0, 1.1, 0.6), _STONE,
           shininess=20.0, spec_strength=0.18, name="cover_low_a")
    _block(scene, physics, Vec3(15, 0.55, -12), (5.0, 1.1, 0.6), _STONE,
           shininess=20.0, spec_strength=0.18, name="cover_low_b")
    _block(scene, physics, Vec3(-19, 0.55, 12), (4.0, 1.1, 0.6), _STONE,
           shininess=20.0, spec_strength=0.18, name="cover_low_c")
    _block(scene, physics, Vec3(19, 0.55, 10), (4.0, 1.1, 0.6), _STONE,
           shininess=20.0, spec_strength=0.18, name="cover_low_d")

    # L 型掩体 ×2（高 1.4：全身遮挡，绕柱战用）
    _block(scene, physics, Vec3(0, 0.70, -16), (4.0, 1.4, 0.6), _CREAM,
           name="cover_l1a")
    _block(scene, physics, Vec3(1.7, 0.70, -17.2), (0.6, 1.4, 3.0), _CREAM,
           name="cover_l1b")
    _block(scene, physics, Vec3(0, 0.70, 16), (4.0, 1.4, 0.6), _CREAM,
           name="cover_l2a")
    _block(scene, physics, Vec3(-1.7, 0.70, 17.2), (0.6, 1.4, 3.0), _CREAM,
           name="cover_l2b")

    # 箱堆 ×2（静态：战斗掩体不可推，避免 bot 推箱乱局）
    _block(scene, physics, Vec3(-8, 0.50, -14), (1, 1, 1), _WOOD_A,
           shininess=14.0, spec_strength=0.12, name="cover_stack1a")
    _block(scene, physics, Vec3(-8, 1.50, -14), (1, 1, 1), _WOOD_B,
           shininess=14.0, spec_strength=0.12, name="cover_stack1b")
    _block(scene, physics, Vec3(8, 0.50, 14), (1, 1, 1), _WOOD_A,
           shininess=14.0, spec_strength=0.12, name="cover_stack2a")
    _block(scene, physics, Vec3(8, 1.50, 14), (1, 1, 1), _WOOD_B,
           shininess=14.0, spec_strength=0.12, name="cover_stack2b")

    # 石碑（中北走廊，2.2m 全遮挡长枪线打断点）
    _block(scene, physics, Vec3(0, 1.10, -8), (2.5, 2.2, 0.8), _STONE,
           shininess=20.0, spec_strength=0.18, name="cover_monolith")

    # ---- Sprint 3 新掩体（ENG-004 需求 D9：避开 §6.3 刷怪 lane 与中央交火区） ----
    # 矮墙 E/F（北侧东西两翼，均在中央交火区外、SP5 中路两侧）
    _block(scene, physics, Vec3(-10, 0.55, -20), (4, 1.1, 0.6), _STONE,
           shininess=20.0, spec_strength=0.18, name="cover_low_e")
    _block(scene, physics, Vec3(10, 0.55, -20), (4, 1.1, 0.6), _STONE,
           shininess=20.0, spec_strength=0.18, name="cover_low_f")
    # 矮墙 G/H（东西两腰，贴边不挡四角→场心枪线）
    _block(scene, physics, Vec3(-22, 0.55, -2), (0.6, 1.1, 4), _STONE,
           shininess=20.0, spec_strength=0.18, name="cover_low_g")
    _block(scene, physics, Vec3(22, 0.55, 2), (0.6, 1.1, 4), _STONE,
           shininess=20.0, spec_strength=0.18, name="cover_low_h")
    # 断柱 ×2
    _block(scene, physics, Vec3(-18, 0.9, -18), (1.2, 1.8, 1.2), _CREAM,
           shininess=20.0, spec_strength=0.15, name="cover_stump1")
    _block(scene, physics, Vec3(20, 0.7, 18), (1.2, 1.4, 1.2), _CREAM,
           shininess=20.0, spec_strength=0.15, name="cover_stump2")
    # 方桶堆 ×2（各 3 只 0.7³，L 形叠放，木箱色，静态）
    for ox, oz in ((12.0, -14.0), (-12.0, 16.0)):
        _block(scene, physics, Vec3(ox - 0.35, 0.35, oz - 0.17),
               (0.7, 0.7, 0.7), _WOOD_A,
               shininess=14.0, spec_strength=0.12, name="barrel_a")
        _block(scene, physics, Vec3(ox + 0.35, 0.35, oz + 0.17),
               (0.7, 0.7, 0.7), _WOOD_B,
               shininess=14.0, spec_strength=0.12, name="barrel_b")
        _block(scene, physics, Vec3(ox, 1.05, oz),
               (0.7, 0.7, 0.7), _WOOD_A,
               shininess=14.0, spec_strength=0.12, name="barrel_c")

    # ---- 装饰物（需求 D10：纯视觉无碰撞，参照 lighthouse_glow 模式，
    #      全部哑光档 shininess 16~20 / spec 0.15，pastel 色板） ----
    _BUSH = (0.48, 0.78, 0.52)             # 嫩绿球灌木
    for i, (bx, bz, br) in enumerate((
            (-27.5, -12.0, 0.6), (-27.5, 10.0, 0.5), (27.5, -8.0, 0.7),
            (27.5, 12.0, 0.5), (-12.0, 27.5, 0.6), (14.0, 27.5, 0.55))):
        _deco_sphere(scene, Vec3(bx, br, bz), br, _BUSH, "bush%d" % i)
    # 花坛 ×2：低盒暖棕 + 顶面 3 小球（珊瑚红/琥珀/奶油）
    for i, (fx, fz) in enumerate(((8.0, -24.0), (-8.0, 24.0))):
        _deco_box(scene, Vec3(fx, 0.15, fz), (2.0, 0.3, 1.0),
                  (0.72, 0.51, 0.32), "flowerbed%d" % i)
        for j, (dx, col) in enumerate((
                (-0.6, (0.90, 0.42, 0.38)),
                (0.0, (1.00, 0.80, 0.40)),
                (0.6, (0.95, 0.92, 0.88)))):
            _deco_sphere(scene, Vec3(fx + dx, 0.45, fz), 0.15, col,
                         "flower%d_%d" % (i, j))
    # 石凳 ×2（暖灰）
    _deco_box(scene, Vec3(6, 0.25, 8), (1.2, 0.5, 0.5), _STONE, "bench1")
    _deco_box(scene, Vec3(-6, 0.25, -8), (1.2, 0.5, 0.5), _STONE, "bench2")
    # 旗帜杆 ×4（四角刷怪点旁 2m 偏移：奶油杆 + 珊瑚红旗面）
    for i, (px, pz) in enumerate(((-26.0, -22.0), (26.0, -22.0),
                                  (-26.0, 22.0), (26.0, 22.0))):
        _deco_box(scene, Vec3(px, 1.5, pz), (0.12, 3.0, 0.12),
                  (0.95, 0.92, 0.88), "pole%d" % i)
        _deco_box(scene, Vec3(px + 0.51, 2.6, pz), (0.9, 0.6, 0.05),
                  (0.90, 0.42, 0.38), "flag%d" % i)

    # 导航点（REPOSITION 用，8 点硬编码，无 A*）
    app.nav_points = list(NAV_POINTS)

    # 战斗三件套（需求 E14：双武器列表 + 当前武器）
    app.combat = CombatManager(app)
    rifle = Rifle(app)
    sniper = Sniper(app)
    sniper.set_visible(False)
    app.weapons = [rifle, sniper]
    app.weapon = rifle
    app.waves = WaveManager(app)
    # Boss 关管理器**两种模式都建**，但只有 boss 模式会用到。
    # 提前建好（而不是按需 lazy）是为了让 `app.bosses` 恒不为 None，
    # 避免 `on_update` / HUD 里到处写 `if self.bosses` 的空判断。
    app.bosses = BossManager(app)


def _deco_box(scene, center, size, color, name,
              shininess=16.0, spec_strength=0.15):
    """纯视觉装饰盒（无碰撞体，不投影——draw call 预算控制）。"""
    e = Entity(name, "deco")
    e.transform.position = center.clone()
    e.renderer = MeshRenderer(box_mesh(size[0], size[1], size[2]),
                              color=color, cast_shadow=False,
                              shininess=shininess, spec_strength=spec_strength)
    scene.add(e)
    return e


def _deco_sphere(scene, center, radius, color, name,
                 shininess=16.0, spec_strength=0.15):
    """纯视觉装饰球（无碰撞体）。"""
    e = Entity(name, "deco")
    e.transform.position = center.clone()
    e.renderer = MeshRenderer(sphere_mesh(radius), color=color,
                              cast_shadow=False,
                              shininess=shininess, spec_strength=spec_strength)
    scene.add(e)
    return e


# ----------------------------------------------------------------------
# 整体世界装配
# ----------------------------------------------------------------------
def build_world(app, mode="explore"):
    """构建完整可玩世界并回填 app.player / app.turret / app.eye / app.lighthouse。

    mode 三选一：
    - "explore"   观光模式（炮台/眼球/调试快捷键）
    - "challenge" 波次挑战 5 波人形 bot（TITLE [1]）
    - "boss"      奶龙 Boss 关两层（TITLE [3]），**与 challenge 共用战斗层**
    """
    scene, physics = app.scene, app.physics
    app.nav_points = []
    app.combat = None
    app.weapon = None
    app.weapons = []
    app.waves = None
    app.bosses = None

    # ---- 玩家 ----
    app.player = Player()
    app.player._physics = physics     # 姿态起身受阻判定用（ENG-007）
    physics.register(app.player.body)
    scene.add(app.player)

    # ---- 地面：视觉层（棋盘格平面 y=0）+ 隐形碰撞板（顶面 y=0）----
    # 全部世界物体均按「地面顶面 y=0」摆放，故碰撞板中心下沉 0.5，
    # 与视觉平面严格对齐，避免玩家/箱子/球悬浮。
    ground = Entity("ground", "terrain")
    ground.renderer = MeshRenderer(plane_mesh(60.0, 60.0, 30, uv_scale=0.5),
                                   color=(1.0, 1.0, 1.0), texture="checker",
                                   shininess=8.0, spec_strength=0.05,
                                   cast_shadow=False)
    scene.add(ground)

    ground_collider = Entity("ground_collider", "terrain")
    ground_collider.transform.position = Vec3(0.0, -0.5, 0.0)
    ground_collider.body = PhysicsBody(
        ground_collider, "aabb", half=Vec3(30.0, 0.5, 30.0), is_static=True)
    physics.register(ground_collider.body)
    scene.add(ground_collider)

    # ---- 四周围墙 ----
    wall = (0.88, 0.84, 0.77)
    _block(scene, physics, Vec3(0, 1.5, -30), (61, 3, 1), wall, name="wall_n")
    _block(scene, physics, Vec3(0, 1.5, 30), (61, 3, 1), wall, name="wall_s")
    _block(scene, physics, Vec3(-30, 1.5, 0), (1, 3, 61), wall, name="wall_w")
    _block(scene, physics, Vec3(30, 1.5, 0), (1, 3, 61), wall, name="wall_e")

    # ---- 跳跃平台（石阶 → 高台 → 木桥）----
    stone = (0.80, 0.77, 0.74)
    _block(scene, physics, Vec3(0.5, 0.5, 4.5), (2.5, 1.0, 2.5), stone,
           shininess=20.0, spec_strength=0.18, name="step1")
    _block(scene, physics, Vec3(-3.2, 1.0, 3.0), (2.5, 2.0, 2.5), stone,
           shininess=20.0, spec_strength=0.18, name="step2")
    _block(scene, physics, Vec3(-7.4, 1.5, 0.8), (2.0, 3.0, 2.0), stone,
           shininess=20.0, spec_strength=0.18, name="tower")
    _block(scene, physics, Vec3(-4.6, 2.7, 0.8), (5.2, 0.4, 1.4),
           (0.74, 0.51, 0.32), shininess=12.0, spec_strength=0.10,
           name="bridge")

    # ---- 装饰立柱 + 墙顶自发光路标 ----
    for i, (px, pz) in enumerate(((10, -6), (16, -6), (10, -1), (16, -1))):
        _block(scene, physics, Vec3(px, 1.6, pz), (0.8, 3.2, 0.8),
               (0.86, 0.81, 0.74), shininess=20.0, spec_strength=0.15,
               name="pillar%d" % i)
    for i, (mx, mz) in enumerate(((0, -29.2), (0, 29.2),
                                  (-29.2, 0), (29.2, 0))):
        _block(scene, physics, Vec3(mx, 3.3, mz), (0.5, 0.5, 0.5),
               (1.0, 0.80, 0.40), cast_shadow=False, unlit=True,
               name="marker%d" % i)

    # ---- 木箱区（AABB 动态：可推 / 可堆 / 重箱难推）----
    _block(scene, physics, Vec3(4.5, 0.5, -3.0), (1, 1, 1), (0.82, 0.58, 0.34),
           static=False, mass=3.0, friction=1.5,
           shininess=14.0, spec_strength=0.12, name="crate_a")
    _block(scene, physics, Vec3(5.7, 0.5, -3.3), (1, 1, 1), (0.76, 0.53, 0.31),
           static=False, mass=3.0, friction=1.5,
           shininess=14.0, spec_strength=0.12, name="crate_b")
    _block(scene, physics, Vec3(4.5, 1.5, -3.0), (1, 1, 1), (0.86, 0.63, 0.38),
           static=False, mass=2.5, friction=1.5,
           shininess=14.0, spec_strength=0.12, name="crate_c")
    _block(scene, physics, Vec3(7.6, 0.7, -5.6), (1.4, 1.4, 1.4),
           (0.66, 0.48, 0.31), static=False, mass=9.0, friction=2.5,
           shininess=14.0, spec_strength=0.12, name="crate_heavy")

    # ---- 弹力球区（球体碰撞 + 弹性冲量）----
    _ball(scene, physics, Vec3(-4.6, 0.9, -4.4), 0.45, (0.94, 0.42, 0.38),
          restitution=0.62, name="ball_red")
    _ball(scene, physics, Vec3(-5.5, 0.9, -3.5), 0.45, (0.42, 0.62, 0.90),
          restitution=0.50, mass=1.6, name="ball_blue")
    _ball(scene, physics, Vec3(-3.7, 0.9, -5.4), 0.45, (0.48, 0.78, 0.52),
          restitution=0.55, name="ball_green")
    _ball(scene, physics, Vec3(9.0, 1.3, 4.0), 0.80, (0.97, 0.84, 0.40),
          restitution=0.75, mass=2.0, name="ball_bouncy")

    # ---- 追踪炮台（站在炮台垫上）----
    _block(scene, physics, Vec3(-12.0, 0.5, 9.0), (4, 1, 4), stone,
           shininess=20.0, spec_strength=0.18, name="turret_pad")
    if mode == "explore":
        # 挑战模式不生成炮台/眼球（GDD §0 取舍：避免敌我火力混淆）
        app.turret = Turret(scene, physics, app.player, Vec3(-12.0, 1.0, 9.0))
        app.eye = Eye(scene, physics, app.player, Vec3(0.0, 3.3, -1.0))
    else:
        app.turret = None
        app.eye = None

    # ---- 灯塔（旋转聚光 + 实时阴影）----
    app.lighthouse = Lighthouse(scene, physics, app, Vec3(14.0, 0.0, 14.0))

    # ---- 挑战模式战斗层 ----
    # "boss" 与 "challenge" 共用同一套战斗层（玩家/武器/combat/波次管理器），
    # 差别只是 `app.waves` 换成了 `app.bosses`。见 build_world 开头说明。
    if mode in ("challenge", "boss"):
        _build_combat_layer(app)
