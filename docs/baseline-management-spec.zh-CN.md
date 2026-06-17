# OCR Consensus 基线管理与差异稽核 Spec

## 1. 背景

当前 OCR Consensus 已经具备基础信息、任职信息、四路 OCR 原始结果、Harness 规则增强结果、字段级共识结果和人工复核状态。

下一阶段需要将“识别结果”沉淀为可版本化、可追踪、可比对、可稽核的基线数据资产。目标是：当用户导入新一批数据后，系统可以直接和历史基线进行字段级、记录级、任职明细级比对，快速定位真实变化和可疑变化，减少人工逐条复核成本。

## 2. 目标

### 2.1 产品目标

1. 支持将一次 OCR 任务的结构化结果保存为基线版本。
2. 支持基线版本管理，区分快照、候选基线、正式基线。
3. 支持当前任务结果与任意基线版本进行差异比对。
4. 支持基线版本之间进行差异比对。
5. 支持按差异类型筛选、排序、查看详情和人工确认。
6. 支持从已确认差异生成新的基线版本。
7. 支持标记基线错误、修订基线字段，并保留审计记录。

### 2.2 工程目标

1. 基线数据与任务运行数据解耦，避免任务结果被覆盖或删除后基线不可用。
2. 基线版本不可变，更新必须生成新版本。
3. 差异比对结果可复现，需记录使用的基线版本、当前任务、规则版本和代码版本。
4. 基线比对逻辑应优先服务稽核效率，避免截图时间等低价值变化污染主差异列表。
5. 所有敏感业务数据只存储在受控环境，不进入 GitHub 仓库。

## 3. 非目标

1. 第一阶段不做复杂权限系统，仅预留创建人、更新人、操作人字段。
2. 第一阶段不做多人并发编辑冲突处理，仅支持串行保存和更新。
3. 第一阶段不把基线当作绝对真值，允许标记和修订基线错误。
4. 第一阶段不实现复杂机器学习匹配，先采用确定性规则 + 相似度阈值 + 人工确认。

## 4. 核心概念

### 4.1 任务结果

任务结果是一次上传处理后的输出，包含：

- 原始图片列表
- 四路 OCR 执行结果
- Harness 规则增强结果
- 共识结构化结果
- 人工复核状态
- 任务元数据

任务结果是运行态数据，可以被清理、重跑或覆盖。

### 4.2 基线

基线是从任务结果中保存出来的稳定版本，包含共识结构化结果、字段状态、证据摘要、人工复核状态和版本元信息。

基线不是原始 OCR 文本，也不是绝对真值。它是某一时点经过系统和人工确认后的可追踪数据快照。

### 4.3 基线版本类型

| 类型 | 含义 | 是否可作为默认比对对象 |
| --- | --- | --- |
| snapshot | 任意任务结果快照 | 否 |
| candidate | 准备成为正式基线的候选版本 | 可选 |
| golden | 人工确认后的正式基线 | 是 |

### 4.4 差异稽核

差异稽核是将当前任务结果与基线版本进行结构化比对，识别记录新增、记录缺失、字段变化、任职明细变化和 OCR 质量变化。

## 5. 总体流程

```text
导入新数据
  -> OCR 多路执行
  -> Harness 规则增强
  -> 字段级共识
  -> 当前任务结构化结果
  -> 选择基线版本进行比对
  -> 生成差异报告
  -> 人工稽核差异
  -> 接受当前值 / 保留基线值 / 手工修正 / 标记基线错误
  -> 生成新的 candidate baseline
  -> 人工确认后升级为 golden baseline
```

## 6. 数据范围

### 6.1 基础信息字段

基础信息字段包括：

| 字段 | 是否参与业务差异 | 说明 |
| --- | --- | --- |
| 文件编号 | 是 | 重要匹配键之一 |
| 文件名 | 否 | 展示和追踪用，不作为强业务字段 |
| 姓名 | 是 | 需结合 OCR 与文件名规则判断 |
| 身份证号 | 是 | 脱敏格式，重点字段 |
| 截图日期 | 弱参与 | 单独作为采集时间变化处理 |
| 截图时间 | 弱参与 | 单独作为采集时间变化处理 |
| 相关企业数 | 是 | 需与任职明细数量对账 |
| 任职企业数 | 是 | 需与任职明细数量对账 |
| 参股企业数 | 是 | 需与任职明细数量对账 |
| 查询结论 | 可选 | 若与明细和数量重复，可低优先级 |

### 6.2 任职信息字段

任职信息字段包括：

