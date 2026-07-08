# confirmed-ctl — Code Review & Fixes Log

This document records the review of the initial project-environment build
(PR #2) and the follow-up fix that came out of that review (PR #3). It is
intended as a durable engineering record: what was reviewed, what was found,
what was changed, how it was verified, and what remains as follow-up work.

- **Reviewed PR:** [#2 — Build the confirmed-ctl project environment (scaffold + CLI + tests)](https://github.com/k4rlski/confirmed-ctl/pull/2)
- **Fix PR:** [#3 — fix(crm): select real case number column for Dropbox filenames](https://github.com/k4rlski/confirmed-ctl/pull/3)
- **Review date:** 2026-07-08

---

## 1. Context

The repository originally contained only documentation (`README.md`, `docs/`,
`rag/`) describing `confirmed-ctl` — the final stage of the newspaper-ad payment
reconciliation pipeline (Gmail receipt download → Dropbox archiving → CRM final
`Done` write). PR #2 turned those design docs into a runnable, installable
Python project: packaging, config loading, a Click CLI, per-case orchestration,
CRM/Gmail/Plaid/Dropbox modules, and a unit-test suite.

The task here was to **review that PR**, and then to **implement the fix** for
the one correctness bug the review surfaced.

---

## 2. Review of PR #2

### 2.1 Scope of the PR

| Area | Delivered |
|------|-----------|
| Packaging & tooling | `pyproject.toml` (setuptools, `confirmed-ctl` entry point, `[dev]` extras, ruff/pytest config), `requirements*.txt`, `Makefile`, `.gitignore` |
| Package `src/confirmed_ctl/` | `main.py` (CLI), `config.py`, `models.py`, `crm_client.py`, `dropbox_store.py`, `plaid_verifier.py`, `gmail_receipt.py`, `pipeline.py` |
| Config & tests | `confirmed-ctl.yml.example`, `tests/` (24 unit tests) |

The live `gmail-ctl`, `plaid-ctl`, and `rclone` bridges are intentionally
stubbed and marked `TODO(phase1)`, which is appropriate for a v0.1.0 scaffold.

### 2.2 Verification performed

The PR's claims were reproduced locally (Python 3.12, dependencies installed
manually because the environment lacks `python3-venv`):

| Check | Result |
|-------|--------|
| `pytest` | **24 passed** |
| `ruff check src tests` | clean |
| `confirmed-ctl --help` / `--version` | works |
| `process-confirmed` / `status` with no CRM reachable | fails gracefully with a credentials hint (exit handled by `_guard`) |

### 2.3 Strengths

- **Write safety is enforced, not just documented.** `CrmClient.write_case_fields`
  raises on any field outside the four-field allow-list
  (`statacctgcreditnews`, `urlgmailadconfirm`, `trxstring`, `datepaidnews`),
  and this is unit-tested.
- **Read-only by default**; `--write` is required for CRM writes and Dropbox
  uploads, wired consistently through CLI → `Pipeline` → clients.
- **Parameterized SQL** for the `WHERE` values; table/join names come from
  config, not user input.
- **Testable design:** pure functions (slug/path/`trxstring`/amount tolerance)
  are separated from I/O, and the partial-completion decision table from
  `docs/DESIGN.md` is faithfully implemented in `pipeline._compute_writes` /
  `_classify` and covered by tests (Done / Partial / Skip / amount-mismatch).
- **Secret hygiene:** `.gitignore` protects `confirmed-ctl.yml`, `.env`, and key
  files; only the `.example` template is committed.

### 2.4 Findings

#### Finding 1 (bug, fixed) — case number was never the human case number

`build_trigger_query` selected `p.id`, `p.name AS company`, etc., but **never
aliased any column to `case_number`**. Consequently `row_to_case` always hit its
fallback:

```python
case_number=str(row.get("case_number") or row.get("id"))  # -> always row["id"]
```

So `receipt_filename` produced
`Case-<internal-guid>_Eduexplora-International_IPR00160880_2026-03-02.pdf`
instead of the documented `Case-10349_...pdf` convention in `docs/DROPBOX.md`.

The existing tests passed only because the fixture set `case_number` by hand —
no test exercised the query → `row_to_case` → filename path end to end. The
`_case_number_from_row` helper that was evidently intended for this was **dead
code**, and it referenced keys (`company_case`, `name`) that were not present in
the query result set.

> **Status: fixed in PR #3** (see Section 3).

#### Finding 2 (minor, follow-up) — env-override coercion ignores list fields

`config._coerce` only coerces `int`/`float`. Setting a list-typed field via an
environment variable (e.g. `CONFIRMED_CTL_CRM_TRIGGER_STATUSES` or
`CONFIRMED_CTL_GMAIL_ACCOUNTS`) would store a raw string where a list is
expected, silently breaking list semantics. Low risk, but worth a guard or a
documented limitation.

#### Finding 3 (minor, follow-up) — misleading error hint on missing driver

A missing `mysql-connector-python` driver surfaces as
`Hint: check CRM credentials…`. It is edge-only (the driver is a core
dependency in production), but distinguishing "driver missing" from "auth
failed" would be clearer.

#### Finding 4 (doc drift, follow-up) — `datenewsstart`

`docs/CRM-SCHEMA.md` lists `datenewsstart` as a Dropbox-filename source, but the
implementation (correctly, per `docs/DROPBOX.md`) uses `dateinvoicednews`. The
schema doc should be reconciled.

---

## 3. Fix — PR #3

**Commit:** `fix(crm): select real case number column for Dropbox filenames`
**Branch:** `cursor/fix-case-number-query-9bc1` (stacked on the PR #2 branch)

### 3.1 Root cause

The trigger query did not select a case-number column, so the case number
defaulted to the CRM internal `id`, defeating the documented Dropbox filename
convention.

### 3.2 Changes

| File | Change |
|------|--------|
| `src/confirmed_ctl/config.py` | Added `crm.case_number_column` (default `"id"`), so behaviour is unchanged/safe until a deployment sets the real column. |
| `src/confirmed_ctl/crm_client.py` | Select that column as `case_number` in the trigger query, guarded by a new `_safe_identifier` check (the value is config-supplied and interpolated into SQL). Rewrote the previously-dead `_case_number_from_row` helper and wired it into `row_to_case` with an id fallback. |
| `confirmed-ctl.yml.example` | Documented the new `case_number_column` option. |
| `docs/CRM-SCHEMA.md` | Added the `case_number` select to the reference trigger query. |
| `tests/test_pipeline.py` | Added 4 regression tests. |

### 3.3 Key code

The configurable, validated column selection:

```python
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_identifier(name: str, kind: str) -> str:
    """Validate a config-supplied SQL identifier (table/column) before interpolation."""
    if not _IDENTIFIER.match(name or ""):
        raise ValueError(f"Invalid {kind} identifier: {name!r}")
    return name
```

```python
case_number_column = _safe_identifier(config.case_number_column, "case_number_column")
sql = f"""
    SELECT
      p.id,
      p.{case_number_column}   AS case_number,
      ...
"""
```

The helper wired into `row_to_case`:

```python
def _case_number_from_row(row: dict[str, Any]) -> str:
    """Derive the human case number, falling back to the CRM id when absent."""
    number = row.get("case_number")
    if number is not None and str(number).strip():
        return str(number).strip()
    return str(row.get("id", ""))
```

### 3.4 Tests added

- `test_trigger_query_selects_case_number_column` — the configured column is
  selected as `case_number`.
- `test_trigger_query_rejects_bad_case_number_column` — an injection-style
  identifier (`"number; DROP TABLE p"`) raises `ValueError`.
- `test_row_to_case_uses_case_number_alias` — `row_to_case` → `receipt_filename`
  yields `Case-10349_Eduexplora-International_IPR00160880_2026-03-02.pdf`.
- `test_row_to_case_falls_back_to_id_without_case_number` — the id fallback when
  no case number is present.

### 3.5 Verification

| Check | Result |
|-------|--------|
| `pytest` | **28 passed** (24 existing + 4 new) |
| `ruff check src tests` | clean |

### 3.6 Deployment note

The default remains `id` because the exact CRM case-number column name is not
confirmed in the docs. Deployments should set `crm.case_number_column` (e.g. to
`number`) to produce the `Case-10349_...` filenames documented in
`docs/DROPBOX.md`.

---

## 4. Remaining follow-ups

These items from the review were intentionally left out of PR #3 to keep it
focused, and are tracked here for future work:

1. **Env-override coercion for list fields** (Finding 2) — guard or document
   that `CONFIRMED_CTL_*` env overrides only support scalar fields.
2. **Driver-vs-credentials error hint** (Finding 3) — distinguish a missing
   `mysql-connector-python` driver from an authentication failure.
3. **`datenewsstart` doc drift** (Finding 4) — reconcile `docs/CRM-SCHEMA.md`
   with the implemented `dateinvoicednews` filename source.
4. **Phase-1 live bridges** — implement the `gmail-ctl`, `plaid-ctl`, and
   `rclone` integrations currently stubbed with `TODO(phase1)`.

---

## 5. References

- Design: `docs/DESIGN.md`
- CRM schema & write policy: `docs/CRM-SCHEMA.md`
- Dropbox path convention: `docs/DROPBOX.md`
- Roadmap: `docs/ROADMAP.md`
- Open questions: `docs/OPEN-QUESTIONS.md`
