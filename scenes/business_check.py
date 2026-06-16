"""
经商办企场景 — 从工商查询截图提取姓名、身份证、投资任职信息
V18+ 规则收敛版：
  1. bbox空间同行拼接（碎片化ID重组）
  2. 投资任职信息分隔 + 顶部噪音清理
  3. 文件名兜底姓名 + 姓名提取增强
  4. 日期时间提取去噪（降低日期吸附到身份证）
  5. 未查询场景下栏位归零
  6. 多模式身份证号提取（标准/脱敏/★修复/补位构造）
  7. 多行任职信息合并
  8. 栏位竖线清理（"1相关企业"→"相关企业"）
  9. 相关企业/任职/参股按“涉及企业数”取值，不引入明细页“持股比例”噪声
"""
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from .base import Scene, register_scene


class BusinessCheckScene(Scene):

    @property
    def name(self) -> str:
        return "business_check"

    @property
    def display_name(self) -> str:
        return "经商办企查询"

    @property
    def description(self) -> str:
        return "从手机工商查询截图中提取姓名、身份证号、投资任职信息（V17+优化版）"

    def parse(self, ocr_result: dict) -> dict:
        filename = ocr_result.get("filename", "")
        raw_text = ocr_result.get("text", "")
        lines_data = ocr_result.get("lines", [])

        result = {
            "filename": filename,
            "text": raw_text,
            "姓名": "",
            "身份证号": "",
            "相关企业": 0,
            "任职": 0,
            "参股": 0,
            "截图日期": "",
            "截图时间": "",
            "查询结论": "",
            "任职情况信息": "",
            "原始OCR": "",
            "field_provenance": {
                "姓名": {},
                "身份证号": {},
                "相关企业": {},
                "任职": {},
                "参股": {},
                "截图日期": {},
                "截图时间": {},
                "查询结论": {},
                "任职情况信息": {},
            },
        }

        # Step 1: bbox空间同行拼接
        merged_lines = self._bbox_merge(lines_data)
        cleaned_lines = merged_lines[:]
        sep_line_clean = ""

        # Step 2: 找投资任职信息分隔符，清理顶部噪音
        filename_name = self._extract_name_from_filename(filename)
        sep_idx = -1
        for i, line in enumerate(merged_lines):
            if "投资任职信息" in line and "未查询" not in line:
                sep_idx = i
                sep_line_clean = self._clean_separator_prefix(line)
                cleaned_lines[i] = sep_line_clean
                break

        # 清理原始OCR：保留分隔符及之后的内容
        if sep_idx >= 0:
            result["原始OCR"] = "\n".join(cleaned_lines[sep_idx:])
        else:
            result["原始OCR"] = "\n".join(merged_lines)

        # Step 3: 提取身份证号（从全量合并文本）
        full_text = "\n".join(cleaned_lines)
        id_number, id_meta = self._extract_id_with_meta(cleaned_lines, sep_idx)
        result["身份证号"] = id_number
        result["field_provenance"]["身份证号"] = id_meta

        # Step 4: 提取姓名
        name = self._extract_name(cleaned_lines, sep_idx, filename_name)
        result["姓名"] = name
        if filename_name and len(filename_name) >= 2 and name == filename_name:
            result["field_provenance"]["姓名"] = {
                "source": "filename_fallback",
                "rules": ["filename_based"],
                "rule_processed": True,
            }
        else:
            result["field_provenance"]["姓名"] = {
                "source": "ocr_line",
                "rules": ["line_name_regex"],
                "rule_processed": True if name else False,
            }

        # Step 4.5: 提取截图日期与时间
        capture_date, capture_time = self._extract_capture_datetime(cleaned_lines, sep_idx)
        result["截图日期"] = capture_date
        result["截图时间"] = capture_time
        if capture_date or capture_time:
            result["field_provenance"]["截图日期"] = {
                "source": "line_fusion_rule",
                "rules": ["capture_datetime_extraction"],
                "rule_processed": True,
            }
            result["field_provenance"]["截图时间"] = {
                "source": "line_fusion_rule",
                "rules": ["capture_datetime_extraction"],
                "rule_processed": True,
            }
        else:
            result["field_provenance"]["截图日期"] = {
                "source": "missing",
                "rules": [],
                "rule_processed": False,
            }
            result["field_provenance"]["截图时间"] = {
                "source": "missing",
                "rules": [],
                "rule_processed": False,
            }

        # Step 5: 栏位统计（相关企业/任职/参股按“企业数量”口径）
        biz_text = "\n".join(cleaned_lines[sep_idx + 1:]) if sep_idx >= 0 else full_text
        result["查询结论"] = self._extract_conclusion(cleaned_lines)
        result["field_provenance"]["查询结论"] = {
            "source": "ocr_line",
            "rules": ["conclusion_line_scan"],
            "rule_processed": True,
        }
        no_result = self._is_no_result_query(cleaned_lines, sep_idx)
        if no_result:
            # 未查询到任职信息时，计数字段固定置零，减少噪点误伤
            result["相关企业"] = 0
            result["任职"] = 0
            result["参股"] = 0
            result["field_provenance"]["相关企业"] = {
                "source": "no_result_forced_zero",
                "rules": ["unfound_force_zero"],
                "rule_processed": True,
            }
            result["field_provenance"]["任职"] = {
                "source": "no_result_forced_zero",
                "rules": ["unfound_force_zero"],
                "rule_processed": True,
            }
            result["field_provenance"]["参股"] = {
                "source": "no_result_forced_zero",
                "rules": ["unfound_force_zero"],
                "rule_processed": True,
            }
        else:
            related_count = self._count_field(biz_text, "相关企业")
            job_count = self._count_field(biz_text, "任职")
            share_count = self._count_field(biz_text, "参股")
            result["相关企业"] = related_count
            result["任职"] = job_count
            result["参股"] = share_count
            result["field_provenance"]["相关企业"] = {
                "source": "count_rule_match" if related_count > 0 else "count_rule_nomatch",
                "rules": ["count_field_line_pattern"],
                "rule_processed": True,
            }
            result["field_provenance"]["任职"] = {
                "source": "count_rule_match" if job_count > 0 else "count_rule_nomatch",
                "rules": ["count_field_line_pattern"],
                "rule_processed": True,
            }
            result["field_provenance"]["参股"] = {
                "source": "count_rule_match" if share_count > 0 else "count_rule_nomatch",
                "rules": ["count_field_line_pattern"],
                "rule_processed": True,
            }

        # Step 7: 多行任职信息合并（格式3）
        if sep_idx >= 0:
            biz_lines = cleaned_lines[sep_idx + 1:]
            result["任职情况信息"] = self._merge_employment(biz_lines)
        else:
            result["任职情况信息"] = ""
        result["field_provenance"]["任职情况信息"] = {
            "source": "employment_merge",
            "rules": ["merge_employment_lines"],
            "rule_processed": True,
        }

        if not result["身份证号"] and not result["姓名"]:
            result["parse_error"] = "未提取到有效信息"

        return result

    # ========== 核心优化：bbox空间同行拼接 ==========

    @staticmethod
    def _clean_separator_prefix(line: str) -> str:
        """
        过滤“投资任职信息”一行前缀噪音（如运营商状态栏、图标文字等）
        保留从“投资任职信息”开始到行尾内容
        """
        if not line:
            return line
        idx = line.find("投资任职信息")
        if idx <= 0:
            return line.strip()
        return line[idx:].strip()

    @staticmethod
    def _bbox_merge(lines_data: list, y_threshold: float = 15.0) -> list:
        """
        按Y坐标邻近性合并OCR碎片到同一行。
        y_threshold: Y坐标差小于该值视为同一行。
        """
        if not lines_data:
            return []

        # 过滤掉bbox为None的行，保留原始文本
        items = []
        for ld in lines_data:
            bbox = ld.get("bbox")
            text = ld.get("text", "").strip()
            if not text:
                continue
            if bbox:
                # bbox是 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
                y_center = (bbox[0][1] + bbox[2][1]) / 2
                x_left = bbox[0][0]
                items.append({"text": text, "y": y_center, "x": x_left})
            else:
                # 无bbox：追加为独立行
                items.append({"text": text, "y": None, "x": None})

        if not items:
            return []

        # 按Y坐标排序（None放最后）
        items_with_y = [i for i in items if i["y"] is not None]
        items_no_y = [i for i in items if i["y"] is None]
        items_with_y.sort(key=lambda i: (i["y"], i["x"] or 0))
        items = items_with_y + items_no_y

        # 按Y阈值分组
        merged = []
        current_group = [items[0]]

        for item in items[1:]:
            if item["y"] is None or current_group[0]["y"] is None:
                # 无bbox的行各自独立，不跨行拼接
                if current_group[0]["y"] is None and item["y"] is None:
                    # 当前组和新item都无y → 先输出当前组，新item独立
                    merged.append("".join(g["text"] for g in current_group))
                    current_group = [item]
                    continue
                current_group.append(item)
                continue

            if abs(item["y"] - current_group[0]["y"]) <= y_threshold:
                current_group.append(item)
            else:
                # 按X排序后拼接
                current_group.sort(key=lambda i: i["x"] or 0)
                merged.append("".join(g["text"] for g in current_group))
                current_group = [item]

        # 最后一组
        current_group.sort(key=lambda i: i["x"] or 0)
        merged.append("".join(g["text"] for g in current_group))

        return merged

    # ========== 身份证号提取 ==========

    @staticmethod
    def _extract_id_with_meta(merged_lines: list, sep_idx: int) -> Tuple[str, dict]:
        """
        多模式身份证号提取：
        A. 标准18位：前6+4位+星号+4位+校验位
        B. 脱敏格式：前3+星号+后4
        C. ★修复：OCR把*识别为★，全角修复
        D. 补位构造：仅前3后4，构造"前3+8星+后4"
           后4不够时，使用X补位（支持“最后一位X”）
        E. 容错：
           - 星号数量非8时自动收敛
           - 身份证片段顺序漂移（头尾颠倒）与跨行噪音
           - 优先选择高置信度候选，避免时间信息拼接
        """
        # 先把★和全角字符修复为标准格式
        def normalize_id_text(text: str) -> str:
            text = text.replace("★", "*").replace("☆", "*")
            text = text.replace("＊", "*").replace("×", "*")
            text = text.replace("Ｘ", "X").replace("ｘ", "x")
            for fc, hc in zip("０１２３４５６７８９", "0123456789"):
                text = text.replace(fc, hc)
            return text

        def to_std_masked(head: str, star_blob: str, tail_src: str) -> str:
            if not head or len(head) != 3 or not head.isdigit():
                return ""
            if len(star_blob) < 1:
                return ""

            tail_chars = [ch.upper() for ch in normalize_id_text(tail_src) if ch.isdigit() or ch.upper() == "X"]
            if not tail_chars:
                return ""
            # 不允许中间位 X，允许末位 X
            if "X" in tail_chars[:-1]:
                tail_chars = [c for c in tail_chars if c != "X"]
            if not tail_chars:
                return ""
            # 证件号脱敏尾号优先取“离星号最近”的4位，避免与日期/时间拼接后再取末位导致误吸附到时间。
            if len(tail_chars) > 4:
                tail_chars = tail_chars[:4]
            while len(tail_chars) < 4:
                tail_chars.append("X")
            tail = "".join(tail_chars)
            if any(ch == "X" for ch in tail[:-1]):
                return ""
            return f"{head}{'*' * 8}{tail}"

        def normalize_masked_id(raw: str) -> str:
            if not raw:
                return ""

            text = normalize_id_text(raw).replace("\u200b", "")
            compact = re.sub(r"[^0-9X*]", "", text).upper()

            # 首选：三位 + 8星 + 四位尾码
            m_exact = re.fullmatch(r"(\d{3})(\*+)([0-9X]{4})", compact)
            if m_exact and m_exact.group(2):
                return to_std_masked(m_exact.group(1), m_exact.group(2), m_exact.group(3))

            # 标准形态：多/少星统一收敛
            for m in re.finditer(r"(\d{3})(\*+)([0-9X*]{1,12})", compact):
                tail = m.group(3).replace("*", "")
                cand = to_std_masked(m.group(1), m.group(2), tail)
                if cand:
                    return cand

            # 头尾漂移（尾码先于头码）或分隔噪声场景
            for m in re.finditer(r"([0-9X*]{1,12})(\*+)(\d{3})", compact):
                tail = m.group(1).replace("*", "")
                cand = to_std_masked(m.group(3), m.group(2), tail)
                if cand:
                    return cand

            # 无星场景：完整15/18位也统一掩码
            plain_digits = re.sub(r"[^0-9X]", "", compact)
            if len(plain_digits) in (15, 18):
                return f"{plain_digits[:3]}{'*' * 8}{plain_digits[-4:]}"

            return ""

        def strip_datetime_noise(text: str) -> str:
            # 去掉常见时间日期噪点，降低身份证尾部误识别
            t = re.sub(r"\d{1,2}:\d{2}", "", text)
            t = re.sub(r"\d{1,2}[-/]\d{1,2}(?!\d)", "", t)
            return t

        def has_datetime_suffix(text: str, pos: int, limit: int = 16) -> bool:
            tail = text[pos: pos + limit]
            return bool(re.search(r"^\s*[-/]?\d{1,2}[-/]\d{1,2}(?:\s*\d{1,2}:\d{2})?", tail)) or bool(
                re.search(r"^\s*\d{1,2}:\d{2}", tail)
            )

        def collect_matches(text: str):
            """
            收集候选值 + 分值（越高越优先）：
            - 严格命中（3+8*+4）最高
            - 邻近有“身份证”加分
            - 尾码4位完整比3位更优
            """
            cands = []

            def add(raw: str, base_score: int, tag: str):
                normalized = normalize_masked_id(raw)
                if normalized:
                    # 尾码长度不足时降权：例如 320********7 会误吸掉 27/027
                    tail_digits = re.findall(r"[0-9Xx×]", raw[3:])
                    if len(tail_digits) == 1:
                        score = base_score - 28
                    elif len(tail_digits) == 2:
                        score = base_score - 18
                    elif len(tail_digits) == 3:
                        score = base_score - 10
                    else:
                        score = base_score
                    if score > 0:
                        cands.append((normalized, score, tag))

            # 工具：判断片段里是否出现“身份证”
            def has_id_keyword(chunk: str, pos: int) -> bool:
                left = max(0, pos - 24)
                right = min(len(chunk), pos + 24)
                return "身份证" in chunk[left:right]

            lines = [ln for ln in text.splitlines() if ln is not None and str(ln).strip()]
            chunks = set()
            # 行内噪声与同行拼接
            for ln in lines:
                if not str(ln):
                    continue
                chunks.add(str(ln))
                ln_nospace = re.sub(r"\s+", "", str(ln))
                if ln_nospace:
                    chunks.add(ln_nospace)

            # 跨行片段拼接（1~4行）
            for i in range(len(lines)):
                if not str(lines[i]).strip():
                    continue
                chunks.add(str(lines[i]))
                chunks.add(re.sub(r"\s+", "", str(lines[i])))
                for w in range(2, 5):
                    if i + w > len(lines):
                        break
                    window = "".join(str(x) for x in lines[i:i+w])
                    chunks.add(window)
                    window_nospace = re.sub(r"\s+", "", window)
                    if window_nospace:
                        chunks.add(window_nospace)

            # 若 text 本身是单行 OCR，补充一次直接匹配
            if text:
                compact_src = re.sub(r"\s+", "", text)
                chunks.add(compact_src)

            for chunk in chunks:
                # 固定规则：前3位 + 星号 + 后码（支持尾码不足与多星）
                for m in re.finditer(r'(\d{3})\s*([*＊]+)\s*([0-9Xx×＊*]{1,10})(?![0-9])', chunk):
                    score = 120 + (20 if has_id_keyword(chunk, m.start()) else 0)
                    if has_datetime_suffix(chunk, m.end()):
                        score -= 8
                    add(f"{m.group(1)}{m.group(2)}{m.group(3)}", score, "id_base_3head")

                # 顺序漂移：尾码先于头码（常见OCR乱序）
                for m in re.finditer(r'([0-9Xx×＊*]{1,10})\s*([*＊]+)\s*(\d{3})(?![0-9])', chunk):
                    score = 110 + (20 if has_id_keyword(chunk, m.start()) else 0)
                    if has_datetime_suffix(chunk, m.end()):
                        score -= 8
                    add(f"{m.group(3)}{m.group(2)}{m.group(1)}", score, "id_reversed_head_tail")

                # 前3位 + 星号 + 超长后码（如前缀是身份证尾码+日期）
                for m in re.finditer(r'(\d{3})\s*([*＊]{3,})\s*([0-9Xx＊*]{4,})(?![0-9])', chunk):
                    score = 125 + (20 if has_id_keyword(chunk, m.start()) else 0)
                    if has_datetime_suffix(chunk, m.end()):
                        score -= 8
                    add(f"{m.group(1)}{m.group(2)}{m.group(3)}", score, "id_long_tail_noise")

                # 规则增强：要求邻域出现“身份证”关键字时再匹配，可减少误识别
                for m in re.finditer(r'身份证[^\n]{0,20}?(\d{3})\s*([*＊]+)\s*([0-9Xx＊*]{1,10})(?![0-9])', chunk):
                    score = 160
                    if has_datetime_suffix(chunk, m.end(2)):
                        score -= 8
                    add(f"{m.group(1)}{m.group(2)}{m.group(3)}", score, "id_keyword_anchor")

                # 模式A: 标准格式 前6+4位+*+4位
                # 例: 前缀、年份片段、脱敏片段和尾码分散出现时，合并为标准脱敏格式。
                for m in re.finditer(r'(\d{6})\s*(\d{4})\s*[*＊]{2,}\s*[*＊]{2,}\s*(\d{3}[\dXx])', chunk):
                    score = 80 + (20 if has_id_keyword(chunk, m.start()) else 0)
                    add(m.group(1) + m.group(2) + "**********" + m.group(3), score, "id_template_6_plus")

                # 模式A2: 6+4+星号+2位校验（短尾）
                for m in re.finditer(r'(\d{6})\s*(\d{4})\s*[*＊]{2,}\s*(\d{2,4})', chunk):
                    score = 70 + (20 if has_id_keyword(chunk, m.start()) else 0)
                    add(m.group(1) + m.group(2) + "**********" + m.group(3), score, "id_template_6_short")

                # 模式C: 宽松 前6+星号+后4
                for m in re.finditer(r'(\d{6})\s*[*＊]{4,}\s*(\d{4})', chunk):
                    score = 50 + (20 if has_id_keyword(chunk, m.start()) else 0)
                    add(m.group(1) + "**********" + m.group(2), score, "id_template_6_loose")

            # 去重 + 按分值优先，分值同分用短文本稳定顺序
            unique = {}
            for val, score, tag in cands:
                cur = unique.get(val)
                if cur is None or score > cur[0]:
                    unique[val] = (score, tag)

            if not unique:
                return []

            # 统一按“高分优先 + 有效候选优先”
            sorted_items = sorted(unique.items(), key=lambda kv: kv[1][0], reverse=True)
            return sorted_items

        # 搜索范围：优先分隔符附近，再全量搜索
        search_texts = []
        if sep_idx >= 0 and sep_idx + 1 < len(merged_lines):
            search_texts.append("\n".join(merged_lines[max(0, sep_idx - 1):]))
            search_texts.append("\n".join(merged_lines[sep_idx:]))
        search_texts.append("\n".join(merged_lines))

        best = ("", -1, "")
        for search_text in search_texts:
            raw_text = normalize_id_text(search_text)
            no_time_text = strip_datetime_noise(raw_text)
            for t in (raw_text, no_time_text):
                candidates = collect_matches(t)
                if candidates and candidates[0][1][0] > best[1]:
                    best = (candidates[0][0], candidates[0][1][0], candidates[0][1][1])

            compact = re.sub(r"\s+", "", raw_text)
            no_time_compact = re.sub(r"\s+", "", no_time_text)
            for t in (compact, no_time_compact):
                candidates = collect_matches(t)
                if candidates and candidates[0][1][0] > best[1]:
                    best = (candidates[0][0], candidates[0][1][0], candidates[0][1][1])

            # 保留空格版兼容 OCR 空格断词，作为补充
            candidates = collect_matches(raw_text)
            if candidates and candidates[0][1][0] > best[1]:
                best = (candidates[0][0], candidates[0][1][0], candidates[0][1][1])

        if not best[0]:
            return "", {
                "source": "missing",
                "rules": [],
                "rule_processed": False,
            }

        return best[0], {
            "source": "id_pattern_reconstruct",
            "rules": [best[2]],
            "rule_processed": True,
            "score": best[1],
        }

    @staticmethod
    def _extract_id(merged_lines: list, sep_idx: int) -> str:
        return BusinessCheckScene._extract_id_with_meta(merged_lines, sep_idx)[0]

    # ========== 姓名提取 ==========

    @staticmethod
    def _extract_name(merged_lines: list, sep_idx: int, filename_name: str = "") -> str:
        """从分隔符附近提取中文姓名"""
        if filename_name and len(filename_name) >= 2:
            return filename_name

        # 优先从分隔符前几行找
        start = max(0, sep_idx - 3) if sep_idx >= 0 else 0
        end = sep_idx if sep_idx >= 0 else len(merged_lines)

        for i in range(start, end):
            line = merged_lines[i].strip()
            # 纯中文2-6字
            m = re.match(r'^([\u4e00-\u9fff]{2,6})$', line)
            if m:
                return m.group(1)

        # 全量搜索（排除常见非姓名关键词）
        skip_words = {"查询", "结果", "截图", "时间", "运营商", "手机", "工商", "信息"}
        for line in merged_lines:
            line = line.strip()
            m = re.match(r'^([\u4e00-\u9fff]{2,4})$', line)
            if m and m.group(1) not in skip_words:
                return m.group(1)

        if filename_name:
            return filename_name
        return ""

    @staticmethod
    def _extract_name_from_filename(filename: str) -> str:
        if not filename:
            return ""

        stem = Path(filename).name
        if not stem:
            return ""
        stem = stem.rsplit(".", 1)[0]

        stem = stem.strip()
        noise = {
            "无经商办企业",
            "无经商办企",
            "有经商办企业",
            "经商办企业",
            "投资任职信息查询",
            "投资任职情况查询",
            "查询",
            "结果",
            "截图",
            "时间",
            "运营商",
            "手机",
            "工商",
            "信息",
        }

        candidates = []
        # 处理含分隔符场景：73820803-张三-xxx / xxx-张三-xxx / 张三 xxx
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

        # 兜底：直接抽取中文片段
        if not candidates:
            candidates = re.findall(r"[\u4e00-\u9fff]{2,8}", stem)

        for name in candidates:
            if name in noise:
                continue
            if 2 <= len(name) <= 8:
                return name

        return ""

    @staticmethod
    def _is_no_result_query(merged_lines: list, sep_idx: int) -> bool:
        """是否是未查询到任职信息场景。"""
        start = sep_idx if sep_idx >= 0 else 0
        for line in merged_lines[start:]:
            if not line:
                continue
            txt = str(line)
            if "未查询到" in txt and "任职" in txt and "投资" in txt:
                return True
            if "查询结论" in txt and "未查询到" in txt:
                return True
        return False

    # ========== 截图日期/时间 ==========

    @staticmethod
    def _extract_capture_datetime(merged_lines: list, sep_idx: int) -> Tuple[str, str]:
        """提取截图中的日期（MM-DD）与时间（HH:MM）"""
        if not merged_lines:
            return "", ""

        start = 0 if sep_idx < 0 else max(0, sep_idx - 2)
        end = sep_idx + 6 if sep_idx >= 0 else min(len(merged_lines), 20)
        lines = merged_lines[start:min(len(merged_lines), max(end, 20))]
        if not lines:
            lines = merged_lines[:20]
        lines = lines[:20]

        def norm_time(value: str) -> str:
            t = re.sub(r"\s+", "", value)
            m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", t)
            if not m:
                return ""
            return f"{int(m.group(1)):02d}:{m.group(2)}"

        def norm_mmdd(m: str, d: str) -> str:
            try:
                mi = int(m)
                di = int(d)
            except Exception:
                return ""
            if 1 <= mi <= 12 and 1 <= di <= 31:
                return f"{mi:02d}-{di:02d}"
            return ""

        def is_time_like(v: str) -> bool:
            return bool(norm_time(v))

        def is_id_like(line: str) -> bool:
            return bool(re.search(r"\d{3}\s*[*＊]", line))

        def line_score(line: str, idx: int) -> int:
            score = 20
            if is_id_like(line):
                score -= 8
            if sep_idx >= 0:
                score -= abs(idx - sep_idx)
            if "截图" in line or "时" in line:
                score += 2
            return score

        candidates_dt = []
        date_lines = []
        time_lines = []

        for idx, raw in enumerate(lines):
            line = str(raw or "").replace("\u200b", "").replace("\uFEFF", "")
            if not line:
                continue
            compact = re.sub(r"\s+", "", line)

            # 同行带日期时间（包含分隔符）
            for mmdd_m in re.finditer(r"(?<!\d)(?P<m>\d{1,2})[-/](?P<d>\d{1,2})\D+(?P<t>\d{1,2}:\d{2})(?!\d)", line):
                mm = norm_mmdd(mmdd_m.group("m"), mmdd_m.group("d"))
                tt = norm_time(mmdd_m.group("t"))
                if mm and tt:
                    candidates_dt.append((idx, mm, tt, 3 + line_score(line, idx)))

            # 无分隔符日期+时间（如 0722 10:09 -> 07-22 10:09）
            for mmdd_m in re.finditer(r"(?<!\d)(?<![-/])(?P<m>\d{1,2})(?P<d>\d{2})(?P<t>\d{1,2}:\d{2})(?!\d)", compact):
                mm = norm_mmdd(mmdd_m.group("m"), mmdd_m.group("d"))
                tt = norm_time(mmdd_m.group("t"))
                if mm and tt:
                    candidates_dt.append((idx, mm, tt, 2 + line_score(line, idx)))

            # 07-2210:09 / 07/2210:09
            for mmdd_m in re.finditer(r"(?<!\d)(?P<m>\d{1,2})[-/](?P<d>\d{2})(?P<t>\d{1,2}:\d{2})(?!\d)", compact):
                mm = norm_mmdd(mmdd_m.group("m"), mmdd_m.group("d"))
                tt = norm_time(mmdd_m.group("t"))
                if mm and tt:
                    candidates_dt.append((idx, mm, tt, 2 + line_score(line, idx)))

            # 同行：先时间后日期（少见，例如 10:09 07-22）
            for mmdd_m in re.finditer(r"\b(?P<t>\d{1,2}:\d{2})\D+(?P<m>\d{1,2})[-/](?P<d>\d{1,2})(?!\d)", line):
                mm = norm_mmdd(mmdd_m.group("m"), mmdd_m.group("d"))
                tt = norm_time(mmdd_m.group("t"))
                if mm and tt:
                    candidates_dt.append((idx, mm, tt, 1 + line_score(line, idx)))

            if re.search(r"\b(?P<m>\d{1,2}[-/]\d{1,2})\b", line):
                for date_m in re.finditer(r"(?<!\d)(?P<m>\d{1,2})[-/](?P<d>\d{1,2})(?!\d)", line):
                    mmdd = norm_mmdd(date_m.group("m"), date_m.group("d"))
                    if mmdd:
                        date_lines.append((idx, mmdd, line_score(line, idx)))

            for time_m in re.finditer(r"\b(?P<t>\d{1,2}:\d{2})\b", compact):
                tt = norm_time(time_m.group("t"))
                if tt:
                    time_lines.append((idx, tt, line_score(line, idx)))

        if candidates_dt:
            candidates_dt.sort(key=lambda x: (x[3], -x[0]), reverse=True)
            return candidates_dt[0][1], candidates_dt[0][2]

        if date_lines and time_lines:
            # 用最近日期与最近时间组成一组（优先时间在日期前后 2 行内）
            best = None
            for d_idx, d_v, d_sc in date_lines:
                for t_idx, t_v, t_sc in time_lines:
                    gap = abs(t_idx - d_idx)
                    if gap <= 2:
                        weight = d_sc + t_sc - gap
                        best = (weight, d_v, t_v)
                        break
                if best:
                    break
            if best:
                return best[1], best[2]

            # fallback：取最早出现的日期 + 最近时间
            d_v = max(date_lines, key=lambda x: x[2])[1]
            t_v = max(time_lines, key=lambda x: x[2])[1]
            return d_v, t_v

        if time_lines:
            return "", max(time_lines, key=lambda x: x[2])[1]

        if date_lines:
            return max(date_lines, key=lambda x: x[2])[1], ""

        return "", ""

    # ========== 栏位统计 ==========

    @staticmethod
    def _count_field(text: str, field_name: str) -> int:
        """统计栏位数字，清理左侧竖线误识别"""
        # 栏位模式优化：
        # 1) 兼容 "1相关企业"、"I相关企业"、"|相关企业" 等噪音前缀
        # 2) 优先匹配字段名后面的数字：相关企业0 / 相关企业:0
        # 3) 兜底匹配字段独占行 + 下一行数字
        # 4) 禁止跨行到时间/其他数字位置回退，避免拿到 10:09 -> 9 这类污染
        if not text:
            return 0

        sep_noise = "|｜‖︱︳\u2502\uFF5C丨\"“”‘’「」【】『』[]()<>-\u200b\uFEFFilI丨"
        prefix_strip = re.compile(rf"^[{re.escape(sep_noise)}]+")
        line_prefix_strip_digits = re.compile(rf"^\d+(?=(?:相关企业|任职|参股))")

        # 明细页持股比例示例：参股0.4、参股10% 等不应作为“企业数”
        def has_share_ratio_noise(line: str, value: str) -> bool:
            if not value:
                return True
            idx = line.find(value)
            if idx < 0:
                return True
            tail = line[idx + len(value):].strip()
            # 允许仅遇到结尾、|、/、-、:、空格等分隔符；命中 . 或 % 时视为持股比例
            if tail.startswith(".") or tail.startswith("％") or tail.startswith("%") or "比例" in line:
                return True
            if tail and tail[0].isdigit():
                return True
            return False

        def line_score(line: str, i: int) -> int:
            # 计数口径更偏好“摘要区域”的简洁栏位行
            sc = 100 - i
            if "查询结论" in line:
                sc -= 10
            if "未查询到" in line:
                sc -= 8
            return sc

        def add_candidate(cands: List[tuple], value: str, score: int, line: str):
            try:
                v = int(value)
            except Exception:
                return
            if v < 0:
                return
            if has_share_ratio_noise(line, value):
                return
            cands.append((score, -len(cands), v))

        def normalize_field_line(line: str) -> str:
            ln = line.replace("\u200b", "").replace("\uFEFF", "").strip()
            ln = prefix_strip.sub("", ln)
            ln = line_prefix_strip_digits.sub("", ln)
            return ln.strip()

        candidates = []
        lines = text.splitlines()

        # 先取“行头”命中（字段在行首、噪音仅含分隔符/图标字符），减少明细页文本污染
        line_head_pattern = re.compile(
            rf"^(?:[{re.escape(sep_noise)}]*){re.escape(field_name)}\s*[:：]?\s*(\d+)(?![.\uFF05%％])(?=$|\D)"
        )

        # 直接命中文段：相关企业0 / |相关企业0| / 相关企业:0
        for i, line in enumerate(lines):
            clean_line = normalize_field_line(line)
            # 栏位名在前、数字在后
            for m in line_head_pattern.finditer(clean_line):
                add_candidate(candidates, m.group(1), line_score(clean_line, i), clean_line)

            # 兼容左侧先数字后字段的格式：1相关企业 / I相关企业0x
            # 先清理数字前缀，避免把噪音数字当计数
            if not re.search(rf'{field_name}\s*[:：]?\s*\d+', clean_line):
                for m in re.finditer(rf'\d+\s*{field_name}\s*[:：]?\s*(\d+)(?![.\uFF05%％])(?=$|\D)', clean_line):
                    add_candidate(candidates, m.group(1), line_score(clean_line, i) - 2, clean_line)

            # 字段独占行，下一行通常有数字
            if re.fullmatch(rf'{field_name}\s*[:：]?\s*$', clean_line) and len(lines) > 1:
                if i + 1 < len(lines):
                    nxt = normalize_field_line(lines[i + 1])
                    if re.fullmatch(r'\d+', nxt) and not has_share_ratio_noise(clean_line, nxt):
                        add_candidate(candidates, nxt, line_score(clean_line, i) - 4, clean_line)

        if candidates:
            candidates.sort(key=lambda x: (x[0], x[2]), reverse=True)
            return candidates[0][2]

        return 0

    # ========== 查询结论 ==========

    @staticmethod
    def _extract_conclusion(merged_lines: list) -> str:
        """提取查询结论"""
        for line in merged_lines:
            if "未查询到" in line or "查询结论" in line:
                # 提取结论内容
                m = re.search(r'查询结论[：:]\s*(.+)', line)
                if m:
                    return m.group(1).strip()
                if "未查询到" in line:
                    return line.strip()
        return ""

    # ========== 多行任职信息合并 ==========

    @staticmethod
    def _merge_employment(biz_lines: list) -> str:
        """
        格式3：合并同一人的多行任职信息
        输出: 公司名(相关企业N) | 角色(任职N) | 参股(N)
        多行公司名合并为一行
        """
        if not biz_lines:
            return ""

        # 清理栏位：去掉 "1相关企业" 的前导"1"
        cleaned = []
        for line in biz_lines:
            # 清理 "1相关企业" "1任职" "1参股" 前的竖线/数字
            line = re.sub(r'^[|｜‖︱︳\u2502\uFF5C丨1lIl\"“”‘’「」【】『』\\[\\]()<>\-\u200b\uFEFF]+\s*(?=[相关企业任职参股])', '', line)
            line = re.sub(r'\s*[|｜]\s*$', '', line)
            cleaned.append(line)

        # 合并连续的非结构行（公司名可能跨多行）
        records = []
        current_record = []
        for line in cleaned:
            line = line.strip()
            if not line:
                continue

            # 检测是否是新记录的开始（包含企业关键字或公司名特征）
            is_separator = bool(re.match(
                r'^(相关企业|任职|参股|查询结论|未查询)',
                line
            ))

            if is_separator and current_record:
                records.append(" ".join(current_record))
                current_record = []
            
            current_record.append(line)

        if current_record:
            records.append(" ".join(current_record))

        # 去除重复栏位名
        result_lines = []
        for rec in records:
            # 去掉独立的 "相关企业" "任职" "参股" 等纯栏位名
            if re.match(r'^(相关企业|任职|参股)\s*\d*$', rec.strip()):
                continue
            result_lines.append(rec.strip())

        return " | ".join(result_lines)

    # ========== 摘要 ==========

    def summary(self, parsed_results: list) -> dict:
        base = super().summary(parsed_results)
        base["has_name"] = sum(1 for r in parsed_results if r.get("姓名"))
        base["has_id"] = sum(1 for r in parsed_results if r.get("身份证号"))
        base["has_employment"] = sum(1 for r in parsed_results if r.get("任职情况信息"))
        total_corp = sum(r.get("相关企业", 0) for r in parsed_results)
        total_work = sum(r.get("任职", 0) for r in parsed_results)
        total_share = sum(r.get("参股", 0) for r in parsed_results)
        base["total_corp"] = total_corp
        base["total_work"] = total_work
        base["total_share"] = total_share
        return base


register_scene(BusinessCheckScene())
