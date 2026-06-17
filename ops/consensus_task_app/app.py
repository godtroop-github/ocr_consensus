from __future__ import annotations

import asyncio
import csv
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

try:
    from .baseline_store import BaselineError, BaselineStore
except ImportError:
    from baseline_store import BaselineError, BaselineStore


ROOT = Path(__file__).resolve().parents[2]
RUNS_ROOT = Path(os.getenv("OCR_CONSENSUS_RUNS_DIR", ROOT / "ops" / "reports" / "ocr_consensus_task_runs"))
BASELINES_ROOT = Path(os.getenv("OCR_CONSENSUS_BASELINES_DIR", ROOT / "ops" / "reports" / "ocr_consensus_baselines"))
BASELINE_DB = Path(os.getenv("OCR_CONSENSUS_BASELINE_DB", BASELINES_ROOT / "baselines.db"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
COLLECT_RUNNER = ROOT / "ops" / "199_ocr_collect_runner.py"
DASHBOARD_BUILDER = ROOT / "ops" / "199_build_ocr_html_list.py"
EMPLOYMENT_EXTRACTOR = ROOT / "ops" / "extract_employment_info.py"
WEIGHTED_STRATEGY = ROOT / "ops" / "199_build_weighted_baseline_698_tables.py"
DASHBOARD_TEMPLATE = ROOT / "ops" / "consensus_task_app" / "dashboard_template"
DASHBOARD_ASSETS = DASHBOARD_TEMPLATE / "assets"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
BASE_FIELDS = ["姓名", "身份证号", "截图日期", "截图时间", "相关企业", "任职", "参股"]
METHODS = ["paddleocr_2_7_3", "rapidocr_2_7_3", "rapidocr_isolated", "paddleocr_3_x"]
RUNNER_ENGINE_NAMES = {
    "paddleocr_2_7_3": "paddleocr_2_7_3",
    "rapidocr_2_7_3": "rapidocr_current",
    "rapidocr_isolated": "rapidocr_isolated",
    "paddleocr_3_x": "paddleocr_3_x",
}
ID_PREFIX3_ALLOWED = re.compile(
    r"^(11[0-9]|12[0-9]|13[0-9]|14[0-9]|15[0-9]|21[0-9]|22[0-9]|23[0-9]|"
    r"31[0-9]|32[0-9]|33[0-9]|34[0-9]|35[0-9]|36[0-9]|37[0-9]|"
    r"41[0-9]|42[0-9]|43[0-9]|44[0-9]|45[0-9]|46[0-9]|50[0-9]|"
    r"51[0-9]|52[0-9]|53[0-9]|54[0-9]|61[0-9]|62[0-9]|63[0-9]|"
    r"64[0-9]|65[0-9]|71[0-9]|81[0-9]|82[0-9])$"
)
NAME_NOISE_TOKENS = {
    "无经商办企业",
    "无经商办企",
    "有经商办企业",
    "经商办企业",
    "新增经商办企业",
    "无新增经商办企业",
    "无新增办企业",
    "无新增",
    "经商办企截图",
    "经商办企",
    "投资任职信息查询",
    "投资任职情况查询",
    "投资任职情况",
    "投资任职信息",
    "龙信图片",
    "图片",
    "截图",
}
PERSON_STOPWORDS = NAME_NOISE_TOKENS | {
    "自查",
    "龙信",
    "注销",
    "已注销",
    "未注销",
    "吊销",
    "存续",
    "开业",
    "在业",
    "迁出",
    "撤销",
    "停业",
    "查询",
    "结果",
    "时间",
    "运营商",
    "手机",
    "工商",
    "信息",
    "相关企业",
    "任职",
    "参股",
    "查询结论",
    "未查询到",
    "本查询结果",
}

RUNS_ROOT.mkdir(parents=True, exist_ok=True)
BASELINES_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="4OCR Consensus Task App", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/runs", StaticFiles(directory=str(RUNS_ROOT)), name="runs")
app.mount("/assets", StaticFiles(directory=str(DASHBOARD_ASSETS)), name="dashboard_assets")

TASKS: Dict[str, Dict[str, Any]] = {}
TASK_QUEUE: asyncio.Queue[str] = asyncio.Queue()
WORKER_TASK: Optional[asyncio.Task] = None
TASK_PROCS: Dict[str, Set[Any]] = defaultdict(set)
BASELINE_STORE = BaselineStore(BASELINE_DB, RUNS_ROOT)


class TaskStopped(Exception):
    pass


@app.middleware("http")
async def _no_cache_static(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path in {"/", "/baselines"} or path.startswith("/runs/") or path.startswith("/static/") or path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_name(name: str) -> str:
    name = Path(name or "upload").name.strip()
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = name.replace("/", "_").replace("\\", "_").replace(":", "_")
    return name or "upload"


def _task_path(task_id: str) -> Path:
    return RUNS_ROOT / task_id / "task.json"


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_task(task: Dict[str, Any]) -> None:
    path = _task_path(task["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    TASKS[task["id"]] = task


def _update_task(task_id: str, **updates: Any) -> Dict[str, Any]:
    task = TASKS[task_id]
    if "progress" in updates:
        try:
            old_progress = int(task.get("progress") or 0)
            new_progress = int(updates.get("progress") or 0)
            if task.get("status") == "running" and new_progress < old_progress:
                updates["progress"] = old_progress
        except Exception:
            pass
    task.update(updates)
    task["updated_at"] = _now()
    _save_task(task)
    return task


def _run_timer_start_updates(task: Dict[str, Any]) -> Dict[str, Any]:
    updates: Dict[str, Any] = {
        "run_active_since_ms": _now_ms(),
    }
    if not task.get("started_at"):
        updates["started_at"] = _now()
    if not task.get("first_started_at"):
        updates["first_started_at"] = updates.get("started_at") or _now()
    if task.get("run_elapsed_ms") is None:
        updates["run_elapsed_ms"] = 0
    return updates


def _run_timer_stop_updates(task_id: str) -> Dict[str, Any]:
    task = TASKS.get(task_id, {})
    elapsed = int(task.get("run_elapsed_ms") or 0)
    active_since = int(task.get("run_active_since_ms") or 0)
    if active_since > 0:
        elapsed += max(0, _now_ms() - active_since)
    return {
        "run_elapsed_ms": elapsed,
        "run_active_since_ms": 0,
    }


def _load_existing_tasks() -> None:
    TASKS.clear()
    for path in sorted(RUNS_ROOT.glob("*/task.json")):
        task = _load_json(path, None)
        if not isinstance(task, dict) or not task.get("id"):
            continue
        if task.get("status") == "running":
            elapsed = int(task.get("run_elapsed_ms") or 0)
            active_since = int(task.get("run_active_since_ms") or 0)
            if active_since > 0:
                elapsed += max(0, _now_ms() - active_since)
            task["status"] = "queued"
            task["phase"] = "requeued_after_restart"
            task["progress"] = min(int(task.get("progress") or 0), 5)
            task["run_elapsed_ms"] = elapsed
            task["run_active_since_ms"] = 0
        TASKS[task["id"]] = task
        _save_task(task)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _python_for_engine(engine: str) -> str:
    defaults = {
        "paddleocr_2_7_3": sys.executable,
        "rapidocr_2_7_3": sys.executable,
        "rapidocr_isolated": sys.executable,
        "paddleocr_3_x": sys.executable,
    }
    env_keys = {
        "paddleocr_2_7_3": "OCR_PADDLE2_PY",
        "rapidocr_2_7_3": "OCR_RAPID_PY",
        "rapidocr_isolated": "OCR_RAPID_ISOLATED_PY",
        "paddleocr_3_x": "OCR_PADDLE3_PY",
    }
    configured = os.getenv(env_keys[engine])
    if configured:
        return configured
    default = defaults[engine]
    return default if Path(default).exists() else sys.executable


def _runner_env(overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = os.environ.copy()
    root_parent = str(ROOT.parent)
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root_parent + (os.pathsep + current if current else "")
    if overrides:
        env.update({str(k): str(v) for k, v in overrides.items()})
    return env


def _stop_requested(task_id: str) -> bool:
    task = TASKS.get(task_id, {})
    return bool(task.get("stop_requested")) or task.get("status") == "stopped"


def _raise_if_stopped(task_id: str) -> None:
    if _stop_requested(task_id):
        raise TaskStopped("任务已停止")


def _register_proc(task_id: str, proc: Any) -> None:
    TASK_PROCS[task_id].add(proc)


def _unregister_proc(task_id: str, proc: Any) -> None:
    if task_id in TASK_PROCS:
        TASK_PROCS[task_id].discard(proc)
        if not TASK_PROCS[task_id]:
            TASK_PROCS.pop(task_id, None)


async def _terminate_task_procs(task_id: str) -> None:
    procs = list(TASK_PROCS.get(task_id, set()))
    for proc in procs:
        try:
            if proc.returncode is None:
                proc.terminate()
        except ProcessLookupError:
            pass
        except Exception:
            pass
    for proc in procs:
        try:
            if proc.returncode is None:
                await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
        except Exception:
            pass


def _is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS


def _unique_dest(directory: Path, filename: str) -> Path:
    base = _safe_name(filename)
    stem = Path(base).stem
    suffix = Path(base).suffix
    if not suffix:
        suffix = ".jpg"
    candidate = directory / f"{stem}{suffix}"
    idx = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{idx}{suffix}"
        idx += 1
    return candidate


def _extract_images(task: Dict[str, Any]) -> List[str]:
    task_dir = RUNS_ROOT / task["id"]
    input_paths = task.get("input_paths")
    if not isinstance(input_paths, list) or not input_paths:
        input_paths = [task.get("input_path", "")]
    image_dir = task_dir / "input_images"
    if image_dir.exists():
        shutil.rmtree(image_dir)
    image_dir.mkdir(parents=True, exist_ok=True)

    image_paths: List[str] = []
    for raw_path in input_paths:
        input_path = Path(str(raw_path))
        if zipfile.is_zipfile(input_path):
            with zipfile.ZipFile(input_path) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    raw_name = info.filename
                    if raw_name.startswith("__MACOSX/") or Path(raw_name).name.startswith("._"):
                        continue
                    if Path(raw_name).suffix.lower() not in IMAGE_EXTS:
                        continue
                    dest = _unique_dest(image_dir, Path(raw_name).name)
                    with zf.open(info) as src, dest.open("wb") as out:
                        shutil.copyfileobj(src, out)
                    image_paths.append(str(dest))
        elif _is_image(input_path):
            dest = _unique_dest(image_dir, input_path.name)
            shutil.copy2(input_path, dest)
            image_paths.append(str(dest))
        else:
            raise ValueError(f"不支持的上传文件：{input_path.name}")

    image_paths = sorted(image_paths, key=lambda p: Path(p).name)
    if not image_paths:
        raise ValueError("未从上传内容中找到图片")
    return image_paths


async def _run_engine(
    task_id: str,
    engine: str,
    image_dir: Path,
    output_path: Path,
    progress_start: int,
    progress_end: int,
) -> Dict[str, Any]:
    _raise_if_stopped(task_id)
    python_bin = _python_for_engine(engine)
    runner_engine = RUNNER_ENGINE_NAMES.get(engine, engine)
    log_path = output_path.with_suffix(".log")
    cmd = [
        python_bin,
        str(COLLECT_RUNNER),
        "--engine",
        runner_engine,
        "--img-dir",
        str(image_dir),
        "--output",
        str(output_path),
    ]
    if os.getenv("OCR_DISABLE_GPU", "").lower() in {"1", "true", "yes"}:
        cmd.append("--no-gpu")

    started = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(ROOT),
        env=_runner_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _register_proc(task_id, proc)
    stdout_lines: List[str] = []
    try:
        stderr_task = asyncio.create_task(proc.stderr.read())
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            if _stop_requested(task_id):
                try:
                    proc.terminate()
                except Exception:
                    pass
                break
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            stdout_lines.append(line)
            if not line.startswith("OCR_PROGRESS "):
                continue
            try:
                progress_event = json.loads(line[len("OCR_PROGRESS ") :])
            except Exception:
                continue
            total = int(progress_event.get("total") or 0)
            index = int(progress_event.get("index") or 0)
            done_units = max(0, index - 1)
            if progress_event.get("stage") in {"done", "error"}:
                done_units = index
            if total > 0:
                fraction = max(0.0, min(1.0, done_units / total))
            else:
                fraction = 0.0
            filename = str(progress_event.get("filename") or "")
            task = TASKS.get(task_id, {})
            engine_progress = dict(task.get("engine_progress") or {})
            engine_progress[engine] = {
                "fraction": fraction,
                "filename": filename,
                "index": index,
                "total": total,
                "stage": str(progress_event.get("stage") or ""),
            }
            avg_fraction = sum(float(v.get("fraction") or 0.0) for v in engine_progress.values()) / max(1, len(METHODS))
            progress = progress_start + int((progress_end - progress_start) * avg_fraction)
            _update_task(
                task_id,
                current_engine=engine,
                current_image=filename,
                current_image_index=index,
                current_image_total=total,
                engine_progress=engine_progress,
                progress=min(progress, progress_end),
                phase=f"ocr:{engine}:{filename}" if filename else f"ocr:{engine}",
            )
        stderr = await stderr_task
        await proc.wait()
    finally:
        _unregister_proc(task_id, proc)
    elapsed = time.perf_counter() - started
    log_path.write_text(
        "\n".join(
            [
                "$ " + " ".join(cmd),
                "",
                "[stdout]",
                "\n".join(stdout_lines),
                "",
                "[stderr]",
                stderr.decode("utf-8", errors="replace"),
                "",
                f"[returncode] {proc.returncode}",
                f"[elapsed_sec] {elapsed:.3f}",
            ]
        ),
        encoding="utf-8",
    )
    _raise_if_stopped(task_id)
    return {
        "engine": engine,
        "runner_engine": runner_engine,
        "python": python_bin,
        "returncode": proc.returncode,
        "elapsed_sec": elapsed,
        "output": str(output_path),
        "log": str(log_path),
    }


def _engine_output_complete(output_path: Path, image_paths: List[str]) -> bool:
    records = _load_json(output_path, [])
    if not isinstance(records, list) or len(records) != len(image_paths):
        return False
    expected = [Path(p).name for p in image_paths]
    got = [Path(str(r.get("filename", ""))).name for r in records if isinstance(r, dict)]
    return got == expected


def _queue_result_count(result_dir: Path) -> int:
    if not result_dir.exists():
        return 0
    return len([p for p in result_dir.glob("*.json") if p.is_file()])


def _prepare_engine_queue(queue_dir: Path, result_dir: Path, image_paths: List[str], fresh: bool) -> None:
    if fresh and queue_dir.exists():
        shutil.rmtree(queue_dir)
    pending_dir = queue_dir / "pending"
    processing_dir = queue_dir / "processing"
    done_dir = queue_dir / "done"
    result_dir.mkdir(parents=True, exist_ok=True)
    pending_dir.mkdir(parents=True, exist_ok=True)
    processing_dir.mkdir(parents=True, exist_ok=True)
    done_dir.mkdir(parents=True, exist_ok=True)

    for directory in (pending_dir, processing_dir):
        if directory.exists():
            for path in directory.glob("*.json"):
                try:
                    path.unlink()
                except OSError:
                    pass

    for seq, image_path in enumerate(image_paths, start=1):
        result_path = result_dir / f"{seq:06d}.json"
        if result_path.exists():
            continue
        payload = {
            "seq": seq,
            "path": image_path,
            "filename": Path(image_path).name,
        }
        (pending_dir / f"{seq:06d}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _pending_count(queue_dir: Path) -> int:
    return len(list((queue_dir / "pending").glob("*.json")))


def _merge_queue_results(image_paths: List[str], result_dir: Path, output_path: Path) -> None:
    records: List[Dict[str, Any]] = []
    missing: List[str] = []
    for seq, image_path in enumerate(image_paths, start=1):
        result_path = result_dir / f"{seq:06d}.json"
        rec = _load_json(result_path, None)
        if not isinstance(rec, dict):
            missing.append(Path(image_path).name)
            continue
        got_name = Path(str(rec.get("filename", ""))).name
        if got_name != Path(image_path).name:
            raise RuntimeError(f"RapidOCR 归并错位：期望 {Path(image_path).name}，实际 {got_name}")
        records.append(rec)
    if missing:
        raise RuntimeError(f"RapidOCR 归并缺失 {len(missing)} 张：{', '.join(missing[:5])}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


async def _run_queue_worker(
    task_id: str,
    engine: str,
    queue_dir: Path,
    result_dir: Path,
    worker_output: Path,
    total: int,
    progress_start: int,
    progress_end: int,
) -> Dict[str, Any]:
    _raise_if_stopped(task_id)
    python_bin = _python_for_engine(engine)
    runner_engine = RUNNER_ENGINE_NAMES.get(engine, engine)
    log_path = worker_output.with_suffix(".log")
    cmd = [
        python_bin,
        str(COLLECT_RUNNER),
        "--engine",
        runner_engine,
        "--img-dir",
        str(queue_dir),
        "--output",
        str(worker_output),
        "--queue-dir",
        str(queue_dir),
        "--result-dir",
        str(result_dir),
        "--queue-total",
        str(total),
    ]
    if os.getenv("OCR_DISABLE_GPU", "").lower() in {"1", "true", "yes"}:
        cmd.append("--no-gpu")

    env_overrides: Dict[str, str] = {}
    if engine in {"rapidocr_2_7_3", "rapidocr_isolated"}:
        rapid_threads = str(max(1, int(os.getenv("OCR_RAPID_WORKER_THREADS", "1"))))
        env_overrides.update(
            {
                "OMP_NUM_THREADS": rapid_threads,
                "OPENBLAS_NUM_THREADS": rapid_threads,
                "MKL_NUM_THREADS": rapid_threads,
                "NUMEXPR_NUM_THREADS": rapid_threads,
                "VECLIB_MAXIMUM_THREADS": rapid_threads,
                "OMP_WAIT_POLICY": "PASSIVE",
            }
        )

    started = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(ROOT),
        env=_runner_env(env_overrides),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _register_proc(task_id, proc)
    stdout_lines: List[str] = []
    try:
        stderr_task = asyncio.create_task(proc.stderr.read())
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            if _stop_requested(task_id):
                try:
                    proc.terminate()
                except Exception:
                    pass
                break
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            stdout_lines.append(line)
            if not line.startswith("OCR_PROGRESS "):
                continue
            try:
                progress_event = json.loads(line[len("OCR_PROGRESS ") :])
            except Exception:
                continue
            done = _queue_result_count(result_dir)
            fraction = max(0.0, min(1.0, done / max(1, total)))
            filename = str(progress_event.get("filename") or "")
            task = TASKS.get(task_id, {})
            engine_progress = dict(task.get("engine_progress") or {})
            prior = dict(engine_progress.get(engine) or {})
            engine_progress[engine] = {
                **prior,
                "fraction": fraction,
                "filename": filename,
                "index": done,
                "total": total,
                "stage": str(progress_event.get("stage") or ""),
                "workers": int(prior.get("workers") or 1),
            }
            avg_fraction = sum(float(v.get("fraction") or 0.0) for v in engine_progress.values()) / max(1, len(METHODS))
            progress = progress_start + int((progress_end - progress_start) * avg_fraction)
            _update_task(
                task_id,
                current_engine=engine,
                current_image=filename,
                current_image_index=done,
                current_image_total=total,
                engine_progress=engine_progress,
                progress=min(progress, progress_end),
                phase=f"ocr:tail_boost:{engine}",
            )
        stderr = await stderr_task
        await proc.wait()
    finally:
        _unregister_proc(task_id, proc)
    elapsed = time.perf_counter() - started
    log_path.write_text(
        "\n".join(
            [
                "$ " + " ".join(cmd),
                "",
                "[stdout]",
                "\n".join(stdout_lines),
                "",
                "[stderr]",
                stderr.decode("utf-8", errors="replace"),
                "",
                f"[returncode] {proc.returncode}",
                f"[elapsed_sec] {elapsed:.3f}",
            ]
        ),
        encoding="utf-8",
    )
    _raise_if_stopped(task_id)
    return {
        "engine": engine,
        "runner_engine": runner_engine,
        "python": python_bin,
        "returncode": proc.returncode,
        "elapsed_sec": elapsed,
        "output": str(worker_output),
        "log": str(log_path),
    }


async def _run_rapid_queue_pool(
    task_id: str,
    engine: str,
    image_paths: List[str],
    work_dir: Path,
    output_path: Path,
    boost_event: asyncio.Event,
    progress_start: int,
    progress_end: int,
    max_workers: Optional[int] = None,
) -> Dict[str, Any]:
    if _engine_output_complete(output_path, image_paths):
        return {
            "engine": engine,
            "runner_engine": RUNNER_ENGINE_NAMES.get(engine, engine),
            "python": _python_for_engine(engine),
            "returncode": 0,
            "elapsed_sec": 0.0,
            "output": str(output_path),
            "log": "",
            "skipped": True,
        }

    queue_dir = work_dir / f"{engine}_queue"
    result_dir = queue_dir / "results"
    fresh = not bool(TASKS.get(task_id, {}).get("resumed"))
    _prepare_engine_queue(queue_dir, result_dir, image_paths, fresh=fresh)

    total = len(image_paths)
    max_workers = max(1, int(max_workers or int(os.getenv("OCR_TAIL_BOOST_RAPID_WORKERS", "3"))))
    worker_tasks: List[asyncio.Task] = []
    worker_results: List[Dict[str, Any]] = []
    started = time.perf_counter()

    def start_worker(worker_no: int) -> None:
        task = TASKS.get(task_id, {})
        engine_progress = dict(task.get("engine_progress") or {})
        prior = dict(engine_progress.get(engine) or {})
        engine_progress[engine] = {
            **prior,
            "workers": worker_no,
            "total": total,
            "index": _queue_result_count(result_dir),
            "fraction": _queue_result_count(result_dir) / max(1, total),
            "stage": "running",
        }
        _update_task(task_id, engine_progress=engine_progress, phase=f"ocr:tail_boost:{engine}")
        worker_output = work_dir / f"{engine}.worker{worker_no}.json"
        worker_tasks.append(
            asyncio.create_task(
                _run_queue_worker(task_id, engine, queue_dir, result_dir, worker_output, total, progress_start, progress_end)
            )
        )

    start_worker(1)
    boosted = False
    while True:
        _raise_if_stopped(task_id)
        done_count = _queue_result_count(result_dir)
        if done_count >= total:
            break
        if boost_event.is_set() and not boosted:
            boosted = True
            for worker_no in range(2, max_workers + 1):
                if _pending_count(queue_dir) <= 0:
                    break
                start_worker(worker_no)
        pending = [t for t in worker_tasks if not t.done()]
        if not pending:
            if _pending_count(queue_dir) <= 0:
                break
            start_worker(len(worker_tasks) + 1)
        await asyncio.sleep(0.5)

    if worker_tasks:
        worker_results = await asyncio.gather(*worker_tasks)
    _merge_queue_results(image_paths, result_dir, output_path)
    elapsed = time.perf_counter() - started
    return {
        "engine": engine,
        "runner_engine": RUNNER_ENGINE_NAMES.get(engine, engine),
        "python": _python_for_engine(engine),
        "returncode": 0,
        "elapsed_sec": elapsed,
        "output": str(output_path),
        "log": ";".join(str(r.get("log", "")) for r in worker_results if isinstance(r, dict)),
        "workers": min(max_workers, len(worker_tasks)),
        "tail_boost": True,
    }


def _line_confidence(lines: List[dict]) -> Dict[str, Any]:
    confs: List[float] = []
    for line in lines or []:
        if not isinstance(line, dict):
            continue
        val = line.get("confidence", line.get("score", line.get("conf")))
        try:
            conf = float(val)
        except (TypeError, ValueError):
            continue
        if conf > 1.0:
            conf = conf / 100.0
        if 0.0 <= conf <= 1.0:
            confs.append(conf)
    if not confs:
        return {"avg_confidence": 0.0, "min_confidence": 0.0, "max_confidence": 0.0, "line_conf_count": 0}
    return {
        "avg_confidence": sum(confs) / len(confs),
        "min_confidence": min(confs),
        "max_confidence": max(confs),
        "line_conf_count": len(confs),
    }


def _safe_ocr_lines(lines: List[dict]) -> List[Dict[str, Any]]:
    safe_lines: List[Dict[str, Any]] = []
    for idx, line in enumerate(lines or []):
        if not isinstance(line, dict):
            continue
        item: Dict[str, Any] = {
            "index": idx + 1,
            "text": str(line.get("text", "") or ""),
        }
        val = line.get("confidence", line.get("score", line.get("conf")))
        try:
            conf = float(val)
            if conf > 1.0:
                conf = conf / 100.0
            if 0.0 <= conf <= 1.0:
                item["confidence"] = conf
        except (TypeError, ValueError):
            pass
        if "bbox" in line:
            item["bbox"] = line.get("bbox")
        safe_lines.append(item)
    return safe_lines


def _norm_compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().lower()


def _normalize_capture_date(value: Any) -> str:
    raw = str(value or "").strip().replace("：", ":").replace("－", "-").replace("/", "-")
    compact = re.sub(r"[^0-9]", "", raw)
    if len(compact) == 4:
        return f"{compact[:2]}-{compact[2:]}"
    m = re.search(r"(?<!\d)(\d{2})-(\d{2})(?!\d)", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return ""


def _normalize_capture_time(value: Any) -> str:
    raw = str(value or "").strip().replace("：", ":")
    m = re.search(r"(?<!\d)(\d{2}):(\d{2})(?!\d)", raw)
    if not m:
        return ""
    return f"{m.group(1)}:{m.group(2)}"


def _is_valid_person_name(value: Any) -> bool:
    name = str(value or "").strip()
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,4}", name):
        return False
    if name in PERSON_STOPWORDS:
        return False
    if any(token in name for token in PERSON_STOPWORDS if len(token) >= 2):
        return False
    return True


def _clean_person_candidate(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    for token in sorted(NAME_NOISE_TOKENS, key=len, reverse=True):
        raw = raw.replace(token, " ")
    raw = re.sub(r"^\d{6,20}", "", raw)
    chunks = re.findall(r"[\u4e00-\u9fff]{2,8}", raw)
    for chunk in reversed(chunks):
        if _is_valid_person_name(chunk):
            return chunk
    return ""


def _person_candidates_from_text(text: Any) -> List[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    for token in sorted(NAME_NOISE_TOKENS, key=len, reverse=True):
        raw = raw.replace(token, " ")
    candidates: List[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,8}", raw):
        cand = _clean_person_candidate(chunk)
        if cand:
            candidates.append(cand)
    return candidates


def _parse_filename_person(filename: str) -> str:
    stem = Path(str(filename or "")).name.rsplit(".", 1)[0]
    candidates: List[str] = []
    for seg in re.split(r"[-_+\s]+", stem):
        seg = re.sub(r"^\d{6,20}", "", seg.strip())
        if not seg:
            continue
        candidates.extend(_person_candidates_from_text(seg))
    if not candidates:
        candidates = _person_candidates_from_text(stem)
    for cand in reversed(candidates):
        if 2 <= len(cand) <= 4:
            return cand
    return candidates[-1] if candidates else ""


def _line_is_name_noise(line: str) -> bool:
    text = str(line or "").strip()
    if not text:
        return True
    if re.search(r"\d{3}\s*[*＊★]|[0-9]{1,2}[-/][0-9]{1,2}|[0-9]{1,2}:[0-9]{2}", text):
        return True
    noise_words = {
        "投资任职信息",
        "查询结论",
        "未查询到",
        "本查询结果",
        "相关企业",
        "任职",
        "参股",
        "身份证",
        "企业名称",
        "经营状态",
        "承担职务",
        "持股比例",
    }
    return any(word in text for word in noise_words)


def _extract_ocr_name_from_text(text: Any) -> str:
    lines = [str(x or "").strip() for x in str(text or "").splitlines()]
    if not lines:
        return ""
    sep_idx = -1
    for i, line in enumerate(lines):
        if "投资任职信息" in line and "未查询" not in line:
            sep_idx = i
            break
    windows: List[tuple[int, int]] = []
    if sep_idx >= 0:
        windows.append((sep_idx + 1, min(len(lines), sep_idx + 7)))
        windows.append((max(0, sep_idx - 3), sep_idx))
    windows.append((0, min(len(lines), 12)))
    windows.append((0, len(lines)))
    for start, end in windows:
        for line in lines[start:end]:
            if _line_is_name_noise(line):
                continue
            cleaned = re.sub(r"^[.。·•丨|｜\[\]【】「」『』()（）\s]+", "", line)
            cleaned = re.sub(r"[.。·•丨|｜\[\]【】「」『』()（）\s]+$", "", cleaned)
            cand = _clean_person_candidate(cleaned)
            if cand:
                return cand
    return ""


def _calibrated_name_consensus(rec: Dict[str, Any], selected: Any, selected_methods: List[str], selected_score: float) -> Dict[str, Any]:
    methods_data = rec.get("methods", {}) or {}
    filename = str(rec.get("filename", "") or "")
    file_candidate = _parse_filename_person(filename)
    current = _clean_person_candidate(selected)

    ocr_votes: Dict[str, List[str]] = defaultdict(list)
    method_votes: Dict[str, List[str]] = defaultdict(list)
    for method in METHODS:
        md = methods_data.get(method, {}) or {}
        ocr_name = _extract_ocr_name_from_text(md.get("原始OCR", ""))
        if ocr_name:
            ocr_votes[ocr_name].append(method)
        method_name = _clean_person_candidate(md.get("姓名"))
        if method_name:
            method_votes[method_name].append(method)

    ocr_top = ""
    ocr_methods: List[str] = []
    if ocr_votes:
        ocr_top, ocr_methods = sorted(ocr_votes.items(), key=lambda kv: (len(kv[1]), kv[0]), reverse=True)[0]
    method_top = ""
    method_methods: List[str] = []
    if method_votes:
        method_top, method_methods = sorted(method_votes.items(), key=lambda kv: (len(kv[1]), kv[0]), reverse=True)[0]

    value = current
    support = selected_methods
    score = selected_score
    source = "weighted_harness"
    if ocr_top and len(ocr_methods) >= 2 and (not current or not _is_valid_person_name(current) or current == ocr_top or file_candidate == ocr_top):
        value = ocr_top
        support = ocr_methods
        score = max(score, 1.0 + 0.2 * len(ocr_methods))
        source = "name_calibration_ocr_line"
    elif file_candidate and (not value or not _is_valid_person_name(value) or value in PERSON_STOPWORDS):
        value = file_candidate
        support = ["filename"]
        score = max(score, 0.85)
        source = "name_calibration_filename"
    elif not value and method_top:
        value = method_top
        support = method_methods
        score = max(score, 0.75 + 0.1 * len(method_methods))
        source = "name_calibration_method_vote"

    return {
        "value": value,
        "support": support,
        "score": score,
        "source": source,
        "filename_candidate": file_candidate,
        "ocr_line_top": ocr_top,
        "ocr_line_support": len(ocr_methods),
        "raw_value": selected,
    }


def _best_line_match(value: Any, lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    target = _norm_compact(value)
    if not target:
        return {}
    best: Dict[str, Any] = {}
    best_score = -1
    for line in lines or []:
        text = str(line.get("text", "") or "")
        compact = _norm_compact(text)
        if not compact:
            continue
        score = 0
        if target in compact:
            score = len(target) + 100
        elif compact in target:
            score = len(compact)
        if score <= best_score:
            continue
        best_score = score
        best = line
    return best if best_score > 0 else {}


def _field_scores_from_lines(parsed: Dict[str, Any], raw_lines: List[dict]) -> Dict[str, Any]:
    existing = parsed.get("field_scores")
    if isinstance(existing, dict) and existing:
        return existing

    lines = _safe_ocr_lines(raw_lines)
    method_score = parsed.get("method_score") or _line_confidence(raw_lines)
    method_avg = float(method_score.get("avg_confidence") or 0.0) if isinstance(method_score, dict) else 0.0
    provenance_map = parsed.get("field_provenance") if isinstance(parsed.get("field_provenance"), dict) else {}
    scores: Dict[str, Any] = {}

    for field in BASE_FIELDS + ["查询结论", "任职情况信息"]:
        value = parsed.get(field)
        if value is None or str(value).strip() == "":
            continue
        provenance = provenance_map.get(field, {}) if isinstance(provenance_map, dict) else {}
        rule_processed = bool(provenance.get("rule_processed")) if isinstance(provenance, dict) else False
        match = _best_line_match(value, lines)
        confidence = match.get("confidence")
        if confidence is not None and not rule_processed:
            source = "ocr_line_match"
            line_indices = [match.get("index")]
        elif confidence is not None:
            source = "rule_processed_line_match"
            line_indices = [match.get("index")]
        else:
            confidence = method_avg
            source = "rule_processed_fallback" if rule_processed else "method_avg_fallback"
            line_indices = []

        scores[field] = {
            "confidence": confidence,
            "confidence_source": source,
            "line_indices": [i for i in line_indices if i],
            "provenance": provenance,
            "rule_processed": rule_processed,
        }
    return scores


def _roi_id_candidate_from_text(text: str) -> Dict[str, Any]:
    raw = str(text or "").upper().replace("＊", "*").replace("★", "*").replace("·", "*").replace("。", ".")
    raw = raw.replace("……", "*").replace("…", "*")
    candidates: List[Dict[str, Any]] = []
    for m in re.finditer(r"(?<!\d)(\d{3})([\s*.\-_/]{3,40})(\d{3}[0-9X])(?!\d)", raw):
        prefix, middle, tail = m.group(1), m.group(2), m.group(3)
        mask_count = len(re.findall(r"[*.\-_/]", middle))
        if mask_count < 3:
            continue
        if not ID_PREFIX3_ALLOWED.match(prefix):
            continue
        candidates.append(
            {
                "value": f"{prefix}********{tail}",
                "prefix": prefix,
                "tail": tail,
                "mask_count": mask_count,
                "raw": m.group(0),
            }
        )
    if not candidates:
        return {}
    candidates.sort(key=lambda c: (c["mask_count"], len(c["raw"])), reverse=True)
    return candidates[0]


def _make_roi_id_crops(image_path: Path, out_dir: Path) -> List[Path]:
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    except Exception:
        return []

    try:
        im = Image.open(image_path).convert("RGB")
    except Exception:
        return []
    w, h = im.size
    if w < 600 or h < 1000:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    boxes = {
        "header": (0, int(h * 0.04), w, int(h * 0.19)),
        "id_band": (0, int(h * 0.09), w, int(h * 0.17)),
        "id_band_left": (0, int(h * 0.09), int(w * 0.74), int(h * 0.17)),
        "id_band_center": (int(w * 0.07), int(h * 0.085), int(w * 0.94), int(h * 0.175)),
    }
    crop_paths: List[Path] = []
    for name, box in boxes.items():
        left, top, right, bottom = box
        if right <= left or bottom <= top:
            continue
        crop = im.crop((left, top, right, bottom))
        variants = [("orig", crop)]
        for scale in (2, 3):
            up = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
            gray = ImageOps.grayscale(up)
            enhanced = ImageEnhance.Contrast(gray).enhance(2.0)
            sharp = enhanced.filter(ImageFilter.UnsharpMask(radius=1.1, percent=180, threshold=2))
            variants.append((f"x{scale}", sharp.convert("RGB")))
        for suffix, variant in variants:
            p = out_dir / f"{image_path.stem}__{name}_{suffix}.jpg"
            variant.save(p, quality=95)
            crop_paths.append(p)
    return crop_paths


def _run_roi_id_ocr(image_path: Path) -> Dict[str, Any]:
    task_dir = image_path.parent.parent
    crop_dir = task_dir / "work" / "roi_id" / image_path.stem
    output_dir = task_dir / "work" / "roi_id_outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    crop_paths = _make_roi_id_crops(image_path, crop_dir)
    if not crop_paths:
        return {}

    engine_results: List[Dict[str, Any]] = []
    for engine in ("rapidocr_isolated", "paddleocr_3_x"):
        python_bin = _python_for_engine(engine)
        runner_engine = RUNNER_ENGINE_NAMES.get(engine, engine)
        out_json = output_dir / f"{image_path.stem}__{engine}.json"
        cmd = [
            python_bin,
            str(COLLECT_RUNNER),
            "--engine",
            runner_engine,
            "--img-dir",
            str(crop_dir),
            "--output",
            str(out_json),
        ]
        if os.getenv("OCR_DISABLE_GPU", "").lower() in {"1", "true", "yes"}:
            cmd.append("--no-gpu")
        try:
            subprocess.run(
                cmd,
                cwd=str(ROOT),
                env=_runner_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90,
                check=False,
            )
        except Exception as exc:
            engine_results.append({"engine": engine, "error": repr(exc)})
            continue
        records = _load_json(out_json, [])
        if not isinstance(records, list):
            records = []
        for rec in records:
            text = "\n".join(str(x.get("text", "")) for x in rec.get("lines", []) if isinstance(x, dict))
            if not text:
                text = str(rec.get("text", "") or "")
            cand = _roi_id_candidate_from_text(text)
            if not cand:
                continue
            engine_results.append(
                {
                    "engine": engine,
                    "crop": Path(str(rec.get("filename", ""))).name,
                    "text": text,
                    "candidate": cand,
                    "avg_confidence": rec.get("avg_confidence"),
                }
            )

    votes: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in engine_results:
        cand = item.get("candidate") if isinstance(item, dict) else None
        value = cand.get("value") if isinstance(cand, dict) else ""
        if value:
            votes[value].append(item)
    if not votes:
        return {"attempted": True, "crop_count": len(crop_paths), "engine_results": engine_results, "value": ""}
    value, support = sorted(votes.items(), key=lambda kv: (len(kv[1]), kv[0]), reverse=True)[0]
    return {
        "attempted": True,
        "crop_count": len(crop_paths),
        "value": value,
        "support_count": len(support),
        "support": support[:6],
        "engine_results": engine_results[:20],
    }


def _import_business_scene():
    for p in (str(ROOT.parent), str(ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        from ops.consensus_task_app.business_check_harness import BusinessCheckScene
        return BusinessCheckScene
    except Exception:
        pass
    try:
        from ocr_system.scenes.business_check import BusinessCheckScene
    except Exception:
        from scenes.business_check import BusinessCheckScene
    return BusinessCheckScene


def _parse_engine_records(engine_jsons: Dict[str, Path], image_paths: List[str]) -> List[Dict[str, Any]]:
    BusinessCheckScene = _import_business_scene()
    scene = BusinessCheckScene()
    by_engine: Dict[str, Dict[str, dict]] = {}

    for engine, path in engine_jsons.items():
        raw_records = _load_json(path, [])
        if not isinstance(raw_records, list):
            raw_records = []
        by_engine[engine] = {}
        for rec in raw_records:
            filename = Path(str(rec.get("filename", ""))).name
            if filename:
                by_engine[engine][filename] = rec

    flat_records: List[Dict[str, Any]] = []
    for image_path in image_paths:
        filename = Path(image_path).name
        methods: Dict[str, dict] = {}
        issues: List[str] = []

        for engine in METHODS:
            raw = by_engine.get(engine, {}).get(filename)
            if not raw:
                methods[engine] = {
                    "error": "engine output missing",
                    "time_ms": 0,
                    "line_count": 0,
                    "原始OCR": "",
                    "method_score": _line_confidence([]),
                    "field_scores": {},
                }
                issues.append(f"{engine}: missing output")
                continue

            ocr_result = {
                "text": raw.get("text", ""),
                "lines": raw.get("lines", []),
                "filename": filename,
            }
            try:
                parsed = scene.parse(ocr_result)
                if not isinstance(parsed, dict):
                    parsed = {}
            except Exception as exc:
                parsed = {}
                issues.append(f"{engine}: harness parse failed: {exc}")

            parsed["error"] = raw.get("error", "") or parsed.get("error", "")
            parsed["time_ms"] = raw.get("elapsed_ms", raw.get("time_ms", 0))
            parsed["line_count"] = len(raw.get("lines") or [])
            parsed["原始OCR"] = raw.get("text", "") or parsed.get("原始OCR", "")
            parsed["method_score"] = parsed.get("method_score") or _line_confidence(raw.get("lines") or [])
            parsed["ocr_lines"] = _safe_ocr_lines(raw.get("lines") or [])
            parsed["field_scores"] = _field_scores_from_lines(parsed, raw.get("lines") or [])
            methods[engine] = parsed
            if parsed.get("error"):
                issues.append(f"{engine}: {parsed.get('error')}")

        rec = {
            "filename": filename,
            "source_path": image_path,
            "methods": methods,
            "issues": issues,
        }
        rec["weighted_consensus"] = _weighted_consensus(rec)
        if not str(rec.get("weighted_consensus", {}).get("fields", {}).get("身份证号", "") or "").strip():
            roi = _run_roi_id_ocr(Path(image_path))
            if roi.get("value"):
                rec["roi_id_fallback"] = roi
                rec["weighted_consensus"].setdefault("fields", {})["身份证号"] = roi["value"]
                rec["weighted_consensus"].setdefault("support", {})["身份证号"] = ["roi_id_fallback"]
                rec["weighted_consensus"].setdefault("scores", {})["身份证号"] = 1.2 + min(0.3, 0.05 * int(roi.get("support_count") or 1))
                rec["weighted_consensus"].setdefault("sources", {})["身份证号"] = "roi_id_fallback"
            elif roi:
                rec["roi_id_fallback"] = roi
        flat_records.append(rec)

    return flat_records


_DIRECT_MASKED_ID_RE = re.compile(
    r"(?<![0-9A-Za-z])(\d{3})\s*([*＊★.\-_/·\s]{3,24})\s*([0-9Xx×*＊★.\-_/·\s]{1,18})(?![0-9A-Za-z])"
)


def _normalize_direct_id_candidate(prefix: str, tail: str) -> str:
    p = re.sub(r"\D", "", prefix or "")
    raw_tail = str(tail or "").upper().replace("×", "X").replace("Ｘ", "X")
    t = re.sub(r"[^0-9X]", "", raw_tail)
    if len(p) != 3 or len(t) < 4 or not ID_PREFIX3_ALLOWED.match(p):
        return ""
    for start in range(0, len(t) - 3):
        window = t[start : start + 4]
        if "X" in window[:3]:
            continue
        if window.endswith("X"):
            return f"{p}********{window}"
    for start in range(0, len(t) - 3):
        window = t[start : start + 4]
        if "X" not in window:
            return f"{p}********{window}"
    return ""


def _direct_id_candidates_from_method(method_name: str, method_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    seen = set()

    def add_candidate(value: str, raw: str, confidence: float, quality: float, line_index: int) -> None:
        if not value or value in seen:
            return
        seen.add(value)
        candidates.append(
            {
                "method": method_name,
                "value": value,
                "raw": raw,
                "confidence": confidence,
                "quality": quality,
                "line_index": line_index,
            }
        )

    line_items: List[Dict[str, Any]] = []
    for line in method_data.get("ocr_lines") or []:
        if not isinstance(line, dict):
            continue
        text = str(line.get("text", "") or "")
        if text.strip():
            line_items.append(
                {
                    "text": text,
                    "confidence": float(line.get("confidence") or 0.0),
                    "index": int(line.get("index") or 0),
                }
            )
    existing_texts = {str(item.get("text", "") or "") for item in line_items}
    extra_texts: List[str] = []
    for key in ["任职情况信息", "原始OCR"]:
        text = str(method_data.get(key, "") or "")
        if not text.strip():
            continue
        if not re.search(r"[Xx×Ｘｘ]", text):
            continue
        extra_texts.extend(line for line in text.splitlines() if line.strip())
        compact = re.sub(r"\s+", "", text)
        if compact:
            extra_texts.append(compact)
    for text in extra_texts:
        if not text.strip() or text in existing_texts:
            continue
        existing_texts.add(text)
        line_items.append({"text": text, "confidence": 0.0, "index": 0})

    for item in line_items:
        text = str(item.get("text", "") or "")
        normalized_text = text.replace("＊", "*").replace("★", "*").replace("·", "*").replace("×", "X")
        for match in _DIRECT_MASKED_ID_RE.finditer(normalized_text):
            prefix, stars, tail = match.group(1), match.group(2), match.group(3)
            value = _normalize_direct_id_candidate(prefix, tail)
            if not value:
                continue
            star_count = len(re.sub(r"[^*]", "", stars.replace("＊", "*").replace("★", "*")))
            tail_norm = str(tail or "").upper().replace("×", "X")
            tail_len = len(re.sub(r"[^0-9X]", "", tail_norm))
            quality = 1.0
            if star_count < 6:
                quality -= 0.25
            elif star_count != 8:
                quality -= 0.08
            if tail_len > 4:
                quality -= 0.12
            explicit_x = "X" in tail_norm
            if explicit_x and value.endswith("X"):
                quality += 0.28
            elif explicit_x and not value.endswith("X"):
                quality -= 0.35
            add_candidate(
                value=value,
                raw=match.group(0),
                confidence=float(item.get("confidence") or 0.0),
                quality=max(0.2, quality),
                line_index=int(item.get("index") or 0),
            )

    return candidates


def _pick_direct_id_consensus(methods_data: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    engine_weight = {
        "paddleocr_2_7_3": 0.95,
        "rapidocr_2_7_3": 1.00,
        "rapidocr_isolated": 1.05,
        "paddleocr_3_x": 1.02,
    }
    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for method in METHODS:
        for cand in _direct_id_candidates_from_method(method, methods_data.get(method, {}) or {}):
            buckets[cand["value"]].append(cand)
    if not buckets:
        return {}

    best_value = ""
    best_items: List[Dict[str, Any]] = []
    best_score = -1.0
    for value, items in buckets.items():
        support_methods = {str(item.get("method", "")) for item in items}
        confidence_avg = sum(float(item.get("confidence") or 0.0) for item in items) / max(1, len(items))
        quality_avg = sum(float(item.get("quality") or 0.0) for item in items) / max(1, len(items))
        weight_sum = sum(engine_weight.get(method, 1.0) for method in support_methods)
        score = weight_sum + 0.35 * confidence_avg + 0.50 * quality_avg + 0.20 * len(support_methods)
        if score > best_score + 1e-9:
            best_value = value
            best_items = items
            best_score = score
        elif abs(score - best_score) <= 1e-9:
            if len(support_methods) > len({str(item.get("method", "")) for item in best_items}):
                best_value = value
                best_items = items
                best_score = score

    return {
        "value": best_value,
        "methods": sorted({str(item.get("method", "")) for item in best_items if item.get("method")}),
        "score": best_score,
        "candidates": best_items,
    }


def _weighted_consensus(rec: Dict[str, Any]) -> Dict[str, Any]:
    strategy = _load_module("ocr_weighted_strategy_task_app", WEIGHTED_STRATEGY)
    methods_data = rec.get("methods", {})
    decision_methods_data = {
        m: {**(methods_data.get(m, {}) or {}), "field_scores": {}}
        for m in METHODS
    }
    fields: Dict[str, str] = {}
    support: Dict[str, List[str]] = {}
    scores: Dict[str, float] = {}
    sources: Dict[str, str] = {}

    for field in ["姓名", "身份证号", "相关企业", "任职", "参股"]:
        if field == "身份证号":
            direct = _pick_direct_id_consensus(methods_data)
            if direct.get("value"):
                fields[field] = direct["value"]
                support[field] = direct.get("methods", [])
                scores[field] = float(direct.get("score") or 0.0)
                sources[field] = "direct_ocr_id"
                continue
        candidates = {}
        for m in METHODS:
            raw_value = methods_data.get(m, {}).get(field)
            candidates[m] = "" if raw_value is None else str(raw_value)
        selected = strategy.weighted_pick(field, candidates, decision_methods_data)
        if field == "姓名":
            calibrated = _calibrated_name_consensus(rec, selected.value, selected.methods, selected.score)
            fields[field] = calibrated["value"]
            support[field] = calibrated["support"]
            scores[field] = calibrated["score"]
            sources[field] = calibrated["source"]
        else:
            fields[field] = selected.value
            support[field] = selected.methods
            scores[field] = selected.score
            sources[field] = "weighted_harness"

    date_votes: Dict[str, List[str]] = defaultdict(list)
    time_votes: Dict[str, List[str]] = defaultdict(list)
    for m in METHODS:
        md = methods_data.get(m, {})
        d = _normalize_capture_date(md.get("截图日期"))
        t = _normalize_capture_time(md.get("截图时间"))
        if not d or not t:
            extracted_d, extracted_t = strategy.extract_date_time(md.get("任职情况信息", ""))
            if not d:
                d = _normalize_capture_date(extracted_d)
            if not t:
                t = _normalize_capture_time(extracted_t)
        if d:
            date_votes[d].append(m)
        if t:
            time_votes[t].append(m)
    if date_votes:
        fields["截图日期"] = sorted(date_votes.items(), key=lambda kv: (len(kv[1]), len(kv[0])), reverse=True)[0][0]
        support["截图日期"] = date_votes[fields["截图日期"]]
        scores["截图日期"] = 0.9
    else:
        fields["截图日期"] = ""
        support["截图日期"] = []
        scores["截图日期"] = 0.0
    if time_votes:
        fields["截图时间"] = sorted(time_votes.items(), key=lambda kv: (len(kv[1]), len(kv[0])), reverse=True)[0][0]
        support["截图时间"] = time_votes[fields["截图时间"]]
        scores["截图时间"] = 0.9
    else:
        fields["截图时间"] = ""
        support["截图时间"] = []
        scores["截图时间"] = 0.0

    return {
        "fields": fields,
        "support": support,
        "scores": scores,
        "sources": sources,
        "strategy": "4ocr_weighted_consensus+harness",
    }


def _copy_dashboard_template(out_dir: Path) -> None:
    for name in ["index.html", "detail.html"]:
        src = DASHBOARD_TEMPLATE / name
        if not src.exists():
            raise FileNotFoundError(f"dashboard template missing: {src}")
        dst = out_dir / name
        shutil.copy2(src, dst)
    assets_dst = out_dir / "assets"
    if DASHBOARD_ASSETS.exists():
        shutil.copytree(DASHBOARD_ASSETS, assets_dst, dirs_exist_ok=True)


def _inject_home_link(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if "data-consensus-home-link" in text:
        return
    link = (
        '<a data-consensus-home-link href="/" '
        'style="position:fixed;right:18px;top:18px;z-index:9999;'
        'padding:10px 14px;border-radius:999px;background:#0a5748;color:#fff;'
        'text-decoration:none;font:700 13px -apple-system,BlinkMacSystemFont,'
        "'PingFang SC','Microsoft YaHei',sans-serif;box-shadow:0 12px 28px rgba(10,87,72,.24);"
        '">返回处理台</a>'
    )
    if "<body>" in text:
        text = text.replace("<body>", "<body>\n" + link, 1)
    else:
        text = link + "\n" + text
    path.write_text(text, encoding="utf-8")


def _row_id_for_detail_record(row: Dict[str, Any]) -> str:
    idx = row.get("idx")
    if isinstance(idx, int):
        return f"r_{idx}"
    return f"r_{_safe_name(str(row.get('filename', 'unknown')))}"


def _group_key_for_detail(row: Dict[str, Any]) -> str:
    file_no = str(row.get("file_no", "") or "").strip()
    file_name = str(row.get("file_name_from_filename", "") or "").strip()
    if not file_no or not file_name:
        return ""
    return f"{file_no}|||{file_name}"


def _write_detail_record_files(detail_data: Dict[str, Any], dashboard_dir: Path) -> None:
    detail_rows_dir = dashboard_dir / "detail_records"
    if detail_rows_dir.exists():
        shutil.rmtree(detail_rows_dir)
    detail_rows_dir.mkdir(parents=True, exist_ok=True)
    groups: Dict[str, List[str]] = defaultdict(list)
    for row in detail_data.get("rows", []):
        if not isinstance(row, dict):
            continue
        rid = _row_id_for_detail_record(row)
        key = _group_key_for_detail(row)
        if key:
            groups[key].append(rid)
        (detail_rows_dir / f"{rid}.js").write_text(
            "window.OCR_DETAIL_RECORD = " + json.dumps(row, ensure_ascii=False, indent=2) + ";\n",
            encoding="utf-8",
        )
    (detail_rows_dir / "groups.js").write_text(
        "window.OCR_DETAIL_GROUPS = " + json.dumps(groups, ensure_ascii=False, indent=2) + ";\n",
        encoding="utf-8",
    )


def _write_consensus_tsv(flat_records: List[Dict[str, Any]], out_path: Path) -> None:
    columns = [
        "文件名",
        "姓名",
        "身份证号",
        "上传日期",
        "上传时间",
        "相关企业数",
        "任职企业数",
        "参股企业数",
        "共识支持",
        "共识分数",
    ]
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(columns)
        for rec in flat_records:
            consensus = rec.get("weighted_consensus", {})
            fields = consensus.get("fields", {})
            support = consensus.get("support", {})
            scores = consensus.get("scores", {})
            writer.writerow(
                [
                    rec.get("filename", ""),
                    fields.get("姓名", ""),
                    fields.get("身份证号", ""),
                    fields.get("截图日期", ""),
                    fields.get("截图时间", ""),
                    fields.get("相关企业", ""),
                    fields.get("任职", ""),
                    fields.get("参股", ""),
                    json.dumps(support, ensure_ascii=False, sort_keys=True),
                    json.dumps(scores, ensure_ascii=False, sort_keys=True),
                ]
            )


def _build_dashboard(task: Dict[str, Any], flat_records: List[Dict[str, Any]]) -> str:
    task_dir = RUNS_ROOT / task["id"]
    dashboard_dir = task_dir / "dashboard"
    images_dir = dashboard_dir / "images"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    _copy_dashboard_template(dashboard_dir)
    for rec in flat_records:
        src = Path(rec["source_path"])
        if src.exists():
            shutil.copy2(src, images_dir / src.name)

    flat_path = task_dir / "raw_4way_flat.json"
    flat_path.write_text(json.dumps(flat_records, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_consensus_tsv(flat_records, task_dir / "consensus_base_info.tsv")

    builder = _load_module("ocr_dashboard_builder_task_app", DASHBOARD_BUILDER)
    data = builder._build_output(flat_records, str(flat_path), "images", include_detail=False)
    detail_data = builder._build_output(flat_records, str(flat_path), "images", include_detail=True)
    try:
        extractor = _load_module("ocr_employment_extractor_task_app", EMPLOYMENT_EXTRACTOR)
        extractor.build_employment_outputs(flat_path, task_dir / "employment_extraction", dashboard_dir)
    except Exception as exc:
        fallback = {
            "meta": {"error": str(exc), "placeholder_policy": "masked_enterprise_count_fill"},
            "candidate_rows": [],
            "image_rows": [],
            "person_rows": [],
            "reconcile_rows": [],
        }
        (dashboard_dir / "employment_data.js").write_text(
            "window.OCR_EMPLOYMENT = " + json.dumps(fallback, ensure_ascii=False, indent=2) + ";\n",
            encoding="utf-8",
        )
    data["task"] = {
        "id": task["id"],
        "name": task.get("name", ""),
        "created_at": task.get("created_at", ""),
        "strategy": "4ocr_weighted_consensus+harness",
        "consensus_tsv": "../consensus_base_info.tsv",
        "flat_json": "../raw_4way_flat.json",
    }
    detail_data["task"] = data["task"]
    payload = "window.OCR_REPORT = " + json.dumps(data, ensure_ascii=False, indent=2) + ";\n"
    (dashboard_dir / "data.js").write_text(payload, encoding="utf-8")
    detail_payload = "window.OCR_REPORT_DETAIL = " + json.dumps(detail_data, ensure_ascii=False, indent=2) + ";\n"
    (dashboard_dir / "detail_data.js").write_text(detail_payload, encoding="utf-8")
    _write_detail_record_files(detail_data, dashboard_dir)
    return f"/runs/{task['id']}/dashboard/index.html"


async def _process_task(task_id: str) -> None:
    task = TASKS[task_id]
    task_dir = RUNS_ROOT / task_id
    work_dir = task_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        _update_task(
            task_id,
            status="running",
            phase="extracting_images",
            progress=3,
            **_run_timer_start_updates(task),
        )
        image_paths = _extract_images(task)
        _raise_if_stopped(task_id)
        image_dir = Path(image_paths[0]).parent
        _update_task(task_id, files_total=len(image_paths), progress=8, phase="images_ready")

        engine_outputs: Dict[str, Path] = {}
        engine_runs: List[Dict[str, Any]] = []
        parallel_mode = os.getenv("OCR_ENGINE_PARALLEL", "aggressive").strip().lower()
        if parallel_mode == "rapid_2w":
            _update_task(task_id, phase="ocr:rapid_2w", progress=8, engine_progress={})
            boost_event = asyncio.Event()
            boost_event.set()
            paddle_tasks = []
            rapid_tasks = []

            for engine in ["paddleocr_2_7_3", "paddleocr_3_x"]:
                out = work_dir / f"{engine}.json"
                engine_outputs[engine] = out
                if _engine_output_complete(out, image_paths):
                    engine_runs.append(
                        {
                            "engine": engine,
                            "runner_engine": RUNNER_ENGINE_NAMES.get(engine, engine),
                            "python": _python_for_engine(engine),
                            "returncode": 0,
                            "elapsed_sec": 0.0,
                            "output": str(out),
                            "log": "",
                            "skipped": True,
                        }
                    )
                else:
                    paddle_tasks.append(_run_engine(task_id, engine, image_dir, out, 8, 84))

            for engine in ["rapidocr_2_7_3", "rapidocr_isolated"]:
                out = work_dir / f"{engine}.json"
                engine_outputs[engine] = out
                rapid_tasks.append(
                    asyncio.create_task(
                        _run_rapid_queue_pool(task_id, engine, image_paths, work_dir, out, boost_event, 8, 84, max_workers=2)
                    )
                )

            if paddle_tasks:
                paddle_results = await asyncio.gather(*paddle_tasks)
                engine_runs.extend(paddle_results)
            rapid_results = await asyncio.gather(*rapid_tasks)
            engine_runs.extend(rapid_results)

            _update_task(task_id, engine_runs=engine_runs, progress=84, phase="ocr:rapid_2w:done")
            for engine_result in engine_runs:
                out = Path(engine_result["output"])
                if engine_result["returncode"] != 0 or not out.exists():
                    raise RuntimeError(
                        f"{engine_result['engine']} 执行失败，returncode={engine_result['returncode']}，日志：{engine_result['log']}"
                    )
        elif parallel_mode == "tail_boost":
            _update_task(task_id, phase="ocr:tail_boost", progress=8, engine_progress={})
            boost_event = asyncio.Event()
            paddle_tasks = []
            rapid_tasks = []

            for engine in ["paddleocr_2_7_3", "paddleocr_3_x"]:
                out = work_dir / f"{engine}.json"
                engine_outputs[engine] = out
                if _engine_output_complete(out, image_paths):
                    engine_runs.append(
                        {
                            "engine": engine,
                            "runner_engine": RUNNER_ENGINE_NAMES.get(engine, engine),
                            "python": _python_for_engine(engine),
                            "returncode": 0,
                            "elapsed_sec": 0.0,
                            "output": str(out),
                            "log": "",
                            "skipped": True,
                        }
                    )
                else:
                    paddle_tasks.append(_run_engine(task_id, engine, image_dir, out, 8, 84))

            for engine in ["rapidocr_2_7_3", "rapidocr_isolated"]:
                out = work_dir / f"{engine}.json"
                engine_outputs[engine] = out
                rapid_tasks.append(
                    asyncio.create_task(
                        _run_rapid_queue_pool(task_id, engine, image_paths, work_dir, out, boost_event, 8, 84)
                    )
                )

            if paddle_tasks:
                paddle_results = await asyncio.gather(*paddle_tasks)
                engine_runs.extend(paddle_results)
            boost_event.set()
            rapid_results = await asyncio.gather(*rapid_tasks)
            engine_runs.extend(rapid_results)

            _update_task(task_id, engine_runs=engine_runs, progress=84, phase="ocr:tail_boost:done")
            for engine_result in engine_runs:
                out = Path(engine_result["output"])
                if engine_result["returncode"] != 0 or not out.exists():
                    raise RuntimeError(
                        f"{engine_result['engine']} 执行失败，returncode={engine_result['returncode']}，日志：{engine_result['log']}"
                    )
        elif parallel_mode == "aggressive":
            _update_task(task_id, phase="ocr:all_parallel", progress=8, engine_progress={})
            run_tasks = []
            for engine in METHODS:
                out = work_dir / f"{engine}.json"
                engine_outputs[engine] = out
                if _engine_output_complete(out, image_paths):
                    engine_runs.append(
                        {
                            "engine": engine,
                            "runner_engine": RUNNER_ENGINE_NAMES.get(engine, engine),
                            "python": _python_for_engine(engine),
                            "returncode": 0,
                            "elapsed_sec": 0.0,
                            "output": str(out),
                            "log": "",
                            "skipped": True,
                        }
                    )
                else:
                    run_tasks.append(_run_engine(task_id, engine, image_dir, out, 8, 84))
            if run_tasks:
                engine_runs.extend(await asyncio.gather(*run_tasks))
            _update_task(task_id, engine_runs=engine_runs, progress=84, phase="ocr:all_parallel:done")
            for engine_result in engine_runs:
                out = Path(engine_result["output"])
                if engine_result["returncode"] != 0 or not out.exists():
                    raise RuntimeError(
                        f"{engine_result['engine']} 执行失败，returncode={engine_result['returncode']}，日志：{engine_result['log']}"
                    )
        else:
            for idx, engine in enumerate(METHODS, start=1):
                phase = f"ocr:{engine}"
                _update_task(task_id, phase=phase, progress=8 + (idx - 1) * 19, current_engine=engine)
                out = work_dir / f"{engine}.json"
                engine_outputs[engine] = out
                if _engine_output_complete(out, image_paths):
                    engine_result = {
                        "engine": engine,
                        "runner_engine": RUNNER_ENGINE_NAMES.get(engine, engine),
                        "python": _python_for_engine(engine),
                        "returncode": 0,
                        "elapsed_sec": 0.0,
                        "output": str(out),
                        "log": "",
                        "skipped": True,
                    }
                else:
                    engine_result = await _run_engine(task_id, engine, image_dir, out, 8, 84)
                engine_runs.append(engine_result)
                progress = min(84, 8 + idx * 19)
                _update_task(task_id, engine_runs=engine_runs, progress=progress, phase=f"{phase}:done")
                if engine_result["returncode"] != 0 or not out.exists():
                    raise RuntimeError(
                        f"{engine} 执行失败，returncode={engine_result['returncode']}，日志：{engine_result['log']}"
                    )

        _update_task(task_id, phase="harness_parse_and_consensus", progress=88)
        _raise_if_stopped(task_id)
        flat_records = _parse_engine_records(engine_outputs, image_paths)

        _update_task(task_id, phase="building_dashboard", progress=94)
        _raise_if_stopped(task_id)
        dashboard_url = _build_dashboard(TASKS[task_id], flat_records)

        _update_task(
            task_id,
            status="completed",
            phase="completed",
            progress=100,
            completed_at=_now(),
            **_run_timer_stop_updates(task_id),
            dashboard_url=dashboard_url,
            records_total=len(flat_records),
            output_dir=str(task_dir),
        )
    except TaskStopped:
        _update_task(
            task_id,
            status="stopped",
            phase="stopped",
            stop_requested=False,
            **_run_timer_stop_updates(task_id),
            progress=min(int(TASKS[task_id].get("progress") or 0), 99),
            output_dir=str(task_dir),
        )
    except Exception as exc:
        _update_task(
            task_id,
            status="failed",
            phase="failed",
            error=str(exc),
            completed_at=_now(),
            **_run_timer_stop_updates(task_id),
            progress=min(int(TASKS[task_id].get("progress") or 0), 99),
            output_dir=str(task_dir),
        )


async def _worker() -> None:
    while True:
        task_id = await TASK_QUEUE.get()
        try:
            task = TASKS.get(task_id)
            if task and task.get("status") in {"queued", "running"}:
                await _process_task(task_id)
        finally:
            TASK_QUEUE.task_done()


def _ensure_worker() -> None:
    global WORKER_TASK
    if WORKER_TASK is None or WORKER_TASK.done():
        WORKER_TASK = asyncio.create_task(_worker())


@app.on_event("startup")
async def _startup() -> None:
    _load_existing_tasks()
    BASELINE_STORE.init_db()
    for task in sorted(TASKS.values(), key=lambda t: t.get("created_at", "")):
        if task.get("status") == "queued":
            await TASK_QUEUE.put(task["id"])
    _ensure_worker()


@app.get("/")
async def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/baselines")
async def baselines_page():
    return FileResponse(STATIC_DIR / "baselines.html")


@app.post("/api/tasks")
async def create_task(
    files: Optional[List[UploadFile]] = File(default=None),
    file: Optional[UploadFile] = File(default=None),
    task_name: str = Form(default=""),
):
    upload_files = list(files or [])
    if file is not None:
        upload_files.append(file)
    upload_files = [f for f in upload_files if f and f.filename]
    if not upload_files:
        raise HTTPException(status_code=400, detail="请上传图片或 zip 压缩包")

    task_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    task_dir = RUNS_ROOT / task_id
    input_dir = task_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    input_paths: List[str] = []
    input_size = 0
    original_names: List[str] = []
    for item in upload_files:
        original_name = _safe_name(item.filename or "upload")
        suffix = Path(original_name).suffix.lower()
        if suffix not in IMAGE_EXTS and suffix != ".zip":
            raise HTTPException(status_code=400, detail=f"不支持的文件类型：{original_name}")
        input_path = _unique_dest(input_dir, original_name)
        content = await item.read()
        if not content:
            raise HTTPException(status_code=400, detail=f"上传文件为空：{original_name}")
        input_path.write_bytes(content)
        input_paths.append(str(input_path))
        input_size += len(content)
        original_names.append(original_name)

    custom_name = _safe_name(task_name)
    display_name = custom_name if custom_name and custom_name != "upload" else (
        original_names[0] if len(original_names) == 1 else f"{len(original_names)} files: {original_names[0]} ..."
    )

    task = {
        "id": task_id,
        "name": display_name,
        "status": "queued",
        "phase": "queued",
        "progress": 0,
        "created_at": _now(),
        "updated_at": _now(),
        "input_path": input_paths[0],
        "input_paths": input_paths,
        "input_files": original_names,
        "input_size": input_size,
        "files_total": 0,
        "records_total": 0,
        "engine_runs": [],
        "dashboard_url": "",
        "error": "",
        "run_elapsed_ms": 0,
        "run_active_since_ms": 0,
    }
    _save_task(task)
    await TASK_QUEUE.put(task_id)
    _ensure_worker()
    return JSONResponse(task)


@app.get("/api/tasks")
async def list_tasks():
    rows = sorted(TASKS.values(), key=lambda t: t.get("created_at", ""), reverse=True)
    return {"tasks": rows, "queue_size": TASK_QUEUE.qsize()}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@app.post("/api/tasks/{task_id}/baseline")
async def save_task_as_baseline(task_id: str, payload: Optional[Dict[str, Any]] = Body(default=None)):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    payload = payload or {}
    try:
        baseline = BASELINE_STORE.create_from_task(
            task,
            name=str(payload.get("name") or "").strip(),
            baseline_type=str(payload.get("type") or "snapshot").strip() or "snapshot",
            notes=str(payload.get("notes") or "").strip(),
            created_by=str(payload.get("created_by") or "").strip(),
        )
    except BaselineError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "baseline": baseline}


@app.get("/api/baselines")
async def list_baselines():
    return {"baselines": BASELINE_STORE.list_baselines()}


@app.get("/api/baselines/{baseline_id}")
async def get_baseline(baseline_id: str):
    try:
        return BASELINE_STORE.get_baseline(baseline_id)
    except BaselineError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.get("status") == "running":
        raise HTTPException(status_code=409, detail="运行中的任务暂不支持删除")
    TASKS.pop(task_id, None)
    task_dir = RUNS_ROOT / task_id
    if task_dir.exists():
        shutil.rmtree(task_dir)
    return {"ok": True, "id": task_id}


@app.post("/api/tasks/{task_id}/stop")
async def stop_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.get("status") in {"completed", "stopped"}:
        return {"ok": True, "id": task_id, "status": task.get("status")}
    if task.get("status") == "running":
        _update_task(
            task_id,
            stop_requested=True,
            status="stopped",
            phase="stopping",
            **_run_timer_stop_updates(task_id),
        )
        await _terminate_task_procs(task_id)
    elif task.get("status") == "queued":
        _update_task(task_id, stop_requested=False, status="stopped", phase="stopped")
    else:
        _update_task(task_id, stop_requested=False, status="stopped", phase="stopped")
    return {"ok": True, "id": task_id, "status": TASKS[task_id].get("status")}


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.get("status") in {"queued", "running"}:
        return {"ok": True, "id": task_id, "status": task.get("status")}
    if task.get("status") == "completed":
        raise HTTPException(status_code=409, detail="已完成任务不需要继续")
    _update_task(
        task_id,
        status="queued",
        phase="queued",
        stop_requested=False,
        resumed=True,
        error="",
        run_active_since_ms=0,
    )
    await TASK_QUEUE.put(task_id)
    _ensure_worker()
    return {"ok": True, "id": task_id, "status": "queued"}


@app.get("/tasks/{task_id}/dashboard")
async def open_dashboard(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    url = task.get("dashboard_url")
    if not url:
        raise HTTPException(status_code=409, detail="dashboard not ready")
    return RedirectResponse(url)