| 字段 | 是否参与业务差异 | 说明 |
| --- | --- | --- |
| 序号 | 否 | 仅展示，不作为企业匹配主键 |
| 企业名称 | 是 | 企业匹配核心字段 |
| 经营状态 | 是 | 支持多状态，如吊销、已注销等 |
| 承担职务 | 是 | 支持多个职务合并展示 |
| 持股比例 | 是 | 仅任职明细中的参股比例，不等同于参股企业数 |
| 来源图片 | 是 | 一人多图场景需要追踪来源 |
| 字段状态 | 是 | 一致、冲突、缺失、占位、证据补齐 |
| 来源模型 | 证据 | 用于追溯，不直接作为业务差异 |
| 置信度 | 证据 | 用于决策加权和质量分析 |

## 7. 记录匹配策略

### 7.1 人员记录匹配

不能只依赖文件名。建议使用多级匹配策略。

优先级：

1. `文件编号 + 姓名`
2. `身份证号前3后4 + 姓名`
3. `文件编号`
4. `图片 hash`
5. `姓名 + 截图时间接近 + 其他字段相似`
6. 模糊匹配候选，进入人工确认

### 7.2 匹配结果状态

| 状态 | 含义 |
| --- | --- |
| exact | 强匹配 |
| probable | 高置信匹配 |
| weak | 弱匹配，需要复核 |
| unmatched_new | 当前结果新增 |
| unmatched_missing | 基线中存在但当前缺失 |

### 7.3 一人多图处理

一人多图需要先按人员维度聚合，再做基线比对。

聚合键建议：

```text
文件编号 + 姓名
```

当文件名姓名与 OCR 姓名冲突时，应使用当前姓名共识规则产出的最终姓名，同时保留文件名姓名作为证据字段。

一人多图下应保留：

- 人员级聚合结果
- 图片级原始结果
- 每条任职明细的来源图片
- 每张图片的截图时间

### 7.4 任职明细挂载兼容

任职明细必须能稳定挂到人员记录上。由于不同版本任务输出字段可能不一致，挂载优先级为：

1. `baseline_record_id`
2. `人员键`
3. `文件编号 + 姓名`
4. `record_key`
5. 来源图片文件名

同一人员多图时，任职明细应挂到同一 `人员键` 下的所有图片记录，避免明细只挂到其中一张图而另一张图详情为空。

任职明细字段读取应兼容标准列和原始证据列：

| 标准字段 | 兼容来源 |
| --- | --- |
| 企业名称 | `company_name / 企业名称 / 公司名称` |
| 经营状态 | `business_status / 经营状态` |
| 承担职务 | `role / 承担职务 / 职务` |
| 持股比例 | `share_ratio / 持股比例 / 持股` |
| 来源图片 | `source_image / 来源文件 / 文件名` |

若标准列为空，应从 `evidence_summary` 原始行中回填，避免旧基线或旧任务出现“有任职数据但详情取空”的问题。

## 8. 企业明细匹配策略

### 8.1 不按序号直接匹配

企业序号只代表页面展示顺序，不应作为企业匹配主键。

企业匹配应使用：

```text
企业名称归一化 key + 经营状态 + 职务摘要
```

### 8.2 企业名称归一化

归一化规则包括：

1. 去除空格、换行、制表符。
2. 去除明显 OCR 噪声符号。
3. 统一全角半角。
4. 统一常见公司后缀表达。
5. 过滤无企业特征的职务残片。
6. 对缺失企业名称使用占位符，但标记为弱证据。

### 8.3 企业匹配状态

| 状态 | 含义 |
| --- | --- |
| exact | 企业名称归一化后完全一致 |
| fuzzy | 企业名称高度相似，需要记录相似度 |
| placeholder | 使用未识别企业占位匹配 |
| unmatched_new | 当前结果新增企业 |
| unmatched_missing | 基线中存在但当前缺失企业 |
| conflict | 企业疑似同一条，但字段冲突较大 |

### 8.4 未识别企业占位

当基础信息显示存在任职数量，但企业名称未识别时，可以使用：

```text
未识别企业#1
未识别企业#2
```

占位企业必须带有：

```text
status = placeholder
confidence = low
requires_review = true
```

占位企业不能自动升级为一致，只能通过人工复核或后续更强证据补齐。

## 9. 截图日期时间处理策略

截图日期时间不应作为普通业务字段直接参与强差异判断。

### 9.1 字段分类

