# Package description — capture the control synopsis and show it in review

The review UI names the source package carrying a patch, but a reviewer staring
at `rman` or `gatos` gets no hint what the package *is*. Every Debian source
package answers that itself: `debian/control` carries a one-line `Description:`
synopsis per binary package — and `debian/control` is already in the
`.debian.tar.*` the corpus fetch downloads, so capturing it costs no extra
network. This mirrors the package-age feature
([PLAN-patch-classification-phase-04-package-age.md](PLAN-patch-classification-phase-04-package-age.md))
end to end: capture at corpus build, index in `measure`, show in review.

**Status: implemented (P1–P4).** Populated only on the NEXT corpus rebuild +
re-measure; shown "if available" (absent on the current ledger until then, and
absent for native/non-quilt sources, which ship no separate `.debian.tar.*`).

## Design

1. **Capture (`apt_patches`).** The `.debian.tar.*` that `_extract_patches` and
   `_extract_changelog_date` open also holds `debian/control`. A new
   `_extract_description` reads it and a pure `_control_synopsis` picks the
   short description: descriptions are per BINARY package, so prefer the stanza
   whose `Package:` equals the source name, else fall back to the first binary
   stanza carrying a `Description:`. Only the first line (the synopsis) is
   kept. `fetch_source_details` grows the field via the shared `_fetch_source`
   5-tuple; `fetch_patch_texts` keeps its 2-tuple signature (the client
   divergence path is unchanged).
2. **Record (`corpus`).** The per-package row in `packages.jsonl` gains
   `description` (None for native / non-quilt / unresolved / pre-feature
   corpora), exactly beside `changelog_date` and `binaries`.
3. **Index (`measure`).** The index `package` table gains a `description`
   column, so the synopsis is one indexed lookup at review time.
4. **Show (`review` + UIs).** `_package_description(index_path, source_package)`
   reads it with the same graceful degradation as `_package_date` (an index
   built before the column existed yields None, never an error). It rides on
   `ReviewContext.package_description` and renders as its own line in
   `_format_package_lines` — which both the CLI reviewer and the web UI already
   share, so the web review page gets it with no template change.

## Honest framing
The synopsis is the *maintainer's* one-line pitch, read from the same
author-controlled packaging the patches come from. It orients the reviewer
("what even is this package?"); it is informational context, never a verdict
input.

## Steps
| Step | Brief |
|------|-------|
| P1 | `apt_patches`: `_control_synopsis` + `_extract_description`; `_fetch_source`/`fetch_source_details` grow the field. Offline tests with a synthetic `.debian.tar`. |
| P2 | `corpus`: record `description` in `packages.jsonl`; `measure`: add the `package.description` column. Tests. |
| P3 | `review`: `_package_description` + `ReviewContext.package_description` + `_format_package_lines` line (CLI and web both inherit). Tests. |
| P4 | Docs (AGENTS/ARCHITECTURE + plan index): note the rebuild requirement. |

## Out of scope
- Backfilling existing corpora (only the next rebuild + re-measure populates
  it; the operator has said they will force one).
- Long/extended descriptions (only the synopsis line is captured).
- Per-binary descriptions (one synopsis represents the whole source package).
