# -*- coding: utf-8 -*-
"""轻量三维数学库（零第三方依赖）。

约定（全项目统一）：
- 右手坐标系，+Y 为上；yaw=0 时朝向 -Z（OpenGL 惯例）。
- Mat4 以「列主序」一维列表存储（与 OpenGL / moderngl 直接兼容）。
- 角度一律使用弧度；yaw 绕 +Y（正值向左转），pitch 绕 +X（抬头为正）。
"""
import math

PI = math.pi
TAU = math.pi * 2.0


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def lerp(a, b, t):
    return a + (b - a) * t


class Vec3:
    """不可变风格的三维向量（运算均返回新对象，避免共享状态 bug）。"""

    __slots__ = ("x", "y", "z")

    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)

    def __repr__(self):
        return "Vec3(%.3f, %.3f, %.3f)" % (self.x, self.y, self.z)

    # ---------- 基础运算 ----------
    def clone(self):
        return Vec3(self.x, self.y, self.z)

    def __add__(self, o):
        return Vec3(self.x + o.x, self.y + o.y, self.z + o.z)

    def __sub__(self, o):
        return Vec3(self.x - o.x, self.y - o.y, self.z - o.z)

    def __neg__(self):
        return Vec3(-self.x, -self.y, -self.z)

    def __mul__(self, s):
        return Vec3(self.x * s, self.y * s, self.z * s)

    __rmul__ = __mul__

    def __truediv__(self, s):
        return Vec3(self.x / s, self.y / s, self.z / s)

    # ---------- 向量代数 ----------
    def dot(self, o):
        return self.x * o.x + self.y * o.y + self.z * o.z

    def cross(self, o):
        return Vec3(
            self.y * o.z - self.z * o.y,
            self.z * o.x - self.x * o.z,
            self.x * o.y - self.y * o.x,
        )

    def length_sq(self):
        return self.x * self.x + self.y * self.y + self.z * self.z

    def length(self):
        return math.sqrt(self.length_sq())

    def normalized(self):
        n = self.length()
        if n < 1e-12:
            return Vec3(0.0, 0.0, 0.0)
        return Vec3(self.x / n, self.y / n, self.z / n)

    def scaled_add(self, o, t):
        """返回 self + o*t（常用于位移累积与平滑追踪）。"""
        return Vec3(self.x + o.x * t, self.y + o.y * t, self.z + o.z * t)

    def to_tuple(self):
        return (self.x, self.y, self.z)


def vec_lerp(a, b, t):
    """向量线性插值。"""
    return Vec3(
        lerp(a.x, b.x, t),
        lerp(a.y, b.y, t),
        lerp(a.z, b.z, t),
    )


def dir_from_yaw_pitch(yaw, pitch):
    """由 yaw/pitch 计算单位朝向向量。

    推导：yaw=0、pitch=0 时 forward = (0,0,-1)；
    yaw 增大向左（-X 方向），pitch 增大抬头（+Y 方向）。
    """
    cp = math.cos(pitch)
    return Vec3(-math.sin(yaw) * cp, math.sin(pitch), -math.cos(yaw) * cp)


def yaw_pitch_from_dir(d):
    """由方向向量反解 (yaw, pitch)；d 无需归一化。"""
    yaw = math.atan2(-d.x, -d.z)
    pitch = math.asin(clamp(d.normalized().y, -1.0, 1.0))
    return yaw, pitch


def wrap_angle(a):
    """把角度差归一化到 [-pi, pi]，用于平滑转向时避免绕远路。"""
    while a > PI:
        a -= TAU
    while a < -PI:
        a += TAU
    return a


def ray_aabb(origin, direction, center, half):
    """slab 法射线 vs AABB，返回最近进入距离 t 或 None。

    - direction 需归一化；只报告 t >= 0 的命中（射线背后的盒返回 None）。
    - 起点在盒内时返回 0.0（hitscan 语义：立即命中）。
    - 平行轴零方向分量：起点落在该轴 slab 外 → 必然不中。
    - 负方向分量通过交换 t1/t2 天然处理。
    """
    t_min = 0.0
    t_max = float("inf")
    for o, d, c, h in ((origin.x, direction.x, center.x, half.x),
                       (origin.y, direction.y, center.y, half.y),
                       (origin.z, direction.z, center.z, half.z)):
        lo = c - h
        hi = c + h
        if -1e-12 < d < 1e-12:
            if o < lo or o > hi:
                return None
            continue
        t1 = (lo - o) / d
        t2 = (hi - o) / d
        if t1 > t2:
            t1, t2 = t2, t1
        if t1 > t_min:
            t_min = t1
        if t2 < t_max:
            t_max = t2
        if t_min > t_max:
            return None
    return t_min


