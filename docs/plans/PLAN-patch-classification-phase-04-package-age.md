# Package age — capture the changelog date and show it in review

The patch's DEP-3 `Last-Update` answers "when was this *patch* last touched", but
not "is the *package* itself ancient and unloved". The package's last-upload date
(the top `debian/changelog` entry) is the missing half — and it is already in the
`.debian.tar.*` the corpus fetch downloads, so capturing it costs no extra network.

**Status: planned.** Populated only on the NEXT corpus rebuild; shown "if available".

## Design

1. **Capture (`apt_patches`).** The `.debian.tar.*` that `_extract_patches` opens
   also holds `debian/changelog`. `_extract_changelog_date` reads the top entry's
   ` -- maintainer  <date>` line and normalises it to ISO `YYYY-MM-DD`.
   `fetch_source_details` returns `(source_format, texts, changelog_date)` from the
   SAME download; `fetch_patch_texts` keeps its 2-tuple signature (the client is
   unchanged) by delegating to a shared `_fetch_source`.
2. **Record (`corpus`).** `_default_fetch` uses `fetch_source_details`; the
   per-package row in `packages.jsonl` gains `changelog_date` (None for native /
   non-quilt / pre-feature corpora).
3. **Index (`measure`).** Build a `package` table `(source_package, version,
   changelog_date)` in `fingerprints.sqlite` from the latest package rows — so the
   date is one indexed lookup at review time, not a 2.7 MB manifest re-read.
4. **Show (`review` + UI).** `package_dates_by_name(index_path)` →
   `{source_package: changelog_date}` (latest version per package). The review
   context surfaces it next to each carrying package ("rman — last upload
   2020-05-20"); absent dates are simply omitted. Web claim/meta block + the CLI
   reviewer both show it.

## Honest framing
Like the patch date, this is informational, not a verdict input. It says when the
PACKAGE was last uploaded, which still does not date the code a patch carries (an
ancient vendored blob in a recently-uploaded package) — but "patch Last-Update +
package last-upload + the diff itself" together let a reviewer judge "ancient and
unloved vs written this century" at a glance.

## Steps
| Step | Brief |
|------|-------|
| P1 | `apt_patches`: `_extract_changelog_date` + `fetch_source_details` (shared `_fetch_source`); `corpus`: record `changelog_date` in `packages.jsonl`. Offline tests with a synthetic `.debian.tar`. |
| P2 | `measure`: write the `package` table from the latest package rows. Tests. |
| P3 | `review` + `review_web` + CLI: look up + display package dates. Tests. |
| P4 | Docs (README/AGENTS/ARCHITECTURE + runbook): note the rebuild requirement. |

## Out of scope
- Backfilling existing corpora (only the next rebuild populates it).
- Parsing the full changelog history (only the top entry's date is read).
