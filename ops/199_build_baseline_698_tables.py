#!/usr/bin/env python3
import csv
import json
import re
from collections import defaultdict, Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

METHODS = [
    "paddleocr_2_7_3",
    "rapidocr_2_7_3",
    "rapidocr_isolated",
    "paddleocr_3_x",
]


@dataclass
class EngineValue:
    raw: str
    norm: str
    support: str


def normalize_space(text: str) -> str:
    if text is None:
        return ""
    return str(text).replace("\u2013", "-").replace("\u2014", "-").strip()


def norm_field(text: str) -> str:
    s = normalize_space(text)
    s = re.sub(r"\s+", "", s)
    return s


def norm_for_date_time(text: str) -> str:
    s = normalize_space(text)
    s = s.replace("：", ":")
    s = s.replace("－", "-")
    return s


def extract_date_time(text: str) -> Tuple[str, str]:
    s = norm_for_date_time(text)
    if not s:
        return "", ""

    candidates: List[Tuple[str, str]] = []
    # 07-15 09:44 / 0715 09:44 / 07-1509:44
    for m in re.finditer(
        r"(?<!\d)(\d{2}[-/]?\d{2})(?:\D{0,8})(\d{2}:\d{2})(?!\d)",
        s,
    ):
        date_s = m.group(1).replace("/", "-")
        time_s = m.group(2)
        candidates.append((date_s, time_s))

    # fallback: 09:44 07-15
    if not candidates:
        for m in re.finditer(
            r"(\d{2}:\d{2})(?:\D{0,8})(\d{2}[-/]?\d{2})",
            s,
        ):
            date_s = m.group(2).replace("/", "-")
            time_s = m.group(1)
            candidates.append((date_s, time_s))

    if not candidates:
        return "", ""
    return candidates[0]


def normalize_count(raw: str) -> str:
    s = normalize_space(raw)
    if not s:
        return ""
    m = re.search(r"\d+", s)
    if not m:
        return ""
    return str(int(m.group(0)))


def canonical_number(raw: str) -> str:
    return normalize_count(raw)


def pick_consensus(values: Dict[str, List[str]]) -> Tuple[str, float, List[str]]:
    if not values:
        return "", 0.0, []
    counts = {k: len(vs) for k, vs in values.items() if k != ""}
    if not counts:
        return "", 0.0, []
    top_value = max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
    support_engines = values[top_value]
    conf = len(support_engines) / sum(len(vs) for vs in values.values())
    return top_value, conf, support_engines


def build_field_votes(raw_records: Dict[str, str]) -> Tuple[str, float, List[str], Dict[str, int]]:
    votes: Dict[str, List[str]] = defaultdict(list)
    for method, raw in raw_records.items():
        value = "" if raw is None else normalize_space(raw)
        if value == "":
            continue
        key = value
        votes[key].append(method)
    selected_norm, conf, support = pick_consensus(votes)
    return selected_norm, conf, support, {k: len(vs) for k, vs in votes.items()}


def build_field_votes_with_normalization(
    raw_records: Dict[str, str], normalize_fn
) -> Tuple[str, float, List[str], Dict[str, int]]:
    if normalize_fn is None:
        return build_field_votes(raw_records)

    votes: Dict[str, List[str]] = defaultdict(list)
    canonical_to_raw: Dict[str, str] = {}
    for method, raw in raw_records.items():
        value = "" if raw is None else normalize_fn(raw)
        if value == "":
            continue
        votes[value].append(method)
        canonical_to_raw.setdefault(value, normalize_space(raw))
    if not votes:
        return "", 0.0, [], {}

    top_norm, conf, support = pick_consensus(votes)
    # pick first raw form for this canonical value
    raw_selected = canonical_to_raw.get(top_norm, top_norm)
    return raw_selected, conf, support, {k: len(vs) for k, vs in votes.items()}


