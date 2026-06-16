#!/usr/bin/env python3
"""生成 OCR 列表页所需 data.js（单文件前端读取）。"""

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


METHODS_DEFAULT = [
    "paddleocr_2_7_3",
    "rapidocr_2_7_3",
    "rapidocr_isolated",
    "paddleocr_3_x",
]
FIELDS = ["姓名", "身份证号", "相关企业", "任职", "参股"]
CAPTURE_FIELDS = ["截图日期", "截图时间"]
NOISE_PERSON_TOKENS = {
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
PERSON_STOPWORDS = NOISE_PERSON_TOKENS | {
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


def _norm(v: Any) -> str:
    return "" if v is None else str(v).strip()


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


def _parse_file_meta(filename: str):
    stem = Path(filename).name.rsplit(".", 1)[0]
    if not stem:
        return "", ""
    file_no = ""
    file_name = ""

    m = re.match(r"^(\d{6,20})(?=\D|$)", stem)
    if m:
        file_no = m.group(1)

    candidates = []
    for seg in re.split(r"[-_+\s]+", stem):
        seg = seg.strip()
        if not seg or seg in NOISE_PERSON_TOKENS:
            continue
        seg = re.sub(r"^\d{6,20}", "", seg)
        candidates.extend(_person_candidates_from_text(seg))
        m2 = re.match(r"^\d+([\u4e00-\u9fff]{2,8})$", seg)
        if m2:
            candidates.extend(_person_candidates_from_text(m2.group(1)))

    if not candidates:
        candidates = _person_candidates_from_text(stem)

    for name in reversed(candidates):
        if 2 <= len(name) <= 4:
            file_name = name
            break
    if not file_name:
        for name in reversed(candidates):
            if 2 <= len(name) <= 8:
                file_name = name
                break

    return file_no, file_name


def _is_valid_person_name(value: Any) -> bool:
    name = _norm(value)
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,4}", name):
        return False
    if name in PERSON_STOPWORDS:
        return False
    if any(token in name for token in PERSON_STOPWORDS if len(token) >= 2):
        return False
    return True


def _clean_person_candidate(value: Any) -> str:
    raw = _norm(value)
    if not raw:
        return ""
    for token in sorted(NOISE_PERSON_TOKENS, key=len, reverse=True):
        raw = raw.replace(token, " ")
    raw = re.sub(r"^\d{6,20}", "", raw)
    chunks = re.findall(r"[\u4e00-\u9fff]{2,8}", raw)
    for chunk in reversed(chunks):
        if _is_valid_person_name(chunk):
            return chunk
    return ""


def _person_candidates_from_text(text: Any) -> List[str]:
    raw = _norm(text)
    if not raw:
        return []
    for token in sorted(NOISE_PERSON_TOKENS, key=len, reverse=True):
        raw = raw.replace(token, " ")
    candidates: List[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,8}", raw):
        cand = _clean_person_candidate(chunk)
        if cand:
            candidates.append(cand)
    return candidates


def _line_is_name_noise(line: str) -> bool:
    text = _norm(line)
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
    lines = [_norm(x) for x in str(text or "").splitlines()]
    if not lines:
        return ""
    sep_idx = -1
    for i, line in enumerate(lines):
        if "投资任职信息" in line and "未查询" not in line:
            sep_idx = i
            break
    windows = []
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


def _calibrate_name_cell(filename: str, file_name: str, methods: Dict[str, Dict[str, Any]], cell: Dict[str, Any]) -> Dict[str, Any]:
    file_candidate = _clean_person_candidate(file_name) or _parse_file_meta(filename)[1]
    current = _clean_person_candidate(cell.get("value"))

    ocr_votes = Counter()
    method_votes = Counter()
    for m in METHODS_DEFAULT:
        md = methods.get(m, {}) or {}
        ocr_name = _extract_ocr_name_from_text(md.get("原始OCR", ""))
        if ocr_name:
            ocr_votes[ocr_name] += 1
        method_name = _clean_person_candidate(md.get("姓名"))
        if method_name:
            method_votes[method_name] += 1

    ocr_top, ocr_support = ("", 0)
    if ocr_votes:
        ocr_top, ocr_support = ocr_votes.most_common(1)[0]
    method_top, method_support = ("", 0)
    if method_votes:
        method_top, method_support = method_votes.most_common(1)[0]

    selected = current
    source = cell.get("source", "weighted_consensus")
    support = list(cell.get("support", [])) if isinstance(cell.get("support"), list) else []
    rules = []

    if ocr_top and ocr_support >= 2 and (not current or current != ocr_top):
        if not file_candidate or file_candidate == ocr_top or not _is_valid_person_name(current):
            selected = ocr_top
            source = "name_calibration_ocr_line"
            support = [m for m in METHODS_DEFAULT if _extract_ocr_name_from_text((methods.get(m, {}) or {}).get("原始OCR", "")) == ocr_top]
            rules.append("ocr_line_vote_ge2")
    if file_candidate and (not selected or not _is_valid_person_name(selected) or selected in PERSON_STOPWORDS):
        selected = file_candidate
        source = "name_calibration_filename"
        support = ["filename"]
        rules.append("filename_noise_clean")
    if file_candidate and selected and selected != file_candidate and selected in PERSON_STOPWORDS:
        selected = file_candidate
        source = "name_calibration_filename"
        support = ["filename"]
        rules.append("reject_name_stopword")
    if not selected and method_top:
        selected = method_top
        source = "name_calibration_method_vote"
        support = [m for m in METHODS_DEFAULT if _clean_person_candidate((methods.get(m, {}) or {}).get("姓名")) == method_top]
        rules.append("method_vote")

    out = dict(cell)
    if selected and selected != _norm(cell.get("value")):
        out.update(
            {
                "value": selected,
                "status": "agree",
                "source": source,
                "support": support,
                "raw_value": _norm(cell.get("value")),
                "raw_status": cell.get("status", "missing"),
                "rule_processed": True,
                "rules": rules,
                "ocr_line_top": ocr_top,
                "ocr_line_support": ocr_support,
                "filename_candidate": file_candidate,
            }
        )
    elif selected:
        out["value"] = selected
    return out


def _consensus_text(values: List[str]) -> Dict[str, Any]:
    score = Counter(v for v in values if v)
    if not score:
        return {"value": "", "status": "missing", "unique_count": 0}
    items = sorted(score.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    return {
        "value": items[0][0],
        "status": "agree" if len(score) == 1 else "conflict",
        "unique_count": len(score),
    }


def _weighted_cell(file_row: Dict[str, Any], field: str, fallback_values: List[str]) -> Dict[str, Any]:
    weighted = file_row.get("weighted_consensus", {}) if isinstance(file_row, dict) else {}
    fields = weighted.get("fields", {}) if isinstance(weighted, dict) else {}
    support = weighted.get("support", {}) if isinstance(weighted, dict) else {}
    scores = weighted.get("scores", {}) if isinstance(weighted, dict) else {}
    value = _norm(fields.get(field)) if isinstance(fields, dict) else ""
    if not value:
        return _consensus_text(fallback_values)

    non_empty = [v for v in fallback_values if v]
    unique_count = len(set(non_empty))
    return {
        "value": value,
        "status": "agree",
        "unique_count": unique_count,
        "source": "weighted_consensus",
        "support": support.get(field, []) if isinstance(support, dict) else [],
        "score": scores.get(field, 0.0) if isinstance(scores, dict) else 0.0,
        "raw_status": "missing" if not non_empty else ("agree" if unique_count <= 1 else "conflict"),
    }


def _compact_ocr_lines(lines: Any) -> List[Dict[str, Any]]:
    compact: List[Dict[str, Any]] = []
    if not isinstance(lines, list):
        return compact
    for idx, line in enumerate(lines):
        if not isinstance(line, dict):
            continue
        item: Dict[str, Any] = {
            "index": line.get("index", idx + 1),
            "text": _norm(line.get("text")),
        }
        conf = _to_float(line.get("confidence"))
        if conf is not None:
            item["confidence"] = conf
        compact.append(item)
    return compact


def _method_row(file_row: Dict[str, Any], method: str, include_detail: bool = True) -> Dict[str, Any]:
    m = file_row.get("methods", {}).get(method, {}) if isinstance(file_row, dict) else {}
    row = {
        "姓名": _norm(m.get("姓名")),
        "身份证号": _norm(m.get("身份证号")),
        "相关企业": _norm(m.get("相关企业")),
        "任职": _norm(m.get("任职")),
        "参股": _norm(m.get("参股")),
        "截图日期": _norm(m.get("截图日期")),
        "截图时间": _norm(m.get("截图时间")),
        "查询结论": _norm(m.get("查询结论")),
        "任职情况信息": _norm(m.get("任职情况信息")),
        "error": _norm(m.get("error")),
        "time_ms": float(m.get("time_ms", 0.0) or 0.0),
        "line_count": float(m.get("line_count", 0.0) or 0.0),
        "method_score": m.get("method_score", {}),
        "image_type": _norm(m.get("image_type", "query_summary")) or "query_summary",
    }
    if include_detail:
        row["原始OCR"] = _norm(m.get("原始OCR"))
        row["ocr_lines"] = _compact_ocr_lines(m.get("ocr_lines", []))
        row["field_scores"] = m.get("field_scores", {})
        row["field_provenance"] = m.get("field_provenance", {})
    return row


def _row_group_key(row: Dict[str, Any]) -> str:
    file_no = _norm(row.get("file_no"))
    file_name = _norm(row.get("file_name_from_filename"))
    if not file_no or not file_name:
        return ""
    return f"{file_no}|||{file_name}"


def _cell_map(row: Dict[str, Any], field: str) -> Dict[str, Any]:
    if field in CAPTURE_FIELDS:
        return row.get("capture_consensus", {}).get(field, {})
    return row.get("field_consensus", {}).get(field, {})


def _set_cell(row: Dict[str, Any], field: str, cell: Dict[str, Any]) -> None:
    if field in CAPTURE_FIELDS:
        row.setdefault("capture_consensus", {})[field] = cell
    else:
        row.setdefault("field_consensus", {})[field] = cell


def _recount_row(row: Dict[str, Any]) -> None:
    missing_count = 0
    conflict_count = 0
    for f in FIELDS + CAPTURE_FIELDS:
        status = _cell_map(row, f).get("status", "missing")
        if status == "missing":
            missing_count += 1
        elif status == "conflict":
            conflict_count += 1
    row["missing_count"] = missing_count
    row["conflict_count"] = conflict_count


def _apply_group_fill(rows: List[Dict[str, Any]]) -> None:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        key = _row_group_key(row)
        if not key:
            continue
        groups.setdefault(key, []).append(row)

    for group_rows in groups.values():
        if len(group_rows) <= 1:
            continue
        for field in FIELDS + CAPTURE_FIELDS:
            values = [_norm(_cell_map(row, field).get("value")) for row in group_rows]
            non_empty = [v for v in values if v]
            unique = sorted(set(non_empty))
            if len(unique) != 1:
                continue
            fill_value = unique[0]
            for row in group_rows:
                cell = dict(_cell_map(row, field))
                if _norm(cell.get("value")):
                    continue
                cell.update(
                    {
                        "value": fill_value,
                        "status": "agree",
                        "source": "group_fill",
                        "raw_status": "missing",
                        "group_fill": True,
                    }
                )
                _set_cell(row, field, cell)
                row["search"] = f"{row.get('search', '')} {fill_value}".strip().lower()
        for row in group_rows:
            _recount_row(row)


def _build_rows(flat_records: List[Dict[str, Any]], include_detail: bool = True) -> List[Dict[str, Any]]:
    rows = []
    for idx, rec in enumerate(flat_records):
        fn = _norm(rec.get("filename"))
        file_no, file_name = _parse_file_meta(fn)

        methods = {m: _method_row(rec, m, include_detail=include_detail) for m in METHODS_DEFAULT}

        field_consensus = {}
        missing_count = 0
        conflict_count = 0
        for f in FIELDS:
            c = _weighted_cell(rec, f, [methods[m].get(f, "") for m in METHODS_DEFAULT])
            if f == "姓名":
                c = _calibrate_name_cell(fn, file_name, methods, c)
                if c.get("value"):
                    file_name = _clean_person_candidate(file_name) or _norm(c.get("value"))
            field_consensus[f] = c
            if c["status"] == "missing":
                missing_count += 1
            elif c["status"] == "conflict":
                conflict_count += 1

        capture_consensus = {}
        for f in CAPTURE_FIELDS:
            c = _weighted_cell(rec, f, [methods[m].get(f, "") for m in METHODS_DEFAULT])
            capture_consensus[f] = c
            if c["status"] == "missing":
                missing_count += 1
            elif c["status"] == "conflict":
                conflict_count += 1

        times = [methods[m]["time_ms"] for m in METHODS_DEFAULT if isinstance(methods[m].get("time_ms"), (int, float))]
        if times:
            time_stats = {
                "min": min(times),
                "max": max(times),
                "avg": sum(times) / len(times),
            }
        else:
            time_stats = {"min": 0.0, "max": 0.0, "avg": 0.0}

        has_error = any(bool(methods[m].get("error")) for m in METHODS_DEFAULT)
        issue_count = len(rec.get("issues", []) or [])
        method_avg_conf = []
        method_min_conf = []
        method_max_conf = []
        for m in METHODS_DEFAULT:
            ms = methods[m].get("method_score", {})
            avg = _to_float(ms.get("avg_confidence"))
            mn = _to_float(ms.get("min_confidence"))
            mx = _to_float(ms.get("max_confidence"))
            if avg is not None:
                method_avg_conf.append(avg)
            if mn is not None:
                method_min_conf.append(mn)
            if mx is not None:
                method_max_conf.append(mx)
        method_confidence = {
            "avg": sum(method_avg_conf) / len(method_avg_conf) if method_avg_conf else 0.0,
            "min": min(method_min_conf) if method_min_conf else 0.0,
            "max": max(method_max_conf) if method_max_conf else 0.0,
        }

        rows.append({
            "filename": fn,
            "file_name": fn,
            "file_no": file_no,
            "file_name_from_filename": _clean_person_candidate(file_name),
            "idx": idx,
            "methods": methods,
            "field_consensus": field_consensus,
            "capture_consensus": capture_consensus,
            "time_stats": time_stats,
            "has_error": has_error,
            "issues": rec.get("issues", []),
            "issue_count": issue_count,
            "method_confidence": method_confidence,
            "missing_count": missing_count,
            "conflict_count": conflict_count,
            "search": f"{fn} {file_no} {_clean_person_candidate(file_name)} {field_consensus.get('姓名', {}).get('value', '')}".strip().lower(),
            "image_type": "query_summary",
            "method_image_types": {m: methods[m].get("image_type", "query_summary") for m in METHODS_DEFAULT},
            "method_scores": {m: methods[m].get("method_score", {}) for m in METHODS_DEFAULT},
            "field_scores": {m: methods[m].get("field_scores", {}) for m in METHODS_DEFAULT},
            "weighted_consensus": rec.get("weighted_consensus", {}),
            "roi_id_fallback": rec.get("roi_id_fallback", {}),
        })

    _apply_group_fill(rows)
    return rows


def _build_output(flat_records: List[Dict[str, Any]], source: str, image_dir: str, include_detail: bool = True) -> Dict[str, Any]:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "generated_at": ts,
        "source": source,
        "methods": METHODS_DEFAULT,
        "fields": FIELDS,
        "rows": _build_rows(flat_records, include_detail=include_detail),
        "meta": {
            "generated_at": ts,
            "record_count": len(flat_records),
            "source": source,
            "image_dir": image_dir,
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--flat", default="ops/reports/199_rule_eval_20260611_1700/ocr_engine_698_rule_after_flat.json")
    parser.add_argument("--out", default="ops/reports/ocr_list_dashboard/data.js")
    parser.add_argument("--image-dir", default="images")
    args = parser.parse_args()

    flat_path = Path(args.flat)
    if not flat_path.exists():
        raise FileNotFoundError(f"flat 不存在：{flat_path}")
    flat_records = json.loads(flat_path.read_text(encoding="utf-8"))
    if not isinstance(flat_records, list):
        raise ValueError("flat 文件不是 JSON 数组")

    data = _build_output(flat_records, str(flat_path), args.image_dir)
    payload = "window.OCR_REPORT = " + json.dumps(data, ensure_ascii=False, indent=2) + ";\n"
    Path(args.out).write_text(payload, encoding="utf-8")
    print(f"写入 {args.out}，记录数: {len(flat_records)}")


if __name__ == "__main__":
    main()
