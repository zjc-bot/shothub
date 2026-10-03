# -*- coding: utf-8 -*-
"""光源数据类：平行光（太阳）与聚光灯。

SpotLight 为纯数据对象（可运行时改参数，无 GL 依赖）：
- 手电筒：game.app 每帧把它同步到相机位置/朝向
- 灯塔：固定位置，方向随时间旋转扫射
- cast_shadow=True 的灯由 Renderer 分配 depth FBO 并执行阴影 pass

光锥三要素（对应任务要求「锥角、强度、衰减可实时调节」）：
- inner/outer 角 : 光锥角度（软边过渡，smoothstep）
- intensity      : 强度（分子）
- attenuation    : 距离平方衰减系数（分母 1 + k*d^2）
"""
import math

from engine.math_utils import Vec3, mat_look_at, mat_perspective, mat_mul


class DirectionalLight:
    """平行光：仅方向起作用，模拟远处太阳。"""

    def __init__(self, direction, color=(1.0, 0.96, 0.88), intensity=0.9):
        self.direction = direction.normalized()
        self.color = tuple(c * intensity for c in color)


class SpotLight:
    def __init__(self, position, direction, color=(1.0, 1.0, 1.0),
                 inner_angle_deg=14.0, outer_angle_deg=22.0,
                 intensity=3.0, attenuation=0.03,
                 cast_shadow=False, shadow_res=1024,
                 shadow_near=0.2, shadow_far=45.0, enabled=True):
        self.position = position.clone()
        self.direction = direction.normalized()
        self.color = tuple(color)
        self.inner_angle = math.radians(inner_angle_deg)
        self.outer_angle = math.radians(outer_angle_deg)
        self.intensity = float(intensity)
        self.attenuation = float(attenuation)
        self.cast_shadow = bool(cast_shadow)
        self.shadow_res = int(shadow_res)
        self.shadow_near = float(shadow_near)
        self.shadow_far = float(shadow_far)
        self.enabled = bool(enabled)
        # GPU 资源（由 graphics.renderer.Renderer 分配）
        self._depth_tex = None
        self._fbo = None

    # ---------- 运行时调节（快捷键调用，参数均做范围钳制） ----------
    # 注意：self.inner_angle / self.outer_angle 恒为弧度，
    # 下面所有接口入参与钳制上下限统一使用「度」，边界处显式换算。
    def set_inner_angle_deg(self, deg):
        hi = math.degrees(self.outer_angle) - 0.05   # 内锥必须小于外锥
        self.inner_angle = math.radians(max(2.0, min(deg, hi)))

    def set_outer_angle_deg(self, deg):
        lo = math.degrees(self.inner_angle) + 0.05   # 外锥必须大于内锥
        deg = max(lo, min(60.0, deg))
        self.outer_angle = math.radians(deg)

    def add_intensity(self, delta):
        self.intensity = max(0.0, min(10.0, self.intensity + delta))

    def add_attenuation(self, delta):
        self.attenuation = max(0.002, min(0.2, self.attenuation + delta))

    # ---------- 光锥参数打包（主着色器 u_spot_params 语义） ----------
    def params_vec4(self):
        return (
            math.cos(self.inner_angle),
            math.cos(self.outer_angle),
            self.attenuation,
            self.intensity,
        )

    # ---------- 阴影投影矩阵 ----------
    def shadow_view_proj(self):
        """聚光灯透视投影（fov = 外锥角*2 覆盖整个光锥范围）。"""
        view = mat_look_at(
            self.position,
            self.position + self.direction,
            Vec3(0.0, 1.0, 0.0),
        )
        fov = min(self.outer_angle * 2.0 + 0.08, math.radians(150.0))
        proj = mat_perspective(fov, 1.0, self.shadow_near, self.shadow_far)
        return mat_mul(proj, view)
