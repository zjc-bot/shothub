# 程序化人形角色构建规则（Procedural Humanoid Construction Rules）

> **用途**：把本文交给任意 AI / 开发者，让它**不写任何模型文件**、不导入外部资源，
> 仅用本项目的引擎 API 拼装出一个新角色。
>
> **项目**：mini_world —— Python 3.13 + pygame-ce 2.5.8 + moderngl（OpenGL 3.3 Core）。
> **零外部资源**是本项目的硬约束：所有几何体在运行时由代码生成，纹理为程序化字节数组。
>
> **参考实现**：`game/bots.py` 的 `HumanoidBot`（7 部件）、`game/actors.py` 的 `Turret`（5 部件）。

---

## 0. 核心思路：角色 = 若干个独立的立方体

**不要建骨骼、不要做蒙皮、不要导入 glTF。** 本项目的角色是这样长出来的：

```
1 个根实体（只有它有物理体，负责移动与碰撞）
  └─ N 个子实体（只有渲染组件，无物理体、无逻辑）
       每帧由根实体算出自己的世界坐标与朝向
```

每个子部件就是**一个轴对齐立方体**（`box_mesh`），加上**局部偏移量**和**颜色/材质**。
角色的"形状"完全由这三条信息决定。星球大战风格的暴风兵、绝地、机器人，
都只是**换一张部件表**（不同尺寸 + 不同偏移 + 不同配色），逻辑代码一行都不用动。

**为什么能这么简单**：
- 引擎没有父子层级（`transform.py` 明确写了"demo 规模不需要"），
  所以每帧要**手工**把每个部件的世界坐标算出来。
- 代价是纯 Python 循环，收益是完全可控的 draw call 预算与零资源依赖。

---

## 1. 引擎 API 速查（必须只使用这些）

### 1.1 生成几何体 —— `engine/mesh.py`

```python
from engine.mesh import box_mesh, plane_mesh, sphere_mesh, MeshRenderer

box_mesh(w, h, d)      # 立方体，尺寸 (w,h,d) 米，中心在原点 —— 角色的主力
sphere_mesh(r)         # UV 球
plane_mesh(sx, sz)     # XZ 平面，法线 +Y

MeshRenderer(
    mesh,                          # 上面任一网格函数的结果
    color=(0.5, 0.6, 0.6),         # 线性空间 RGB，逐通道 0~1
    texture=None,                  # 字符串名；角色的部件一般用 None（纯色）
    cast_shadow=True,              # 是否投射阴影 —— 关键性能开关，见 §4
    shininess=20.0,                # 高光锐度：布料 ~20，金属 ~72
    spec_strength=0.15,            # 高光强度：布料 0.15，金属 0.70
    unlit=False,                   # True = 自发光（不受光照/阴影/雾影响）
)
```

> **网格有缓存**：相同尺寸的 `box_mesh` 返回**同一个对象**。所以尺寸档位越少越好，
> 复用越充分；新造一堆 0.013 尺寸的独特立方体不会增加显存，但会失去缓存复用。

### 1.2 实体与场景 —— `engine/scene.py`

```python
from engine.scene import Entity, Scene

e = Entity(name="bot_head", tag="bot_part")   # tag 供 scene.query() 批量查询
e.renderer = MeshRenderer(...)                 # 挂渲染组件
scene.add(e)                                   # 注册
scene.remove(e)                                # 标记删除，下一帧统一清理
```

`Entity` 只有三个可挂载位：`transform`（必有）、`renderer`（视觉）、`body`（物理）。
**没有父子关系、没有约束、没有蒙皮。**

### 1.3 变换 —— `engine/transform.py`

```python
e.transform.position   # Vec3，实体中心的世界坐标
e.transform.yaw        # 绕 Y 轴（水平转身）
e.transform.pitch      # 绕 X 轴（前后倾）
e.transform.roll       # 绕 Z 轴（侧倾）
e.transform.scale      # 均匀缩放（引擎只用均匀缩放！）

# 矩阵在渲染时自动合成：M = T · Ry(yaw) · Rx(pitch) · Rz(roll) · Scale
```

> **重要约定**：局部偏移用 **(x 右, y 上, z 前)** 描述。
> "前"是角色面朝的方向。`yaw=0` 时角色朝 **-Z**。

### 1.4 局部 → 世界 —— `game/bots.py` 的 `_rot_local`

```python
def _rot_local(off, yaw):
    s, c = math.sin(yaw), math.cos(yaw)
    return Vec3(off.x * c - off.z * s, off.y, -off.x * s - off.z * c)
```

