"""
通用OCR场景 — 仅做文字提取，不做结构化解析
"""
from typing import Dict, Any
from .base import Scene, register_scene


class GenericScene(Scene):

    @property
    def name(self) -> str:
        return "generic"

    @property
    def display_name(self) -> str:
        return "通用OCR"

    @property
    def description(self) -> str:
        return "仅提取图片中的文字，不做结构化处理"

    def parse(self, ocr_result: dict) -> dict:
        return {
            "filename": ocr_result.get("filename", ""),
            "text": ocr_result.get("text", ""),
            "avg_confidence": ocr_result.get("avg_confidence", 0),
            "time_ms": ocr_result.get("time_ms", 0),
            "engine": ocr_result.get("engine", ""),
        }

    def summary(self, parsed_results: list) -> dict:
        base = super().summary(parsed_results)
        confs = [r["avg_confidence"] for r in parsed_results if r.get("avg_confidence")]
        base["avg_confidence"] = round(sum(confs) / len(confs), 4) if confs else 0
        base["total_chars"] = sum(len(r.get("text", "")) for r in parsed_results)
        return base


register_scene(GenericScene())
