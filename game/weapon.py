# -*- coding: utf-8 -*-
"""双武器系统（ENG-004 需求 E）：Weapon 基类 + 步枪 MW-1 + 狙击 SR-1。

Weapon 基类抽出弹匣/备弹/换弹/开火循环/view-model 同步框架；
子类以类属性覆盖参数（射速/弹匣/扩散/伤害/部件表）。

步枪 "MW-1"（GDD §2.3，备弹初始 60——需求 G18）：
全自动 7.5 发/s；弹匣 30 / 备弹 60；换弹 1.6s；
扩散 = (0.6° + bloom) × move_mult；bloom 每发 +0.35° 上限 3.0°，
停射 0.25s 后 8°/s 线性收敛；后座经 camera_rig.kick 注入。

狙击 "SR-1"（需求 E11）：伤害 260 躯干 / 650 爆头（×2.5）；
射速 1 发/1.5s；弹匣 5 / 备弹 15；换弹 2.5s；腰射扩散 4.0°、开镜 0.05°。
ADS（需求 E12）：右键按住开镜，ads_t 0→1 阻尼逼近（~0.8s 达 95%），
FOV = lerp(62°, 18°, ads_t)（app 层写 camera_rig.fov）；
ads_t>0.5 隐藏 view-model、准星换狙击样式（HUD "scope" kind）。

view-model（GDD §2.1）：纯视觉部件（cast_shadow=False），无父子层级，
每帧由本类同步到相机局部空间（与 Turret 同步炮管同模式）。
摆感三件套（bob/sway/后座/防穿墙）与枪口火光同 Sprint 2。

切枪（需求 E14）：0.4s raise time 不可开火并取消换弹；
由 app 层持有 weapons 列表并驱动（K_q 循环 / COMBAT 内 K_1/K_2 直选）。
"""
import math
import random

from engine.scene import Entity
from engine.mesh import box_mesh, MeshRenderer
from engine.math_utils import Vec3, dir_from_yaw_pitch, clamp, lerp

from .combat import raycast_world, KNOCKBACK_BOT, KNOCKBACK_SNIPER
from .states import GUN_STATES

# ---- 枪械通用参数 ----
RAISE_TIME = 0.4                 # 切枪 raise time（需求 E14）
WALL_AVOID_DIST = 0.75
BLOOM_PER_SHOT = 0.35
BLOOM_MAX = 3.0
BLOOM_DECAY = 8.0                # °/s（停射 0.25s 后）
BLOOM_IDLE_DELAY = 0.25
RECOIL_PITCH_DEG = 0.35
RECOIL_YAW_DEG = 0.12
ADS_RATE = 3.7443                # 1-exp(-k·0.8)=0.95 → k=ln20/0.8（开镜很慢）
ADS_FOV_HIP = 62.0               # app 层 FOV 插值端点（需求 E12）
ADS_FOV_SCOPE = 18.0

# ---- MW-1 view-model 部件表（GDD §2.1 字面值） ----
_GUN_METAL = (0.40, 0.42, 0.46)
_RIFLE_PARTS = (
    # name,      size,                 offset,              color,        shin,  spec
    ("vm_body",   (0.09, 0.13, 0.52),  (0.22, -0.20, 0.45), _GUN_METAL,        48.0, 0.50),
    ("vm_barrel", (0.05, 0.05, 0.30),  (0.22, -0.17, 0.72), _GUN_METAL,        72.0, 0.70),
    ("vm_grip",   (0.07, 0.16, 0.09),  (0.22, -0.31, 0.38), (0.82, 0.58, 0.34), 12.0, 0.10),
    ("vm_mag",    (0.06, 0.15, 0.08),  (0.22, -0.31, 0.50), (1.00, 0.80, 0.40), 32.0, 0.40),
)
_RIFLE_MUZZLE = (0.22, -0.17, 0.905)    # 枪口（枪管前端再出半个闪光盒）

