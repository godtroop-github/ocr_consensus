"""
通用OCR识别系统 — FastAPI主应用
"""
import io
import os
import json
import zipfile
import asyncio
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import HOST, PORT, RESULTS_DIR, IMAGE_EXTENSIONS, ARCHIVE_EXTENSIONS, MAX_UPLOAD_SIZE
from .core.ocr_engine import get_engine, OcrResult
from .core.task_manager import TaskManager, TaskStatus
from .scenes import list_scenes, get_scene

# 注册场景
from .scenes import business_check as _bc
from .scenes import generic as _gc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("ocr_app")

app = FastAPI(title="通用OCR识别系统", version="1.0.0")

# 静态文件和模板
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)

# 定义并创建 UPLOAD_DIR，使其作为静态目录的子目录
UPLOAD_DIR = STATIC_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# 全局实例
engine = get_engine("rapidocr")
task_mgr = TaskManager()


def _is_image(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTENSIONS


def _is_archive(name: str) -> bool:
    return Path(name).suffix.lower() in ARCHIVE_EXTENSIONS


def _extract_zip(data: bytes, target_dir: Path) -> list:
    """解压ZIP，返回图片文件名列表"""
    images = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if name.startswith("__MACOSX") or name.startswith("."):
                continue
            if _is_image(name):
                fname = Path(name).name
                target_dir.mkdir(parents=True, exist_ok=True)
                with open(target_dir / fname, "wb") as f:
                    f.write(zf.read(name))
                images.append(fname)
    return images


# ========== 页面路由 ==========

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "scenes": list_scenes(),
    })


# ========== API路由 ==========

@app.get("/api/health")
async def health():
    return {"status": "ok", "engine": engine.name(), "version": "1.0.0"}


@app.get("/api/scenes")
async def api_scenes():
    return {"scenes": list_scenes()}


@app.post("/api/ocr/upload")
async def ocr_upload(
    file: UploadFile = File(...),
    scene: str = Form("business_check"),
    enhance: bool = Form(False),
):
    """单文件/ZIP上传OCR"""
    if file.size and file.size > MAX_UPLOAD_SIZE:
        raise HTTPException(400, f"文件超过200MB限制")

    data = await file.read()
    scene_obj = get_scene(scene) or get_scene("business_check")
    results = []

    if _is_image(file.filename):
        task_dir = UPLOAD_DIR / "single"
        task_dir.mkdir(parents=True, exist_ok=True)
        img_path = task_dir / file.filename
        img_path.write_bytes(data)

        result = engine.ocr_image(str(img_path), file.filename, enhance)
        parsed = scene_obj.parse(result.to_dict())
        parsed["img_path"] = f"/static/uploads/single/{file.filename}"
        results.append(parsed)

    elif _is_archive(file.filename) and file.filename.lower().endswith(".zip"):
        zip_name = file.filename.rsplit(".", 1)[0]
        task_dir = UPLOAD_DIR / zip_name
        images = _extract_zip(data, task_dir)
        for img_name in sorted(images):
            img_path = task_dir / img_name
            result = engine.ocr_image(str(img_path), img_name, enhance)
            parsed = scene_obj.parse(result.to_dict())
            parsed["img_path"] = f"/static/uploads/{zip_name}/{img_name}"
            results.append(parsed)
    else:
        raise HTTPException(400, f"不支持的文件格式: {file.filename}")

    summary = scene_obj.summary(results)
    return {"results": results, "summary": summary}


@app.post("/api/tasks/create")
async def create_task(
    file: UploadFile = File(...),
    name: str = Form(""),
    scene: str = Form("business_check"),
    enhance: bool = Form(False),
):
    """创建跑批任务"""
    if file.size and file.size > MAX_UPLOAD_SIZE:
        raise HTTPException(400, f"文件超过200MB限制")

    data = await file.read()
    task_name = name or file.filename

    # 先解压获取文件列表
    images = []
    if _is_image(file.filename):
        task_dir = UPLOAD_DIR / file.filename.rsplit(".", 1)[0]
        task_dir.mkdir(parents=True, exist_ok=True)
        fpath = task_dir / file.filename
        fpath.write_bytes(data)
        images = [file.filename]
    elif _is_archive(file.filename) and file.filename.lower().endswith(".zip"):
        task_dir = UPLOAD_DIR / file.filename.rsplit(".", 1)[0]
        images = _extract_zip(data, task_dir)
    else:
        raise HTTPException(400, f"不支持的文件格式: {file.filename}")

    if not images:
        raise HTTPException(400, "未找到可处理的图片文件")

    task = task_mgr.create_task(task_name, scene, images, config={"enhance": enhance})
    return {"task": task.to_dict()}


