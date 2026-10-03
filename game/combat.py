# -*- coding: utf-8 -*-
"""战斗管理：hitscan 命中判定、伤害/击杀统计、玩家 HP 与脱战回血。

命中判定（GDD §3.3）：hitscan 不经过物理世界。
- 静态遮挡：raycast_world 遍历物理世界碰撞体取最近 t（墙/地面/掩体/箱子），
  返回 (t, body)——body 用于击退命中动态箱（需求 C7）。
- bot 命中：CombatManager 为每只存活 bot 维护头/躯干两个纯数学 AABB，
  与遮挡比较取最近 t；头优先补偿：t_head <= t_body + 0.15 时算爆头。

玩家 HP / 回血（GDD §3.1 + 需求 F16）：HP 上限唯一数据源在 player.max_hp
（初始 100，升级 [1] 可加）；最后一次受击 4.0s 后 10 HP/s 回复至上限。

Sprint 3（ENG-004）新增：
- Tracer  激光尾迹（需求 B4/B5）：unlit 细盒，寿命 0.10s，截面 0.055m，
  尾迹比弹道长 1.2m；玩家蓝 / 敌方红。
- Pickup  掉落拾取（需求 G19/G20）：bot 死亡掉 hp/ammo 包，靠近拾取。
  ammo 包同时补给两把枪：步枪 reserve +30、狙击 reserve +5（SR-1 初始仅
  5+15 且波间不补弹，不补给则前两波即永久哑火）。
- apply_knockback（需求 C7）：命中击退，冲量 = k/(mass+armor)。
- player_shoot 伤害参数化（需求 E13）+ 精英狙击减伤 ×0.5；
  damage_mult（需求 F16 火力升级）乘算。
- damage_player 乘 (1 - player.damage_reduction)（需求 F16 防护升级）。
"""
import math
import random

# 掉落节流会用到 math.cos / math.sin（写在 combat.py 里而非依赖调用方）

from engine.scene import Entity
from engine.mesh import box_mesh, MeshRenderer
from engine.math_utils import Vec3, ray_aabb, yaw_pitch_from_dir
from .states import DROP_STATES

# bot 命中盒（GDD §3.2 字面值，相对 bot 原点=脚底）
HEAD_OFFSET = Vec3(0.0, 1.66, 0.0)
HEAD_HALF = Vec3(0.18, 0.17, 0.18)
BODY_OFFSET = Vec3(0.0, 0.90, 0.0)
BODY_HALF = Vec3(0.32, 0.90, 0.26)

PLAYER_HALF = Vec3(0.31, 0.9, 0.31)     # 与 player.body 一致
REGEN_DELAY = 4.0               # 脱战回血延迟（秒）
REGEN_RATE = 10.0               # 回血速度（HP/s）

BODY_DAMAGE = 26.0              # 步枪躯干伤害（GDD §2.3）
HEADSHOT_MULT = 2.5             # 爆头倍率 → 65

# 击退冲量 k（需求 C7/C8 字面值）。实际位移 = k/(mass+armor)，故 k 是
# "冲量强度"而非米/秒：玩家 70kg+55 防护几乎不动，bot 65kg+25 明显踉跄。
KNOCKBACK_BOT = 110.0           # 步枪 MW-1 命中 bot / 动态箱
KNOCKBACK_PLAYER = 40.0         # bot 命中玩家
KNOCKBACK_SNIPER = 300.0        # 狙击 SR-1（ENG-007 模块三：威力越大冲量越大）

# 掉落（需求 G19）：普通必掉 1 件 50/50；精英掉 2 件各一
PICKUP_HP_COLOR = (0.90, 0.42, 0.38)     # 珊瑚红
PICKUP_AMMO_COLOR = (1.00, 0.80, 0.40)   # 琥珀金


