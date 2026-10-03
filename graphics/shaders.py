# -*- coding: utf-8 -*-
"""全部 GLSL 着色器源码（#version 330 core）。

管线组成：
1) SHADOW_*    聚光灯深度 pass：把场景渲染到 depth texture（阴影贴图）
2) MAIN_*      主 pass：Blinn-Phong 光照 + 聚光灯光锥 + 聚光灯阴影(PCF 3x3)
               + 距离雾 + gamma 校正
3) HUD_*       正交覆盖层：pygame 渲染的中文 HUD 以纹理贴屏

光照模型（Blinn-Phong）：
    color = albedo*ambient
          + Σ 平行光 [漫反射 + 镜面]
          + Σ 聚光灯 [漫反射 + 镜面] * 距离衰减 * 光锥平滑系数 * 阴影系数
"""

MAX_SPOTS = 3   # 同时支持的聚光灯数量上限（手电筒 + 灯塔 + 备用）

# ----------------------------------------------------------------------
# 阴影 pass（仅深度）
# ----------------------------------------------------------------------
SHADOW_VS = """#version 330 core
uniform mat4 u_light_vp;   // 光源 view-projection
uniform mat4 u_model;
in vec3 in_pos;
void main() {
    gl_Position = u_light_vp * u_model * vec4(in_pos, 1.0);
}
"""

SHADOW_FS = """#version 330 core
void main() {
    // 深度由帧缓冲自动写入，无需输出颜色
}
"""

# ----------------------------------------------------------------------
# 主 pass 顶点着色器
# ----------------------------------------------------------------------
MAIN_VS = """#version 330 core
#define MAX_SPOTS %d
uniform mat4 u_proj;
uniform mat4 u_view;
uniform mat4 u_model;
uniform mat4 u_spot_vp[MAX_SPOTS];     // 每盏聚光灯的阴影投影矩阵

in vec3 in_pos;
in vec3 in_normal;
in vec2 in_uv;

out vec3 v_world;
out vec3 v_normal;
out vec2 v_uv;
out vec4 v_spot_shadow[MAX_SPOTS];

void main() {
    vec4 world = u_model * vec4(in_pos, 1.0);
    v_world = world.xyz;
    // 均匀缩放假设下 mat3(u_model) 旋转部分即可（FS 中再归一化）
    v_normal = mat3(u_model) * in_normal;
    v_uv = in_uv;
    for (int i = 0; i < MAX_SPOTS; i++) {
        v_spot_shadow[i] = u_spot_vp[i] * world;
    }
    gl_Position = u_proj * u_view * world;
}
""" % MAX_SPOTS

