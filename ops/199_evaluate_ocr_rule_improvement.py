#!/usr/bin/env python3
"""规则增量评估脚本（199 全量 698 张）

功能：
1. 从四路 OCR raw JSON 中用当前 business_check 规则重建结构化字段。
2. 与历史基线 flat JSON 进行逐字段 A/B 对账。
3. 产出：
   - 新版 flat（与 baseline schema 兼容）
   - 逐字段变更明细（按图片+方案）
   - 每个方案字段级提升率/回退率汇总
   - 新旧方案基线对账（共识与单字段支持引擎）

输入要求：
- raw JSON（四路）需是 list，每条包含 filename/file、text、lines、time_ms、error。
- 基线 flat 为空时可设为 "": 将只生成新 flat，不做 A/B 对比。
"""
import argparse
import csv
import json
import re
import sys
from statistics import mean
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from scenes.business_check import BusinessCheckScene

METHODS = [
    "paddleocr_2_7_3",
    "rapidocr_2_7_3",
    "rapidocr_isolated",
    "paddleocr_3_x",
]

RAW_FILE_MAP_DEFAULT = {
    "rapidocr_2_7_3": "rapidocr_2_7_3_report.json",
    "rapidocr_isolated": "rapidocr_isolated_report.json",
    "paddleocr_2_7_3": "paddleocr_2_7_3_report.json",
    "paddleocr_3_x": "paddleocr_3_x_report.json",
}

