from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import unquote


BASELINE_TYPES = {"snapshot", "candidate", "golden"}
BUSINESS_FIELDS = ["姓名", "身份证号", "相关企业", "任职", "参股"]
COUNT_FIELDS = {"相关企业", "任职", "参股"}
EMPLOYMENT_FIELDS = ["企业名称", "经营状态", "承担职务", "持股比例"]
EMPLOYMENT_FIELD_KEYS = {
    "企业名称": "company_name",
    "经营状态": "business_status",
    "承担职务": "role",
    "持股比例": "share_ratio",
}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


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


def _normalize_count(value: Any) -> str:
    text = _to_text(value)
    if text == "":
        return ""
    m = re.search(r"-?\d+", text)
    return m.group(0) if m else text


def _normalize_field_value(field: str, value: Any) -> str:
    if field in COUNT_FIELDS:
        return _normalize_count(value)
    return _to_text(value)


def _capture_tuple(date_value: Any, time_value: Any) -> Optional[tuple[int, int, int, int]]:
    date_text = _to_text(date_value).replace("/", "-").replace("－", "-")
    time_text = _to_text(time_value).replace("：", ":")
    dm = re.fullmatch(r"(\d{2})-(\d{2})", date_text)
    tm = re.fullmatch(r"(\d{2}):(\d{2})", time_text)
    if not dm or not tm:
        return None
    month, day = int(dm.group(1)), int(dm.group(2))
    hour, minute = int(tm.group(1)), int(tm.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return month, day, hour, minute


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

    def update_baseline(self, baseline_id: str, name: str = "", notes: Optional[str] = None) -> Dict[str, Any]:
        self.init_db()
        baseline_id = _to_text(baseline_id)
        name = _to_text(name)
        if not baseline_id:
            raise BaselineError("baseline_id is required")
        if not name:
            raise BaselineError("baseline name is required")
        with self.connect() as conn:
            existing = conn.execute("SELECT * FROM baseline_versions WHERE id = ?", (baseline_id,)).fetchone()
            if not existing:
                raise BaselineError("baseline not found")
            if notes is None:
                conn.execute("UPDATE baseline_versions SET name = ? WHERE id = ?", (name, baseline_id))
            else:
                conn.execute("UPDATE baseline_versions SET name = ?, notes = ? WHERE id = ?", (name, _to_text(notes), baseline_id))
        return self.get_baseline(baseline_id, include_records=False)

    def delete_baseline(self, baseline_id: str) -> Dict[str, Any]:
        self.init_db()
        baseline_id = _to_text(baseline_id)
        if not baseline_id:
            raise BaselineError("baseline_id is required")
        with self.connect() as conn:
            existing = conn.execute("SELECT * FROM baseline_versions WHERE id = ?", (baseline_id,)).fetchone()
            if not existing:
                raise BaselineError("baseline not found")
            was_active_golden = bool(existing["is_active_golden"])
            conn.execute("DELETE FROM baseline_versions WHERE id = ?", (baseline_id,))
            promoted = ""
            if was_active_golden:
                row = conn.execute(
                    """
                    SELECT id FROM baseline_versions
                    WHERE type = 'golden' AND status = 'active'
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row:
                    promoted = _to_text(row["id"])
                    conn.execute("UPDATE baseline_versions SET is_active_golden = 1 WHERE id = ?", (promoted,))
        return {"ok": True, "id": baseline_id, "promoted_active_golden": promoted}

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

    def compare_task_to_baseline(self, baseline_id: str, task: Dict[str, Any]) -> Dict[str, Any]:
        self.init_db()
        if task.get("status") != "completed":
            raise BaselineError("only completed tasks can be compared with baseline")
        baseline = self.get_baseline(baseline_id, include_records=True)
        task_dir = Path(_to_text(task.get("output_dir")) or self.runs_root / _to_text(task.get("id")))
        if not task_dir.exists():
            raise BaselineError("task output directory not found")
        current_records = self._merge_person_records(self._compare_records_from_task(task_dir))
        if not current_records:
            raise BaselineError("task has no structured records to compare")

        baseline_records = self._merge_person_records(list(baseline.get("records") or []))
        baseline_employment_by_record = self._employment_by_record_id(
            baseline.get("employment_rows") or [],
            baseline_records,
        )
        baseline_task_dir = self.runs_root / _to_text(baseline.get("source_task_id"))
        baseline_image_index = self._image_info_index(baseline_task_dir)
        current_image_index = self._image_info_index(task_dir)
        same_source_task = _to_text(task.get("id")) and _to_text(task.get("id")) == _to_text(baseline.get("source_task_id"))
        if same_source_task:
            diff_records = [
                self._diff_record(
                    baseline_record,
                    baseline_record,
                    "matched",
                    baseline_employment_by_record.get(_to_text(baseline_record.get("id")), []),
                    baseline_employment_by_record.get(_to_text(baseline_record.get("id")), []),
                    baseline_image_index,
                    baseline_image_index,
                )
                for baseline_record in baseline_records
            ]
            summary = self._compare_summary(diff_records, baseline_records, baseline_records)
            return {
                "baseline": {
                    "id": baseline.get("id"),
                    "name": baseline.get("name"),
                    "type": baseline.get("type"),
                    "source_task_id": baseline.get("source_task_id"),
                    "source_task_name": baseline.get("source_task_name"),
                    "created_at": baseline.get("created_at"),
                    "record_count": len(baseline_records),
                    "image_count": baseline.get("image_count"),
                },
                "task": {
                    "id": task.get("id"),
                    "name": task.get("name"),
                    "created_at": task.get("created_at"),
                    "completed_at": task.get("completed_at"),
                    "record_count": len(baseline_records),
                    "image_count": task.get("files_total"),
                },
                "summary": summary,
                "records": diff_records,
            }

        current_employment_by_record = self._current_employment_by_record_id(task_dir, current_records)
        baseline_index: Dict[str, List[Dict[str, Any]]] = {}
        for row in baseline_records:
            for key in self._match_keys(row):
                baseline_index.setdefault(key, []).append(row)

        used_baseline_ids: set[str] = set()
        diff_records: List[Dict[str, Any]] = []

        for current in current_records:
            matched = None
            for key in self._match_keys(current):
                candidates = baseline_index.get(key) or []
                matched = next((row for row in candidates if row.get("id") not in used_baseline_ids), None)
                if matched:
                    break
            if matched:
                used_baseline_ids.add(_to_text(matched.get("id")))
                diff_records.append(
                    self._diff_record(
                        matched,
                        current,
                        "matched",
                        baseline_employment_by_record.get(_to_text(matched.get("id")), []),
                        current_employment_by_record.get(_to_text(current.get("id")), []),
                        baseline_image_index,
                        current_image_index,
                    )
                )
            else:
                diff_records.append(
                    self._new_record_diff(
                        current,
                        current_employment_by_record.get(_to_text(current.get("id")), []),
                        current_image_index,
                    )
                )

        for baseline_record in baseline_records:
            if _to_text(baseline_record.get("id")) in used_baseline_ids:
                continue
            diff_records.append(
                self._missing_record_diff(
                    baseline_record,
                    baseline_employment_by_record.get(_to_text(baseline_record.get("id")), []),
                    baseline_image_index,
                )
            )

        summary = self._compare_summary(diff_records, baseline_records, current_records)
        return {
            "baseline": {
                "id": baseline.get("id"),
                "name": baseline.get("name"),
                "type": baseline.get("type"),
                "source_task_id": baseline.get("source_task_id"),
                "source_task_name": baseline.get("source_task_name"),
                "created_at": baseline.get("created_at"),
                "record_count": baseline.get("record_count"),
                "image_count": baseline.get("image_count"),
            },
            "task": {
                "id": task.get("id"),
                "name": task.get("name"),
                "created_at": task.get("created_at"),
                "completed_at": task.get("completed_at"),
                "record_count": len(current_records),
                "image_count": task.get("files_total"),
            },
            "summary": summary,
            "records": diff_records,
        }

    def create_from_task(
        self,
        task: Dict[str, Any],
        name: str = "",
        baseline_type: str = "snapshot",
        notes: str = "",
        created_by: str = "",
        review_state: Optional[Dict[str, Any]] = None,
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

        raw_records = self._load_task_records(task_dir)
        records = self._merge_person_records(self._compare_records_from_task(task_dir))
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
            "raw_record_count": len(raw_records),
            "strategy": "4ocr_weighted_consensus+harness",
            "review_state_count": len(review_state or {}),
            "person_multi_image_merge": True,
        }

        record_ids_by_person_key: Dict[str, str] = {}
        record_rows = []
        for idx, rec in enumerate(records, start=1):
            fields = self._consensus_fields(rec)
            filename = self._record_file_name_value(rec)
            file_no = _to_text(rec.get("file_no")) or _extract_file_no(filename)
            file_name_from_filename = _to_text(rec.get("file_name_from_filename")) or _extract_filename_name(filename)
            person_name = _to_text(fields.get("姓名") or rec.get("person_name") or rec.get("姓名") or file_name_from_filename)
            masked_id = _to_text(fields.get("身份证号") or rec.get("masked_id") or rec.get("身份证号"))
            capture_date = _to_text(fields.get("截图日期") or rec.get("截图日期"))
            capture_time = _to_text(fields.get("截图时间") or rec.get("截图时间"))
            record_key = _record_key(file_no, person_name, masked_id, filename)
            record_id = f"{baseline_id}_r{idx:06d}"
            person_key = f"{file_no}-{person_name}" if file_no and person_name else record_key
            review_status = self._review_status_for_record(review_state or {}, rec, idx, file_no, person_name)
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
                    review_status or _to_text(rec.get("review_status")) or "unreviewed",
                    _json_dumps(fields),
                    _json_dumps(self._record_evidence(rec)),
                )
            )

        employment_by_current_record = self._current_employment_by_record_id(task_dir, records)
        employment_inserts = []
        emp_idx = 0
        for rec in records:
            baseline_record_id = record_ids_by_person_key.get(
                f"{_to_text(rec.get('file_id'))}-{_to_text(rec.get('person_name'))}"
            ) or record_ids_by_person_key.get(_to_text(rec.get("record_key")))
            if not baseline_record_id:
                continue
            for row in employment_by_current_record.get(_to_text(rec.get("id")), []):
                emp_idx += 1
                company_name = _to_text(row.get("company_name"))
                row_status = _to_text(row.get("row_status"))
                employment_inserts.append(
                    (
                        f"{baseline_id}_e{emp_idx:06d}",
                        baseline_id,
                        baseline_record_id,
                        emp_idx,
                        _to_text(row.get("company_key")) or _normalize_company_key(company_name),
                        company_name,
                        _to_text(row.get("business_status")),
                        _to_text(row.get("role")),
                        _to_text(row.get("share_ratio")),
                        _to_text(row.get("source_image")),
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

    def create_updated_from_compare(
        self,
        baseline_id: str,
        task: Dict[str, Any],
        decisions: List[Dict[str, Any]],
        name: str = "",
        baseline_type: str = "golden",
        notes: str = "",
        created_by: str = "",
    ) -> Dict[str, Any]:
        self.init_db()
        baseline_type = _to_text(baseline_type) or "golden"
        if baseline_type not in BASELINE_TYPES:
            raise BaselineError(f"unsupported baseline type: {baseline_type}")
        compare = self.compare_task_to_baseline(baseline_id, task)
        parent = self.get_baseline(baseline_id, include_records=False)
        decision_by_idx: Dict[int, Dict[str, Any]] = {}
        decision_by_record_key: Dict[str, Dict[str, Any]] = {}
        for item in decisions or []:
            try:
                idx = int(item.get("idx"))
            except Exception:
                idx = -1
            if idx >= 0:
                decision_by_idx[idx] = item
            record_key = _to_text(item.get("record_key"))
            if record_key:
                decision_by_record_key[record_key] = item

        new_baseline_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        baseline_name = name.strip() or f"{parent.get('name') or baseline_id} updated"
        task_id = _to_text(task.get("id"))
        task_name = _to_text(task.get("name"))
        record_rows: List[tuple] = []
        employment_inserts: List[tuple] = []
        action_rows: List[tuple] = []
        selected_count = 0

        def selected_record_from_diff(diff: Dict[str, Any], decision: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], str]:
            action = _to_text(decision.get("action")) or "keep_baseline"
            if diff.get("match_status") == "unmatched_new":
                if action in {"accept_current", "manual"}:
                    return dict(diff.get("current") or {}), list(diff.get("current_employment") or []), action
                return None, [], "ignore_new"
            if diff.get("match_status") == "unmatched_missing":
                if action in {"accept_current", "remove"}:
                    return None, [], "remove_missing"
                return dict(diff.get("baseline") or {}), list(diff.get("baseline_employment") or []), action or "keep_baseline"
            if action == "field_decisions":
                return (
                    self._merge_record_by_field_decisions(diff, decision),
                    self._merge_employment_by_field_decisions(diff, decision),
                    action,
                )
            if action in {"accept_current", "manual"}:
                return dict(diff.get("current") or {}), list(diff.get("current_employment") or []), action
            return dict(diff.get("baseline") or {}), list(diff.get("baseline_employment") or []), action or "keep_baseline"

        for idx, diff in enumerate(compare.get("records") or []):
            decision = decision_by_idx.get(idx) or decision_by_record_key.get(_to_text(diff.get("record_key"))) or {}
            record, employment_rows, effective_action = selected_record_from_diff(diff, decision)
            if idx in decision_by_idx or _to_text(diff.get("record_key")) in decision_by_record_key:
                selected_count += 1
            if record is None:
                continue
            manual_fields = decision.get("fields") if isinstance(decision.get("fields"), dict) else {}
            if effective_action == "manual" and manual_fields:
                self._apply_manual_fields(record, manual_fields)
            record_index = len(record_rows) + 1
            record_id = f"{new_baseline_id}_r{record_index:06d}"
            fields = self._fields_from_public_record(record)
            support = self._updated_support(diff, record, task, effective_action)
            evidence = {
                "parent_baseline_id": baseline_id,
                "decision": effective_action,
                "source_task_id": task_id,
                "source_task_name": task_name,
                "support": support,
                "manual_fields": manual_fields if effective_action == "manual" else {},
            }
            record_rows.append(
                (
                    record_id,
                    new_baseline_id,
                    record_index,
                    _to_text(record.get("record_key")) or _record_key(
                        _to_text(record.get("file_id")),
                        _to_text(record.get("person_name")),
                        _to_text(record.get("masked_id")),
                        _to_text(record.get("file_name")),
                    ),
                    _to_text(record.get("file_id")),
                    _to_text(record.get("file_name")),
                    _extract_filename_name(record.get("file_name")),
                    _to_text(record.get("person_name")),
                    _to_text(record.get("masked_id")),
                    _to_text(record.get("capture_date")),
                    _to_text(record.get("capture_time")),
                    " ".join(x for x in [_to_text(record.get("capture_date")), _to_text(record.get("capture_time"))] if x),
                    _to_text(record.get("related_count")),
                    _to_text(record.get("employment_count")),
                    _to_text(record.get("shareholding_count")),
                    f"baseline_{effective_action}",
                    _json_dumps(fields),
                    _json_dumps(evidence),
                )
            )
            for employment in self._dedupe_employment_rows(employment_rows):
                emp_idx = len(employment_inserts) + 1
                company_name = _to_text(employment.get("company_name")) or "未识别企业"
                emp_evidence = dict(employment.get("evidence_summary") or {})
                emp_evidence.update(
                    {
                        "parent_baseline_id": baseline_id,
                        "decision": effective_action,
                        "source_task_id": task_id,
                        "source_task_name": task_name,
                    }
                )
                employment_inserts.append(
                    (
                        f"{new_baseline_id}_e{emp_idx:06d}",
                        new_baseline_id,
                        record_id,
                        emp_idx,
                        _to_text(employment.get("company_key")) or _normalize_company_key(company_name),
                        company_name,
                        _to_text(employment.get("business_status")),
                        _to_text(employment.get("role")),
                        _to_text(employment.get("share_ratio")),
                        _to_text(employment.get("source_image")),
                        _to_text(employment.get("row_status")) or "baseline_selected",
                        _json_dumps(emp_evidence),
                    )
                )

        if not record_rows:
            raise BaselineError("no records selected for updated baseline")

        metadata = {
            "kind": "truth_baseline_update",
            "parent_baseline_id": baseline_id,
            "source_task": {
                "id": task.get("id"),
                "name": task.get("name"),
                "created_at": task.get("created_at"),
                "completed_at": task.get("completed_at"),
                "dashboard_url": task.get("dashboard_url"),
            },
            "decision_count": len(decision_by_idx),
            "selected_decision_count": selected_count,
            "strategy": "manual_truth_baseline_update",
        }

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
                    new_baseline_id,
                    baseline_name,
                    baseline_type,
                    baseline_id,
                    _to_text(parent.get("source_task_id")),
                    _to_text(parent.get("source_task_name")),
                    "active",
                    1 if baseline_type == "golden" else 0,
                    len(record_rows),
                    int(parent.get("image_count") or task.get("files_total") or 0),
                    _now(),
                    created_by,
                    "",
                    "manual_truth_baseline_update",
                    _json_dumps({}),
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
            for idx, decision in decision_by_idx.items():
                action_rows.append(
                    (
                        uuid.uuid4().hex,
                        "baseline_version",
                        new_baseline_id,
                        _to_text(decision.get("action")) or "keep_baseline",
                        baseline_id,
                        new_baseline_id,
                        _to_text(decision.get("reason") or notes),
                        created_by,
                        _now(),
                    )
                )
            if action_rows:
                conn.executemany(
                    """
                    INSERT INTO manual_review_actions (
                        id, target_type, target_id, action, old_value, new_value,
                        reason, operator, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    action_rows,
                )

        return self.get_baseline(new_baseline_id, include_records=False)

    def _apply_manual_fields(self, record: Dict[str, Any], fields: Dict[str, Any]) -> None:
        mapping = {
            "姓名": "person_name",
            "身份证号": "masked_id",
            "截图日期": "capture_date",
            "截图时间": "capture_time",
            "相关企业": "related_count",
            "任职": "employment_count",
            "参股": "shareholding_count",
            "person_name": "person_name",
            "masked_id": "masked_id",
            "capture_date": "capture_date",
            "capture_time": "capture_time",
            "related_count": "related_count",
            "employment_count": "employment_count",
            "shareholding_count": "shareholding_count",
        }
        for key, target in mapping.items():
            if key in fields:
                record[target] = _to_text(fields.get(key))

    def _decision_pick_value(self, baseline_value: Any, current_value: Any, decision: Dict[str, Any]) -> str:
        action = _to_text(decision.get("action"))
        if action == "accept_current":
            return _to_text(current_value)
        if action == "manual":
            return _to_text(decision.get("value"))
        return _to_text(baseline_value)

    def _decision_pick_count_triplet(
        self,
        baseline: Dict[str, Any],
        current: Dict[str, Any],
        decision: Dict[str, Any],
    ) -> Dict[str, str]:
        keys = ["related_count", "employment_count", "shareholding_count"]
        action = _to_text(decision.get("action"))
        if action == "accept_current":
            return {key: _to_text(current.get(key)) for key in keys}
        if action == "manual":
            numbers = re.findall(r"-?\d+", _to_text(decision.get("value")))
            if len(numbers) >= 3:
                return dict(zip(keys, numbers[:3]))
        return {key: _to_text(baseline.get(key)) for key in keys}

    def _merge_record_by_field_decisions(self, diff: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, Any]:
        baseline = dict(diff.get("baseline") or {})
        current = dict(diff.get("current") or {})
        record = dict(baseline or current)
        basic = decision.get("basic") if isinstance(decision.get("basic"), dict) else {}
        mapping = {
            "姓名": "person_name",
            "身份证号": "masked_id",
            "相关企业": "related_count",
            "任职": "employment_count",
            "参股": "shareholding_count",
        }
        for label, key in mapping.items():
            field_decision = basic.get(label)
            if isinstance(field_decision, dict):
                record[key] = self._decision_pick_value(baseline.get(key), current.get(key), field_decision)
        group_decision = basic.get("企/任/参")
        if isinstance(group_decision, dict):
            record.update(self._decision_pick_count_triplet(baseline, current, group_decision))
        return record

    def _align_public_employment_pairs(
        self,
        baseline_rows: List[Dict[str, Any]],
        current_rows: List[Dict[str, Any]],
    ) -> List[Dict[str, Optional[Dict[str, Any]]]]:
        used_current: set[int] = set()
        pairs: List[Dict[str, Optional[Dict[str, Any]]]] = []
        for baseline_row in baseline_rows:
            current_idx = self._find_employment_match(baseline_row, current_rows, used_current)
            if current_idx is None:
                pairs.append({"baseline": baseline_row, "current": None})
                continue
            used_current.add(current_idx)
            pairs.append({"baseline": baseline_row, "current": current_rows[current_idx]})
        for idx, current_row in enumerate(current_rows):
            if idx not in used_current:
                pairs.append({"baseline": None, "current": current_row})
        return pairs

    def _merge_employment_by_field_decisions(self, diff: Dict[str, Any], decision: Dict[str, Any]) -> List[Dict[str, Any]]:
        baseline_rows = list(diff.get("baseline_employment") or [])
        current_rows = list(diff.get("current_employment") or [])
        employment = decision.get("employment") if isinstance(decision.get("employment"), dict) else {}
        employment_companies = (
            decision.get("employment_companies")
            if isinstance(decision.get("employment_companies"), dict)
            else {}
        )
        field_mapping = {
            "企业名称": "company_name",
            "经营状态": "business_status",
            "承担职务": "role",
            "持股比例": "share_ratio",
        }

        def company_matches_decision(row: Dict[str, Any], item: Dict[str, Any]) -> bool:
            if not row or not item:
                return False
            row_key = _to_text(row.get("company_key"))
            row_name = _to_text(row.get("company_name"))
            row_image = _to_text(row.get("source_image"))
            item_key = _to_text(item.get("company_key"))
            item_name = _to_text(item.get("company_name"))
            item_image = _to_text(item.get("source_image"))
            if row_key and item_key and row_key == item_key:
                return True
            if row_name and item_name and row_name == item_name:
                return not item_image or not row_image or item_image == row_image
            return False

        def remove_decision_for_pair(pair_index: int, baseline: Dict[str, Any], current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            by_index = employment_companies.get(str(pair_index))
            if isinstance(by_index, dict) and by_index.get("action") == "remove":
                return by_index
            for item in employment_companies.values():
                if not isinstance(item, dict) or item.get("action") != "remove":
                    continue
                if company_matches_decision(baseline, item) or company_matches_decision(current, item):
                    return item
            return None

        merged: List[Dict[str, Any]] = []
        for pair_index, pair in enumerate(self._align_public_employment_pairs(baseline_rows, current_rows)):
            baseline = pair.get("baseline") or {}
            current = pair.get("current") or {}
            company_decision = employment_companies.get(str(pair_index))
            remove_decision = remove_decision_for_pair(pair_index, baseline, current)
            if remove_decision:
                continue
            if not baseline:
                if isinstance(company_decision, dict) and company_decision.get("action") == "accept_current":
                    merged.append(dict(current))
                continue
            if not current:
                if isinstance(company_decision, dict) and company_decision.get("action") in {"accept_current", "remove"}:
                    continue
                merged.append(dict(baseline))
                continue
            row = dict(baseline)
            source_from_current = False
            for label, key in field_mapping.items():
                field_decision = employment.get(f"{pair_index}:{label}")
                if isinstance(field_decision, dict):
                    row[key] = self._decision_pick_value(baseline.get(key), current.get(key), field_decision)
                    if field_decision.get("action") == "accept_current":
                        source_from_current = True
            if source_from_current and current.get("source_image"):
                row["source_image"] = current.get("source_image")
            merged.append(row)
        return merged

    def _fields_from_public_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "姓名": _to_text(record.get("person_name")),
            "身份证号": _to_text(record.get("masked_id")),
            "截图日期": _to_text(record.get("capture_date")),
            "截图时间": _to_text(record.get("capture_time")),
            "相关企业": _to_text(record.get("related_count")),
            "任职": _to_text(record.get("employment_count")),
            "参股": _to_text(record.get("shareholding_count")),
        }

    def _updated_support(self, diff: Dict[str, Any], record: Dict[str, Any], task: Dict[str, Any], action: str) -> Dict[str, Any]:
        baseline = diff.get("baseline") or {}
        current = diff.get("current") or {}
        same_business = False
        if baseline and current:
            same_business = self._business_field_values(baseline) == self._business_field_values(current)
        return {
            "action": action,
            "same_business_observation": same_business,
            "source_task_id": task.get("id"),
            "source_task_name": task.get("name"),
            "selected_record_key": record.get("record_key"),
        }

    def _compare_records_from_task(self, task_dir: Path) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for idx, rec in enumerate(self._load_task_records(task_dir), start=1):
            fields = self._consensus_fields(rec)
            filename = self._record_file_name_value(rec)
            file_no = _to_text(rec.get("file_no")) or _extract_file_no(filename)
            file_name_from_filename = _to_text(rec.get("file_name_from_filename")) or _extract_filename_name(filename)
            person_name = _to_text(fields.get("姓名") or rec.get("person_name") or rec.get("姓名") or file_name_from_filename)
            masked_id = _to_text(fields.get("身份证号") or rec.get("masked_id") or rec.get("身份证号"))
            capture_date = _to_text(fields.get("截图日期") or rec.get("截图日期"))
            capture_time = _to_text(fields.get("截图时间") or rec.get("截图时间"))
            row = {
                "id": f"current_r{idx:06d}",
                "record_index": idx,
                "record_key": _record_key(file_no, person_name, masked_id, filename),
                "file_id": file_no,
                "file_name": filename,
                "file_name_from_filename": file_name_from_filename,
                "person_name": person_name,
                "masked_id": masked_id,
                "capture_date": capture_date,
                "capture_time": capture_time,
                "capture_datetime": " ".join(x for x in [capture_date, capture_time] if x),
                "related_count": _to_text(fields.get("相关企业") or rec.get("相关企业")),
                "employment_count": _to_text(fields.get("任职") or rec.get("任职")),
                "shareholding_count": _to_text(fields.get("参股") or rec.get("参股")),
                "review_status": _to_text(rec.get("review_status")) or "unreviewed",
                "fields": fields,
                "evidence_summary": self._record_evidence(rec),
            }
            rows.append(row)
        return rows

    def _person_merge_key(self, row: Dict[str, Any]) -> str:
        file_id = _to_text(row.get("file_id"))
        person_name = _to_text(row.get("person_name"))
        masked_id = _to_text(row.get("masked_id"))
        record_key = _to_text(row.get("record_key"))
        if file_id and person_name:
            return f"file_name:{file_id}|||{person_name}"
        if masked_id and person_name:
            return f"id_name:{masked_id}|||{person_name}"
        if record_key:
            return f"record:{record_key}"
        return f"single:{_to_text(row.get('id')) or _to_text(row.get('file_name'))}"

    def _merge_person_records(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        order: List[str] = []
        for row in records or []:
            key = self._person_merge_key(row)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(row)
        return [self._merge_person_record_group(groups[key]) for key in order]

    def _merge_person_record_group(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not records:
            return {}
        if len(records) == 1:
            merged = dict(records[0])
            merged.setdefault("merged_record_count", 1)
            merged.setdefault("merged_file_names", self._record_images(merged, []))
            return merged

        def first_text(key: str) -> str:
            for row in records:
                value = _to_text(row.get(key))
                if value:
                    return value
            return ""

        def best_text(key: str) -> str:
            values = [_to_text(row.get(key)) for row in records if _to_text(row.get(key))]
            if not values:
                return ""
            counts: Dict[str, int] = {}
            for value in values:
                counts[value] = counts.get(value, 0) + 1
            return sorted(values, key=lambda value: (-counts[value], -len(value), values.index(value)))[0]

        def max_count(key: str) -> str:
            values = [_normalize_count(row.get(key)) for row in records if _normalize_count(row.get(key)) != ""]
            if not values:
                return ""
            numeric = []
            for value in values:
                try:
                    numeric.append(int(value))
                except Exception:
                    pass
            if numeric:
                return str(max(numeric))
            return values[0]

        def add_unique(items: List[str], value: Any) -> None:
            for item in re.split(r"[|,，;；]+", _to_text(value)):
                name = Path(unquote(_to_text(item))).name
                if name and name not in items:
                    items.append(name)

        images: List[str] = []
        for row in records:
            add_unique(images, row.get("file_name"))
            merged_files = row.get("merged_file_names")
            if isinstance(merged_files, list):
                for item in merged_files:
                    add_unique(images, item)

        latest = None
        for row in records:
            capture = _capture_tuple(row.get("capture_date"), row.get("capture_time"))
            if capture and (latest is None or capture > latest[0]):
                latest = (capture, row)
        capture_row = latest[1] if latest else next((row for row in records if _to_text(row.get("capture_date") or row.get("capture_time"))), records[0])

        review_values = [_to_text(row.get("review_status")) for row in records]
        non_empty_review_values = [value for value in review_values if value]
        if "failed" in non_empty_review_values:
            review_status = "failed"
        elif non_empty_review_values and all(value == "passed" for value in non_empty_review_values):
            review_status = "passed"
        elif "passed" in non_empty_review_values:
            review_status = "passed"
        else:
            review_status = next((value for value in non_empty_review_values if value), "unreviewed")

        fields: Dict[str, Any] = {}
        for row in records:
            row_fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
            for key, value in row_fields.items():
                if _to_text(value) and not _to_text(fields.get(key)):
                    fields[key] = value

        fields.update(
            {
                "姓名": best_text("person_name"),
                "身份证号": best_text("masked_id"),
                "截图日期": _to_text(capture_row.get("capture_date")),
                "截图时间": _to_text(capture_row.get("capture_time")),
                "相关企业": max_count("related_count"),
                "任职": max_count("employment_count"),
                "参股": max_count("shareholding_count"),
            }
        )

        evidence = {
            "merged_record_count": len(records),
            "merged_file_names": images,
            "merged_record_keys": [_to_text(row.get("record_key")) for row in records if _to_text(row.get("record_key"))],
            "sources": [row.get("evidence_summary") for row in records if isinstance(row.get("evidence_summary"), dict)],
        }

        merged = dict(records[0])
        file_id = first_text("file_id")
        person_name = best_text("person_name")
        masked_id = best_text("masked_id")
        filename = "|".join(images)
        merged.update(
            {
                "record_key": _record_key(file_id, person_name, masked_id, filename),
                "file_id": file_id,
                "file_name": filename,
                "file_name_from_filename": best_text("file_name_from_filename"),
                "person_name": person_name,
                "masked_id": masked_id,
                "capture_date": _to_text(capture_row.get("capture_date")),
                "capture_time": _to_text(capture_row.get("capture_time")),
                "capture_datetime": " ".join(x for x in [_to_text(capture_row.get("capture_date")), _to_text(capture_row.get("capture_time"))] if x),
                "related_count": max_count("related_count"),
                "employment_count": max_count("employment_count"),
                "shareholding_count": max_count("shareholding_count"),
                "review_status": review_status or "unreviewed",
                "fields": fields,
                "evidence_summary": evidence,
                "merged_record_count": len(records),
                "merged_file_names": images,
            }
        )
        return merged

    def _employment_by_record_id(
        self,
        rows: List[Dict[str, Any]],
        records: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        person_to_records: Dict[str, List[str]] = {}
        image_to_records: Dict[str, List[str]] = {}

        def add_mapping(mapping: Dict[str, List[str]], key: str, record_id: str) -> None:
            key = _to_text(key)
            record_id = _to_text(record_id)
            if not key or not record_id:
                return
            mapping.setdefault(key, [])
            if record_id not in mapping[key]:
                mapping[key].append(record_id)

        for rec in records or []:
            record_id = _to_text(rec.get("id"))
            file_id = _to_text(rec.get("file_id"))
            person_name = _to_text(rec.get("person_name"))
            record_key = _to_text(rec.get("record_key"))
            if file_id and person_name:
                add_mapping(person_to_records, f"{file_id}-{person_name}", record_id)
            if record_key:
                add_mapping(person_to_records, record_key, record_id)
            for image in self._record_images(rec, []):
                add_mapping(image_to_records, self._image_key(image), record_id)

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for idx, row in enumerate(rows, start=1):
            target_ids: List[str] = []

            def add_target(record_id: str) -> None:
                record_id = _to_text(record_id)
                if record_id and record_id not in target_ids:
                    target_ids.append(record_id)

            add_target(row.get("baseline_record_id"))
            evidence = row.get("evidence_summary") if isinstance(row.get("evidence_summary"), dict) else {}
            person_key = _to_text(evidence.get("人员键") or row.get("人员键"))
            if not person_key:
                file_no = _to_text(evidence.get("文件编号") or row.get("文件编号"))
                name = _to_text(evidence.get("姓名") or row.get("姓名"))
                person_key = f"{file_no}-{name}" if file_no and name else ""
            for record_id in person_to_records.get(person_key, []):
                add_target(record_id)
            source_image = _to_text(row.get("source_image") or evidence.get("来源文件") or evidence.get("文件名"))
            for image in re.split(r"[|,，;；]+", source_image):
                for record_id in image_to_records.get(self._image_key(image), []):
                    add_target(record_id)
            if not target_ids:
                continue
            normalized = self._normalize_employment_row(row, idx)
            for record_id in target_ids:
                grouped.setdefault(record_id, []).append(normalized)
        return {record_id: self._dedupe_employment_rows(items) for record_id, items in grouped.items()}

    def _current_employment_by_record_id(
        self,
        task_dir: Path,
        current_records: List[Dict[str, Any]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        person_to_records: Dict[str, List[str]] = {}
        image_to_records: Dict[str, List[str]] = {}

        def add_mapping(mapping: Dict[str, List[str]], key: str, record_id: str) -> None:
            key = _to_text(key)
            record_id = _to_text(record_id)
            if not key or not record_id:
                return
            mapping.setdefault(key, [])
            if record_id not in mapping[key]:
                mapping[key].append(record_id)

        for rec in current_records:
            file_id = _to_text(rec.get("file_id"))
            person_name = _to_text(rec.get("person_name"))
            record_id = _to_text(rec.get("id"))
            record_key = _to_text(rec.get("record_key"))
            if file_id and person_name:
                add_mapping(person_to_records, f"{file_id}-{person_name}", record_id)
            if record_key:
                add_mapping(person_to_records, record_key, record_id)
            for image in self._record_images(rec, []):
                add_mapping(image_to_records, self._image_key(image), record_id)

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for idx, row in enumerate(self._load_employment_rows(task_dir), start=1):
            target_ids: List[str] = []

            def add_target(record_id: str) -> None:
                record_id = _to_text(record_id)
                if record_id and record_id not in target_ids:
                    target_ids.append(record_id)

            person_key = _to_text(row.get("人员键"))
            if not person_key:
                file_no = _to_text(row.get("文件编号"))
                name = _to_text(row.get("姓名"))
                person_key = f"{file_no}-{name}" if file_no and name else ""
            for record_id in person_to_records.get(person_key, []):
                add_target(record_id)
            file_no = _to_text(row.get("文件编号"))
            name = _to_text(row.get("姓名"))
            if file_no and name:
                for record_id in person_to_records.get(_record_key(file_no, name, "", _to_text(row.get("文件名"))), []):
                    add_target(record_id)
            source_image = _to_text(row.get("来源文件") or row.get("文件名"))
            for image in re.split(r"[|,，;；]+", source_image):
                for record_id in image_to_records.get(self._image_key(image), []):
                    add_target(record_id)
            if not target_ids:
                continue
            normalized = self._normalize_employment_row(row, idx)
            for record_id in target_ids:
                grouped.setdefault(record_id, []).append(normalized)
        return {record_id: self._dedupe_employment_rows(items) for record_id, items in grouped.items()}

    def _dedupe_employment_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        by_key: Dict[str, Dict[str, Any]] = {}

        def merge_sources(left: Any, right: Any) -> str:
            values: List[str] = []
            for value in [left, right]:
                for item in re.split(r"[|,，;；]+", _to_text(value)):
                    name = Path(unquote(_to_text(item))).name
                    if name and name not in values:
                        values.append(name)
            return "|".join(values)

        def row_key(row: Dict[str, Any]) -> str:
            company_key = _to_text(row.get("company_key")) or _normalize_company_key(row.get("company_name"))
            return "|||".join(
                [
                    company_key,
                    self._normalize_employment_value("经营状态", row.get("business_status")),
                    self._normalize_employment_value("承担职务", row.get("role")),
                    self._normalize_employment_value("持股比例", row.get("share_ratio")),
                ]
            )

        for row in rows or []:
            key = row_key(row)
            if key not in by_key:
                copied = dict(row)
                by_key[key] = copied
                merged.append(copied)
                continue
            target = by_key[key]
            target["source_image"] = merge_sources(target.get("source_image"), row.get("source_image"))
            if not _to_text(target.get("row_status")) and _to_text(row.get("row_status")):
                target["row_status"] = row.get("row_status")
        return merged

    def _normalize_employment_row(self, row: Dict[str, Any], idx: int) -> Dict[str, Any]:
        evidence = row.get("evidence_summary") if isinstance(row.get("evidence_summary"), dict) else {}
        company_name = self._employment_value(row, evidence, ["company_name", "企业名称", "公司名称"])
        company_key = _to_text(row.get("company_key")) or _normalize_company_key(company_name)
        row_index = row.get("row_index") or row.get("企业序号") or idx
        try:
            row_index_int = int(row_index)
        except Exception:
            row_index_int = idx
        return {
            "id": _to_text(row.get("id")) or f"employment_{idx:06d}",
            "row_index": row_index_int,
            "company_key": company_key,
            "company_name": company_name,
            "business_status": self._employment_value(row, evidence, ["business_status", "经营状态"]),
            "role": self._employment_value(row, evidence, ["role", "承担职务", "职务"]),
            "share_ratio": self._employment_value(row, evidence, ["share_ratio", "持股比例", "持股"]),
            "source_image": self._employment_value(row, evidence, ["source_image", "来源文件", "文件名"]),
            "row_status": _to_text(row.get("row_status")) or self._employment_row_status(row),
            "evidence_summary": evidence or row,
        }

    def _employment_value(self, row: Dict[str, Any], evidence: Dict[str, Any], aliases: List[str]) -> str:
        for source in [row, evidence]:
            value = self._dict_value_by_alias(source, aliases, fuzzy=False)
            if value:
                return value
        for source in [row, evidence]:
            value = self._dict_value_by_alias(source, aliases, fuzzy=True)
            if value:
                return value
        return ""

    def _dict_value_by_alias(self, source: Dict[str, Any], aliases: List[str], fuzzy: bool = False) -> str:
        if not isinstance(source, dict):
            return ""
        noise = ["置信", "confidence", "score", "来源模型", "证据", "支持"]
        for key in aliases:
            value = source.get(key)
            text = self._scalar_text(value)
            if text:
                return text
        for container_key in ["fields", "consensus", "weighted_consensus", "final", "values", "data"]:
            container = source.get(container_key)
            if not isinstance(container, dict):
                continue
            value = self._dict_value_by_alias(container, aliases, fuzzy=fuzzy)
            if value:
                return value
        if not fuzzy:
            return ""
        for key, value in source.items():
            key_text = _to_text(key)
            if any(item in key_text for item in noise):
                continue
            if aliases and aliases[0] in {"company_name", "企业名称", "公司名称"} and "状态" in key_text:
                continue
            if aliases and any(alias in key_text for alias in aliases if len(alias) >= 2):
                text = self._scalar_text(value)
                if text:
                    return text
        return ""

    def _scalar_text(self, value: Any) -> str:
        if isinstance(value, dict):
            for key in ["value", "text", "final", "consensus", "selected", "current_value", "baseline_value"]:
                text = _to_text(value.get(key))
                if text:
                    return text
            return ""
        if isinstance(value, list):
            return "，".join(_to_text(item) for item in value if _to_text(item))
        return _to_text(value)

    def _match_keys(self, row: Dict[str, Any]) -> List[str]:
        keys: List[str] = []
        record_key = _to_text(row.get("record_key"))
        file_id = _to_text(row.get("file_id"))
        person_name = _to_text(row.get("person_name"))
        masked_id = _to_text(row.get("masked_id"))
        if record_key:
            keys.append(f"record:{record_key}")
        if file_id and person_name:
            keys.append(f"file_name:{file_id}|||{person_name}")
        if masked_id and person_name:
            keys.append(f"id_name:{masked_id}|||{person_name}")
        if file_id:
            keys.append(f"file:{file_id}")
        return list(dict.fromkeys(keys))

    def _business_field_values(self, row: Dict[str, Any]) -> Dict[str, str]:
        fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
        return {
            "姓名": _normalize_field_value("姓名", row.get("person_name") or fields.get("姓名")),
            "身份证号": _normalize_field_value("身份证号", row.get("masked_id") or fields.get("身份证号")),
            "相关企业": _normalize_field_value("相关企业", row.get("related_count") or fields.get("相关企业")),
            "任职": _normalize_field_value("任职", row.get("employment_count") or fields.get("任职")),
            "参股": _normalize_field_value("参股", row.get("shareholding_count") or fields.get("参股")),
        }

    def _public_record(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "record_key": row.get("record_key"),
            "file_id": row.get("file_id"),
            "file_name": row.get("file_name"),
            "person_name": row.get("person_name"),
            "masked_id": row.get("masked_id"),
            "capture_date": row.get("capture_date"),
            "capture_time": row.get("capture_time"),
            "related_count": _normalize_count(row.get("related_count")),
            "employment_count": _normalize_count(row.get("employment_count")),
            "shareholding_count": _normalize_count(row.get("shareholding_count")),
            "review_status": row.get("review_status"),
        }

    def _diff_record(
        self,
        baseline: Dict[str, Any],
        current: Dict[str, Any],
        match_status: str,
        baseline_employment: Optional[List[Dict[str, Any]]] = None,
        current_employment: Optional[List[Dict[str, Any]]] = None,
        baseline_image_index: Optional[Dict[str, Dict[str, Any]]] = None,
        current_image_index: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        baseline_values = self._business_field_values(baseline)
        current_values = self._business_field_values(current)
        field_diffs = []
        for field in BUSINESS_FIELDS:
            bval = baseline_values.get(field, "")
            cval = current_values.get(field, "")
            if bval == cval:
                continue
            if bval and not cval:
                diff_type = "field_missing_current"
            elif cval and not bval:
                diff_type = "field_filled"
            else:
                diff_type = "field_changed"
            field_diffs.append(
                {
                    "section": "basic",
                    "field_name": field,
                    "baseline_value": bval,
                    "current_value": cval,
                    "diff_type": diff_type,
                    "severity": "high" if field == "身份证号" else "medium",
                }
            )

        capture_status = self._capture_time_diff_status(baseline, current)
        employment_diff = self._diff_employment_rows(baseline_employment or [], current_employment or [])
        employment_diffs = employment_diff["diffs"]
        baseline_images = self._record_images(baseline, baseline_employment or [])
        current_images = self._record_images(current, current_employment or [])
        baseline_image_details = self._image_details(baseline_images, baseline_image_index or {})
        current_image_details = self._image_details(current_images, current_image_index or {})
        image_diffs = self._diff_images(baseline_images, current_images, baseline_image_details, current_image_details)
        business_changed = bool(field_diffs or employment_diffs)
        business_status = "changed" if business_changed else "same"
        severity = self._record_severity(business_changed, capture_status, field_diffs, employment_diffs)
        requires_review = severity in {"medium", "high", "critical"}
        if not business_changed:
            requires_review = False

        return {
            "record_key": current.get("record_key") or baseline.get("record_key"),
            "match_status": match_status,
            "business_diff_status": business_status,
            "capture_time_diff_status": capture_status,
            "ocr_quality_diff_status": "same",
            "severity": severity,
            "requires_review": requires_review,
            "field_diffs": field_diffs,
            "employment_diffs": employment_diffs,
            "employment_summary": employment_diff["summary"],
            "image_diffs": image_diffs,
            "diff_summary": self._diff_summary(field_diffs, employment_diffs, image_diffs, capture_status),
            "baseline": self._public_record(baseline),
            "current": self._public_record(current),
            "baseline_employment": [self._public_employment_row(row) for row in baseline_employment or []],
            "current_employment": [self._public_employment_row(row) for row in current_employment or []],
            "baseline_images": baseline_images,
            "current_images": current_images,
            "baseline_image_details": baseline_image_details,
            "current_image_details": current_image_details,
        }

    def _new_record_diff(
        self,
        current: Dict[str, Any],
        current_employment: Optional[List[Dict[str, Any]]] = None,
        current_image_index: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        current_images = self._record_images(current, current_employment or [])
        return {
            "record_key": current.get("record_key"),
            "match_status": "unmatched_new",
            "business_diff_status": "new",
            "capture_time_diff_status": "filled" if current.get("capture_date") or current.get("capture_time") else "missing",
            "ocr_quality_diff_status": "same",
            "severity": "info",
            "requires_review": False,
            "field_diffs": [],
            "employment_diffs": [],
            "employment_summary": self._employment_summary([], current_employment or [], 0, 0, len(current_employment or []), 0),
            "image_diffs": [],
            "diff_summary": ["新增记录"],
            "baseline": None,
            "current": self._public_record(current),
            "baseline_employment": [],
            "current_employment": [self._public_employment_row(row) for row in current_employment or []],
            "baseline_images": [],
            "current_images": current_images,
            "baseline_image_details": [],
            "current_image_details": self._image_details(current_images, current_image_index or {}),
        }

    def _missing_record_diff(
        self,
        baseline: Dict[str, Any],
        baseline_employment: Optional[List[Dict[str, Any]]] = None,
        baseline_image_index: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        baseline_images = self._record_images(baseline, baseline_employment or [])
        return {
            "record_key": baseline.get("record_key"),
            "match_status": "unmatched_missing",
            "business_diff_status": "missing",
            "capture_time_diff_status": "missing",
            "ocr_quality_diff_status": "same",
            "severity": "info",
            "requires_review": False,
            "field_diffs": [],
            "employment_diffs": [],
            "employment_summary": self._employment_summary(baseline_employment or [], [], 0, 0, 0, len(baseline_employment or [])),
            "image_diffs": [],
            "diff_summary": ["基线记录缺失"],
            "baseline": self._public_record(baseline),
            "current": None,
            "baseline_employment": [self._public_employment_row(row) for row in baseline_employment or []],
            "current_employment": [],
            "baseline_images": baseline_images,
            "current_images": [],
            "baseline_image_details": self._image_details(baseline_images, baseline_image_index or {}),
            "current_image_details": [],
        }

    def _capture_time_diff_status(self, baseline: Dict[str, Any], current: Dict[str, Any]) -> str:
        b_date, b_time = _to_text(baseline.get("capture_date")), _to_text(baseline.get("capture_time"))
        c_date, c_time = _to_text(current.get("capture_date")), _to_text(current.get("capture_time"))
        if not b_date and not b_time and not c_date and not c_time:
            return "same"
        if not b_date and not b_time and (c_date or c_time):
            return "filled"
        if (b_date or b_time) and not c_date and not c_time:
            return "missing_current"
        if b_date == c_date and b_time == c_time:
            return "same"
        b_tuple = _capture_tuple(b_date, b_time)
        c_tuple = _capture_tuple(c_date, c_time)
        if not b_tuple or not c_tuple:
            return "invalid"
        if c_tuple > b_tuple:
            return "newer"
        if c_tuple < b_tuple:
            return "older"
        return "changed"

    def _record_severity(
        self,
        business_changed: bool,
        capture_status: str,
        field_diffs: List[Dict[str, Any]],
        employment_diffs: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        if not business_changed:
            return "info"
        if business_changed and capture_status == "older":
            return "critical"
        if any(diff.get("field_name") == "身份证号" for diff in field_diffs):
            return "high"
        if employment_diffs:
            if any(diff.get("severity") == "high" for diff in employment_diffs):
                return "high"
            return "medium"
        if business_changed:
            return "high"
        if capture_status == "older":
            return "medium"
        if capture_status in {"invalid", "missing_current"}:
            return "medium"
        if capture_status in {"newer", "filled", "changed"}:
            return "info"
        return "info"

    def _compare_summary(
        self,
        diff_records: List[Dict[str, Any]],
        baseline_records: List[Dict[str, Any]],
        current_records: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        summary = {
            "baseline_records": len(baseline_records),
            "current_records": len(current_records),
            "total_records": len(diff_records),
            "matched_records": 0,
            "same_records": 0,
            "business_changed_records": 0,
            "new_records": 0,
            "missing_records": 0,
            "capture_time_only_changed": 0,
            "capture_time_older": 0,
            "requires_review": 0,
            "field_diff_count": 0,
            "employment_changed_records": 0,
            "employment_diff_count": 0,
            "image_changed_records": 0,
            "image_diff_count": 0,
            "severity": {"info": 0, "low": 0, "medium": 0, "high": 0, "critical": 0},
        }
        for rec in diff_records:
            if rec.get("match_status") == "matched":
                summary["matched_records"] += 1
            if rec.get("business_diff_status") == "same":
                summary["same_records"] += 1
            if rec.get("business_diff_status") == "changed":
                summary["business_changed_records"] += 1
            if rec.get("business_diff_status") == "new":
                summary["new_records"] += 1
            if rec.get("business_diff_status") == "missing":
                summary["missing_records"] += 1
            if rec.get("business_diff_status") == "same" and rec.get("capture_time_diff_status") not in {"same"}:
                summary["capture_time_only_changed"] += 1
            if rec.get("capture_time_diff_status") == "older":
                summary["capture_time_older"] += 1
            if rec.get("requires_review"):
                summary["requires_review"] += 1
            summary["field_diff_count"] += len(rec.get("field_diffs") or [])
            if rec.get("employment_diffs"):
                summary["employment_changed_records"] += 1
                summary["employment_diff_count"] += len(rec.get("employment_diffs") or [])
            if rec.get("image_diffs"):
                summary["image_changed_records"] += 1
                summary["image_diff_count"] += len(rec.get("image_diffs") or [])
            sev = _to_text(rec.get("severity")) or "info"
            if sev not in summary["severity"]:
                summary["severity"][sev] = 0
            summary["severity"][sev] += 1
        return summary

    def _diff_employment_rows(
        self,
        baseline_rows: List[Dict[str, Any]],
        current_rows: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        diffs: List[Dict[str, Any]] = []
        used_current: set[int] = set()
        matched = 0
        changed = 0

        for baseline_row in baseline_rows:
            current_idx = self._find_employment_match(baseline_row, current_rows, used_current)
            if current_idx is None:
                diffs.extend(self._employment_missing_diffs([baseline_row]))
                continue
            used_current.add(current_idx)
            current_row = current_rows[current_idx]
            matched += 1
            row_changed = False
            for field in EMPLOYMENT_FIELDS:
                key = EMPLOYMENT_FIELD_KEYS[field]
                bval = self._normalize_employment_value(field, baseline_row.get(key))
                cval = self._normalize_employment_value(field, current_row.get(key))
                if bval == cval:
                    continue
                row_changed = True
                diffs.append(
                    {
                        "section": "employment",
                        "field_name": field,
                        "baseline_value": bval,
                        "current_value": cval,
                        "diff_type": self._employment_field_diff_type(field),
                        "severity": self._employment_field_severity(field),
                        "baseline_row": self._public_employment_row(baseline_row),
                        "current_row": self._public_employment_row(current_row),
                    }
                )
            if row_changed:
                changed += 1

        added_rows = [row for idx, row in enumerate(current_rows) if idx not in used_current]
        diffs.extend(self._employment_added_diffs(added_rows))
        return {
            "diffs": diffs,
            "summary": self._employment_summary(
                baseline_rows,
                current_rows,
                matched,
                changed,
                len(added_rows),
                len([d for d in diffs if d.get("diff_type") == "company_missing"]) // max(1, len(EMPLOYMENT_FIELDS)),
            ),
        }

    def _find_employment_match(
        self,
        baseline_row: Dict[str, Any],
        current_rows: List[Dict[str, Any]],
        used_current: set[int],
    ) -> Optional[int]:
        baseline_key = _to_text(baseline_row.get("company_key"))
        baseline_placeholder = self._is_placeholder_company(baseline_row)
        for idx, current_row in enumerate(current_rows):
            if idx in used_current:
                continue
            current_key = _to_text(current_row.get("company_key"))
            if baseline_key and current_key and baseline_key == current_key:
                return idx
        for idx, current_row in enumerate(current_rows):
            if idx in used_current:
                continue
            current_key = _to_text(current_row.get("company_key"))
            if self._company_keys_compatible(baseline_key, current_key, baseline_placeholder):
                return idx
        for idx, current_row in enumerate(current_rows):
            if idx in used_current:
                continue
            if (
                baseline_placeholder
                and self._is_placeholder_company(current_row)
                and baseline_row.get("row_index") == current_row.get("row_index")
            ):
                return idx
        return None

    def _company_keys_compatible(self, baseline_key: str, current_key: str, baseline_placeholder: bool) -> bool:
        baseline_key = _to_text(baseline_key)
        current_key = _to_text(current_key)
        if baseline_placeholder or not baseline_key or not current_key:
            return False
        if baseline_key in current_key or current_key in baseline_key:
            return min(len(baseline_key), len(current_key)) >= 6
        return SequenceMatcher(None, baseline_key, current_key).ratio() >= 0.86

    def _employment_added_diffs(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        diffs: List[Dict[str, Any]] = []
        for row in rows:
            for field in EMPLOYMENT_FIELDS:
                key = EMPLOYMENT_FIELD_KEYS[field]
                diffs.append(
                    {
                        "section": "employment",
                        "field_name": field,
                        "baseline_value": "",
                        "current_value": self._normalize_employment_value(field, row.get(key)),
                        "diff_type": "company_added",
                        "severity": "high",
                        "baseline_row": None,
                        "current_row": self._public_employment_row(row),
                    }
                )
        return diffs

    def _employment_missing_diffs(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        diffs: List[Dict[str, Any]] = []
        for row in rows:
            for field in EMPLOYMENT_FIELDS:
                key = EMPLOYMENT_FIELD_KEYS[field]
                diffs.append(
                    {
                        "section": "employment",
                        "field_name": field,
                        "baseline_value": self._normalize_employment_value(field, row.get(key)),
                        "current_value": "",
                        "diff_type": "company_missing",
                        "severity": "high",
                        "baseline_row": self._public_employment_row(row),
                        "current_row": None,
                    }
                )
        return diffs

    def _employment_summary(
        self,
        baseline_rows: List[Dict[str, Any]],
        current_rows: List[Dict[str, Any]],
        matched: int,
        changed: int,
        added: int,
        missing: int,
    ) -> Dict[str, Any]:
        return {
            "baseline_rows": len(baseline_rows),
            "current_rows": len(current_rows),
            "matched_rows": matched,
            "changed_rows": changed,
            "added_rows": added,
            "missing_rows": missing,
        }

    def _public_employment_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "row_index": row.get("row_index"),
            "company_key": row.get("company_key"),
            "company_name": row.get("company_name"),
            "business_status": row.get("business_status"),
            "role": row.get("role"),
            "share_ratio": row.get("share_ratio"),
            "source_image": row.get("source_image"),
            "row_status": row.get("row_status"),
        }

    def _image_info_index(self, task_dir: Path) -> Dict[str, Dict[str, Any]]:
        index: Dict[str, Dict[str, Any]] = {}
        if not task_dir or not task_dir.exists():
            return index
        for path in task_dir.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            key = self._image_key(path.name)
            if not key or key in index:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            width: Any = ""
            height: Any = ""
            try:
                from PIL import Image

                with Image.open(path) as image:
                    width, height = image.size
            except Exception:
                pass
            index[key] = {
                "name": path.name,
                "path": str(path),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "width": width,
                "height": height,
            }
        return index

    def _image_details(self, images: Iterable[Any], image_index: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        details: List[Dict[str, Any]] = []
        for image in images:
            name = Path(unquote(_to_text(image))).name
            key = self._image_key(name)
            info = dict(image_index.get(key) or {})
            if not info:
                info = {"name": name, "path": "", "size_bytes": "", "modified_at": "", "width": "", "height": ""}
            elif not info.get("name"):
                info["name"] = name
            details.append(info)
        return details

    def _record_images(self, record: Dict[str, Any], employment_rows: List[Dict[str, Any]]) -> List[str]:
        images: List[str] = []

        def add(value: Any) -> None:
            text = _to_text(value)
            if not text:
                return
            for item in re.split(r"[|,，;；]+", text):
                name = Path(unquote(_to_text(item))).name
                if name and name not in images:
                    images.append(name)

        add(record.get("file_name"))
        for row in employment_rows:
            add(row.get("source_image"))
        return images

    def _diff_images(
        self,
        baseline_images: List[str],
        current_images: List[str],
        baseline_details: Optional[List[Dict[str, Any]]] = None,
        current_details: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        if not baseline_images or not current_images:
            return []
        baseline_map = {self._image_key(image): image for image in baseline_images if self._image_key(image)}
        current_map = {self._image_key(image): image for image in current_images if self._image_key(image)}
        baseline_detail_map = {
            self._image_key(detail.get("name")): detail
            for detail in baseline_details or []
            if self._image_key(detail.get("name"))
        }
        current_detail_map = {
            self._image_key(detail.get("name")): detail
            for detail in current_details or []
            if self._image_key(detail.get("name"))
        }
        baseline_set = set(baseline_map)
        current_set = set(current_map)
        diffs = self._image_missing_diffs([baseline_map[key] for key in sorted(baseline_set - current_set)]) + self._image_added_diffs(
            [current_map[key] for key in sorted(current_set - baseline_set)]
        )
        for key in sorted(baseline_set & current_set):
            baseline_detail = baseline_detail_map.get(key) or {}
            current_detail = current_detail_map.get(key) or {}
            for label, field in [("图片尺寸", "dimensions"), ("图片大小", "size_bytes"), ("图片时间", "modified_at")]:
                if field == "dimensions":
                    baseline_value = self._image_dimensions(baseline_detail)
                    current_value = self._image_dimensions(current_detail)
                else:
                    baseline_value = _to_text(baseline_detail.get(field))
                    current_value = _to_text(current_detail.get(field))
                if not baseline_value or not current_value or baseline_value == current_value:
                    continue
                diffs.append(
                    {
                        "section": "image",
                        "field_name": label,
                        "baseline_value": baseline_value,
                        "current_value": current_value,
                        "diff_type": f"{field}_changed",
                        "severity": "low",
                        "baseline_image": baseline_map.get(key),
                        "current_image": current_map.get(key),
                    }
                )
        return diffs

    def _image_dimensions(self, detail: Dict[str, Any]) -> str:
        width = _to_text(detail.get("width"))
        height = _to_text(detail.get("height"))
        return f"{width}x{height}" if width and height else ""

    def _image_key(self, value: Any) -> str:
        text = Path(unquote(_to_text(value))).name.strip()
        return re.sub(r"\s+", "", text).casefold()

    def _image_added_diffs(self, images: List[str]) -> List[Dict[str, Any]]:
        return [
            {
                "section": "image",
                "field_name": "图片",
                "baseline_value": "",
                "current_value": image,
                "diff_type": "image_added",
                "severity": "medium",
            }
            for image in images
        ]

    def _image_missing_diffs(self, images: List[str]) -> List[Dict[str, Any]]:
        return [
            {
                "section": "image",
                "field_name": "图片",
                "baseline_value": image,
                "current_value": "",
                "diff_type": "image_missing",
                "severity": "medium",
            }
            for image in images
        ]

    def _diff_summary(
        self,
        field_diffs: List[Dict[str, Any]],
        employment_diffs: List[Dict[str, Any]],
        image_diffs: List[Dict[str, Any]],
        capture_status: str,
    ) -> List[str]:
        summary: List[str] = []
        for diff in field_diffs[:4]:
            field = _to_text(diff.get("field_name"))
            baseline_value = _to_text(diff.get("baseline_value")) or "—"
            current_value = _to_text(diff.get("current_value")) or "—"
            if field:
                summary.append(f"{field} {baseline_value} -> {current_value}")
        if len(field_diffs) > 4:
            summary.append(f"基础字段另有 {len(field_diffs) - 4} 项变化")

        if employment_diffs:
            by_type: Dict[str, int] = {}
            for diff in employment_diffs:
                diff_type = _to_text(diff.get("diff_type")) or "employment_changed"
                by_type[diff_type] = by_type.get(diff_type, 0) + 1
            type_label = {
                "company_added": "新增企业字段",
                "company_missing": "缺失企业字段",
                "company_name_changed": "企业名称变化",
                "company_status_changed": "经营状态变化",
                "company_role_changed": "职务变化",
                "company_share_changed": "持股比例变化",
            }
            for diff_type, count in list(by_type.items())[:4]:
                summary.append(f"{type_label.get(diff_type, diff_type)} {count}")
            if len(by_type) > 4:
                summary.append(f"任职明细另有 {len(by_type) - 4} 类变化")

        if image_diffs:
            added = len([diff for diff in image_diffs if diff.get("diff_type") == "image_added"])
            missing = len([diff for diff in image_diffs if diff.get("diff_type") == "image_missing"])
            meta = len([diff for diff in image_diffs if diff.get("diff_type") not in {"image_added", "image_missing"}])
            if added:
                summary.append(f"图片增加 {added}")
            if missing:
                summary.append(f"图片减少 {missing}")
            if meta:
                summary.append(f"图片证据变化 {meta}")

        if not summary and capture_status not in {"same"}:
            summary.append(f"采集时间 {capture_status}")
        return summary

    def _is_placeholder_company(self, row: Dict[str, Any]) -> bool:
        company_name = _to_text(row.get("company_name"))
        company_key = _to_text(row.get("company_key"))
        return not company_key or "未识别企业" in company_name or "未识别企业" in company_key

    def _normalize_employment_value(self, field: str, value: Any) -> str:
        text = _to_text(value)
        if field == "经营状态":
            parts = [p for p in re.split(r"[\s,，、/|]+", text) if p]
            return "，".join(dict.fromkeys(parts))
        if field == "持股比例":
            return text.replace("％", "%")
        return text

    def _employment_field_diff_type(self, field: str) -> str:
        return {
            "企业名称": "company_name_changed",
            "经营状态": "company_status_changed",
            "承担职务": "company_role_changed",
            "持股比例": "company_share_changed",
        }.get(field, "company_field_changed")

    def _employment_field_severity(self, field: str) -> str:
        return "high" if field in {"企业名称", "经营状态", "持股比例"} else "medium"

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
        return self._extract_employment_rows(payload)

    def _extract_employment_rows(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ["person_rows", "rows", "employment_rows", "company_rows", "details", "records"]:
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        rows: List[Dict[str, Any]] = []

        def collect(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    collect(item)
                return
            if not isinstance(value, dict):
                return
            keys = "".join(str(key) for key in value.keys())
            if any(token in keys for token in ["企业名称", "公司名称", "经营状态", "承担职务", "持股比例", "company_name"]):
                rows.append(value)
                return
            for item in value.values():
                collect(item)

        collect(payload)
        return rows

    def _record_file_name_value(self, rec: Dict[str, Any]) -> str:
        names: List[str] = []

        def add(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    add(item)
                return
            if isinstance(value, dict):
                for key in ["filename", "file_name", "name", "path"]:
                    if key in value:
                        add(value.get(key))
                return
            text = _to_text(value)
            if not text:
                return
            for item in re.split(r"[|,，;；]+", text):
                name = Path(_to_text(item)).name
                if name and name not in names:
                    names.append(name)

        for key in ["filename", "file_name", "filenames", "files", "images", "image_files", "source_images", "source_files"]:
            if key in rec:
                add(rec.get(key))
        return "|".join(names)

    def _consensus_fields(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(rec.get("fields"), dict):
            return dict(rec["fields"])
        consensus = rec.get("weighted_consensus")
        if isinstance(consensus, dict) and isinstance(consensus.get("fields"), dict):
            return dict(consensus["fields"])
        fields: Dict[str, Any] = {}
        for key in ["姓名", "身份证号", "截图日期", "截图时间", "相关企业", "任职", "参股", "查询结论"]:
            if key in rec:
                fields[key] = rec.get(key)
        return fields

    def _record_evidence(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(rec.get("evidence_summary"), dict):
            return dict(rec["evidence_summary"])
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

    def _review_status_for_record(
        self,
        review_state: Dict[str, Any],
        rec: Dict[str, Any],
        idx: int,
        file_no: str,
        person_name: str,
    ) -> str:
        candidates = [f"r:{idx - 1}"]
        raw_idx = rec.get("idx")
        if isinstance(raw_idx, int):
            candidates.append(f"r:{raw_idx}")
        if file_no and person_name:
            candidates.append(f"g:{file_no}|||{person_name}")
        for key in candidates:
            if key not in review_state:
                continue
            entry = review_state.get(key)
            if entry is True:
                return "passed"
            if isinstance(entry, dict):
                status = _to_text(entry.get("status"))
                if status in {"passed", "failed"}:
                    return status
                return "passed"
            if _to_text(entry) in {"passed", "failed"}:
                return _to_text(entry)
        return "unreviewed"
