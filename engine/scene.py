# -*- coding: utf-8 -*-
"""场景与实体管理。

Entity 是一切场景对象的载体：
- transform  : 变换（必有）
- renderer   : 网格渲染组件（可选，graphics.renderer 会收集它）
- body       : 物理碰撞体（可选，engine.physics.PhysicsWorld 会收集它）
- update(dt) : 每帧逻辑回调（子类覆写，如玩家/炮台/炮弹）

Scene 负责：
- 实体的增删（延迟删除，避免迭代中修改集合）
- 每帧调用所有实体的 update
- 按需查询（find 名称 / query 标签）
"""
from .transform import Transform


class Entity:
    def __init__(self, name="entity", tag=""):
        self.name = name
        self.tag = tag
        self.transform = Transform()
        self.renderer = None      # MeshRenderer（engine.mesh 中定义）
        self.body = None          # PhysicsBody（engine.physics 中定义）
        self.alive = True         # 置 False 后由 Scene.flush_removed 统一移除

    def update(self, dt):
        """每帧逻辑（子类覆写）。dt 为渲染帧间隔（秒）。"""
        pass

    def on_removed(self):
        """被移除时的回调（子类可覆写做清理）。"""
        pass


class Scene:
    def __init__(self):
        self.entities = []

    # ---------- 生命周期 ----------
    def add(self, entity):
        self.entities.append(entity)
        return entity

    def remove(self, entity):
        """标记删除（下一帧统一清理，安全）。"""
        entity.alive = False

    def clear(self):
        for e in self.entities:
            e.alive = False
        self.flush_removed(None)
        self.entities = []

    def flush_removed(self, physics_world=None):
        """统一移除 alive=False 的实体，并同步清理物理世界中的碰撞体。"""
        dead = [e for e in self.entities if not e.alive]
        if not dead:
            return
        for e in dead:
            e.on_removed()
            if physics_world is not None and e.body is not None:
                physics_world.unregister(e.body)
        self.entities = [e for e in self.entities if e.alive]

    # ---------- 每帧驱动 ----------
    def update(self, dt):
        for e in self.entities:
            if e.alive:
                e.update(dt)

    # ---------- 查询 ----------
    def find(self, name):
        for e in self.entities:
            if e.name == name and e.alive:
                return e
        return None

    def query(self, tag):
        return [e for e in self.entities if e.tag == tag and e.alive]