**每个部件每帧都要过这一步**，把局部偏移按角色朝向旋到世界坐标。
加上根实体位置就是部件世界坐标。

### 1.5 物理体 —— `engine/physics.py`

```python
from engine.physics import PhysicsBody

self.body = PhysicsBody(
    self,                    # 绑定实体
    "aabb",
    half=Vec3(0.32, 0.91, 0.32),   # 半尺寸
    mass=65.0,               # 质量
    friction=0.0,            # 摩擦（着地指数衰减系数）
    is_static=False,         # True = 墙/地面这类不动的
)
physics.register(self.body)
```

---

## 2. 硬性架构约束（违反会出错，不是建议）

### 规则 1：一个角色**只能有一个** `PhysicsBody`

- 引擎的碰撞世界**没有碰撞过滤**，两个物体会互相推挤。
- 如果给头单独挂物理体，它会与身体碰撞互推 → 角色抖动/被弹飞。
- 所以：物理体挂在**根实体**上，代表整具身体的包围盒。
- 各部件只是视觉，**不参与碰撞**。

### 规则 2：物理体中心 ≠ 脚底

根实体的 `transform.position` = **身体中心**（约 0.91m 高处），
而所有部件偏移都以**脚底**为基准。所以需要一个转换：

```python
def base_pos(self):
    p = self.transform.position
    return Vec3(p.x, p.y - BODY_HALF_Y, p.z)    # BODY_HALF_Y = 0.91
```

### 规则 3：命中判定用**数学 AABB**，不用物理体

本项目 hitscan 射击**不查物理世界**，而是为每个部位维护纯数学包围盒
（`game/combat.py`）：

```python
HEAD_OFFSET = Vec3(0.0, 1.66, 0.0)   # 头盒中心（相对脚底）
HEAD_HALF   = Vec3(0.18, 0.17, 0.18)
BODY_OFFSET = Vec3(0.0, 0.90, 0.0)
BODY_HALF   = Vec3(0.32, 0.90, 0.26)
```

**新角色若改了身高/比例，必须同步改这两个盒子**，否则打头打不中、打身体打空。
爆头判定有"头优先"补偿：射线穿头时即使先穿过躯干盒也算爆头。

### 规则 4：每帧同步部件，不能只在创建时算一次

```python
def _sync_parts(self, dt):
    base = self.base_pos()
    for key, (e, off, color) in self.parts.items():
        e.transform.position = base + _rot_local(off, self.yaw)
        e.transform.yaw = self.yaw
        # 再叠加该部件自己的动画角（走路摆腿等）
        e.transform.pitch = ...
```

---

## 3. 部件表：设计的唯一入口

角色外观**完全**由这张表决定。格式（照抄 `HumanoidBot` 的 `spec`）：

```python
spec = (
    # key,    尺寸 (w,h,d),        局部偏移 (x右,y上,z前),  颜色,             shin,  spec,  shadow
    ("head",  (0.34, 0.32, 0.34), (0.0, 1.66, 0.0),      _SKIN,            24.0, 0.20, True),
    ("torso", (0.60, 0.70, 0.34), (0.0, 1.15, 0.0),      _TORSO_NORMAL,    20.0, 0.15, True),
    ("leg_l", (0.22, 0.80, 0.26), (-0.14, 0.40, 0.0),    _LEG_COLOR,       20.0, 0.15, False),
    ("leg_r", (0.22, 0.80, 0.26), (0.14, 0.40, 0.0),     _LEG_COLOR,       20.0, 0.15, False),
    ("arm_l", (0.16, 0.58, 0.20), (-0.38, 1.18, 0.06),   _TORSO_NORMAL,    20.0, 0.15, False),
    ("arm_r", (0.16, 0.58, 0.20), (0.38, 1.18, 0.06),    _TORSO_NORMAL,    20.0, 0.15, False),
    ("gun",   (0.11, 0.15, 0.80), (0.30, 1.22, 0.42),    _GUN_COLOR,       72.0, 0.70, False),
)
```

### 3.1 尺寸与比例规则

以总高 **1.84m** 为基准（各部件偏移以脚底 y=0 起算）：

| 部位 | y 范围 | 高度 | 说明 |
|---|---|---|---|
| 头 | 1.50 ~ 1.82 | 0.32 | 头高约等于身高 1/5.7 |
| 躯干 | 0.80 ~ 1.50 | 0.70 | 肩宽 0.60 > 躯干厚 0.34（扁身） |
| 腿 | 0.00 ~ 0.80 | 0.80 | 腿占 43% 身高 |
| 臂 | 0.89 ~ 1.47 | 0.58 | 挂在 x=±0.38，**在躯干外面** |

