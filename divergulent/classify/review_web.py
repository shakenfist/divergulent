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

from divergulent.classify import ledger as ledger_mod
from divergulent.classify import review as review_mod

# The handlers reuse review.py's fingerprint-keyed read helpers directly rather
# than duplicating context-building; they are package-internal shared API, used
# here exactly as the CLI uses them.
DEFAULT_PORT = 8765
LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '::1')


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


def create_app(conn: sqlite3.Connection, corpus_dir: str, index_path: str, *, fetch,
               signer=None):
    """Build the Flask app over an open ledger ``conn`` and the corpus/index.

    ``fetch`` and ``signer`` are injected exactly as the CLI injects them, so the
    handlers are pure given fakes and test offline through ``app.test_client()``.
    ``signer`` is unused while the UI is read-only; it is threaded through now so
    the verdict POST can pick it up without changing this signature.
    """
    from flask import Flask, redirect, render_template_string, request, url_for

    app = Flask('divergulent.review_web')

    def _pending_item(fingerprint: str):
        """The pending queue row for ``fingerprint``, or ``None`` if not queued."""
        for item in ledger_mod.pending_review_items(conn):
            if item['fingerprint'] == fingerprint:
                return item
        return None

    def _worklist_row(item) -> dict:
        fingerprint = item['fingerprint']
        packages = review_mod._carrying_packages(index_path, fingerprint)
        return {
            'fingerprint': fingerprint,
            'short': fingerprint[:16],
            'priority': item['priority'],
            'draft_category': item['draft_category'],
            'reason': item['reason'],
            'n_packages': len(packages),
        }

    def _categories_present() -> list[str]:
        """Distinct draft categories among pending items, for the filter chips."""
        return sorted({
            item['draft_category'] for item in ledger_mod.pending_review_items(conn)
            if item['draft_category']})

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
        rows = [_worklist_row(item) for item in items]
        top = items[0]['fingerprint'] if items else None
        return render_template_string(
            WORKLIST_TEMPLATE, rows=rows, category=category,
            categories=_categories_present(), top=top, total=len(items))

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
        return render_template_string(
            REVIEW_TEMPLATE, ctx=context, queued=item is not None,
            package_lines=review_mod._format_package_lines(context),
            diff=diff_lines(context.context_view))

    return app


def main(argv=None) -> int:
    """``python -m divergulent.classify.review_web``: serve the review UI locally.

    Read-only for now (no signer wired); binds loopback only and refuses any
    routable host.  The signed verdict path is added in a later step.
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
    app = create_app(conn, args.corpus_dir, index_path, fetch=fetch, signer=None)

    print('divergulent review UI (read-only) on http://%s:%d/' % (host, args.port))
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
 body { font: 14px/1.5 system-ui, sans-serif; margin: 1.5rem; color: #1a1a1a; }
 a { color: #0645ad; } h1 { font-size: 1.3rem; }
 table { border-collapse: collapse; width: 100%; }
 th, td { text-align: left; padding: 0.3rem 0.6rem; border-bottom: 1px solid #ddd; }
 .chip { display: inline-block; padding: 0.1rem 0.5rem; margin: 0.1rem;
         border: 1px solid #bbb; border-radius: 1rem; text-decoration: none; }
 .chip.on { background: #0645ad; color: #fff; border-color: #0645ad; }
 .next { display: inline-block; margin: 0.5rem 0; padding: 0.4rem 0.8rem;
         background: #0645ad; color: #fff; border-radius: 0.3rem; text-decoration: none; }
 .meta-block { background: #f6f6f6; padding: 0.6rem 0.8rem; border-radius: 0.3rem; }
 pre.diff { background: #fafafa; border: 1px solid #e0e0e0; padding: 0.6rem;
            overflow-x: auto; font: 12px/1.4 ui-monospace, monospace; }
 pre.diff .add { color: #08660d; } pre.diff .del { color: #a31515; }
 pre.diff .hunk { color: #555; background: #eee; } pre.diff .meta { color: #888; }
 .mono { font-family: ui-monospace, monospace; }
 .muted { color: #777; }
</style></head><body>
'''

_FOOT = '''
</body></html>'''

WORKLIST_TEMPLATE = _HEAD.replace('{{ title }}', 'worklist') + '''
<h1>Review worklist</h1>
<form method="get" action="/">
  <input type="text" name="fingerprint" placeholder="jump to fingerprint / prefix"
         class="mono" size="40">
  <button type="submit">go</button>
</form>
<p>
  <a class="chip {{ 'on' if not category }}" href="/">all</a>
  {% for cat in categories %}
    <a class="chip {{ 'on' if category == cat }}"
       href="/?category={{ cat | urlencode }}">{{ cat }}</a>
  {% endfor %}
</p>
{% if top %}
  <a class="next" href="/review/{{ top }}">Review next most important &rarr;</a>
{% endif %}
<p class="muted">{{ total }} pending{% if category %} in <b>{{ category }}</b>{% endif %}.</p>
<table>
  <tr><th>priority</th><th>draft</th><th>pkgs</th><th>fingerprint</th><th>reason</th></tr>
  {% for row in rows %}
  <tr>
    <td>{{ row.priority }}</td>
    <td>{{ row.draft_category or '-' }}</td>
    <td>{{ row.n_packages }}</td>
    <td class="mono"><a href="/review/{{ row.fingerprint }}">{{ row.short }}</a></td>
    <td class="muted">{{ row.reason or '' }}</td>
  </tr>
  {% endfor %}
</table>
''' + _FOOT

REVIEW_TEMPLATE = _HEAD.replace('{{ title }}', 'review') + '''
<p><a href="/">&larr; worklist</a></p>
<h1 class="mono">{{ ctx.fingerprint[:16] }}<span class="muted">{{ ctx.fingerprint[16:] }}</span></h1>
<div class="meta-block">
  {% for line in package_lines %}<div>{{ line }}</div>{% endfor %}
  {% if ctx.reason %}<div>routed to review because: {{ ctx.reason }}</div>{% endif %}
  <div>author claim category: <b>{{ ctx.claim_category }}</b></div>
  {% if ctx.draft_category %}
    <div>LLM draft: <b>{{ ctx.draft_category }}</b> (confidence {{ ctx.draft_confidence }})</div>
    {% if ctx.draft_reasoning %}<div class="muted">LLM reasoning: {{ ctx.draft_reasoning }}</div>{% endif %}
  {% else %}
    <div>LLM draft: <span class="muted">(none)</span></div>
  {% endif %}
  {% if not queued %}<div class="muted">(not in the review queue)</div>{% endif %}
</div>
<h2>Diff in upstream context</h2>
<pre class="diff">{% for line in diff %}<span class="{{ line.cls }}">{{ line.text }}</span>
{% endfor %}</pre>
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
