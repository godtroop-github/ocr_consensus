#!/usr/bin/env python3

"""Build 4-way weighted baseline for 698 samples.

The script compares three groups of strategies:
- raw value majority (old behavior)
- weighted consensus by method weights + shape/consistency
- optional compare against existing baseline for regression tracking

Output:
- ocr_698_baseline_base_info_weighted.tsv
- ocr_698_baseline_coverage_weighted.md
- ocr_698_strategy_field_diff.tsv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

METHODS = [
    "paddleocr_2_7_3",
    "rapidocr_2_7_3",
    "rapidocr_isolated",
    "paddleocr_3_x",
]

FIELD_WEIGHTS: Dict[str, Dict[str, float]] = {
    "姓名": {
        "paddleocr_2_7_3": 1.00,
        "rapidocr_2_7_3": 1.00,
        "rapidocr_isolated": 1.00,
        "paddleocr_3_x": 1.00,
    },
    "身份证号": {
        "paddleocr_2_7_3": 0.60,
        "rapidocr_2_7_3": 1.05,
        "rapidocr_isolated": 1.05,
        "paddleocr_3_x": 1.00,
    },
    "相关企业": {
        "paddleocr_2_7_3": 0.25,
        "rapidocr_2_7_3": 1.05,
        "rapidocr_isolated": 1.05,
        "paddleocr_3_x": 1.00,
    },
    "任职": {
        "paddleocr_2_7_3": 0.25,
        "rapidocr_2_7_3": 1.05,
        "rapidocr_isolated": 1.05,
        "paddleocr_3_x": 1.00,
    },
    "参股": {
        "paddleocr_2_7_3": 0.25,
        "rapidocr_2_7_3": 1.05,
        "rapidocr_isolated": 1.05,
        "paddleocr_3_x": 1.00,
    },
    "截图日期": {
        "paddleocr_2_7_3": 0.50,
        "rapidocr_2_7_3": 1.05,
        "rapidocr_isolated": 1.05,
        "paddleocr_3_x": 0.35,
    },
    "截图时间": {
        "paddleocr_2_7_3": 0.50,
        "rapidocr_2_7_3": 1.05,
        "rapidocr_isolated": 1.05,
        "paddleocr_3_x": 0.35,
    },
}


def normalize_space(text: object) -> str:
    if text is None:
        return ""
    return str(text).replace("\u2013", "-").replace("\u2014", "-").strip()


def norm_no_space(text: object) -> str:
    return re.sub(r"\s+", "", normalize_space(text))


def normalize_count(text: object) -> str:
    s = normalize_space(text)
    if not s:
        return ""
    m = re.search(r"\d+", s)
    if not m:
        return ""
    return str(int(m.group(0)))


def canonical_name(text: object) -> str:
    return norm_no_space(text)


def canonical_id(text: object) -> str:
    return norm_no_space(text)


def normalize_date_time(text: object) -> str:
    s = normalize_space(text)
    s = s.replace("：", ":").replace("－", "-")
    return s


def extract_date_time(text: object) -> Tuple[str, str]:
    s = normalize_date_time(text)
    if not s:
        return "", ""

    def fmt_mmdd(raw: str) -> str:
        raw = normalize_space(raw).replace("/", "-")
        compact = raw.replace("-", "")
        if re.fullmatch(r"\d{4}", compact):
            return f"{compact[:2]}-{compact[2:]}"
        return raw

    # 07-15 10:09 / 07-1509:09 / 0715 10:09
    for m in re.finditer(r"(?<!\d)(\d{2}[-/]?\d{2})(?:\D{0,8})(\d{2}:\d{2})(?!\d)", s):
        date_s = fmt_mmdd(m.group(1))
        time_s = m.group(2)
        return date_s, time_s

    # 10:09 07-15
    for m in re.finditer(r"(\d{2}:\d{2})(?:\D{0,8})(\d{2}[-/]?\d{2})", s):
        date_s = fmt_mmdd(m.group(2))
        time_s = m.group(1)
        return date_s, time_s

    return "", ""


def _clamp01(v: object) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    if x != x:
        return 0.0
    if x < 0:
        return 0.0
    if x > 1:
        return 1.0
    return x


def _field_confidence(md: Dict[str, object], field: str) -> float:
    fs = md.get("field_scores")
    if isinstance(fs, dict):
        conf = None
        fv = fs.get(field)
        if isinstance(fv, dict):
            conf = fv.get("confidence")
            rule_meta = _normalize_rule_meta(fv.get("provenance"))
            rule_factor = _rule_quality_score(rule_meta)
        elif isinstance(fv, (str, int, float)):
            conf = fv
            rule_factor = 0.95
        else:
            rule_factor = 1.0
        if conf is not None:
            return _clamp01(conf) * rule_factor

    ms = md.get("method_score")
    if isinstance(ms, dict):
        conf = ms.get("avg_confidence")
        if conf is not None:
            return _clamp01(conf)
    return 0.0


def _normalize_rule_meta(meta: Any) -> Dict[str, Any]:
    if isinstance(meta, dict):
        src = str(meta.get("source", "")).strip().lower()
        rules = meta.get("rules")
        if isinstance(rules, (list, tuple)):
            rules = [str(r).strip() for r in rules if str(r).strip()]
        elif rules:
            rules = [str(rules)]
        else:
            rules = []
        return {
            "source": src,
            "rules": rules,
            "rule_processed": bool(meta.get("rule_processed")),
        }
    return {"source": "", "rules": [], "rule_processed": False}


def _rule_quality_score(rule_meta: Dict[str, object]) -> float:
    src = str(rule_meta.get("source", "")).lower()
    rules = set(str(r) for r in rule_meta.get("rules", []) if str(r).strip())
    processed = bool(rule_meta.get("rule_processed"))

    if src == "missing":
        return 0.30
    if src in {"raw_line", "ocr_line"}:
        return 1.0
    if src == "line_fusion_rule":
        return 0.95
    if src == "count_rule_match":
        return 0.96
    if src == "count_rule_nomatch":
        return 0.78
    if src == "no_result_forced_zero":
        return 0.70
    if src == "id_pattern_reconstruct":
        return 0.82
    if "filename" in src:
        return 0.92
    if "reversed" in ", ".join(rules):
        return 0.83
    if "line_pattern" in ", ".join(rules):
        return 0.88
    if processed:
        return 0.85
    return 1.0


def _normalize_id(raw: str) -> str:
    s = normalize_space(raw)
    if not s:
        return ""
    return re.sub(r"[^0-9Xx*]", "", s)


def _id_tail_len_and_stars(raw: str) -> Tuple[int, int]:
    s = _normalize_id(raw)
    if not s:
        return 0, 0
    m = re.fullmatch(r"\d{3}(\*+)([0-9Xx]*)", s)
    if not m:
        return 0, 0
    stars = m.group(1)
    tail = m.group(2)
    # 尾号允许只保留可见数字段；如尾号有*，视为异常长度
    if "*" in tail:
        return 0, len(stars)
    return len(tail), len(stars)


def _id_shape_score(raw: str) -> float:
    s = _normalize_id(raw)
    if not s:
        return 0.0

    # 最理想：3位 + 8星 + 4位尾号
    if re.fullmatch(r"\d{3}\*{8}[0-9Xx]{4}", s):
        return 1.0

    m = re.fullmatch(r"\d{3}(\*+)([0-9Xx]{1,})", s)
    if not m:
        return 0.0

    stars = len(m.group(1))
    tail = m.group(2)

    if len(tail) == 4 and stars >= 6:
        return 0.85
    if len(tail) in (3, 5) and stars >= 6:
        return 0.45
    if len(tail) in (2, 6) and stars >= 6:
        return 0.25
    if len(tail) >= 1 and stars >= 6:
        return 0.15
    return 0.05


def _count_shape_score(raw: str) -> float:
    return 1.0 if re.fullmatch(r"\d+", normalize_space(raw)) else 0.0


def _date_shape_score(raw: str) -> float:
    return 1.0 if re.fullmatch(r"\d{2}-\d{2}", normalize_space(raw)) else 0.0


def _time_shape_score(raw: str) -> float:
    return 1.0 if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", normalize_space(raw)) else 0.0


def _name_shape_score(raw: str) -> float:
    return 1.0 if normalize_space(raw) else 0.0


def _shape_score(field: str, raw: str) -> float:
    if field == "身份证号":
        return _id_shape_score(raw)
    if field in {"相关企业", "任职", "参股"}:
        return _count_shape_score(raw)
    if field == "截图日期":
        return _date_shape_score(raw)
    if field == "截图时间":
        return _time_shape_score(raw)
    return _name_shape_score(raw)


def normalize_for_field(field: str, raw: str) -> str:
    if field in {"相关企业", "任职", "参股"}:
        return normalize_count(raw)
    if field in {"截图日期", "截图时间"}:
        return normalize_space(raw)
    if field == "身份证号":
        return canonical_id(raw)
    return canonical_name(raw)


@dataclass
class Candidate:
    value: str
    raw: str
    methods: List[str]
    score: float


def weighted_pick(field: str, candidate_by_method: Dict[str, str], method_datas: Dict[str, dict]) -> Candidate:
    buckets: Dict[str, List[tuple]] = defaultdict(list)  # canonical -> [(method, raw, conf, rule_score)]
    for m in METHODS:
        raw = normalize_space(candidate_by_method.get(m, ""))
        if not raw:
            continue
        key = normalize_for_field(field, raw)
        if not key:
            continue
        conf = _field_confidence(method_datas.get(m, {}), field)
        rule_score = _rule_quality_score(
            _normalize_rule_meta(
                method_datas.get(m, {})
                .get("field_scores", {})
                .get(field, {})
                .get("provenance", {})
            )
        )
        buckets[key].append((m, raw, conf, rule_score))

    if not buckets:
        return Candidate("", "", [], 0.0)

    best: Optional[Candidate] = None
    for key, infos in buckets.items():
        methods = [i[0] for i in infos]
        confs = [i[2] for i in infos if i[2] > 0]
        rule_scores = [i[3] for i in infos if i[3] > 0]
        conf_avg = sum(confs) / len(confs) if confs else 0.0
        rule_quality = sum(rule_scores) / len(rule_scores) if rule_scores else 0.0
        weight_sum = 0.0
        for m in methods:
            weight_sum += FIELD_WEIGHTS.get(field, {}).get(m, 0.5)
        shape = _shape_score(field, key)
        support_ratio = len(methods) / len(METHODS)
        score = weight_sum + 0.42 * conf_avg + 0.30 * shape + 0.25 * support_ratio + 0.15 * rule_quality

        # 显示值取该候选下置信度最高的一条；若置信都为 0，取最长字符串
        top_raw = sorted(infos, key=lambda i: (i[2], i[3], len(i[1])), reverse=True)[0][1]

        if best is None or score > best.score + 1e-9:
            best = Candidate(key, top_raw, methods, score)
        elif abs(score - best.score) <= 1e-9:
            # 平局时支持更多、再看更高均值置信、再看更长
            best_support = len(best.methods)
            cur_support = len(methods)
            if cur_support > best_support:
                best = Candidate(key, top_raw, methods, score)
            elif cur_support == best_support:
                cur_conf = conf_avg
                best_conf = sum(_field_confidence(method_datas.get(m, {}), field) for m in best.methods)
                best_conf = (best_conf / len(best.methods)) if best.methods else 0.0
                if cur_conf > best_conf:
                    best = Candidate(key, top_raw, methods, score)
                elif cur_conf == best_conf:
                    if field == "身份证号":
                        best_tail, best_stars = _id_tail_len_and_stars(best.raw)
                        cur_tail, cur_stars = _id_tail_len_and_stars(top_raw)

                        if cur_tail > best_tail:
                            best = Candidate(key, top_raw, methods, score)
                        elif cur_tail == best_tail and cur_stars > best_stars:
                            best = Candidate(key, top_raw, methods, score)
                        elif cur_tail == best_tail and cur_stars == best_stars and len(top_raw) > len(best.raw):
                            best = Candidate(key, top_raw, methods, score)
                    elif len(top_raw) > len(best.raw):
                        best = Candidate(key, top_raw, methods, score)

    return best or Candidate("", "", [], 0.0)


def majority_pick(field_values: Dict[str, str]) -> str:
    votes: Dict[str, List[str]] = defaultdict(list)
    for m, v in field_values.items():
        vv = normalize_for_field("姓名", v)  # placeholder, override in caller as needed
        if not vv:
            continue
        votes[vv].append(m)
    if not votes:
        return ""
    best = sorted(votes.items(), key=lambda kv: (len(kv[1]), len(kv[0])), reverse=True)[0]
    return best[0]


def parse_employment_tsv(path: Path) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = defaultdict(list)
    if not path.exists():
        return out

    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            filename = normalize_space(row.get("文件名", ""))
            if not filename:
                continue
            out[filename].append(
                {
                    "engine": normalize_space(row.get("引擎", "")),
                    "seq": normalize_space(row.get("序号", "")),
                    "company": normalize_space(row.get("企业名称", "")),
                    "status": normalize_space(row.get("经营状态", "")),
                    "position": normalize_space(row.get("承担职务", "")),
                    "share": normalize_space(row.get("持股比例", "")),
                }
            )
    return out


def normalize_company_key(name: str) -> str:
    s = normalize_no_space = norm_no_space(name)
    s = re.sub(r"[（）()·•【】《》\[\]\"'，,.:：；;!！?？\-，]+", "", s)
    return s.lower()


def similar_company(a: str, b: str) -> bool:
    if a == b:
        return True
    if not a or not b:
        return False
    if min(len(a), len(b)) <= 2:
        return a == b
    return SequenceMatcher(None, a, b).ratio() >= 0.82


def cluster_emp_rows(rows: List[dict]) -> List[dict]:
    clusters: List[dict] = []
    for r in rows:
        key = normalize_company_key(r["company"])
        matched = None
        for idx, c in enumerate(clusters):
            if similar_company(key, c["key"]):
                matched = idx
                break
        if matched is None:
            clusters.append({"key": key, "rows": [r]})
        else:
            clusters[matched]["rows"].append(r)
    return clusters


def most_common(values: List[str]) -> str:
    if not values:
        return ""
    c = Counter(values)
    return c.most_common(1)[0][0]


def consensus_emp(cluster: dict) -> dict:
    rows = cluster["rows"]
    status = most_common([r["status"] for r in rows if r["status"]])
    pos = most_common([r["position"] for r in rows if r["position"]])
    share = most_common([r["share"] for r in rows if r["share"]])
    company = most_common([r["company"] for r in rows if r["company"]])
    if not company:
        company = cluster["key"]
    support = sorted({r["engine"] for r in rows if r["engine"]})
    return {
        "company": company,
        "status": status,
        "position": pos,
        "share": share,
        "support_engines": support,
    }


def _parse_old_baseline(path: Optional[Path]) -> Dict[str, Dict[str, str]]:
    if not path or not path.exists():
        return {}
    out: Dict[str, Dict[str, str]] = {}
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            fn = normalize_space(row.get("文件名", ""))
            if not fn:
                continue
            out[fn] = {
                "姓名": normalize_space(row.get("姓名", "")),
                "身份证号": normalize_space(row.get("身份证号", "")),
                "相关企业": normalize_space(row.get("相关企业数", "")),
                "任职": normalize_space(row.get("任职企业数", "")),
                "参股": normalize_space(row.get("参股企业数", "")),
                "截图日期": normalize_space(row.get("上传日期", "")),
                "截图时间": normalize_space(row.get("上传时间", "")),
            }
    return out


def _field_mode(field: str) -> str:
    if field in {"相关企业", "任职", "参股"}:
        return "count"
    if field in {"截图日期", "截图时间"}:
        return "dt"
    return "text"


def select_majority(field: str, methods_data: Dict[str, dict]) -> str:
    if field in {"截图日期", "截图时间"}:
        votes: Dict[str, List[str]] = defaultdict(list)
        for m in METHODS:
            txt = methods_data.get(m, {}).get("任职情况信息", "")
            d, t = extract_date_time(txt)
            if field == "截图日期" and d:
                votes[d].append(m)
            if field == "截图时间" and t:
                votes[t].append(m)
        if not votes:
            return ""
        return sorted(votes.items(), key=lambda kv: (len(kv[1]), len(kv[0])), reverse=True)[0][0]

    vals = {}
    for m in METHODS:
        raw = normalize_space(methods_data.get(m, {}).get(field, ""))
        if not raw:
            continue
        if _field_mode(field) == "count":
            vals[m] = normalize_count(raw)
        elif field == "截图日期" or field == "截图时间":
            vals[m] = normalize_space(raw)
        else:
            vals[m] = canonical_name(raw)

    if not vals:
        return ""
    votes2 = defaultdict(list)
    for m, v in vals.items():
        votes2[v].append(m)
    return sorted(votes2.items(), key=lambda kv: (len(kv[1]), len(kv[0])), reverse=True)[0][0]


def build():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--flat-report",
        default="ops/reports/ocr_engine_698_record_flat.json",
    )
    parser.add_argument(
        "--emp-tsv",
        default="ops/reports/ocr_698_form_employment.tsv",
    )
    parser.add_argument(
        "--out-dir",
        default="ops/reports/199_weighted_strategy_20260611",
    )
    parser.add_argument(
        "--old-baseline",
        default="ops/reports/ocr_698_baseline_base_info.tsv",
    )
    args = parser.parse_args()

    flat_report = Path(args.flat_report)
    emp_tsv = Path(args.emp_tsv)
    out_dir = Path(args.out_dir)
    old_baseline_path = Path(args.old_baseline) if args.old_baseline else None

    if not flat_report.exists():
        raise FileNotFoundError(f"missing {flat_report}")

    raw_records = json.loads(flat_report.read_text(encoding="utf-8"))
    if not isinstance(raw_records, list):
        raise ValueError("flat report should be a list")

    old_baseline = _parse_old_baseline(old_baseline_path)
    emp_by_file = parse_employment_tsv(emp_tsv)

    out_dir.mkdir(parents=True, exist_ok=True)
    base_out = out_dir / "ocr_698_baseline_base_info_weighted.tsv"
    coverage_out = out_dir / "ocr_698_baseline_coverage_weighted.md"
    diff_out = out_dir / "ocr_698_strategy_field_diff.tsv"

    base_rows = []
    diff_rows = []
    emp_rows = []

    summary = {
        "total": len(raw_records),
        "field_non_empty": Counter(),
        "date_non_empty": 0,
        "time_non_empty": 0,
        "support_stats": {f: Counter() for f in ["姓名", "身份证号", "相关企业", "任职", "参股", "截图日期", "截图时间"]},
        "changed_vs_old": Counter(),
        "changed_vs_majority": Counter(),
        "count_match": {"related": 0, "job": 0, "share": 0},
        "with_emp": 0,
    }

    for rec in raw_records:
        filename = Path(rec.get("filename", "")).name
        methods_data = rec.get("methods", {})
        if not isinstance(methods_data, dict):
            methods_data = {}

        # base fields
        field_values = {
            f: {m: normalize_space(methods_data.get(m, {}).get(f, "")) for m in METHODS}
            for f in ["姓名", "身份证号", "相关企业", "任职", "参股"]
        }

        weighted = {}
        weighted_support = {}
        weighted_score = {}
        weighted_rule_score = {}
        majority = {}

        for f in ["姓名", "身份证号", "相关企业", "任职", "参股"]:
            # 用同一字段进行加权
            selected = weighted_pick(f, field_values[f], methods_data)
            weighted[f] = selected.value
            weighted_support[f] = selected.methods
            weighted_score[f] = selected.score
            if selected.methods:
                rs = []
                for m in selected.methods:
                    rs.append(
                        _rule_quality_score(
                            _normalize_rule_meta(
                                methods_data.get(m, {})
                                .get("field_scores", {})
                                .get(f, {})
                                .get("provenance", {})
                            )
                        )
                    )
                weighted_rule_score[f] = sum(rs) / len(rs)
            else:
                weighted_rule_score[f] = 0.0

            # 朴素多数
            majority_vals = {}
            for m in METHODS:
                raw = field_values[f].get(m, "")
                if f in {"相关企业", "任职", "参股"}:
                    v = normalize_count(raw)
                else:
                    v = canonical_name(raw)
                if v:
                    majority_vals[m] = v
            majority[f] = majority_pick({})
            if majority_vals:
                votes = defaultdict(list)
                for m, v in majority_vals.items():
                    votes[v].append(m)
                majority[f] = sorted(votes.items(), key=lambda kv: (len(kv[1]), len(kv[0])), reverse=True)[0][0]

        # date/time from 任职情况信息
        date_votes: Dict[str, List[str]] = defaultdict(list)
        time_votes: Dict[str, List[str]] = defaultdict(list)
        for m in METHODS:
            d, t = extract_date_time(methods_data.get(m, {}).get("任职情况信息", ""))
            if d:
                date_votes[d].append(m)
            if t:
                time_votes[t].append(m)

        if date_votes:
            weighted["截图日期"] = sorted(date_votes.items(), key=lambda kv: (len(kv[1]), len(kv[0])), reverse=True)[0][0]
            weighted_support["截图日期"] = date_votes[weighted["截图日期"]]
            weighted_score["截图日期"] = 0.9
        else:
            weighted["截图日期"] = ""
            weighted_support["截图日期"] = []
            weighted_score["截图日期"] = 0.0

        if time_votes:
            weighted["截图时间"] = sorted(time_votes.items(), key=lambda kv: (len(kv[1]), len(kv[0])), reverse=True)[0][0]
            weighted_support["截图时间"] = time_votes[weighted["截图时间"]]
            weighted_score["截图时间"] = 0.9
        else:
            weighted["截图时间"] = ""
            weighted_support["截图时间"] = []
            weighted_score["截图时间"] = 0.0

        majority["截图日期"] = sorted(date_votes.items(), key=lambda kv: (len(kv[1]), len(kv[0])), reverse=True)[0][0] if date_votes else ""
        majority["截图时间"] = sorted(time_votes.items(), key=lambda kv: (len(kv[1]), len(kv[0])), reverse=True)[0][0] if time_votes else ""

        # 过滤明显异常：姓名被截断太短，尝试回退为最大长度非空
        if weighted["姓名"] and len(weighted["姓名"]) <= 2:
            fallback = max((normalize_space(v) for v in field_values["姓名"].values() if v), key=len, default="")
            if fallback and len(fallback) > len(weighted["姓名"]):
                weighted["姓名"] = fallback

        # employment table by file
        rows = emp_by_file.get(filename, [])
        clusters = cluster_emp_rows(rows)
        ordered = []
        for idx, cl in enumerate(clusters):
            c = consensus_emp(cl)
            c["file"] = filename
            c["seq"] = idx + 1
            ordered.append(c)

        related_true = len(ordered)
        job_true = sum(1 for r in ordered if r["position"])
        share_true = sum(1 for r in ordered if r["share"])

        rel_final = weighted["相关企业"] if weighted["相关企业"] else str(related_true)
        job_final = weighted["任职"] if weighted["任职"] else str(job_true)
        share_final = weighted["参股"] if weighted["参股"] else str(share_true)

        # baseline vs old report
        old_row = old_baseline.get(filename, {}) if old_baseline else {}

        for f in ["姓名", "身份证号", "相关企业", "任职", "参股", "截图日期", "截图时间"]:
            if old_row:
                if old_row.get(f, "") != weighted.get(f, ""):
                    summary["changed_vs_old"][f] += 1
                if old_row.get(f, "") != majority.get(f, ""):
                    summary["changed_vs_majority"][f] += 1

            if weighted.get(f, ""):
                summary["field_non_empty"][f] += 1
                summary["support_stats"][f][len(weighted_support.get(f, []))] += 1

        if weighted["截图日期"]:
            summary["date_non_empty"] += 1
        if weighted["截图时间"]:
            summary["time_non_empty"] += 1

        if str(rel_final) == str(related_true):
            summary["count_match"]["related"] += 1
        if str(job_final) == str(job_true):
            summary["count_match"]["job"] += 1
        if str(share_final) == str(share_true):
            summary["count_match"]["share"] += 1

        summary["with_emp"] += 1 if ordered else 0

        # record diff vs old baseline + majority
        for f in ["姓名", "身份证号", "相关企业", "任职", "参股", "截图日期", "截图时间"]:
            old_v = old_row.get(f, "") if old_row else ""
            maj_v = majority.get(f, "")
            new_v = weighted.get(f, "")
            if old_row and (old_v != new_v or old_v != maj_v):
                diff_rows.append(
                    [
                        filename,
                        f,
                        old_v,
                        new_v,
                        maj_v,
                        "|".join(sorted(weighted_support.get(f, []))),
                        f"{weighted_score.get(f,0.0):.4f}",
                        f"{weighted_rule_score.get(f,0.0):.4f}",
                    ]
                )

        base_rows.append(
            [
                filename,
                weighted["姓名"],
                weighted["身份证号"],
                weighted["截图日期"],
                weighted["截图时间"],
                rel_final,
                job_final,
                share_final,
                str(related_true),
                str(job_true),
                str(share_true),
                "1" if str(rel_final) == str(related_true) else "0",
                "1" if str(job_final) == str(job_true) else "0",
                "1" if str(share_final) == str(share_true) else "0",
                "|".join(sorted(weighted_support.get("姓名", []))),
                "|".join(sorted(weighted_support.get("身份证号", []))),
                "|".join(sorted(weighted_support.get("相关企业", []))),
                "|".join(sorted(weighted_support.get("任职", []))),
                "|".join(sorted(weighted_support.get("参股", []))),
                f"{weighted_rule_score.get('姓名', 0.0):.4f}",
                f"{weighted_rule_score.get('身份证号', 0.0):.4f}",
                f"{weighted_rule_score.get('相关企业', 0.0):.4f}",
                f"{weighted_rule_score.get('任职', 0.0):.4f}",
                f"{weighted_rule_score.get('参股', 0.0):.4f}",
            ]
        )

        for r in ordered:
            emp_rows.append(
                [
                    filename,
                    str(r["seq"]),
                    r["company"],
                    r["status"],
                    r["position"],
                    r["share"],
                    ",".join(r["support_engines"]),
                    str(len(r["support_engines"])),
                ]
            )

    # write outputs
    with base_out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(
            [
                "文件名",
                "姓名",
                "身份证号",
                "上传日期",
                "上传时间",
                "相关企业数",
                "任职企业数",
                "参股企业数",
                "相关企业数_任职表",
                "任职企业数_任职表",
                "参股企业数_任职表",
                "相关企业计数一致",
                "任职企业计数一致",
                "参股企业数一致",
                "姓名_支持引擎",
                "身份证号_支持引擎",
                "相关企业_支持引擎",
                "任职企业_支持引擎",
                "参股企业_支持引擎",
                "姓名_rule_score",
                "身份证号_rule_score",
                "相关企业_rule_score",
                "任职_rule_score",
                "参股_rule_score",
            ]
        )
        w.writerows(base_rows)

    with out_dir.joinpath("ocr_698_baseline_employment_weighted.tsv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow([
            "文件名",
            "序号",
            "企业名称",
            "经营状态",
            "承担职务",
            "持股比例",
            "出现引擎",
            "出现引擎数",
        ])
        w.writerows(emp_rows)

    with diff_out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["文件名", "字段", "old_baseline", "weighted", "majority", "weighted_support", "weighted_score", "weighted_rule_score"])
        w.writerows(diff_rows)

    lines = [
        "# 4 路加权基线策略（对照基线）",
        "",
        f"- 样本数: {summary['total']}",
        f"- 解析到任职表: {summary['with_emp']}",
        "",
        "## 字段覆盖率（加权）",
        "| 字段 | 有值样本 | 覆盖率 |",
        "| --- | ---: | ---: |",
    ]
    for f in ["姓名", "身份证号", "相关企业", "任职", "参股", "截图日期", "截图时间"]:
        lines.append(f"| {f} | {summary['field_non_empty'].get(f,0)} | {(summary['field_non_empty'].get(f,0)/summary['total']):.2%} |")

    lines.extend(
        [
            "",
            "## 与 old baseline 的差异字段计数",
            f"- 姓名: {summary['changed_vs_old'].get('姓名',0)}",
            f"- 身份证号: {summary['changed_vs_old'].get('身份证号',0)}",
            f"- 相关企业: {summary['changed_vs_old'].get('相关企业',0)}",
            f"- 任职: {summary['changed_vs_old'].get('任职',0)}",
            f"- 参股: {summary['changed_vs_old'].get('参股',0)}",
            f"- 截图日期: {summary['changed_vs_old'].get('截图日期',0)}",
            f"- 截图时间: {summary['changed_vs_old'].get('截图时间',0)}",
            "",
            "## 与多数决的差异字段计数",
            f"- 姓名: {summary['changed_vs_majority'].get('姓名',0)}",
            f"- 身份证号: {summary['changed_vs_majority'].get('身份证号',0)}",
            f"- 相关企业: {summary['changed_vs_majority'].get('相关企业',0)}",
            f"- 任职: {summary['changed_vs_majority'].get('任职',0)}",
            f"- 参股: {summary['changed_vs_majority'].get('参股',0)}",
            f"- 截图日期: {summary['changed_vs_majority'].get('截图日期',0)}",
            f"- 截图时间: {summary['changed_vs_majority'].get('截图时间',0)}",
            "",
            "## 计数一致率（与任职表结构统计）",
            f"- 相关企业一致率: {summary['count_match']['related']}/{summary['total']} ({summary['count_match']['related']/summary['total']:.2%})",
            f"- 任职一致率: {summary['count_match']['job']}/{summary['total']} ({summary['count_match']['job']/summary['total']:.2%})",
            f"- 参股一致率: {summary['count_match']['share']}/{summary['total']} ({summary['count_match']['share']/summary['total']:.2%})",
        ]
    )

    coverage_out.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    build()