BASE_FIELDS = ["姓名", "身份证号", "相关企业", "任职", "参股"]
def _norm_text(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _norm_count(v: Any) -> str:
    s = _norm_text(v)
    if not s:
        return ""
    m = re.search(r"\d+", s)
    return m.group(0) if m else ""


def _to_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        if not (f == f):
            return None
        return f
    except Exception:
        return None


def _clamp01(v: float) -> float:
    if v != v:
        return 0.0
    if v < 0:
        return 0.0
    if v > 1:
        return 1.0
    return v


def _line_norm_text(v: Any) -> str:
    s = _norm_text(v)
    # 常见全角符号标准化，避免 OCR 将分隔符读成中文标点。
    repl = {
        "：": ":",
        "﹕": ":",
        "／": "/",
        "，": ",",
        "。": ".",
        "—": "-",
        "–": "-",
        "−": "-",
        "Ｏ": "0",
        "o": "0",
        "O": "0",
        "Ｉ": "1",
        "ｌ": "1",
        "I": "1",
        "丨": "|",
        "│": "|",
        "｜": "|",
    }
    for src, dst in repl.items():
        s = s.replace(src, dst)
    return re.sub(r"\s+", "", s).upper()


def _normalize_rule_meta(meta: Any) -> Dict[str, Any]:
    if isinstance(meta, dict):
        src = _norm_text(meta.get("source"))
        rules = meta.get("rules")
        if isinstance(rules, (list, tuple)):
            rules = [str(r) for r in rules if str(r).strip()]
        else:
            rules = [str(rules)] if rules not in (None, "") else []
        rule_processed = meta.get("rule_processed")
        return {
            "source": src,
            "rules": rules,
            "rule_processed": bool(rule_processed),
            "rule_processed_value": 1.0 if bool(rule_processed) else 0.0,
        }
    return {"source": "", "rules": [], "rule_processed": False, "rule_processed_value": 0.0}


def _rule_quality_score(rule_meta: Dict[str, Any]) -> float:
    src = str(rule_meta.get("source", "")).strip().lower()
    rules = [str(r).strip().lower() for r in rule_meta.get("rules", []) if str(r).strip()]
    rules_set = set(rules)
    processed = bool(rule_meta.get("rule_processed"))

    if src == "missing":
        return 0.30
    if src in {"raw_line", "ocr_line"}:
        return 1.0
    if src == "line_fusion_rule":
        return 0.95
    if src in {"count_rule_match", "count_field_line_pattern"}:
        return 0.96
    if src in {"count_rule_nomatch", "count_rule_none"}:
        return 0.78
    if src == "no_result_forced_zero":
        return 0.70
    if src == "id_pattern_reconstruct":
        return 0.82
    if src == "employment_merge":
        return 0.82
    if src == "conclusion_line_scan":
        return 0.84
    if src == "filename_fallback":
        return 0.92
    if "filename" in src:
        return 0.90
    if any(tag.startswith("id_") for tag in rules_set):
        return 0.82
    if any(tag.endswith("line_pattern") for tag in rules_set):
        return 0.88
    if processed:
        return 0.85
    return 1.0


def _is_subsequence(pattern: str, text: str) -> bool:
    if not pattern:
        return False
    pi = 0
    for ch in text:
        if pi >= len(pattern):
            return True
        p = pattern[pi]
        if p == "X":
            pi += 1
        elif ch == p:
            pi += 1
    return pi == len(pattern)


def _subseq_after_anchor(text: str, head: str, tail: str, max_gap: int | None = None) -> bool:
    if not head or not tail:
        return False
    i = text.find(head)
    if i < 0:
        return False
    if max_gap is not None:
        # 约束尾号相对头码距离，减少误匹配
        # 身份证字段常见形态下，头尾中间不应异常离散。
        if len(text) - (i + len(head)) > max_gap:
            # 仍给一次机会：目标头码再出现在后续位置
            i = text.find(head, i + 1)
            if i < 0 or len(text) - (i + len(head)) > max_gap:
                return False
    return _is_subsequence(tail, text[i + len(head):])


def _ordered_seg_match(text: str, segs: List[str]) -> bool:
    pos = 0
    for seg in segs:
        p = text.find(seg, pos)
        if p < 0:
            return False
        pos = p + len(seg)
    return True


def _digits_only(s: str) -> str:
    return re.sub(r"[^0-9]", "", s)


def _contains_date_like_match(target: str, text: str) -> bool:
    t = _line_norm_text(target)
    if not t:
        return False
    t_digits = _digits_only(t)

    # 常见“07-15”/“7-15”/“0715”等形态都兜底。
    if not t_digits:
        return False
    candidates = set()
    candidates.add(t_digits)
    if len(t_digits) == 3:
        candidates.add(f"0{t_digits}")
    if len(t_digits) == 2 and int(t_digits) < 32:
        # 处理异常输入“7-”一类的退化值
        candidates.add(f"0{t_digits}")
    if len(t_digits) == 5:
        candidates.add(f"{t_digits[:2]}{t_digits[3:]}")
        candidates.add(f"0{t_digits[3:]}")
        candidates.add(f"{t_digits[:2]}{t_digits[3:]}")

    s_digits = _digits_only(_line_norm_text(text))
    if not s_digits:
        return False
    for c in candidates:
        if c and c in s_digits:
            return True

    # 宽松匹配：支持“070922”“07-2210:09”这类日期挤在一串数字里
    compact = _digits_only(_line_norm_text(text))
    for c in candidates:
        if c and compact.startswith(c[:2]) and compact[2:].startswith(c[2:]):
            return True
    return False


def _contains_time_like_match(target: str, text: str) -> bool:
    t = _line_norm_text(target)
    if not t:
        return False
    t_digits = _digits_only(t)
    if not t_digits:
        return False
    # 常见“2:34”这种漏掉前导零
    candidates = {t_digits}
    if len(t_digits) == 3:
        candidates.add(f"0{t_digits}")
    if len(t_digits) == 4 and t_digits.startswith("0"):
        candidates.add(t_digits[1:])
    if len(t_digits) == 2 and int(t_digits) < 60:
        candidates.add(f"00{t_digits}")
    elif len(t_digits) > 4:
        candidates.discard(t_digits)
        candidates.add(t_digits[:2] + t_digits[-2:])

    # HH:mm，目标可能是 1717 或 17:17
    norm = _line_norm_text(text)
    compact = _digits_only(norm)
    for c in candidates:
        if c and c in compact:
            return True
        if re.search(rf"{re.escape(c[:2])}[:\s]*{re.escape(c[2:])}", norm):
            return True
    return False


def _contains_id_match(target: str, text: str) -> bool:
    t = _line_norm_text(target).upper()
    s = _line_norm_text(text).upper()
    if not t or not s:
        return False
    if t in s:
        return True

    # 先做无星号/无分隔符的数字命中兜底（用于OCR把*误识别掉的情况）
    t_digits = re.sub(r"[^0-9X]", "", t)
    s_digits = re.sub(r"[^0-9X]", "", s)
    if t_digits and len(t_digits) >= 10:
        if t_digits in s_digits:
            return True

    if "*" not in t:
        return False

    # 身份证号有“前缀*后缀”结构时，用片段顺序匹配；解决星号个数变化/截断问题。
    segs = [seg for seg in re.split(r"\*+", t) if seg]
    if not segs:
        return False

    if _ordered_seg_match(s, segs):
        return True

    # OCR 常会把部分星号读成“*”或把位点切碎为单字符，需要更强的锚定匹配。
    t_no_sep = re.sub(r"[^0-9X*]", "", t)
    s_no_sep = re.sub(r"[^0-9X*]", "", s)
    if t_no_sep and s_no_sep:
        # 统一压缩连续星号，减少无意义差异
        t_norm = re.sub(r"\*+", "*", t_no_sep)
        s_norm = re.sub(r"\*+", "*", s_no_sep)
        if t_norm in s_norm:
            return True

        # 用首末码锚点 + 子序列匹配，处理“320********7*027”这类尾号抖动。
        t_vis = re.sub(r"[^0-9X]", "", t_norm)
        if len(t_vis) >= 7:
            if t in s_no_sep:
                return True
            if _is_subsequence(t_vis, s_no_sep):
                return True
            head = t_vis[:3]
            tail = t_vis[-4:] if len(t_vis) >= 4 else t_vis
            if head and tail and _subseq_after_anchor(s_no_sep, head, tail, max_gap=28):
                return True

    # 少数情况下出现“尾号先读到前面/倒序拼接”时兜底
    if len(segs) >= 2:
        if _ordered_seg_match(s, list(reversed(segs))):
            return True

    # 忽略*，按“数字片段+字符片段”做相似性兜底
    if len(segs) == 2:
        head, tail = segs[0], segs[1]
        if head and tail and head in s and tail in s:
            return True

    # 先做“无空格/无分隔符”版本匹配，规避 OCR 把中间字符读成不同符号的情况。
    t_compact = re.sub(r"[^0-9X*]", "", t)
    s_compact = re.sub(r"[^0-9X*]", "", s)
    if t_compact and t_compact in s_compact:
        return True

    if "*" in t_compact and len(t_compact) >= 10:
        c_segs = [seg for seg in re.split(r"\*+", t_compact) if seg]
        if c_segs:
            if _ordered_seg_match(s_compact, c_segs):
                return True
            if len(c_segs) >= 2 and _ordered_seg_match(s_compact, list(reversed(c_segs))):
                return True
            if len(c_segs) >= 2 and c_segs[0] in s_compact and c_segs[-1] in s_compact:
                return True

    return False


def _collect_line_confidences(lines: Any) -> Tuple[List[Tuple[str, float, int]], Dict[str, float]]:
    if not isinstance(lines, list):
        return [], {}
    pairs: List[Tuple[str, float, int]] = []
    for idx, ln in enumerate(lines):
        if not isinstance(ln, dict):
            continue
        conf = _to_float(ln.get("confidence"))
        if conf is None:
            conf = _to_float(ln.get("score"))
        if conf is None:
            continue
        text = _line_norm_text(ln.get("text", ""))
        pairs.append((text, conf, idx))
    if not pairs:
        return [], {}
    values = [c for _, c, _ in pairs]
    stats = {
        "avg_confidence": mean(values),
        "min_confidence": min(values),
        "max_confidence": max(values),
        "line_confidence_count": len(values),
    }
    return pairs, stats


def _field_confidence(
    field_name: str,
    field_value: Any,
    line_pairs: List[Tuple[str, float, int]],
    rule_meta: Optional[Dict[str, Any]] = None,
) -> float | None:
    if not field_name or field_value is None:
        return None
    target = _norm_text(field_value)
    if not target:
        return None
    norm_target = _line_norm_text(target)
    if not line_pairs:
        return None

    candidates: List[float] = []
    if field_name in {"身份证号"}:
        # 身份证号先尝试行内/跨行直接命中
        for text, conf, idx in line_pairs:
            if not text:
                continue
            if _contains_id_match(norm_target, text):
                candidates.append(conf)

            # 跨行拼接兜底（例如“142”与“********7593”分在两行）
            if idx + 1 < len(line_pairs):
                next_text, next_conf, _ = line_pairs[idx + 1]
                if next_text and _contains_id_match(norm_target, text + next_text):
                    candidates.append(max(conf, next_conf))
            if idx - 1 >= 0:
                prev_text, prev_conf, _ = line_pairs[idx - 1]
                if prev_text and _contains_id_match(norm_target, prev_text + text):
                    candidates.append(max(conf, prev_conf))

        # 全文兜底（OCR 解析到空格/断字符号导致行级失败）
        if not candidates and line_pairs:
            joined = "".join(t for t, _, _ in line_pairs)
            if _contains_id_match(norm_target, joined):
                candidates.append(max((c for _, c, _ in line_pairs), default=0.0))

    if candidates:
        base_conf = max(candidates)
        return _clamp01(base_conf * _rule_quality_score(rule_meta or {}))

    if field_name in {"截图日期", "截图时间"}:
        matcher = _contains_date_like_match if field_name == "截图日期" else _contains_time_like_match
        for text, conf, idx in line_pairs:
            if not text:
                continue
            if matcher(norm_target, text):
                candidates.append(conf)
                continue
            if idx + 1 < len(line_pairs):
                next_text, next_conf, _ = line_pairs[idx + 1]
                if matcher(norm_target, text + next_text):
                    candidates.append(max(conf, next_conf))
            if idx - 1 >= 0:
                prev_text, prev_conf, _ = line_pairs[idx - 1]
                if matcher(norm_target, prev_text + text):
                    candidates.append(max(conf, prev_conf))
        if not candidates and line_pairs:
            joined = "".join(t for t, _, _ in line_pairs)
            if matcher(norm_target, joined):
                candidates.append(max((c for _, c, _ in line_pairs), default=0.0))
        if candidates:
            return max(candidates)

    for text, conf, _ in line_pairs:
        if not text:
            continue
        if field_name in {"身份证号"} and _contains_id_match(norm_target, text):
            candidates.append(conf)
            continue
        if norm_target in text:
            candidates.append(conf)
            continue

        # 字段附近匹配，兼容“相关企业0/任职0/参股0”一行场景
        marker = re.sub(r"\s+", "", field_name)
        if marker and marker in text and _line_norm_text(_norm_text(field_value)) in text:
            candidates.append(conf)
            continue

        if field_name in ["相关企业", "任职", "参股"]:
            m = re.search(rf"{re.escape(marker)}(?:[:：]?)?(\d+)", text)
            if m and m.group(1) == _line_norm_text(_norm_text(_norm_count(field_value))):
                candidates.append(conf)

    if not candidates:
        return None
    # 取最高置信度作为字段证据
    return _clamp01(max(candidates) * _rule_quality_score(rule_meta or {}))


def _to_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _parse_file_meta(filename: str) -> Tuple[str, str]:
    stem = Path(filename).name.rsplit(".", 1)[0]
    file_id = ""
    file_name = ""

    stem = stem.strip()
    if not stem:
        return "", ""

    noise = {
        "无经商办企业",
        "无经商办企",
        "有经商办企业",
        "经商办企业",
        "投资任职信息查询",
        "投资任职情况查询",
    }

    m = re.match(r"^(\d{6,20})(?=\D|$)", stem)
    if m:
        file_id = m.group(1)
    else:
        end_num = re.search(r"(?:^|[-_\s])(\d{6,20})(?:$|[-_\s])", stem)
        if end_num:
            file_id = end_num.group(1)

    candidates = []
    for seg in re.split(r"[-_\s]+", stem):
        seg = seg.strip()
        if not seg:
            continue
        if seg in noise:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]{2,8}", seg):
            candidates.append(seg)
        m2 = re.match(r"^\d+([\u4e00-\u9fff]{2,8})$", seg)
        if m2:
            candidates.append(m2.group(1))
        if seg.startswith("投资任职信息") and len(seg) > 5:
            tail = seg[5:]
            if re.fullmatch(r"[\u4e00-\u9fff]{2,8}", tail):
                candidates.append(tail)

    if not file_name:
        candidates = re.findall(r"[\u4e00-\u9fff]{2,8}", stem)

    for name in candidates:
        if name in noise:
            continue
        if 2 <= len(name) <= 8:
            file_name = name
            break

    return file_id, file_name