| 字段 | 类型 | 比对策略 |
| --- | --- | --- |
| 截图日期 | 采集时间字段 | 弱比对 |
| 截图时间 | 采集时间字段 | 弱比对 |
| 导入时间 | 系统元数据 | 不参与业务差异 |
| 处理时间 | 系统元数据 | 不参与业务差异 |

### 9.2 差异判断

| 情况 | 状态 | 是否进入主稽核 |
| --- | --- | --- |
| 业务字段一致，仅截图时间不同 | business_same_capture_time_changed | 否 |
| 当前截图时间晚于基线 | capture_time_newer | 否，仅提示 |
| 当前截图时间早于基线 | capture_time_older | 否，仅提示 |
| 基线有时间，当前缺失 | capture_time_missing_current | 否，仅提示 |
| 基线缺失，当前有时间 | capture_time_filled | 否，仅提示 |
| 时间格式非法 | capture_time_invalid | 否，仅提示 |
| 业务字段变化且截图时间更晚 | business_changed_with_newer_capture | 是 |
| 业务字段变化但截图时间更早 | business_changed_with_older_capture | 是，高风险 |

### 9.3 聚合状态字段

建议在 diff 中拆分三个状态：

```json
{
  "business_diff_status": "same | changed | new | missing",
  "capture_time_diff_status": "same | newer | older | missing | filled | invalid",
  "ocr_quality_diff_status": "same | improved | regressed | mixed"
}
```

### 9.4 UI 展示原则

1. 截图时间保留展示。
2. 截图时间变化不默认计入字段冲突。
3. 仅截图日期/时间变化时，风险为 info，不进入待复核。
4. 截图时间更旧时，仅在列表中提示，不单独触发复核。
5. 业务字段变化且截图时间更旧时，置为高风险复核。

### 9.5 批次图片比对策略

基线比对需要支持“基线批次图片”和“本次批次图片”两组图片独立预览。

图片不是业务字段，但属于稽核证据完整性字段。

| 情况 | 状态 | 处理 |
| --- | --- | --- |
| 两边图片集合一致 | image_same | 不提示 |
| 本次有、基线无 | image_added | 不作为差异，仅用于预览 |
| 基线有、本次无 | image_missing | 不作为差异，仅用于预览 |
| 图片集合变化但结构化字段不变 | image_changed_only | 不进入复核 |

图片集合来源：

1. 人员记录自身图片字段，如 `filename / file_name / files / images / source_images`。
2. 任职明细来源图片字段。
3. 去重后按批次分别展示。

图片比对原则：

1. 基线批次和本次批次各自独立翻页。
2. 不做跨批次图片内容相似度判断。
3. 图片增加或减少不计入差异，也不进入待复核。
4. 图片只作为人工核验结构化差异时的证据预览。
5. 如果图片缺失导致结构化字段缺失，应由字段级 diff 共同触发复核。

## 10. 差异类型设计

### 10.1 记录级差异

| 类型 | 说明 |
| --- | --- |
| record_added | 当前结果新增记录 |
| record_missing | 基线记录在当前结果中缺失 |
| record_matched | 记录成功匹配 |
| record_weak_matched | 弱匹配，需要复核 |
| person_multi_image_changed | 一人多图数量或来源变化 |

### 10.2 基础字段级差异

| 类型 | 说明 |
| --- | --- |
| field_same | 字段一致 |
| field_changed | 字段值变化 |
| field_missing_current | 当前缺失 |
| field_missing_baseline | 基线缺失 |
| field_filled | 当前补齐了基线缺失字段 |
| field_conflict_current | 当前共识内部仍有冲突 |
| field_rule_processed | 当前字段经过规则加工 |

### 10.3 任职明细级差异

| 类型 | 说明 |
| --- | --- |
| company_added | 当前新增企业 |
| company_missing | 基线企业缺失 |
| company_name_changed | 企业名称变化 |
| company_status_changed | 经营状态变化 |
| company_role_changed | 职务变化 |
| company_share_changed | 持股比例变化 |
| company_placeholder_changed | 占位企业变化 |
| company_evidence_changed | 最终值一致但证据来源变化 |

### 10.4 数量对账差异

| 类型 | 说明 |
| --- | --- |
| count_same | 数量一致 |
| count_changed | 数量字段变化 |
| count_detail_mismatch | 数量字段与企业明细不一致 |
| count_placeholder_filled | 通过占位补齐数量 |
| count_evidence_weak | 数量满足但证据弱 |

### 10.5 证据级差异

