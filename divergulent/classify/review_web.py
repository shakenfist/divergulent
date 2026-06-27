"""A local, single-user web UI for human review of the patch residue.

A presentation swap over the existing review machinery in ``review.py``: it
reuses the read path (``build_review_context`` and the fingerprint-keyed render
helpers) and the signed verdict path verbatim, so a web verdict and a CLI verdict
are byte-identical.  Flask + Jinja2 (autoescaping HTML) live behind the optional
``review`` extra, off the default scan/report path.

This module is read-only for now: the worklist (three slices -- next most
important, by category, cherry-pick by fingerprint) and the per-item review page.
The signed verdict POST and the audit/spot-check view arrive in later steps.

Bound to the loopback interface only, with no authentication: it is a
single-user local tool, never a networked service.  All handler logic is driven
through Flask's test client in the tests, with an injected fake ``fetch`` and a
temp ledger -- no real socket, no real network.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from urllib.parse import urlencode

from divergulent.classify import ledger as ledger_mod
from divergulent.classify import review as review_mod
from divergulent.classify import reviewability as reviewability_mod
from divergulent.classify import verdict as verdict_mod

# The handlers reuse review.py's fingerprint-keyed read helpers directly rather
# than duplicating context-building; they are package-internal shared API, used
# here exactly as the CLI uses them.
DEFAULT_PORT = 8765
LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '::1')
# The audit view can span the whole settled archive; cap the rendered rows and
# tell the operator how many were dropped rather than building a vast page.
AUDIT_LIMIT = 500


def require_loopback(host: str) -> str:
    """Return ``host`` if it is a loopback address, else raise ``ValueError``.

    The review UI has no authentication and serves a local curation tool; binding
    it to a routable interface would expose the ledger and the signing entry to
    the network.  The entry point refuses anything but loopback.
    """
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            'refusing to bind a non-loopback host %r; the review UI has no auth '
            'and must stay local (use one of %s)' % (host, ', '.join(LOOPBACK_HOSTS)))
    return host


def diff_lines(text: str) -> list[dict]:
    """Split a rendered diff-in-context into ``{cls, text}`` rows for coloring.

    Classifies each line so the template can colour additions, deletions, hunk
    headers and file markers distinctly from upstream context -- the diff reads
    nicer than the CLI pager without any highlighter dependency.
    """
    rows = []
    for line in text.splitlines():
        if line.startswith('@@'):
            cls = 'hunk'
        elif line.startswith(('+++', '---')):
            cls = 'meta'
        elif line.startswith('+'):
            cls = 'add'
        elif line.startswith('-'):
            cls = 'del'
        else:
            cls = 'ctx'
        rows.append({'cls': cls, 'text': line})
    return rows


def category_chips(counts: dict) -> list[dict]:
    """The full assignable category set as ``{name, count}`` chips, in enum order.

    Renders EVERY category (a stable, complete filter bar) from a name->count map,
    so the bar does not jump as items are reviewed and an empty category -- notably
    ``test``, which the LLM never drafts (it is assigned by the deterministic rule
    or a human) -- is visibly empty rather than silently missing.  Any category in
    ``counts`` outside the assignable set is appended so nothing is hidden.
    """
    names = list(review_mod._assignable_categories())
    names.extend(sorted(name for name in counts if name not in names))
    return [{'name': name, 'count': counts.get(name, 0)} for name in names]


def create_app(conn: sqlite3.Connection, corpus_dir: str, index_path: str, *, fetch,
               signer=None, clock=None):
    """Build the Flask app over an open ledger ``conn`` and the corpus/index.

    ``fetch``, ``signer`` and ``clock`` are injected exactly as the CLI injects
    them, so the handlers are pure given fakes and test offline through
    ``app.test_client()``.  ``signer`` is the ``record_bytes -> (signature,
    signed_by)`` callable used to sign a human verdict; when ``None`` the UI is
    read-only (no verdict form, no POST).  ``clock`` is the single clock read --
    a ``() -> ISO-8601 str`` -- captured once per POST and threaded into the
    signed record; it defaults to the CLI's UTC clock.
    """
    from flask import Flask, abort, redirect, render_template_string, request, url_for

    app = Flask('divergulent.review_web')
    clock = clock or review_mod._cli_now
    categories = review_mod._assignable_categories()
    valid_choices = set(categories) | {review_mod.CHOICE_ACCEPT, review_mod.CHOICE_DEFER}

    def _pending_item(fingerprint: str):
        """The pending queue row for ``fingerprint``, or ``None`` if not queued."""
        for item in ledger_mod.pending_review_items(conn):
            if item['fingerprint'] == fingerprint:
                return item
        return None

    def _worklist_row(item, level) -> dict:
        fingerprint = item['fingerprint']
        packages = review_mod._carrying_packages(index_path, fingerprint)
        return {
            'fingerprint': fingerprint,
            'short': fingerprint[:16],
            'priority': item['priority'],
            'draft_category': item['draft_category'],
            'reason': item['reason'],
            'n_packages': len(packages),
            # 'normal' is rendered as no badge; only large/oversized show.
            'reviewability': None if level == 'normal' else level,
        }

    def _worklist_category_chips() -> list[dict]:
        """The category filter chips for the worklist: full set, pending counts."""
        counts: dict[str, int] = {}
        for item in ledger_mod.pending_review_items(conn):
            category = item['draft_category']
            if category:
                counts[category] = counts.get(category, 0) + 1
        return category_chips(counts)

    def _worklist_reviewability_chips(levels: dict) -> list[dict]:
        """Filter chips for the non-normal reviewability tiers, with pending counts.

        Only ``large`` / ``oversized`` are surfaced (``normal`` is the unbadged
        bulk); the ``oversized`` chip is the "not line-reviewable" bucket the
        operator handles deliberately.
        """
        counts: dict[str, int] = {}
        for item in ledger_mod.pending_review_items(conn):
            level = levels.get(item['fingerprint'], 'normal')
            if level != 'normal':
                counts[level] = counts.get(level, 0) + 1
        return [{'name': name, 'count': counts[name]}
                for name in ('large', 'oversized') if name in counts]

    @app.route('/')
    def index():
        query = request.args.get('fingerprint', '').strip()
        if query:
            # Cherry-pick: resolve a full hex or unambiguous prefix and jump to it.
            resolved, matches = review_mod.resolve_fingerprint(conn, query)
            if resolved is not None:
                return redirect(url_for('review', fingerprint=resolved))
            return render_template_string(SEARCH_TEMPLATE, query=query, matches=matches), 404

        category = request.args.get('category', '').strip() or None
        if category:
            items = ledger_mod.pending_review_items_in_category(conn, category)
        else:
            items = ledger_mod.pending_review_items(conn)
        # Package filter: narrow to pending items whose fingerprint is carried by a
        # source package matching the query (priority order preserved).
        package = request.args.get('package', '').strip() or None
        if package:
            fps = review_mod.fingerprints_for_package(index_path, package)
            items = [item for item in items if item['fingerprint'] in fps]
        # Reviewability filter: the size axis (e.g. the oversized, not-line-
        # reviewable bucket). Composes with the category/package filters.
        levels = reviewability_mod.reviewability_by_fingerprint(conn)
        reviewability = request.args.get('reviewability', '').strip() or None
        if reviewability:
            items = [item for item in items
                     if levels.get(item['fingerprint'], 'normal') == reviewability]
        rows = [_worklist_row(item, levels.get(item['fingerprint'], 'normal')) for item in items]
        top = items[0]['fingerprint'] if items else None
        # The category/package filters, as a query string, so the reviewability
        # chips can preserve them (and "all sizes" can reset only reviewability).
        base_params = {}
        if category:
            base_params['category'] = category
        if package:
            base_params['package'] = package
        return render_template_string(
            WORKLIST_TEMPLATE, rows=rows, category=category, package=package,
            categories=_worklist_category_chips(), top=top, total=len(items),
            reviewability=reviewability, reviewabilities=_worklist_reviewability_chips(levels),
            base_qs=urlencode(base_params))

    @app.route('/review/<fingerprint>')
    def review(fingerprint):
        resolved, matches = review_mod.resolve_fingerprint(conn, fingerprint)
        if resolved is None:
            return render_template_string(
                SEARCH_TEMPLATE, query=fingerprint, matches=matches), 404
        item = _pending_item(resolved)
        context = review_mod.build_review_context(
            conn, corpus_dir, index_path, fingerprint=resolved, item=item, fetch=fetch)
        if context is None:
            return render_template_string(NO_PATCH_TEMPLATE, fingerprint=resolved), 404
        queued = item is not None
        # A settled, non-queued item (reached from /audit) shows its current
        # derived verdict and a re-queue action instead of the verdict form.
        verdict = None if queued else verdict_mod.current_verdict(conn).get(resolved)
        level = reviewability_mod.reviewability_by_fingerprint(conn).get(resolved, 'normal')
        return render_template_string(
            REVIEW_TEMPLATE, ctx=context, queued=queued,
            can_verdict=signer is not None, categories=categories,
            verdict=verdict, can_requeue=signer is not None and not queued,
            reviewability=None if level == 'normal' else level,
            oversized_lines=reviewability_mod.REVIEWABILITY_OVERSIZED_LINES,
            package_lines=review_mod._format_package_lines(context),
            diff=diff_lines(context.context_view))

    @app.route('/review/<fingerprint>', methods=['POST'])
    def submit_review(fingerprint):
        if signer is None:
            abort(405)  # read-only instance: no verdicts
        resolved, matches = review_mod.resolve_fingerprint(conn, fingerprint)
        if resolved is None:
            return render_template_string(
                SEARCH_TEMPLATE, query=fingerprint, matches=matches), 404
        item = _pending_item(resolved)
        if item is None:
            # Already reviewed (e.g. a second tab, or a re-submit): nothing left to
            # record.  Idempotent -- navigate back rather than erroring.
            return redirect(url_for('index'))
        context = review_mod.build_review_context(
            conn, corpus_dir, index_path, fingerprint=resolved, item=item, fetch=fetch)
        if context is None:
            return render_template_string(NO_PATCH_TEMPLATE, fingerprint=resolved), 404

        choice = request.form.get('choice', '').strip()
        if choice not in valid_choices:
            return render_template_string(
                REVIEW_TEMPLATE, ctx=context, queued=True, can_verdict=True,
                categories=categories,
                package_lines=review_mod._format_package_lines(context),
                diff=diff_lines(context.context_view),
                error='pick a verdict: accept the draft, a category, or defer'), 400

        # Capture the clock ONCE, server-side, so the signed record and the
        # decision share the timestamp -- exactly as the CLI threads _cli_now().
        now = clock()
        try:
            outcome = review_mod.record_review_verdict(
                conn, item, context, choice, signer=signer, now=now)
        except Exception as exc:  # noqa: BLE001 -- a signing/auth failure is a page, not a 500
            # record_review_verdict signs BEFORE it writes, so a signer failure
            # leaves the ledger untouched; surface it as an actionable page.
            return render_template_string(
                ERROR_TEMPLATE, fingerprint=resolved, error=str(exc)), 502

        if outcome.recorded:
            # A fresh human verdict tops precedence immediately, and any items the
            # ledger can now settle deterministically are dequeued -- mirroring the
            # CLI's post-review rebuild.
            verdict_mod.rebuild_current_verdict(conn)
            ledger_mod.resolve_settled_review_items(conn, now=now)
        return redirect(url_for('index'))

    @app.route('/audit')
    def audit():
        # Spot-check settled patches that are NOT in the review queue: the derived
        # current verdict, filterable by category and by provenance (a decision
        # kind, or a specific decided_by rule).  Category here is the DERIVED
        # verdict -- which for a rule-classified fingerprint is the rule's
        # category -- the deliberate counterpart to the queue's LLM-draft category.
        # "Not in the queue": exclude fingerprints with a pending review item, so
        # the audit view is the settled residue, distinct from the review worklist.
        pending = {item['fingerprint'] for item in ledger_mod.pending_review_items(conn)}
        all_verdicts = [
            v for v in verdict_mod.current_verdict(conn).values() if v.fingerprint not in pending]
        verdicts = sorted(
            all_verdicts, key=lambda v: (v.kind, v.decided_by, v.category, v.fingerprint))
        category = request.args.get('category', '').strip() or None
        source = request.args.get('source', '').strip() or None
        if category:
            verdicts = [v for v in verdicts if v.category == category]
        if source:
            verdicts = [v for v in verdicts if source in (v.kind, v.decided_by)]

        total = len(verdicts)
        shown = verdicts[:AUDIT_LIMIT]
        cat_counts: dict[str, int] = {}
        for verdict in all_verdicts:
            cat_counts[verdict.category] = cat_counts.get(verdict.category, 0) + 1
        return render_template_string(
            AUDIT_TEMPLATE, rows=shown, total=total, shown=len(shown),
            limit=AUDIT_LIMIT, category=category, source_sel=source,
            categories=category_chips(cat_counts),
            kinds=sorted({v.kind for v in all_verdicts}))

    @app.route('/requeue/<fingerprint>', methods=['POST'])
    def requeue(fingerprint):
        if signer is None:
            abort(405)  # read-only instance: no mutations
        resolved, matches = review_mod.resolve_fingerprint(conn, fingerprint)
        if resolved is None:
            return render_template_string(
                SEARCH_TEMPLATE, query=fingerprint, matches=matches), 404
        # Re-queue records NO decision -- it supersedes the live human verdict (if
        # any) and re-opens the item for review.  Mirror the CLI: commit, then
        # rebuild so the superseded fingerprint drops back to pending immediately.
        now = clock()
        review_mod.requeue_one(conn, resolved, now=now)
        conn.commit()
        verdict_mod.rebuild_current_verdict(conn)
        return redirect(url_for('audit'))

    return app


def _lazy_sigstore_signer():
    """A signer that builds the real Sigstore signer on first use, then reuses it.

    Deferring ``build_sigstore_signer()`` until the first verdict means the UI
    starts (and browses) without the ``verify`` extra installed and without
    triggering the OIDC browser flow; a browse-only operator never pays for
    signing, and a missing ``sigstore`` surfaces as the actionable error page on
    the first POST rather than at startup.
    """
    holder: dict = {}

    def signer(record_bytes):
        if 'signer' not in holder:
            holder['signer'] = review_mod.build_sigstore_signer()
        return holder['signer'](record_bytes)

    return signer


def main(argv=None) -> int:
    """``python -m divergulent.classify.review_web``: serve the review UI locally.

    Binds loopback only and refuses any routable host.  Verdicts are signed with
    a lazily-built Sigstore signer (the browser OIDC flow runs on the first
    verdict, not at startup), so browsing works without the verify extra.
    """
    parser = argparse.ArgumentParser(
        prog='python -m divergulent.classify.review_web',
        description='Local web UI for human review of the patch residue.')
    parser.add_argument('--ledger', required=True, help='path to the ledger sqlite')
    parser.add_argument('--corpus', required=True, dest='corpus_dir',
                        help='path to the corpus directory (bodies + index)')
    parser.add_argument('--index', default=None,
                        help='path to fingerprints.sqlite (default: <corpus>/fingerprints.sqlite)')
    parser.add_argument('--host', default='127.0.0.1',
                        help='loopback host to bind (default: 127.0.0.1)')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help='port to bind (default: %d)' % DEFAULT_PORT)
    args = parser.parse_args(argv)

    try:
        host = require_loopback(args.host)
    except ValueError as exc:
        parser.error(str(exc))

    index_path = args.index or os.path.join(args.corpus_dir, 'fingerprints.sqlite')
    conn = ledger_mod.open_ledger(args.ledger)
    fetch = review_mod._real_fetch()
    app = create_app(conn, args.corpus_dir, index_path, fetch=fetch,
                     signer=_lazy_sigstore_signer())

    print('divergulent review UI on http://%s:%d/' % (host, args.port))
    # Single connection, single user: serve requests serially so the injected
    # sqlite connection is only ever touched from one thread.
    app.run(host=host, port=args.port, threaded=False)
    return 0


# ---------------------------------------------------------------------------
# Templates.  Inline strings rendered with Flask's render_template_string, whose
# Jinja environment autoescapes -- a patch/package/path containing < or & cannot
# break the page (the load-bearing reason for the Jinja dependency).
# ---------------------------------------------------------------------------

_HEAD = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{{ title }} -- divergulent review</title>
<style>
 :root { color-scheme: dark; }
 body { font: 14px/1.5 system-ui, sans-serif; margin: 1.5rem;
        background: #16181c; color: #ccd0d6; }
 a { color: #6cb6ff; } h1 { font-size: 1.3rem; } h2 { font-size: 1.05rem; }
 table { border-collapse: collapse; width: 100%; }
 th, td { text-align: left; padding: 0.3rem 0.6rem; border-bottom: 1px solid #2a2f38; }
 th { background: #232730; color: #e0e3e8; }
 tr:nth-child(even) td { background: #1d2026; }
 tr:hover td { background: #262b34; }
 .chip { display: inline-block; padding: 0.1rem 0.5rem; margin: 0.1rem;
         border: 1px solid #3a4150; border-radius: 1rem; text-decoration: none; }
 .chip.on { background: #2563eb; color: #fff; border-color: #2563eb; }
 .chip.empty { opacity: 0.45; }
 .rev { display: inline-block; padding: 0 0.4rem; border-radius: 0.2rem;
        font-size: 0.8rem; font-weight: bold; }
 .rev.large { background: #3a2f12; color: #e3c878; }
 .rev.oversized { background: #4a1c1c; color: #ff9a92; }
 .next { display: inline-block; margin: 0.5rem 0; padding: 0.4rem 0.8rem;
         background: #2563eb; color: #fff; border-radius: 0.3rem; text-decoration: none; }
 .meta-block { background: #232730; padding: 0.6rem 0.8rem; border-radius: 0.3rem; }
 .claim-block { background: #1e2128; border-left: 3px solid #b8860b;
                padding: 0.5rem 0.8rem; border-radius: 0.3rem; margin: 0.6rem 0; }
 .claim-desc { white-space: pre-wrap; margin: 0.3rem 0; color: #e0e3e8; }
 pre.diff { background: #0f1115; border: 1px solid #2a2f38; padding: 0.6rem;
            overflow-x: auto; font: 12px/1.4 ui-monospace, monospace; }
 pre.diff span { display: block; min-width: 100%; width: fit-content; min-height: 1.4em; }
 pre.diff .add { color: #5fd17a; } pre.diff .del { color: #ff7b72; }
 pre.diff .hunk { color: #9aa0aa; background: #232730; } pre.diff .meta { color: #6b7280; }
 /* Upstream context (lines not part of the patch) gets a faint purple wash so the
    added/removed lines, left on the base background, read as the changed regions. */
 pre.diff .ctx { background: #1a1228; }
 .mono { font-family: ui-monospace, monospace; }
 .muted { color: #8a909a; } .error { color: #ff7b72; font-weight: bold; }
 fieldset.verdict { border: 1px solid #2a2f38; border-radius: 0.3rem; }
 fieldset.verdict label { display: block; padding: 0.15rem 0; }
 input[type=text] { background: #232730; color: #ccd0d6; border: 1px solid #3a4150;
                    border-radius: 0.2rem; padding: 0.2rem 0.4rem; }
 button { font-size: 1rem; padding: 0.4rem 0.8rem; cursor: pointer;
          background: #2a2f38; color: #e0e3e8; border: 1px solid #3a4150;
          border-radius: 0.2rem; }
 button:hover { background: #333944; }
 .key { display: inline-block; min-width: 1.1em; padding: 0 0.25em; text-align: center;
        background: #2a2f38; border: 1px solid #3a4150; border-radius: 0.2rem;
        font: 11px/1.4 ui-monospace, monospace; color: #b0b6c0; }
</style></head><body>
'''

_FOOT = '''
</body></html>'''

WORKLIST_TEMPLATE = _HEAD.replace('{{ title }}', 'worklist') + '''
<h1>Review worklist</h1>
<p><a href="/audit">audit settled patches &rarr;</a></p>
<form method="get" action="/">
  <input type="text" name="fingerprint" placeholder="jump to fingerprint / prefix"
         class="mono" size="34">
  <button type="submit">go</button>
</form>
<form method="get" action="/">
  <input type="text" name="package" placeholder="filter by package (e.g. llvm)"
         value="{{ package or '' }}" size="34">
  {% if category %}<input type="hidden" name="category" value="{{ category }}">{% endif %}
  <button type="submit">filter</button>
  {% if package %}<a href="/{{ '?category=' + category if category }}">clear</a>{% endif %}
</form>
<p>
  <a class="chip {{ 'on' if not category }}"
     href="/{{ '?package=' + package if package }}">all</a>
  {% for cat in categories %}
    <a class="chip {{ 'on' if category == cat.name }}{{ ' empty' if cat.count == 0 }}"
       href="/?category={{ cat.name | urlencode }}{{ '&package=' + package if package }}"
       >{{ cat.name }} <span class="muted">({{ cat.count }})</span></a>
  {% endfor %}
</p>
{% if reviewabilities or reviewability %}
<p>
  <span class="muted">size:</span>
  <a class="chip {{ 'on' if not reviewability }}" href="/{{ '?' + base_qs if base_qs }}">all sizes</a>
  {% for r in reviewabilities %}
    <a class="chip {{ 'on' if reviewability == r.name }}"
       href="/?reviewability={{ r.name }}{{ '&' + base_qs if base_qs }}"
       >{{ r.name }} <span class="muted">({{ r.count }})</span></a>
  {% endfor %}
</p>
{% endif %}
{% if top %}
  <a class="next" href="/review/{{ top }}">Review next most important &rarr;</a>
  <span class="muted">(press <span class="key">j</span>)</span>
{% endif %}
<p class="muted">{{ total }} pending{% if category %} in <b>{{ category }}</b>{% endif %}{%
  if package %} carried by <b>{{ package }}</b>{% endif %}.</p>
<script>
document.addEventListener('keydown', function(e) {
  if (e.target.tagName === 'INPUT' || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === 'j' || e.key === 'n') {
    var a = document.querySelector('a.next');
    if (a) location.href = a.getAttribute('href');
  }
});
</script>
<table>
  <tr><th>priority</th><th>draft</th><th>size</th><th>pkgs</th><th>fingerprint</th><th>reason</th></tr>
  {% for row in rows %}
  <tr>
    <td>{{ row.priority }}</td>
    <td>{{ row.draft_category or '-' }}</td>
    <td>{% if row.reviewability %}<span class="rev {{ row.reviewability }}"
        >{{ row.reviewability }}</span>{% endif %}</td>
    <td>{{ row.n_packages }}</td>
    <td class="mono"><a href="/review/{{ row.fingerprint }}">{{ row.short }}</a></td>
    <td class="muted">{{ row.reason or '' }}</td>
  </tr>
  {% endfor %}
</table>
''' + _FOOT

REVIEW_TEMPLATE = _HEAD.replace('{{ title }}', 'review') + '''
<p><a href="/">&larr; worklist</a></p>
<h1 class="mono">{{ ctx.fingerprint[:16] }}<span class="muted">{{ ctx.fingerprint[16:] }}</span>
{% if reviewability %} <span class="rev {{ reviewability }}">{{ reviewability }}</span>{% endif %}</h1>
{% if reviewability == 'oversized' %}
<p class="rev oversized" style="padding: 0.4rem 0.6rem;">This diff is oversized (&gt;{{ oversized_lines }}
changed lines) and is not realistically line-reviewable. Treat it as trust-upstream / spot-check rather
than a line-by-line read.</p>
{% endif %}
<div class="meta-block">
  {% for line in package_lines %}<div>{{ line }}</div>{% endfor %}
  {% if ctx.reason %}<div>routed to review because: {{ ctx.reason }}</div>{% endif %}
  {% if ctx.draft_category %}
    <div>LLM draft: <b>{{ ctx.draft_category }}</b> (confidence {{ ctx.draft_confidence }})</div>
    {% if ctx.draft_reasoning %}<div class="muted">LLM reasoning: {{ ctx.draft_reasoning }}</div>{% endif %}
  {% else %}
    <div>LLM draft: <span class="muted">(none)</span></div>
  {% endif %}
  {% if not queued %}
    <div class="muted">(not in the review queue -- spot-checking a settled patch)</div>
    {% if verdict %}
      <div>current verdict: <b>{{ verdict.category }}</b>
        ({{ verdict.kind }}{% if verdict.verified %}, verified{% endif %},
        by {{ verdict.decided_by }} v{{ verdict.rule_version }})</div>
    {% endif %}
  {% endif %}
</div>
<div class="claim-block">
  <div class="muted">What the author claims (unverified -- read it against the diff):</div>
  {% if ctx.claim_description %}
    <div class="claim-desc">{{ ctx.claim_description }}</div>
  {% else %}
    <div class="muted">(no DEP-3 description in the patch header)</div>
  {% endif %}
  <div class="muted">claimed category: <b>{{ ctx.claim_category }}</b>
    &middot; forwarding: {{ ctx.claim_forwarded }}
    {% if ctx.claim_bugs %}&middot; bugs:
      {% for b in ctx.claim_bugs %}{%
        if b.ref.startswith('http') %}<a href="{{ b.ref }}">{{ b.tracker }}</a>{%
        else %}{{ b.tracker }}:{{ b.ref }}{% endif %}{% if not loop.last %}, {% endif %}{% endfor %}
    {% endif %}
    {% if ctx.claim_cves %}&middot; CVEs: {{ ctx.claim_cves | join(', ') }}{% endif %}
  </div>
</div>
{% if can_requeue %}
<form method="post" action="/requeue/{{ ctx.fingerprint }}">
  <button type="submit">Re-queue for human review</button>
  <span class="muted">supersedes the current verdict; records no decision</span>
</form>
{% endif %}
{% if queued and can_verdict %}
<h2>Your verdict</h2>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post" action="/review/{{ ctx.fingerprint }}">
  <fieldset class="verdict">
    {% set ns = namespace(n=0) %}
    {% if ctx.draft_category %}{% set ns.n = ns.n + 1 %}
      <label><span class="key">{{ ns.n }}</span>
        <input type="radio" name="choice" value="accept" checked>
        accept the draft (<b>{{ ctx.draft_category }}</b>)</label>
    {% endif %}
    {% for cat in categories %}{% set ns.n = ns.n + 1 %}
      <label><span class="key">{{ ns.n }}</span>
        <input type="radio" name="choice" value="{{ cat }}"> {{ cat }}</label>
    {% endfor %}
    {% set ns.n = ns.n + 1 %}
    <label><span class="key">{{ ns.n }}</span>
      <input type="radio" name="choice" value="defer"> defer (record nothing)</label>
  </fieldset>
  <button type="submit">Record verdict &amp; sign</button>
  <p class="muted">keys: <span class="key">1</span>-<span class="key">9</span> pick &middot;
    <span class="key">a</span> accept &middot; <span class="key">d</span> defer &middot;
    <span class="key">Enter</span> submit</p>
</form>
<script>
document.addEventListener('keydown', function(e) {
  if (e.target.tagName === 'INPUT' || e.metaKey || e.ctrlKey || e.altKey) return;
  var radios = Array.prototype.slice.call(document.querySelectorAll('input[name=choice]'));
  if (!radios.length) return;
  var pick = null;
  if (e.key >= '1' && e.key <= '9') pick = radios[parseInt(e.key, 10) - 1];
  else if (e.key === 'a') pick = radios.filter(function(r){ return r.value === 'accept'; })[0];
  else if (e.key === 'd') pick = radios.filter(function(r){ return r.value === 'defer'; })[0];
  if (pick) { pick.checked = true; e.preventDefault(); }
  else if (e.key === 'Enter') document.querySelector('form').submit();
});
</script>
{% endif %}
<h2>Diff in upstream context</h2>
<pre class="diff">{% for line in diff %}<span class="{{ line.cls }}">{{ line.text }}</span>{% endfor %}</pre>
''' + _FOOT

ERROR_TEMPLATE = _HEAD.replace('{{ title }}', 'error') + '''
<p><a href="/">&larr; worklist</a></p>
<h1>Could not record the verdict</h1>
<p>The verdict for <span class="mono">{{ fingerprint[:16] }}</span> was NOT recorded
-- the ledger is unchanged (the record is signed before it is written).</p>
<pre class="diff error">{{ error }}</pre>
<p class="muted">Fix the issue and try again. Signing needs the verify extra:
<span class="mono">pip install divergulent[review,verify]</span>.</p>
''' + _FOOT

AUDIT_TEMPLATE = _HEAD.replace('{{ title }}', 'audit') + '''
<p><a href="/">&larr; worklist</a></p>
<h1>Audit settled patches</h1>
<p class="muted">Spot-check patches that are <b>not</b> in the review queue -- the
derived current verdict, including rule-classified patches. Confirm a rule is
right, or re-queue a misfire for human review.</p>
<p>category:
  <a class="chip {{ 'on' if not category }}"
     href="/audit{{ '?source=' + source_sel if source_sel }}">all</a>
  {% for cat in categories %}
    <a class="chip {{ 'on' if category == cat.name }}{{ ' empty' if cat.count == 0 }}"
       href="/audit?category={{ cat.name | urlencode }}{{ '&source=' + source_sel if source_sel }}"
       >{{ cat.name }} <span class="muted">({{ cat.count }})</span></a>
  {% endfor %}
</p>
<p>source:
  <a class="chip {{ 'on' if not source_sel }}"
     href="/audit{{ '?category=' + category if category }}">all</a>
  {% for k in kinds %}
    <a class="chip {{ 'on' if source_sel == k }}"
       href="/audit?source={{ k | urlencode }}{{ '&category=' + category if category }}">{{ k }}</a>
  {% endfor %}
</p>
<p class="muted">
  showing {{ shown }} of {{ total }}{% if category %} in <b>{{ category }}</b>{% endif %}{%
  if source_sel %} from <b>{{ source_sel }}</b>{% endif %}{% if total > limit %}
  (capped at {{ limit }} -- filter to narrow){% endif %}.
</p>
<table>
  <tr><th>category</th><th>kind</th><th>decided by</th><th>fingerprint</th></tr>
  {% for v in rows %}
  <tr>
    <td>{{ v.category }}</td>
    <td>{{ v.kind }}{% if v.verified %} <span class="muted">(verified)</span>{% endif %}</td>
    <td><a href="/audit?source={{ v.decided_by | urlencode }}">{{ v.decided_by }}</a>
        <span class="muted">v{{ v.rule_version }}</span></td>
    <td class="mono"><a href="/review/{{ v.fingerprint }}">{{ v.fingerprint[:16] }}</a></td>
  </tr>
  {% endfor %}
</table>
''' + _FOOT

SEARCH_TEMPLATE = _HEAD.replace('{{ title }}', 'no match') + '''
<p><a href="/">&larr; worklist</a></p>
<h1>No single match for <span class="mono">{{ query }}</span></h1>
{% if matches %}
  <p>Ambiguous prefix matches {{ matches | length }} fingerprints:</p>
  <ul>
  {% for fp in matches[:25] %}
    <li class="mono"><a href="/review/{{ fp }}">{{ fp }}</a></li>
  {% endfor %}
  </ul>
{% else %}
  <p class="muted">No fingerprint matches that query.</p>
{% endif %}
''' + _FOOT

NO_PATCH_TEMPLATE = _HEAD.replace('{{ title }}', 'no patch') + '''
<p><a href="/">&larr; worklist</a></p>
<h1>Nothing to show for <span class="mono">{{ fingerprint[:16] }}</span></h1>
<p class="muted">This fingerprint has no representative patch in the index, so there
is no diff to review.</p>
''' + _FOOT


if __name__ == '__main__':
    raise SystemExit(main())