**渲染成"人"的关键比例**：
- 肩宽 : 身高 ≈ 0.60 : 1.84 ≈ **1 : 3**（常见人体 1:2~1:2.5，游戏中取窄更显低多边形）
- 头宽 : 肩宽 ≈ 0.34 : 0.60 ≈ **0.57**
- 躯干厚度只有肩宽的一半（0.34）→ 侧视是薄片，正视有体积

### 3.2 偏移量规则

- `x`：左右。0 = 中线，正 = 角色右侧（对称部件用 ±）
- `y`：高度，从**脚底**算起
- `z`：前后。0 = 身体中心线，正 = 角色前方
- 手臂略微前移（z=0.06）→ 双臂不在躯干正后方，从斜后方看有层次
- 枪挂在 `x=+0.38`（右手）、`z=+0.42`（身前）→ 单手持枪

### 3.3 颜色与材质规则

材质分三档，**别混用**：

| 材质档 | shininess | spec_strength | 用于 | 手感 |
|---|---|---|---|---|
| 布料 / 皮肤 | 20 ~ 24 | 0.15 ~ 0.20 | 头、躯干、四肢 | 哑光 |
| 金属 | 72 | 0.70 | 枪、机械件 | 亮面高光 |
| 自发光 | `unlit=True` | — | 灯、能量刃、目镜 | 不受光照 |

**配色遵守项目的 pastel 三原则**（`docs/art/visual-style.md` §0）：

1. 明度全部落在 **0.60 ~ 0.95**，禁止纯黑/深灰
2. 饱和度压低：**RGB 三通道差值 ≤ 0.45**
3. 天空色 = 雾色 = 清屏色，三者同值

现有角色配色（可直接复用或对照）：

```python
_SKIN        = (0.93, 0.80, 0.66)   # 肤色
_TORSO_NORMAL= (0.52, 0.62, 0.60)   # 普通兵躯干：灰蓝绿
_TORSO_ELITE = (0.90, 0.42, 0.38)   # 精英躯干：珊瑚红
_LEG_COLOR   = (0.58, 0.62, 0.70)   # 腿
_GUN_COLOR   = (0.40, 0.42, 0.46)   # 枪：冷深灰（场景唯一冷色暗部）
_FLASH_COLOR = (1.0, 0.82, 0.45)    # 枪口火光：琥珀
```

> **明度下限是 0.60**，所以"星球大战的深灰装甲"不能直接用 (0.2,0.2,0.2)，
> 会被这个世界的光照洗成一片灰。深色件请用 **0.45~0.55** 之间，
> 并靠 `shininess=72 / spec=0.70` 的金属高光来读出"金属感"，
> 而不是靠压暗基色。

---

## 4. 性能预算（这是硬约束，不是建议）

### 规则 5：阴影投射要极度节制

场景有 **2 盏投影聚光灯**（手电 + 灯塔），每盏灯都要把场景重画一遍阴影。
公式：

```
总 draw call ≈ (场景实体数) × (1 主 pass + 2 阴影 pass)
```

| 项目 | 数量 |
|---|---|
| 场上 bot 上限（同屏） | 5 |
| 每 bot 部件数 | 7 |
| bot 部件的 draw call | 5 × 7 × 3 = **105** |
| 其中投影的部件 | 只有 head + torso = 2 个 |
| 实际投影 draw call | 5 × 2 × 2 = **20** |

**所以：`cast_shadow` 只给最大的 1~2 个部件（头、躯干），四肢和枪必须 False。**
这是 `HumanoidBot` 里 shadow 列的实际取值，不是我随手写的。

### 规则 6：部件总数控制在 8~12 个

- 5 bot × 12 部件 = 60 实体 ≈ 240 draw call/帧，纯 Python 循环会吃力
- 加部件前先算：`新增部件数 × 同屏角色数 × 3` 是否还能接受
- 想要更多细节？**优先加颜色块而不是加立方体**——同尺寸的立方体可以复用网格缓存，
  而每个实体都要走一遍 Python 循环 + draw call

### 规则 7：聚光灯上限 3，`unlit` 不占预算

`graphics/shaders.py` 里 `MAX_SPOTS = 3`，手电和灯塔已占 2。
**任何发光诉求都不许加灯**，一律用 `unlit=True` 材质替换
（枪口火光、尾迹、掉落物、弹痕、火花都是这么做的）。

---

## 5. 动画规则

无骨骼动画，靠**摆动角度**实现。全部在 `_sync_parts` 里每帧算。

### 5.1 行走摆动