| 类型 | 说明 |
| --- | --- |
| evidence_same | 模型证据无明显变化 |
| evidence_confidence_changed | 置信度变化 |
| evidence_source_changed | 支持模型变化 |
| evidence_rule_changed | 规则加工状态变化 |
| evidence_raw_ocr_changed | 原始 OCR 变化 |

证据级差异默认不进入主业务稽核，但应保留给质量分析。

## 11. 差异严重等级

| 等级 | 含义 | 示例 |
| --- | --- | --- |
| info | 信息提示 | 仅截图时间更新 |
| low | 低风险 | 当前补齐了基线缺失字段 |
| medium | 中风险 | 时间缺失、弱匹配、证据变化 |
| high | 高风险 | 身份证号变化、企业新增/减少、状态变化 |
| critical | 极高风险 | 业务字段变化且疑似旧截图、匹配冲突严重 |

## 12. 人工稽核动作

### 12.1 记录级动作

| 动作 | 含义 |
| --- | --- |
| accept_current | 接受当前结果 |
| keep_baseline | 保留基线结果 |
| manual_correct | 手工修正 |
| mark_baseline_wrong | 标记基线错误 |
| mark_no_action | 标记无需处理 |
| defer | 暂缓处理 |

### 12.2 字段级动作

| 动作 | 含义 |
| --- | --- |
| accept_current_field | 接受当前字段值 |
| keep_baseline_field | 保留基线字段值 |
| edit_field | 手工编辑字段值 |
| ignore_field_diff | 忽略字段差异 |
| require_reprocess | 要求重新 OCR 或重新抽取 |

### 12.3 审计记录

每次人工动作必须记录：

- 操作人
- 操作时间
- 操作对象
- 旧值
- 新值
- 操作理由
- 来源基线版本
- 当前任务 ID

## 13. 基线更新策略

### 13.1 不允许直接覆盖

基线版本不可变。任何更新都必须生成新版本。

```text
old golden baseline + accepted diffs + manual corrections = new candidate baseline
```

新 candidate 经过人工确认后，才能提升为 golden。

### 13.2 基线生成规则

生成新基线时：

1. 对已接受当前值的字段，使用当前结果。
2. 对保留基线的字段，沿用旧基线。
3. 对手工修正字段，使用修正值。
4. 对未处理高风险差异，不允许生成 golden。
5. 对仅截图时间变化的记录，可批量接受。
6. 对证据级变化但最终值不变的记录，可不影响新基线。

### 13.3 基线状态流转

```text
snapshot -> candidate -> golden
                  -> rejected
```

## 14. 数据存储设计

### 14.1 推荐存储

第一版建议使用 SQLite，而不是纯 JSON 文件。

原因：

1. 基线版本会持续增加。
2. 差异结果需要分页、筛选、排序。
3. 人工复核动作需要审计记录。
4. UI 性能会明显优于加载大 JSON。
5. 后续迁移到服务端数据库更容易。

### 14.2 表设计草案

#### baseline_versions

| 字段 | 说明 |
| --- | --- |
| id | 基线版本 ID |
| name | 基线名称 |
| type | snapshot/candidate/golden |
| parent_id | 上一个基线版本 |
| source_task_id | 来源任务 ID |
| record_count | 记录数 |
| image_count | 图片数 |
| created_at | 创建时间 |
| created_by | 创建人 |
| code_version | 代码版本 |
| harness_version | 规则版本 |
| engine_versions | OCR 引擎版本 JSON |
| status | active/rejected/archived |
| notes | 备注 |

#### baseline_records

| 字段 | 说明 |
| --- | --- |
| id | 记录 ID |
| baseline_id | 基线版本 ID |
| record_key | 人员级匹配 key |
| file_id | 文件编号 |
| file_name | 文件名 |
| person_name | 姓名 |
| masked_id | 脱敏身份证号 |
| capture_datetime | 截图时间 |
| related_count | 相关企业数 |
| employment_count | 任职企业数 |
| shareholding_count | 参股企业数 |
| review_status | 复核状态 |
| evidence_summary | 证据摘要 JSON |

#### baseline_employment_rows

| 字段 | 说明 |
| --- | --- |
| id | 企业明细 ID |
| baseline_record_id | 所属记录 |
| company_key | 企业归一化 key |
| company_name | 企业名称 |
| business_status | 经营状态 |
| role | 承担职务 |
| share_ratio | 持股比例 |
| source_image | 来源图片 |
| row_status | 一致/冲突/占位/证据补齐 |
| evidence_summary | 证据摘要 JSON |

#### compare_jobs

