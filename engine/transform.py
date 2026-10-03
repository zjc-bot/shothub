# -*- coding: utf-8 -*-
"""变换系统：位置 + 欧拉角(yaw/pitch/roll) + 均匀缩放。

设计说明：
- 为保持法线矩阵简单可靠（避免通用逆矩阵），全引擎使用「均匀缩放」；
  需要长条形状时通过程序化生成对应尺寸的网格（engine/mesh.py）实现。
- 不做父子层级（demo 规模不需要），组合对象由游戏逻辑直接同步子部件。
"""
from .math_utils import (
    Vec3, dir_from_yaw_pitch, yaw_pitch_from_dir,
    mat_mul, mat_translate, mat_scale,
    mat_rot_x, mat_rot_y, mat_rot_z,
)


class Transform:
    """实体变换：position 为实体「中心」位置（与碰撞体中心一致）。"""

    __slots__ = ("position", "yaw", "pitch", "roll", "scale")

    def __init__(self, position=None, yaw=0.0, pitch=0.0, roll=0.0, scale=1.0):
        self.position = position.clone() if position is not None else Vec3()
        self.yaw = float(yaw)
        self.pitch = float(pitch)
        self.roll = float(roll)
        self.scale = float(scale)

    # ---------- 朝向工具 ----------
    def forward(self):
        """本变换的朝向单位向量（含 pitch）。"""
        return dir_from_yaw_pitch(self.yaw, self.pitch)

    def flat_forward(self):
        """水平面上的前方（忽略 pitch，用于移动控制）。"""
        return dir_from_yaw_pitch(self.yaw, 0.0)

    def right(self):
        """右方 = forward × up（水平系）。"""
        f = self.flat_forward()
        return f.cross(Vec3(0.0, 1.0, 0.0)).normalized()

    def look_at(self, target):
        """瞬间转向目标点（平滑追踪请在调用方做角度插值）。"""
        d = target - self.position
        if d.length_sq() < 1e-10:
            return
        self.yaw, self.pitch = yaw_pitch_from_dir(d)

    # ---------- 矩阵输出 ----------
    def model_matrix(self):
        """M = T · Ry · Rx · Rz · S（列主序，供 moderngl 直接上传）。"""
        m = mat_mul(mat_rot_y(self.yaw), mat_scale(self.scale))
        if self.pitch != 0.0:
            m = mat_mul(m, mat_rot_x(self.pitch))
        if self.roll != 0.0:
            m = mat_mul(m, mat_rot_z(self.roll))
        return mat_mul(mat_translate(self.position), m)