def _read_json_list(p: Path) -> List[dict]:
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{p} is not a JSON array")
    return data


def _load_raw_records(path: Path) -> Dict[str, dict]:
    by_file: Dict[str, dict] = {}
    for row in _read_json_list(path):
        if not isinstance(row, dict):
            continue
        filename = _norm_text(row.get("filename") or row.get("file"))
        if not filename:
            continue
        by_file[Path(filename).name] = row
    return by_file


def _collect_method_raw(input_dir: Path) -> Dict[str, Dict[str, dict]]:
    result: Dict[str, Dict[str, dict]] = {}
    for method, fname in RAW_FILE_MAP_DEFAULT.items():
        p = input_dir / fname
        if not p.exists():
            result[method] = {}
            continue
        result[method] = _load_raw_records(p)
    return result


def _load_flat_records(p: Path) -> Dict[str, dict]:
    if not p.exists():
        return {}
    data = _read_json_list(p)
    by_file: Dict[str, dict] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        by_file[_norm_text(row.get("filename")).split("/")[-1]] = row
    return by_file


def _parse_record(parser: BusinessCheckScene, rec: dict) -> dict:
    if not rec:
        return {
            "姓名": "",
            "身份证号": "",
            "相关企业": 0,
            "任职": 0,
            "参股": 0,
            "查询结论": "",
            "任职情况信息": "",
            "原始OCR": "",
            "error": "raw_record_missing",
            "time_ms": 0.0,
            "line_count": 0,
            "method_score": {
                "avg_confidence": 0.0,
                "min_confidence": 0.0,
                "max_confidence": 0.0,
                "line_confidence_count": 0,
                "raw_avg_confidence": 0.0,
            },
            "field_scores": {},
        }
    raw = {
        "filename": _norm_text(rec.get("filename") or rec.get("file")),
        "text": _norm_text(rec.get("text")),
        "lines": rec.get("lines") if isinstance(rec.get("lines"), list) else [],
    }
    parsed = parser.parse(raw)
    parsed = dict(parsed or {})
    line_pairs, line_stats = _collect_line_confidences(rec.get("lines"))
    provenance = parsed.get("field_provenance", {})
    if not isinstance(provenance, dict):
        provenance = {}
    field_score_fields = [
        "姓名",
        "身份证号",
        "相关企业",
        "任职",
        "参股",
        "截图日期",
        "截图时间",
    ]
    field_scores: Dict[str, Dict[str, Any]] = {}
    for fname in field_score_fields:
        val = _to_str(parsed.get(fname, ""))
        prov = _normalize_rule_meta(provenance.get(fname))
        score = _field_confidence(fname, val, line_pairs, prov)
        if score is not None:
            field_scores[fname] = {
                "value": val,
                "confidence": round(score, 4),
                "provenance": prov,
                "rule_processed": prov["rule_processed"],
                "rule_source": prov["source"],
                "rule_tags": "|".join(prov["rules"]),
            }
    out = {
        "姓名": _to_str(parsed.get("姓名", "")),
        "身份证号": _to_str(parsed.get("身份证号", "")),
        "相关企业": _to_str(parsed.get("相关企业", "")),
        "任职": _to_str(parsed.get("任职", "")),
        "参股": _to_str(parsed.get("参股", "")),
        "截图日期": _to_str(parsed.get("截图日期", "")),
        "截图时间": _to_str(parsed.get("截图时间", "")),
        "查询结论": _to_str(parsed.get("查询结论", "")),
        "任职情况信息": _to_str(parsed.get("任职情况信息", "")),
        "原始OCR": _to_str(parsed.get("原始OCR", "")),
        "error": _norm_text(parsed.get("parse_error") or rec.get("error", "")),
        "time_ms": float(rec.get("time_ms", 0.0) or 0.0),
        "line_count": len(raw.get("lines") or []),
        "method_score": {
            "avg_confidence": _to_float(line_stats.get("avg_confidence")) or _to_float(rec.get("avg_confidence")) or 0.0,
            "min_confidence": _to_float(line_stats.get("min_confidence")) or 0.0,
            "max_confidence": _to_float(line_stats.get("max_confidence")) or 0.0,
            "line_confidence_count": int(line_stats.get("line_confidence_count", 0) or 0),
            "raw_avg_confidence": _to_float(rec.get("avg_confidence")) or 0.0,
        },
        "field_scores": field_scores,
        "field_provenance": {
            k: _normalize_rule_meta(v) for k, v in provenance.items()
        },
    }
    return out


