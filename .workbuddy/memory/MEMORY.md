# mini_world 项目长期记忆

## 运行环境（2026-09-27 定）

- **使用 `.venv313`**（Python 3.13.14，项目根目录下）：pygame-ce 2.5.8 + moderngl 5.12.0 + glcontext 3.0.0。
- 运行：`./.venv313/Scripts/python.exe main.py`；自检：`./.venv313/Scripts/python.exe main.py --selftest`。
- 旧 `.venv`（Python 3.15.0a8 alpha）已删除（2026-09-27，用户确认）：glcontext 无 cp315 wheel 且无 MSVC 可编译。
- 本机 pip 清华镜像不可用，装包必须显式 `--index-url https://pypi.org/simple/`。

## 架构速记

- 分层：`game → graphics → engine`（声明如此，实际有 2 处违规：core.py import graphics.renderer；hud.py compose_lines 读 app 玩法属性）。
- 物理：固定步 1/120s；动态-动态碰撞双重循环未去重（每对解算 2 次）；O(n²) 无 broadphase。
- 渲染：OpenGL 3.3 Core；最多 3 盏聚光灯（MAX_SPOTS=3）；阴影 1024² PCF 3×3。

## 玩法体系（2026-09-30 定）

- 双模式：进入页 `[1]` 波次挑战（5 波人形 bot FPS）/ `[2]` 观光（原物理 demo）。GDD：`design/gdd/fps-wave-mode.md`。
- 关键模块：game/combat.py（hitscan+爆头双 AABB+回血+Tracer/Pickup/击退）、weapon.py（基类+MW-1 步枪/SR-1 狙击 ADS）、bots.py（人形 FSM）、waves.py（波次+debug_spawn）；引擎新增 ray_aabb/鼠标按键/CameraRig.kick/HUD 通用绘制/project_to_screen。
- 成长系统：波次三选一升级（生命/火力/防护）、掉落拾取（血包+30HP／弹药包步枪+30·狙击+5）、步枪初始备弹 60（波间不补弹）、击退 k/(mass+armor)（玩家 55/兵 25/精英 60）。
- selftest 21 项断言（--frames 3000，seed 20250913）；挑战模式改动必须保持全绿。

## 协作约定（2026-09-30 用户定）

- **验收以 selftest 21 项全绿为准**，不把 QA 截图当验收门槛。
- **例外（2026-09-30 用户补充）**：排查 HUD 布局类问题时，允许把 `HUD.render()` 的输出渲成 PNG 逐张目检——布局/遮挡/截断类缺陷用断言测不出来（`measure_card` 与真实渲染误差 0px，"面板偏左"这类主观描述也复现不了），必须看图才能证伪。目检产物放 `_qa_shots/`，纯诊断用的重复图及时删掉，只留前后对比证据。
- **报 bug 先复现再改**：用户描述可能与代码不同源（ENG-005 里描述写"7 波"实为 5 波、面板偏左实际正常）。先渲染/测量取证，**照着描述改会改坏本来正确的代码**；确认复现不了就如实说明，并去找描述里可用的线索（那次"提示遮盖"就指向了真 bug）。
- **动渲染 / HUD 贴屏后加跑 `--glcheck`**：headless 自检覆盖不到 GL 合成（见「GL 合成层」）。

## 视觉体系（2026-09-29 定）

- 风格：pastel 低多边形卡通，规格文档 `docs/art/visual-style.md`（调色板/光照/材质/HUD 四节字面值 + §5 落地清单）。调视觉先看它，改完回 §0 三原则核对（明度 0.6~0.95、RGB 通道差 ≤0.45、天色=雾色=清屏色）。
- HUD：左上角圆角胶囊卡片，行标记三态 True/False/"dim"；双字号经 `HUD._load(size, bold)` 加载（CJK 回退保留）。

## HUD 布局约定（2026-09-30 ENG-005 定，改 HUD 前必读）