| 字段 | 说明 |
| --- | --- |
| id | 比对任务 ID |
| baseline_id | 基线版本 ID |
| current_task_id | 当前任务 ID |
| compare_type | task_vs_baseline / baseline_vs_baseline |
| created_at | 创建时间 |
| status | running/done/failed |
| summary | 差异汇总 JSON |

#### compare_record_diffs

| 字段 | 说明 |
| --- | --- |
| id | 记录差异 ID |
| compare_job_id | 比对任务 ID |
| baseline_record_id | 基线记录 |
| current_record_id | 当前记录 |
| match_status | exact/probable/weak/unmatched |
| business_diff_status | same/changed/new/missing |
| capture_time_diff_status | same/newer/older/missing/filled/invalid |
| ocr_quality_diff_status | same/improved/regressed/mixed |
| severity | info/low/medium/high/critical |
| requires_review | 是否需要复核 |
| review_status | 待复核/已确认/已忽略 |

#### compare_field_diffs

| 字段 | 说明 |
| --- | --- |
| id | 字段差异 ID |
| record_diff_id | 所属记录差异 |
| section | basic/employment/count/evidence |
| field_name | 字段名 |
| baseline_value | 基线值 |
| current_value | 当前值 |
| diff_type | 差异类型 |
| severity | 严重等级 |
| baseline_evidence | 基线证据摘要 |
| current_evidence | 当前证据摘要 |

#### manual_review_actions

| 字段 | 说明 |
| --- | --- |
| id | 操作 ID |
| target_type | record/field/company |
| target_id | 操作对象 ID |
| action | 操作类型 |
| old_value | 旧值 |
| new_value | 新值 |
| reason | 操作原因 |
| operator | 操作人 |
| created_at | 操作时间 |

## 15. API 设计草案

### 15.1 基线管理

```text
GET  /api/baselines
POST /api/baselines
GET  /api/baselines/{baseline_id}
POST /api/baselines/{baseline_id}/promote
POST /api/baselines/{baseline_id}/archive
```

### 15.2 比对

```text
POST /api/compare
GET  /api/compare/{compare_job_id}
GET  /api/compare/{compare_job_id}/records
GET  /api/compare/{compare_job_id}/records/{record_diff_id}
```

### 15.3 人工稽核

```text
POST /api/review/actions
GET  /api/review/actions?target_type=&target_id=
POST /api/baselines/from-compare/{compare_job_id}
```

## 16. UI 设计

### 16.1 处理台新增入口

在任务完成后增加：

- 保存为基线
- 与基线比对
- 查看差异报告

### 16.2 基线管理页

展示：

- 基线名称
- 版本类型
- 创建时间
- 来源任务
- 记录数
- 图片数
- 创建人
- 状态
- 操作按钮

操作：

- 查看基线
- 设为默认 golden
- 与当前任务比对
- 与其他基线比对
- 归档

设计边界：

1. 基线管理页只浏览“基线自身数据”，不承担差异稽核。
2. 基线详情展示版本信息、基础信息表和任职明细表。
3. 不展示“首条记录预览”，避免把样例预览误认为比对详情。
4. 不展示原始 OCR、模型置信度和方法级证据；这些属于任务质量分析，不属于基线版本管理主流程。
5. 基线版本列表和比对下拉使用同一个选中状态，避免两套选择入口产生歧义。

### 16.3 差异列表页

顶部聚合指标：

- 总记录
- 业务一致
- 有业务差异
- 新增记录
- 缺失记录
- 基础信息变更
- 任职信息变更
- 数量不一致
- 疑似旧截图
- 待人工确认

筛选：

- 只看有变化
- 只看新增
- 只看缺失
- 只看基础信息变化
- 只看任职变化
- 只看高风险
- 只看未确认
- 只看疑似旧截图

列表字段：

| 列 | 内容 |
| --- | --- |
| 状态 | 正常、待复核、新增、缺失 |
| 风险 | info/medium/high/critical |
| 编号 / 姓名 | 人员定位信息 |
| 身份证号 | 当前共识身份证号 |
| 截图时间 | 当前共识截图时间 |
| 基础信息差异 | 直接列出字段级变化，例如 `任职 0 -> 1` |
| 任职明细差异 | 直接列出企业明细变化摘要，例如 `经营状态变化 1` |
| 操作 | 查看记录比对详情 |

设计边界：

1. 差异列表必须先提示具体 diff，不能只显示“有差异”。
2. 列表用于发现问题，详情用于判断原因。
3. 列表比较的是共识后的结构化结果，不展示原始 OCR 证据。