def _rebuild_flat(method_raw: Dict[str, Dict[str, dict]]) -> Dict[str, dict]:
    parser = BusinessCheckScene()
    all_files = set()
    for m in METHODS:
        all_files.update(method_raw.get(m, {}).keys())
    parser_results: Dict[str, dict] = {}

    for fn in sorted(all_files):
        method_results: Dict[str, dict] = {}
        for m in METHODS:
            method_results[m] = _parse_record(parser, method_raw.get(m, {}).get(fn))
        parser_results[fn] = {
            "filename": fn,
            "methods": method_results,
            "field_stats": {},
            "score": {},
            "issues": [],
        }
    return parser_results


def _consensus(values: Dict[str, str], normalizer=_norm_text) -> Tuple[str, List[str]]:
    votes = defaultdict(list)
    for method, value in values.items():
        value = _norm_text(normalizer(value))
        if value == "":
            continue
        votes[value].append(method)
    if not votes:
        return "", []
    picked = sorted(votes.items(), key=lambda kv: (len(kv[1]), kv[0]), reverse=True)[0]
    return picked[0], picked[1]


def _build_baseline_rows(flat: Dict[str, dict], label: str):
    rows = []
    for fn, rec in sorted(flat.items()):
        file_no, file_name = _parse_file_meta(fn)
        methods = rec.get("methods", {})
        base_vals = {}
        for field in BASE_FIELDS + ["查询结论"]:
            base_vals[field] = {m: _norm_text((methods.get(m, {}).get(field, ""))) for m in METHODS}
        row = {
            "文件名": fn,
            "文件编号": file_no,
            "文件名中的姓名": file_name,
            "姓名_共识": "",
            "姓名_支持引擎": "",
            "身份证号_共识": "",
            "身份证号_支持引擎": "",
            "相关企业_共识": "",
            "相关企业_支持引擎": "",
            "任职_共识": "",
            "任职_支持引擎": "",
            "参股_共识": "",
            "参股_支持引擎": "",
            "上传日期": "",
            "上传时间": "",
        }

        name_val, name_sup = _consensus({k: base_vals["姓名"][k] for k in METHODS}, _norm_text)
        id_val, id_sup = _consensus({k: base_vals["身份证号"][k] for k in METHODS}, _norm_text)
        rel_val, rel_sup = _consensus({k: base_vals["相关企业"][k] for k in METHODS}, _norm_count)
        job_val, job_sup = _consensus({k: base_vals["任职"][k] for k in METHODS}, _norm_count)
        share_val, share_sup = _consensus({k: base_vals["参股"][k] for k in METHODS}, _norm_count)

        row.update({
            "姓名_共识": name_val,
            "姓名_支持引擎": "|".join(name_sup),
            "身份证号_共识": id_val,
            "身份证号_支持引擎": "|".join(id_sup),
            "相关企业_共识": rel_val,
            "相关企业_支持引擎": "|".join(rel_sup),
            "任职_共识": job_val,
            "任职_支持引擎": "|".join(job_sup),
            "参股_共识": share_val,
            "参股_支持引擎": "|".join(share_sup),
        })

        # 上传日期/时间优先使用结构化字段，其次回退到任职情况信息里正则提取
        date_raw = [methods.get(m, {}).get("任职情况信息", "") for m in METHODS]
        date_hits = []
        time_hits = []
        for txt in date_raw:
            text = _norm_text(txt)
            if not text:
                continue
            m = re.search(r"(\d{2}[-/]\d{2})", text)
            if m:
                date_hits.append(m.group(1).replace("/", "-"))
            m2 = re.search(r"(\d{2}:\d{2})", text)
            if m2:
                time_hits.append(m2.group(1))
        struct_date_raw = [methods.get(m, {}).get("截图日期", "") for m in METHODS]
        struct_time_raw = [methods.get(m, {}).get("截图时间", "") for m in METHODS]

        if struct_date_raw:
            date_hits.extend([_norm_text(v) for v in struct_date_raw if _norm_text(v)])
        if struct_time_raw:
            time_hits.extend([_norm_text(v) for v in struct_time_raw if _norm_text(v)])

        if date_hits:
            row["上传日期"] = Counter(date_hits).most_common(1)[0][0]
        if time_hits:
            row["上传时间"] = Counter(time_hits).most_common(1)[0][0]

        rows.append(row)
    return rows


