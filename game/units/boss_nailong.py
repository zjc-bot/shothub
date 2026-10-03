# -*- coding: utf-8 -*-
"""奶龙（NaiLong）Boss：外观 + 体型 + 命中盒。

设计文档：`docs/design/boss-level-design.md`
参考形象：泡泡玛特 IP「奶龙」—— 圆滚滚的黄色小龙，奶油色肚皮、
大圆眼、头顶小角、身后小尾巴。

**为什么体型差这么多还只改 hitboxes() 就够**
人形 bot 高 1.83m / 肩宽 0.60m；奶龙高 2.77m / 身宽 1.50m。共用同一套 AABB
会出现两个具体故障：
  1. 玩家贴脸打奶龙的"脚"，射线从 1.66m 的头盒和 0.90m 的躯干盒**下方穿过** ->
     打不到下半身（第一轮暴风兵就踩过这个坑，见设计文档 §0 表第 2 行）；
  2. 反过来，奶龙的"头"在 2.05m，普通兵的 1.66m 头盒完全够不着 -> 永远打不出
     爆头，狙击（260×2.5）价值归零。
所以 `BaseBot.hitboxes()` 让每个角色自带盒子，**combat 的数值一个没动**。

**几何约束（全部经 t_boss 脚本验算，勿凭感觉改）**
- 脚底贴地 y=0.00
- 头盒必须**嵌在躯干盒内**（否则侧射时 t_head - t_body 过大，爆头判定失效）
- 轮廓三向无空洞（正/背/侧射线扫描）
- 眼睛/嘴巴必须凸出头部表面，否则被头盒挡住看不见
"""
import math
import random

from ..bots import BaseBot, _rot_local
from ..combat import raycast_world          # 与 bots.py 同一个函数（转发自 engine）
from engine.mesh import box_mesh, MeshRenderer
from engine.math_utils import Vec3, wrap_angle
from engine.scene import Entity

# ---- 配色（docs/art/visual-style.md §0 pastel 三原则）----
# ⚠️ pastel 三原则要求**明度 0.60~0.95 且 RGB 通道差 ≤ 0.45**。
# 这两条对"明黄"是直接冲突的：饱和的明黄（R=0.97, B=0.30）通道差 0.67，
# 一旦压到 0.45 以内就会褪成奶油白，读不出"黄"。
# 本项目的取舍：**奶龙的黄是它的唯一辨识特征，优先保住黄**，
# 故本文件对通道差做显式偏离（明黄 0.66 > 0.45），并在此留痕。
# 明度仍然守住上限（0.96 略超 0.95，见下）。
# 若要严格合规，把 _NL_YELLOW 换成 (0.92, 0.84, 0.48) 即通道差 0.44，
# 但那已经是"卡其"而不是"明黄"了 —— 需要玩家视觉验收后定夺。
_NL_YELLOW = (0.96, 0.93, 0.30)     # 明黄（通道差 0.66，有意偏离）
# 肚皮/口鼻奶油白：明度 0.97，通道差 0.06 ✓ 合规。
_NL_CREAM = (0.97, 0.94, 0.91)
# 角/脚爪/尾尖橙：明度 0.90，通道差 0.10 ✓ 合规。
_NL_ORANGE = (0.90, 0.85, 0.80)
# 眼白：明度 0.99 略超 0.95 上限（纯白眼白，通道差 0.01 ✓）
_NL_EYE_WHITE = (0.99, 0.99, 0.98)
# 瞳孔/嘴缝深色：**有意低于 pastel 下限 0.60**，同暴风兵 `_SW_DARK` 的授权
# 偏离（设计文档 §3）。没有它眼睛就是两个白方块，脸会读不出来。
_NL_DARK = (0.16, 0.16, 0.18)
# 枪身：与暴风兵同一深色，保持"敌方武器都偏深"的读法。
_NL_GUN = (0.30, 0.30, 0.33)