### 16.4 差异详情页

布局建议：

```text
顶部：记录身份、匹配状态、严重等级、复核状态
上方：基线图片 / 本次图片
中部：基础信息共识结果逐字段对比
下方：任职明细先按企业对齐，再展开逐字段对比
```

操作：

- 接受当前结果
- 保留基线结果
- 手工修正
- 标记基线错误
- 标记无需处理
- 上一条/下一条差异

设计边界：

1. 详情页只比较两条“共识后的结构化记录”。
2. 不展示四路 OCR 原文、OCR 置信度、模型报错、方法级详情。
3. 图片用于人工判断原因；结构化表用于确认差异。
4. 一人多图场景按“基线批次图片 / 本次批次图片”分别左右切换，并保留任职明细来源图片字段。
5. 一侧有图、一侧无图，列表中展示为图片增加或图片减少。
6. 任职明细先展示公司级新增、缺失、变化、一致；展开公司后再展示企业名称、经营状态、承担职务、持股比例等字段的左右对比。

### 16.5 基线生成页

展示：

- 已确认差异数量
- 未确认高风险数量
- 将接受的字段数
- 将保留的字段数
- 手工修正字段数
- 仅时间变化自动接受数量

操作：

- 生成 candidate baseline
- 升级为 golden baseline

## 17. 规则版本与可追溯性

每个基线版本必须记录：

- 代码 commit
- Harness 规则版本
- OCR 引擎名称和版本
- 模型路径或模型版本摘要
- 数据源任务 ID
- 创建时间
- 创建人

每次比对任务必须记录：

- 使用的 baseline_id
- 使用的 current_task_id
- 比对规则版本
- 生成时间

## 18. 质量指标

### 18.1 基线质量指标

- 字段完整率
- 人工复核完成率
- 占位企业比例
- 证据补齐字段比例
- 当前基线疑似错误数
- 高风险未确认数

### 18.2 比对质量指标

- 总记录数
- 匹配成功率
- 弱匹配比例
- 业务差异记录数
- 仅截图时间变化记录数
- 任职明细变化记录数
- 数量与明细不一致记录数
- OCR 质量退化记录数

### 18.3 稽核效率指标

- 本次需人工复核记录数
- 自动判定一致记录数
- 自动过滤时间变化记录数
- 人工接受当前比例
- 人工保留基线比例
- 手工修正比例

## 19. MVP 实施计划

### 阶段 1：数据结构与保存

1. 增加 SQLite 基线数据库。
2. 支持将当前任务保存为 snapshot baseline。
3. 支持 baseline_versions、baseline_records、baseline_employment_rows 三类核心数据。
4. 增加基线列表 API。

验收：可以从一次任务结果保存基线，并在基线管理页看到版本记录。

### 阶段 2：基础比对

1. 实现当前任务 vs 基线的记录匹配。
2. 实现基础字段 diff。
3. 实现截图时间弱比对策略。
4. 生成 compare_jobs、compare_record_diffs、compare_field_diffs。

验收：导入新任务后，可以生成基础字段级差异列表，且仅截图时间变化不进入主业务差异。

### 阶段 3：任职明细比对

1. 实现企业名称归一化 key。
2. 实现企业行匹配。
3. 实现企业新增、缺失、字段变化、占位变化 diff。
4. 实现数量字段与企业明细对账。

验收：可以识别任职企业新增、减少、状态变化、职务变化、持股比例变化。

### 阶段 4：差异稽核 UI

1. 增加差异列表页。
2. 增加差异详情页。
3. 支持聚合筛选和高风险筛选。
4. 支持人工动作记录。

验收：用户可以只看变化数据，并对每条差异做接受、保留、修正或忽略。

### 阶段 5：基线更新

1. 根据人工确认动作生成 candidate baseline。
2. 支持 candidate 升级为 golden。
3. 支持标记旧基线归档。
4. 增加基线版本链路展示。

验收：用户可以完成一次从旧基线到新基线的闭环更新。

## 20. 风险与边界

### 20.1 基线错误风险

基线可能存在历史错误，不能作为绝对真值压制当前结果。

应对：支持标记基线错误、字段修订和版本审计。

### 20.2 文件名不规范风险

文件名中的编号和姓名可能错误。

应对：匹配时结合 OCR 姓名、身份证号、文件编号、图片 hash 和人工确认。

### 20.3 一人多图风险

同一人员多张图可能分别包含基础信息和任职信息。

应对：人员级聚合后比对，保留图片级证据。

