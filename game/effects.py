# -*- coding: utf-8 -*-
"""命中特效：火花四溅 + 弹痕贴片（ENG-007）。

设计约束（与既有系统一致）：
- **零外部资源**：弹痕贴图由 `graphics/texture.decal_bytes()` 程序化生成
  （RGBA，弹孔 + 放射裂纹 + 边缘羽化），已注册为内置纹理 "decal"。
- **不加灯**：全场聚光灯上限 3（MAX_SPOTS，手电+灯塔已占 2），所以所有
  特效一律 `unlit=True`，不挤灯光预算（同 Tracer / 枪口火光 / 掉落物）。
- **对象池 + 就地回收**：MAX_DECALS / MAX_SPARKS 为硬上限。超限时把池里
  最旧的实体**原地改属性复用**，不走 Scene 的延迟删除队列——长时间射击
  不会累积实体，也不会产生 add/remove 抖动（FPS 175 要求）。
- **弹痕只挂静态面**：bot 身上不生成（尸体消失后弹痕会悬空），会移动的
  动态木箱也跳过（贴面弹痕会脱节）。

坐标系约定（关键）：引擎 `Transform.model_matrix() = T · Ry(yaw) · Rx(pitch) · Rz(roll)`，
所以"让平面法线（局部 +Y）对准命中法线 n"的解为
    pitch = acos(n.y)，yaw = atan2(n.x, n.z)
见 `_yaw_pitch_from_normal()`，与 `mesh.plane_mesh` 的 +Y 法线配套。
"""
import math
import random

from engine.scene import Entity
from engine.mesh import plane_mesh, box_mesh, MeshRenderer
from engine.math_utils import Vec3, clamp

# ---- 可调参数（集中在此，方便调参） --------------------------------
MAX_DECALS = 40          # 同时存在的弹痕上限（超出回收最旧）
MAX_SPARKS = 64          # 同时存在的火花上限

DECAL_SIZE = 0.16        # 弹痕贴片边长（米）
DECAL_NORMAL_OFFSET = 0.012   # 沿法线偏移，防与墙面 z-fighting
DECAL_LIFE = 10.0        # 弹痕存活秒数（需求：约 10s 后淡出销毁）
DECAL_FADE = 2.5         # 末尾淡出秒数
DECAL_MIN_SCALE = 0.75   # 随机缩放下限（1.0 = 标称大小）
DECAL_MAX_SCALE = 1.25   # 随机缩放上限
DECAL_COLOR = (0.16, 0.14, 0.18)    # 深炭紫（纯黑在 pastel 场景里显脏）

SPARK_LIFE = 0.16        # 单条火花寿命（秒）
SPARK_SIZE = 0.035       # 火花小方块边长（米）
SPARK_COUNT = 4          # 每次命中的火花条数
SPARK_SPEED = 3.2        # 火花初速度（米/秒）
SPARK_GRAVITY = 14.0     # 火花重力（米/秒²），略小于世界重力 18，飞得久
SPARK_SPREAD = 0.75      # 火花相对命中法线的锥角（弧度）
SPARK_COLOR = (1.0, 0.86, 0.52)    # 琥珀偏暖（沿用枪口火光色系）
IMPACT_FLASH_LIFE = 0.07 # 命中瞬间的暖色亮斑
IMPACT_FLASH_SIZE = 0.09


def _yaw_pitch_from_normal(n):
    """解出使**局部 +Y 对齐 n** 的 (yaw, pitch)。

    推导（列主序，M = T·Ry(yaw)·Rx(pitch)·Rz(roll)，见 Transform.model_matrix）。
    取 roll=0，展开 Ry·Rx 作用于局部 +Y 得
        (cos(yaw)·sin(pitch),  cos(pitch),  sin(yaw)·sin(pitch))
    要它等于 n：
        cos(pitch) = n.y                       → pitch = acos(n.y)
        sin(pitch)·cos(yaw) = n.x
        sin(pitch)·sin(yaw) = n.z
    两式同除 sin(pitch) = hypot(n.x, n.z) 即得 yaw = atan2(n.x, n.z)。
    退化情形（n 正上/正下，sin(pitch)≈0）yaw 不影响结果，取 0。
    """
    n = n.normalized()
    p = math.acos(clamp(n.y, -1.0, 1.0))
    sp = math.sin(p)
    yaw = math.atan2(n.x, n.z) if abs(sp) > 1e-9 else 0.0
    return yaw, p


# ======================================================================
# 实体
# ======================================================================
class Spark(Entity):
    """一颗火花：unlit 小方块，沿命中法线半球发散，受自身重力下坠。

    不是粒子系统——引擎没有粒子系统且要求零外部资源；3~4 个复用网格的
    小方块即可得到同样的视觉密度，draw call 代价极低。
    以"缩放趋 0"表现熄灭，无需 alpha 混合（主 pass 走 unlit 不透明）。
    """

    def __init__(self, scene, pos, vel, life, size):
        super().__init__("spark", "fx")
        self.scene = scene
        self._size = size
        self.renderer = MeshRenderer(box_mesh(size, size, size),
                                     color=SPARK_COLOR,
                                     cast_shadow=False, unlit=True)
        self.revive(pos, vel, life)

    def revive(self, pos, vel, life):
        """就地复用：重置位置/速度/寿命（对象池命中上限时调用，不走增删）。"""
        self._vel = vel
        self._life = life
        self._full = life
        self.alive = True
        self.transform.scale = 1.0
        self.transform.position = pos.clone()
        self.transform.yaw = 0.0
        self.transform.pitch = 0.0

    def update(self, dt):
        self._life -= dt
        if self._life <= 0.0:
            self.scene.remove(self)
            return
        k = self._life / self._full
        self.transform.scale = max(0.05, k)
        self._vel.y -= SPARK_GRAVITY * dt
        self.transform.position = \
            self.transform.position.scaled_add(self._vel, dt)