# ---- SR-1 view-model 部件表（需求 E14：长枪管 + 镜筒 + 暖棕枪托） ----
_SNIPER_PARTS = (
    ("vm_s_body",   (0.09, 0.13, 0.62), (0.22, -0.20, 0.50), _GUN_METAL,        48.0, 0.50),
    ("vm_s_barrel", (0.05, 0.05, 0.50), (0.22, -0.17, 0.95), _GUN_METAL,        72.0, 0.70),
    ("vm_s_scope",  (0.07, 0.07, 0.28), (0.22, -0.10, 0.55), _GUN_METAL,        48.0, 0.50),
    ("vm_s_stock",  (0.08, 0.14, 0.30), (0.22, -0.24, 0.18), (0.82, 0.58, 0.34), 12.0, 0.10),
    ("vm_s_mag",    (0.06, 0.12, 0.08), (0.22, -0.29, 0.48), (1.00, 0.80, 0.40), 32.0, 0.40),
)
_SNIPER_MUZZLE = (0.22, -0.17, 1.20)

_FLASH_COLOR = (1.0, 0.82, 0.45)    # 枪口火光琥珀（Turret.flash 同款）


class Weapon:
    """单件武器逻辑 + view-model 同步；实体在世界构建时创建。

    子类覆盖类属性即可获得全套框架（弹匣/换弹/开火/摆感/同步）。
    """

    # ---- 子类覆盖的参数表 ----
    name = "MW-1"
    fire_interval = 1.0 / 7.5           # 射速间隔（s）
    mag_size = 30
    reserve_size = 60                   # 需求 G18：120 → 60
    reload_time = 1.6
    base_spread_deg = 0.6               # 腰射基础扩散
    ads_spread_deg = None               # 开镜扩散（None = 无 ADS）
    has_ads = False
    is_sniper = False                   # 精英减伤标记（需求 E13）
    damage_body = 26.0                  # 躯干伤害（combat.player_shoot 参数）
    headshot_mult = 2.5                 # 爆头倍率
    recoil_pitch_deg = RECOIL_PITCH_DEG
    recoil_yaw_deg = RECOIL_YAW_DEG
    ads_recoil_scale = 1.0         # 开镜后座系数（1.0=不衰减；狙击为 0.15）
    ads_hip_sens_scale = 1.0       # 开镜鼠标灵敏度系数（狙击为 0.35）
    knockback = KNOCKBACK_BOT     # 命中冲量强度（ENG-007 模块三）
    parts_spec = _RIFLE_PARTS
    muzzle_local = _RIFLE_MUZZLE

    def __init__(self, app):
        self.app = app
        self.scene = app.scene
        self.mag = self.mag_size
        self.reserve = self.reserve_size
        self.reloading = False
        self._reload_t = 0.0
        self._cooldown = 0.0
        self._raise_t = 0.0             # 切枪 raise time（需求 E14）
        self._bloom = 0.0
        self._idle_t = 999.0
        self.ads_t = 0.0                # ADS 进度 0~1（has_ads 时有效）

        # 摆感状态
        self._bob_phase = 0.0
        self._bob_amp = 0.003
        self._sway_x = 0.0
        self._sway_y = 0.0
        self._recoil_off = 0.0
        self._flash_t = 0.0
        self._flash_box_t = 0.0

        # ---- view-model 实体 ----
        self.parts = []
        self._barrel = None
        for name, size, offset, color, shin, spec in self.parts_spec:
            e = Entity(name, "weapon")
            e.renderer = MeshRenderer(box_mesh(*size), color=color,
                                      cast_shadow=False,
                                      shininess=shin, spec_strength=spec)
            e.renderer.visible = False
            self.scene.add(e)
            self.parts.append((e, Vec3(*offset)))
            if name.endswith("barrel"):
                self._barrel = e

        self.muzzle_flash = Entity("vm_muzzle_flash", "weapon")
        self.muzzle_flash.renderer = MeshRenderer(
            box_mesh(0.07, 0.07, 0.07), color=_FLASH_COLOR,
            cast_shadow=False, unlit=True)
        self.muzzle_flash.renderer.visible = False
        self.scene.add(self.muzzle_flash)

    # ------------------------------------------------------------------
    # 换弹 / 补给 / 切枪
    # ------------------------------------------------------------------
    def start_reload(self):
        if (self.reloading or self.mag >= self.mag_size
                or self.reserve <= 0):
            return
        self.reloading = True
        self._reload_t = self.reload_time

    def supply(self):
        """INTERMISSION 补给（需求 G18）：弹药不再补满；HP 由 combat.supply
        负责。不打断进行中的换弹（换弹自然完成，语义与原版一致）。"""
        return

    def raise_weapon(self):
        """切枪（需求 E14）：0.4s raise time，取消换弹。"""
        self._raise_t = RAISE_TIME
        self.reloading = False
        self._reload_t = 0.0

    def set_visible(self, visible):
        """整枪 view-model 显隐（切枪时隐藏非当前武器；
        显示态由每帧 _sync_view_model 按状态自行管理）。"""
        if not visible:
            self.hide_view_model()

    def hide_view_model(self):
        for e, _ in self.parts:
            e.renderer.visible = False
        self.muzzle_flash.renderer.visible = False

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def spread_deg(self):
        """当前扩散角（度）——准星线隙映射的数据源。"""
        if self.has_ads:
            spread = lerp(self.base_spread_deg, self.ads_spread_deg,
                          self.ads_t)
        else:
            spread = self.base_spread_deg
        return (spread + self._bloom) * self._move_mult()

    @property
    def bloom_deg(self):
        """当前 bloom 分量（度），selftest 低扩散开火窗口用。"""
        return self._bloom

    # ------------------------------------------------------------------
    # 每帧
    # ------------------------------------------------------------------
    def update(self, dt):
        app = self.app
        inp = app.input

        # --- 换弹计时 ---
        if self.reloading:
            self._reload_t -= dt
            if self._reload_t <= 0.0:
                need = self.mag_size - self.mag
                take = min(need, self.reserve)
                self.mag += take
                self.reserve -= take
                self.reloading = False

        # --- 切枪 raise time ---
        if self._raise_t > 0.0:
            self._raise_t -= dt

        # --- bloom 收敛 ---
        self._idle_t += dt
        if self._idle_t > BLOOM_IDLE_DELAY and self._bloom > 0.0:
            self._bloom = max(0.0, self._bloom - BLOOM_DECAY * dt)

        # --- ADS（需求 E12：右键按住开镜，阻尼逼近） ---
        if self.has_ads:
            target = 1.0 if inp.is_mouse_down(3) else 0.0
            k = 1.0 - math.exp(-ADS_RATE * dt)
            self.ads_t += (target - self.ads_t) * k

        # --- 开火 ---
        # 状态门用 `GUN_STATES`（states.py 的唯一真源）。曾写成硬编码
        # `("COMBAT", "INTERMISSION")`，加 Boss 关忘了改 -> Boss 战里
        # 扣扳机完全不响。**加新状态只改 states.py。**
        self._cooldown -= dt
        want_fire = inp.is_mouse_down(1) and app.state in GUN_STATES
        if (want_fire and not self.reloading and self._cooldown <= 0.0
                and self._raise_t <= 0.0):
            if self.mag <= 0:
                self.start_reload()
            else:
                self._fire()
                self._cooldown = self.fire_interval
        # 打空自动换弹
        if self.mag <= 0 and not self.reloading and self.reserve > 0:
            self.start_reload()

        self._sync_view_model(dt)

    # ------------------------------------------------------------------
    def _fire(self):
        app = self.app
        rig = app.camera_rig
        self.mag -= 1
        self._idle_t = 0.0

        eye, fwd = rig.eye_and_forward()
        direction = self._cone_deflect(fwd, math.radians(self.spread_deg))
        app.combat.player_shoot(eye, direction,
                                damage_body=self.damage_body,
                                headshot_mult=self.headshot_mult,
                                sniper=self.is_sniper,
                                knockback=self.knockback)

        # 后座：注入相机（回弹由 CameraRig.KICK_RECOVER_RATE 负责）+ 枪体后移
        # 开镜（ads_t→1）时后座按 ads_recoil_scale 衰减：狙击镜已架好，
        # 镜头几乎不晃，这是"开镜提升精准度"的手感来源（ENG-007 模块二）。
        scale = 1.0 - (1.0 - self.ads_recoil_scale) * self.ads_t
        # 回弹变慢让"后坐"停留更久（腰射最明显，开镜时回到 1.0 快速归位）
        rig.kick_recovery_scale(1.0 - 0.45 * scale)
        rig.kick(math.radians(self.recoil_pitch_deg) * scale,
                 math.radians(random.uniform(-self.recoil_yaw_deg,
                                             self.recoil_yaw_deg)) * scale)
        self._recoil_off = 0.03 * (0.4 + 0.6 * scale)
        self._bloom = min(BLOOM_MAX, self._bloom + BLOOM_PER_SHOT)
        self._flash_t = 0.05
        self._flash_box_t = 0.04

    def _cone_deflect(self, fwd, spread_rad):
        """视线方向在 spread 圆锥内均匀随机偏转。"""
        if spread_rad <= 1e-9:
            return fwd
        theta = spread_rad * math.sqrt(random.random())   # 面积均匀
        phi = random.uniform(0.0, math.tau)
        right = fwd.cross(Vec3(0.0, 1.0, 0.0))
        if right.length_sq() < 1e-8:
            right = Vec3(1.0, 0.0, 0.0)
        right = right.normalized()
        up = right.cross(fwd).normalized()
        return (fwd * math.cos(theta)
                + right * (math.sin(theta) * math.cos(phi))
                + up * (math.sin(theta) * math.sin(phi))).normalized()

    def _move_mult(self):
        """移动状态扩散系数（GDD §2.3）：站 1.0 / 走 1.6 / 疾跑 2.2 / 空 2.5。"""
        body = self.app.player.body
        h_speed = math.hypot(body.velocity.x, body.velocity.z)
        if not body.grounded:
            return 2.5
        if h_speed > 6.0:
            return 2.2
        if h_speed > 0.5:
            return 1.6
        return 1.0

    # ------------------------------------------------------------------
    # view-model 同步
    # ------------------------------------------------------------------
    def _sync_view_model(self, dt):
        app = self.app
        rig = app.camera_rig
        visible = (app.state in GUN_STATES
                   and rig.mode == "fps"
                   and not (self.has_ads and self.ads_t > 0.5))
        for e, _ in self.parts:
            e.renderer.visible = visible
        if not visible:
            self.muzzle_flash.renderer.visible = False
            return

        eye, fwd = rig.eye_and_forward()
        right = fwd.cross(Vec3(0.0, 1.0, 0.0))
        if right.length_sq() < 1e-8:
            right = Vec3(1.0, 0.0, 0.0)
        right = right.normalized()
        up = right.cross(fwd).normalized()

        # --- bob：移动/呼吸摆动（趴下时压到 stability 倍，更稳）---
        body = app.player.body
        stab = getattr(app.player, "stability", None)
        stab = stab() if callable(stab) else 1.0
        ratio = math.hypot(body.velocity.x, body.velocity.z) / \
            app.player.run_speed
        target_amp = max(0.003, min(1.0, ratio) * 0.012) * stab
        k = 1.0 - math.exp(-10.0 * dt)
        self._bob_amp += (target_amp - self._bob_amp) * k
        self._bob_phase += 9.0 * dt
        bob_x = math.sin(self._bob_phase) * self._bob_amp
        bob_y = math.sin(self._bob_phase * 2.0) * self._bob_amp * 0.5

        # --- sway：视角滞后（趴下时上限同步收紧）---
        dx, dy = app.input.mouse_delta()
        sway_cap = 0.03 * stab
        tx = clamp(-dx * 0.0015, -sway_cap, sway_cap)
        ty = clamp(dy * 0.0015, -sway_cap, sway_cap)
        ks = 1.0 - math.exp(-14.0 * dt)
        self._sway_x += (tx - self._sway_x) * ks
        self._sway_y += (ty - self._sway_y) * ks

        # --- 后座回位 ---
        kr = 1.0 - math.exp(-18.0 * dt)
        self._recoil_off *= (1.0 - kr)

        # --- 防穿墙后拉 ---
        z_shift = -self._recoil_off
        t, _body = raycast_world(app.physics, eye, fwd, WALL_AVOID_DIST,
                                 include_dynamic=True)
        if t is not None and t < WALL_AVOID_DIST:
            z_shift += (t - 0.1) - WALL_AVOID_DIST

        for e, off in self.parts:
            pos = (eye
                   + right * (off.x + bob_x + self._sway_x)
                   + up * (off.y + bob_y + self._sway_y)
                   + fwd * (off.z + z_shift))
            e.transform.position = pos
            e.transform.yaw = rig.yaw
            e.transform.pitch = rig.pitch

        # --- 枪口火光（unlit 替换，不加灯） ---
        if self._flash_t > 0.0:
            self._flash_t -= dt
            self._barrel.renderer.color = _FLASH_COLOR
        else:
            self._barrel.renderer.color = _GUN_METAL
        if self._flash_box_t > 0.0:
            self._flash_box_t -= dt
            m = self.muzzle_local
            self.muzzle_flash.transform.position = (
                eye + right * m[0] + up * m[1] + fwd * (m[2] + z_shift))
            self.muzzle_flash.transform.yaw = rig.yaw
            self.muzzle_flash.renderer.visible = True
        else:
            self.muzzle_flash.renderer.visible = False


