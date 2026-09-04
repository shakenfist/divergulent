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
single-user local tool, never a networked service.  A loopback bind is not on its
own a security boundary -- any page in the operator's browser can POST to it -- so
every mutating request must additionally clear :func:`guard_request`: a same-origin
``Origin``, a loopback ``Host`` on the bound port, and the per-process CSRF token
(see the guard's docstring).  All handler logic is driven through Flask's test
client in the tests, with an injected fake ``fetch`` and a temp ledger -- no real
socket, no real network.
"""

from __future__ import annotations

import argparse
import hmac
import os
import secrets
import sqlite3
from urllib.parse import urlencode, urlsplit

from divergulent.classify import cross_reference as xref_mod
from divergulent.classify import generated as generated_mod
from divergulent.classify import injection as injection_mod
from divergulent.classify import ledger as ledger_mod
from divergulent.classify import review as review_mod
from divergulent.classify import reach as reach_mod
from divergulent.classify import reviewability as reviewability_mod
from divergulent.classify import risk as risk_mod
from divergulent.classify import verdict as verdict_mod

# The handlers reuse review.py's fingerprint-keyed read helpers directly rather
# than duplicating context-building; they are package-internal shared API, used
# here exactly as the CLI uses them.
DEFAULT_PORT = 8765
LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '::1')
# The audit view can span the whole settled archive; cap the rendered rows and
# tell the operator how many were dropped rather than building a vast page.
AUDIT_LIMIT = 500

# The CSRF token's cookie name, its hidden-form-field name, and the app.config
# key holding the value.  The token is minted once per ``create_app`` (per
# process) from ``secrets.token_urlsafe`` and is never derived from anything
# stable, so it cannot be guessed, and a token learned from one run is worthless
# against the next.
CSRF_COOKIE = 'divergulent_csrf'
CSRF_FIELD = 'csrf_token'
CSRF_CONFIG_KEY = 'DIVERGULENT_CSRF_TOKEN'
# Methods that cannot change the ledger.  Everything else is a mutating request
# and must clear the full origin + host + token check.
SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})


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


def _split_authority(value: str) -> tuple[str, str | None]:
    """Split an HTTP authority (``Host`` value) into ``(host, port_or_None)``.

    IPv6-literal aware: ``[::1]:8765`` splits to ``('::1', '8765')``, while a bare
    ``::1`` (which a client should bracket, but might not) keeps its colons and is
    returned whole with no port rather than being mangled into a wrong host.
    """
    if value.startswith('['):
        closing = value.find(']')
        if closing < 0:
            return value, None
        rest = value[closing + 1:]
        return value[1:closing], rest[1:] if rest.startswith(':') else None
    if value.count(':') == 1:
        host, _, port = value.partition(':')
        return host, port
    return value, None


def host_is_local(value: str | None, port: int) -> bool:
    """Is ``value`` (an HTTP authority) this app's own loopback address and port?

    The defence against DNS rebinding, which is what would otherwise let a page on
    ``evil.example`` read the ledger through the UI: the attacker controls where the
    NAME resolves, but the browser still sends the name it typed in ``Host``, so a
    rebound request carries ``Host: evil.example`` and is refused here.

    The accepted NAMES are the loopback set rather than the single literal that was
    bound, because they are interchangeable in practice -- a browser pointed at
    ``http://localhost:8765/`` reaches a ``127.0.0.1`` bind -- and none of them is
    attacker-controllable.  The PORT, when the client sends one, must be the bound
    one exactly: a different port is a different server.
    """
    if not value:
        return False
    host, host_port = _split_authority(value.strip())
    if host.lower() not in LOOPBACK_HOSTS:
        return False
    return host_port is None or host_port == str(port)


def origin_is_local(value: str | None, port: int) -> bool:
    """Is ``value`` (an ``Origin`` header) this app's own origin?

    An ``Origin`` is scheme + authority; the UI is served over plain HTTP on
    loopback, so anything else -- another scheme, another host, another port, or the
    literal ``null`` a sandboxed iframe sends -- is not us.

    The PORT is required EXACTLY, unlike :func:`host_is_local`, which tolerates an
    absent one because a client may legitimately omit it from ``Host``.  An origin
    has no such licence: a browser writes the port whenever it is not the scheme's
    default, so an absent one means 80 and nothing else.  Delegating the
    port-optional rule here would let a page served from ``http://localhost:80``
    pass as this app on 8765 -- which is what the paragraph above promises it does
    not.
    """
    if not value:
        return False
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return False  # unparseable is certainly not us
    if parsed.scheme != 'http':
        return False
    host, origin_port = _split_authority(parsed.netloc)
    if host.lower() not in LOOPBACK_HOSTS:
        return False
    return (origin_port or '80') == str(port)


def guard_request(*, method: str, host: str | None, origin: str | None,
                  cookie_token: str | None, form_token: str | None,
                  expected_token: str, port: int) -> str | None:
    """Return why this request must be refused, or ``None`` if it may proceed.

    Three independent layers, because none of them is sufficient alone:

    1. ``Host`` must name loopback on the bound port -- on EVERY request, safe ones
       included, since DNS rebinding would otherwise make the read pages (the whole
       ledger) readable by a remote page.
    2. ``Origin`` must be present and must be this app's own origin.  A browser
       sends ``Origin`` on every cross-origin form POST, so a MISSING one on a
       mutating request is treated as hostile rather than waved through -- the
       permissive reading is exactly the hole this closes.
    3. The per-process CSRF token must arrive in BOTH the ``SameSite=Strict``
       cookie and the hidden form field, and the two must match the process token
       under :func:`hmac.compare_digest`.  ``SameSite=Strict`` means a cross-site
       POST carries no cookie at all, and the hidden field means a token leaked
       through a cookie-only channel still cannot be replayed.

    Pure and header-shaped rather than Flask-shaped so the policy is testable on its
    own, and so the caller (a ``before_request`` hook) stays three lines long.
    """
    if not host_is_local(host, port):
        return ('the Host header %r is not this review UI on loopback port %d -- refusing to '
                'answer, since a name that resolves here is how a remote page reaches a local '
                'server (DNS rebinding)' % (host or '', port))
    if method.upper() in SAFE_METHODS:
        return None
    if origin is None:
        return ('this state-changing request carried no Origin header; a browser sends one on '
                'every cross-origin form post, so an absent Origin cannot be trusted to mean '
                '"same origin"')
    if not origin_is_local(origin, port):
        return ('this state-changing request came from %r, which is not this review UI; a '
                'verdict may only be submitted from the UI itself' % origin)
    if not cookie_token:
        return ('this state-changing request carried no %s cookie; the cookie is SameSite=Strict, '
                'so a cross-site post never has one' % CSRF_COOKIE)
    if not form_token:
        return 'this state-changing request carried no %s form field' % CSRF_FIELD
    expected = expected_token.encode('utf-8')
    # Compared as BYTES: ``compare_digest`` raises on a non-ASCII str, and both of
    # these came off the wire, so a hostile cookie would otherwise be a 500.
    if not (hmac.compare_digest(cookie_token.encode('utf-8'), expected)
            and hmac.compare_digest(form_token.encode('utf-8'), expected)):
        return ('the CSRF token did not match this process; reload the page (the token is minted '
                'per run, so a tab left open across a restart carries a stale one)')
    return None


def diff_lines(text: str) -> list[dict]:
    """Split a rendered diff-in-context into ``{cls, text, block_start}`` rows.

    Classifies each line so the template can colour additions, deletions, hunk
    headers and file markers distinctly from upstream context -- the diff reads
    nicer than the CLI pager without any highlighter dependency. ``block_start``
    marks the FIRST line of each contiguous changed (add/del) run -- the anchors
    the review page's keyboard navigation jumps between, so a reviewer can skip
    from change to change instead of scrolling through the expanded context. A
    delete-then-add modification is one block (both lines are "changed").

    The context view's per-file ``### <path>`` block headers are classed
    ``file`` and numbered (``file_index``, 1-based, in render order) so the
    files-changed list above the diff can anchor-link each row to its block --
    the segment order matches ``review.diff_file_stats`` exactly, since both
    come from ``split_diff_by_file``.
    """
    rows = []
    prev_changed = False
    file_index = 0
    for line in text.splitlines():
        if line.startswith('### '):
            cls = 'file'
        elif line.startswith('@@'):
            cls = 'hunk'
        elif line.startswith(('+++', '---')):
            cls = 'meta'
        elif line.startswith('+'):
            cls = 'add'
        elif line.startswith('-'):
            cls = 'del'
        else:
            cls = 'ctx'
        changed = cls in ('add', 'del')
        block_start = changed and not prev_changed
        row = {
            'cls': cls, 'text': line, 'block_start': block_start,
            'css': cls + (' block-start' if block_start else '')}
        if cls == 'file':
            file_index += 1
            row['file_index'] = file_index
        rows.append(row)
        prev_changed = changed
    return rows


def file_rows(diff_body: str, marked_paths: frozenset[str] = frozenset()) -> list[dict]:
    """The files-changed rows for the review page, largest change first.

    Sorted by total churn so a huge patch's bulk (e.g. a full autotools
    regeneration) tops the list and the small hand-written edits buried in it
    stand out at the bottom.  Each row keeps its 1-based position in DIFF order
    (``index``) so its link still targets the matching ``### <path>`` block
    anchor (``diff_lines``'s ``file_index``) after sorting.

    ``marked_paths`` is the fingerprint's generated-content mark (the context's
    ``generated_paths``); a row whose path is in it carries ``generated`` and the
    template tags it ``[gen]`` -- the same story the CLI's file list tells, and the
    reason the matching diff block below is collapsed.  The row keeps its anchor
    link either way: a marked file is tagged, never dropped.  The default empty set
    renders exactly what an unmarked fingerprint rendered before the mark existed.
    """
    stats = review_mod.diff_file_stats(diff_body)
    indexed = sorted(enumerate(stats, start=1),
                     key=lambda pair: (-(pair[1].added + pair[1].removed), pair[1].path))
    return [{'index': index, 'path': stat.path,
             'added': stat.added, 'removed': stat.removed,
             'generated': stat.path in marked_paths}
            for index, stat in indexed]


def generated_entries(mark: dict | None) -> dict[str, dict]:
    """``{path: evidence entry}`` from a ``generated-content`` mark's per-file list.

    The collapsed summary states what the mark says about THAT file -- which signals
    fired, and the generator/version where a banner carried one -- and only the mark's
    evidence list has it (the review context carries the badge and the path set, not the
    per-file entries).  ``{}`` for ``None``, the unmarked common case.

    An entry with no usable ``path`` is skipped rather than raised on, the same defensive
    posture ``generated_marks`` takes towards append-only operator data: one malformed
    entry costs one collapsed block, never the page.  First entry wins per path, matching
    ``generated.project_residue_first``.
    """
    entries: dict[str, dict] = {}
    for entry in (mark or {}).get('files') or ():
        if isinstance(entry, dict) and isinstance(entry.get('path'), str):
            entries.setdefault(entry['path'], entry)
    return entries


def _collapsed_segment(stat: dict, entry: dict, header: dict) -> dict:
    """One collapsed diff segment: the marked file's lines plus the facts its summary states.

    The summary is data, never markup -- path, the block's own +/- counts, the mark's
    signals and its generator/version -- rendered (and escaped) by the template.  The
    counts come from the diff being displayed rather than from the evidence, so the
    summary always describes the block it is hiding.

    The anchor for a marked file lives on the ``<details>`` element, so the ``### <path>``
    header line inside it drops its ``file_index`` (an id may not appear twice); see
    :func:`diff_segments`.
    """
    signals = entry.get('signals')
    return {
        'generated': True,
        'lines': [dict(header, file_index=None)],
        'file_index': stat['index'],
        'path': stat['path'],
        'added': stat['added'],
        'removed': stat['removed'],
        'signals': '+'.join(str(signal) for signal in signals) if isinstance(signals, (list, tuple)) else '',
        'generator': entry.get('generator') or '',
        'version': entry.get('version') or '',
    }


def diff_segments(rows: list[dict], files: list[dict], entries: dict[str, dict]) -> list[dict]:
    """Group ``diff_lines`` rows into render chunks, isolating the marked files' blocks.

    Returns ``{generated, lines, ...}`` chunks in RENDER order: every run of consecutive
    unmarked files (and any preamble) is one chunk, which the template renders as today's
    single ``<pre class="diff">``, and each file whose path carries a generated-content
    mark is a chunk of its own, which the template wraps in a collapsed ``<details>``.

    With nothing marked the whole diff is one unmarked chunk, so an unmarked fingerprint's
    page is byte-identical to the one this UI rendered before collapse existed -- the
    do-no-harm bar every phase of this work has held.

    ``files`` is :func:`file_rows`'s output (its 1-based diff-order ``index`` is
    ``diff_lines``'s ``file_index``, and it carries the path and counts the summary
    states); ``entries`` is :func:`generated_entries`.  A marked path this diff does not
    touch simply never matches, and collapses nothing.
    """
    by_index = {row['index']: row for row in files}
    segments: list[dict] = []
    collapsing = False
    for line in rows:
        index = line.get('file_index')
        if index is not None:
            stat = by_index.get(index)
            entry = entries.get(stat['path']) if stat is not None else None
            collapsing = entry is not None
            if collapsing:
                segments.append(_collapsed_segment(stat, entry, line))
                continue
        if collapsing:
            # Still inside a marked file's block: its hunks ride into the <details>.
            segments[-1]['lines'].append(line)
            continue
        if not segments or segments[-1]['generated']:
            segments.append({'generated': False, 'lines': []})
        segments[-1]['lines'].append(line)
    return segments


def construct_tally_row(context) -> dict | None:
    """The dangerous-construct split for the detail page, or ``None`` to render nothing.

    ``{total, in_marked, in_residue, residue_hits}`` from
    ``generated.construct_tally`` over the context's own diff body, so the page can say
    whether a marked patch's construct hits are all inside the generated-claiming files or
    whether one landed in the hand-written residue -- the single fact that decides how much
    of a 47k-line regeneration a reviewer has to worry about.

    ``None`` when the fingerprint carries no mark (there is no marked/residue split to
    make, and an unmarked page must render exactly as it did before) or when the body has
    no construct hits at all (a zero row is noise).  ``residue_hits`` is a compact
    ``'<detail> in <path>'`` list naming the loud case, for the summary's title text.

    ADVISORY: recomputed from this body at render time.  No ``dangerous-construct``
    observation is read or rewritten, and the page never presents this as the ledger's
    count.
    """
    if context.generated_detail is None:
        return None
    tally = generated_mod.construct_tally(context.diff_body, context.generated_paths)
    if not tally.total:
        return None
    return {'total': tally.total, 'in_marked': tally.in_marked, 'in_residue': tally.in_residue,
            'residue_hits': ', '.join('%s in %s' % (detail, path)
                                      for path, detail in tally.residue_hits)}


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
               signer=None, clock=None, port: int = DEFAULT_PORT):
    """Build the Flask app over an open ledger ``conn`` and the corpus/index.

    ``fetch``, ``signer`` and ``clock`` are injected exactly as the CLI injects
    them, so the handlers are pure given fakes and test offline through
    ``app.test_client()``.  ``signer`` is the ``record_bytes -> (signature,
    signed_by)`` callable used to sign a human verdict; when ``None`` the UI is
    read-only (no verdict form, no POST).  ``clock`` is the single clock read --
    a ``() -> ISO-8601 str`` -- captured once per POST and threaded into the
    signed record; it defaults to the CLI's UTC clock.

    ``port`` is the port the caller will bind, and is what the ``Host`` check
    validates against; it defaults to :data:`DEFAULT_PORT` so a test client (which
    sends a port-less ``Host: localhost``) needs no ceremony.
    """
    from flask import Flask, abort, g, redirect, render_template_string, request, url_for

    app = Flask('divergulent.review_web')
    # One token for the life of the process, handed to the templates as a Jinja
    # global so every form carries it without threading it through each render.
    csrf_token = secrets.token_urlsafe(32)
    app.config[CSRF_CONFIG_KEY] = csrf_token
    app.jinja_env.globals[CSRF_FIELD] = csrf_token

    @app.before_request
    def _guard():
        """Refuse anything that is not this UI talking to itself; see guard_request.

        A ``before_request`` hook rather than a per-route decorator so a route added
        later is protected by default -- the failure mode of the decorator (a new
        mutating route someone forgets to annotate) is the whole bug class this
        closes.  ``require_loopback`` guards the bind; this guards the request.
        """
        refusal = guard_request(
            method=request.method, host=request.headers.get('Host'),
            origin=request.headers.get('Origin'),
            cookie_token=request.cookies.get(CSRF_COOKIE),
            form_token=request.form.get(CSRF_FIELD),
            expected_token=csrf_token, port=port)
        if refusal is not None:
            return render_template_string(FORBIDDEN_TEMPLATE, reason=refusal, port=port), 403
        g.request_is_ours = True
        return None

    @app.after_request
    def _issue_csrf_cookie(response):
        """Plant the CSRF cookie on any ACCEPTED request that did not carry it.

        ``SameSite=Strict`` is the load-bearing attribute: a cross-site POST is sent
        without the cookie at all, so it cannot satisfy the double-submit check even
        if the token itself leaked.  ``HttpOnly`` keeps a scripted read out of it;
        the form field is the only place the page needs the value, and it is
        rendered server-side.  A refused request gets no cookie: handing the token
        to whatever host just failed the check would be handing it to the caller we
        just decided is not us.
        """
        if not g.get('request_is_ours'):
            return response
        if request.cookies.get(CSRF_COOKIE) != csrf_token:
            response.set_cookie(CSRF_COOKIE, csrf_token, path='/', httponly=True,
                                samesite='Strict')
        return response

    # Reviewer notes are an optional, additive table; backfill it so a ledger
    # built before notes existed gains it with no rebuild.
    ledger_mod.ensure_note_table(conn)
    clock = clock or review_mod._cli_now
    categories = review_mod._assignable_categories()
    valid_choices = set(categories) | {review_mod.CHOICE_ACCEPT, review_mod.CHOICE_DEFER}

    def _pending_item(fingerprint: str):
        """The pending queue row for ``fingerprint``, or ``None`` if not queued."""
        for item in ledger_mod.pending_review_items(conn):
            if item['fingerprint'] == fingerprint:
                return item
        return None

    def _worklist_row(item, level, risk_level, reach_level, note_count, injection_families,
                      generated_detail) -> dict:
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
            # The security-risk level (none if un-scored); shown as a badge.
            'risk': risk_level,
            # The install-base reach t-shirt size (none if un-ranked); a badge.
            'reach': reach_level,
            # The injection-tripwire families that fired (none if clean); a badge.
            # Its presence means the patch was NOT sent to the LLM -- routed to a human.
            'injection': injection_families,
            # The generated-content mark's detail ('autotools/99'), or None when nothing
            # claimed generation -- which renders no badge at all.  It rides in the SIZE
            # cell: the mark is that axis's explanation (a huge patch that is almost all
            # generator output), and a column of its own would be empty for nearly every
            # row.  A CLAIM, never a verdict: the badge says what the files say.
            'generated': generated_detail,
            # How many reviewer notes this fingerprint carries (0 -> no indicator).
            'notes': note_count,
        }

    def _worklist_reach_chips(reach_levels: dict) -> list[dict]:
        """Filter chips for the reach tiers present in the queue, in scale order.

        Reach is the install-base t-shirt size; the chips let a reviewer focus the
        widely-run patches (e.g. only ``XL``/``L``) within whatever other filters
        are active. Levels with no pending items are omitted.
        """
        counts: dict[str, int] = {}
        for item in ledger_mod.pending_review_items(conn):
            level = reach_levels.get(item['fingerprint'])
            if level:
                counts[level] = counts.get(level, 0) + 1
        return [{'name': name, 'count': counts[name]}
                for name in reach_mod.REACH_LEVELS if name in counts]

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
        # Reach filter: the install-base axis (e.g. only the widely-run XL/L
        # patches). Composes with every other filter.
        reach_levels = reach_mod.reach_by_fingerprint(conn)
        reach = request.args.get('reach', '').strip() or None
        if reach:
            items = [item for item in items if reach_levels.get(item['fingerprint']) == reach]
        # Order by the LIVE security-risk level first, then reach, then the stored
        # priority -- so a patch scored scary (or ranked widely-run) AFTER it was
        # queued surfaces immediately, even if its frozen stored priority has not
        # been re-stamped yet. Reach is a within-risk key (never crosses a tier).
        risk_levels = risk_mod.risk_level_by_fingerprint(conn)
        items = sorted(
            items,
            key=lambda it: (risk_mod.RISK_RANK.get(risk_levels.get(it['fingerprint']), 0),
                            reach_mod.REACH_RANK.get(reach_levels.get(it['fingerprint']), 0),
                            it['priority']),
            reverse=True)
        note_counts = ledger_mod.note_counts_by_fingerprint(conn)
        injection_families = injection_mod.injection_by_fingerprint(conn)
        # One mark read for the whole worklist, then a lookup per row -- the injection
        # dict pattern above; only the badge's detail string is needed here.
        generated_details = {digest: mark['detail']
                             for digest, mark in generated_mod.generated_marks(conn).items()}
        rows = [_worklist_row(item, levels.get(item['fingerprint'], 'normal'),
                              risk_levels.get(item['fingerprint']),
                              reach_levels.get(item['fingerprint']),
                              note_counts.get(item['fingerprint'], 0),
                              injection_families.get(item['fingerprint']),
                              generated_details.get(item['fingerprint'])) for item in items]
        top = items[0]['fingerprint'] if items else None
        # Two query strings so each filter row preserves the OTHER axes: the size
        # chips keep category/package/reach, the reach chips keep category/package/
        # reviewability (and each "all ..." link resets only its own axis).
        base_params = {}
        if category:
            base_params['category'] = category
        if package:
            base_params['package'] = package
        size_params = dict(base_params, **({'reach': reach} if reach else {}))
        reach_params = dict(base_params, **({'reviewability': reviewability} if reviewability else {}))
        return render_template_string(
            WORKLIST_TEMPLATE, rows=rows, category=category, package=package,
            categories=_worklist_category_chips(), top=top, total=len(items),
            reviewability=reviewability, reviewabilities=_worklist_reviewability_chips(levels),
            reach=reach, reaches=_worklist_reach_chips(reach_levels),
            base_qs=urlencode(size_params), reach_qs=urlencode(reach_params))

    def _diff_render_args(context) -> dict:
        """The files-changed / diff template args, shared by the review page and its
        error re-render so both tag and collapse identically.

        The context already carries the mark's badge and path set (they come from the same
        row); the per-file evidence the collapsed summaries state does not ride on the
        context, so the mark is read here for it.

        ``tally`` is the construct-vs-residue split (:func:`construct_tally_row`) --
        ``None`` for an unmarked page, so nothing new renders there.
        """
        entries = generated_entries(generated_mod.generated_marks(conn).get(context.fingerprint))
        files = file_rows(context.diff_body, context.generated_paths)
        segments = diff_segments(diff_lines(context.context_view), files, entries)
        return {
            'files': files,
            'segments': segments,
            'collapsed': sum(1 for segment in segments if segment['generated']),
            'generated': context.generated_detail,
            'tally': construct_tally_row(context),
        }

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
            risk=risk_mod.risk_level_by_fingerprint(conn).get(resolved),
            reach=reach_mod.reach_by_fingerprint(conn).get(resolved),
            provenance=xref_mod.provenance_by_fingerprint(conn).get(resolved),
            injection=injection_mod.injection_by_fingerprint(conn).get(resolved),
            oversized_lines=reviewability_mod.REVIEWABILITY_OVERSIZED_LINES,
            notes=ledger_mod.notes_for(conn, resolved), can_note=signer is not None,
            package_lines=review_mod._format_package_lines(context),
            **_diff_render_args(context))

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
                notes=ledger_mod.notes_for(conn, resolved), can_note=True,
                package_lines=review_mod._format_package_lines(context),
                error='pick a verdict: accept the draft, a category, or defer',
                **_diff_render_args(context)), 400

        # Capture the clock ONCE, server-side, so the signed record and the
        # decision share the timestamp -- exactly as the CLI threads _cli_now().
        now = clock()
        try:
            outcome = review_mod.record_review_verdict(
                conn, item, context, choice, signer=signer, now=now)
        except Exception as exc:  # noqa: BLE001 -- a signing/auth failure is a page, not a 500
            # Tell the reviewer it failed only if it REALLY did not land.  Signing
            # happens before any write, so a signer failure never reached the
            # ledger -- but a failure BETWEEN the decision insert and the queue
            # update would leave the signed row uncommitted on this long-lived
            # connection, where the next successful commit would flush it.
            # record_review_verdict rolls that back itself; this is the handler's
            # own guarantee, and it holds for anything else the try block grows.
            conn.rollback()
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
        try:
            review_mod.requeue_one(conn, resolved, now=now)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 -- a ledger failure is a page, not a 500
            # Same guarantee the verdict submission makes, for the same reason: the
            # re-queue's writes are uncommitted until the commit above, and this
            # connection lives for the life of the process.  requeue_one rolls its
            # own set back; this is the handler's own guarantee, and it holds for
            # anything else the try block grows.
            conn.rollback()
            return render_template_string(
                ERROR_TEMPLATE, fingerprint=resolved, error=str(exc)), 502
        verdict_mod.rebuild_current_verdict(conn)
        return redirect(url_for('audit'))

    @app.route('/note/<fingerprint>', methods=['POST'])
    def add_note(fingerprint):
        if signer is None:
            abort(405)  # read-only instance: notes are signed, so need a signer
        resolved, matches = review_mod.resolve_fingerprint(conn, fingerprint)
        if resolved is None:
            return render_template_string(
                SEARCH_TEMPLATE, query=fingerprint, matches=matches), 404
        body = request.form.get('body', '').strip()
        if not body:
            return redirect(url_for('review', fingerprint=resolved))  # empty -> no-op
        try:
            review_mod.record_note(conn, resolved, body, signer=signer, now=clock())
        except Exception as exc:  # noqa: BLE001 -- a signing/auth failure is a page, not a 500
            # record_note signs BEFORE it writes, so a SIGNER failure leaves the
            # ledger untouched -- but that reasoning covers only the signer. The
            # append itself commits, and under SQLite a deferred transaction takes
            # EXCLUSIVE only at commit time, so a busy database fails AFTER the
            # INSERT and leaves the signed note staged on this process-lifetime
            # connection for the next commit to flush. The reviewer would be shown
            # this page and the note would land anyway. Roll back, as the verdict
            # and re-queue handlers do.
            conn.rollback()
            return render_template_string(ERROR_TEMPLATE, fingerprint=resolved, error=str(exc)), 502
        return redirect(url_for('review', fingerprint=resolved))

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
                     signer=_lazy_sigstore_signer(), port=args.port)

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
 .risk { display: inline-block; padding: 0 0.4rem; border-radius: 0.2rem;
         font-size: 0.8rem; font-weight: bold; }
 .risk.high { background: #5a1212; color: #ff8a82; }
 .risk.elevated { background: #4a3212; color: #f0b860; }
 .risk.low { color: #8a909a; }
 .risk.none { color: #5a606a; }
 .reach { display: inline-block; padding: 0 0.4rem; border-radius: 0.2rem;
          font-size: 0.8rem; font-weight: bold; }
 .reach.XL { background: #123a3a; color: #6fe0d0; }
 .reach.L { background: #10302f; color: #5ac0b4; }
 .reach.M { color: #8a909a; }
 .reach.S { color: #6a707a; }
 .reach.XS { color: #5a606a; }
 .prov { display: inline-block; padding: 0 0.4rem; border-radius: 0.2rem;
         font-size: 0.8rem; font-weight: bold; }
 .prov.cve-confirmed { background: #12331d; color: #6fe08a; }
 .prov.claim-unconfirmed { background: #4a1c1c; color: #ff9a92; }
 .inj { display: inline-block; padding: 0 0.4rem; border-radius: 0.2rem;
        font-size: 0.8rem; font-weight: bold; background: #3a1147; color: #e29aff; }
 .gen { display: inline-block; padding: 0 0.4rem; border-radius: 0.2rem;
        font-size: 0.8rem; font-weight: bold; background: #1b2b3a; color: #7fb0d8; }
 /* A generated-claiming file's diff block: collapsed by default, expandable, and
    summarised loudly -- presentation only, nothing is dropped from the page. */
 details.gen-seg { border: 1px solid #2a2f38; border-radius: 0.3rem; margin: 0.4rem 0;
                   background: #14171b; }
 details.gen-seg > summary { cursor: pointer; padding: 0.35rem 0.6rem; color: #b0b6c0;
                             font: 12px/1.4 ui-monospace, monospace; }
 details.gen-seg > pre.diff { border: 0; border-top: 1px solid #2a2f38; margin: 0; }
 /* A dangerous-construct hit in the hand-written residue: the attention-worthy case,
    so it is loud where an all-generated tally is merely muted reassurance. */
 .residue-hit { color: #ff7b72; font-weight: bold; }
 .inj-block { background: #1e1428; border-left: 3px solid #a855f7;
              padding: 0.5rem 0.8rem; border-radius: 0.3rem; margin: 0.6rem 0; }
 .next { display: inline-block; margin: 0.5rem 0; padding: 0.4rem 0.8rem;
         background: #2563eb; color: #fff; border-radius: 0.3rem; text-decoration: none; }
 .meta-block { background: #232730; padding: 0.6rem 0.8rem; border-radius: 0.3rem; }
 .claim-block { background: #1e2128; border-left: 3px solid #b8860b;
                padding: 0.5rem 0.8rem; border-radius: 0.3rem; margin: 0.6rem 0; }
 .claim-desc { white-space: pre-wrap; margin: 0.3rem 0; color: #e0e3e8; }
 .notes .note { background: #1b1f26; border-left: 3px solid #3a4150;
                padding: 0.4rem 0.6rem; margin: 0.4rem 0; border-radius: 0.2rem; }
 .note-body { white-space: pre-wrap; color: #e0e3e8; }
 .notes details summary { cursor: pointer; }
 pre.sig { white-space: pre-wrap; word-break: break-all; max-height: 12rem; overflow: auto;
           background: #0f1115; padding: 0.4rem; font-size: 11px; color: #8a909a; }
 textarea { width: 100%; background: #232730; color: #ccd0d6; border: 1px solid #3a4150;
            border-radius: 0.3rem; padding: 0.4rem; font: inherit; box-sizing: border-box; }
 .note-badge { font-size: 0.85rem; color: #b0b6c0; }
 pre.diff { background: #0f1115; border: 1px solid #2a2f38; padding: 0.6rem;
            overflow-x: auto; font: 12px/1.4 ui-monospace, monospace; }
 pre.diff span { display: block; min-width: 100%; width: fit-content; min-height: 1.4em; }
 pre.diff .add { color: #5fd17a; } pre.diff .del { color: #ff7b72; }
 pre.diff .hunk { color: #9aa0aa; background: #232730; } pre.diff .meta { color: #6b7280; }
 pre.diff .file { color: #e0e3e8; background: #232730; font-weight: bold; }
 table.files td { padding: 0.1rem 0.6rem; font: 12px/1.4 ui-monospace, monospace; }
 table.files td.n { text-align: right; }
 table.files .add, h2 .add { color: #5fd17a; }
 table.files .del, h2 .del { color: #ff7b72; }
 pre.diff .block-current { box-shadow: inset 3px 0 0 #6cb6ff; }
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
{% if reaches or reach %}
<p>
  <span class="muted">reach:</span>
  <a class="chip {{ 'on' if not reach }}" href="/{{ '?' + reach_qs if reach_qs }}">all reach</a>
  {% for r in reaches %}
    <a class="chip {{ 'on' if reach == r.name }}"
       href="/?reach={{ r.name }}{{ '&' + reach_qs if reach_qs }}"
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
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA'
      || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === 'j' || e.key === 'n') {
    var a = document.querySelector('a.next');
    if (a) location.href = a.getAttribute('href');
  }
});
</script>
<table>
  <tr><th>risk</th><th>reach</th><th>draft</th><th>size</th><th>pkgs</th><th>fingerprint</th><th>reason</th></tr>
  {% for row in rows %}
  <tr>
    <td>{% if row.risk %}<span class="risk {{ row.risk }}">{{ row.risk }}</span>{% else %}
        <span class="muted">-</span>{% endif %}</td>
    <td>{% if row.reach %}<span class="reach {{ row.reach }}">{{ row.reach }}</span>{% else %}
        <span class="muted">-</span>{% endif %}</td>
    <td>{{ row.draft_category or '-' }}</td>
    <td>{% if row.reviewability %}<span class="rev {{ row.reviewability }}"
        >{{ row.reviewability }}</span>{% endif %}{% if row.generated %}
        <span class="gen"
              title="files claiming to be generator output; collapsed on the review page"
              >{{ row.generated }}</span>{% endif %}</td>
    <td>{{ row.n_packages }}</td>
    <td class="mono"><a href="/review/{{ row.fingerprint }}">{{ row.short }}</a>{% if row.injection %}
        <span class="inj"
              title="injection-suspect ({{ row.injection }}); not sent to the LLM">&#9888;</span>{% endif %}{%
        if row.notes %}
        <span class="note-badge" title="{{ row.notes }} note(s)">&#128221;{{ row.notes }}</span>{% endif %}</td>
    <td class="muted">{{ row.reason or '' }}</td>
  </tr>
  {% endfor %}
</table>
''' + _FOOT

REVIEW_TEMPLATE = _HEAD.replace('{{ title }}', 'review') + '''{#
  One diff line -> one span, shared by the plain blocks and the collapsed ones so both
  render the SAME markup; a line inside a collapsed block carries no file_index, because
  its anchor moved to the <details> wrapping it.
#}{% macro spans(lines) %}{% for line in lines %}<span{% if line.file_index
  %} id="file-{{ line.file_index }}"{% endif
  %} class="{{ line.css }}">{{ line.text }}</span>{% endfor %}{% endmacro %}
<p id="top"><a href="/">&larr; worklist</a></p>
<h1 class="mono">{{ ctx.fingerprint[:16] }}<span class="muted">{{ ctx.fingerprint[16:] }}</span>
{% if risk %} <span class="risk {{ risk }}">risk: {{ risk }}</span>{% endif %}
{% if reach %} <span class="reach {{ reach }}">reach: {{ reach }}</span>{% endif %}
{% if provenance %} <span class="prov {{ provenance }}">{{ provenance }}</span>{% endif %}
{% if injection %} <span class="inj" title="not sent to the LLM">&#9888; injection-suspect</span>{% endif %}
{% if reviewability %} <span class="rev {{ reviewability }}">{{ reviewability }}</span>{% endif %}{%
  if generated %} <span class="gen"
  title="files claiming to be generator output; collapsed in the diff below, never hidden"
  >claims generated: {{ generated }}</span>{% endif %}</h1>
{% if injection %}
<p class="inj-block">This patch tripped the prompt-injection tripwire
(<b>{{ injection }}</b>): its text carries injection-shaped content aimed at the classifier, so it was
<b>not sent to the LLM</b> and routed here for a human. This is a tripwire, not a proof of malice --
read the diff and judge it on its merits.</p>
{% endif %}
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
    &middot; last updated: <b>{{ ctx.claim_date or 'no date in header' }}</b>
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
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <button type="submit">Re-queue for human review</button>
  <span class="muted">supersedes the current verdict; records no decision</span>
</form>
{% endif %}
{% if queued and can_verdict %}
<h2 id="verdict">Your verdict</h2>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post" action="/review/{{ ctx.fingerprint }}">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
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
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA'
      || e.metaKey || e.ctrlKey || e.altKey) return;
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
<h2 id="notes">Notes</h2>
<div class="notes">
  {% for note in notes %}
    <div class="note">
      <div class="note-body">{{ note.body }}</div>
      <div class="muted">&mdash; <b>{{ note.signed_by or '(unsigned)' }}</b> at {{ note.created_at }}
        <details><summary>signature</summary><pre class="sig">{{ note.signature }}</pre></details>
      </div>
    </div>
  {% else %}
    <p class="muted">No notes yet.</p>
  {% endfor %}
  {% if can_note %}
    <form method="post" action="/note/{{ ctx.fingerprint }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <textarea name="body" rows="2"
        placeholder="Leave a signed note (e.g. unsafe sprintf() near a privilege boundary)..."></textarea>
      <button type="submit">Add note &amp; sign</button>
    </form>
  {% else %}
    <p class="muted">(read-only instance: notes need a signer)</p>
  {% endif %}
</div>
{% if files %}
<h2>Files changed ({{ files | length }},
  <span class="add">+{{ files | sum(attribute='added') }}</span>
  <span class="del">-{{ files | sum(attribute='removed') }}</span>)</h2>
<table class="files">
  {% for f in files %}
  <tr>
    <td class="n add">+{{ f.added }}</td>
    <td class="n del">-{{ f.removed }}</td>
    <td><a href="#file-{{ f.index }}">{{ f.path }}</a>{% if f.generated %} <span class="gen"
        title="claims to be generator output; its block below is collapsed">[gen]</span>{% endif %}</td>
  </tr>
  {% endfor %}
</table>
{% endif %}
<h2>Diff in upstream context</h2>
{% if collapsed %}<p class="muted">{{ collapsed }} generated-claiming file(s) below are
collapsed &mdash; click a summary to expand one. Nothing is hidden: every collapsed block is
on this page in full, and a mark says only what a file claims about itself.</p>
{% endif %}{% if tally %}<p class="muted">dangerous constructs in this body:
{{ tally.total }} total &mdash; {{ tally.in_marked }} in generated-claiming files,
{% if tally.in_residue %}<span class="residue-hit"
  title="{{ tally.residue_hits }}">{{ tally.in_residue }} in the residue</span>{%
  else %}0 in the residue{% endif %}. Recomputed from this diff at display time (not the
ledger's recorded observation count, which may differ); a construct claiming to be
generated is still a construct.</p>
{% endif %}{% for seg in segments %}{% if seg.generated %}<details class="gen-seg"
  id="file-{{ seg.file_index }}"><summary>{{ seg.path }}
  <span class="add">+{{ seg.added }}</span> <span class="del">-{{ seg.removed }}</span>
  &middot; claims generated{% if seg.signals %} ({{ seg.signals }}){% endif %}{%
  if seg.generator %} &middot; {{ seg.generator }}{% if seg.version %} {{ seg.version }}{%
  endif %}{% endif %}</summary><pre class="diff">{{ spans(seg.lines) }}</pre></details>{%
  else %}<pre class="diff">{{ spans(seg.lines) }}</pre>{% endif %}{% endfor %}
<p class="muted">diff: <span class="key">[</span> previous change &middot;
  <span class="key">]</span> next change
  {% if queued and can_verdict %}&middot; <span class="key">v</span> jump to verdict
  (the <span class="key">1</span>-<span class="key">9</span>/<span class="key">a</span>/<span
  class="key">d</span> + <span class="key">Enter</span> verdict keys also work from here)
  &middot; <a href="#verdict">enter verdict &uarr;</a>{% endif %}
  &middot; <a href="#top">back to top</a></p>
<script>
(function () {
  var blocks = Array.prototype.slice.call(document.querySelectorAll('pre.diff .block-start'));
  var idx = -1;
  function jump(delta) {
    if (!blocks.length) return;
    idx = Math.max(0, Math.min(blocks.length - 1, (idx < 0 ? 0 : idx + delta)));
    blocks.forEach(function (b) { b.classList.remove('block-current'); });
    blocks[idx].classList.add('block-current');
    blocks[idx].scrollIntoView({block: 'center'});
  }
  document.addEventListener('keydown', function (e) {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA'
      || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === ']') { e.preventDefault(); jump(1); }
    else if (e.key === '[') { e.preventDefault(); jump(-1); }
    else if (e.key === 'v') {
      var f = document.querySelector('fieldset.verdict');
      if (f) { e.preventDefault(); f.scrollIntoView({block: 'center'}); }
    }
  });
})();
</script>
''' + _FOOT

FORBIDDEN_TEMPLATE = _HEAD.replace('{{ title }}', 'refused') + '''
<h1>Request refused</h1>
<p>This request did not come from the review UI running on this machine, so it was
<b>not</b> carried out -- nothing was written to the ledger.</p>
<pre class="diff error">{{ reason }}</pre>
<p class="muted">A human verdict is signed with your Sigstore identity and outranks
every rule and every LLM verdict, so it is only ever recorded from a form this UI
rendered. Open <span class="mono">http://127.0.0.1:{{ port }}/</span> and try again
from the page itself.</p>
''' + _FOOT

ERROR_TEMPLATE = _HEAD.replace('{{ title }}', 'error') + '''
<p><a href="/">&larr; worklist</a></p>
<h1>Could not record the verdict</h1>
<p>The verdict for <span class="mono">{{ fingerprint[:16] }}</span> was NOT recorded
-- the ledger is unchanged. Signing happens before any write, and a failure part-way
through the write is rolled back, so there is nothing half-recorded to clean up.</p>
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