```python
# 相位随移动距离累加（不是随时间），走得越快步频越高
self._walk_phase += speed * 3.2 * dt

leg_swing = math.sin(self._walk_phase) * math.radians(25.0)   # 腿摆 25°
arm_swing = math.sin(self._walk_phase) * math.radians(8.0)    # 臂摆 8°（弱得多）
```

左右对称部件**取相反符号**，形成交替迈步：

```python
if key == "leg_l":   e.transform.pitch =  leg_swing
if key == "leg_r":   e.transform.pitch = -leg_swing
if key == "arm_l":   e.transform.pitch = -arm_swing
if key == "arm_r":   e.transform.pitch =  arm_swing
else:                e.transform.pitch = 0.0     # 头/躯干/枪 不摆
```

> 只有 **leg_* 和 arm_** 这类 key 接受摆动角。要新增加动画的部位，
> 在这个 if/elif 链里加分支；不要给 head/torso 加 pitch（会点头，很怪）。

### 5.2 死亡动画（无布娃娃，用变换模拟）

```python
p = min(1.0, self._death_t / 0.25)          # 0.25s 内完成
roll   = p * math.radians(90.0)             # 绕前进方向侧倒 90°
squash = 1.0 - 0.85 * p                     # y 压扁到 15%

# 每个部件的局部偏移也要跟着转 + 压扁
cr, sr = math.cos(roll), math.sin(roll)
o = Vec3(off.x * cr - off.y * sr, (off.x * sr + off.y * cr) * squash, off.z)
e.transform.roll = roll
```

0.5s 后从场景移除（`physics.unregister` + 遍历 `self.parts` 移除各部件）。

### 5.3 受击 / 开火反馈（用颜色，不用粒子）

```python
# 受击：被命中部位提亮 1.25 倍，0.08s
brighten = 1.25 if self._hit_flash_t > 0 else 1.0
e.renderer.color = tuple(min(1.0, c * brighten) for c in color)   # 只给 head/torso

# 开火：枪身颜色换成自发光琥珀，0.06s
e.renderer.color = _FLASH_COLOR if self._flash_t > 0 else _GUN_COLOR
```

> 这两个都是**纯色替换**，零 draw call、零纹理、零粒子系统。

---

## 6. 改造现有角色的步骤

假设要把 `HumanoidBot` 改成星球大战风格：

1. **新建部件表常量**（别改旧的，便于回退）
   ```python
   _SW_COLOR_ARMOR  = (0.52, 0.55, 0.58)   # 装甲灰
   _SW_COLOR_CLOTH  = (0.78, 0.74, 0.66)   # 内衬米色
   _SW_COLOR_HELMET = (0.50, 0.53, 0.56)
   ```

2. **写新 spec 表**，部件名可以自定义，但要保留 `leg_l/leg_r/arm_l/arm_r`
   这四个 key（`_sync_parts` 的摆动逻辑依赖它们），必要时同步改 if/elif 链

3. **决定要不要保留"头"这个 key** —— `combat.py` 的爆头判定按几何位置算，
   与部件名无关；但如果头盔做大了，视觉和判定盒会不一致

4. **同步 `combat.py` 的命中盒**（规则 3）：改了身高/头位就必须改
   `HEAD_OFFSET/HEAD_HALF/BODY_OFFSET/BODY_HALF`

5. **同步 `BODY_HALF_Y`**（`bots.py` 顶部 0.91）—— 它同时用于物理体高度和脚底换算

6. **投影部件砍到 2 个**，检查新表的 shadow 列

7. **跑自检**：`python main.py --selftest`，21 项必须全绿
   （其中 `bots_spawned` / `hitscan_kill` / `headshot_bonus` 直接覆盖角色与命中盒）

---

## 7. 完整模板