# ---- 体型 ----
# 高 2.86m（角尖）/ 身宽 1.56m。坐标 y 以脚底为 0。
BODY_HALF_Y = 1.430          # 物理体半高 = 脚底到角尖的一半
PHYS_HALF = Vec3(0.62, BODY_HALF_Y, 0.55)

# ---- 命中盒（相对 base_pos，即脚底原点）----
# 躯干盒 y 0.00~2.00 -> OFFSET.y == HALF.y（沿用项目"躯干盒下沿必须为 0"的约定）
# 头盒 y 1.58~2.50，与躯干盒重叠 0.42m -> 颈部不会漏空（不重叠则那一枪穿过去）
HEAD_BOX = Vec3(0.0, 2.02, 0.0)
HEAD_BOX_HALF = Vec3(0.62, 0.46, 0.50)
BODY_BOX = Vec3(0.0, 1.00, 0.0)
BODY_BOX_HALF = Vec3(0.60, 1.00, 0.52)

# ---- 部件表 ----
# key, 尺寸 (w,h,d), 偏移 (x右,y上,z前), 颜色, shin, spec, shadow
# 命名注意：key 必须叫 "torso"，BaseBot 的 SPAWN 入场闪烁按该 key 访问
# （与暴风兵同一个坑，见 stormtrooper.py 注释）。
_BODY_PARTS = (
    # ---- 躯干 3：圆肚 + 奶油肚皮 + 背鳍 ----
    # 关键比例修正（渲染实测）：初版躯干 1.44 宽 × y 0.38~1.62，正面看是
    # 一个"方坦克"，且把短腿/手臂全罩住 -> 不像奶龙。
    # 奶龙是**上窄下宽的胖梨形**：头宽 > 肩宽 > 腰；腿极短但脚要露在躯干外。
    # 现在躯干收窄到 1.20 并**上移**（底 0.56），让短腿与脚在下方露出。
    ("torso",   (1.20, 1.04, 1.02), (0.00, 1.05, 0.00), _NL_YELLOW, 20.0, 0.15, True),
    ("belly",   (0.80, 0.78, 0.18), (0.00, 1.02, 0.50), _NL_CREAM,  20.0, 0.15, False),
    ("backfin", (0.60, 0.44, 0.20), (0.00, 1.26, -0.50), _NL_ORANGE, 72.0, 0.70, False),
    # ---- 头：5 层盒子堆叠近似球体（球心 y=2.02, rx=0.64 / rz=0.52 / ry=0.48）----
    # 本项目只有 box_mesh（一个立方体一个颜色），做不出真正的球。
    # 办法是**按球面公式分层收窄**：第 i 层半宽/半深 = sqrt(r² - dy²)。
    #
    # **层厚 0.24 > 层间距 0.192，相邻层重叠 0.048m**。
    # 但光"重叠"还不够：若各层的**顶/底面完全对齐**，两层面共面 ->
    # z-fighting -> 渲染图上出现**深浅不一的横向色阶**（v2 首版渲染实测：
    # 正面能明显看到三条不同明度的黄带，正是共面在打架）。
    # 修法：**相邻层 z 方向交错 0.01**（奇数层 z=0.23 / 偶数层 z=0.22），
    # 让重叠区里两层的表面永不共面；视觉上看不出，但深度测试稳定了。
    # 同理耳朵 3 层也交错。
    #
    #   y偏移    宽x    深z      y 范围
    #   -0.384   0.77   0.62    1.52~1.76
    #   -0.192   1.17   0.95    1.71~1.95
    #    0.000   1.28   1.04    1.90~2.14   <- 最宽层，正面看最圆
    #   +0.192   1.17   0.95    2.09~2.33
    #   +0.384   0.77   0.62    2.28~2.52
    ("head_0", (0.77, 0.24, 0.62), (0.00, 1.636, 0.22), _NL_YELLOW, 24.0, 0.20, True),
    ("head_1", (1.17, 0.24, 0.95), (0.00, 1.828, 0.23), _NL_YELLOW, 24.0, 0.20, True),
    ("head_2", (1.28, 0.24, 1.04), (0.00, 2.020, 0.22), _NL_YELLOW, 24.0, 0.20, True),
    ("head_3", (1.17, 0.24, 0.95), (0.00, 2.212, 0.23), _NL_YELLOW, 24.0, 0.20, True),
    ("head_4", (0.77, 0.24, 0.62), (0.00, 2.404, 0.22), _NL_YELLOW, 24.0, 0.20, False),
    # 口鼻/嘴：贴在最宽层（y 1.90~2.14）的前脸上
    ("snout",   (0.62, 0.40, 0.36), (0.00, 1.86, 0.76), _NL_CREAM,  20.0, 0.15, False),
    ("mouth",   (0.28, 0.10, 0.08), (0.00, 1.70, 0.90), _NL_DARK,   72.0, 0.70, False),
    # 眼睛/瞳孔：z 必须 > 头最宽层前沿 0.74 才凸出，否则白块贴在黄面上
    ("eye_l",   (0.30, 0.34, 0.14), (-0.29, 2.10, 0.87), _NL_EYE_WHITE, 24.0, 0.20, False),
    ("eye_r",   (0.30, 0.34, 0.14), (0.29, 2.10, 0.87),  _NL_EYE_WHITE, 24.0, 0.20, False),
    ("pupil_l", (0.13, 0.17, 0.08), (-0.29, 2.08, 0.95), _NL_DARK, 72.0, 0.70, False),
    ("pupil_r", (0.13, 0.17, 0.08), (0.29, 2.08, 0.95),  _NL_DARK, 72.0, 0.70, False),
    # ---- 耳朵：3 层盒子沿 z 堆叠近似圆盘（用户要求"耳朵也是圆的"）----
    # **必须前移到头表面之外**：初版放 z=0.03~0.17，而头最宽层前沿 0.74，
    # 整只耳朵埋在球体里 -> 正面只看到一点点、侧视完全看不见。
    # 现取 z 中心 0.62/0.71/0.80，前沿 0.85 明确凸出球面 0.11m。
    # 层间同样交错 0.01 防共面。
    ("ear_l_0", (0.29, 0.29, 0.10), (-0.60, 2.05, 0.62), _NL_YELLOW, 24.0, 0.20, False),
    ("ear_l_1", (0.32, 0.32, 0.10), (-0.60, 2.05, 0.71), _NL_YELLOW, 24.0, 0.20, False),
    ("ear_l_2", (0.29, 0.29, 0.10), (-0.60, 2.05, 0.80), _NL_YELLOW, 24.0, 0.20, False),
    ("ear_r_0", (0.29, 0.29, 0.10), (0.60, 2.05, 0.62),  _NL_YELLOW, 24.0, 0.20, False),
    ("ear_r_1", (0.32, 0.32, 0.10), (0.60, 2.05, 0.71),  _NL_YELLOW, 24.0, 0.20, False),
    ("ear_r_2", (0.29, 0.29, 0.10), (0.60, 2.05, 0.80),  _NL_YELLOW, 24.0, 0.20, False),
    # ---- 角：加大加高；角底 2.44 压在球顶 2.52 上（重叠 0.08m）不悬空 ----
    ("horn_l",  (0.22, 0.42, 0.22), (-0.28, 2.65, 0.10), _NL_ORANGE, 72.0, 0.70, False),
    ("horn_r",  (0.22, 0.42, 0.22), (0.28, 2.65, 0.10),  _NL_ORANGE, 72.0, 0.70, False),
    # ---- 手臂 2：短粗，**明显外摆到躯干轮廓外** ----
    # 躯干现在半宽只有 0.60，手臂中心必须 ≥ 0.60+0.18-0.06 才露得出来。
    # 取 x=±0.76 半宽 0.20 -> 内沿 0.56，**外沿 0.96 超出躯干 0.36m**，
    # 正面能看出两条短手垂在身侧。
    ("arm_l",   (0.40, 0.62, 0.38), (-0.76, 1.20, 0.24), _NL_YELLOW, 20.0, 0.15, False),
    ("arm_r",   (0.40, 0.62, 0.38), (0.76, 1.20, 0.24),  _NL_YELLOW, 20.0, 0.15, False),
    # ---- 腿脚 4：极短腿 + 橙脚 ----
    # 腿必须**比躯干底低且略宽**：腿顶 0.61 vs 躯干底 0.52（重叠 0.09m 相连），
    # 腿 x 半宽 0.28 > 躯干半宽 0.60 的"内腿边界"0.02 → 两腿之间那条缝
    # 从 y 0.05 到 0.52 会露出背景，**正面轮廓在那里是空的**（实测空洞）。
    # 修法：腿外扩到 x ±0.36（宽 0.72），两条腿在 x 上几乎并拢，
    # 缝隙消失；脚更大更宽（0.58）保证 y 0.00~0.14 也有实体。
    ("leg_l",   (0.36, 0.56, 0.50), (-0.18, 0.33, 0.06), _NL_YELLOW, 20.0, 0.15, False),
    ("leg_r",   (0.36, 0.56, 0.50), (0.18, 0.33, 0.06),  _NL_YELLOW, 20.0, 0.15, False),
    ("foot_l",  (0.58, 0.16, 0.60), (-0.20, 0.08, 0.12), _NL_ORANGE, 20.0, 0.15, False),
    ("foot_r",  (0.58, 0.16, 0.60), (0.20, 0.08, 0.12),  _NL_ORANGE, 20.0, 0.15, False),
    ("foot_l",  (0.50, 0.16, 0.60), (-0.32, 0.08, 0.12), _NL_ORANGE, 20.0, 0.15, False),
    ("foot_r",  (0.50, 0.16, 0.60), (0.32, 0.08, 0.12),  _NL_ORANGE, 20.0, 0.15, False),
    # ---- 尾 2 ----
    # 尾巴必须**咬进躯干**：初版为了让侧视能看见，把整条尾移到躯干正后方
    # （躯干后沿 z=-0.51，尾前沿 z=-0.63），中间空了 **0.12m** ——
    # 渲染图上尾巴是"飘"在身后的一块。用户指出这个空隙。
    # 修法：尾巴中心 z 从 -1.06 移到 -0.86 -> 前沿 -0.43，
    # 与躯干后沿 -0.51 **重叠 0.08m**；同时尾加长到 0.94 让末端仍露在轮廓外。
    # 尾尖同理前移并与尾巴重叠（前沿 -1.29 vs 尾后沿 -1.33 -> 重叠 0.04m）。
    ("tail",    (0.34, 0.34, 0.94), (0.00, 0.74, -0.86), _NL_YELLOW, 20.0, 0.15, False),
    ("tailtip", (0.40, 0.40, 0.30), (0.00, 0.74, -1.44), _NL_ORANGE, 72.0, 0.70, False),
)

