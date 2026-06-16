#!/usr/bin/env python3
"""Extract employment/company detail rows from the 4-way OCR flat result.

This is a first-pass, auditable extractor for the business-check corpus. It
does not call OCR engines. It consumes `raw_4way_flat.json`, extracts
method-level company candidates, aligns them into image-level consensus rows,
then merges by person key.
"""

from __future__ import annotations

import csv
import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN_DIR = ROOT / "ops" / "reports" / "aggressive_final698_regression_20260615"
RAW_JSON = DEFAULT_RUN_DIR / "raw_4way_flat.json"
OUT_DIR = DEFAULT_RUN_DIR / "employment_extraction"

METHODS = ["paddleocr_2_7_3", "rapidocr_2_7_3", "rapidocr_isolated", "paddleocr_3_x"]
STATUS_PATTERN = re.compile(
    r"(吊销[，,、]?\s*已注销|吊销[，,、]?\s*未注销|吊销|已注销|注销|存续|开业|在业|迁出|撤销|停业)"
)
NOISE_LINE_RE = re.compile(
    r"(投资任职信息|相关企业\s*\d|任职\s*\d|参股\s*\d|"
    r"经查询|15位身份证|15位号码|信息重合|供您参考|报告|预览|下载|"
    r"统一社会信用代码|企业类型|成立日期|注册币种|注册资本|登记机关|吊销日期|注销日期|人民币)"
)
COMPANY_HINT_RE = re.compile(
    r"(公司|有限|服务部|工作室|经营部|个体工商户|商行|中心|店|厂|合作社|事务所|"
    r"管理咨询|网络科技|文化传播|科技|围场)"
)
ROLE_FRAGMENT_RE = re.compile(
    r"(法定|代表人|负责人|经营者|执行事务|合伙人|首席代表|"
    r"代责人|岱代|代委人|货代责人|責人)"
)

ROLE_RULES = [
    ("法定代表人", "法定代表人（负责人、经营者、执行事务合伙人、首席代表）"),
    ("负责人", "法定代表人（负责人、经营者、执行事务合伙人、首席代表）"),
    ("经营者", "法定代表人（负责人、经营者、执行事务合伙人、首席代表）"),
    ("执行事务合伙人", "法定代表人（负责人、经营者、执行事务合伙人、首席代表）"),
    ("首席代表", "法定代表人（负责人、经营者、执行事务合伙人、首席代表）"),
    ("执行董事", "执行董事"),
    ("总经理", "总经理"),
    ("总经", "总经理"),
    ("监事", "监事"),
    ("股东", "股东"),
    ("投资人", "投资人"),
]


@dataclass
class Candidate:
    filename: str
    file_no: str
    person_name: str
    method: str
    candidate_index: int
    company: str
    status: str
    role: str
    share_ratio: str
    line_start: int
    line_end: int
    evidence: str
    related_count: str
    job_count: str
    share_count: str


def clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("丨", "|")
        .replace("｜", "|")
        .replace("【", "|")
        .replace("】", "|")
        .replace("■", "|")
        .replace("「", "|")
        .replace("」", "|")
        .replace("○", "0")
        .replace("．", ".")
        .replace("。", "。")
        .strip()
    )


def norm_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", clean_text(value)).strip()


def normalize_status(value: str) -> str:
    value = clean_text(value)
    compact = re.sub(r"\s+", "", value)
    if "吊销" in compact and "已注销" in compact:
        return "吊销，已注销"
    if "吊销" in compact and "未注销" in compact:
        return "吊销，未注销"
    if "已注销" in compact:
        return "已注销"
    for status in ["注销", "存续", "开业", "在业", "吊销", "迁出", "撤销", "停业"]:
        if status in value:
            return status
    return ""


def normalize_company_key(company: str) -> str:
    value = clean_text(company)
    value = re.sub(r"[\s|,，.。．…·、:：;；()（）\[\]【】<>《》\"'“”‘’_-]+", "", value)
    value = value.replace("有限责任公司", "有限公司")
    return value


