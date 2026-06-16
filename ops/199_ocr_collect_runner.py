#!/usr/bin/env python3
import argparse
import json
import os
import time
from pathlib import Path
from typing import List, Tuple

ImagePath = Path


def list_images(img_dir: str) -> List[ImagePath]:
    p = Path(img_dir)
    exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    files = [
        x
        for x in p.rglob("*")
        if x.is_file()
        and x.suffix.lower() in exts
        and not x.name.startswith(".")
        and not x.name.startswith("._")
    ]
    return sorted(files)


def to_record(path: ImagePath, text: str, lines: list, elapsed_ms: float, error: str = "") -> dict:
    return {
        "filename": str(path),
        "text": text,
        "lines": lines,
        "avg_confidence": round((sum(float(x.get("confidence", 0.0)) for x in lines) / len(lines)) if lines else 0.0, 6),
        "time_ms": round(float(elapsed_ms), 6),
        "error": error,
    }


class RapidEngine:
    def __init__(self, py: str = ""):
        self.py = py
        from rapidocr_onnxruntime import RapidOCR

        self._engine = RapidOCR()

    def infer(self, path: Path) -> Tuple[str, list]:
        out = self._engine(str(path))
        if out is None:
            return "", []

        if not isinstance(out, (list, tuple)):
            return "", []

        # rapidocr_onnxruntime 常见两种返回形态：
        # 1) ([detections], [cost_times])
        # 2) (code, [detections])
        infos = []
        code = 0

        if len(out) == 2 and isinstance(out[0], (str, int)):
            code, infos = out
        else:
            infos = out[0]

        # 兼容 (list, times) 情况
        if isinstance(infos, (list, tuple)) and all(isinstance(v, (float, int)) for v in infos):
            return "", []

        if not infos:
            return "", []

        if code not in (None, 0, "0", []):
            return "", []

        text_lines = []
        for item in infos:
            if not item:
                continue
            try:
                _bbox, text, conf = item[0], item[1], item[2]
                text_lines.append({"text": str(text), "confidence": float(conf)})
            except Exception:
                continue
        return "\n".join(x["text"] for x in text_lines), text_lines


class Paddle2Engine:
    def __init__(self, use_gpu: bool = True):
        from paddleocr import PaddleOCR

        self._engine = PaddleOCR(use_angle_cls=True, lang="ch", use_gpu=use_gpu, show_log=False)

    def infer(self, path: Path) -> Tuple[str, list]:
        result = self._engine.ocr(str(path), cls=True)
        text_lines = []
        if not result:
            return "", []
        first = result[0] if isinstance(result, list) and result else result
        if not first:
            return "", []
        for line in first:
            if not line or len(line) < 2:
                continue
            try:
                bbox, payload = line
                txt, conf = payload
                text_lines.append({
                    "text": str(txt),
                    "confidence": float(conf),
                    "bbox": bbox,
                })
            except Exception:
                continue
        return "\n".join(x["text"] for x in text_lines), text_lines


class Paddle3Engine:
    def __init__(self, device: str = "gpu"):
        from paddleocr import PaddleOCR
        try:
            import paddle
            device_type = str(device)
            if device_type == "gpu":
                try:
                    if not bool(paddle.base.core.is_compiled_with_cuda()):
                        device_type = "cpu"
                except Exception:
                    device_type = "cpu"
        except Exception:
            device_type = device

        self._engine = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            lang="ch",
            device=device_type,
            enable_mkldnn=False,
            enable_cinn=False,
            engine="paddle_static",
        )

    def infer(self, path: Path) -> Tuple[str, list]:
        result = self._engine.predict(str(path))
        text_lines = []
        for item in result or []:
            payload = item.json if hasattr(item, "json") else item
            if isinstance(payload, dict) and isinstance(payload.get("res"), dict):
                payload = payload["res"]

            if isinstance(payload, dict):
                rec_texts = payload.get("rec_texts") or []
                rec_scores = payload.get("rec_scores") or []
            else:
                rec_texts = getattr(payload, "rec_texts", [])
                rec_scores = getattr(payload, "rec_scores", [])

            if not rec_texts:
                continue

            if rec_scores is None:
                rec_scores = []

            for txt, conf in zip(rec_texts, rec_scores):
                if txt is None:
                    continue
                text_lines.append({
                    "text": str(txt),
                    "confidence": float(conf) if conf is not None else 0.0,
                })
        return "\n".join(x["text"] for x in text_lines), text_lines


