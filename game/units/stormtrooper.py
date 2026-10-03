# -*- coding: utf-8 -*-
"""帝国暴风兵（Stormtrooper）：30 部件层叠装甲人形 bot。

设计文档：`docs/design/stormtrooper-design.md`
构建规则：`docs/design/procedural-humanoid-rules.md`

继承结构：`BaseBot`（物理 + FSM + 命中盒契约）→ `HumanoidBot`（人形外观）
→ `Stormtrooper`（层叠装甲外观 + 摆动分组）。BaseBot/HumanoidBot 拆分是为了
让 Boss（奶龙）能共用同一套物理与 AI，只换外观。

**层叠装甲的构造原理**
------------------------
本项目的 `box_mesh` 一个立方体只能整体一个颜色，无法在一个部件上做"白甲+黑缝"。
所以"黑色内衬 + 白色装甲板 + 黑色缝隙"是用**三层不同深度的盒子**表达的：

    core   （黑内衬，z 较浅）  ← 从装甲板边缘露出的"黑缝"
    plate  （白甲，z 较深）    ← 叠在 core 外侧
    detail （黑细节，最浅）    ← 贴在装甲板最前面

实测数据：core 前沿 z=0.12，chest 前沿 z=0.18（外露 0.06m 黑边），
上臂 0.36→0.365 露 0.005m 细缝。

**层叠的"接缝"靠 Z 方向深度差表达，不靠 Y 方向小重叠。**
最初误把"层叠"理解为相邻板 Y 方向只重叠 0.02m。实测发现腹部纵向只有 0.34m
（腰带顶 0.82 → 胸甲底 1.16），塞不下"两片腹甲 + 三处小重叠"——求解器穷举
返回 0 组可行解，数学上无解。实际暴风兵腹甲本就是**大幅交叠**的整块，
故改为 Y 方向正常交叠、Z 方向递减表达前后层次。

**关键：摆动逻辑已重写**
父类 `_sync_parts` 靠 `leg_l/leg_r/arm_l/arm_r` 四个 key 分发 pitch，
本表把它们细分为 `thigh/knee/shin` 与 `upperarm/forearm/hand`，
因此**不能**直接复用父类的摆动分支 —— 见本文件 `ANIM_GROUPS` 与
`_sync_parts` 覆写。物理体/命中盒/FSM AI/伤害/死亡侧倒仍全部继承。

**draw call**：26 部件 / 3 投影 → 同屏 5 个 = 160，同屏 12 个 = 384。
实测（Intel 核显 1280×720）：同屏 5 = 7.89ms、同屏 15 = 11.10ms、
同屏 30 = 15.87ms，均在 60 FPS 预算（16.7ms）内。
"""
import math

from ..bots import HumanoidBot, _rot_local
from engine.mesh import box_mesh, MeshRenderer
from engine.math_utils import Vec3

# ---- 配色（docs/art/visual-style.md §0 pastel 三原则）----
# 白色装甲：不用纯白 (1,1,1)，避免高光下失去层次。明度 0.88，通道差 0.02。
_SW_WHITE = (0.88, 0.88, 0.86)
# 黑色内衬/缝隙：明度 0.14，**远低于 pastel 下限 0.60**。
#
# 为什么这么暗：着色器是 `albedo * u_ambient`（乘法），base color 线性压低结果，
# 但**感知被 gamma 2.2 强烈压缩** —— linear 0.256 只显示成约 0.54。
# 所以"看起来不够深"不是错觉：实测 base 0.50 → 渲染 0.648（灰不拉几），
# 要真正读成"黑"必须压到 0.12 附近。渲染后实测（正面视角，暗部平均亮度）：
#   base 0.50 -> 0.648   base 0.26 -> 0.526   base 0.16 -> 0.454
#   base 0.11 -> 0.409   base 0.07 -> 0.365   base 0.04 -> 0.318
# 选 0.12 而不选 0.07/0.04：再深就成了"洞"，低多边形角色靠明暗梯度读形体，
# 纯黑会让面罩/腰带失去体积感。
#
# spec 实测**不影响暗部深浅**（spec 0.15 与 0.70 渲染结果相同），
# 保留 0.70 只为让暗件仍读作金属。通道差 0.02 仍满足 pastel 约束。
# 这是全项目唯一一处对三原则的显式偏离，改动前请先读设计文档 §3。
_SW_DARK = (0.12, 0.12, 0.14)