@app.post("/api/tasks/{task_id}/run")
async def run_task(task_id: str, background_tasks: BackgroundTasks):
    """异步执行任务"""
    task = task_mgr.get_task(task_id)
    if not task:
        raise HTTPException(404, f"任务不存在: {task_id}")

    enhance = task.config.get("enhance", False)
    scene_obj = get_scene(task.scene) or get_scene("business_check")

    async def ocr_func(filename, idx):
        # 找到实际文件路径
        task_dir = UPLOAD_DIR
        for d in UPLOAD_DIR.iterdir():
            if d.is_dir():
                fpath = d / filename
                if fpath.exists():
                    task_dir = d
                    break
        else:
            fpath = task_dir / filename

        if not fpath.exists():
            # 尝试子目录
            for sub in UPLOAD_DIR.rglob(filename):
                fpath = sub
                break

        result = engine.ocr_image(str(fpath), filename, enhance)
        parsed = scene_obj.parse(result.to_dict())
        try:
            rel_path = fpath.relative_to(UPLOAD_DIR)
            parsed["img_path"] = f"/static/uploads/{rel_path}"
        except Exception:
            parsed["img_path"] = ""
        return parsed

    background_tasks.add_task(task_mgr.run_task, task_id, ocr_func)
    return {"status": "started", "task_id": task_id}


@app.get("/api/tasks")
async def list_tasks():
    return {"tasks": task_mgr.list_tasks()}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str, full: bool = False):
    task = task_mgr.get_task(task_id)
    if not task:
        raise HTTPException(404, f"任务不存在: {task_id}")
    if full:
        return {"task": task.to_dict_full()}
    return {"task": task.to_dict()}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    task = task_mgr.cancel_task(task_id)
    if not task:
        raise HTTPException(404, f"任务不存在: {task_id}")
    return {"task": task.to_dict()}


@app.get("/api/tasks/{task_id}/summary")
async def task_summary(task_id: str):
    task = task_mgr.get_task(task_id)
    if not task:
        raise HTTPException(404, f"任务不存在: {task_id}")
    scene_obj = get_scene(task.scene) or get_scene("generic")
    parsed = [item.result for item in task.items if item.result]
    summary = scene_obj.summary(parsed)
    summary["task"] = task.to_dict()
    return summary


@app.get("/api/tasks/{task_id}/export")
async def export_task(task_id: str, format: str = "json"):
    """导出任务结果"""
    task = task_mgr.get_task(task_id)
    if not task:
        raise HTTPException(404, f"任务不存在: {task_id}")

    data = task.to_dict_full()
    if format == "json":
        fname = f"{task_id}_results.json"
        fpath = RESULTS_DIR / fname
        fpath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return FileResponse(str(fpath), media_type="application/json", filename=fname)
    elif format == "xlsx":
        fname = f"{task_id}_results.xlsx"
        fpath = RESULTS_DIR / fname
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = task.scene
        # Build rows from task items
        all_keys = set()
        row_dicts = []
        for item in task.items:
            d = {"filename": item.filename, "status": item.status}
            if item.result:
                d.update(item.result)
            if item.error:
                d["error"] = item.error
            all_keys.update(d.keys())
            row_dicts.append(d)
        headers = sorted(all_keys) if all_keys else ["filename", "status", "error"]
        ws.append(headers)
        for row_dict in row_dicts:
            ws.append([row_dict.get(h, "") for h in headers])
        wb.save(str(fpath))
        return FileResponse(str(fpath), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=fname)
    else:
        raise HTTPException(400, f"不支持的导出格式: {format}")


