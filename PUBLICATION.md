# Publication Notes

This repository contains OCR application code and local runtime artifacts.

## Safe source areas

- `app.py`
- `templates/`
- `scenes/`
- `ops/consensus_task_app/`
- `ops/*.py`

## Dashboard template

The reusable dashboard template lives in:

- `ops/consensus_task_app/dashboard_template/`

Runtime dashboards are generated under `ops/reports/` and must not be committed.

## Do not publish

The following paths may contain input images, OCR text, personal names, masked ID numbers,
company names, internal hostnames, local filesystem paths, and benchmark results:

- `data/`
- `uploads/`
- `static/uploads/`
- `ops/reports/`
- `ocr_results.json`
- `ocr_results_with_bbox.json`
- `uploaded_parsed_results.json`
- `portal_chat.json`
- `ops/199_proxy_gateway.sh`
- `ops/199_remote_bootstrap.sh`

Private SSH/proxy scripts should stay local and should not have public templates in this repository.

## Release allowlist

Publish only source code, templates, and documentation needed to run the application.

Recommended publish scope:

- `app.py`
- `templates/`
- `scenes/`
- `ops/*.py`
- `ops/consensus_task_app/`
- `PUBLICATION.md`
- `.gitignore`
- dependency/config files such as `requirements*.txt`, `pyproject.toml`, `package.json`, if present

Dashboard source template:

- `ops/consensus_task_app/dashboard_template/index.html`
- `ops/consensus_task_app/dashboard_template/detail.html`
- `ops/consensus_task_app/dashboard_template/assets/`

## Release denylist

Never publish local input, output, benchmark, baseline, dashboard runtime data, or private environment files.

Must stay excluded:

- `data/`
- `uploads/`
- `static/uploads/`
- `ops/reports/`
- `ops/reports/baseline/`
- `ops/reports/ocr_consensus_task_runs/`
- `ops/reports/ocr_list_dashboard/`
- `ocr_results.json`
- `ocr_results_with_bbox.json`
- `uploaded_parsed_results.json`
- `portal_chat.json`
- `ops/199_proxy_gateway.sh`
- `ops/199_remote_bootstrap.sh`
- `ops/*.md` process reports containing local/server paths

## Sensitive content checks

Before publishing, the release candidate should have no matches for:

- Client or bank markers from private projects
- Internal hosts, internal users, jump hosts, SSH ports, or local usernames
- Local absolute paths from developer machines or deployment servers
- Test data: personal names, masked ID numbers, company names, OCR original text, uploaded images

Suggested scan:

```bash
rg -n -i --hidden \
  --glob '!.git/**' \
  --glob '!ops/reports/**' \
  --glob '!ops/*.md' \
  --glob '!data/**' \
  --glob '!uploads/**' \
  --glob '!static/uploads/**' \
  --glob '!ocr_results*.json' \
  --glob '!uploaded_parsed_results.json' \
  --glob '!portal_chat.json' \
  --glob '!ops/199_proxy_gateway.sh' \
  --glob '!ops/199_remote_bootstrap.sh' \
  '<client-marker-1>|<client-marker-2>|<internal-ip-regex>|<local-path-regex>'
```

## Git index cleanup

`.gitignore` only prevents new files from being added. If sensitive files were already tracked,
remove them from the Git index before publishing:

```bash
git rm --cached -r data uploads static/uploads ops/reports
git rm --cached ocr_results.json ocr_results_with_bbox.json uploaded_parsed_results.json portal_chat.json
git rm --cached ops/199_proxy_gateway.sh ops/199_remote_bootstrap.sh
git rm --cached ops/*.md
```

Do not delete local files unless intentionally cleaning the workspace; use `--cached` to keep them locally.