### 20.4 企业名称 OCR 噪声风险

企业名称容易出现错字、漏字、残片和职位误识别。

应对：企业名称归一化、职务残片过滤、弱匹配标记和人工确认。

### 20.5 截图时间误导风险

截图时间天然变化，若作为普通字段会制造大量无效差异。

应对：独立为采集时间差异，不默认计入业务差异。

## 21. 开放问题

1. 是否需要支持多个 golden baseline 并行存在，例如不同业务场景或不同数据集？
2. 是否需要对每个字段设置不同的差异阈值和严重等级？
3. 是否需要导出 Excel 格式的差异稽核表？
4. 是否需要支持手工导入外部基线？
5. 是否需要在生成新基线前强制完成全部高风险差异复核？
6. 是否需要支持字段级审批流，而不是记录级复核？

## 22. 推荐默认策略

第一版建议采用以下默认策略：

1. 只允许一个 active golden baseline 作为默认比对对象。
2. 基线保存默认生成 snapshot。
3. 人工确认后才能升级为 golden。
4. 仅截图时间变化不进入主业务差异。
5. 当前截图时间早于基线时标记为疑似旧截图。
6. 企业明细不按序号匹配，按企业归一化 key 匹配。
7. 占位企业永远不自动视为强一致。
8. 高风险差异未处理时，不允许生成 golden baseline。
9. 所有人工修正必须记录审计日志。
10. 所有基线版本不可变，更新只能生成新版本。

## 23. 已确认决策

截至 2026-06-17，已确认以下实施约束：

1. 第一版只允许一个 `active golden baseline`。
2. 高风险差异未处理完，不允许生成新的 `golden baseline`。
3. MVP 必须支持 Excel 导出，并要求格式美观。

Excel 导出默认设计：

| Sheet | 内容 |
| --- | --- |
| Summary | 基线版本、任务信息、总差异数、风险分布、复核进度 |
| Record Diff | 每条记录的匹配状态、业务差异、截图时间状态、风险等级、复核状态 |
| Basic Field Diff | 基础字段逐字段差异 |
| Employment Diff | 企业明细逐项差异 |
| Review Actions | 人工处理记录 |
| Quality Metrics | 自动一致率、仅时间变化数、弱匹配数、占位企业数等 |

格式要求：

1. 表头冻结。
2. 自动筛选。
3. 风险等级颜色标记。
4. 差异字段高亮。
5. 当前值和基线值左右相邻。
6. 高风险记录红色标记。
7. 仅截图时间变化使用灰色或浅蓝色标记。
8. 已处理和未处理状态用颜色区分。
9. 每个 Sheet 顶部保留导出时间、基线版本、当前任务等摘要信息。

## 24. 当前实施切片

第一阶段先推进到可测试点：完成基线存储、保存 API、基线详情展示和任务结果对基线的即时比对。

范围：

1. 新增 SQLite 基线库。
2. 支持从已完成任务保存 `snapshot` 基线。
3. 保存人员级基础信息、证据摘要、任职明细行。
4. 支持列出基线版本。
5. 支持查看单个基线版本详情。
6. 结果列表页支持在人工复核后保存 snapshot 基线。
7. 保存基线时携带浏览器端人工复核状态，用于落库记录 `passed / failed / unreviewed`。
8. 基线管理页支持查看基线列表和基线详情。
9. 基线管理页将“基线详情”和“基线比对”分区展示：详情区只看该基线自身数据，比对区只看当前任务与基线的差异。
10. 比对结果覆盖基础信息字段和任职明细字段。

暂不包含：

1. 比对结果持久化。
2. Excel 导出。
3. candidate/golden 生成闭环。
4. 基线版本之间直接比对。

当前可测试接口：

```text
POST /api/tasks/{task_id}/baseline
GET  /api/baselines
GET  /api/baselines/{baseline_id}
POST /api/compare
```

当前可测试页面：

```text
/                       处理台，进入任务结果和基线管理
/runs/{task}/dashboard  结果列表页，人工复核后保存基线
/baselines              基线管理页，查看基线列表、详情，并执行基础字段比对
```

### 24.1 当前字段级比对规则

#### 基础信息逐字段比对

