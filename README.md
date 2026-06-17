# OCR Consensus

[English](README.md) | [简体中文](README.zh-CN.md)

OCR Consensus is a multi-engine OCR review workbench for converting screenshots and document images into auditable structured records.

It is designed for workflows where raw OCR text is not enough. The system keeps model-level evidence, applies deterministic rule enhancement, performs field-level consensus, and provides a review UI for human verification.

## Why this project

- **From OCR text to structured records**: extracts names, masked identifiers, timestamps, count fields, company rows, roles, statuses, and share ratios where available.
- **Multi-engine consensus**: runs several OCR lanes independently, then compares and merges field-level results.
- **Explainable decisions**: every accepted field can be traced back to model output, confidence, rule processing, and consensus status.
- **Human-in-the-loop quality control**: supports manual pass/fail review, record navigation, filtering, and issue discovery.
- **Batch-friendly operation**: supports single images, multi-image upload, and ZIP processing through a task queue.

## Screenshots

The screenshots below use hand-redacted sample data and are intended to demonstrate workflow and UI layout only.

### Processing workbench

Upload images or ZIP packages, create tasks, track progress, and open generated review results.

![Processing workbench](docs/assets/workbench.jpg)

### Structured result list

Use aggregate counters, search, sorting, field filters, and review-state filters to quickly locate suspicious records.

![Structured result list](docs/assets/result-list.jpg)

### Detail review page

Review the consensus result, source image, structured fields, company rows, and manual review controls in one place.

![Detail review page](docs/assets/detail-review.jpg)

### Model evidence comparison

Compare four OCR lanes side by side, including raw OCR evidence, confidence, rule-processing marks, and extracted employment rows.

![Model evidence comparison](docs/assets/model-evidence.png)

## Core features

| Feature | Description |
| --- | --- |
| Multi-engine OCR harness | Orchestrates multiple OCR engines and preserves independent outputs. |
| Field extraction and normalization | Extracts and normalizes structured fields from noisy OCR text. |
| Rule-enhanced parsing | Applies deterministic rules for masked identifiers, timestamps, counts, and layout artifacts. |
| Employment detail extraction | Extracts company names, business status, roles, and share ratios when available. |
| Field-level consensus | Merges multiple model outputs while preserving missing, conflict, placeholder, and weak-evidence states. |
| Evidence retention | Keeps per-engine fields, confidence, raw OCR text, and execution status for review. |
| Review dashboard | Provides filtering, sorting, aggregate counters, image preview, detail navigation, and manual review state. |
| Batch task queue | Supports images and ZIP packages through a controlled task queue. |

## Workflow

1. **Upload**: add one or more images, or upload a ZIP package.
2. **OCR lanes**: run configured OCR engines independently.
3. **Harness enhancement**: apply deterministic rules for text cleanup, field extraction, normalization, and candidate filtering.
4. **Consensus**: merge model-level fields into a single structured result while preserving conflict and uncertainty signals.
5. **Review**: inspect list and detail pages, compare evidence, and mark pass/fail review states.
6. **Export or iterate**: use the results for downstream processing, quality analysis, or rule iteration.

## What gets tracked

- Basic fields: file id, file name, person name, masked identifier, screenshot time, and summary counts.
- Employment detail rows: company name, business status, role/title, and share ratio when present.
- Consensus status: agreed, conflict, missing, placeholder, evidence-filled, or rule-processed.
- Evidence: per-engine extracted fields, confidence, raw OCR text, and method execution status.
- Review state: pending, passed, failed, and review reasons.

## Repository layout

```text
.
├── app.py                         # Generic Flask entry point
├── ops/
│   ├── consensus_task_app/         # Task queue and dashboard
│   ├── extract_basics.py           # Basic-field extraction
│   ├── extract_employment_info.py  # Employment detail extraction
│   └── *.py                        # Evaluation, export, and utility scripts
├── scenes/                         # Scenario helpers
├── templates/                      # Legacy/simple templates
├── docs/assets/                    # README screenshots
└── PUBLICATION.md                  # Release and privacy checklist
```

## Quick start

```bash
python3 ops/consensus_task_app/app.py
```

Then open the printed local URL in a browser and upload test images or a ZIP file.

For real OCR execution, configure engine commands and runtime paths through environment variables rather than hardcoding local paths.


## Status

OCR Consensus is currently a controlled extraction and review workbench. It is suitable for batch evaluation, rule iteration, quality analysis, and human-in-the-loop review.

Before production use, verify OCR engine availability, storage policy, deployment isolation, access control, and data retention requirements.
