# -*- coding: utf-8 -*-
"""物理与碰撞模块：AABB / 球体碰撞检测与响应。

核心概念
--------
PhysicsBody : 挂在 Entity 上的碰撞体
    kind        'aabb' 或 'sphere'
    half/radius AABB 半尺寸 / 球半径
    velocity    速度（重力与积分在本模块内完成）
    is_static   静态体（墙/地面），不参与积分，只作为碰撞面
    mass        质量：动态-动态碰撞按 inv_mass 比例分摊位移（实现「推箱子」）
    restitution 弹性系数（>0 时沿法线反弹，用于弹跳球）
    friction    着地水平摩擦（指数衰减系数，越大停得越快）
    grounded    本步是否着地（玩家跳跃判定）

求解策略（稳定优先，规模可控）：
- 固定步长积分（1/120s，引擎调用方保证）
- 位置修正：每步 2 次迭代遍历 动态×全部 碰撞对
- 动态 vs 静态：位移全给动态方；法向速度按弹性系数处理；法线朝上记 grounded
- 动态 vs 动态：位移按质量反比分摊（重物少动、轻物多动 → 玩家能推箱子）；
  涉及球体时额外做冲量式速度交换（弹跳感）
"""
from .math_utils import Vec3, clamp


# ----------------------------------------------------------------------
# 碰撞检测原语：返回 (法线[从 B 指向 A], 穿透量) 或 None
# ----------------------------------------------------------------------

def aabb_vs_aabb(pa, ha, pb, hb):
    """AABB-AABB：pa/pb 中心，ha/hb 半尺寸。"""
    dx = ha.x + hb.x - abs(pa.x - pb.x)
    if dx <= 0.0:
        return None
    dy = ha.y + hb.y - abs(pa.y - pb.y)
    if dy <= 0.0:
        return None
    dz = ha.z + hb.z - abs(pa.z - pb.z)
    if dz <= 0.0:
        return None
    # 选穿透量最小的轴作为分离轴
    if dx <= dy and dx <= dz:
        s = 1.0 if pa.x >= pb.x else -1.0
        return Vec3(s, 0.0, 0.0), dx
    if dy <= dz:
        s = 1.0 if pa.y >= pb.y else -1.0
        return Vec3(0.0, s, 0.0), dy
    s = 1.0 if pa.z >= pb.z else -1.0
    return Vec3(0.0, 0.0, s), dz


def sphere_vs_aabb(sc, radius, bc, hb):
    """球 vs AABB：closest-point 法。球心在盒内时退化为最小穿透轴。"""
    cx = clamp(sc.x, bc.x - hb.x, bc.x + hb.x)
    cy = clamp(sc.y, bc.y - hb.y, bc.y + hb.y)
    cz = clamp(sc.z, bc.z - hb.z, bc.z + hb.z)
    d = Vec3(sc.x - cx, sc.y - cy, sc.z - cz)
    dist2 = d.length_sq()
    if dist2 > radius * radius:
        return None
    if dist2 > 1e-10:
        dist = dist2 ** 0.5
        return d / dist, radius - dist
    # 球心陷入盒内：找最小穿透方向推出
    px = hb.x + radius - abs(sc.x - bc.x)
    py = hb.y + radius - abs(sc.y - bc.y)
    pz = hb.z + radius - abs(sc.z - bc.z)
    if px <= py and px <= pz:
        return Vec3(1.0 if sc.x >= bc.x else -1.0, 0.0, 0.0), px
    if py <= pz:
        return Vec3(0.0, 1.0 if sc.y >= bc.y else -1.0, 0.0), py
    return Vec3(0.0, 0.0, 1.0 if sc.z >= bc.z else -1.0), pz


def sphere_vs_sphere(sa, ra, sb, rb):
    d = sa - sb
    dist2 = d.length_sq()
    rsum = ra + rb
    if dist2 >= rsum * rsum or dist2 < 1e-12:
        return None
    dist = dist2 ** 0.5
    return d / dist, rsum - dist


# ----------------------------------------------------------------------
# 碰撞体
# ----------------------------------------------------------------------

class PhysicsBody:
    __slots__ = (
        "entity", "kind", "half", "radius", "velocity",
        "mass", "inv_mass", "is_static", "restitution", "friction",
        "gravity_scale", "grounded", "on_hit",
    )

    def __init__(self, entity, kind="aabb", half=None, radius=0.5,
                 mass=1.0, is_static=False, restitution=0.0, friction=0.0,
                 gravity_scale=1.0):
        assert kind in ("aabb", "sphere")
        self.entity = entity
        self.kind = kind
        self.half = half.clone() if half is not None else Vec3(0.5, 0.5, 0.5)
        self.radius = float(radius)
        self.velocity = Vec3()
        self.mass = max(1e-6, float(mass))
        self.is_static = bool(is_static)
        self.inv_mass = 0.0 if is_static else 1.0 / self.mass
        self.restitution = float(restitution)
        self.friction = float(friction)
        self.gravity_scale = float(gravity_scale)
        self.grounded = False
        self.on_hit = None  # 可选回调 on_hit(other_body, normal)，用于音效/特效扩展

    @property
    def center(self):
        """碰撞体世界中心 = 实体位置。"""
        return self.entity.transform.position

    def describe(self):
        if self.kind == "aabb":
            return "AABB(%.2f,%.2f,%.2f)" % (self.half.x * 2, self.half.y * 2, self.half.z * 2)
        return "Sphere(r=%.2f)" % self.radius