| 字段 | 当前处理 | 差异类型 | 风险 |
| --- | --- | --- | --- |
| 姓名 | 归一化为空白后直接比较 | field_changed / field_missing_current / field_filled | medium |
| 身份证号 | 脱敏号直接比较 | field_changed / field_missing_current / field_filled | high |
| 相关企业 | 提取数字后比较 | field_changed / field_missing_current / field_filled | medium |
| 任职 | 提取数字后比较 | field_changed / field_missing_current / field_filled | medium |
| 参股 | 提取数字后比较 | field_changed / field_missing_current / field_filled | medium |
| 截图日期 | 与截图时间合并为采集时间弱差异 | same / newer / older / missing_current / filled / invalid / changed | older 或 invalid 为 medium |
| 截图时间 | 与截图日期合并为采集时间弱差异 | same / newer / older / missing_current / filled / invalid / changed | older 或 invalid 为 medium |

说明：

1. 基础业务字段任一变化，`business_diff_status = changed`。
2. 仅截图时间变化，不计入基础业务字段变化。
3. 当前截图时间早于基线，标记为疑似旧截图并进入复核。
4. 身份证号变化始终按高风险处理。

#### 任职明细逐字段比对

| 字段 | 当前处理 | 差异类型 | 风险 |
| --- | --- | --- | --- |
| 企业名称 | 企业名称归一化 key 匹配后比较展示值 | company_name_changed / company_added / company_missing | high |
| 经营状态 | 多状态按分隔符归一化后比较 | company_status_changed / company_added / company_missing | high |
| 承担职务 | 直接比较 | company_role_changed / company_added / company_missing | medium |
| 持股比例 | 统一 `% / ％` 后比较 | company_share_changed / company_added / company_missing | high |

任职明细行匹配规则：

1. 优先按 `企业名称归一化 key` 精确匹配。
2. `未识别企业`、空企业 key 视为占位企业，不与真实企业强匹配。
3. 占位企业只允许同一人员下相同行序占位匹配，匹配后仍保留弱证据属性。
4. 基线有企业行而本次没有，生成 `company_missing`，四个字段分别给出基线值到空值的差异。
5. 本次有企业行而基线没有，生成 `company_added`，四个字段分别给出空值到本次值的差异。
6. 任职明细任一字段变化，记录级 `business_diff_status = changed`，并计入 `employment_changed_records`。

### 24.2 当前比对能力

1. 支持当前任务与指定 baseline 做即时比对。
2. 记录匹配优先级：`record_key`、`文件编号+姓名`、`身份证号+姓名`、`文件编号`。
3. 业务字段覆盖：姓名、身份证号、相关企业、任职、参股。
4. 截图日期时间作为采集时间弱差异，不默认进入主业务差异。
5. 任职明细覆盖：企业名称、经营状态、承担职务、持股比例。
6. 输出记录级 summary、基础字段 diff、任职明细 diff、严重等级和是否需要复核。

当前比对 API 示例：

```bash
curl -X POST http://127.0.0.1:8090/api/compare \
  -H 'Content-Type: application/json' \
  -d '{"baseline_id":"<baseline_id>","task_id":"<task_id>"}'
```

当前比对页面能力：

1. `/baselines` 支持选择基线版本。
2. `/baselines` 支持选择已完成任务。
3. 支持基线和被比较任务互换：用当前被比较任务查找其已保存基线，再用当前基线的来源任务作为新的被比较任务。
4. 点击“开始比对”后调用 `POST /api/compare`。
5. 页面展示总记录、业务一致、业务变化、任职变化、新增/缺失、仅时间变化、需复核等聚合指标。
6. 页面展示变化构成条和风险分布。
7. 页面支持按全部、需复核、业务变化、任职变化、新增、缺失、疑似旧截图、仅时间变化快速切片。
8. 页面支持搜索、业务状态、采集时间状态、风险等级、只看需复核等精细筛选。
9. 页面左侧用差异卡片展示重点记录，包含编号、姓名、身份证号、截图时间、基础信息差异和任职明细差异。
10. 选中记录后，右侧展示基线批次图片、本次批次图片、基础信息共识结果和任职明细共识结果。
11. 基础字段差异采用“基线值 -> 本次值”的左右对照展示，便于人工稽核。
12. 任职明细差异先按企业维度对齐，再展开逐字段左右对照展示。
13. 基线详情区不再占用主页面空间，基线版本列表只负责选择和管理。
14. 当基线与其来源任务进行比对时，必须按基线快照自比，结果应为零业务差异、零图片差异、零待复核。

限制：

1. 当前比对结果不持久化。
2. 当前不包含 Excel 导出。
3. 当前不包含 candidate/golden 生成闭环。
4. 当前任职明细企业匹配只使用确定性归一化 key 和占位行序，不做模糊企业名合并。
5. 图片只作为证据预览，不进入差异判断。