def _build_method_rows(flat: Dict[str, dict]) -> List[dict]:
    rows = []
    for fn, rec in sorted(flat.items()):
        file_no, file_name = _parse_file_meta(fn)
        methods = rec.get("methods", {})
        for method in METHODS:
            m = methods.get(method, {})
            rows.append({
                "文件名": fn,
                "文件编号": file_no,
                "文件名中的姓名": file_name,
                "方案": method,
                "姓名": _norm_text(m.get("姓名", "")),
                "身份证号": _norm_text(m.get("身份证号", "")),
                "相关企业": _norm_text(m.get("相关企业", "")),
                "任职": _norm_text(m.get("任职", "")),
                "参股": _norm_text(m.get("参股", "")),
                "查询结论": _norm_text(m.get("查询结论", "")),
                "任职情况信息": _norm_text(m.get("任职情况信息", "")),
                "原始OCR": _norm_text(m.get("原始OCR", "")),
                "time_ms": str(m.get("time_ms", "")),
                "line_count": str(m.get("line_count", "")),
                "method_avg_confidence": str(m.get("method_score", {}).get("avg_confidence", "")),
                "method_min_confidence": str(m.get("method_score", {}).get("min_confidence", "")),
                "method_max_confidence": str(m.get("method_score", {}).get("max_confidence", "")),
                "method_line_conf_count": str(m.get("method_score", {}).get("line_confidence_count", "")),
                "method_raw_avg_confidence": str(m.get("method_score", {}).get("raw_avg_confidence", "")),
                "error": _norm_text(m.get("error", "")),
            })
    return rows