# ----------------------------------------------------------------------
# 物理世界
# ----------------------------------------------------------------------

class PhysicsWorld:
    def __init__(self, gravity=18.0, iterations=2):
        self.bodies = []
        self.gravity = float(gravity)
        self.iterations = max(1, int(iterations))
        self.contact_count = 0  # 调试统计

    # ---------- 注册 ----------
    def register(self, body):
        self.bodies.append(body)
        return body

    def unregister(self, body):
        try:
            self.bodies.remove(body)
        except ValueError:
            pass

    # ---------- 主步进 ----------
    def step(self, dt):
        # 1) 积分：重力 + 位移（摩擦放在碰撞解算后，仅对本步着地体生效）
        for b in self.bodies:
            b.grounded = False
            if b.is_static:
                continue
            b.velocity.y -= self.gravity * b.gravity_scale * dt
            tr = b.entity.transform
            tr.position = tr.position.scaled_add(b.velocity, dt)

        # 2) 碰撞解算（多次迭代让堆叠更稳定）
        self.contact_count = 0
        for _ in range(self.iterations):
            for a in self.bodies:
                if a.is_static:
                    continue
                for b in self.bodies:
                    if a is b:
                        continue
                    self._resolve(a, b)

        # 3) 着地摩擦：指数衰减水平速度（grounded 由第 2 步写入）
        #    放在解算后保证「刚落地立即减速、空中不损失动量」。
        for b in self.bodies:
            if b.is_static or not b.grounded or b.friction <= 0.0:
                continue
            f = max(0.0, 1.0 - b.friction * dt)
            b.velocity.x *= f
            b.velocity.z *= f

    # ---------- 单对解算 ----------
    def _resolve(self, a, b):
        hit = self._detect(a, b)
        if hit is None:
            return
        n, pen = hit           # n：从 b 指向 a 的推出方向
        self.contact_count += 1
        if b.is_static:
            self._resolve_vs_static(a, n, pen)
        else:
            self._resolve_dynamic_pair(a, b, n, pen)

    def _detect(self, a, b):
        pa = a.center
        pb = b.center
        if a.kind == "aabb" and b.kind == "aabb":
            return aabb_vs_aabb(pa, a.half, pb, b.half)
        if a.kind == "sphere" and b.kind == "aabb":
            return sphere_vs_aabb(pa, a.radius, pb, b.half)
        if a.kind == "aabb" and b.kind == "sphere":
            hit = sphere_vs_aabb(pb, b.radius, pa, a.half)
            if hit is None:
                return None
            n, pen = hit
            return -n, pen      # 法线翻转：从 b(球) 指向 a(盒)
        return sphere_vs_sphere(pa, a.radius, pb, b.radius)

    def _resolve_vs_static(self, a, n, pen):
        """动态 vs 静态：全部位移给动态方 + 法向速度处理。"""
        tr = a.entity.transform
        tr.position = tr.position.scaled_add(n, pen)
        vn = a.velocity.dot(n)
        if vn < 0.0:
            e = a.restitution
            if e > 0.0:
                a.velocity = a.velocity - n * (vn * (1.0 + e))
            else:
                a.velocity = a.velocity - n * vn   # 消除法向分量 → 沿面滑动
        if n.y > 0.5:
            a.grounded = True
        if a.on_hit is not None:
            a.on_hit(None, n)

    def _resolve_dynamic_pair(self, a, b, n, pen):
        """动态 vs 动态：按质量反比分摊位移（推箱子核心），球体做冲量反弹。"""
        inv_sum = a.inv_mass + b.inv_mass
        if inv_sum <= 0.0:
            return
        wa = a.inv_mass / inv_sum
        wb = b.inv_mass / inv_sum
        ta = a.entity.transform
        tb = b.entity.transform
        ta.position = ta.position.scaled_add(n, pen * wa)
        tb.position = tb.position.scaled_add(n, -pen * wb)

        rel = (a.velocity - b.velocity).dot(n)
        if rel < 0.0 and (a.kind == "sphere" or b.kind == "sphere"):
            e = max(a.restitution, b.restitution)
            imp = -(1.0 + e) * rel / inv_sum
            a.velocity = a.velocity + n * (imp * a.inv_mass)
            b.velocity = b.velocity - n * (imp * b.inv_mass)
        if n.y > 0.5 and a.kind == "aabb":
            a.grounded = True    # 可以站在箱子/球上
        if a.on_hit is not None:
            a.on_hit(b, n)