- **面板尺寸不写死**：一律由 `HUD.measure_card` 按当帧文字实测得出（逐段量宽取最大值 + 内边距），换语言/加文案/换字号自动跟随。实测 `measure_card` 与真实渲染误差 0px。
- **必须夹紧画布**：`HUD.draw_card_at` 已把卡片左上角夹进 `[0, 画布-尺寸]`。新增卡片若绕过此函数自行 blit，务必自行夹紧——3 行弹药卡在 `y=H-78` 处底边会超画布 25px 把末行切掉。
- **居中元素用相对锚点**：`W/2`、`H/2`，不要固定像素坐标。
- **顶部横幅纵向位置**由 `App._banner_center_y()` 按左上状态卡实测高度推出。1280×720 下状态卡 x 16..653、横幅中心恰在 x=640，二者横向必然重叠——写死 y=64 会遮盖状态卡尾部"击杀/FPS"（真 bug，已修）。
- **`H` 是全局键**：开关处理在 `App.on_update` 状态分发**之前**（`_handle_shortcuts` 在 TITLE/RESULT 根本不被调用）；按键表是居中卡片 `App._help_card_lines()`，在 `get_hud_cards` 末尾追加故画在最上层；输入与显示解耦，帮助开着仍能按 1/2/3 选升级。

## 状态机与退出流程（2026-09-30 ENG-006 定）

- 状态：`TITLE / EXPLORE / COMBAT / INTERMISSION / RESULT_WIN / RESULT_LOSE / QUIT`。`QUIT` 是退出确认页，冻结模拟（`simulation_active()` 里与两个 RESULT 一起排除）。
- **`Esc` 进不了 `Input`**（`core._process_events` 的 KEYDOWN 分支里 `continue` 掉了），游戏层想响应 Esc **只能**用 `Engine.on_escape()` 钩子，返回 True=消费，默认 False=走引擎原行为（捕获态释放鼠标 / 未捕获直接退出）。
- 任何页面都不会"按一下 Esc 就杀进程"：`Enter`（或 `Space`/小键盘 Enter）是**唯一**真正结束进程的入口，走 `Engine.request_quit()`。`T` 随时回标题页。
- 交战/间隙/观光**不拦** Esc（鼠标捕获态，Esc 只释放鼠标），第二次才弹确认页。

## 窗口缩放（2026-09-30 ENG-005 补齐）

- 窗口已加 `pygame.RESIZABLE`；`Engine._process_events` 处理 `VIDEORESIZE` → `Engine.handle_resize(w,h)`。
- `handle_resize` 顺序：钳制到 `MIN_WIDTH/MIN_HEIGHT`(480×320) → 跳过同尺寸 → 更新 `self.width/height` → `pygame.display.set_mode`（失败则 `except pygame.error` 保留上一档）→ `HUD.resize()` 重建画布 → `Renderer.resize()` 重建 `hud_tex` → `on_resize()` 钩子。
- **`App.on_resize` 必须覆写更新 `_aspect`**，否则投影矩阵沿用旧比例，窗口变形后 3D 画面与准星一起拉伸。
- 与尺寸绑定的只有 `HUD.surface` 和 `Renderer.hud_tex`；阴影 FBO 固定 `shadow_res²`、网格 VAO 与尺寸无关，**不要重建**；viewport 每帧从 `self.width/height` 取，改属性即可。
- 引擎基类**不**知道宽高比，`_aspect` 属玩法/相机侧，因此用 `on_resize` 钩子而非引擎直接改。

## 射击手感与特效约定（2026-09-30 ENG-007 定，改 combat/weapon/effects 前必读）

- **姿态参数**集中在 `player.py` 顶部三张表（`POSTURE_HALF_Y`/`POSTURE_EYE`/`POSTURE_SPEED`）。起身受阻判定用 `aabb_vs_aabb` 比"新增占据体积"，**不要改回射线探查**——`ray_aabb` 的 hitscan 语义下"起点在盒内"恒返回 0.0，会把所有姿态误判成受阻。
- **表面朝向**：`M = T·Ry·Rx·Rz`，局部 +Y 映为 `(cos yaw·sin p, cos p, sin yaw·sin p)`，故对齐法线 n 用 `pitch=acos(n.y)`、`yaw=atan2(n.x, n.z)`。这是唯一正确的组合，其他符号变体只能对上部分方向。
- **弹痕必须走独立的半透明 decal pass**（renderer 2.5 段，`u_alpha` uniform + `MeshRenderer.decal/alpha`）。主 pass 不开 BLEND，alpha 恒 1，在主 pass 里画弹痕就淡不出。
- **对象池有界**：特效上限 `MAX_DECALS=40`/`MAX_SPARKS=64`，超限原地 `revive()` 复用，不走 `Scene.remove` 的延迟删除队列。调大上限前先确认 draw call 预算。
- **弹痕只挂静态面**：bot 身上只出火花（尸体消失后弹痕悬空），动态木箱同理（贴面会脱节）。

