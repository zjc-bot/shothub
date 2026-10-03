# -*- coding: utf-8 -*-
"""程序化网格生成：立方体 / 地面平面 / UV 球（零外部资源文件）。

MeshData 为纯 CPU 数据（顶点交错 [px,py,pz, nx,ny,nz, u,v] + 索引），
由 graphics.renderer 首次渲染时上传 GPU 并缓存 —— 因此 headless 自检
模式完全无需 OpenGL 即可运行游戏逻辑。

所有网格以「局部原点为中心」，与 Transform.position（碰撞体中心）一致。
带尺寸参数的生成器带模块级缓存：同尺寸只生成一次。
"""
from array import array

from .math_utils import Vec3
from .transform import Transform


class MeshData:
    """CPU 侧网格数据容器。"""

    __slots__ = ("name", "vertices", "indices")

    def __init__(self, name):
        self.name = name
        self.vertices = []   # [px,py,pz, nx,ny,nz, u,v] * N
        self.indices = []

    def add_vertex(self, pos, normal, uv):
        self.vertices.extend((pos.x, pos.y, pos.z,
                              normal.x, normal.y, normal.z,
                              uv[0], uv[1]))

    def packed_vertices(self):
        return array("f", self.vertices).tobytes()

    def packed_indices(self):
        return array("I", self.indices).tobytes()

    def vertex_count(self):
        return len(self.vertices) // 8


class MeshRenderer:
    """渲染组件：GPU 上传前的材质描述（GPU 资源由 Renderer 管理）。"""

    def __init__(self, mesh, color=(1.0, 1.0, 1.0), texture=None,
                 cast_shadow=True, shininess=32.0, spec_strength=0.5,
                 unlit=False):
        self.mesh = mesh
        self.color = tuple(color)
        self.texture = texture            # 纹理名字符串（'checker' 等），None=纯色
        self.cast_shadow = bool(cast_shadow)
        self.shininess = float(shininess)
        self.spec_strength = float(spec_strength)
        self.unlit = bool(unlit)          # 自发光：不受光照/阴影/雾外影响
        self.visible = True
        # 弹痕：走 renderer 的 2.5 半透明 pass（主 pass 跳过），alpha 逐帧调低做淡出
        self.decal = False
        self.alpha = 1.0


# ======================================================================
# 生成器（带缓存）
# ======================================================================
_box_cache = {}
_plane_cache = {}
_sphere_cache = {}


def box_mesh(w, h, d):
    """轴对齐立方体，尺寸 (w,h,d)，中心在原点。"""
    key = (round(w, 4), round(h, 4), round(d, 4))
    if key in _box_cache:
        return _box_cache[key]
    hx, hy, hz = w * 0.5, h * 0.5, d * 0.5
    m = MeshData("box(%.2f,%.2f,%.2f)" % key)
    # 6 个面：法线 + 四角顶点 + uv
    faces = [
        # (法线,                    四角[右系展开],             u轴, v轴)
        (Vec3(0, 0, 1),  [Vec3(-hx, -hy, hz), Vec3(hx, -hy, hz), Vec3(hx, hy, hz), Vec3(-hx, hy, hz)]),
        (Vec3(0, 0, -1), [Vec3(hx, -hy, -hz), Vec3(-hx, -hy, -hz), Vec3(-hx, hy, -hz), Vec3(hx, hy, -hz)]),
        (Vec3(1, 0, 0),  [Vec3(hx, -hy, hz), Vec3(hx, -hy, -hz), Vec3(hx, hy, -hz), Vec3(hx, hy, hz)]),
        (Vec3(-1, 0, 0), [Vec3(-hx, -hy, -hz), Vec3(-hx, -hy, hz), Vec3(-hx, hy, hz), Vec3(-hx, hy, -hz)]),
        (Vec3(0, 1, 0),  [Vec3(-hx, hy, hz), Vec3(hx, hy, hz), Vec3(hx, hy, -hz), Vec3(-hx, hy, -hz)]),
        (Vec3(0, -1, 0), [Vec3(-hx, -hy, -hz), Vec3(hx, -hy, -hz), Vec3(hx, -hy, hz), Vec3(-hx, -hy, hz)]),
    ]
    uvs = [(0, 0), (1, 0), (1, 1), (0, 1)]
    for normal, corners in faces:
        base = m.vertex_count()
        for i in range(4):
            m.add_vertex(corners[i], normal, uvs[i])
        m.indices.extend([base, base + 1, base + 2,
                          base, base + 2, base + 3])
    _box_cache[key] = m
    return m


def plane_mesh(size_x, size_z, segments=1, uv_scale=0.25):
    """XZ 平面（法线 +Y），中心在原点。uv_scale 控制 uv 随世界尺寸的重复率。"""
    key = (round(size_x, 3), round(size_z, 3), segments, round(uv_scale, 4))
    if key in _plane_cache:
        return _plane_cache[key]
    sx, sz = size_x * 0.5, size_z * 0.5
    m = MeshData("plane(%.1f,%.1f)" % (size_x, size_z))
    step_x = size_x / segments
    step_z = size_z / segments
    n = Vec3(0, 1, 0)
    for iz in range(segments + 1):
        z = -sz + iz * step_z
        for ix in range(segments + 1):
            x = -sx + ix * step_x
            m.add_vertex(Vec3(x, 0.0, z), n, (x * uv_scale, z * uv_scale))
    row = segments + 1
    for iz in range(segments):
        for ix in range(segments):
            a = iz * row + ix
            m.indices.extend([a, a + row, a + row + 1,
                              a, a + row + 1, a + 1])
    _plane_cache[key] = m
    return m


def sphere_mesh(radius, stacks=20, slices=28):
    """UV 球，中心在原点，法线 = 归一化局部坐标。"""
    key = (round(radius, 3), stacks, slices)
    if key in _sphere_cache:
        return _sphere_cache[key]
    m = MeshData("sphere(r=%.2f)" % radius)
    import math
    for i in range(stacks + 1):
        phi = math.pi * i / stacks          # 0(顶) ~ pi(底)
        for j in range(slices + 1):
            theta = math.tau * j / slices
            nx = math.sin(phi) * math.cos(theta)
            ny = math.cos(phi)
            nz = math.sin(phi) * math.sin(theta)
            m.add_vertex(Vec3(nx * radius, ny * radius, nz * radius),
                         Vec3(nx, ny, nz),
                         (j / slices, 1.0 - i / stacks))
    row = slices + 1
    for i in range(stacks):
        for j in range(slices):
            a = i * row + j
            # 绕序必须「从球外侧看为逆时针」才能通过背面剔除：
            # a+row 沿 φ 增大（向下），a+1 沿 θ 增大，二者与 plane 的轴向含义不同。
            m.indices.extend([a, a + row + 1, a + row,
                              a, a + 1, a + row + 1])
    _sphere_cache[key] = m
    return m


# ======================================================================
# 实体快速构建工具
# ======================================================================

def make_visual_entity(scene, name, mesh, position, color, tag="",
                       texture=None, cast_shadow=True, unlit=False,
                       yaw=0.0, pitch=0.0, shininess=32.0, spec_strength=0.5):
    """创建一个「仅视觉」实体（无碰撞体）。"""
    from .scene import Entity
    e = Entity(name, tag)
    e.transform.position = position.clone() if position else Vec3()
    e.transform.yaw = yaw
    e.transform.pitch = pitch
    e.renderer = MeshRenderer(mesh, color=color, texture=texture,
                              cast_shadow=cast_shadow, unlit=unlit,
                              shininess=shininess, spec_strength=spec_strength)
    scene.add(e)
    return e