def aabb_surface_normal(origin, direction, center, half, t, eps=0.02):
    """射线在 AABB 表面命中点 t 处的外法线（指向射线来向）。

    slab 法只返回进入距离，这里由"命中点贴着哪一面"反解法线：取命中点
    到 6 个面中距离最小的一个，其外法线即为表面法线。eps 需大于浮点噪声
    又要小于最薄掩体（0.6m），0.02 足够。
    """
    p = origin + direction * t
    # 取"命中点最贴近的那个面"（六个面里挑距离最小的）作为主法线。
    # 命中点由 origin + dir·t 得出，天然贴住射线打到的**进入面**，
    # 不需要额外剔除背后的面——那反而会在擦棱角时选错边。
    #
    # 兜底：若没有任何面在 eps 内（t 由调用方传入、理论上不该发生），
    # 用 -direction，即"背对来弹方向"，比默认 +Y 合理得多。
    n = -direction
    best = eps
    for axis, c, h in (("x", center.x, half.x),
                       ("y", center.y, half.y),
                       ("z", center.z, half.z)):
        v = getattr(p, axis)
        for face, sgn in ((c - h, -1.0), (c + h, 1.0)):
            d = abs(v - face)
            if d < best:
                best = d
                n = Vec3(0.0, 0.0, 0.0)
                setattr(n, axis, sgn)
    return n.normalized()


def raycast_world(physics, origin, direction, max_dist,
                  include_dynamic=False, want_normal=False):
    """射线 vs 物理世界碰撞体，返回 (最近命中距离 t, 命中 body)；
    无命中返回 (None, None)。want_normal=True 时额外返回表面法线。

    - 默认只测静态体（墙/地面/掩体），GDD §2.2 定义。
    - include_dynamic=True 时额外纳入动态 AABB（可推箱挡子弹），
      但排除 bot / player（bot 走数学双盒、玩家不挡自己枪口）；
      动态球体（弹力球/炮弹）不参与，demo 简化。
    """
    best = None
    best_body = None
    for b in physics.bodies:
        if not b.is_static:
            if not include_dynamic or b.kind != "aabb":
                continue
            tag = b.entity.tag
            if tag in ("bot", "player"):
                continue
        t = ray_aabb(origin, direction, b.center, b.half)
        if t is not None and t <= max_dist and (best is None or t < best):
            best = t
            best_body = b
    if not want_normal:
        return best, best_body
    if best is None:
        return None, None, Vec3(0.0, 1.0, 0.0)
    n = (aabb_surface_normal(origin, direction, best_body.center,
                             best_body.half, best)
         if best_body.kind == "aabb" else -direction)
    return best, best_body, n


# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# Boss 掉落节流
# ----------------------------------------------------------------------
# 普通 bot 死一次掉 1~2 件；奶龙血量 1000 / 300，按"每 100 血一件"节流：
#   第 1 层 1000 血 -> 至多 10 次中途掉落 + 死亡 1 次 = 最多 11 次
#   第 2 层  300 血 -> 至多  3 次中途掉落 + 死亡 1 次 = 最多 4 次
# 交替 hp/ammo，保证长战里两样都能续上。
# BOSS_DROP_HP_STEP 是**用户指定**的字面值（100 血一掉）。
BOSS_DROP_HP_STEP = 100.0
# 每掉一件时最多同时存在几个（防止 Boss 反复掉把地上铺满盒子）
BOSS_DROP_MAX_LIVE = 4

# ----------------------------------------------------------------------
# Tracer：激光尾迹（需求 B）——瞬时 unlit 细盒，长度 0.5m 档位取整
#
# 尺寸是**实测定的，不是拍的**。原参数 0.025m 截面在交战距离上会退化到
# 亚像素：eng/bots.py 的 ENGAGE_MIN/MAX 是 8~14m，该距离下 1m≈84px，
# 25mm 只有 2.1px；20m 处只剩 0.75px —— 加上 0.07s(≈4 帧)寿命，
# bot 的红色尾迹实际上"看不见"。现取：
#   截面 0.055m（3.3 倍）-> 14m 处约 3.4px，可辨认
#   尾迹长度 +TRACER_TAIL（向枪口后方延伸）-> 比真实弹道更长，读作激光束
#   寿命 0.10s（6 帧）
# 颜色按阵营区分：玩家蓝、敌方红（unlit，不受光照影响，远距离也认得出）。
# ----------------------------------------------------------------------
PLAYER_TRACER_COLOR = (0.30, 0.62, 1.00)     # 蓝 —— 玩家
BOT_TRACER_COLOR = (1.00, 0.16, 0.13)        # 红 —— 敌方
TRACER_LIFE = 0.10
TRACER_THICK = 0.055                         # 截面边长（米）
TRACER_TAIL = 1.20                           # 向枪口后方延伸的长度（米）