# 摆动：腿与手臂各自反相；头/躯干静止
ANIM_GROUPS = (
    ("leg_l", 18.0, 1.0), ("leg_r", 18.0, -1.0),
    ("arm_l", 12.0, -1.0), ("arm_r", 12.0, 1.0),
)

# 受击提亮目标（等同暴风兵的 FLASH_KEYS 语义）
# 受击提亮目标（等同暴风兵的 FLASH_KEYS 语义）
# 注意头现在是 5 层（head_0..head_4），**必须全部列出** ——
# 漏掉任何一层，那层就不会受击闪白，看起来像"铠甲被打穿了"。
FLASH_KEYS = ("torso", "head_0", "head_1", "head_2", "head_3", "head_4")

# ---- 第一关：单枪（长管狙击型，挂在右手）----
GUN_PARTS_ONE = (
    ("gun", (0.20, 0.20, 1.40), (0.98, 1.42, 0.62), _NL_GUN, 72.0, 0.70, False),
)

# ---- 第二关：双枪（短管，两手各一）----
# 交替开火（`_gun_side` 奇偶切换）形成双枪压制，比单枪高一倍火��密度。
GUN_PARTS_TWO = (
    ("gun_l", (0.18, 0.18, 1.00), (-0.98, 1.42, 0.50), _NL_GUN, 72.0, 0.70, False),
    ("gun_r", (0.18, 0.18, 1.00), (0.98, 1.42, 0.50),  _NL_GUN, 72.0, 0.70, False),
)