def _compare(before: Dict[str, dict], after: Dict[str, dict]) -> Dict[str, Any]:
    # 默认字段
    files = set(before.keys()) | set(after.keys())
    total = len(files)

    method_summary = {
        m: {
            "file_count": total,
            "non_empty_before": Counter(),
            "non_empty_after": Counter(),
            "fix_count": Counter(),
            "regress_count": Counter(),
            "change_count": Counter(),
        }
        for m in METHODS
    }
    for m in METHODS:
        for field in BASE_FIELDS:
            method_summary[m][field] = 0

    diffs: List[dict] = []
    for fn in sorted(files):
        file_no, file_name = _parse_file_meta(fn)
        before_methods = before.get(fn, {}).get("methods", {})
        after_methods = after.get(fn, {}).get("methods", {})
        for method in METHODS:
            b = before_methods.get(method, {})
            a = after_methods.get(method, {})
            for field in BASE_FIELDS:
                bv = _norm_text(b.get(field, "")) if b else ""
                av = _norm_text(a.get(field, "")) if a else ""
                if field in {"相关企业", "任职", "参股"}:
                    bn = _norm_count(bv)
                    an = _norm_count(av)
                else:
                    bn = bv
                    an = av
                status = "unchanged"
                if bn == "" and an:
                    status = "fix"
                    method_summary[method]["fix_count"][field] += 1
                    method_summary[method]["change_count"][field] += 1
                elif bn and not an:
                    status = "regress"
                    method_summary[method]["regress_count"][field] += 1
                    method_summary[method]["change_count"][field] += 1
                elif bn != an:
                    status = "modify"
                    method_summary[method]["change_count"][field] += 1

                if bn:
                    method_summary[method]["non_empty_before"][field] += 1
                if an:
                    method_summary[method]["non_empty_after"][field] += 1

                diffs.append({
                    "文件名": fn,
                    "文件编号": file_no,
                    "文件名中的姓名": file_name,
                    "方案": method,
                    "字段": field,
                    "基线值": bv,
                    "新值": av,
                    "状态": status,
                })

    summary = {"file_count": total, "methods": {}}
    for method, agg in method_summary.items():
        by_method = {}
        for field in BASE_FIELDS:
            before_cnt = int(agg["non_empty_before"][field])
            after_cnt = int(agg["non_empty_after"][field])
            fix_cnt = int(agg["fix_count"][field])
            regress_cnt = int(agg["regress_count"][field])
            change_cnt = int(agg["change_count"][field])
            by_method[field] = {
                "non_empty_before": before_cnt,
                "non_empty_after": after_cnt,
                "fix": fix_cnt,
                "regress": regress_cnt,
                "modify": change_cnt,
                "before_coverage": before_cnt / total if total else 0.0,
                "after_coverage": after_cnt / total if total else 0.0,
                "delta": after_cnt - before_cnt,
            }
        summary["methods"][method] = by_method
    summary["diff_count"] = len([d for d in diffs if d["状态"] != "unchanged"])
    summary["fix_count"] = sum(1 for d in diffs if d["状态"] == "fix")
    summary["regress_count"] = sum(1 for d in diffs if d["状态"] == "regress")
    summary["modify_count"] = sum(1 for d in diffs if d["状态"] == "modify")
    return {
        "summary": summary,
        "diffs": diffs,
    }