class Tracer(Entity):
    """一发子弹的视觉尾迹：细盒 (TRACER_THICK, THICK, L)，中心放起止中点，
    yaw/pitch 对准方向（与 view-model 同一前向约定），TRACER_LIFE 后移除。

    `tail` 让盒子从 `start - dir*tail` 起算，因此光带比真实弹道更长 ——
    这是让远距离尾迹"读得出来"的关键，比单纯加粗更有效。
    """

    def __init__(self, scene, start, end, color, tail=TRACER_TAIL):
        super().__init__("tracer", "tracer")
        self.scene = scene
        d = end - start
        dist = d.length()
        if dist < 1e-6:
            d = Vec3(0.0, 0.0, -1.0)
            dist = 1e-6
        else:
            d = d / dist
        a = start - d * tail                     # 尾迹向枪口后方延伸
        seg = max(0.5, round((dist + tail) / 0.5) * 0.5)  # 0.5m 档（mesh 缓存分档）
        self.transform.position = a.scaled_add(d, (dist + tail) * 0.5)
        yaw, pitch = yaw_pitch_from_dir(d)
        self.transform.yaw = yaw
        self.transform.pitch = pitch
        self.renderer = MeshRenderer(box_mesh(TRACER_THICK, TRACER_THICK, seg),
                                     color=color, cast_shadow=False,
                                     unlit=True)
        self._life = TRACER_LIFE
        scene.add(self)

    def update(self, dt):
        self._life -= dt
        if self._life <= 0.0:
            self.scene.remove(self)


# ----------------------------------------------------------------------
# Pickup：掉落拾取（需求 G）
# ----------------------------------------------------------------------
class Pickup(Entity):
    """bot 死亡掉落物：0.3³ unlit 盒（hp=珊瑚红 / ammo=琥珀金），
    yaw 旋转 2rad/s + y 上下浮动 0.1m，无碰撞体；玩家中心 1.3m 内拾取。"""

    SPIN = 2.0            # rad/s
    HOVER = 0.1           # m
    PICK_DIST = 1.3       # m
    HEAL = 30.0           # hp 包回复量
    AMMO = 30             # ammo 包步枪备弹增量
    SNIPER_AMMO = 5       # ammo 包狙击备弹增量（恰好一个弹匣）

    def __init__(self, scene, app, pos, kind):
        super().__init__("pickup", "pickup")
        self.scene = scene
        self.app = app
        self.kind = kind                     # "hp" / "ammo"
        color = PICKUP_HP_COLOR if kind == "hp" else PICKUP_AMMO_COLOR
        self._base = Vec3(pos.x, pos.y + 0.55, pos.z)
        self._t = 0.0
        self.renderer = MeshRenderer(box_mesh(0.3, 0.3, 0.3), color=color,
                                     cast_shadow=False, unlit=True)
        self.transform.position = self._base.clone()
        scene.add(self)

    def teleport(self, pos):
        """自检钩子：整体搬移（写 _base，下一帧起浮动跟随新位置）。"""
        self._base = Vec3(pos.x, pos.y, pos.z)

    def update(self, dt):
        self._t += dt
        self.transform.yaw += Pickup.SPIN * dt
        self.transform.position = Vec3(
            self._base.x,
            self._base.y + Pickup.HOVER * math.sin(self._t * 2.0),
            self._base.z)

        player = self.app.player
        combat = self.app.combat
        if player is None or combat is None:
            return
        pc = player.transform.position
        d = Vec3(pc.x - self._base.x,
                 pc.y - self.transform.position.y,
                 pc.z - self._base.z)
        if d.length() >= Pickup.PICK_DIST:
            return
        if self.kind == "hp":
            if player.hp >= player.max_hp:
                return                      # 满血留场不拾
            player.hp = min(player.max_hp, player.hp + Pickup.HEAL)
            combat.pickups_hp += 1
        else:
            weapons = getattr(self.app, "weapons", None)
            if weapons:                     # 步枪备弹 +30（需求 G20）
                weapons[0].reserve += Pickup.AMMO
            # 狙击备弹 +5：SR-1 初始仅 5+15 且波间从不补给，不补给则前两波哑火
            if weapons and len(weapons) > 1:
                weapons[1].reserve += Pickup.SNIPER_AMMO
            combat.pickups_ammo += 1
        self.scene.remove(self)