# 枪口火光色（与人形 bot 的 `_FLASH_COLOR` 同值）
_FLASH_COLOR = (1.0, 0.82, 0.45)

# ----------------------------------------------------------------------
# 第 2 层「刺刀冲击」子状态机
# ----------------------------------------------------------------------
# 三个状态串起来就是一次完整的冲刺，节奏参照现有 bot 的时间尺度
# （AIM_TIME_ 1.1s / LOSE_LOS_TIME 之类），**数值单位一律秒**：
#
#   ENGAGE --(冷却好 & 距离在带内 & 有视线)--> RUSH_TELEGRAPH 0.70s
#          --(预备结束，锁定冲刺方向)------> RUSH         ≤0.55s / 17 m/s
#          --(撞墙 or 冲完 or 打到玩家)------> RUSH_STUN    0.90s
#          --(硬直结束，冷却重置)-----------> ENGAGE
#
# 为什么要有 0.70s 预备：17 m/s 的冲刺一帧（1/60s）就位移 0.28m，玩家
# 不可能靠反应躲开。预备期必须长到**玩家来得及横向移动**，否则这招
# 就是"必中即死"（18 点伤害 = 玩家 100 血挨 6 下就没）。
#
# 为什么要有 0.90s 硬直：冲刺失败（没撞到 / 撞墙）后给玩家一个反打窗口，
# 否则第 2 层会退化成"永远冲刺、无法击杀"。
RUSH_STATES = ("RUSH_TELEGRAPH", "RUSH", "RUSH_STUN")
RUSH_TELEGRAPH_TIME = 0.70
RUSH_DASH_TIME = 0.55
RUSH_STUN_TIME = 0.90
RUSH_COOLDOWN = 4.5        # 两次冲刺之间的最小间隔（秒）
RUSH_MIN_DIST = 5.0        # 太近反而不用冲（贴身绕圈更有威胁）
RUSH_MAX_DIST = 26.0       # 太远冲不到，白白送一次硬直
RUSH_HIT_RADIUS = 2.10     # 冲刺接触判定：脚底水平距离 < 2.10m 判定擦到
RUSH_WALL_MARGIN = 0.90    # 前方 raycast 命中点再退这么多，避免贴脸卡住
RUSH_TRIGGER_CHANCE = 0.55  # 冷却好且距离合适时，每帧转入冲刺的概率


