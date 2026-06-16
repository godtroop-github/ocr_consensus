"""
场景基类 — 所有OCR场景的抽象
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional


class Scene(ABC):
    """场景插件基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """场景唯一标识，如 'generic', 'business_check'"""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """场景显示名"""
        ...

    @property
    def description(self) -> str:
        return ""

    @abstractmethod
    def parse(self, ocr_result: dict) -> dict:
        """
        对单条OCR结果做结构化提取
        :param ocr_result: OcrResult.to_dict()的输出
        :return: 结构化字段字典
        """
        ...

    def parse_batch(self, ocr_results: List[dict]) -> List[dict]:
        """批量解析（可重写做跨文件聚合）"""
        return [self.parse(r) for r in ocr_results]

    def summary(self, parsed_results: List[dict]) -> Dict[str, Any]:
        """生成统计摘要"""
        total = len(parsed_results)
        successful = sum(1 for r in parsed_results if not r.get("error"))
        return {
            "scene": self.name,
            "total": total,
            "successful": successful,
            "failed": total - successful,
            "success_rate": round(successful / total, 4) if total > 0 else 0,
        }

    def get_config_schema(self) -> Dict[str, Any]:
        """返回场景配置项的schema（供前端渲染）"""
        return {}


# --- 全局场景注册表 ---
_scenes: Dict[str, Scene] = {}


def register_scene(scene: Scene):
    _scenes[scene.name] = scene


def get_scene(name: str) -> Optional[Scene]:
    return _scenes.get(name)


def list_scenes() -> List[Dict[str, str]]:
    return [
        {"name": s.name, "display_name": s.display_name, "description": s.description}
        for s in _scenes.values()
    ]
