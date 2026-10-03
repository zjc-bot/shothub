# -*- coding: utf-8 -*-
"""HUD：pygame 渲染中文文本 → 生成覆盖层 Surface（由 Renderer 作为纹理上屏）。

视觉规格（docs/art/visual-style.md §4）：
- 左上角 (16,14) 悬浮圆角胶囊卡片（border_radius=12，自适应宽度），
  左侧 4px 琥珀强调条；帮助页为独立第二张卡片（垂直间距 8px）。
- 双字号：标题 22 bold（font_title）/ 正文 18（font_body）。
- 行标记三态：True→强调琥珀（L1）、False→正文白（L2）、"dim"→灰蓝（L3/帮助行）。
- 帮助行按键 token（[X] 段）琥珀高亮、说明文字灰蓝，逐段渲染累加 x。

字体策略：自动在系统字体中寻找常见中文字体；找不到则回退默认字体
（此时 HUD 退化为英文标签，保证任何环境可运行）。
"""
import re

import pygame

# 常见中文字体族名（pygame.font.get_fonts() 的小写无空格形式）
_CJK_CANDIDATES = [
    "notosanscjksc", "notosanscjk", "notosanssc", "sourcehansanssc",
    "wenquanyimicrohei", "wenquanyizenhei", "droidsansfallback",
    "microsoftyahei", "msyh", "simhei", "simsun", "pingfangsc",
    "hiraginosansgb", "stkaiti", "stheiti",
]

# 中文字体不可用时的英文对照
_EN = {
    "迷你世界": "MiniWorld", "视角": "Cam", "第一人称": "FPS",
    "跟随": "Follow", "手电筒": "Torch", "开": "ON", "关": "OFF",
    "锥角": "Cone", "强度": "Power", "衰减": "Falloff", "帮助": "Help",
    "实体": "ents", "帧率": "FPS",
    # ---- Sprint 2（GDD §7 HUD 元素）新增文案 ----
    "弹匣｜备弹": "MAG | RESERVE", "换弹中…": "Reloading...",
    "第 %d 波 · 敌人 %d": "Wave %d · Enemies %d",
    "第 %d 波已肃清 · 下一波 %ds": "Wave %d cleared · Next in %ds",
    "准备迎战 · 第 1 波 %ds": "Get ready · Wave 1 in %ds",
    "+1 击杀": "+1 Kill", "爆头击杀 +1": "Headshot Kill +1",
    # ---- Sprint 3（ENG-004 需求 E/F）新增文案 ----
    "选择强化": "Choose Upgrade",
    "[1] 生命强化：HP 上限 +25 且 HP +25":
        "[1] Vitality: Max HP +25 & HP +25",
    "[2] 火力强化：伤害 ×1.25": "[2] Firepower: Damage x1.25",
    "[3] 防护强化：护甲 +30 且减伤 +8%": "[3] Guard: Armor +30 & DR +8%",
    "HP上限 %d · 火力 ×%.2f · 防护 %d · 减伤 %d%%":
        "MaxHP %d · Dmg x%.2f · Armor %d · DR %d%%",
    "强化：%s": "Upgrades: %s",
}

# 按键 token 切分（"[F]"、"[Tab]" 等括号段高亮）
_TOKEN_RE = re.compile(r"(\[[^\]]+\])")