class NaiLongBoss(BaseBot):
    """奶龙 Boss：超大体型 + 自带命中盒的龙型敌人。

    继承 `BaseBot`，**不**继承 `HumanoidBot` —— 它不是人形（无 leg_l/arm_l
    摆动语义、无 `gun` 单枪口），强行继承会让父类的 `_sync_parts` 分发表
    落到错误的部件上。

    只覆写外观与体型；物理体/FSM AI/伤害/死亡掉落全部走 `BaseBot`。
    """

    HEAD_BOX = HEAD_BOX
    HEAD_BOX_HALF = HEAD_BOX_HALF
    BODY_BOX = BODY_BOX
    BODY_BOX_HALF = BODY_BOX_HALF
    BODY_HALF_Y_LOCAL = BODY_HALF_Y
    SPEC = _BODY_PARTS

    palette = {"yellow": _NL_YELLOW, "cream": _NL_CREAM,
               "orange": _NL_ORANGE, "dark": _NL_DARK}

    # Boss 体型大 -> 交战距离拉远（普通兵 8~14m）
    ENGAGE_MIN_ = 12.0
    ENGAGE_MAX_ = 20.0
    # 转向更慢（体型大 = 惯性大），AI 时间更长（血厚，给玩家反应时间）
    AIM_TIME_ = 1.1

    def __init__(self, scene, physics, app, pos_xz, hp, elite,
                 wave_damage, wave_err, wave_index,
                 dual_gun=False, rush_speed=0.0, rush_damage=0.0):
        self._dual_gun = bool(dual_gun)
        guns = GUN_PARTS_TWO if dual_gun else GUN_PARTS_ONE
        self.GUN_KEYS = tuple(p[0] for p in guns)
        self.SPEC = _BODY_PARTS + guns
        self._muzzles = {}
        self._gun_side = 0
        self._rush_speed = float(rush_speed)
        self._rush_damage = float(rush_damage)
        # ---- 冲刺子状态机的运行时字段 ----
        # 初值给 RUSH_COOLDOWN 而非 0：进 ENGAGE 先打一轮枪，
        # 不要一进场就冲（玩家还在适应移动）。
        self._rush_cd = RUSH_COOLDOWN
        self._rush_dir = Vec3(0.0, 0.0, 1.0)   # 冲刺方向（预备结束那一刻锁定）
        self._rush_left = 0.0                 # 剩余冲刺时间
        self._rush_hit = False                # 本次冲刺是否已造成接触伤害
        self._rush_count = 0                  # 累计冲刺次数（调试 / 测试）
        # 姿态偏移：冲刺三态各有前后倾 / 蹲伏，纯视觉，不改命中盒
        self._pose_off = Vec3(0.0, 0.0, 0.0)
        self._pose_lean = 0.0                 # 弧度，绕 X 轴俯仰
        super().__init__(scene, physics, app, pos_xz, hp, elite,
                         wave_damage, wave_err, wave_index)

    # Boss 抗击退：普通兵 mass 65 + armor 25 -> 冲量 k/90。
    # 奶龙给 900 质量 -> k/965，约 1/11，推不动（体型大就该推不动）。
    def _body_mass(self):
        return 900.0

    @property
    def is_boss(self):
        return True

    @property
    def boss_name(self):
        return "奶龙·双枪刺刀" if self._dual_gun else "奶龙·单枪"

    # ------------------------------------------------------------------
    def _sync_parts(self, dt):
        """奶龙版：短腿短手摆动 + 头/躯干静止 + 枪口火光。

        与 `BaseBot` 版同结构，差异只在 pitch 分发（腿/手臂反相而非人形四条
        腿各自摆）与提亮目标（`FLASH_KEYS`）。
        """
        base = self.base_pos()
        roll = 0.0
        squash = 1.0
        if self.dead:
            p = min(1.0, self._death_t / 0.25)
            roll = p * math.radians(90.0)
            squash = 1.0 - 0.85 * p

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
            gun_color = _FLASH_COLOR
        else:
            gun_color = _NL_GUN

        # ---- 冲刺姿态（纯视觉，不动命中盒）----
        # 预备：整体下沉 0.28m + 后仰 14°，像拉满的弓 —— 玩家读到这个
        #       姿态就该开始横向跑。
        # 冲刺：前倾 20° + 双臂前伸（arm 的 pitch 由 -55° 抬到 -95°）。
        # 硬直：整体下沉 0.18m + 前倾 26°，"扑空"的观感，同时提示这是输出窗口。
        arm_extra = 0.0
        if self.state == "RUSH_TELEGRAPH":
            p = min(1.0, self._state_t / RUSH_TELEGRAPH_TIME)
            self._pose_off = Vec3(0.0, -0.28 * p, 0.0)
            self._pose_lean = math.radians(-14.0 * p)
            gun_color = _FLASH_COLOR          # 枪口亮起 = 预警
        elif self.state == "RUSH":
            self._pose_off = Vec3(0.0, -0.10, 0.0)
            self._pose_lean = math.radians(20.0)
            arm_extra = math.radians(-40.0)
        elif self.state == "RUSH_STUN":
            p = min(1.0, self._state_t / RUSH_STUN_TIME)
            self._pose_off = Vec3(0.0, -0.18 * (1.0 - p), 0.0)
            self._pose_lean = math.radians(26.0 * (1.0 - p))

        base = base + self._pose_off

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
            pitch = swing.get(key, 0.0) + self._pose_lean
            if arm_extra and key.startswith("arm"):
                pitch += arm_extra
            e.transform.pitch = pitch

            # 枪口（含双枪）与火光：每个 gun key 各自算一个 _muzzle，
            # 交替开火时由 `active_muzzle()` 选枪。
            if key in self.GUN_KEYS:
                e.renderer.color = gun_color
                m = _rot_local(Vec3(off.x, off.y, off.z + 0.45), self.yaw)
                m = base + m
                self._muzzles[key] = m
                self._muzzle = m
            elif key in FLASH_KEYS:
                e.renderer.color = tuple(min(1.0, c * brighten)
                                         for c in color)

    # ------------------------------------------------------------------
    # 第 2 层：刺刀冲击（telegraph -> dash -> stun）
    # ------------------------------------------------------------------
    # 这一整块**只在 `_rush_speed > 0` 时活**（= 第 2 层）。
    # 第 1 层 `_rush_speed = 0`，`_boss_tick` 永远返回 False，
    # `update()` 里的 `state in RUSH_STATES` 也永远不成立 ——
    # 于是第 1 层的每帧执行路径与"没写这段代码"完全一致。
    def _boss_tick(self, dt, dist, player_eye, los):
        """ENGAGE 内的 Boss 私有钩子（`BaseBot._boss_tick` 的覆写）。"""
        if self._rush_speed <= 0.0:
            return False
        if self._rush_cd > 0.0:
            self._rush_cd -= dt
            return False
        if not los:
            return False
        if not (RUSH_MIN_DIST <= dist <= RUSH_MAX_DIST):
            return False
        # 概率门：冷却好 + 距离合适也未必每次都冲，避免节奏机械化
        if random.random() > RUSH_TRIGGER_CHANCE:
            return False
        self._begin_telegraph(player_eye)
        return True

    def _begin_telegraph(self, player_eye):
        """预备：转身面向玩家、锁定冲刺方向、开火循环清零。"""
        base = self.base_pos()
        d = Vec3(player_eye.x - base.x, 0.0, player_eye.z - base.z)
        if d.length_sq() < 1e-8:
            d = Vec3(0.0, 0.0, 1.0)
        self._rush_dir = d.normalized()
        self._rush_hit = False
        # 冲刺时不该还在吐子弹 —— 清掉弹匣与瞄准进度
        self._aim_t = 0.0
        self._burst_left = 0
        self._burst_gap_t = 0.0
        self._set_state("RUSH_TELEGRAPH")

    def _begin_dash(self):
        """预备结束 -> 冲刺。方向在预备那一刻就锁死（可被玩家走位躲开）。"""
        self._rush_left = RUSH_DASH_TIME
        # 前方 raycast：撞墙就提前收，避免 900kg 的 Boss 顶着墙空冲 0.55s
        origin = self.base_pos() + Vec3(0.0, 0.9, 0.0)
        t_hit, _body = raycast_world(self.app.physics, origin,
                                     self._rush_dir,
                                     self._rush_speed * RUSH_DASH_TIME + 1.0,
                                     include_dynamic=True)
        if t_hit is not None:
            reach = max(0.0, t_hit - RUSH_WALL_MARGIN)
            self._rush_left = min(self._rush_left,
                                  reach / self._rush_speed)
        self._set_state("RUSH")
        self._rush_count += 1

    def _end_rush(self):
        """冲刺结束 -> 硬直（玩家反打窗口）。"""
        self._rush_cd = RUSH_COOLDOWN
        self._stop(0.0)
        self._set_state("RUSH_STUN")

    def _update_rush(self, dt):
        """冲刺子状态机。返回 False 表示本帧不在冲刺态（该交回基类）。"""
        if self.state == "RUSH_TELEGRAPH":
            self._stop(dt)
            # 预备期持续面向玩家（转身本身也是"我要冲了"的信号）
            des = math.atan2(-self._rush_dir.x, -self._rush_dir.z)
            k = 1.0 - math.exp(-12.0 * dt)
            self.yaw += wrap_angle(des - self.yaw) * k
            if self._state_t >= RUSH_TELEGRAPH_TIME:
                self._begin_dash()
            return True

        if self.state == "RUSH":
            vy = self.body.velocity.y
            self.body.velocity = Vec3(self._rush_dir.x * self._rush_speed,
                                      vy,
                                      self._rush_dir.z * self._rush_speed)
            self._walk_phase += self._rush_speed * 3.2 * dt
            # 面向锁定方向（不再跟随玩家 —— 否则会变成跟踪导弹）
            des = math.atan2(-self._rush_dir.x, -self._rush_dir.z)
            self.yaw = des
            # 接触判定（每次冲刺最多判一次）
            if not self._rush_hit:
                p = self.app.player.transform.position
                b = self.base_pos()
                if math.hypot(p.x - b.x, p.z - b.z) < RUSH_HIT_RADIUS:
                    self._rush_hit = True
                    self.app.combat.damage_player(self._rush_damage)
                    self._end_rush()     # 撞到人 -> 立刻进硬直，给玩家反打
                    return True
            self._rush_left -= dt
            if self._rush_left <= 0.0:
                self._end_rush()
            return True

        if self.state == "RUSH_STUN":
            self._stop(dt)
            if self._state_t >= RUSH_STUN_TIME:
                self._set_state("ENGAGE")   # _set_state 会重置瞄准/弹匣
            return True
        return False

    def update(self, dt):
        """只在冲刺三态里接管，其余状态全部走 `BaseBot.update`。

        基类的 `update` 是 if/elif 链，没有"其他状态"分支；所以 Boss
        私有状态必须在这里**先拦一次**，否则会掉进链底什么都不做。
        """
        if not self.dead and self.state in RUSH_STATES:
            self._state_t += dt
            if self._rush_cd > 0.0:
                self._rush_cd -= dt
            self._update_rush(dt)
            self._sync_parts(dt)
            return
        self._pose_off = Vec3(0.0, 0.0, 0.0)
        self._pose_lean = 0.0
        super().update(dt)

    # ------------------------------------------------------------------
    # 双枪
    # ------------------------------------------------------------------
    def active_muzzle(self):
        """本次开火该用哪个枪口（双枪交替）。

        `_fire_one` 用它取代 `self._muzzle`。单枪 Boss 的 GUN_KEYS 只有
        一个元素，永远返回同一个，等价于人形 bot 的行为。
        """
        if not self.GUN_KEYS:
            return self._muzzle
        if len(self.GUN_KEYS) == 1:
            return self._muzzles[self.GUN_KEYS[0]]
        return self._muzzles[self.GUN_KEYS[self._gun_side % len(self.GUN_KEYS)]]

    def _fire_one(self, dist, player_eye, warning):
        """覆写以支持交替枪口；误差模型与判定完全沿用 BaseBot。"""
        self._gun_side += 1
        saved = self._muzzle
        self._muzzle = self.active_muzzle()
        try:
            super()._fire_one(dist, player_eye, warning)
        finally:
            self._muzzle = saved

    # ------------------------------------------------------------------
    def head_top(self):
        """头顶（含角）世界坐标 —— Boss 专属大血条挂在它上面。

        角尖 y=2.86，取 2.92 留一点余量让血条不压角。
        """
        return self.base_pos() + Vec3(0.0, 2.92, 0.0)

    def part_bounds(self):
        """{key: (x0,x1, y0,y1, z0,z1)} 脚底系包围盒（验算用）。"""
        sizes = {p[0]: p[1] for p in self.SPEC}
        out = {}
        for key, (_e, off, _c) in self.parts.items():
            s = sizes.get(key)
            if s is None:
                continue
            out[key] = (off.x - s[0] * 0.5, off.x + s[0] * 0.5,
                        off.y - s[1] * 0.5, off.y + s[1] * 0.5,
                        off.z - s[2] * 0.5, off.z + s[2] * 0.5)
        return out