## GL 合成层（2026-09-30 修，改渲染 / HUD 贴屏前必读）

- HUD 链路 = pygame 全屏 Surface → `hud_tex.write()` 上传 → **一个全屏 TRIANGLE_STRIP quad** 贴回屏幕（`graphics/renderer.py` 的 `hud_verts` / `vao_hud`）。布局层（`engine/hud.py`）只负责 Surface 里画什么，**贴屏不在它的职责里**——所以布局层的实测数据（尺寸/居中/夹紧）再干净也不能证明真窗口里没问题。
- **顶点顺序必须是 Z 字：左下 → 右下 → 左上 → 右上。** 写成 左下→右下→右上→左上 时，TRIANGLE_STRIP 的两个三角形 `{v0,v1,v2}` / `{v1,v2,v3}` 都落在屏幕右半边，左侧留下一个以画面中心为顶点的楔形空洞：HUD 只覆盖 **75%** 屏幕，表现是「蝴蝶结 / 两个梯形拼接 / 中间接缝 / 左侧文字被斜边切断」。2026-09-30 真窗口实测 75.00% → 100.00%。
- **这个缺陷 headless 测不到**：`--selftest` 无窗口无 GL；「渲染 HUD Surface 再叠假天空」的 QA 截图也必然正常（`_qa_shots/03,07,08,09` 的 3D 全黑就是没走 GL 的证据）。遇到「只有真窗口才有的 HUD 变形」，先跑 `--glcheck` 再怀疑布局。
- **`./.venv313/Scripts/python.exe main.py --glcheck`**（`engine/qa_gl.py`）：起真窗口（`SDL_VIDEO_WINDOW_POS` 置屏幕外、不抢鼠标、冻结场景），把 HUD 换成「纯色底 + 四角标记色」图案，读回默认帧缓冲断言 ①铺屏率 100% ②四角朝向正确；退出码 0/1，无 GL 环境自动 SKIP。已用「故意写错顶点顺序」反向验证过它会 FAIL（75.00%、退出码 1）。
- 读回默认帧缓冲必须在 `pygame.display.flip()` **之前**（flip 后后台缓冲内容未定义）；GL 读回行序自下而上，比对屏幕坐标要换算 `height-1-y`。
- 排除项（已实测，别再重复怀疑）：HUD 纹理上传正确（读回 `hud_tex` 逐像素一致）；不是 premultiply/blend 失配（换常量色片段着色器仍复现缺口）；不是行对齐（宽 1282 非 4 的倍数也不斜切）；不是 DPI/viewport 不一致（逻辑像素与 framebuffer 尺寸实测相同、均为 `width×height`）；Z-fighting 不可能（HUD pass `enable_only(BLEND)` 关掉了深度测试）。

## 程序化人形与暴风兵（2026-09-30 定，改 `game/units/` / `bots.py` 前必读）

- **gamma 2.2 会把"把颜色调深"的效果压扁**：着色器是 `albedo * u_ambient`（乘法），
  base color 线性压低结果，但感知被 gamma 强烈压缩 —— linear 0.256 只显示成约 0.54。
  所以"看起来不够深"不是错觉，而是可预测的：base 减半，屏幕上只暗一点点。
  实测暗部渲染亮度：base 0.50→0.648、0.26→0.526、0.16→0.454、0.12→0.43、0.07→0.365。
  要真黑必须压到 0.12 附近；再深就成了"洞"（低多边形靠明暗梯度读形体）。
  spec/spec_strength **不影响**暗部深浅（0.15 与 0.70 结果相同）。