def _write_tsv(path: Path, rows: List[dict], headers: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(headers)
        for r in rows:
            w.writerow([r.get(h, "") for h in headers])


def _write_flat_json(path: Path, flat_records: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = [flat_records[k] for k in sorted(flat_records)]
    path.write_text(
        json.dumps(out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_summary_md(path: Path, eval_result: Dict[str, Any], out_before: Path, out_after: Path):
    s = eval_result["summary"]
    lines = [
        "# 4 路规则 A/B 对账报告（业务字段）",
        "",
        f"- 生成时间：{datetime.now().strftime('%F %T')}",
        f"- 基线样本数：{s['file_count']}",
        f"- 总变更项（不含 unchanged）：{s['diff_count']}",
        f"- 新增（fix）：{s['fix_count']}",
        f"- 回退（regress）：{s['regress_count']}",
        f"- 其他改动（modify）：{s['modify_count']}",
        "",
        f"- 旧版基线 flat：{out_before}",
        f"- 新版基线 flat：{out_after}",
        "",
        "## 方案级覆盖率变化",
        "| 方案 | 字段 | 旧有值 | 新有值 | 覆盖率旧 | 覆盖率新 | Δ值数 | 新增 | 回退 | 改动 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for method in METHODS:
        m = s["methods"].get(method, {})
        for field in BASE_FIELDS:
            item = m.get(field, {})
            lines.append(
                f"| {method} | {field} | {item.get('non_empty_before', 0)} | {item.get('non_empty_after', 0)} | "
                f"{item.get('before_coverage', 0):.2%} | {item.get('after_coverage', 0):.2%} | "
                f"{item.get('delta', 0)} | {item.get('fix', 0)} | {item.get('regress', 0)} |"
            )
    lines += [
        "",
        "## 说明",
        "- 字段“新增”定义为旧版为空、新版非空。",
        "- 字段“回退”定义为旧版非空、新版为空。",
        "- 字段“改动”包括“新增/回退/值变更”。",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True, help="四路 raw JSON 所在目录")
    parser.add_argument("--before-flat", default="", help="历史基线 flat JSON（可为空则跳过 A/B）")
    parser.add_argument("--out-base-dir", default="", help="输出目录，默认输入目录/rule_eval")
    parser.add_argument("--out-flat", default="", help="新版 flat 输出完整路径（覆盖）")
    parser.add_argument("--out-before-baseline", default="", help="旧版共识基线输出路径")
    parser.add_argument("--out-after-baseline", default="", help="新版共识基线输出路径")
    parser.add_argument("--out-diff", default="", help="AB 逐字段明细输出路径")
    parser.add_argument("--out-summary", default="", help="AB 汇总报告输出路径")
    parser.add_argument("--out-method-flat", default="", help="AB 按图片-方案展开明细输出路径")
    parser.add_argument("--out-method-rows", default="", help="后置方案字段展开明细（每张每方案）")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    out_dir = Path(args.out_base_dir) if args.out_base_dir else input_dir / "rule_eval"
    out_dir.mkdir(parents=True, exist_ok=True)

    method_raw = _collect_method_raw(input_dir)

    rebuilt = _rebuild_flat(method_raw)
    out_flat = (
        Path(args.out_flat)
        if args.out_flat
        else out_dir / "ocr_engine_698_rule_after_flat.json"
    )
    _write_flat_json(out_flat, rebuilt)

    # 基于后置 flat 生成共识基线（用于横向分析与后续判据）
    after_baseline_rows = _build_baseline_rows(rebuilt, "after")
    out_after_baseline = Path(args.out_after_baseline) if args.out_after_baseline else out_dir / "ocr_698_baseline_after.tsv"
    _write_tsv(
        out_after_baseline,
        after_baseline_rows,
        [
            "文件名", "文件编号", "文件名中的姓名", "姓名_共识", "姓名_支持引擎", "身份证号_共识", "身份证号_支持引擎",
            "相关企业_共识", "相关企业_支持引擎", "任职_共识", "任职_支持引擎",
            "参股_共识", "参股_支持引擎", "上传日期", "上传时间",
        ],
    )

    # 未传基线时，仅用于生成新 flat
    if not args.before_flat:
        return

    before = _load_flat_records(Path(args.before_flat))
    comp = _compare(before, rebuilt)

    out_before_baseline = Path(args.out_before_baseline) if args.out_before_baseline else out_dir / "ocr_698_baseline_before.tsv"
    before_baseline_rows = _build_baseline_rows(before, "before")
    _write_tsv(
        out_before_baseline,
        before_baseline_rows,
        [
            "文件名", "文件编号", "文件名中的姓名", "姓名_共识", "姓名_支持引擎", "身份证号_共识", "身份证号_支持引擎",
            "相关企业_共识", "相关企业_支持引擎", "任职_共识", "任职_支持引擎",
            "参股_共识", "参股_支持引擎", "上传日期", "上传时间",
        ],
    )

    out_diff = Path(args.out_diff) if args.out_diff else out_dir / "ocr_engine_698_rule_ab_diffs.tsv"
    _write_tsv(
        out_diff,
        comp["diffs"],
        ["文件名", "文件编号", "文件名中的姓名", "方案", "字段", "基线值", "新值", "状态"],
    )

    out_summary = Path(args.out_summary) if args.out_summary else out_dir / "ocr_698_rule_ab_summary.md"
    _write_summary_md(out_summary, comp, out_before_baseline, out_after_baseline)

    method_rows = []
    for d in comp["diffs"]:
        method_rows.append(d)
    out_method_flat = Path(args.out_method_flat) if args.out_method_flat else out_dir / "ocr_engine_698_rule_ab_method_rows.tsv"
    _write_tsv(out_method_flat, method_rows, ["文件名", "文件编号", "文件名中的姓名", "方案", "字段", "基线值", "新值", "状态"])

    out_method_rows = Path(args.out_method_rows) if args.out_method_rows else out_dir / "ocr_engine_698_rule_after_method_rows.tsv"
    _write_tsv(
        out_method_rows,
        _build_method_rows(rebuilt),
        [
            "文件名", "文件编号", "文件名中的姓名", "方案", "姓名", "身份证号",
            "相关企业", "任职", "参股", "查询结论", "任职情况信息", "原始OCR",
            "time_ms", "line_count", "method_avg_confidence", "method_min_confidence", "method_max_confidence",
            "method_line_conf_count", "method_raw_avg_confidence", "error",
        ],
    )

    # 附带写 JSON 供后续脚本消费
    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_dir": str(input_dir),
        "before_flat": str(args.before_flat),
        "out_flat": str(out_flat),
        "after_baseline": str(out_after_baseline),
        "before_baseline": str(out_before_baseline),
        "summary": comp["summary"],
        "diff_count": comp["summary"]["diff_count"],
        "fix_count": comp["summary"]["fix_count"],
        "regress_count": comp["summary"]["regress_count"],
        "modify_count": comp["summary"]["modify_count"],
    }
    (out_dir / "ocr_698_rule_ab_result.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