```python
import math
from engine.scene import Entity
from engine.physics import PhysicsBody
from engine.mesh import box_mesh, MeshRenderer
from engine.math_utils import Vec3

BODY_HALF_Y = 0.91                      # 物理体半高（脚底 ↔ 中心换算）


def _rot_local(off, yaw):
    s, c = math.sin(yaw), math.cos(yaw)
    return Vec3(off.x * c - off.z * s, off.y, -off.x * s - off.z * c)


class MyUnit(Entity):
    def __init__(self, scene, physics, pos_xz, color_main, color_accent):
        super().__init__("unit", "unit")
        self.scene = scene

        # 1) 唯一物理体：全身 AABB
        self.transform.position = Vec3(pos_xz[0], BODY_HALF_Y, pos_xz[1])
        self.body = PhysicsBody(self, "aabb",
                                half=Vec3(0.32, BODY_HALF_Y, 0.32),
                                mass=65.0, friction=0.0)
        physics.register(self.body)

        # 2) 部件表：外观的唯一入口
        spec = (
            ("head",  (0.34, 0.32, 0.34), (0.0, 1.66, 0.0),   color_accent, 24.0, 0.20, True),
            ("torso", (0.60, 0.70, 0.34), (0.0, 1.15, 0.0),   color_main,   20.0, 0.15, True),
            ("leg_l", (0.22, 0.80, 0.26), (-0.14, 0.40, 0.0), color_main,   20.0, 0.15, False),
            ("leg_r", (0.22, 0.80, 0.26), (0.14, 0.40, 0.0),  color_main,   20.0, 0.15, False),
            ("arm_l", (0.16, 0.58, 0.20), (-0.38, 1.18, 0.06),color_main,   20.0, 0.15, False),
            ("arm_r", (0.16, 0.58, 0.20), (0.38, 1.18, 0.06), color_main,   20.0, 0.15, False),
        )

        # 3) 生成部件实体（无物理体、无逻辑）
        self.parts = {}
        for key, size, off, color, shin, sp, shadow in spec:
            e = Entity("unit_" + key, "unit_part")
            e.renderer = MeshRenderer(box_mesh(*size), color=color,
                                      cast_shadow=shadow,
                                      shininess=shin, spec_strength=sp)
            scene.add(e)
            self.parts[key] = [e, Vec3(*off), color]

        self.yaw = 0.0
        self._walk_phase = 0.0
        self._sync_parts(0.0)
        scene.add(self)

    def base_pos(self):
        p = self.transform.position
        return Vec3(p.x, p.y - BODY_HALF_Y, p.z)

    def _sync_parts(self, dt):
        base = self.base_pos()
        leg_swing = math.sin(self._walk_phase) * math.radians(25.0)
        arm_swing = math.sin(self._walk_phase) * math.radians(8.0)
        for key, (e, off, color) in self.parts.items():
            e.transform.position = base + _rot_local(off, self.yaw)
            e.transform.yaw = self.yaw
            if key == "leg_l":     e.transform.pitch = leg_swing
            elif key == "leg_r":   e.transform.pitch = -leg_swing
            elif key == "arm_l":   e.transform.pitch = -arm_swing
            elif key == "arm_r":   e.transform.pitch = arm_swing
            else:                  e.transform.pitch = 0.0
```

---

## 8. 常见错误

| 错误 | 后果 |
|---|---|
| 给部件挂 `PhysicsBody` | 无碰撞过滤 → 部件与身体互推，角色抖动 |
| 忘了 `base_pos()` 换算 | 部件整体上移 0.91m，脚悬空 |
| 每帧只算一次部件坐标 | 角色不转向，部件原地不动 |
| 全部件 `cast_shadow=True` | draw call 翻 3 倍，帧率崩 |
| 改了身高没改 `BODY_HALF_Y` | 物理体与视觉错位，卡地/悬空 |
| 改了头位没改 `combat.py` 命中盒 | 打头不触发爆头倍率 |
| 深色基色（< 0.5） | 违反 pastel 明度下限，被环境光洗灰 |
| 部件数超过 12 | 同屏 5 个时 240+ draw call |
| 用固定像素当锚点写死坐标 | 窗口缩放/换分辨率后错位（用相对锚点） |
| 加新发光件时申请新聚光灯 | `MAX_SPOTS=3` 已满，必然失效 → 用 `unlit=True` |

---

## 9. 自检要求

任何角色改造后必须：

```bash
python main.py --selftest
```

期望末行 `RESULT: PASS`（21 项）。直接相关的断言：

| 断言 | 覆盖什么 |
|---|---|
| `bots_spawned` | 角色能正常创建、注册、同步 |
| `hitscan_kill` | 躯干盒能被打中（尺寸/偏移错位会失败） |
| `headshot_bonus` | 头盒位置正确（爆头倍率能触发） |
| `positions_finite` | 坐标无 NaN/Inf（除零、sin/cos 未归一） |
| `entities_alive` | 实体数正常（部件泄漏会异常） |

**视觉不能只靠断言**：角色是"对"的（数值）不代表"好看"（观感）。
需手动跑窗口模式目检，并对照 `docs/art/visual-style.md` §0 三原则核对配色。

---

*本规则基于 `game/bots.py`、`engine/mesh.py`、`engine/scene.py`、
`engine/transform.py`、`engine/physics.py`、`docs/art/visual-style.md` 的实际代码整理。*