- **Tracer 一直在生成，问题是在交战距离退化成亚像素**：`bots.py` 的 ENGAGE_MIN/MAX
  是 8~14m，原截面 0.025m 在 14m 处只有 1.07 px、20m 处 0.75 px，加 `LIFE=0.07s`(4 帧)
  等于没有。现 0.055m + `TAIL=1.2` + `LIFE=0.10`，玩家蓝/敌方红。
  **教训**："看起来没有"要先确认"到底生成没有"——数实体，别凭印象。
- **测"某个特效画出来没有"要数它自己的颜色像素，别做整屏 diff**。unlit 材质直接输出
  albedo，所以颜色是已知常量，可以精确匹配。整屏 diff 会被 knockback/AI 位移、
  双缓冲交换污染（我测出过"921600 px"——画布才 682000 px，明显是假数）。
  同理，**截"有没有尾迹"的图要把相机侧过来看**，沿弹道方向看会被前缩成几个像素。
- **性能数字必须同进程相对比较**：本机核显+共享机器，同一配置连测 4 次
  8.04/9.52/9.67/10.01 ms，抖动 25%。绝对值不可信。
  **合成场景（清空世界只放 bot）测出的帧时间严重偏低**，不能当游戏性能依据 ——
  真实世界还有 94 个实体 + HUD + 2 盏阴影灯，基线高得多。
  真实场景：空场景 9.99ms、同屏 5 只 30 部件 bot 12.86ms（增量 +2.87ms / +29%）。
- **部件数/外形改完后确认"生成入口"真的用了新类**：`waves.py` 走模块级 `BOT_CLASS`。
- **让黑色露出必须"加内衬 + 缩短白甲留缝"两件事一起做**：只加内衬会被更宽的白甲
  完全挡住（还是看不见）；只留缝而没有黑色底 = 空洞（射线直接打穿）。
  内衬要比白甲**窄**（宽/厚都小于被包裹的甲板），才只在缝隙里露出、不从侧面凸出。
  内衬还要**进摆动分组**跟所属肢体同步，否则摆动时肘/膝裂开 —— 静止截图看不出来。
- **判据可以比"不许有间隙"更强**：白甲之间的缝不要求为 0，而要求
  **"该缝必须被某个黑色内衬纵向覆盖"** —— 同时排除了空洞和脱节。
- **角色正面在世界 −Z，不是 +Z。** `bots.py` 的 `_rot_local(off, yaw)` 返回
  `(off.x*c - off.z*s, off.y, -off.x*s - off.z*c)` —— **yaw=0 时就把 z 取反**，
  `Transform.model_matrix()` 的 Ry 同约定。所以"局部 +z = 角色前方"成立，
  但它映射到世界 −Z。截产品图时相机方位角 **180° 才是正面**（我按"世界 +Z 是正面"
  截了一整轮"正面图"，全是背面，白白怀疑面罩/胸口细节的位置）。
- **每个角色只有 1 个 `PhysicsBody`**；命中盒是 `combat.py` 的模块级 AABB 常量
  （`HEAD_OFFSET/HEAD_HALF/BODY_OFFSET/BODY_HALF`），**所有兵种共用**，改它会同时改手感。
- **父类按 key 字符串硬编码的地方是隐藏地雷**：`HumanoidBot.update()` 的 SPAWN 入场态
  直接访问 `self.parts["torso"][0]`，改名即 `KeyError`。换部件表前先 grep 父类里所有
  `parts["..."]` 字面量。解法优先"把部件命名成父类要的 key"，而不是加别名
  （别名会让部件计数虚增，且 `_sync_parts` 遍历时同一实体被处理两次）。
- **`_sync_parts` 的 leg_l/arm_l 分发不能照搬**：一旦把腿细分成 thigh/knee/shin、
  臂细分成 upperarm/forearm/hand，就必须覆写 `_sync_parts` 并自建摆动分组表。
- **draw call 不是瓶颈，别拿它当主要约束。** 实测（Intel 核显 1280×720，`ctx.finish()` 同步）：
  场景基线 0 个 bot 就 6.00ms；draw call 55→960（17 倍）帧时间只涨 2.3 倍；
  真正的成本是**阴影 pass**（灯 2→0 省 26%）和**填充率**（降分辨率省 24%）。
  由此推翻旧认知：**175 FPS（5.71ms）本来就没达到**。预算已从 80 重定为 400。
