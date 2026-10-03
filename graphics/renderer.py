# -*- coding: utf-8 -*-
"""渲染器：阴影 pass + 主光照 pass + HUD 覆盖 pass。

帧流程（render()）：
1. 对每盏 cast_shadow 且 enabled 的聚光灯：
   - 用其 view-proj 矩阵把场景（cast_shadow 物体）渲染进 depth FBO
   - 正面剔除(front-face culling)减少阴影痤疮
2. 主 pass：清屏(雾色) → 逐实体上传材质/模型矩阵 → Blinn-Phong 着色
3. HUD pass：禁用深度，把 pygame 生成的中文 HUD 纹理以混合模式铺屏

GPU 资源策略：
- MeshData -> VAO 首次使用时上传并缓存（id(mesh) 为键）
- 纹理按名字缓存（'checker'、'white'）
"""
import struct

import pygame
import moderngl

from engine.mesh import MeshRenderer
from . import shaders
from .texture import checker_bytes, solid_bytes, decal_bytes

_surface_to_bytes = getattr(pygame.image, "tobytes", None) or pygame.image.tostring


class Renderer:
    def __init__(self, ctx, width, height):
        self.ctx = ctx
        self.width = width
        self.height = height

        # ---- 程序 ----
        self.prog_main = ctx.program(vertex_shader=shaders.MAIN_VS,
                                     fragment_shader=shaders.MAIN_FS)
        self.prog_shadow = ctx.program(vertex_shader=shaders.SHADOW_VS,
                                       fragment_shader=shaders.SHADOW_FS)
        self.prog_hud = ctx.program(vertex_shader=shaders.HUD_VS,
                                    fragment_shader=shaders.HUD_FS)

        # ---- 全局 GL 状态 ----
        ctx.enable_only(moderngl.DEPTH_TEST | moderngl.CULL_FACE)
        ctx.cull_face = "back"

        # ---- 纹理缓存 ----
        self._textures = {}
        self._make_builtin_textures()

        # ---- HUD 全屏 quad（TRIANGLE_STRIP 4 顶点）----
        # uv 与 pygame surface 同向：v=0 是画面顶部
        #
        # 顶点顺序必须走"Z"字：左下 → 右下 → 左上 → 右上。
        # TRIANGLE_STRIP 的三角形是 {v0,v1,v2} / {v1,v2,v3}：
        #   左下 右下 右上 左上 → {BL,BR,TR} + {BR,TR,TL}，两个三角形
        #     都落在右半边，屏幕左侧留下一个以画面中心为顶点的楔形空洞，
        #     HUD 只覆盖 75% 屏幕、左缘被斜边切断（观感即"蝴蝶结 /
        #     两个梯形拼接 / 接缝"）。
        #   左下 右下 左上 右上 → {BL,BR,TL} + {BR,TL,TR}，铺满全屏。
        hud_verts = struct.pack(
            "16f",
            -1.0, -1.0, 0.0, 1.0,   # 左下
            1.0, -1.0, 1.0, 1.0,    # 右下
            -1.0, 1.0, 0.0, 0.0,    # 左上
            1.0, 1.0, 1.0, 0.0,     # 右上
        )
        hud_vbo = ctx.buffer(hud_verts)
        self.vao_hud = ctx.vertex_array(
            self.prog_hud, [(hud_vbo, "2f 2f", "in_clip", "in_uv")],
            mode=moderngl.TRIANGLE_STRIP)
        self.hud_tex = ctx.texture((width, height), 4)
        self.hud_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

        # ---- GPU 网格缓存 ----
        self._vao_cache = {}      # id(MeshData) -> (vao_main, vao_shadow)

        # 数组 uniform 的键名因驱动而异（NVIDIA 报 'x[0]'，Intel/Mesa 报 'x'），
        # 探测一次缓存，render() 里统一经此访问。
        self._ukey = ((lambda n: n) if "u_spot_pos" in self.prog_main._members
                      else (lambda n: n + "[0]"))

        # ---- 阴影 uniform 打包缓冲 ----
        self._identity_spot_vp = [1.0] * 16 * shaders.MAX_SPOTS

    # ------------------------------------------------------------------
    # 窗口尺寸
    # ------------------------------------------------------------------
    def resize(self, width, height):
        """窗口尺寸变化后重建与尺寸绑定的 GL 资源。

        只有 HUD 全屏纹理依赖窗口尺寸；阴影 FBO 固定 shadow_res²、
        网格 VAO 与尺寸无关，均无需重建。viewport 在 render() 里每帧
        从 self.width/height 取，改这两个属性即可。
        """
        if (width, height) == (self.width, self.height):
            return
        self.width, self.height = width, height
        self.hud_tex.release()
        self.hud_tex = self.ctx.texture((width, height), 4)
        self.hud_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

    # ------------------------------------------------------------------
    # 资源
    # ------------------------------------------------------------------
    def _make_builtin_textures(self):
        tex = self.ctx.texture((256, 256), 3, checker_bytes())
        tex.filter = (moderngl.NEAREST_MIPMAP_LINEAR, moderngl.LINEAR)
        tex.repeat_x = tex.repeat_y = True
        tex.build_mipmaps()
        self._textures["checker"] = tex

        white = self.ctx.texture((1, 1), 3, solid_bytes())
        white.filter = (moderngl.NEAREST, moderngl.NEAREST)
        self._textures["white"] = white

        # 弹痕：RGBA + 线性过滤（需要 alpha 羽化，NEAREST 会露锯齿）
        decal = self.ctx.texture((64, 64), 4, decal_bytes())
        decal.filter = (moderngl.LINEAR, moderngl.LINEAR)
        decal.repeat_x = decal.repeat_y = False
        self._textures["decal"] = decal

    def get_mesh_vaos(self, mesh):
        """MeshData -> (主 pass VAO, 阴影 pass VAO)，首次上传并缓存。"""
        key = id(mesh)
        hit = self._vao_cache.get(key)
        if hit is not None:
            return hit
        vbo = self.ctx.buffer(mesh.packed_vertices())
        ibo = self.ctx.buffer(mesh.packed_indices())
        vao_main = self.ctx.vertex_array(
            self.prog_main,
            [(vbo, "3f 3f 2f", "in_pos", "in_normal", "in_uv")],
            ibo)
        # 阴影 pass 只需要位置：用 'x' 填充跳过法线(12B)与uv(8B)
        vao_shadow = self.ctx.vertex_array(
            self.prog_shadow,
            [(vbo, "3f 20x", "in_pos")],
            ibo)
        self._vao_cache[key] = (vao_main, vao_shadow)
        return vao_main, vao_shadow

    def ensure_shadow_resource(self, light):
        """为聚光灯创建/复用 depth texture + FBO。"""
        if light._fbo is not None:
            return
        depth = self.ctx.depth_texture((light.shadow_res, light.shadow_res))
        depth.filter = (moderngl.NEAREST, moderngl.NEAREST)
        depth.repeat_x = depth.repeat_y = False
        fbo = self.ctx.framebuffer(depth_attachment=depth)
        light._depth_tex = depth
        light._fbo = fbo

    def get_texture(self, name):
        return self._textures.get(name) or self._textures["white"]

    # ------------------------------------------------------------------
    # 帧
    # ------------------------------------------------------------------
    def render(self, scene, camera_rig, sun, spot_lights, hud_surface,
               clear_color, fog):
        ctx = self.ctx
        active = [l for l in spot_lights if l.enabled]
        shadow_spots = [l for l in active if l.cast_shadow][:shaders.MAX_SPOTS]

        # ---------- 1) 阴影 pass ----------
        ctx.enable_only(moderngl.DEPTH_TEST | moderngl.CULL_FACE)
        ctx.cull_face = "front"
        spot_vp_flat = []
        for light in shadow_spots:
            self.ensure_shadow_resource(light)
            vp = light.shadow_view_proj()
            light._vp_flat = vp
            spot_vp_flat.extend(vp)
        # 不足 MAX_SPOTS 用单位阵占位
        while len(spot_vp_flat) < 16 * shaders.MAX_SPOTS:
            spot_vp_flat.append(1.0 if len(spot_vp_flat) % 5 == 0 else 0.0)

        shadow_casters = [e for e in scene.entities
                          if e.alive and e.renderer is not None
                          and e.renderer.visible and e.renderer.cast_shadow]
        for light in shadow_spots:
            light._fbo.use()
            ctx.clear(depth=1.0)
            for e in shadow_casters:
                self.prog_shadow["u_light_vp"].write(
                    struct.pack("16f", *light._vp_flat))
                self.prog_shadow["u_model"].write(
                    struct.pack("16f", *e.transform.model_matrix()))
                vao_main, vao_shadow = self.get_mesh_vaos(e.renderer.mesh)
                vao_shadow.render()
        ctx.cull_face = "back"

        # ---------- 2) 主 pass ----------
        ctx.enable_only(moderngl.DEPTH_TEST | moderngl.CULL_FACE)
        ctx.viewport = (0, 0, self.width, self.height)
        self.ctx.screen.use()
        clear = tuple(min(1.0, c) for c in clear_color)
        ctx.clear(*clear, depth=1.0)

        view = camera_rig.view_matrix()
        proj = camera_rig.projection_matrix()
        eye, _ = camera_rig.eye_and_forward()

        p = self.prog_main
        p["u_proj"].write(struct.pack("16f", *proj))
        p["u_view"].write(struct.pack("16f", *view))
        p["u_view_pos"].value = eye.to_tuple()
        p["u_sun_dir"].value = sun.direction.to_tuple()
        p["u_sun_color"].value = sun.color
        p["u_ambient"].value = fog.ambient
        p["u_fog_color"].value = clear
        p["u_fog_density"].value = fog.density
        p["u_gamma"].value = fog.gamma
        p["u_shadow_bias"].value = fog.shadow_bias

        # 聚光灯数组 uniforms
        count = len(active)
        p["u_spot_count"].value = count
        pos_arr, dir_arr, col_arr, par_arr = [], [], [], []
        has_shadow = [0] * shaders.MAX_SPOTS
        vp_all = []
        for i, l in enumerate(active[:shaders.MAX_SPOTS]):
            pos_arr.extend(l.position.to_tuple())
            dir_arr.extend(l.direction.to_tuple())
            col_arr.extend(l.color)
            par_arr.extend(l.params_vec4())
            vp_all.extend(l.shadow_view_proj() if l not in shadow_spots
                          else l._vp_flat)
            if l in shadow_spots:
                has_shadow[i] = 1
        pad = shaders.MAX_SPOTS - min(count, shaders.MAX_SPOTS)
        pos_arr.extend([0.0] * (3 * pad))
        dir_arr.extend([0.0, -1.0, 0.0] * pad)
        col_arr.extend([0.0] * (3 * pad))
        par_arr.extend([1.0, 0.9, 0.03, 0.0] * pad)
        vp_all.extend([1.0 if (k % 5) == 0 else 0.0
                       for k in range(16 * pad)])
        k = self._ukey
        p[k("u_spot_pos")].write(struct.pack("%df" % (3 * shaders.MAX_SPOTS), *pos_arr))
        p[k("u_spot_dir")].write(struct.pack("%df" % (3 * shaders.MAX_SPOTS), *dir_arr))
        p[k("u_spot_color")].write(struct.pack("%df" % (3 * shaders.MAX_SPOTS), *col_arr))
        p[k("u_spot_params")].write(struct.pack("%df" % (4 * shaders.MAX_SPOTS), *par_arr))
        p[k("u_spot_has_shadow")].write(struct.pack("%di" % shaders.MAX_SPOTS, *has_shadow))
        p[k("u_spot_vp")].write(struct.pack("%df" % (16 * shaders.MAX_SPOTS), *vp_all))

        # 绑定阴影贴图到纹理单元 0..N-1，主纹理单元 N（数组一次性整体写入）
        shadow_unit_base = 0
        main_tex_unit = shaders.MAX_SPOTS
        units = [0] * shaders.MAX_SPOTS
        for i, l in enumerate(shadow_spots):
            units[i] = shadow_unit_base + i
            l._depth_tex.use(shadow_unit_base + i)
        p[k("u_spot_shadow_map")].value = tuple(units)
        p["u_texture"].value = main_tex_unit

        # 逐实体绘制（弹痕/decal 留给 2.5 pass 混合绘制）
        p["u_alpha"].value = 1.0
        for e in scene.entities:
            r = e.renderer
            if not e.alive or r is None or not r.visible or r.decal:
                continue
            p["u_model"].write(struct.pack("16f", *e.transform.model_matrix()))
            p["u_albedo"].value = r.color
            p["u_shininess"].value = r.shininess
            p["u_spec_strength"].value = r.spec_strength
            p["u_unlit"].value = 1 if r.unlit else 0
            if r.texture:
                p["u_has_texture"].value = 1
                self.get_texture(r.texture).use(main_tex_unit)
            else:
                p["u_has_texture"].value = 0
                self._textures["white"].use(main_tex_unit)
            vao_main, _ = self.get_mesh_vaos(r.mesh)
            vao_main.render()

        # ---------- 2.5) 弹痕 pass（半透明，深度测试开、深度写关）----------
        # 单独一遍的原因：主 pass 不开 BLEND（alpha 恒 1），弹痕需要真实
        # alpha 才能淡出。深度测试保证弹痕被墙体正确遮挡、不需依赖偏移。
        ctx.enable_only(moderngl.DEPTH_TEST | moderngl.CULL_FACE
                        | moderngl.BLEND)
        ctx.blend_func = moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA
        ctx.depth_mask = False
        for e in scene.entities:
            r = e.renderer
            if not e.alive or r is None or not r.visible or not r.decal:
                continue
            p["u_model"].write(struct.pack("16f", *e.transform.model_matrix()))
            p["u_albedo"].value = r.color
            p["u_unlit"].value = 1
            p["u_has_texture"].value = 1
            self._textures["decal"].use(main_tex_unit)
            p["u_alpha"].value = r.alpha
            vao_main, _ = self.get_mesh_vaos(r.mesh)
            vao_main.render()
        ctx.depth_mask = True
        p["u_alpha"].value = 1.0

        # ---------- 3) HUD pass ----------
        if hud_surface is not None:
            ctx.enable_only(moderngl.BLEND)
            ctx.blend_func = moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA
            data = _surface_to_bytes(hud_surface, "RGBA")
            self.hud_tex.write(data)
            self.hud_tex.use(0)
            self.prog_hud["u_hud"].value = 0
            self.vao_hud.render()

    def release_light_resource(self, light):
        """重置世界时释放灯光 GL 资源。"""
        if light._fbo is not None:
            light._fbo.release()
            light._depth_tex.release()
            light._fbo = None
            light._depth_tex = None


class FogParams:
    """大气参数容器（雾色=清屏色、环境光、gamma、阴影 bias）。"""

    def __init__(self):
        self.ambient = (0.48, 0.51, 0.56)
        self.density = 0.008
        self.gamma = 2.2
        self.shadow_bias = 0.0035