class HUD:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.surface = pygame.Surface((width, height), pygame.SRCALPHA)
        self.show_help = False

        self.using_cjk = False
        self.font_title = self._load(22, bold=True)
        self.font_body = self._load(18)

        # 卡片配色（visual-style §4.3）
        self.bar_color = (16, 24, 38, 165)      # 卡片底：深夜蓝半透明
        self.text_color = (244, 248, 252, 255)  # 正文：暖白
        self.hl_color = (255, 214, 110, 255)    # 强调：琥珀
        self.dim_color = (166, 180, 198, 255)   # 次要：灰蓝
        self.accent_bar = (255, 214, 110, 255)  # 左侧强调条 = hl_color

        # Sprint 2 战斗 HUD 配色（visual-style §1.4/§0 字面值 ×255）
        self.hp_green = (122, 199, 133)         # 叶绿 (0.48,0.78,0.52) >50%
        self.hp_amber = (255, 214, 110)         # 琥珀 25~50%（= hl_color）
        self.hp_coral = (230, 107, 97)          # 珊瑚红 (0.90,0.42,0.38) <25%
        self.hurt_red = (230, 107, 97)          # 受击 vignette / 击杀标记

    def resize(self, width, height):
        """窗口尺寸变化后重建覆盖层画布（内容每帧重绘，无需搬运）。"""
        if (width, height) == (self.width, self.height):
            return
        self.width, self.height = width, height
        self.surface = pygame.Surface((width, height), pygame.SRCALPHA)

    # ------------------------------------------------------------------
    def _load(self, size, bold=False):
        """加载一个字号：优先 CJK，回退等宽字体（两个字号各自独立回退）。"""
        font = self._try_load_cjk_font(size)
        if font is None:
            font = pygame.font.SysFont(
                "consolas,dejavusansmono,monospace", size)
        if bold:
            font.set_bold(True)
        return font

    def _try_load_cjk_font(self, size):
        """命中 CJK 字体则返回 Font 并标记 using_cjk；找不到返回 None。"""
        try:
            fonts = set(pygame.font.get_fonts())
            for name in _CJK_CANDIDATES:
                if name in fonts:
                    self.using_cjk = True
                    return pygame.font.SysFont(name, size)
            # 部分系统以文件形式提供 Noto CJK
            for path in ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                         "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
                         "C:/Windows/Fonts/msyh.ttc",
                         "/System/Library/Fonts/PingFang.ttc"):
                try:
                    font = pygame.font.Font(path, size)
                    self.using_cjk = True
                    return font
                except Exception:
                    continue
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    def _line_font(self, hl):
        # "big"：标题字号（22 bold）但正文暖白色（弹药计数主行用）
        return self.font_title if hl is True or hl == "big" \
            else self.font_body

    def _line_color(self, hl):
        if hl is True:
            return self.hl_color
        if hl == "dim":
            return self.dim_color
        return self.text_color

    def _line_width(self, text, hl, token_split):
        font = self._line_font(hl)
        if token_split:
            return sum(font.size(seg)[0]
                       for seg in _TOKEN_RE.split(text) if seg)
        return font.size(text)[0]

    def _draw_card(self, s, x, y, lines, token_split):
        """绘制一张圆角卡片（含左侧强调条与逐行文本），返回卡片高度。"""
        accent_w = 4
        pad_x, pad_y, gap = 14, 10, 4
        widths = [self._line_width(text, hl, token_split)
                  for text, hl in lines]
        heights = [self._line_font(hl).get_linesize() + gap
                   for _, hl in lines]
        w = accent_w + pad_x + (max(widths) if widths else 0) + pad_x
        h = pad_y * 2 + sum(heights)

        pygame.draw.rect(s, self.bar_color, (x, y, w, h), border_radius=12)
        pygame.draw.rect(s, self.accent_bar, (x, y, accent_w, h))

        ty = y + pad_y
        for (text, hl), line_h in zip(lines, heights):
            font = self._line_font(hl)
            tx = x + accent_w + pad_x
            if token_split:
                # 按键 token 高亮：括号段琥珀、其余灰蓝，逐段累加 x
                for seg in _TOKEN_RE.split(text):
                    if not seg:
                        continue
                    seg_color = (self.hl_color
                                 if seg.startswith("[") and seg.endswith("]")
                                 else self.dim_color)
                    surf = font.render(seg, True, seg_color)
                    s.blit(surf, (tx, ty))
                    tx += surf.get_width()
            else:
                surf = font.render(text, True, self._line_color(hl))
                s.blit(surf, (tx, ty))
            ty += line_h
        return h

    # ------------------------------------------------------------------
    # E3：任意位置文本卡片（进入页 / 结算页 / 后续 HUD 元素复用）
    # ------------------------------------------------------------------
    def measure_card(self, lines, token_split=False):
        """测量一张卡片的 (宽, 高)，不绘制。"""
        accent_w = 4
        pad_x, pad_y, gap = 14, 10, 4
        widths = [self._line_width(text, hl, token_split)
                  for text, hl in lines]
        heights = [self._line_font(hl).get_linesize() + gap
                   for _, hl in lines]
        w = accent_w + pad_x + (max(widths) if widths else 0) + pad_x
        h = pad_y * 2 + sum(heights)
        return w, h

    def draw_card_at(self, surface, x, y, lines, token_split=False,
                     center=False, alpha=255):
        """在 (x, y) 画一张卡片；center=True 时 (x, y) 为卡片中心点。

        面板尺寸由 measure_card 按当帧文字**实测**得出（每段独立量宽后取
        最大值 + 内边距），不是写死的常量，所以换语言/加文案/换字号都会
        自动跟随，不会溢出。

        x/y 先按 center 换算成左上角，再夹紧进画布：极窄窗口或超长文案下
        也不会把文字从左/上边缘裁掉（GDD §7 各 HUD 元素共用此路径）。

        alpha < 255 时先离屏绘制再整体调透明度上屏（波次横幅渐隐用）。
        """
        s = surface if surface is not None else self.surface
        w, h = self.measure_card(lines, token_split)
        if center:
            x -= w / 2.0
            y -= h / 2.0
        # 夹紧到画布内（卡片比画布还宽时退化为贴左上角，保证开头可读）
        x = max(0, min(int(x), s.get_width() - w))
        y = max(0, min(int(y), s.get_height() - h))
        if alpha >= 255:
            return self._draw_card(s, x, y, lines, token_split)
        tmp = pygame.Surface((w, h), pygame.SRCALPHA)
        self._draw_card(tmp, 0, 0, lines, token_split)
        tmp.set_alpha(max(0, int(alpha)))
        s.blit(tmp, (x, y))
        return h

    # ------------------------------------------------------------------
    # E3 完整化（GDD §7 / Sprint 2）：通用绘制原语
    # 与卡片同语言：深夜蓝底 / 圆角 / 琥珀强调 / 双字号；全部由 game 层
    # 以指令列表驱动（HUD.render 的 cards 参数按 "kind" 分发，见 render）。
    # ------------------------------------------------------------------
    def draw_bar(self, s, x, y, w, h, ratio, fill,
                 back=(244, 248, 252, 40), alpha=255):
        """通用进度条：深色底 + 圆角填充。fill 为 RGB 三元组。"""
        ratio = max(0.0, min(1.0, ratio))
        pygame.draw.rect(s, back, (x, y, w, h), border_radius=4)
        fw = int(round(w * ratio))
        if fw > 0:
            color = (fill[0], fill[1], fill[2], max(0, min(255, int(alpha))))
            radius = max(0, min(4, fw // 2, h // 2))
            pygame.draw.rect(s, color, (x, y, fw, h),
                             border_radius=radius)

    def draw_hp_card(self, s, x, y, label, ratio, flash=False, pulse=1.0):
        """血量卡片（GDD §7）：强调条 + "HP" 标签 + 220×14 条形。

        填充色随比例：>50% 叶绿 / 25~50% 琥珀 / <25% 珊瑚红；
        <25% 时强调条换珊瑚红且与条形一起按 pulse(0~1) 做 α 脉冲；
        flash=True（受击帧）整条闪白。返回卡片高度。
        """
        ratio = max(0.0, min(1.0, ratio))
        low = ratio < 0.25
        if flash:
            fill = (255, 255, 255)
        elif ratio > 0.5:
            fill = self.hp_green
        elif low:
            fill = self.hp_coral
        else:
            fill = self.hp_amber
        pa = int(255 * max(0.15, min(1.0, pulse))) if low else 255

        bar_w, bar_h = 220, 14
        font = self.font_body
        lw = font.size(label)[0]
        content_h = max(font.get_linesize(), bar_h)
        pad_x, pad_y = 14, 10
        w = 4 + pad_x + lw + 10 + bar_w + pad_x
        h = pad_y * 2 + content_h

        pygame.draw.rect(s, self.bar_color, (x, y, w, h), border_radius=12)
        accent = self.hp_coral if low else self.accent_bar
        pygame.draw.rect(s, (accent[0], accent[1], accent[2], pa),
                         (x, y, 4, h))
        label_surf = font.render(label, True, self.text_color)
        s.blit(label_surf,
               (x + 4 + pad_x,
                y + pad_y + (content_h - font.get_linesize()) // 2))
        self.draw_bar(s, x + 4 + pad_x + lw + 10,
                      y + pad_y + (content_h - bar_h) // 2,
                      bar_w, bar_h, ratio, fill, alpha=pa)
        return h

    def draw_crosshair(self, s, cx, cy, gap, color, arm=8, thick=2):
        """动态准星：4 短线 + 中心点；gap = 中心到线端的像素间隙。"""
        cx, cy, gap = int(cx), int(cy), int(gap)
        pygame.draw.line(s, color, (cx, cy - gap),
                         (cx, cy - gap - arm), thick)
        pygame.draw.line(s, color, (cx, cy + gap),
                         (cx, cy + gap + arm), thick)
        pygame.draw.line(s, color, (cx - gap, cy),
                         (cx - gap - arm, cy), thick)
        pygame.draw.line(s, color, (cx + gap, cy),
                         (cx + gap + arm, cy), thick)
        pygame.draw.circle(s, color, (cx, cy), 2)

    def draw_hit_marks(self, s, cx, cy, mark):
        """命中标记（GDD §2.4）：准星四角 4 短斜杠。

        body→暖白 / head→琥珀 / kill→珊瑚红加长（× 形观感）。
        显示时长由 game 层控制（120/150/200ms）。
        """
        color = {"body": self.text_color, "head": self.hl_color,
                 "kill": self.hp_coral}[mark]
        cx, cy = int(cx), int(cy)
        off, ln = 12, 8 if mark == "kill" else 6
        for sx in (-1, 1):
            for sy in (-1, 1):
                pygame.draw.line(s, color,
                                 (cx + sx * off, cy + sy * off),
                                 (cx + sx * (off + ln),
                                  cy + sy * (off + ln)), 2)

    def draw_vignette(self, s, alpha, color=None, thickness=None):
        """受击闪红：全屏四边半透明红条（GDD §2.4，α 由 game 层渐隐）。"""
        if alpha <= 0:
            return
        w, h = s.get_size()
        t = thickness or max(28, h // 9)
        r, g, b = color if color is not None else self.hurt_red
        c = (r, g, b, min(255, int(alpha)))
        pygame.draw.rect(s, c, (0, 0, w, t))
        pygame.draw.rect(s, c, (0, h - t, w, t))
        pygame.draw.rect(s, c, (0, t, t, h - 2 * t))
        pygame.draw.rect(s, c, (w - t, t, t, h - 2 * t))

    def draw_float_texts(self, s, cx, base_y, items):
        """击杀飘字：items = [(text, color, alpha, rise_px)] 旧→新，
        从 base_y 向上堆叠，每条按 rise 上浮、按 alpha 渐隐。"""
        y = base_y
        for text, color, alpha, rise in items:
            surf = self.font_body.render(text, True, color)
            surf.set_alpha(max(0, min(255, int(alpha))))
            s.blit(surf, (int(cx - surf.get_width() / 2),
                          int(y - rise - surf.get_height() / 2)))
            y -= self.font_body.get_linesize() + 6

    def draw_mob_bar(self, s, cx, y, w, h, ratio, elite=False):
        """bot 头顶血条（ENG-004 需求 A2）：深色底 + 圆角填充条，
        填充色与玩家血条同色板；精英加 1px 珊瑚红描边。"""
        ratio = max(0.0, min(1.0, ratio))
        if ratio > 0.5:
            fill = self.hp_green
        elif ratio < 0.25:
            fill = self.hp_coral
        else:
            fill = self.hp_amber
        x, y, w, h = int(cx - w / 2), int(y), int(w), int(h)
        self.draw_bar(s, x, y, w, h, ratio, fill)
        if elite:
            pygame.draw.rect(s, self.hp_coral, (x, y, w, h), 1,
                             border_radius=4)

    def draw_boss_bar(self, s, cx, y, w, h, ratio, name="", stage=""):
        """Boss 关专属血条：**屏幕顶部固定**的一条，不是头顶跟随。

        为什么不能复用 `draw_mob_bar`（头顶跟随）：
        - Boss 血量 1000，追一个在 3D 空间里移动的头顶会让血条乱晃；
        - Boss 体型 2.86m 高、离得远时头几乎不可见，血条会突然消失；
        - 玩家需要**随时知道还剩多少血**才能决定要不要继续输出或绕后。

        故固定在顶部，**每只 Boss 一条**（第 1 层 4 只 -> 4 条），
        颜色随血量变化（绿->琥珀->珊瑚），与玩家血条同一色板。

        名字**右对齐贴在血条左侧**而不是居中在条上方：4 条堆叠时，
        居中标题会互相压；右对齐成一列，扫一眼就知道有哪几只。
        `stage`（层数）只由第一条带，居中画在最上面那条的上方。
        """
        ratio = max(0.0, min(1.0, ratio))
        if ratio > 0.5:
            fill = self.hp_green
        elif ratio < 0.25:
            fill = self.hp_coral
        else:
            fill = self.hp_amber
        x, y, w, h = int(cx - w / 2), int(y), int(w), int(h)
        self.draw_bar(s, x, y, w, h, ratio, fill)
        # 琥珀描边：与精英 bot 的 1px 珊瑚红描边区分，一眼看出是 Boss 条
        pygame.draw.rect(s, self.hp_amber, (x, y, w, h), 2,
                         border_radius=6)
        if name:
            surf = self.font_body.render(name, True, (255, 255, 255, 235))
            s.blit(surf, (x - 8 - surf.get_width(), y - 1))
        if stage:
            self._boss_bar_text(s, cx, y - 22, stage, self.font_body,
                                (255, 255, 255, 190))

    @staticmethod
    def _boss_bar_text(s, cx, y, text, font, color):
        """把文字画在 (cx, y) 为**中心**的位置（血条名用）。"""
        surf = font.render(text, True, color)
        s.blit(surf, (int(cx - surf.get_width() / 2), int(y)))

    def draw_scope(self, s, cx, cy, color=None):
        """狙击开镜准星（需求 E12）：圆环 + 十字线 + 中心点。"""
        color = color if color is not None else (244, 248, 252, 220)
        cx, cy = int(cx), int(cy)
        r = max(40, min(s.get_size()) // 6)
        pygame.draw.circle(s, color, (cx, cy), r, 2)
        pygame.draw.line(s, color, (cx - r, cy), (cx + r, cy), 1)
        pygame.draw.line(s, color, (cx, cy - r), (cx, cy + r), 1)
        pygame.draw.circle(s, color, (cx, cy), 2)

    # ------------------------------------------------------------------
    def render(self, lines, cards=None):
        """lines: [(text, highlight)] -> 覆盖层 Surface（圆角卡片排版）。

        highlight 三态：True→琥珀标题行；False→正文白；"dim"→灰蓝次要行。
        行数约定：前 3 行为常驻主卡片（视角/手电/操作提示），
        第 4 行起为可选的追加帮助卡片（垂直间距 8px）。
        完整按键说明不在这里——由 game 层以居中卡片下发（app.get_hud_cards），
        见 _help_card_lines，这样各状态（HUD 元素不同）都能显示同一份按键表。

        cards: 可选，game 层每帧组装的绘制指令列表（按 "kind" 分发，
            列表顺序即绘制顺序，game 层把 vignette 放最前）：
            {"kind": "card", "x", "y", "lines", "token", "center", "alpha"}
            {"kind": "vignette", "alpha"}
            {"kind": "hp", "x", "y", "label", "ratio", "flash", "pulse"}
            {"kind": "crosshair", "cx", "cy", "gap", "color"}
            {"kind": "hitmark", "cx", "cy", "mark": body/head/kill}
            {"kind": "floats", "cx", "y", "items": [(text,color,alpha,rise)]}
            {"kind": "mobbar", "cx", "y", "w", "h", "ratio", "elite"}
            {"kind": "scope", "cx", "cy", "color"}
            缺省 kind = "card"（Sprint 1 指令无需改动）。
        """
        s = self.surface
        s.fill((0, 0, 0, 0))
        main, help_lines = lines[:3], lines[3:]
        if main:
            y = 14
            y += self._draw_card(s, 16, y, main, token_split=False)
            if help_lines:
                y += 8      # 卡片间距
                self._draw_card(s, 16, y, help_lines, token_split=True)
        if cards:
            for c in cards:
                kind = c.get("kind", "card")
                if kind == "card":
                    self.draw_card_at(s, c["x"], c["y"], c["lines"],
                                      token_split=c.get("token", False),
                                      center=c.get("center", False),
                                      alpha=c.get("alpha", 255))
                elif kind == "vignette":
                    self.draw_vignette(s, c["alpha"])
                elif kind == "hp":
                    self.draw_hp_card(s, c["x"], c["y"], c["label"],
                                      c["ratio"],
                                      flash=c.get("flash", False),
                                      pulse=c.get("pulse", 1.0))
                elif kind == "crosshair":
                    self.draw_crosshair(s, c["cx"], c["cy"], c["gap"],
                                        c["color"])
                elif kind == "hitmark":
                    self.draw_hit_marks(s, c["cx"], c["cy"], c["mark"])
                elif kind == "floats":
                    self.draw_float_texts(s, c["cx"], c["y"], c["items"])
                elif kind == "mobbar":
                    self.draw_mob_bar(s, c["cx"], c["y"], c["w"], c["h"],
                                      c["ratio"],
                                      elite=c.get("elite", False))
                elif kind == "bossbar":
                    self.draw_boss_bar(s, c["cx"], c["y"], c["w"], c["h"],
                                       c["ratio"], c.get("name", ""),
                                       c.get("stage", ""))
                elif kind == "scope":
                    self.draw_scope(s, c["cx"], c["cy"],
                                    c.get("color"))
        return s

    # ------------------------------------------------------------------
    @staticmethod
    def compose_lines(app):
        """从 App 状态生成左上角常驻 HUD 文本行（自动中英回退）。

        固定 3 行：L1 标题 / L2 手电参数 / L3 操作提示。
        L3 恒为「[H] 按键说明」提示——完整按键表由 app._help_card_lines()
        以居中卡片下发（H 切换），故本方法不再随 show_help 改变行数。

        标记约定：True→标题（琥珀）、False→正文（白）、"dim"→次要（灰蓝）。
        """
        cjk = app.hud.using_cjk
        rig = app.camera_rig
        fl = app.flashlight

        def t(zh, en):
            return zh if cjk else en

        mode = t("第一人称", "FPS") if rig.mode == "fps" else t("跟随", "Follow")
        torch_state = t("开", "ON") if fl.enabled else t("关", "OFF")
        lines = [
            ("%s | FPS %d | %s: %s | %s: %d" % (
                t("迷你世界", "MiniWorld"), app.fps_display,
                t("视角", "Cam"), mode, t("实体", "ents"), app.scene.entity_count),
             True),
            ("%s: %s | %s %.1f° | %s %.2f | %s %.3f" % (
                t("手电筒", "Torch"), torch_state,
                t("锥角", "Cone"), app.flashlight.outer_angle * 57.2958,
                t("强度", "Power"), fl.intensity,
                t("衰减", "Falloff"), fl.attenuation),
             False),
            ("[H] %s  [Tab] %s  [F] %s" % (
                t("按键说明", "Controls"), t("切换视角", "cam"),
                t("手电", "torch")), "dim"),
        ]
        return lines
