from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


BASELINE_TYPES = {"snapshot", "candidate", "golden"}


class BaselineError(ValueError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _read_js_var(path: Path, var_name: str, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return default
    marker = f"window.{var_name}"
    idx = text.find(marker)
    if idx < 0:
        return default
    eq_idx = text.find("=", idx)
    if eq_idx < 0:
        return default
    payload = text[eq_idx + 1 :].strip()
    if payload.endswith(";"):
        payload = payload[:-1].strip()
    try:
        return json.loads(payload)
    except Exception:
        return default


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _extract_file_no(filename: Any) -> str:
    m = re.match(r"^(\d{6,})", Path(_to_text(filename)).name)
    return m.group(1) if m else ""


def _extract_filename_name(filename: Any) -> str:
    stem = Path(_to_text(filename)).name.rsplit(".", 1)[0]
    parts = re.split(r"[-_+\s]+", stem)
    candidates: List[str] = []
    for part in parts:
        part = re.sub(r"^\d{6,20}", "", part)
        for chunk in re.findall(r"[\u4e00-\u9fff]{2,6}", part):
            if not any(token in chunk for token in ["经商", "企业", "新增", "投资", "任职", "图片", "截图"]):
                candidates.append(chunk)
    return candidates[0] if candidates else ""


def _record_key(file_no: str, name: str, masked_id: str, filename: str) -> str:
    if file_no and name:
        return f"file_name:{file_no}|||{name}"
    if masked_id and name:
        return f"id_name:{masked_id}|||{name}"
    if file_no:
        return f"file:{file_no}"
    return f"filename:{filename}"


def _normalize_company_key(company: Any) -> str:
    value = _to_text(company)
    value = re.sub(r"[\s|,，.。．…·、:：;；()（）\[\]【】<>《》\"'“”‘’_-]+", "", value)
    return value.replace("有限责任公司", "有限公司")


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    for key in [
        "engine_versions",
        "metadata",
        "fields",
        "evidence_summary",
        "baseline_evidence",
        "current_evidence",
        "summary",
    ]:
        if key in data:
            data[key] = _json_loads(data[key], {} if data[key] else {})
    return data


class BaselineStore:
    def __init__(self, db_path: Path, runs_root: Path):
        self.db_path = Path(db_path)
        self.runs_root = Path(runs_root)

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS baseline_versions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    parent_id TEXT,
                    source_task_id TEXT,
                    source_task_name TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    is_active_golden INTEGER NOT NULL DEFAULT 0,
                    record_count INTEGER NOT NULL DEFAULT 0,
                    image_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    created_by TEXT,
                    code_version TEXT,
                    harness_version TEXT,
                    engine_versions TEXT,
                    metadata TEXT,
                    notes TEXT
                );

                CREATE TABLE IF NOT EXISTS baseline_records (
                    id TEXT PRIMARY KEY,
                    baseline_id TEXT NOT NULL,
                    record_index INTEGER NOT NULL,
                    record_key TEXT NOT NULL,
                    file_id TEXT,
                    file_name TEXT,
                    file_name_from_filename TEXT,
                    person_name TEXT,
                    masked_id TEXT,
                    capture_date TEXT,
                    capture_time TEXT,
                    capture_datetime TEXT,
                    related_count TEXT,
                    employment_count TEXT,
                    shareholding_count TEXT,
                    review_status TEXT,
                    fields TEXT,
                    evidence_summary TEXT,
                    FOREIGN KEY (baseline_id) REFERENCES baseline_versions(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_baseline_records_baseline
                    ON baseline_records(baseline_id);
                CREATE INDEX IF NOT EXISTS idx_baseline_records_key
                    ON baseline_records(baseline_id, record_key);

                CREATE TABLE IF NOT EXISTS baseline_employment_rows (
                    id TEXT PRIMARY KEY,
                    baseline_id TEXT NOT NULL,
                    baseline_record_id TEXT,
                    row_index INTEGER NOT NULL,
                    company_key TEXT,
                    company_name TEXT,
                    business_status TEXT,
                    role TEXT,
                    share_ratio TEXT,
                    source_image TEXT,
                    row_status TEXT,
                    evidence_summary TEXT,
                    FOREIGN KEY (baseline_id) REFERENCES baseline_versions(id) ON DELETE CASCADE,
                    FOREIGN KEY (baseline_record_id) REFERENCES baseline_records(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_baseline_employment_baseline
                    ON baseline_employment_rows(baseline_id);

                CREATE TABLE IF NOT EXISTS manual_review_actions (
                    id TEXT PRIMARY KEY,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    reason TEXT,
                    operator TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )

    def list_baselines(self) -> List[Dict[str, Any]]:
        self.init_db()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM baseline_versions
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def get_baseline(self, baseline_id: str, include_records: bool = True) -> Dict[str, Any]:
        self.init_db()
        with self.connect() as conn:
            version = conn.execute("SELECT * FROM baseline_versions WHERE id = ?", (baseline_id,)).fetchone()
            if not version:
                raise BaselineError("baseline not found")
            payload = _row_to_dict(version)
            if include_records:
                records = conn.execute(
                    "SELECT * FROM baseline_records WHERE baseline_id = ? ORDER BY record_index",
                    (baseline_id,),
                ).fetchall()
                employment = conn.execute(
                    "SELECT * FROM baseline_employment_rows WHERE baseline_id = ? ORDER BY row_index",
                    (baseline_id,),
                ).fetchall()
                payload["records"] = [_row_to_dict(row) for row in records]
                payload["employment_rows"] = [_row_to_dict(row) for row in employment]
        return payload

    def create_from_task(
        self,
        task: Dict[str, Any],
        name: str = "",
        baseline_type: str = "snapshot",
        notes: str = "",
        created_by: str = "",
    ) -> Dict[str, Any]:
        self.init_db()
        baseline_type = _to_text(baseline_type) or "snapshot"
        if baseline_type not in BASELINE_TYPES:
            raise BaselineError(f"unsupported baseline type: {baseline_type}")
        if task.get("status") != "completed":
            raise BaselineError("only completed tasks can be saved as baseline")

        task_dir = Path(_to_text(task.get("output_dir")) or self.runs_root / _to_text(task.get("id")))
        if not task_dir.exists():
            raise BaselineError("task output directory not found")

        records = self._load_task_records(task_dir)
        if not records:
            raise BaselineError("task has no structured records to save")

        employment_rows = self._load_employment_rows(task_dir)
        baseline_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        baseline_name = name.strip() or f"{task.get('name') or task.get('id')} baseline"
        engine_versions = self._engine_versions(task)
        metadata = {
            "source_task": {
                "id": task.get("id"),
                "name": task.get("name"),
                "created_at": task.get("created_at"),
                "completed_at": task.get("completed_at"),
                "dashboard_url": task.get("dashboard_url"),
            },
            "image_count": int(task.get("files_total") or len(task.get("input_files") or [])),
            "record_count": len(records),
            "strategy": "4ocr_weighted_consensus+harness",
        }

        record_ids_by_person_key: Dict[str, str] = {}
        record_rows = []
        for idx, rec in enumerate(records, start=1):
            fields = self._consensus_fields(rec)
            filename = _to_text(rec.get("filename") or rec.get("file_name"))
            file_no = _to_text(rec.get("file_no")) or _extract_file_no(filename)
            file_name_from_filename = _to_text(rec.get("file_name_from_filename")) or _extract_filename_name(filename)
            person_name = _to_text(fields.get("姓名") or rec.get("person_name") or rec.get("姓名") or file_name_from_filename)
            masked_id = _to_text(fields.get("身份证号") or rec.get("masked_id") or rec.get("身份证号"))
            capture_date = _to_text(fields.get("截图日期") or rec.get("截图日期"))
            capture_time = _to_text(fields.get("截图时间") or rec.get("截图时间"))
            record_key = _record_key(file_no, person_name, masked_id, filename)
            record_id = f"{baseline_id}_r{idx:06d}"
            person_key = f"{file_no}-{person_name}" if file_no and person_name else record_key
            record_ids_by_person_key[person_key] = record_id
            record_rows.append(
                (
                    record_id,
                    baseline_id,
                    idx,
                    record_key,
                    file_no,
                    filename,
                    file_name_from_filename,
                    person_name,
                    masked_id,
                    capture_date,
                    capture_time,
                    " ".join(x for x in [capture_date, capture_time] if x),
                    _to_text(fields.get("相关企业") or rec.get("相关企业")),
                    _to_text(fields.get("任职") or rec.get("任职")),
                    _to_text(fields.get("参股") or rec.get("参股")),
                    _to_text(rec.get("review_status")) or "unreviewed",
                    _json_dumps(fields),
                    _json_dumps(self._record_evidence(rec)),
                )
            )

        employment_inserts = []
        for idx, row in enumerate(employment_rows, start=1):
            person_key = _to_text(row.get("人员键"))
            if not person_key:
                file_no = _to_text(row.get("文件编号"))
                name = _to_text(row.get("姓名"))
                person_key = f"{file_no}-{name}" if file_no and name else ""
            baseline_record_id = record_ids_by_person_key.get(person_key)
            company_name = _to_text(row.get("企业名称"))
            row_status = self._employment_row_status(row)
            employment_inserts.append(
                (
                    f"{baseline_id}_e{idx:06d}",
                    baseline_id,
                    baseline_record_id,
                    idx,
                    _normalize_company_key(company_name),
                    company_name,
                    _to_text(row.get("经营状态")),
                    _to_text(row.get("承担职务")),
                    _to_text(row.get("持股比例")),
                    _to_text(row.get("来源文件") or row.get("文件名")),
                    row_status,
                    _json_dumps(row),
                )
            )

        with self.connect() as conn:
            if baseline_type == "golden":
                conn.execute("UPDATE baseline_versions SET is_active_golden = 0 WHERE is_active_golden = 1")
            conn.execute(
                """
                INSERT INTO baseline_versions (
                    id, name, type, parent_id, source_task_id, source_task_name, status,
                    is_active_golden, record_count, image_count, created_at, created_by,
                    code_version, harness_version, engine_versions, metadata, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    baseline_id,
                    baseline_name,
                    baseline_type,
                    None,
                    _to_text(task.get("id")),
                    _to_text(task.get("name")),
                    "active",
                    1 if baseline_type == "golden" else 0,
                    len(record_rows),
                    int(metadata["image_count"]),
                    _now(),
                    created_by,
                    "",
                    "4ocr_weighted_consensus+harness",
                    _json_dumps(engine_versions),
                    _json_dumps(metadata),
                    notes,
                ),
            )
            conn.executemany(
                """
                INSERT INTO baseline_records (
                    id, baseline_id, record_index, record_key, file_id, file_name,
                    file_name_from_filename, person_name, masked_id, capture_date,
                    capture_time, capture_datetime, related_count, employment_count,
                    shareholding_count, review_status, fields, evidence_summary
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                record_rows,
            )
            if employment_inserts:
                conn.executemany(
                    """
                    INSERT INTO baseline_employment_rows (
                        id, baseline_id, baseline_record_id, row_index, company_key,
                        company_name, business_status, role, share_ratio, source_image,
                        row_status, evidence_summary
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    employment_inserts,
                )

        return self.get_baseline(baseline_id, include_records=False)

    def _load_task_records(self, task_dir: Path) -> List[Dict[str, Any]]:
        raw_records = _read_json(task_dir / "raw_4way_flat.json", None)
        if isinstance(raw_records, list):
            return [r for r in raw_records if isinstance(r, dict)]
        report = _read_js_var(task_dir / "dashboard" / "data.js", "OCR_REPORT", {})
        rows = report.get("rows") if isinstance(report, dict) else []
        return [r for r in rows if isinstance(r, dict)]

    def _load_employment_rows(self, task_dir: Path) -> List[Dict[str, Any]]:
        payload = _read_js_var(task_dir / "dashboard" / "employment_data.js", "OCR_EMPLOYMENT", {})
        if not isinstance(payload, dict):
            payload = _read_js_var(task_dir / "employment_extraction" / "employment_data.js", "OCR_EMPLOYMENT", {})
        rows = payload.get("person_rows") if isinstance(payload, dict) else []
        return [r for r in rows if isinstance(r, dict)]

    def _consensus_fields(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        consensus = rec.get("weighted_consensus")
        if isinstance(consensus, dict) and isinstance(consensus.get("fields"), dict):
            return dict(consensus["fields"])
        fields: Dict[str, Any] = {}
        for key in ["姓名", "身份证号", "截图日期", "截图时间", "相关企业", "任职", "参股", "查询结论"]:
            if key in rec:
                fields[key] = rec.get(key)
        return fields

    def _record_evidence(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        consensus = rec.get("weighted_consensus") if isinstance(rec.get("weighted_consensus"), dict) else {}
        methods = rec.get("methods") if isinstance(rec.get("methods"), dict) else {}
        method_summary: Dict[str, Any] = {}
        for method, data in methods.items():
            if not isinstance(data, dict):
                continue
            method_summary[method] = {
                "error": data.get("error", ""),
                "time_ms": data.get("time_ms", 0),
                "line_count": data.get("line_count", 0),
                "method_score": data.get("method_score", {}),
            }
        return {
            "support": consensus.get("support", {}) if isinstance(consensus, dict) else {},
            "scores": consensus.get("scores", {}) if isinstance(consensus, dict) else {},
            "sources": consensus.get("sources", {}) if isinstance(consensus, dict) else {},
            "issues": rec.get("issues", []),
            "method_summary": method_summary,
        }

    def _engine_versions(self, task: Dict[str, Any]) -> Dict[str, Any]:
        versions: Dict[str, Any] = {}
        for item in task.get("engine_runs") or []:
            if not isinstance(item, dict):
                continue
            engine = _to_text(item.get("engine"))
            if not engine:
                continue
            versions[engine] = {
                "runner_engine": item.get("runner_engine", ""),
                "python": item.get("python", ""),
                "returncode": item.get("returncode", ""),
                "elapsed_sec": item.get("elapsed_sec", ""),
                "workers": item.get("workers", ""),
                "tail_boost": item.get("tail_boost", False),
            }
        return versions

    def _employment_row_status(self, row: Dict[str, Any]) -> str:
        statuses = []
        for key in ["企业名称状态", "经营状态状态", "承担职务状态", "持股比例状态"]:
            value = _to_text(row.get(key))
            if value:
                statuses.append(value)
        if not statuses:
            return ""
        if any(value in {"冲突", "缺失"} for value in statuses):
            return "冲突" if "冲突" in statuses else "缺失"
        if any(value in {"占位", "证据补齐"} for value in statuses):
            return "证据补齐" if "证据补齐" in statuses else "占位"
        return "一致" if all(value == "一致" for value in statuses) else "|".join(sorted(set(statuses)))
