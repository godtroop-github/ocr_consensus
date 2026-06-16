"""场景插件"""
from .base import Scene, register_scene, get_scene, list_scenes
# 导入内置场景以触发 register_scene
from . import business_check
from . import generic