# ----------------------------------------------------------------------
# CombatManager
# ----------------------------------------------------------------------
class CombatManager:
    """每帧维护 bot 命中盒、裁决玩家/bot 双方 hitscan、玩家 HP 与回血。"""

    def __init__(self, app):
        self.app = app
        # HP 唯一数据源在 player.hp / player.max_hp（需求 F16）
        self.app.player.hp = self.app.player.max_hp
        self.time_since_hit = 999.0       # 距上次受击（大初值=开局即视为脱战）
        self.regen_observed = False       # selftest：回血真实发生过

        # 统计（_report 汇总行 + 结算页数据源）
        self.kills = 0
        self.headshots = 0
        self.shots_fired = 0              # 玩家开枪数（命中率分母）
        self.shots_hit = 0
        self.min_hp = self.app.player.max_hp

        # 升级数据落点（需求 F16）
        self.damage_mult = 1.0            # 火力强化 ×1.25 累乘

        # 拾取计数（显示用，需求 G20）
        self.pickups_hp = 0
        self.pickups_ammo = 0

        # Boss 掉落节流状态：key = id(boss) -> (上次掉时的 hp, 累计掉了几件)
        # 用 id 作 key 是因为 Boss 换层会换实例，且死亡后对象仍在场景里。
        self._boss_drop_state = {}

        # Sprint 2 命中反馈数据接口：元素 = (time, part, killed, headshot)
        self.hit_events = []

    @property
    def player_hp(self):
        return self.app.player.hp

    @player_hp.setter
    def player_hp(self, v):
        self.app.player.hp = v

    # ------------------------------------------------------------------
    # 玩家 hitscan（武器调用，伤害参数化：需求 E13）
    # ------------------------------------------------------------------
    def player_shoot(self, origin, direction, damage_body=BODY_DAMAGE,
                     headshot_mult=HEADSHOT_MULT, sniper=False,
                     max_dist=120.0, knockback=KNOCKBACK_BOT):
        """裁决一发玩家子弹；返回命中信息 dict 或 None。

        - damage_body / headshot_mult 由武器传入（MW-1 26/×2.5，SR-1 260/×2.5）。
        - damage_mult（火力升级）乘算；精英被狙击命中减半（需求 E13）。
        - knockback 由武器传入：狙击的冲量应显著大于步枪（ENG-007 模块三）。
        - 命中 bot / 动态箱施加击退；无论命中与否生成 tracer；
          命中静态面/地面时在命中点生成火花 + 弹痕。
        """
        self.shots_fired += 1
        t_wall, wall_body, wall_normal = raycast_world(
            self.app.physics, origin, direction, max_dist,
            include_dynamic=True, want_normal=True)
        t_wall_lim = t_wall if t_wall is not None else float("inf")

        best_t = float("inf")
        best_bot = None
        best_head = False
        for bot in self._alive_bots():
            base = bot.base_pos()
            # 命中盒向角色要（默认返回模块常量 -> 普通兵行为不变；
            # Boss 体型差一个量级，必须自带盒子，见 bots.HumanoidBot.hitboxes）
            ho, hh, bo, bh = bot.hitboxes()
            th = ray_aabb(origin, direction, base + ho, hh)
            tb = ray_aabb(origin, direction, base + bo, bh)
            if th is None and tb is None:
                continue
            # GDD §3.3 头优先补偿为 +0.05，但按 §3.2 字面值头盒（y 1.49~1.83）
            # 几乎整体嵌套在躯干盒（y 0~1.8）内：从侧面射中头部时 t_head -
            # t_body ≈ 0.32-0.18 = 0.14 > 0.05，会被误判为躯干。容差放宽到
            # 0.15（覆盖最坏入射角），保证"射线穿头即爆头"。
            if th is not None and (tb is None or th <= tb + 0.15):
                t, head = th, True
            else:
                t, head = tb, False
            if t < best_t:
                best_t, best_bot, best_head = t, bot, head

        start = origin + direction * 0.9
        if best_bot is None or best_t >= t_wall_lim or best_t > max_dist:
            # 未命中：tracer 打到墙（或 60m 截断）；命中动态箱则击退
            end_t = t_wall if t_wall is not None else min(max_dist, 60.0)
            end = origin + direction * end_t
            Tracer(self.app.scene, start, end, PLAYER_TRACER_COLOR)
            if wall_body is not None and not wall_body.is_static:
                self.apply_knockback(wall_body, direction, knockback)
            elif wall_body is not None:
                # 打在静态面/地面上：火花 + 弹痕（ENG-007）
                self._impact(end, wall_normal, is_static=True)
            return None

        damage = (damage_body
                  * (headshot_mult if best_head else 1.0)
                  * self.damage_mult)
        if best_bot.elite and sniper:      # 精英狙击减伤（需求 E13）
            damage *= 0.5
        killed = best_bot.take_damage(damage, headshot=best_head)
        self.shots_hit += 1
        # Boss 中途掉落（每扣 100 血一件）。非 Boss 或已击杀时内部直接返回。
        self.check_boss_drop(best_bot)
        if killed:
            self.kills += 1
            if best_head:
                self.headshots += 1
        event = (self.app.time, "head" if best_head else "body",
                 killed, best_head)
        self.hit_events.append(event)
        if len(self.hit_events) > 8:
            self.hit_events.pop(0)

        self.apply_knockback(best_bot.body, direction, knockback)
        Tracer(self.app.scene, start, origin + direction * best_t,
               PLAYER_TRACER_COLOR)
        # bot 身上只出火花不落弹痕（尸体消失后弹痕会悬空，见 effects.py）
        self._impact(origin + direction * best_t, -direction, is_static=False)
        return {"bot": best_bot, "head": best_head, "killed": killed,
                "t": best_t, "damage": damage}

    # ------------------------------------------------------------------
    def _impact(self, point, normal, is_static):
        """在命中点生成火花 + 弹痕（headless / 特效管理器缺失时静默跳过）。"""
        fx = getattr(self.app, "fx", None)
        if fx is not None:
            fx.on_hit(point, normal, is_static)

    # ------------------------------------------------------------------
    # bot hitscan（bots.py 调用）
    # ------------------------------------------------------------------
    def bot_shoot_hits_player(self, origin, direction, max_dist=120.0):
        """裁决一发 bot 子弹是否命中玩家（先算静态/箱子遮挡）。
        命中玩家施加击退 k=40（需求 C7）；生成 bot 色 tracer（需求 B5）。"""
        t_wall, wall_body, wall_normal = raycast_world(
            self.app.physics, origin, direction, max_dist,
            include_dynamic=True, want_normal=True)
        t_wall_lim = t_wall if t_wall is not None else float("inf")
        player = self.app.player
        tp = ray_aabb(origin, direction, player.transform.position,
                      PLAYER_HALF)
        hit = tp is not None and tp < t_wall_lim and tp <= max_dist
        if hit:
            end = origin + direction * tp
            self.apply_knockback(player.body, direction, KNOCKBACK_PLAYER)
        else:
            end_t = t_wall if t_wall is not None else min(max_dist, 60.0)
            end = origin + direction * end_t
            # bot 的弹着点也留弹痕，让战场有交战痕迹
            if wall_body is not None and wall_body.is_static:
                self._impact(end, wall_normal, is_static=True)
        Tracer(self.app.scene, origin, end, BOT_TRACER_COLOR)
        return hit

    def has_los(self, origin, target):
        """origin → target 是否无遮挡（bot 视线/导航点预选用）。"""
        d = target - origin
        dist = d.length()
        if dist < 1e-6:
            return True
        t, _body = raycast_world(self.app.physics, origin, d / dist,
                                 dist - 0.05, include_dynamic=True)
        return t is None

    # ------------------------------------------------------------------
    # 击退（需求 C7）：水平方向冲量 k/(mass+armor)，armor 缺省 25
    # ------------------------------------------------------------------
    def apply_knockback(self, body, direction, k):
        if body is None or body.is_static:
            return
        d = Vec3(direction.x, 0.0, direction.z)
        if d.length_sq() < 1e-10:
            return
        d = d.normalized()
        armor = getattr(body.entity, "armor", 25.0)
        impulse = k / (body.mass + armor)
        body.velocity = body.velocity + d * impulse

    def damage_player(self, amount):
        """bot 子弹命中玩家：扣血（乘 1-减伤）、重置脱战计时、判定战败。"""
        if self.player_hp <= 0.0:
            return
        amount *= (1.0 - self.app.player.damage_reduction)
        self.player_hp = max(0.0, self.player_hp - amount)
        self.time_since_hit = 0.0
        if self.player_hp < self.min_hp:
            self.min_hp = self.player_hp
        if self.player_hp <= 0.0:
            self.app.on_player_dead()

    # ------------------------------------------------------------------
    # 掉落（需求 G19）：bot 死亡时由 HumanoidBot._die 回调
    # ------------------------------------------------------------------
    def on_bot_death(self, bot):
        if self.app.state not in DROP_STATES:
            return
        base = bot.base_pos()
        is_boss = bool(getattr(bot, "is_boss", False))
        if is_boss:
            # Boss 死亡：一次掉 hp + ammo 各一，并清掉节流状态
            self._boss_drop_state.pop(id(bot), None)
            self._drop_pair(base, (-0.75, 0.0), (0.75, 0.0))
            return
        if bot.elite:
            kinds = ("hp", "ammo")        # 精英 2 件各一
            offsets = ((-0.45, 0.0), (0.45, 0.0))
        else:
            kinds = (random.choice(("hp", "ammo")),)
            offsets = ((0.0, 0.0),)
        for kind, (ox, oz) in zip(kinds, offsets):
            Pickup(self.app.scene, self.app,
                   base + Vec3(ox, 0.0, oz), kind)

    def _drop_pair(self, base, off_a, off_b):
        """在 Boss 两侧各掉一件（hp / ammo）。"""
        for kind, off in (("hp", off_a), ("ammo", off_b)):
            Pickup(self.app.scene, self.app,
                   base + Vec3(off[0], 0.0, off[1]), kind)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Boss 中途掉落（用户需求：每扣 100 血掉一次）
    # ------------------------------------------------------------------
    def check_boss_drop(self, bot):
        """玩家命中 Boss 后调用：按掉血量节流掉落。

        在 `player_shoot` 裁决命中之后调用。扣血量由武器与升级倍率共同决定，
        所以按**血量差值**判断最稳（不看发了多少枪）。

        规则：
        - 每累计掉 `BOSS_DROP_HP_STEP`(100) 血掉一件
        - hp / ammo **交替**（奇数次 hp、偶数次 ammo），两样都续得上
        - 地上同时最多 `BOSS_DROP_MAX_LIVE`(4) 件，超出就不再掉
          （玩家不捡时不至于把地铺满盒子）

        **基准 `last_hp` 只在真正掉落后才推进**。这是关键：
        早先版本每次调用都把 `last_hp` 写成当前 hp，于是 `lost` 永远是
        「最后一发的伤害」（26），永远达不到 100 → 一次都不掉。
        正确做法是记住「上次掉点时的血量」，中间各发都不动它。
        """
        if self.app.state not in ("BOSS",):
            return False
        if not bool(getattr(bot, "is_boss", False)):
            return False
        # 已经死了就不要再掉：`_die` 会在死亡回调里掉 hp+ammo 各一。
        # 少了这一句，致命一击会**同时**触发中途掉落和死亡掉落，
        # 地上一次多出 4 件（实测 1000 血打死瞬间地上 6 件 = 4 中途 + 2 死亡）。
        if bot.dead:
            return False
        last_hp, n = self._boss_drop_state.get(id(bot), (bot.max_hp, 0))
        lost = last_hp - bot.hp                 # 距上次掉点的累计掉血
        steps = int(lost // BOSS_DROP_HP_STEP)
        if steps <= 0:
            return False                       # 基准不动，等下一个 100 血

        fired = 0
        live = len(self.app.scene.query("pickup"))
        for _ in range(steps):
            if live >= BOSS_DROP_MAX_LIVE:
                break                          # 地上满了就不再掉
            n += 1
            kind = "hp" if n % 2 == 1 else "ammo"
            # 掉在 Boss 身侧随机一点，避免每次都在正前方
            a = random.uniform(0.0, math.tau)
            r = random.uniform(0.7, 1.15)
            pos = bot.base_pos() + Vec3(math.cos(a) * r, 0.0,
                                       math.sin(a) * r)
            Pickup(self.app.scene, self.app, pos, kind)
            live += 1
            fired += 1

        # 基准只按**实际掉了几步**推进。因地上满了而 break 时，
        # 剩余步数留给下次 —— 否则基准会超前于真实掉落量，
        # 玩家清空地面后又得再等 100 血才继续掉（实测会卡住）。
        new_hp = last_hp - fired * BOSS_DROP_HP_STEP
        self._boss_drop_state[id(bot)] = (new_hp, n)
        return fired > 0

    def clear_boss_drop_state(self):
        """换层 / 回标题时清干净（节流状态跨关卡无意义）。"""
        self._boss_drop_state.clear()

    # ------------------------------------------------------------------
    # 每帧
    # ------------------------------------------------------------------
    def update(self, dt):
        # 脱战回血：受击 4.0s 后 10 HP/s 回复至上限（GDD §3.1 + F16）
        max_hp = self.app.player.max_hp
        self.time_since_hit += dt
        if (self.player_hp < max_hp
                and self.time_since_hit >= REGEN_DELAY):
            new_hp = min(max_hp, self.player_hp + REGEN_RATE * dt)
            if new_hp > self.player_hp:
                self.regen_observed = True
            self.player_hp = new_hp
        if self.player_hp < self.min_hp:
            self.min_hp = self.player_hp

    def supply(self):
        """INTERMISSION 补给（需求 G18）：HP +30（≤player.max_hp 封顶）。
        弹药不再补满（步枪/狙击备弹均不补）。"""
        self.player_hp = min(self.app.player.max_hp,
                             self.player_hp + 30.0)

    # ------------------------------------------------------------------
    # 自检钩子（--selftest 专用）
    # ------------------------------------------------------------------
    def nearest_bot_info(self):
        """最近存活 bot 的 (bot, 距离, 是否有 LOS)；无 bot 返回 None。"""
        eye, _ = self.app.camera_rig.eye_and_forward()
        best = None
        best_d = float("inf")
        for bot in self._alive_bots():
            d = (bot.base_pos() - eye).length()
            if d < best_d:
                best_d, best = d, bot
        if best is None:
            return None
        target = best.base_pos() + best.hitboxes()[2]
        return best, best_d, self.has_los(eye, target)

    def aim_at_nearest_bot(self, head=False):
        """把相机直接对准最近存活 bot 的头/躯干中心（直接写 yaw/pitch）。"""
        eye, _ = self.app.camera_rig.eye_and_forward()
        best = None
        best_d = float("inf")
        for bot in self._alive_bots():
            d = (bot.base_pos() - eye).length_sq()
            if d < best_d:
                best_d, best = d, bot
        if best is None:
            return False
        ho, _hh, bo, _bh = best.hitboxes()
        target = best.base_pos() + (ho if head else bo)
        yaw, pitch = yaw_pitch_from_dir(target - eye)
        self.app.camera_rig.yaw = yaw
        self.app.camera_rig.pitch = pitch
        return True

    def debug_kill_all(self):
        """秒杀全部存活 bot（波次跳转自检钩子）。"""
        for bot in self._alive_bots():
            bot.take_damage(999999.0, headshot=False)

    def alive_bot_count(self):
        return len(self._alive_bots())

    def _alive_bots(self):
        return [e for e in self.app.scene.query("bot")
                if e.alive and not e.dead]