def run_for_engine(engine_name: str, img_dir: str, output: str, max_images: int = 0, use_gpu: bool = True):
    if engine_name == "rapidocr_current":
        engine = RapidEngine()
    elif engine_name == "rapidocr_isolated":
        engine = RapidEngine()
    elif engine_name == "paddleocr_2_7_3":
        engine = Paddle2Engine(use_gpu=use_gpu)
    elif engine_name == "paddleocr_3_x":
        engine = Paddle3Engine(device="gpu" if use_gpu else "cpu")
    else:
        raise ValueError(f"Unknown engine: {engine_name}")

    files = list_images(img_dir)
    if max_images and max_images > 0:
        files = files[:max_images]

    records = []
    total = len(files)
    for idx, path in enumerate(files, start=1):
        print(
            "OCR_PROGRESS "
            + json.dumps(
                {
                    "engine": engine_name,
                    "stage": "start",
                    "index": idx,
                    "total": total,
                    "filename": Path(path).name,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        start = time.perf_counter()
        try:
            text, lines = engine.infer(path)
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            records.append(to_record(path, text, lines, elapsed_ms, ""))
            print(
                "OCR_PROGRESS "
                + json.dumps(
                    {
                        "engine": engine_name,
                        "stage": "done",
                        "index": idx,
                        "total": total,
                        "filename": Path(path).name,
                        "elapsed_ms": elapsed_ms,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            records.append(to_record(path, "", [], elapsed_ms, str(e)))
            print(
                "OCR_PROGRESS "
                + json.dumps(
                    {
                        "engine": engine_name,
                        "stage": "error",
                        "index": idx,
                        "total": total,
                        "filename": Path(path).name,
                        "elapsed_ms": elapsed_ms,
                        "error": str(e),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    return len(records)


def claim_next(queue_dir: Path) -> Tuple[Path, dict]:
    pending_dir = queue_dir / "pending"
    processing_dir = queue_dir / "processing"
    processing_dir.mkdir(parents=True, exist_ok=True)
    for pending in sorted(pending_dir.glob("*.json")):
        claimed = processing_dir / f"{pending.stem}.{os.getpid()}.json"
        try:
            os.replace(pending, claimed)
        except FileNotFoundError:
            continue
        except OSError:
            continue
        try:
            return claimed, json.loads(claimed.read_text(encoding="utf-8"))
        except Exception:
            done_dir = queue_dir / "done"
            done_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(claimed, done_dir / claimed.name)
            except OSError:
                pass
            continue
    return Path(), {}


def run_queue_for_engine(
    engine_name: str,
    queue_dir: str,
    output: str,
    result_dir: str = "",
    queue_total: int = 0,
    use_gpu: bool = True,
):
    if engine_name == "rapidocr_current":
        engine = RapidEngine()
    elif engine_name == "rapidocr_isolated":
        engine = RapidEngine()
    elif engine_name == "paddleocr_2_7_3":
        engine = Paddle2Engine(use_gpu=use_gpu)
    elif engine_name == "paddleocr_3_x":
        engine = Paddle3Engine(device="gpu" if use_gpu else "cpu")
    else:
        raise ValueError(f"Unknown engine: {engine_name}")

    qdir = Path(queue_dir)
    rdir = Path(result_dir) if result_dir else Path(output).parent / (Path(output).stem + "_results")
    done_dir = qdir / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    rdir.mkdir(parents=True, exist_ok=True)
    records = []
    total = int(queue_total or 0)
    processed = 0

    while True:
        claimed, payload = claim_next(qdir)
        if not payload:
            break
        path = Path(str(payload.get("path") or ""))
        filename = str(payload.get("filename") or path.name)
        seq = int(payload.get("seq") or processed)
        processed += 1
        print(
            "OCR_PROGRESS "
            + json.dumps(
                {
                    "engine": engine_name,
                    "stage": "start",
                    "index": processed,
                    "total": total,
                    "filename": filename,
                    "worker_pid": os.getpid(),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        start = time.perf_counter()
        try:
            text, lines = engine.infer(path)
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            record = to_record(path, text, lines, elapsed_ms, "")
            records.append(record)
            stage = "done"
            error = ""
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            record = to_record(path, "", [], elapsed_ms, str(e))
            records.append(record)
            stage = "error"
            error = str(e)
        result_path = rdir / f"{seq:06d}.json"
        tmp_path = result_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, result_path)
        print(
            "OCR_PROGRESS "
            + json.dumps(
                {
                    "engine": engine_name,
                    "stage": stage,
                    "index": processed,
                    "total": total,
                    "filename": filename,
                    "elapsed_ms": elapsed_ms,
                    "error": error,
                    "worker_pid": os.getpid(),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        try:
            os.replace(claimed, done_dir / claimed.name)
        except OSError:
            pass

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    return len(records)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True)
    parser.add_argument("--img-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-images", type=int, default=0)
    parser.add_argument("--no-gpu", action="store_true")
    parser.add_argument("--queue-dir", default="")
    parser.add_argument("--result-dir", default="")
    parser.add_argument("--queue-total", type=int, default=0)
    args = parser.parse_args()

    if args.queue_dir:
        n = run_queue_for_engine(
            args.engine,
            args.queue_dir,
            args.output,
            result_dir=args.result_dir,
            queue_total=args.queue_total,
            use_gpu=not args.no_gpu,
        )
    else:
        n = run_for_engine(
            args.engine,
            args.img_dir,
            args.output,
            max_images=args.max_images,
            use_gpu=not args.no_gpu,
        )
    print(f"done {n} images -> {args.output}")


if __name__ == "__main__":
    main()