class Decal(Entity):
    """弹痕贴片：贴面小平面，走半透明 decal pass 淡出。"""

    def __init__(self, scene):
        super().__init__("decal", "fx")
        self.scene = scene
        r = MeshRenderer(plane_mesh(DECAL_SIZE, DECAL_SIZE, 1, 0.5),
                         color=DECAL_COLOR, cast_shadow=False, unlit=True)
        r.decal = True
        r.alpha = 1.0
        self.renderer = r
        self.alive = False          # 等首次 revive 才进渲染循环

    def revive(self, point, normal, life=DECAL_LIFE):
        """就地复用：按命中点与法线重新贴面。"""
        yaw, pitch = _yaw_pitch_from_normal(normal)
        # 沿法线抬高：即使有深度测试兜底，双保险防 z-fighting
        self.transform.position = point + normal.normalized() * \
            DECAL_NORMAL_OFFSET
        self.transform.yaw = yaw
        self.transform.pitch = pitch
        self.transform.roll = random.uniform(0.0, math.tau)  # 防雷同
        self.transform.scale = random.uniform(DECAL_MIN_SCALE,
                                              DECAL_MAX_SCALE)
        self._life = life
        self._full = life
        self.renderer.alpha = 1.0
        self.alive = True

    def update(self, dt):
        self._life -= dt
        if self._life <= 0.0:
            self.scene.remove(self)
            return
        # 末尾 DECAL_FADE 秒内 alpha 从 1 线性降到 0。
        # 用 life 绝对值而非比例：生命前期（life 远大于 FADE）必须恒为 1，
        # 若写成 life/FADE 会在刚生成时就是 0.2 左右（太淡看不清）。
        if self._life < DECAL_FADE:
            self.renderer.alpha = max(0.0, self._life / DECAL_FADE)
        else:
            self.renderer.alpha = 1.0


# ======================================================================
# 管理器
# ======================================================================
class ImpactFX:
    """命中特效管理器：火花 + 弹痕；对象池有界，长时间射击不涨实体数。

    挂在 App 上（app.fx），由 CombatManager 在 hitscan 裁决处调用。
    """

    def __init__(self, app):
        self.app = app
        self._decals = []        # 存活的 Decal，队首最旧
        self._sparks = []
        self.spawned_decals = 0  # 累计生成数（selftest 断言用）
        self.spawned_sparks = 0
        self.recycles = 0        # 累计回收次数（压力测试观察用）

    # ------------------------------------------------------------------
    def on_hit(self, point, normal, is_static):
        """一次命中的全部反馈。is_static 决定是否落弹痕。"""
        self.spawn_flash(point)
        self.spawn_sparks(point, normal)
        if is_static:
            self.spawn_decal(point, normal)

    # ------------------------------------------------------------------
    def spawn_flash(self, point):
        s = self._obtain_spark(self._sparks, MAX_SPARKS, point,
                               Vec3(0.0, 0.0, 0.0), IMPACT_FLASH_LIFE,
                               IMPACT_FLASH_SIZE)
        self.spawned_sparks += 1
        return s

    def spawn_sparks(self, point, normal):
        n = normal.normalized()
        # 构造与 n 垂直的两个切向
        up = Vec3(0.0, 1.0, 0.0) if abs(n.y) < 0.9 else Vec3(1.0, 0.0, 0.0)
        t1 = n.cross(up).normalized()
        t2 = n.cross(t1).normalized()
        for _ in range(SPARK_COUNT):
            ang = random.uniform(0.0, math.tau)
            # sqrt 保证立体角均匀（锥内均匀采样）
            cone = SPARK_SPREAD * math.sqrt(random.random())
            perp = t1 * math.cos(ang) + t2 * math.sin(ang)
            d = (n * math.cos(cone) + perp * math.sin(cone)).normalized()
            vel = d * (SPARK_SPEED * random.uniform(0.6, 1.15))
            self._obtain_spark(self._sparks, MAX_SPARKS, point, vel,
                               SPARK_LIFE * random.uniform(0.7, 1.2),
                               SPARK_SIZE)
            self.spawned_sparks += 1

    def spawn_decal(self, point, normal):
        d = self._obtain_decal(point, normal)
        self.spawned_decals += 1
        return d

    # ------------------------------------------------------------------
    def _obtain_spark(self, pool, cap, point, vel, life, size):
        """从池取一个火花实体：优先复用最旧的，池空则新建。"""
        self._prune(pool)
        if len(pool) < cap:
            s = Spark(self.app.scene, point, vel, life, size)
            pool.append(s)
            return s
        s = pool.pop(0)                 # 队首 = 最旧，原地复用
        s.revive(point, vel, life)
        self.recycles += 1
        pool.append(s)
        return s

    def _obtain_decal(self, point, normal):
        self._prune(self._decals)
        if len(self._decals) < MAX_DECALS:
            d = Decal(self.app.scene)
            d.revive(point, normal)
            self._decals.append(d)
            return d
        d = self._decals.pop(0)        # 队首 = 最旧，原地复用
        d.revive(point, normal)
        self.recycles += 1
        self._decals.append(d)
        return d

    @staticmethod
    def _prune(pool):
        """清掉已到期（alive=False）的池内引用，避免池无限增长。"""
        for i in range(len(pool) - 1, -1, -1):
            if not pool[i].alive:
                pool.pop(i)

    def clear(self):
        """世界重建时清空池（Scene.clear 已统一删除实体，这里只清引用）。"""
        self._decals.clear()
        self._sparks.clear()
