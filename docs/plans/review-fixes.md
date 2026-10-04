# Review Fixes Implementation Plan

## Objective And Scope
Address the nine confirmed findings from the review of 35a5632..d44df87. Keep existing course-selection rules, score choices and public platform entrypoints. The original implementation was local-only; the subsequent publication request authorizes a sanitized commit and push to main. Do not move tags or replace remote Release assets.

## Current Architecture And Impact
The launcher runs platform scripts in subprocesses during development and via exec in a frozen executable. Platform imports are flat and configuration paths are supplied through FUCKCOURSE_CONFIG, FUCKCOURSE_COOKIES and FUCKCOURSE_LOG_DIR. Chaoxing uses PriorityQueue workers plus a retry worker. ZHS Hike reports integer seconds but currently calculates fractional completion targets. PPT caches currently key downloads by basename. CI runs unittest across Python 3.10-3.13, then PyInstaller and a menu-only smoke test.

## Contracts And Data Flow
- Configuration readers must distinguish a missing file from read errors. On invalid JSON, preserve original bytes in a unique backup before resetting. A failed backup or non-object JSON must not permit destructive writeback. Successful valid writes preserve unrelated sections.
- Queue work accounting and retry limits remain unchanged. Shutdown must wake and join both normal and retry workers on supported Python versions, including versions without Queue.shutdown.
- Keep the configured Hike completion percentage; round its target upward to integer reporting precision. Bound consecutive server responses with no progress and fail explicitly rather than report completion.
- PPT download names include a stable URL digest and extension; cache paths must remain within the configured cache directory. Upload-cache identity must be content based rather than filename plus size.
- WE Learn parses response JSON and never substitutes score 100 for a custom score on retry.
- PDF generation is all-or-nothing for downloads and decoding; close Pillow images on success and failure.
- PyInstaller data is an explicit allowlist of platform source files and required resources. No logs, user JSON, caches or backups are collected.
- Offline smoke mode executes each packaged platform's actual top-level import declarations and required-resource checks only. ZHS has top-level interactive code, so the probe must not execute the entire entry script. It blocks Python network operations, uses temporary data paths, propagates failures and has a timeout. Normal launcher behavior remains unchanged.

## Milestones
1. Harden configuration functions in chaoxing/api/base.py, welearn/welearn_decompiled.py and yuketang/yuketang_login.py; add fault-injection tests.
2. Repair JobProcessor shutdown in chaoxing/main.py; test actual worker/retry execution and thread reclamation, including a queue without shutdown.
3. Repair Hike and PPT identities in zhs/fucker.py; test fractional targets, stalled server progress, distinct URLs sharing basenames and content identities.
4. Repair score handling and PDF conversion; test valid whitespace JSON, failed requests, missing and corrupt pages, and complete PDFs.
5. Replace directory data entries in fuckCourse.spec with explicit sources/resources. Add reusable archive auditing and frozen offline smoke scripts; wire them into .github/workflows/ci.yml with checked return codes and timeouts.
6. Replace formula-only tests with real implementation calls or AST-extracted actual definitions with mocked boundary APIs. Keep tests strictly offline and isolate configuration/log paths.
7. Update README.md and log.md to describe implemented behavior and accurate verification limits.

## Edge Cases And Risks
Backup names must not overwrite older recoverable data. Read and write faults must propagate without silently resetting configuration. Queue sentinels must be priority-compatible, and all threads must terminate after both success and exhausted retries. A server that continually reports old Hike time must terminate as a failure. Cache keys must avoid same-basename/same-size content mixups. Build validation must reject secret-looking entries even if Git ignores them. Smoke output only demonstrates importability, not actual login, study or submission correctness.

## Verification
- python -m unittest discover -s tests -p "test_*.py"
- python -m compileall -q main.py chaoxing zhs welearn yuketang tests
- git diff --check
- python -m PyInstaller --clean -y fuckCourse.spec
- Run the new binary archive audit and offline smoke runner against dist/fuckCourse.exe.
- Use English cp1252 output where applicable to retain coverage for the previous Unicode fix.
- Inspect final git status/diff; do not commit or publish without a separate user request.

## Completion Evidence
- All seven milestones completed and verified locally before publication. The subsequent request authorizes a sanitized source update to main, with no tag change or remote Release mutation.
- Python 3.13.12: full unittest discovery, 64 tests passed with no skips.
- Python 3.11.15: configuration/queue and ZHS regressions, 38 tests run; 37 passed and the Python-3.13-only queue test was appropriately skipped. Python 3.10/3.12 runtime matrix has not been rerun remotely.
- PyInstaller 6.21.0 clean build completed successfully.
- Built EXE verification returned ARCHIVE_ALLOWLIST_OK, MENU_EXIT_OK and SMOKE_IMPORT_OK for chaoxing, welearn, zhs and yuketang.
- Binary SHA-256: e64eb0dcdd10f45088a70c7f8a0f304672067cdd2a6c8442f99aa22aa6df006f.
- Real login/course actions/model calls were not performed. PPT filesystem checks are not an atomic defense against a hostile concurrent path replacement.