- **改部件表后必须真的渲染出图**，AABB 断言会全绿而图是错的。本轮靠渲染抓出 3 个
  断言漏掉的缺陷：肩甲浮空 0.051m（判据只查"不许陷入"没查"不许浮空"）、
  腰部两处穿透空洞（两两关系验算覆盖不到"整体轮廓是否封闭"）、
  相机 aspect 没设导致画面纵向拉伸 2.9 倍（`head` 投影成 30×93px 才暴露）。
- **判据要成对写**：既要查 A 不得 X，也要查 A 不得非 X。只写一半等于没写。
- **轮廓类问题要单独验**：沿视线逐高度打 `ray_aabb` 扫一遍，正/背/侧三个方向各扫，
  且正面要在 x=0 与 x=±0.15 各打一条（只在 x=0 打会从两腿之间穿过去，全是假警报）。
- **数值验算也有"无解"的时候**：腹部纵向只有 0.34m，要塞"两片腹甲+三处小重叠"
  穷举返回 0 组可行解 —— 这时候该改设计（接缝靠 Z 深度差表达），不是继续调参数。
- **截产品图的三个前提**：①相机 `_aspect` 必须设成 `W/H`；②读帧缓冲在 `display.flip()`
  **之前**；③相机方位角与朝向符号要对（见本节第一条）。用 dummy 视频驱动建不出 GL 上下文，
  必须真实驱动 + `pygame.HIDDEN`。
- **调试渲染的杀手锏是"给每个部件唯一颜色"**：灰白盒子肉眼看不出是 core 还是 ab_upper，
  只能靠猜（我因此猜错三轮）。配 `fog.ambient≈0.95` 压平明暗，一次看清所有部件的相对位置。
- `_SW_DARK = 0.50` **有意低于 pastel 明度下限 0.60**，是全项目唯一一处破例，
  理由与授权写在 `docs/design/stormtrooper-design.md` §3。

## 加游戏状态 / 加关卡模式（2026-10 Boss 关）

- **状态集合必须集中**：新增 `ST_BOSS` / `ST_BOSS_BREAK` 时漏改了 3 处**硬编码状态列表**
  （`app.py` 的 `st in (...)`、`weapon.py` 的 `want_fire`、`weapon.py` 的 `_sync_view_model`），
  玩家反馈"Boss 战里左键右键都没反应" —— 因为 `weapon.update` 整帧没跑。
  已把常量与集合搬到 **`game/states.py`**（`GUN_STATES` / `PLAYABLE_STATES` /
  `SUPPLY_STATES` / `COMBAT_HUD_STATES` / `DROP_STATES`），`app.py` re-export `ST_*`。
  **规则：以后加状态只改 `states.py`**，回归由 `t_gun.py` 守（4 个战斗态各验左键/右键）。
- **`if/elif` 链的分支顺序**：`elif st in GUN_STATES:` 会先把 `ST_BOSS` 吃掉，
  后面另写的 `elif st == ST_BOSS:` **永远不会执行**（症状：打死 Boss 不升段）。
  子分支必须写在链内。
- **`NAV_POINTS` 曾是个定时炸弹**：`BaseBot._begin_reposition` 用了 `waves.py` 里的
  `NAV_POINTS` 却**没有 import** → 任何 bot 在 ENGAGE 里丢失视线超 `LOSE_LOS_TIME`
  就 `NameError` 崩掉整个游戏。已把数据搬到 `bots.py`，`waves.py` 转出。
  **教训：跨模块用常量先确认 import 存在**，pyflakes 不查未定义名（只查未使用）。
- **headless 驱帧要三件套**：`App.on_update` **不驱动 Scene** —— bot 的 FSM 由
  `Engine.run` 另调 `scene.update(dt)`。测试里必须自己补
  `physics.step()` + `scene.update()` + `flush_removed()`，否则 bot 永远停在 `SPAWN`。
- **`Input.simulate` 只写 `keys_down`（按住），不写 `keys_pressed`（边沿）**：
  模拟"按一下"必须**手动** `input.keys_pressed.add(key)`，
  且 `begin_frame()` 要在 `simulate` **之前**调（它先清空）。
  否则 `was_pressed` 恒 False —— 我为此误判过一次"接线失败"。