# ----------------------------------------------------------------------
# 主 pass 片段着色器
# ----------------------------------------------------------------------
MAIN_FS = """#version 330 core
#define MAX_SPOTS %d

in vec3 v_world;
in vec3 v_normal;
in vec2 v_uv;
in vec4 v_spot_shadow[MAX_SPOTS];

out vec4 frag_color;

// ---- 相机与材质 ----
uniform vec3  u_view_pos;
uniform vec3  u_albedo;
uniform float u_shininess;
uniform float u_spec_strength;
uniform int   u_has_texture;
uniform sampler2D u_texture;
uniform int   u_unlit;        // 1 = 自发光（不受光照）

// ---- 平行光（太阳）----
uniform vec3  u_sun_dir;      // 指向太阳
uniform vec3  u_sun_color;
uniform vec3  u_ambient;

// ---- 聚光灯数组（含光锥参数与阴影）----
uniform int   u_spot_count;
uniform vec3  u_spot_pos[MAX_SPOTS];
uniform vec3  u_spot_dir[MAX_SPOTS];      // 已归一化
uniform vec3  u_spot_color[MAX_SPOTS];
uniform vec4  u_spot_params[MAX_SPOTS];   // x=innerCos y=outerCos z=attenK w=intensity
uniform int   u_spot_has_shadow[MAX_SPOTS];
uniform sampler2D u_spot_shadow_map[MAX_SPOTS];
uniform float u_shadow_bias;

// ---- 大气 ----
uniform vec3  u_fog_color;
uniform float u_fog_density;
uniform float u_gamma;
uniform float u_alpha;          // 主 pass 恒为 1.0；弹痕 pass 逐实体调低以淡出

/* 单光源 Blinn-Phong：返回漫反射+镜面（未乘光源颜色） */
vec3 blinn_phong(vec3 N, vec3 V, vec3 L, vec3 albedo) {
    float ndl = max(dot(N, L), 0.0);
    vec3 diffuse = vec3(ndl);
    vec3 H = normalize(L + V);                          // 半程向量
    float spec = pow(max(dot(N, H), 0.0), u_shininess)
                 * u_spec_strength * step(0.001, ndl);  // 背光面无高光
    return (diffuse + spec) * albedo;
}

void main() {
    vec3 albedo = u_albedo;
    if (u_has_texture == 1) albedo *= texture(u_texture, v_uv).rgb;
    vec3 N = normalize(v_normal);
    vec3 V = normalize(u_view_pos - v_world);

    if (u_unlit == 1) {                                  // 自发光直接输出
        frag_color = vec4(albedo, u_alpha);
        return;
    }

    vec3 result = albedo * u_ambient;

    // 太阳（平行光）
    result += blinn_phong(N, V, normalize(u_sun_dir), albedo) * u_sun_color;

    // 聚光灯：距离平方衰减 × 光锥 smoothstep 软边 × 阴影
    // 阴影采样必须内联在循环体内：GLSL 3.30 规定采样器数组
    // 只能用「常量索引表达式」（循环索引合法，函数形参不合法）索引。
    for (int i = 0; i < MAX_SPOTS; i++) {
        if (i >= u_spot_count) break;
        vec3  to_light = u_spot_pos[i] - v_world;
        float dist = length(to_light);
        vec3  L = to_light / dist;
        float theta = dot(-L, u_spot_dir[i]);            // 与光轴夹角余弦
        float cone = smoothstep(u_spot_params[i].y,   // outerCos
                                u_spot_params[i].x,   // innerCos
                                theta);               // 软边光锥
        if (cone <= 0.001) continue;
        float atten = u_spot_params[i].w
                    / (1.0 + u_spot_params[i].z * dist * dist);

        // ---- 聚光灯阴影：透视投影到光空间 + 3x3 PCF + N·L 相关 bias ----
        float sh = 1.0;
        if (u_spot_has_shadow[i] == 1) {
            vec3 sc = v_spot_shadow[i].xyz / v_spot_shadow[i].w;
            sc = sc * 0.5 + 0.5;                       // NDC -> [0,1]
            if (sc.z < 1.0 && sc.x > 0.0 && sc.x < 1.0 &&
                sc.y > 0.0 && sc.y < 1.0) {            // 仅阴影覆盖范围内采样
                float bias = max(u_shadow_bias
                                 * (1.0 - max(dot(N, L), 0.0)), 0.0004);
                vec2 texel = 1.0 / vec2(textureSize(u_spot_shadow_map[i], 0));
                float lit = 0.0;
                for (int dx = -1; dx <= 1; dx++) {
                    for (int dy = -1; dy <= 1; dy++) {
                        float d = texture(u_spot_shadow_map[i],
                                          sc.xy + vec2(float(dx), float(dy))
                                          * texel).r;
                        lit += (sc.z - bias > d) ? 0.0 : 1.0;
                    }
                }
                sh = lit / 9.0;
            }
        }

        result += blinn_phong(N, V, L, albedo)
                * u_spot_color[i] * atten * cone * sh;
    }

    // 距离雾（指数），与清屏色一致以融合天际
    float fog_f = 1.0 - exp(-u_fog_density * length(u_view_pos - v_world));
    result = mix(result, u_fog_color, clamp(fog_f, 0.0, 1.0));

    // gamma 校正（近似 sRGB 输出）
    result = pow(clamp(result, 0.0, 1.0), vec3(1.0 / u_gamma));
    frag_color = vec4(result, u_alpha);
}
""" % MAX_SPOTS

# ----------------------------------------------------------------------
# HUD 覆盖层（正交全屏 quad + 纹理）
# ----------------------------------------------------------------------
HUD_VS = """#version 330 core
in vec2 in_clip;    // 直接给裁剪空间坐标 [-1,1]
in vec2 in_uv;
out vec2 v_uv;
void main() {
    v_uv = in_uv;
    gl_Position = vec4(in_clip, 0.0, 1.0);
}
"""

HUD_FS = """#version 330 core
in vec2 v_uv;
out vec4 frag_color;
uniform sampler2D u_hud;
void main() {
    frag_color = texture(u_hud, v_uv);
}
"""