# ---- 26 部件表（全部数值经 t_geo 验算：贴地/层叠/不穿模/对称/命中盒）----
# key, 尺寸 (w,h,d),        偏移 (x右,y上,z前),  颜色,        shin, spec, shadow
# 坐标：y 以脚底为 0；z 正 = 角色前方（yaw=0 时面朝 -Z）
PARTS = (
    # ---- 头部 3：头盔 + 深色面罩 + 呼吸栅格 ----
    ("head",       (0.34, 0.34, 0.30), (0.00, 1.66, 0.00),  _SW_WHITE, 24.0, 0.20, True),
    ("visor",      (0.30, 0.09, 0.02), (0.00, 1.71, 0.15),  _SW_DARK,  72.0, 0.70, False),
    ("respirator", (0.15, 0.09, 0.03), (0.00, 1.55, 0.15),  _SW_DARK,  72.0, 0.70, False),
    # ---- 躯干 5：黑色内衬 core + 白色胸甲 + 胸口细节 + 两层腹甲 ----
    # 注意 chest 的 key 刻意叫 "torso"：父类 bots.py 的 update() 在 SPAWN
    # 入场态硬编码访问 self.parts["torso"][0] 做闪烁，_sync_points 也按
    # ("head","torso") 提亮。用 torso 命名即可零改动复用父类逻辑，
    # 不必加别名 hack（别名会让部件计数虚增 1）。
    # core = 黑色内衬（"皮肤"），必须从髋到肩**连续覆盖**，否则腰腹会出现
    # 穿透空洞。实测（原 h=0.60 @y=1.18 -> y 0.88~1.48）有两处漏光：
    #   y 0.72~0.73  髋甲顶(0.72) 与 腰带底(0.74) 之间 2cm 空档
    #   y 0.82~0.84  腰带顶(0.82) 与 下腹甲底(0.85) 之间 3cm 空档
    # 正对角色打射线会直接看穿到背后的背景。改为 h=0.80 @y=1.08 -> 0.68~1.48，
    # 把两处空档全填成黑色内衬（正好读作"铠甲下的黑色紧身衣"）。
    # 宽度保持 0.40 < 腰带 0.44 / 腰胯甲 0.46，故腰腹仍由白甲在外圈包住。
    ("core",       (0.40, 0.80, 0.24), (0.00, 1.08, 0.00),  _SW_DARK,  20.0, 0.15, True),
    ("torso",      (0.54, 0.34, 0.30), (0.00, 1.33, 0.03),  _SW_WHITE, 20.0, 0.15, True),
    ("chest_dtl",  (0.16, 0.11, 0.02), (0.00, 1.31, 0.19),  _SW_DARK,  72.0, 0.70, False),
    ("ab_upper",   (0.46, 0.16, 0.29), (0.00, 1.08, 0.02),  _SW_WHITE, 20.0, 0.15, False),
    ("ab_lower",   (0.43, 0.14, 0.28), (0.00, 0.92, 0.02),  _SW_WHITE, 20.0, 0.15, False),
    # ---- 腰部 3：黑色腰带 + 战术装备盒 + 白色腰胯甲 ----
    ("belt",       (0.44, 0.08, 0.30), (0.00, 0.78, 0.00),  _SW_DARK,  20.0, 0.15, False),
    ("belt_pouch", (0.13, 0.11, 0.07), (0.17, 0.76, 0.16),  _SW_DARK,  20.0, 0.15, False),
    ("hip",        (0.46, 0.20, 0.32), (0.00, 0.62, 0.01),  _SW_WHITE, 20.0, 0.15, False),
    # ---- 肩甲 2：宽大圆润肩甲 ----
    # 几何约束（屏幕投影实测得出）：y 决定"坐"在上臂上还是浮空。
    #   上臂顶 y = 1.30 + 0.135 = 1.435
    #   y=1.58 -> 肩底 1.48 -> **浮空 0.045m**（渲染图上肩甲明显脱离手臂）
    #   y=1.50 -> 肩底 1.40 -> 压住上臂 0.035m（正确：肩甲"扣"在肩关节上）
    # 肩甲与上臂在 X/Z 上**有意交叠**（0.075m / 全深）——肩甲本就是罩住
    # 肩关节的护盖，不交叠反而不对。这是全表唯一允许三轴交叠的组合，
    # 其余各对仍须满足第 6 节的"不穿模"。
    ("shoulder_l", (0.22, 0.20, 0.30), (-0.48, 1.50, 0.00), _SW_WHITE, 72.0, 0.70, False),
    ("shoulder_r", (0.22, 0.20, 0.30), (0.48, 1.50, 0.00),  _SW_WHITE, 72.0, 0.70, False),
# ---- 手臂 6 + 黑色内衬 2 ----
    # 四肢原本**完全没有黑色内衬**，所以关节处一点黑都露不出来
    # （实测暗部占比：正面 13.2% / 侧面 0.1%，整体读作"一坨白"）。
    # 修法 = 加内衬 **且** 缩短白甲留缝，两件事缺一不可：
    #   只加内衬 -> 内衬比白甲窄，被更宽的白甲完全挡住，还是看不见；
    #   只留缝   -> 四肢没有黑色底，留缝就是空洞（正面射线会直接打穿）。
    # 内衬比白甲窄（宽 0.15 < 上臂 0.17 / 前臂 0.16，厚 0.18 < 0.20 / 0.19），
    # 因此**只在白甲之间的缝隙里露出**，不会从侧面凸出。
    # 上臂顶 1.435 与前臂顶 1.12 之间留 0.095m 缝 -> 肘部读作一圈黑色关节。
    ("arm_core_l",  (0.15, 0.55, 0.18), (-0.36, 1.125, 0.00), _SW_DARK,  20.0, 0.15, False),
    ("arm_core_r",  (0.15, 0.55, 0.18), (0.36, 1.125, 0.00),  _SW_DARK,  20.0, 0.15, False),
    ("upperarm_l",  (0.17, 0.22, 0.20), (-0.36, 1.325, 0.00), _SW_WHITE, 20.0, 0.15, False),
    ("upperarm_r",  (0.17, 0.22, 0.20), (0.36, 1.325, 0.00),  _SW_WHITE, 20.0, 0.15, False),
    ("forearm_l",   (0.16, 0.20, 0.19), (-0.36, 1.020, 0.02), _SW_WHITE, 20.0, 0.15, False),
    ("forearm_r",   (0.16, 0.20, 0.19), (0.36, 1.020, 0.02),  _SW_WHITE, 20.0, 0.15, False),
    ("hand_l",      (0.14, 0.13, 0.16), (-0.36, 0.890, 0.03), _SW_WHITE, 20.0, 0.15, False),
    ("hand_r",      (0.14, 0.13, 0.16), (0.36, 0.890, 0.03),  _SW_WHITE, 20.0, 0.15, False),
    # ---- 腿 6 + 黑色内衬 2 ----
    # 内衬 y 0.00~0.70；宽 0.17 < 大腿 0.21 / 膝 0.20 / 小腿 0.19，
    # 厚 0.20 < 0.24 / 0.23 / 0.22，故只在膝上/膝下的缝隙露出。
    # 大腿底 0.43 与膝甲顶 0.37 间留 0.06m；膝甲底 0.25 与小腿顶 0.20 间留 0.05m。
    ("leg_core_l",  (0.17, 0.70, 0.20), (-0.15, 0.350, 0.00), _SW_DARK,  20.0, 0.15, False),
    ("leg_core_r",  (0.17, 0.70, 0.20), (0.15, 0.350, 0.00),  _SW_DARK,  20.0, 0.15, False),
    ("thigh_l",     (0.21, 0.26, 0.24), (-0.15, 0.560, 0.00), _SW_WHITE, 20.0, 0.15, False),
    ("thigh_r",     (0.21, 0.26, 0.24), (0.15, 0.560, 0.00),  _SW_WHITE, 20.0, 0.15, False),
    ("knee_l",      (0.20, 0.12, 0.23), (-0.15, 0.310, 0.01), _SW_WHITE, 72.0, 0.70, False),
    ("knee_r",      (0.20, 0.12, 0.23), (0.15, 0.310, 0.01),  _SW_WHITE, 72.0, 0.70, False),
    # shin y=0.10 h=0.20 -> 底边 0.00 贴地
    ("shin_l",      (0.19, 0.20, 0.22), (-0.15, 0.100, 0.00), _SW_WHITE, 20.0, 0.15, False),
    ("shin_r",      (0.19, 0.20, 0.22), (0.15, 0.100, 0.00),  _SW_WHITE, 20.0, 0.15, False),

    # ---- 武器 1：深灰爆能步枪（长轴在 z，枪口朝前）----
    ("gun",        (0.14, 0.16, 0.86), (0.34, 1.16, 0.38),  _SW_DARK,  72.0, 0.70, False),
)