def parse_employment_tsv(path: Path) -> Dict[str, List[dict]]:
    groups: Dict[str, List[dict]] = defaultdict(list)
    if not path.exists():
        return groups
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            filename = normalize_space(row.get("文件名", ""))
            if not filename:
                continue
            groups[filename].append(
                {
                    "engine": normalize_space(row.get("引擎", "")),
                    "seq": normalize_space(row.get("序号", "")),
                    "company": normalize_space(row.get("企业名称", "")),
                    "status": normalize_space(row.get("经营状态", "")),
                    "position": normalize_space(row.get("承担职务", "")),
                    "share": normalize_space(row.get("持股比例", "")),
                }
            )
    return groups


def normalize_company(s: str) -> str:
    s = norm_field(s)
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


def most_common_non_empty(values: List[str]) -> Tuple[str, List[str], int]:
    if not values:
        return "", [], 0
    c = Counter(values)
    value, cnt = c.most_common(1)[0]
    return value, [v for v in values if v == value], cnt


def cluster_rows(rows: List[dict]) -> List[dict]:
    clusters: List[dict] = []
    for row in rows:
        company_key = normalize_company(row["company"])
        matched = None
        for idx, c in enumerate(clusters):
            if similar_company(company_key, c["key"]):
                matched = idx
                break
        if matched is None:
            clusters.append(
                {
                    "key": company_key,
                    "rows": [row],
                }
            )
        else:
            clusters[matched]["rows"].append(row)
    return clusters


def consensus_row(cluster: dict) -> Dict[str, object]:
    rows = cluster["rows"]
    support = sorted({r["engine"] for r in rows if r["engine"]})
    status_vals = [r["status"] for r in rows if r["status"]]
    pos_vals = [r["position"] for r in rows if r["position"]]
    share_vals = [r["share"] for r in rows if r["share"]]

    if status_vals:
        status, _, _ = most_common_non_empty(status_vals)
    else:
        status = ""
    if pos_vals:
        pos, _, _ = most_common_non_empty(pos_vals)
    else:
        pos = ""
    if share_vals:
        share, _, _ = most_common_non_empty(share_vals)
    else:
        share = ""
    # 取出现次数最高的一组名称作为实体名
    comp_vals = [r["company"] for r in rows if r["company"]]
    company, _, _ = most_common_non_empty(comp_vals)
    if not company:
        company = cluster["key"]
    return {
        "company": company,
        "status": status,
        "position": pos,
        "share": share,
        "support_engines": support,
        "support_count": len(support),
        "raw_rows": len(rows),
    }