def similar_company(a: str, b: str) -> bool:
    ka = normalize_company_key(a)
    kb = normalize_company_key(b)
    if not ka or not kb:
        return False
    if ka in kb or kb in ka:
        return min(len(ka), len(kb)) >= 4
    return SequenceMatcher(None, ka, kb).ratio() >= 0.82


def extract_file_no(filename: str) -> str:
    m = re.match(r"^(\d{6,})", filename)
    return m.group(1) if m else ""


def clean_company(raw: str) -> str:
    value = clean_text(raw)
    value = re.sub(r".*?供您参考[，。,.\s]*", "", value)
    value = re.split(r"[。；;]", value)[-1]
    value = re.split(r"[|\[\]<>]+", value)[-1]
    value = re.sub(r"^(相关企业|任职|参股)\s*\d*", "", value)
    value = re.sub(r"^[\s\d:：,，、.\-—_!！]+", "", value)
    value = re.sub(r"(经查询|在使用第一代|办理经营主体|存在与您).*", "", value)
    value = value.strip(" \t\r\n|,，.．。…·-—_")
    if len(value) > 52:
        value = value[-52:].strip()
    value = re.sub(r"^[^\u4e00-\u9fffA-Za-z0-9]+", "", value)
    return value.strip(" \t\r\n|,，.．。…·-—_")


def looks_like_company(value: str) -> bool:
    if not value or len(normalize_company_key(value)) < 3:
        return False
    has_company_hint = bool(COMPANY_HINT_RE.search(value))
    if ROLE_FRAGMENT_RE.search(value) and not has_company_hint:
        return False
    if NOISE_LINE_RE.search(value) and not has_company_hint:
        return False
    if re.fullmatch(r"[\d:：.\-\s]+", value):
        return False
    return bool(has_company_hint or len(normalize_company_key(value)) >= 6)


def split_lines(method_data: dict[str, Any]) -> list[str]:
    raw = method_data.get("原始OCR") or method_data.get("任职情况信息") or ""
    lines = [norm_spaces(x) for x in str(raw).splitlines()]
    if len(lines) <= 1:
        text = method_data.get("任职情况信息") or method_data.get("原始OCR") or ""
        lines = [norm_spaces(x) for x in re.split(r"\s{2,}|\n", str(text))]
    return [line for line in lines if line]


def is_masked_company_marker(line: str) -> bool:
    text = clean_text(line)
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return False
    if re.fullmatch(r"[*＊★☆·.。…—_\-]{2,}", compact):
        return True
    return bool(re.search(r"[*＊★]{2,}|…{2,}|\.{3,}", compact))


def masked_company_blocks(lines: list[str]) -> list[dict[str, str]]:
    """Find masked enterprise blocks such as "*** / 注销 / 法定代表人...".

    The block is only used as a counting placeholder. It intentionally does not
    invent a company name; the person-level merge later emits 未识别企业_NN rows
    only when the basic count says there are more enterprises than extracted
    named company rows.
    """

    blocks: list[dict[str, str]] = []
    for idx, line in enumerate(lines):
        if not is_masked_company_marker(line):
            continue
        window_lines = lines[idx : min(len(lines), idx + 6)]
        window = " ".join(window_lines)
        if not STATUS_PATTERN.search(window):
            continue
        blocks.append(
            {
                "status": normalize_status(window),
                "role": extract_role(window),
                "share_ratio": extract_share(window),
                "line_start": str(idx + 1),
                "line_end": str(idx + len(window_lines)),
                "evidence": window[:500],
                "company_mentions": company_mentions_from_evidence(window),
            }
        )
    return blocks


COMPANY_MENTION_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,48}?"
    r"(?:股份有限公司|有限责任公司|有限公司|合伙企业(?:（有[^\\s|，,;；]*)?|个人独资企业|服务部|经营部|门市|工作室|中心|厂|店))"
)