# ---- 摆动分组（父类的 leg_l/arm_l 分发不适用，此表为暴风兵专用）----
# 每组内所有部件吃同一个 pitch 增量；左右取相反符号形成交替迈步。
# 幅度沿用父类：腿 25°、臂 8°（knee/shin 递减模拟关节弯曲）。
ANIM_GROUPS = (
    ("thigh_l", 25.0, 1.00), ("thigh_r", 25.0, -1.00),
    ("knee_l", 12.0, 1.00), ("knee_r", 12.0, -1.00),
    ("shin_l", 8.0, 1.00), ("shin_r", 8.0, -1.00),
    ("upperarm_l", 8.0, -1.00), ("upperarm_r", 8.0, 1.00),
    ("forearm_l", 4.0, -1.00), ("forearm_r", 4.0, 1.00),
    # 黑色内衬必须**跟随它包裹的那条肢体**摆动，否则白甲一转、黑内衬留在
    # 原处，肘/膝就裂开。幅度与所属肢段完全一致。
    ("leg_core_l", 25.0, 1.00), ("leg_core_r", 25.0, -1.00),
    ("arm_core_l", 8.0, -1.00), ("arm_core_r", 8.0, 1.00),
    # hand/shoulder/head/torso/腰腿其余不动
)

# 枪口火光 / 受击提亮的目标部件
# "torso" 即白色胸甲（命名对齐父类，见 PARTS 注释）
GUN_KEY = "gun"
FLASH_KEYS = ("head", "core", "torso")