def build_baseline():
    repo_root = Path(__file__).resolve().parent.parent
    flat_report = repo_root / "ops" / "reports" / "ocr_engine_698_record_flat.json"
    emp_tsv = repo_root / "ops" / "reports" / "ocr_698_form_employment.tsv"
    base_out = repo_root / "ops" / "reports" / "ocr_698_baseline_base_info.tsv"
    emp_out = repo_root / "ops" / "reports" / "ocr_698_baseline_employment.tsv"
    coverage_out = repo_root / "ops" / "reports" / "ocr_698_baseline_coverage_report.md"

    if not flat_report.exists():
        raise FileNotFoundError(f"missing {flat_report}")
    raw_records = json.loads(flat_report.read_text(encoding="utf-8"))

    emp_by_file = parse_employment_tsv(emp_tsv)
    all_files = [Path(r["filename"]).name for r in raw_records]
    assert len(all_files) == 698

    base_rows = []
    emp_rows_out = []
    summary = {
        "total": len(raw_records),
        "base_field_non_empty": defaultdict(int),
        "date_non_empty": 0,
        "time_non_empty": 0,
        "count_consistent": {
            "related": 0,
            "employment": 0,
            "share": 0,
        },
        "employment_by_file": 0,
        "consensus_support": {field: defaultdict(int) for field in ["姓名", "身份证号", "相关企业", "任职", "参股"]},
        "baseline_to_employment_mismatch": 0,
    }

    for rec in raw_records:
        filename = Path(rec["filename"]).name
        methods = rec.get("methods", {})

        # 1) field consensus (raw values)
        field_values = {}
        field_values["姓名"] = {m: methods.get(m, {}).get("姓名", "") for m in METHODS}
        field_values["身份证号"] = {m: methods.get(m, {}).get("身份证号", "") for m in METHODS}
        field_values["相关企业"] = {m: canonical_number(methods.get(m, {}).get("相关企业", "")) for m in METHODS}
        field_values["任职"] = {m: canonical_number(methods.get(m, {}).get("任职", "")) for m in METHODS}
        field_values["参股"] = {m: canonical_number(methods.get(m, {}).get("参股", "")) for m in METHODS}

        # date/time are extracted from 任职情况信息
        dt_votes = {}
        for m in METHODS:
            txt = methods.get(m, {}).get("任职情况信息", "")
            date_s, time_s = extract_date_time(txt)
            if date_s or time_s:
                dt_votes.setdefault(m, (date_s, time_s))

        dt_by_date: Dict[str, List[str]] = defaultdict(list)
        dt_by_time: Dict[str, List[str]] = defaultdict(list)
        for m, (d, t) in dt_votes.items():
            if d:
                dt_by_date[d].append(m)
            if t:
                dt_by_time[t].append(m)

        # consensus for base fields
        name_val, _, name_support, _ = build_field_votes(field_values["姓名"])
        id_val, _, id_support, _ = build_field_votes(field_values["身份证号"])
        rel_val, _, rel_support, _ = build_field_votes(field_values["相关企业"])
        job_val, _, job_support, _ = build_field_votes(field_values["任职"])
        share_val, _, share_support, _ = build_field_votes(field_values["参股"])

        date_val = ""
        if dt_by_date:
            date_val, _, _ = pick_consensus(dt_by_date)

        time_val = ""
        if dt_by_time:
            time_val, _, _ = pick_consensus(dt_by_time)

        # employment baseline
        raw_emp_rows = emp_by_file.get(filename, [])
        clusters = cluster_rows(raw_emp_rows)
        ordered = []
        for i, cl in enumerate(clusters):
            merged = consensus_row(cl)
            merged["file"] = filename
            merged["seq"] = i + 1
            ordered.append(merged)

        job_rows_count = len(ordered)
        job_rows_with_position = sum(1 for r in ordered if r["position"])
        share_rows_with_ratio = sum(1 for r in ordered if r["share"])

        # keep base counts as consensus first, and mark consistency
        rel_final = rel_val or str(job_rows_count)
        job_final = job_val or str(job_rows_with_position)
        share_final = share_val or str(share_rows_with_ratio)

        if str(rel_val) == str(job_rows_count):
            summary["count_consistent"]["related"] += 1
        if str(job_val) == str(job_rows_with_position):
            summary["count_consistent"]["employment"] += 1
        if str(share_val) == str(share_rows_with_ratio):
            summary["count_consistent"]["share"] += 1
        if rel_val or job_val or share_val:
            if str(rel_val) != str(job_rows_count) or str(job_val) != str(job_rows_with_position) or str(share_val) != str(share_rows_with_ratio):
                summary["baseline_to_employment_mismatch"] += 1

        # coverage counters
        summary["base_field_non_empty"]["姓名"] += 1 if name_val else 0
        summary["base_field_non_empty"]["身份证号"] += 1 if id_val else 0
        summary["base_field_non_empty"]["相关企业"] += 1 if rel_val else 0
        summary["base_field_non_empty"]["任职"] += 1 if job_val else 0
        summary["base_field_non_empty"]["参股"] += 1 if share_val else 0
        if date_val:
            summary["date_non_empty"] += 1
        if time_val:
            summary["time_non_empty"] += 1

        for field in summary["consensus_support"].keys():
            if field == "姓名":
                sup = len(name_support)
            elif field == "身份证号":
                sup = len(id_support)
            elif field == "相关企业":
                sup = len(rel_support)
            elif field == "任职":
                sup = len(job_support)
            else:
                sup = len(share_support)
            summary["consensus_support"][field][sup] += 1

        base_rows.append(
            [
                filename,
                name_val,
                id_val,
                date_val,
                time_val,
                rel_final,
                job_final,
                share_final,
                str(job_rows_count),
                str(job_rows_with_position),
                str(share_rows_with_ratio),
                "1" if str(rel_final) == str(job_rows_count) else "0",
                "1" if str(job_final) == str(job_rows_with_position) else "0",
                "1" if str(share_final) == str(share_rows_with_ratio) else "0",
                "|".join(name_support),
                "|".join(id_support),
                "|".join(rel_support),
                "|".join(job_support),
                "|".join(share_support),
            ]
        )

        for row in ordered:
            emp_rows_out.append(
                [
                    row["file"],
                    str(row["seq"]),
                    row["company"],
                    row["status"],
                    row["position"],
                    row["share"],
                    ",".join(row["support_engines"]),
                    str(row["support_count"]),
                    str(row["raw_rows"]),
                ]
            )

        summary["employment_by_file"] += 1 if ordered else 0

    base_header = [
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
        "参股企业计数一致",
        "姓名_支持引擎",
        "身份证号_支持引擎",
        "相关企业_支持引擎",
        "任职企业_支持引擎",
        "参股企业_支持引擎",
    ]
    with base_out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(base_header)
        w.writerows(base_rows)

    emp_header = [
        "文件名",
        "序号",
        "企业名称",
        "经营状态",
        "承担职务",
        "持股比例",
        "出现引擎",
        "出现引擎数",
        "共识支持行",
    ]
    with emp_out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(emp_header)
        w.writerows(emp_rows_out)

    # coverage report
    lines = [
        "# OCR 698 基准表生成报告",
        "",
        f"- 生成时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 基准样本: {summary['total']} 张",
        f"- 任职信息有任职行基线: {summary['employment_by_file']} 张",
        "",
        "## 基础字段一致覆盖率（基线字段）",
        "| 字段 | 有值样本 | 覆盖率 |",
        "| --- | ---: | ---: |",
    ]
    for field in ["姓名", "身份证号", "相关企业", "任职", "参股"]:
        val = summary["base_field_non_empty"][field]
        lines.append(f"| {field} | {val} | {val/summary['total']:.2%} |")
    lines.extend(
        [
            f"| 上传日期 | {summary['date_non_empty']} | {summary['date_non_empty']/summary['total']:.2%} |",
            f"| 上传时间 | {summary['time_non_empty']} | {summary['time_non_empty']/summary['total']:.2%} |",
            "| --- | --- | --- |",
            "",
            "## 共识支持度分布（支持引擎数）",
            "| 字段 | 支持1 | 支持2 | 支持3 | 支持4 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for field in ["姓名", "身份证号", "相关企业", "任职", "参股"]:
        c = summary["consensus_support"][field]
        lines.append(
            f"| {field} | {c.get(1,0)} | {c.get(2,0)} | {c.get(3,0)} | {c.get(4,0)} |"
        )
    lines.extend(
        [
            "",
            "## 计数一致性（基线字段 vs 任职基线行统计）",
            f"- 相关企业数一致: {summary['count_consistent']['related']} / {summary['total']} ({summary['count_consistent']['related']/summary['total']:.2%})",
            f"- 任职企业数一致: {summary['count_consistent']['employment']} / {summary['total']} ({summary['count_consistent']['employment']/summary['total']:.2%})",
            f"- 参股企业数一致: {summary['count_consistent']['share']} / {summary['total']} ({summary['count_consistent']['share']/summary['total']:.2%})",
            f"- 计数字段不一致样本数: {summary['baseline_to_employment_mismatch']}",
        ]
    )
    coverage_out.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    build_baseline()