- **朝向/向量换算一律用引擎函数**（`dir_from_yaw_pitch` / `yaw_pitch_from_dir`），
  **不要手写 `atan2`**：本项目角色**正面是世界 −Z**（`_rot_local` 在 yaw=0 时把 z 取反），
  手写公式在 yaw≠0 时与引擎约定不一致，会把目标放到玩家背后。
- **测试里断言"打中了"要先去随机**：步枪有散布 + bloom，3 发可能全脱靶 → 假红。
  改 `weapon.base_spread_deg`（`spread_deg` 是只读 property）；并每帧重新瞄准。
- **测"位移"会与"设计好的提前中断"打架**：Boss 撞墙时冲刺本来就该提前收，
  此时位移当然到不了 17m/s × 0.55s。判据用**速度**（与墙无关）或换空旷方向单独量。
- **`PhysicsBody` 没有 `position` 字段**（只有 half/radius/velocity/…），
  实体位置在 `entity.transform.position`。
- **`_begin_dash` 之类"读当前方向做 raycast"的函数，改方向必须放在调用之前**，
  否则预算按旧方向算（测试里顺序反了 → 0.063s）。
- **命中盒判据不是"头嵌在躯干里"**（人形 bot 也不满足），而是**没有竖直缝隙**：
  躯干盒要连续覆盖 y=0 到头盒下沿，否则侧向射线从缝里穿过去。
- **球体只能用叠盒近似**（项目只有 `box_mesh`）：层间 z 错开 0.01 避免共面 z-fighting。
  共面只对**同朝向表面**有害，嵌套盒不算。
- **加同屏实体数要同时报 draw call**：一只奶龙 33 个 draw call，4 只 = 132
  （全场 217 / 400 预算，headless 逻辑帧 0.87ms）。数量翻倍会线性吃预算，
  **6 只以上会破**。加完必须跑一次实体计数，别只看逻辑帧。
- **敌人数翻倍要重算 incoming DPS，不能只加数量**：乘积 = 发/s × 单发 × 只数。
  4 只 × 14 伤害 = 52 dps → 100 HP 的玩家 1.9s 就没了（实测不可玩）；
  降到 8 伤害 → 29.8 dps 才可玩。难度应来自**空间压迫**（盯 4 个方向），
  不是血量叠罗汉。
- **多实例状态机**：`self.boss`（单例）→ `self.mobs`（本层列表）时，
  清空判据要写成 `_spawned and len(mobs) > 0 and not any(alive)` ——
  **少判「mobs 非空」会把「还没刷」误判成「已清空」**，直接跳关。
- **测试里量性能增量，基线必须是「零该实体」的场景**：我曾拿「已经死了一只
  Boss 且已 flush」的 app 当基线，差值算成负数，看着像新实体不占 draw call。
- **多只同类 AI 在开阔地会互相顶住卡在 APPROACH**：两只 Boss 从 34m 外朝
  同一玩家收拢时卡了 900 帧没进 ENGAGE。单元测试聚焦子状态机时应先把
  同伴移出场景再测。

## 平台踩坑记录（2026-09-27 修）

- **pygame-ce 2.5.8 API 差异**：相对鼠标模式是 `pygame.mouse.set_relative_mode(bool)`（不是原版 pygame 的 `set_relative_mouse_mode`，也不在 pygame.event 下）。
- **uniform 数组名驱动差异**：NVIDIA 报 `x[0]`、Intel/Mesa 报 `x`。renderer.py 已加 `self._ukey` 探测；sampler 数组必须整体 `.value = tuple(...)` 写入，勿逐元素按键名访问。
- **双显卡路由**：本机隐藏窗口的 GL 上下文落在 Intel 核显（"GL: Intel(R) Graphics"）。若要 RTX 5060 跑：Windows 图形设置里把 `.venv313/Scripts/python.exe` 设为高性能。功能上 Intel 核显跑 GL 3.3 无问题。
- **窗口模式冒烟法**：monkeypatch `pygame.display.set_mode` 加 `pygame.HIDDEN` flag → 无窗口但 GL 链路全真，可自动化验证。