class Stormtrooper(HumanoidBot):
    """暴风兵：26 部件层叠装甲人形 bot。

    与父类的差异：外观 + 摆动分组。**不要**覆写 update / take_damage /
    _die —— 那些逻辑与兵种无关。
    """

    palette = {"white": _SW_WHITE, "dark": _SW_DARK}

    def __init__(self, scene, physics, app, pos_xz, hp, elite,
                 wave_damage, wave_err, wave_index):
        super().__init__(scene, physics, app, pos_xz, hp, elite,
                         wave_damage, wave_err, wave_index)
        self._rebuild_parts(scene)

    # ------------------------------------------------------------------
    def _rebuild_parts(self, scene):
        """按部件表重建视觉层。

        `BaseBot.__init__` 已按 `HumanoidBot.SPEC` 建好 7 个普通兵部件，
        这里移除它们、按本表重建。物理体与 FSM 状态不动。
        """
        for key in list(self.parts.keys()):
            scene.remove(self.parts[key][0])
        self.parts.clear()

        for key, size, off, color, shin, sp, shadow in PARTS:
            e = self._make_part(scene, key, size, off, color, shin, sp,
                                shadow)
            self.parts[key] = [e, Vec3(*off), color]

        self._sync_parts(0.0)

    @staticmethod
    def _make_part(scene, key, size, off, color, shin, sp, shadow):
        from engine.scene import Entity
        e = Entity("sw_" + key, "bot_part")
        e.renderer = MeshRenderer(box_mesh(*size), color=color,
                                  cast_shadow=shadow,
                                  shininess=shin, spec_strength=sp)
        scene.add(e)
        return e

    # ------------------------------------------------------------------
    def _sync_parts(self, dt):
        """覆写父类版：26 部件的摆动分组与受击提亮目标都不同。

        逻辑与父类一致（位置/朝向/roll/death 侧倒），
        仅"pitch 分发"与"提亮目标"两处按新部件表重做。
        """
        base = self.base_pos()
        roll = 0.0
        squash = 1.0
        if self.dead:
            p = min(1.0, self._death_t / 0.25)
            roll = p * math.radians(90.0)
            squash = 1.0 - 0.85 * p

        # 摆动角：sin(相位) 为 -1..1 的往复量，左右取反形成交替迈步。
        # 分组幅度递减（thigh 25° > knee 12° > shin 8°）模拟关节链传导。
        wave = math.sin(self._walk_phase)
        swing = {}
        for key, amp_deg, sign in ANIM_GROUPS:
            swing[key] = sign * math.radians(amp_deg) * wave

        if self._hit_flash_t > 0.0:
            self._hit_flash_t -= dt
            brighten = 1.25
        else:
            brighten = 1.0
        if self._flash_t > 0.0:
            self._flash_t -= dt
            gun_color = (1.0, 0.82, 0.45)     # 琥珀枪口火光
        else:
            gun_color = _SW_DARK

        for key, (e, off, color) in self.parts.items():
            o = off
            if self.dead:
                cr, sr = math.cos(roll), math.sin(roll)
                o = Vec3(off.x * cr - off.y * sr,
                         (off.x * sr + off.y * cr) * squash,
                         off.z)
            e.transform.position = base + _rot_local(o, self.yaw)
            e.transform.yaw = self.yaw
            e.transform.roll = roll
            e.transform.pitch = swing.get(key, 0.0)

            if key == GUN_KEY:
                e.renderer.color = gun_color
                m = _rot_local(Vec3(off.x, off.y, off.z + 0.45), self.yaw)
                self._muzzle = base + m
            elif key in FLASH_KEYS:
                e.renderer.color = tuple(min(1.0, c * brighten)
                                         for c in color)

    # ------------------------------------------------------------------
    def part_bounds(self):
        """{key: (x0,x1, y0,y1, z0,z1)} 世界轴对齐包围盒（脚底系）。

        用于验证贴地/层叠/不穿模等硬约束。
        注：跳过 "torso" 别名（它与 chest 指向同一实体，不算独立部件）。
        """
        out = {}
        sizes = {p[0]: p[1] for p in PARTS}
        for key, (e, off, _c) in self.parts.items():
            size = sizes.get(key)
            if size is None:
                continue
            out[key] = (off.x - size[0] * 0.5, off.x + size[0] * 0.5,
                        off.y - size[1] * 0.5, off.y + size[1] * 0.5,
                        off.z - size[2] * 0.5, off.z + size[2] * 0.5)
        return out