class Rifle(Weapon):
    """步枪 MW-1（GDD §2.3；备弹 60——需求 G18）。"""

    name = "MW-1"
    fire_interval = 1.0 / 7.5
    mag_size = 30
    reserve_size = 60
    reload_time = 1.6
    base_spread_deg = 0.6
    ads_spread_deg = None
    has_ads = False
    is_sniper = False
    damage_body = 26.0
    headshot_mult = 2.5
    parts_spec = _RIFLE_PARTS
    muzzle_local = _RIFLE_MUZZLE


class Sniper(Weapon):
    """狙击 SR-1（需求 E11/E12 字面值）。"""

    name = "SR-1"
    fire_interval = 1.5                 # 1 发 / 1.5s
    mag_size = 5
    reserve_size = 15
    reload_time = 2.5
    base_spread_deg = 4.0               # 腰射扩散
    ads_spread_deg = 0.05               # 开镜扩散
    has_ads = True
    is_sniper = True
    damage_body = 260.0
    headshot_mult = 2.5                 # 爆头 650
    # 狙击专属后座（ENG-007 模块二）：腰射 1.9° 上抬 / ±0.30° 横向，
    # 约为步枪（0.35°/0.12°）的 5 倍 / 2.5 倍；开镜后仅剩 15%（≈0.29°）
    # 且横向抖动几乎归零 —— 栓动 + 高威力需要明显镜头反馈才能卖出"重"。
    recoil_pitch_deg = 1.90
    recoil_yaw_deg = 0.30
    ads_recoil_scale = 0.15
    ads_hip_sens_scale = 0.35         # 开镜时鼠标灵敏度降到 35%（1:1 瞄准）
    # 狙击专属击退：冲量 300 vs 步枪 110，约 2.7 倍。实际位移仍受
    # k/(mass+armor) 约束：普通兵 65kg+25 被推得踉跄，精英 65kg+60 只退半步。
    knockback = KNOCKBACK_SNIPER
    parts_spec = _SNIPER_PARTS
    muzzle_local = _SNIPER_MUZZLE