@app.post("/api/export")
async def export_results(request: Request):
    """导出通用OCR/单次上传结果"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    results = body.get("results", [])
    scene = body.get("scene", "generic")

    if not results:
        raise HTTPException(400, "没有可导出的数据")

    from openpyxl import Workbook
    import time
    wb = Workbook()
    ws = wb.active
    ws.title = scene[:30]  # Excel tab name limit is 31 chars

    # 获取所有键
    all_keys = set()
    for r in results:
        all_keys.update(r.keys())

    # 过滤和排序键
    exclude_keys = {"img_path"}
    headers = sorted(list(all_keys - exclude_keys))

    # 保证 filename 排第一位（如果有的话）
    if "filename" in headers:
        headers.remove("filename")
        headers = ["filename"] + headers

    ws.append(headers)
    for r in results:
        ws.append([str(r.get(h, "")) for h in headers])

    fname = f"ocr_export_{int(time.time())}.xlsx"
    fpath = RESULTS_DIR / fname
    wb.save(str(fpath))
    return FileResponse(
        str(fpath),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=fname
    )


# ========== 上传OCR结果解析 ==========

_uploaded_results: list[dict] = []  # 存储上传的OCR解析结果


@app.post("/api/upload-ocr")
async def upload_ocr_results(file: UploadFile = File(...)):
    """上传已OCR的JSON结果，直接解析为经商办企结构化数据"""
    global _uploaded_results
    if not file.filename.endswith(".json"):
        raise HTTPException(400, "仅支持JSON文件")

    content = await file.read()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(400, "JSON格式错误")

    results = data.get("results", data) if isinstance(data, dict) else data
    if not isinstance(results, list):
        raise HTTPException(400, "JSON中未找到results数组")

    scene_obj = get_scene("business_check") or get_scene("business_check")
    _uploaded_results = []

    for item in results:
        # 适配格式：上传JSON的lines是字符串列表，parse()期望dict列表
        adapted = dict(item)
        raw_lines = adapted.get("lines", [])
        confs = adapted.get("confidences", [])
        if raw_lines and isinstance(raw_lines[0], str):
            adapted["lines"] = [
                {"text": ln, "bbox": None, "confidence": confs[i] if i < len(confs) else 0}
                for i, ln in enumerate(raw_lines)
            ]
        parsed = scene_obj.parse(adapted)
        parsed["filename"] = item.get("filename", "unknown")
        parsed["avg_confidence"] = item.get("avg_confidence", 0)
        _uploaded_results.append(parsed)

    return {
        "status": "ok",
        "total": len(_uploaded_results),
        "results": _uploaded_results,
    }


@app.get("/api/uploaded/results")
async def get_uploaded_results():
    """获取已上传解析的结果"""
    return {"total": len(_uploaded_results), "results": _uploaded_results}


@app.get("/api/uploaded/export")
async def export_uploaded_results(format: str = "xlsx"):
    """导出已上传解析结果为Excel"""
    if not _uploaded_results:
        raise HTTPException(400, "没有可导出的数据")

    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "经商办企解析结果"

    all_keys = set()
    for r in _uploaded_results:
        all_keys.update(r.keys())
    # 优先展示的列顺序
    priority = ["filename", "姓名", "身份证号", "任职情况", "相关企业信息"]
    headers = [k for k in priority if k in all_keys]
    headers += sorted(all_keys - set(headers))

    ws.append(headers)
    for r in _uploaded_results:
        ws.append([str(r.get(h, "")) for h in headers])

    fname = "经商办企_OCR解析结果.xlsx"
    fpath = RESULTS_DIR / fname
    wb.save(str(fpath))
    return FileResponse(
        str(fpath),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=fname,
    )


def main():
    """启动入口"""
    import uvicorn
    print(f"\n🔍 经商办企OCR解析系统 v2.0.0")
    print(f"📁 http://{HOST}:{PORT}")
    print(f"🔧 引擎: {engine.name()}")
    print(f"📋 场景: {[s['display_name'] for s in list_scenes()]}\n")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