def company_mentions_from_evidence(evidence: str) -> list[str]:
    text = clean_text(evidence)
    text = re.sub(r"[*＊★☆·.。…—_\-|丨｜■□●]+", " ", text)
    mentions: list[str] = []
    for match in COMPANY_MENTION_RE.finditer(text):
        company = clean_company(match.group(1))
        if not looks_like_company(company):
            continue
        if any(similar_company(company, existing) for existing in mentions):
            continue
        mentions.append(company)
    return mentions


def find_company_hits(lines: list[str]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        search_start = 0
        for m in STATUS_PATTERN.finditer(line):
            prefix = line[search_start : m.start()]
            company = clean_company(prefix)
            if not looks_like_company(company) and idx > 0:
                company = clean_company(lines[idx - 1])
            if looks_like_company(company):
                hits.append(
                    {
                        "line": idx,
                        "status": normalize_status(m.group(1)),
                        "company": company,
                        "status_start": m.start(),
                        "status_end": m.end(),
                    }
                )
            search_start = m.end()
    return hits


def looks_like_boundary_company(value: str) -> bool:
    cleaned = clean_company(value)
    if not looks_like_company(cleaned):
        return False
    compact = cleaned.replace(" ", "")
    if any(role_word in compact for role_word, _ in ROLE_RULES):
        return False
    return bool(COMPANY_HINT_RE.search(cleaned))


def next_company_line(lines: list[str], start: int, stop: int) -> int | None:
    for idx in range(start, min(stop, len(lines))):
        if looks_like_boundary_company(lines[idx]):
            return idx
    return None


def extract_role(evidence: str) -> str:
    compact = clean_text(evidence).replace(" ", "")
    roles: list[str] = []
    for needle, role in ROLE_RULES:
        if needle in compact and role not in roles:
            roles.append(role)
    if "执行董事" in roles and "总经理" in roles:
        roles = [r for r in roles if r not in {"执行董事", "总经理"}] + ["执行董事总经理"]
    return "；".join(roles)


def extract_share(evidence: str) -> str:
    text = clean_text(evidence)
    patterns = [
        r"参股\s*[:：]?\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*%?",
        r"参\s*股\s*[:：]?\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*%?",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    # Paddle sometimes places the number immediately after a wrapped role line.
    if "参股" in text:
        tail = text.split("参股", 1)[1]
        m = re.search(r"([0-9]{1,3}(?:\.[0-9]+)?)", tail)
        if m:
            return m.group(1)
    return ""


def choose_value(values: list[str], prefer_long: bool = False) -> str:
    values = [v for v in values if clean_text(v)]
    if not values:
        return ""
    counts = Counter(values)
    if prefer_long:
        return sorted(counts, key=lambda v: (counts[v], len(normalize_company_key(v))), reverse=True)[0]
    return sorted(counts, key=lambda v: (counts[v], len(str(v))), reverse=True)[0]


def field_status(values: list[str], field: str) -> str:
    values = [clean_text(v) for v in values if clean_text(v)]
    if not values:
        return "缺失"
    if field == "企业名称":
        keys = {normalize_company_key(v) for v in values if normalize_company_key(v)}
    else:
        keys = {norm_spaces(v) for v in values if norm_spaces(v)}
    return "一致" if len(keys) <= 1 else "冲突"


def merge_field_status(rows: list[dict[str, str]], field: str) -> str:
    status_field = f"{field}状态"
    statuses = {clean_text(r.get(status_field, "")) for r in rows if clean_text(r.get(status_field, ""))}
    if "冲突" in statuses:
        return "冲突"
    if "证据补齐" in statuses or "占位" in statuses:
        return "证据补齐"
    value_status = field_status([r.get(field, "") for r in rows], field)
    if value_status == "冲突":
        return "冲突"
    if value_status == "缺失":
        return "缺失"
    return "一致"


def method_candidates(filename: str, method: str, method_data: dict[str, Any], consensus_fields: dict[str, Any]) -> list[Candidate]:
    lines = split_lines(method_data)
    hits = find_company_hits(lines)
    candidates: list[Candidate] = []
    file_no = extract_file_no(filename)
    person_name = str(consensus_fields.get("姓名") or method_data.get("姓名") or "").strip()

    for i, hit in enumerate(hits):
        line_start = int(hit["line"])
        next_line = int(hits[i + 1]["line"]) if i + 1 < len(hits) else min(len(lines), line_start + 6)
        company_boundary = next_company_line(lines, line_start + 1, next_line)
        if company_boundary is None:
            company_boundary = next_company_line(lines, line_start + 1, min(len(lines), line_start + 6))
        line_end = max(line_start + 1, min(len(lines), company_boundary if company_boundary is not None else next_line))
        evidence_lines = lines[line_start:line_end]
        evidence = " ".join(evidence_lines)
        candidates.append(
            Candidate(
                filename=filename,
                file_no=file_no,
                person_name=person_name,
                method=method,
                candidate_index=len(candidates) + 1,
                company=str(hit["company"]),
                status=str(hit["status"]),
                role=extract_role(evidence),
                share_ratio=extract_share(evidence),
                line_start=line_start + 1,
                line_end=line_end,
                evidence=evidence[:500],
                related_count=str(consensus_fields.get("相关企业", "")),
                job_count=str(consensus_fields.get("任职", "")),
                share_count=str(consensus_fields.get("参股", "")),
            )
        )
    return candidates


def group_candidates(candidates: list[Candidate]) -> list[list[Candidate]]:
    groups: list[list[Candidate]] = []
    for cand in candidates:
        placed = False
        for group in groups:
            if any(similar_company(cand.company, item.company) for item in group):
                group.append(cand)
                placed = True
                break
        if not placed:
            groups.append([cand])
    return groups


def consensus_from_group(group: list[Candidate]) -> dict[str, str]:
    method_count = len({c.method for c in group})

    def status_for(values: list[str], field: str) -> str:
        status = field_status(values, field)
        if status == "一致" and method_count == 1:
            return "证据补齐"
        return status

    return {
        "企业名称": choose_value([c.company for c in group], prefer_long=True),
        "企业名称状态": status_for([c.company for c in group], "企业名称"),
        "经营状态": choose_value([c.status for c in group]),
        "经营状态状态": status_for([c.status for c in group], "经营状态"),
        "承担职务": choose_value([c.role for c in group]),
        "承担职务状态": status_for([c.role for c in group], "承担职务"),
        "持股比例": choose_value([c.share_ratio for c in group]),
        "持股比例状态": status_for([c.share_ratio for c in group], "持股比例"),
        "支持方案": "|".join(sorted({c.method for c in group})),
        "支持数": str(method_count),
        "来源文件": "|".join(sorted({c.filename for c in group})),
        "证据": " || ".join(c.evidence for c in group[:3])[:1000],
    }


def row_from_candidate(c: Candidate) -> dict[str, str]:
    return {
        "文件名": c.filename,
        "文件编号": c.file_no,
        "姓名": c.person_name,
        "方案": c.method,
        "候选序号": str(c.candidate_index),
        "企业名称": c.company,
        "经营状态": c.status,
        "承担职务": c.role,
        "持股比例": c.share_ratio,
        "行起": str(c.line_start),
        "行止": str(c.line_end),
        "相关企业数_基础": c.related_count,
        "任职企业数_基础": c.job_count,
        "参股企业数_基础": c.share_count,
        "证据文本": c.evidence,
    }


def write_tsv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def build_employment_outputs(
    raw_json: Path | str = RAW_JSON,
    out_dir: Path | str = OUT_DIR,
    dashboard_dir: Path | str | None = None,
) -> dict[str, Any]:
    raw_json = Path(raw_json)
    out_dir = Path(out_dir)
    dashboard_dir = Path(dashboard_dir) if dashboard_dir else None

    data = json.loads(raw_json.read_text(encoding="utf-8"))
    candidate_rows: list[dict[str, str]] = []
    image_rows: list[dict[str, str]] = []
    person_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    basic_by_person: dict[str, dict[str, int]] = defaultdict(lambda: {"相关企业": 0, "任职": 0, "参股": 0, "图片数": 0})
    files_by_person: dict[str, set[str]] = defaultdict(set)
    masked_by_person: dict[str, list[dict[str, str]]] = defaultdict(list)

    for rec in data:
        filename = Path(str(rec.get("filename") or "")).name
        weighted = rec.get("weighted_consensus", {}) or {}
        fields = weighted.get("fields", {}) or {}
        file_no = extract_file_no(filename)
        name = str(fields.get("姓名") or "").strip()
        person_key = f"{file_no}-{name}" if file_no else name or filename
        files_by_person[person_key].add(filename)
        for field in ["相关企业", "任职", "参股"]:
            try:
                basic_by_person[person_key][field] = max(basic_by_person[person_key][field], int(str(fields.get(field) or 0)))
            except ValueError:
                pass
        basic_by_person[person_key]["图片数"] += 1

        all_candidates: list[Candidate] = []
        for method in METHODS:
            md = (rec.get("methods") or {}).get(method) or {}
            lines = split_lines(md)
            for block in masked_company_blocks(lines):
                block = dict(block)
                block["method"] = method
                block["filename"] = filename
                masked_by_person[person_key].append(block)
            cands = method_candidates(filename, method, md, fields)
            all_candidates.extend(cands)
            candidate_rows.extend(row_from_candidate(c) for c in cands)

        for idx, group in enumerate(group_candidates(all_candidates), 1):
            row = {
                "文件名": filename,
                "文件编号": file_no,
                "姓名": name,
                "图片企业序号": str(idx),
                **consensus_from_group(group),
            }
            image_rows.append(row)
            person_groups[person_key].append(row)

    person_rows: list[dict[str, str]] = []
    reconcile_rows: list[dict[str, str]] = []
    for person_key, rows in sorted(person_groups.items()):
        groups: list[list[dict[str, str]]] = []
        for row in rows:
            placed = False
            for group in groups:
                if any(similar_company(row["企业名称"], item["企业名称"]) for item in group):
                    group.append(row)
                    placed = True
                    break
            if not placed:
                groups.append([row])

        related_detected = len(groups)
        job_detected = 0
        share_detected = 0
        file_no, _, name = person_key.partition("-")
        for idx, group in enumerate(groups, 1):
            company = choose_value([r["企业名称"] for r in group], prefer_long=True)
            status = choose_value([r["经营状态"] for r in group])
            role = choose_value([r["承担职务"] for r in group])
            share = choose_value([r["持股比例"] for r in group])
            if role:
                job_detected += 1
            if share:
                share_detected += 1
            person_rows.append(
                {
                    "人员键": person_key,
                    "文件编号": file_no,
                    "姓名": name,
                    "企业序号": str(idx),
                    "企业名称": company,
                    "企业名称状态": merge_field_status(group, "企业名称"),
                    "经营状态": status,
                    "经营状态状态": merge_field_status(group, "经营状态"),
                    "承担职务": role,
                    "承担职务状态": merge_field_status(group, "承担职务"),
                    "持股比例": share,
                    "持股比例状态": merge_field_status(group, "持股比例"),
                    "来源图片数": str(len({r["文件名"] for r in group})),
                    "来源文件": "|".join(sorted({r["文件名"] for r in group})),
                    "支持方案": "|".join(sorted(set("|".join(r["支持方案"] for r in group).split("|")) - {""})),
                    "证据": " || ".join(r["证据"] for r in group[:2])[:1000],
                }
            )

        basic = basic_by_person.get(person_key, {})
        basic_related = int(basic.get("相关企业", 0) or 0)
        basic_job = int(basic.get("任职", 0) or 0)
        basic_share = int(basic.get("参股", 0) or 0)
        masked_blocks = masked_by_person.get(person_key, [])
        missing_related = max(0, basic_related - related_detected)
        missing_job = max(0, basic_job - job_detected)
        missing_share = max(0, basic_share - share_detected)

        if missing_related > 0 and masked_blocks:
            status_default = choose_value([b.get("status", "") for b in masked_blocks])
            role_default = choose_value([b.get("role", "") for b in masked_blocks]) or "法定代表人（负责人、经营者、执行事务合伙人、首席代表）"
            source_files = "|".join(sorted(files_by_person.get(person_key, set())))
            used_company_names = [r.get("企业名称", "") for r in person_rows if r.get("人员键") == person_key]
            evidence_company_names: list[str] = []
            for block in masked_blocks:
                for company in block.get("company_mentions", []) or []:
                    if any(similar_company(company, used) for used in used_company_names):
                        continue
                    if any(similar_company(company, used) for used in evidence_company_names):
                        continue
                    evidence_company_names.append(company)
            for offset in range(missing_related):
                block = masked_blocks[offset % len(masked_blocks)]
                should_count_job = offset < missing_job
                should_count_share = offset < missing_share
                company = evidence_company_names[offset] if offset < len(evidence_company_names) else f"未识别企业_{offset + 1:02d}"
                company_status = "证据补齐" if offset < len(evidence_company_names) else "占位"
                person_rows.append(
                    {
                        "人员键": person_key,
                        "文件编号": file_no,
                        "姓名": name,
                        "企业序号": str(related_detected + offset + 1),
                        "企业名称": company,
                        "企业名称状态": company_status,
                        "经营状态": block.get("status") or status_default,
                        "经营状态状态": "一致" if (block.get("status") or status_default) else "缺失",
                        "承担职务": (block.get("role") or role_default) if should_count_job else "",
                        "承担职务状态": "占位" if should_count_job else "缺失",
                        "持股比例": (block.get("share_ratio") or "未识别") if should_count_share else "",
                        "持股比例状态": "占位" if should_count_share else "缺失",
                        "来源图片数": str(len(files_by_person.get(person_key, set()))),
                        "来源文件": source_files,
                        "支持方案": block.get("method", "masked_placeholder"),
                        "证据": block.get("evidence", "")[:1000],
                    }
                )
            related_detected += missing_related
            job_detected += min(missing_job, missing_related)
            share_detected += min(missing_share, missing_related)

        reconcile_rows.append(
            {
                "人员键": person_key,
                "文件编号": file_no,
                "姓名": name,
                "图片数": str(basic.get("图片数", 0)),
                "相关企业数_基础": str(basic.get("相关企业", 0)),
                "相关企业数_明细": str(related_detected),
                "相关企业数_一致": "是" if basic.get("相关企业", 0) == related_detected else "否",
                "任职企业数_基础": str(basic.get("任职", 0)),
                "任职企业数_明细": str(job_detected),
                "任职企业数_一致": "是" if basic.get("任职", 0) == job_detected else "否",
                "参股企业数_基础": str(basic.get("参股", 0)),
                "参股企业数_明细": str(share_detected),
                "参股企业数_一致": "是" if basic.get("参股", 0) == share_detected else "否",
            }
        )

    candidate_cols = [
        "文件名",
        "文件编号",
        "姓名",
        "方案",
        "候选序号",
        "企业名称",
        "经营状态",
        "承担职务",
        "持股比例",
        "行起",
        "行止",
        "相关企业数_基础",
        "任职企业数_基础",
        "参股企业数_基础",
        "证据文本",
    ]
    image_cols = [
        "文件名",
        "文件编号",
        "姓名",
        "图片企业序号",
        "企业名称",
        "企业名称状态",
        "经营状态",
        "经营状态状态",
        "承担职务",
        "承担职务状态",
        "持股比例",
        "持股比例状态",
        "支持方案",
        "支持数",
        "来源文件",
        "证据",
    ]
    person_cols = [
        "人员键",
        "文件编号",
        "姓名",
        "企业序号",
        "企业名称",
        "企业名称状态",
        "经营状态",
        "经营状态状态",
        "承担职务",
        "承担职务状态",
        "持股比例",
        "持股比例状态",
        "来源图片数",
        "来源文件",
        "支持方案",
        "证据",
    ]
    reconcile_cols = [
        "人员键",
        "文件编号",
        "姓名",
        "图片数",
        "相关企业数_基础",
        "相关企业数_明细",
        "相关企业数_一致",
        "任职企业数_基础",
        "任职企业数_明细",
        "任职企业数_一致",
        "参股企业数_基础",
        "参股企业数_明细",
        "参股企业数_一致",
    ]

    write_tsv(out_dir / "employment_candidate_rows.tsv", candidate_rows, candidate_cols)
    write_tsv(out_dir / "employment_image_consensus_rows.tsv", image_rows, image_cols)
    write_tsv(out_dir / "employment_person_consensus_rows.tsv", person_rows, person_cols)
    write_tsv(out_dir / "employment_reconcile_summary.tsv", reconcile_rows, reconcile_cols)

    employment_payload = {
        "meta": {
            "raw_json": str(raw_json),
            "out_dir": str(out_dir),
            "placeholder_policy": "masked_enterprise_count_fill",
        },
        "candidate_rows": candidate_rows,
        "image_rows": image_rows,
        "person_rows": person_rows,
        "reconcile_rows": reconcile_rows,
    }
    employment_js = "window.OCR_EMPLOYMENT = " + json.dumps(employment_payload, ensure_ascii=False, indent=2) + ";\n"
    (out_dir / "employment_data.js").write_text(employment_js, encoding="utf-8")
    if dashboard_dir:
        dashboard_dir.mkdir(parents=True, exist_ok=True)
        (dashboard_dir / "employment_data.js").write_text(employment_js, encoding="utf-8")

    total_people = len(reconcile_rows)
    any_detail = sum(1 for r in reconcile_rows if int(r["相关企业数_基础"] or 0) > 0)
    all_count_ok = sum(
        1
        for r in reconcile_rows
        if r["相关企业数_一致"] == "是" and r["任职企业数_一致"] == "是" and r["参股企业数_一致"] == "是"
    )
    positive_all_count_ok = sum(
        1
        for r in reconcile_rows
        if int(r["相关企业数_基础"] or 0) > 0
        and r["相关企业数_一致"] == "是"
        and r["任职企业数_一致"] == "是"
        and r["参股企业数_一致"] == "是"
    )
    print(f"candidate_rows={len(candidate_rows)}")
    print(f"image_consensus_rows={len(image_rows)}")
    print(f"person_consensus_rows={len(person_rows)}")
    print(f"people={total_people}")
    print(f"people_with_related_count_gt0={any_detail}")
    print(f"people_all_count_ok={all_count_ok}/{total_people}")
    print(f"positive_people_all_count_ok={positive_all_count_ok}/{any_detail if any_detail else 1}")
    print(f"out_dir={out_dir}")
    if dashboard_dir:
        print(f"dashboard_employment_js={dashboard_dir / 'employment_data.js'}")
    return employment_payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract employment/company rows from OCR flat JSON.")
    parser.add_argument("--raw-json", default=str(RAW_JSON))
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    parser.add_argument("--dashboard-dir", default="")
    args = parser.parse_args()
    build_employment_outputs(args.raw_json, args.out_dir, args.dashboard_dir or None)


if __name__ == "__main__":
    main()
