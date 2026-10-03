# -*- coding: utf-8 -*-
"""相机系统：投影矩阵 + 双模式相机装配架（CameraRig）。

两种模式（Tab 键切换，由 game.app 处理按键）：
- fps    : 第一人称 —— 相机位于玩家眼睛处，朝向随鼠标 yaw/pitch 立即更新
- follow : 第三人称跟随 —— 相机位置向「玩家背后期望点」做指数平滑逼近
           （1-exp(-k*dt) 帧率无关的阻尼跟随），始终 look-at 玩家眼睛

平滑跟随的实现要点：
    pos += (target - pos) * (1 - exp(-k * dt))
比线性 lerp(pos, target, k*dt) 更稳定：k 即「响应速度」，与帧率解耦。
"""
import math

from .math_utils import (
    Vec3, dir_from_yaw_pitch, vec_lerp, mat_perspective, mat_look_at, clamp,
)


class CameraRig:
    def __init__(self, fov_deg=62.0, near=0.1, far=240.0):
        self.mode = "fps"
        self.fov = math.radians(fov_deg)
        self.near = near
        self.far = far
        self.sensitivity = 0.0022      # 鼠标灵敏度（弧度/像素）
        self.pitch_limit = math.radians(89.0)

        self.yaw = 0.0                 # 视线偏航
        self.pitch = 0.0               # 视线俯仰
        self.position = Vec3(0.0, 1.7, 0.0)

        # 跟随模式状态
        self._follow_pos = Vec3(0.0, 4.0, 10.0)
        self.follow_distance = 4.5
        self.follow_height = 2.0
        self.follow_speed = 6.0        # 平滑响应速度 k

        self._aspect = 16.0 / 9.0
        self._eye = Vec3()             # 玩家眼睛位置（每帧由 update 写入）
        self._forward = Vec3(0.0, 0.0, -1.0)

        # 后座注入状态（kick 注入、自动回弹 60%/0.12s）
        self._kick_pitch = 0.0
        self._kick_yaw = 0.0
        self._kick_scale = 1.0        # 回弹速率缩放（kick_recovery_scale 改）

    # ---------- 后座注入（武器开火调用） ----------
    # 回弹速率：0.12s 内恢复 60% → 剩余 40% → k = -ln(0.4)/0.12
    KICK_RECOVER_RATE = 7.6357

    def kick(self, pitch_delta, yaw_delta):
        """注入一次开火后座：立即生效并记录未回弹量，update 中指数扣回。"""
        self.pitch = clamp(self.pitch + pitch_delta,
                           -self.pitch_limit, self.pitch_limit)
        self.yaw = (self.yaw + yaw_delta) % math.tau
        self._kick_pitch += pitch_delta
        self._kick_yaw += yaw_delta

    def kick_recovery_scale(self, factor):
        """临时改回弹速率：factor<1 表示回弹更慢（后坐"坐"在镜头上更久）。

        狙击开火时用 factor=0.55 制造"上抬后停一下再回正"的手感；
        由 Weapon 在开火时设置，下一帧起生效（不改全局默认值）。
        """
        self._kick_scale = max(0.05, min(1.0, float(factor)))

    def _recover_kick(self, dt):
        if self._kick_pitch == 0.0 and self._kick_yaw == 0.0:
            self._kick_scale = 1.0     # 无残留后座时恢复默认回弹速率
            return
        k = 1.0 - math.exp(-self.KICK_RECOVER_RATE * self._kick_scale * dt)
        dp = self._kick_pitch * k
        dy = self._kick_yaw * k
        self.pitch = clamp(self.pitch - dp,
                           -self.pitch_limit, self.pitch_limit)
        self.yaw = (self.yaw - dy) % math.tau
        self._kick_pitch -= dp
        self._kick_yaw -= dy
        if abs(self._kick_pitch) < 1e-6:
            self._kick_pitch = 0.0
        if abs(self._kick_yaw) < 1e-6:
            self._kick_yaw = 0.0

    # ---------- 每帧更新 ----------
    def update(self, input_mgr, player, dt, mouse_captured, aspect,
               sens_scale=1.0):
        """根据鼠标与玩家状态更新相机。player 需有 eye_height 属性。

        sens_scale：灵敏度倍率。武器层在开镜时传入 ads_hip_sens_scale
        （狙击 0.35），实现"瞄准时降低鼠标灵敏度"的 1:1 镜手感。
        """
        self._aspect = aspect
        self._recover_kick(dt)
        if mouse_captured:
            dx, dy = input_mgr.mouse_delta()
            s = self.sensitivity * sens_scale
            self.yaw -= dx * s
            self.pitch = clamp(self.pitch - dy * s,
                               -self.pitch_limit, self.pitch_limit)
            self.yaw %= math.tau

        eye = player.transform.position + Vec3(0.0, player.eye_height, 0.0)
        self._eye = eye
        self._forward = dir_from_yaw_pitch(self.yaw, self.pitch)

        if self.mode == "fps":
            self.position = eye
        else:
            # 期望位置：玩家眼睛后方 + 抬高（水平化处理，俯仰不改变跟随距离）
            flat = dir_from_yaw_pitch(self.yaw, 0.0)
            desired = eye - flat * self.follow_distance
            desired.y = eye.y + self.follow_height
            k = 1.0 - math.exp(-self.follow_speed * dt)
            self._follow_pos = vec_lerp(self._follow_pos, desired, k)
            self.position = self._follow_pos

    # ---------- 输出 ----------
    def eye_and_forward(self):
        return self.position.clone(), self._forward.clone()

    def projection_matrix(self):
        return mat_perspective(self.fov, self._aspect, self.near, self.far)

    def view_matrix(self):
        if self.mode == "fps":
            target = self.position + self._forward
            return mat_look_at(self.position, target, Vec3(0.0, 1.0, 0.0))
        # 跟随模式：看向玩家眼睛稍上方
        target = self._eye + Vec3(0.0, 0.35, 0.0)
        return mat_look_at(self.position, target, Vec3(0.0, 1.0, 0.0))

    def toggle_mode(self):
        """切换第一/第三人称；切换瞬间把跟随位置摆到正确侧后方避免甩镜头。"""
        if self.mode == "fps":
            self.mode = "follow"
            flat = dir_from_yaw_pitch(self.yaw, 0.0)
            desired = self._eye - flat * self.follow_distance
            desired.y = self._eye.y + self.follow_height
            self._follow_pos = desired
        else:
            self.mode = "fps"
        return self.mode
