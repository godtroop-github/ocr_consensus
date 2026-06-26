# OCR Consensus

[English](README.md) | [简体中文](README.zh-CN.md)

OCR Consensus is a multi-engine OCR workbench for turning screenshots and document images into auditable structured records.

It is built for workflows where raw OCR text is not enough. The system preserves per-engine evidence, applies deterministic parsing rules, performs field-level consensus, supports human review, and manages reusable baselines for future data comparison.

> The screenshots in this README are rendered with fictitious demonstration data. They do not contain real personal data, real company names, real identifiers, deployment hosts, or customer information.

## Highlights

| Capability | What it provides |
| --- | --- |
| Multi-engine OCR harness | Runs independent OCR lanes and stores each engine's raw output, confidence, timing, and execution status. |
| Rule-enhanced extraction | Normalizes noisy OCR text into names, masked identifiers, screenshot timestamps, count fields, and employment rows. |
| Field-level consensus | Produces one reviewable consensus record while preserving missing, conflict, placeholder, rule-processed, and weak-evidence states. |
| Employment detail parsing | Extracts company name, business status, role/title, and share ratio when present. |
| Baseline management | Saves reviewed results as durable baselines, compares later batches against a baseline, and supports field-level adjudication. |
| Evidence comparison | Shows side-by-side OCR lanes, raw OCR text, confidence, rule marks, and image evidence. |
| Collaborative collection | Creates controlled submission batches, tracks participant status, and feeds accepted images into the same OCR workflow. |
| Safe auxiliary models | Supports auxiliary/shadow OCR engines such as PP-OCRv6 without immediately replacing the primary consensus lanes. |

## Screenshots

The following screens use the real product layout with synthetic demonstration records.

### Processing workbench

Create OCR tasks from images or ZIP packages, keep the queue controlled, and monitor each OCR lane independently.

![Processing workbench](docs/assets/workbench.jpg)

### Structured result list

Use aggregate counters, search, column sorting, field filters, and review-state filters to locate missing fields, conflicts, method errors, or employment records.

![Structured result list](docs/assets/result-list.jpg)

### Detail review page

Review the consensus result, source image, basic fields, employment details, image evidence, and review actions in one place.

![Detail review page](docs/assets/detail-review.jpg)

### Model evidence comparison

Compare OCR lanes side by side, including extracted fields, confidence, raw OCR text, rule processing, and auxiliary evidence.

![Model evidence comparison](docs/assets/model-evidence.png)

## Workflow

1. **Collect images**: upload single images, multiple images, or ZIP packages through the processing workbench or collaborative submission flow.
2. **Run OCR lanes**: execute configured OCR engines independently, preserving raw outputs and method-level metadata.
3. **Apply harness rules**: clean layout artifacts, normalize masked identifiers, extract timestamps, count fields, and candidate employment rows.
4. **Build consensus**: merge model-level candidates into one structured result with explainable decision metadata.
5. **Review exceptions**: use the dashboard to filter missing fields, conflicts, method errors, image-quality issues, and employment changes.
6. **Save or compare baselines**: persist reviewed results as baselines, compare new batches, and adjudicate field-level differences.

## Data model

| Area | Example fields |
| --- | --- |
| Basic information | file id, file name, person name, employee id, masked identifier, screenshot date/time, related count, role count, shareholding count |
| Employment details | row number, company name, business status, role/title, share ratio |
| Consensus metadata | agreed, conflict, missing, placeholder, evidence-filled, rule-processed, auxiliary-supported |
| Method evidence | OCR engine name, runtime status, average confidence, extracted fields, raw OCR text, processing time |
| Review state | pending, passed, failed, abandoned, review reason, adjudication choice |
| Baseline evidence | baseline version, source task, selected images, image quality, image fingerprint hints |

## Runtime modes

| Mode | Purpose |
| --- | --- |
| Primary 4-lane OCR | Main consensus source. Engines are configured by environment variables. |
| Auxiliary/shadow OCR | Runs additional engines such as `ppocrv6_small` for evidence collection or limited field assistance. |
| Baseline comparison | Compares reviewed consensus records against a durable baseline and lists field-level differences. |
| Collaborative submission | Lets multiple participants submit images into a controlled batch before OCR processing. |

## Quick start

```bash
python3 ops/consensus_task_app_server.py
```

Then open:

```text
http://127.0.0.1:8090/
```

The OCR engines themselves are environment-dependent. Configure runtime paths through environment variables instead of hardcoding local paths.

Common variables:

```bash
export OCR_CONSENSUS_PORT=8090
export OCR_ENGINE_PARALLEL=aggressive
export OCR_SHADOW_ENGINES=ppocrv6_small
export OCR_PARALLEL_SHADOW_ENGINES=0
export OCR_ROI_ID_FALLBACK=0
```

## Repository layout

```text
.
├── app.py                         # Legacy/simple OCR entry point
├── ops/
│   ├── consensus_task_app/         # Task queue, review UI, baseline store, collaborative submission
│   ├── consensus_task_app_server.py# FastAPI/Uvicorn launcher for the consensus workbench
│   ├── extract_employment_info.py  # Employment detail extraction
│   └── *.py                        # Evaluation, export, and utility scripts
├── scenes/                         # Scenario helpers for the legacy/simple flow
├── templates/                      # Legacy/simple templates
├── docs/assets/                    # README screenshots with synthetic data
└── PUBLICATION.md                  # Publication and privacy checklist
```

## Privacy and publication policy

This repository is intended to contain source code, templates, and documentation only.

Do not commit runtime data, uploaded images, OCR result JSON files, generated dashboards, baseline databases, proxy scripts, local environment files, internal hostnames, or customer-specific process reports.

See [PUBLICATION.md](PUBLICATION.md) before publishing a release.

## Project status

OCR Consensus is a controlled extraction, consensus, baseline, and review workbench. It is suitable for OCR quality evaluation, rule iteration, batch review, baseline comparison, and human-in-the-loop verification.

Before production use, validate engine availability, access control, storage isolation, retention policy, audit logging, and deployment-specific security requirements.