# ======================================================================
# Mat4：列主序一维数组，索引 = col * 4 + row
# ======================================================================

def mat_identity():
    return [
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def mat_mul(a, b):
    """返回矩阵积 C = A·B（作用顺序：先 B 后 A，与 GLSL 一致）。"""
    c = [0.0] * 16
    for col in range(4):
        for row in range(4):
            s = 0.0
            for k in range(4):
                s += a[k * 4 + row] * b[col * 4 + k]
            c[col * 4 + row] = s
    return c


def mat_translate(t):
    return [
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        t.x, t.y, t.z, 1.0,
    ]


def mat_scale(s):
    return [
        s, 0.0, 0.0, 0.0,
        0.0, s, 0.0, 0.0,
        0.0, 0.0, s, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def mat_rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return [
        1.0, 0.0, 0.0, 0.0,
        0.0, c, s, 0.0,
        0.0, -s, c, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def mat_rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return [
        c, 0.0, -s, 0.0,
        0.0, 1.0, 0.0, 0.0,
        s, 0.0, c, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def mat_rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return [
        c, s, 0.0, 0.0,
        -s, c, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def mat_perspective(fovy, aspect, near, far):
    """透视投影矩阵（OpenGL 标准 gluPerspective 语义）。"""
    f = 1.0 / math.tan(fovy * 0.5)
    nf = 1.0 / (near - far)
    return [
        f / aspect, 0.0, 0.0, 0.0,
        0.0, f, 0.0, 0.0,
        0.0, 0.0, (far + near) * nf, -1.0,
        0.0, 0.0, 2.0 * far * near * nf, 0.0,
    ]


def mat_look_at(eye, target, up):
    """右手系观察矩阵（view matrix）。"""
    f = (target - eye).normalized()          # 前方
    s = f.cross(up).normalized()             # 右方
    if s.length_sq() < 1e-8:                 # up 与视线几乎平行时的退化保护
        s = f.cross(Vec3(0.0, 0.0, 1.0)).normalized()
    u = s.cross(f)                           # 真实上方
    return [
        s.x, u.x, -f.x, 0.0,
        s.y, u.y, -f.y, 0.0,
        s.z, u.z, -f.z, 0.0,
        -s.dot(eye), -u.dot(eye), f.dot(eye), 1.0,
    ]


def transform_point(m, p):
    """用矩阵变换一个点（w=1），返回 Vec3（本引擎矩阵均无透视分量）。"""
    x, y, z = p.x, p.y, p.z
    return Vec3(
        m[0] * x + m[4] * y + m[8] * z + m[12],
        m[1] * x + m[5] * y + m[9] * z + m[13],
        m[2] * x + m[6] * y + m[10] * z + m[14],
    )


def project_to_screen(proj, view, p, width, height):
    """世界点 → 屏幕坐标 (sx, sy, view_depth)；相机背后返回 None。

    - p 依次乘 view、proj（均为列主序 Mat4），再做透视除法；
      w ≤ 1e-6 视为点在相机背后（透视矩阵 w = -z_view）。
    - NDC y 上正 → 屏幕 y 下正，需翻转。
    - view_depth 为 view 空间 z（相机前方为负），供调用方做深度裁剪。
    """
    x, y, z = p.x, p.y, p.z
    vx = view[0] * x + view[4] * y + view[8] * z + view[12]
    vy = view[1] * x + view[5] * y + view[9] * z + view[13]
    vz = view[2] * x + view[6] * y + view[10] * z + view[14]
    cx = proj[0] * vx + proj[4] * vy + proj[8] * vz + proj[12]
    cy = proj[1] * vx + proj[5] * vy + proj[9] * vz + proj[13]
    cw = proj[3] * vx + proj[7] * vy + proj[11] * vz + proj[15]
    if cw <= 1e-6:
        return None
    ndc_x = cx / cw
    ndc_y = cy / cw
    sx = (ndc_x * 0.5 + 0.5) * width
    sy = (0.5 - ndc_y * 0.5) * height
    return (sx, sy, vz)
