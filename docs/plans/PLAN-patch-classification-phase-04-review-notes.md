# Signed reviewer notes — ad hoc human annotations on a fingerprint

The review surface is forced-choice: assign a category (a verdict) or defer and
record nothing. There is nowhere to capture the thing a reviewer actually hits --
"this introduces `sprintf()` into a privilege boundary; I can't prove it's
exploitable, but someone should look harder." That is not a category; it is a
human observation worth keeping, and losing it because it does not fit a verdict is
a real cost, especially for security.

This adds **signed reviewer notes**: append-only, attributed, free-text annotations
on a fingerprint, independent of any verdict.

**Status: planned.**

## Design decisions

### A third kind of ledger entry
A note is neither a decision (not a verdict) nor a rule observation (it is
human-authored), so it gets its own `note` table rather than being shoehorned into
either. Notes ACCUMULATE (a fingerprint can have several) and are never edited or
superseded -- a correction is just another note, consistent with the ledger's
append-only ethos.

```
note(id, fingerprint, body, signed_by, signature, created_at)
```

`note` is OPTIONAL: it is NOT added to `REQUIRED_TABLES` (so existing ledgers keep
opening) and is created idempotently (`ensure_note_table`, `CREATE TABLE IF NOT
EXISTS`) by `create_ledger` and by the web app at startup -- so a ledger built
before this feature gains notes with no rebuild.

### Signed, reusing the verdict signer
A note is signed with the SAME injected Sigstore signer the verdicts use:
`canonical_note(fingerprint, body, created_at) -> bytes` (deterministic JSON, like
`canonical_record`) is signed `signer(bytes) -> (signature, signed_by)`, and the
bundle JSON + OIDC identity are stored on the row. This gives the note identity and
non-repudiation, keeps "human-authored ledger entries are signed" uniform, and adds
no friction -- the once-per-session browser prompt is already paid by the first
verdict. With no signer (browse-only) notes are read-only, exactly like verdicts.

### Shown WITH the signature
Every UI that displays a note shows its provenance: the body, `signed_by`
(identity) and `created_at` prominently, and the full `signature` (the Sigstore
bundle, several KB) in a collapsed `<details>` so it is available to verify without
swamping the page. A worklist **note indicator** (count badge) makes annotated
patches findable later -- the point of a note left weeks ago.

### Curation-side only
Notes are reviewer context, like the rest of the ledger; they are NOT in the
published bundle. Clients consume verdicts, not margin notes.

## Steps

| Step | Brief |
|------|-------|
| N1 | **Storage + signing.** `ledger`: `ensure_note_table`, `append_note`, `notes_for(fp)`, `note_counts_by_fingerprint`. `review`: `canonical_note` + `record_note(conn, fp, body, *, signer, now)`. Append-only; offline tests with a fake signer. |
| N2 | **Web UI.** A "Notes" section on `/review/<fp>` (thread + add box, signature in `<details>`), `POST /note/<fp>` (signed, requires a signer), and a worklist note-count indicator. Audit reaches notes via the review page. Offline `test_client` tests. |
| N3 | **Docs.** README/AGENTS/ARCHITECTURE + the runbook. |

## Testing requirements
- `append_note`/`notes_for` round-trip; `ensure_note_table` is idempotent and works
  on a ledger that predates the table.
- `record_note` signs the canonical bytes (fake signer) and stores signature +
  identity; append-only (no edit/delete API; the invariant test stays green).
- The review page renders a note with its signer + signature; `POST /note` records
  one and a signer failure is a page, not a 500; the worklist shows the count.
- `pre-commit run --all-files` green; house style.

## Out of scope (follow-ups)
- Notes in the CLI `review` tool (web first; the operator is there).
- Structured notes (a concern type, CVE refs) or a notes-driven follow-up queue --
  free text first.
- Editing/deleting a note (append-only by design; add another).

## Back brief
A note is append-only and human-signed (reuse the verdict signer), in its own
OPTIONAL `note` table that existing ledgers gain without a rebuild; every UI shows
the signature/identity; notes never enter the published bundle.
