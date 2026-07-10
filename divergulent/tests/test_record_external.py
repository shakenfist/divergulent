"""Tests for the phase-6 external CVE pass in record.record_to_ledger (E3).

All offline. A small synthetic corpus (bodies + a phase-1 index) is laid down with
patches that CLAIM CVEs, a pinned Security Tracker snapshot is written by hand, and
``record_to_ledger`` is run with ``security_tracker_path`` set. The cases exercise
the pass's contract: settle ``security`` for a code-touching confirmed CVE over the
unknown residue, DEFER on a settled content category, only FLAG a contradiction,
stay idempotent, re-verify when stale, and retract when the tracker no longer
supports the claim.
"""
import os
import sqlite3
import tempfile

import testtools

from divergulent.classify import bts
from divergulent.classify import cross_reference as xref_mod
from divergulent.classify import ledger as ledger_mod
from divergulent.classify import record
from divergulent.classify import security_tracker
from divergulent.classify import verdict as verdict_mod
from divergulent.classify.corpus import body_sha256
from divergulent.classify.fingerprint import fingerprint


WHEN = '2026-06-14T00:00:00Z'
LATER = '2026-06-15T00:00:00Z'
MUCH_LATER = '2026-08-01T00:00:00Z'  # past the +30d freshness horizon of a 2026-06-14 snapshot


# A substantive .c edit whose header cites a real CVE for its source (openssl):
# confirmed + code-touching + unknown residue -> settle security.
SECURITY_FIX = (
    'Description: fix a buffer overflow (CVE-2021-3999)\n'
    '--- a/src/net.c\n'
    '+++ b/src/net.c\n'
    '@@ -10,3 +10,4 @@\n'
    ' int parse(char *buf) {\n'
    '-    strcpy(dst, buf);\n'
    '+    if (len < N)\n'
    '+        strcpy(dst, buf);\n'
    ' }\n'
)

# A doc-only change that merely MENTIONS the same CVE: confirmed, but content is
# high-confidence documentation -> defer (no security decision), still annotated.
DOC_MENTIONS_CVE = (
    'Description: note the CVE-2021-3999 fix in the manpage\n'
    '--- a/doc/tool.1\n'
    '+++ b/doc/tool.1\n'
    '@@ -1,3 +1,3 @@\n'
    ' .TH TOOL 1\n'
    '-old wording\n'
    '+new wording (CVE-2021-3999)\n'
    ' .SH NAME\n'
)

# A substantive edit claiming a CVE that is NOT in the tracker at all -> contradicted.
INVENTED_CVE = (
    'Description: security hardening for CVE-2099-0000\n'
    '--- a/src/auth.c\n'
    '+++ b/src/auth.c\n'
    '@@ -1,2 +1,3 @@\n'
    ' void check(void) {\n'
    '+    harden();\n'
    ' }\n'
)


def _fp(text):
    return fingerprint(text)[1]


def _write_bodies(corpus_dir, bodies):
    for text in bodies.values():
        sha = body_sha256(text)
        directory = os.path.join(corpus_dir, 'bodies', sha[:2])
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, sha), 'w', encoding='utf-8') as handle:
            handle.write(text)


def _build_corpus(corpus_dir):
    """Lay down bodies + a phase-1 index; return the index path."""
    bodies = {
        'fix.patch': SECURITY_FIX,
        'doc.patch': DOC_MENTIONS_CVE,
        'invented.patch': INVENTED_CVE,
    }
    _write_bodies(corpus_dir, bodies)
    # source_package is the FIRST row seen per fingerprint (the representative).
    rows = [
        ('openssl', 'fix.patch', SECURITY_FIX),
        ('openssl', 'doc.patch', DOC_MENTIONS_CVE),
        ('coreutils', 'invented.patch', INVENTED_CVE),
    ]
    index_path = os.path.join(corpus_dir, 'fingerprints.sqlite')
    connection = sqlite3.connect(index_path)
    try:
        connection.execute(
            'CREATE TABLE patch ('
            'source_package TEXT NOT NULL, version TEXT NOT NULL, '
            'patch_name TEXT NOT NULL, raw_sha256 TEXT NOT NULL, '
            'normalisation_version INTEGER NOT NULL, fingerprint TEXT NOT NULL)')
        connection.executemany(
            'INSERT INTO patch (source_package, version, patch_name, raw_sha256, '
            'normalisation_version, fingerprint) VALUES (?, ?, ?, ?, ?, ?)',
            [(pkg, '1-1', name, body_sha256(text), 1, _fp(text)) for pkg, name, text in rows])
        connection.commit()
    finally:
        connection.close()
    return index_path


def _write_tracker(corpus_dir, rows, *, date='2026-06-14'):
    path = security_tracker.default_security_tracker_path(corpus_dir)
    security_tracker.write_snapshot(path, rows, snapshot_date=date, source_url='x', release='trixie')
    return path


CONFIRMING = [('openssl', 'CVE-2021-3999', 'resolved', '3.0.0-1')]


class ExternalPassTestCase(testtools.TestCase):

    def _run(self, *, tracker_rows=CONFIRMING, now=WHEN, tracker_date='2026-06-14', reuse=None):
        if reuse is not None:
            tmp, index_path, ledger_path = reuse
        else:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            index_path = _build_corpus(tmp.name)
            ledger_path = os.path.join(tmp.name, 'ledger.sqlite')
        tracker_path = _write_tracker(tmp.name, tracker_rows, date=tracker_date) if tracker_rows is not None else None
        conn = ledger_mod.create_ledger(ledger_path) if reuse is None else ledger_mod.open_ledger(ledger_path)
        stats = record.record_to_ledger(
            conn, tmp.name, index_path, now=now, reconcile=reuse is not None,
            security_tracker_path=tracker_path)
        return conn, stats, (tmp, index_path, ledger_path)

    def _live_categories(self, conn, fingerprint):
        return sorted(row['category'] for row in ledger_mod.live_decisions(conn)
                      if row['fingerprint'] == fingerprint)

    def _provenance(self, conn, fingerprint):
        for row in ledger_mod.live_observations(conn):
            if row['fingerprint'] == fingerprint and row['kind'] == xref_mod.PROVENANCE_KIND:
                return row
        return None

    def test_confirmed_code_touch_settles_security(self):
        conn, stats, _ = self._run()
        fp = _fp(SECURITY_FIX)
        # The external security decision coexists with the content 'unknown' one...
        self.assertEqual(['security', 'unknown'], self._live_categories(conn, fp))
        # ...and wins the current verdict (heuristic tie broken by confidence/id).
        self.assertEqual('security', verdict_mod.current_verdict(conn)[fp].category)
        self.assertEqual(1, stats.external_decisions_appended)
        # The security decision carries the input snapshot + freshness horizon.
        row = ledger_mod.live_decision_for_rule(
            conn, fingerprint=fp, decided_by=xref_mod.EXTERNAL_CVE_RULE_ID,
            rule_version=xref_mod.EXTERNAL_CVE_VERSION)
        self.assertIn('CVE-2021-3999', row['input_snapshot'])
        self.assertEqual('2026-07-14', row['input_fresh_until'])
        # And a confirmed provenance observation rides alongside.
        self.assertEqual(xref_mod.DETAIL_CVE_CONFIRMED, self._provenance(conn, fp)['detail'])

    def test_confirmed_but_documentation_defers(self):
        conn, _, _ = self._run()
        fp = _fp(DOC_MENTIONS_CVE)
        # High-confidence documentation is NOT overruled: no security decision.
        self.assertEqual(['documentation'], self._live_categories(conn, fp))
        self.assertEqual('documentation', verdict_mod.current_verdict(conn)[fp].category)
        # But the corroboration is still recorded as provenance.
        self.assertEqual(xref_mod.DETAIL_CVE_CONFIRMED, self._provenance(conn, fp)['detail'])

    def test_contradiction_only_flags_never_categorises(self):
        conn, _, _ = self._run()
        fp = _fp(INVENTED_CVE)
        self.assertEqual(['unknown'], self._live_categories(conn, fp))
        prov = self._provenance(conn, fp)
        self.assertEqual(xref_mod.DETAIL_CLAIM_UNCONFIRMED, prov['detail'])
        self.assertIn('not-found', prov['evidence'])

    def test_second_run_is_idempotent(self):
        conn, _, reuse = self._run()
        conn.close()
        conn2, stats2, _ = self._run(reuse=reuse)
        self.assertEqual(0, stats2.external_decisions_appended)
        self.assertEqual(0, stats2.external_decisions_superseded)
        self.assertEqual(0, stats2.external_obs_appended)
        self.assertEqual(1, stats2.external_decisions_skipped)  # the security decision

    def test_stale_verdict_is_reverified(self):
        conn, _, reuse = self._run()
        conn.close()
        # Same confirming snapshot, but 'now' is past the freshness horizon.
        conn2, stats2, _ = self._run(reuse=reuse, now=MUCH_LATER)
        self.assertEqual(1, stats2.external_decisions_superseded)
        self.assertEqual(1, stats2.external_decisions_appended)

    def test_retracts_when_tracker_no_longer_confirms(self):
        conn, _, reuse = self._run()
        conn.close()
        # Re-run against a snapshot that no longer records the CVE for openssl.
        conn2, stats2, _ = self._run(
            reuse=reuse, tracker_rows=[('nginx', 'CVE-2000-0001', 'open', None)], now=LATER)
        fp = _fp(SECURITY_FIX)
        self.assertEqual(1, stats2.external_decisions_superseded)
        self.assertEqual(['unknown'], self._live_categories(conn2, fp))
        # Provenance flips confirmed -> unconfirmed.
        self.assertEqual(xref_mod.DETAIL_CLAIM_UNCONFIRMED, self._provenance(conn2, fp)['detail'])

    def test_no_snapshot_records_no_external_rows(self):
        conn, stats, _ = self._run(tracker_rows=None)
        self.assertEqual(0, stats.external_decisions_appended)
        self.assertEqual(0, stats.external_obs_appended)
        # No provenance observations at all.
        self.assertIsNone(self._provenance(conn, _fp(SECURITY_FIX)))


# A substantive edit that cites a Debian bug (no CVE) -- the BTS-only path.
BUG_FIX = (
    'Description: correct an off-by-one\n'
    'Bug-Debian: https://bugs.debian.org/123456\n'
    '--- a/src/parse.c\n'
    '+++ b/src/parse.c\n'
    '@@ -1,2 +1,3 @@\n'
    ' void f(void) {\n'
    '+    fix();\n'
    ' }\n'
)


class BtsPassTestCase(testtools.TestCase):

    def _run(self, *, bug_rows):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sha = body_sha256(BUG_FIX)
        directory = os.path.join(tmp.name, 'bodies', sha[:2])
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, sha), 'w', encoding='utf-8') as handle:
            handle.write(BUG_FIX)
        index_path = os.path.join(tmp.name, 'fingerprints.sqlite')
        connection = sqlite3.connect(index_path)
        connection.execute(
            'CREATE TABLE patch (source_package TEXT NOT NULL, version TEXT NOT NULL, '
            'patch_name TEXT NOT NULL, raw_sha256 TEXT NOT NULL, '
            'normalisation_version INTEGER NOT NULL, fingerprint TEXT NOT NULL)')
        connection.execute(
            'INSERT INTO patch VALUES (?, ?, ?, ?, ?, ?)',
            ('coreutils', '1-1', 'bug.patch', sha, 1, _fp(BUG_FIX)))
        connection.commit()
        connection.close()
        bts_path = bts.default_bts_path(tmp.name)
        bts.write_snapshot(bts_path, bug_rows, snapshot_date='2026-07-10', source_url='x')
        conn = ledger_mod.create_ledger(os.path.join(tmp.name, 'ledger.sqlite'))
        self.addCleanup(conn.close)
        record.record_to_ledger(conn, tmp.name, index_path, now=WHEN, bts_path=bts_path)
        return conn

    def _provenance(self, conn, fingerprint):
        for row in ledger_mod.live_observations(conn):
            if row['fingerprint'] == fingerprint and row['kind'] == xref_mod.PROVENANCE_KIND:
                return row
        return None

    def test_bug_confirmed_records_provenance_only(self):
        conn = self._run(bug_rows=[(123456, 'coreutils', 'done')])
        fp = _fp(BUG_FIX)
        self.assertEqual(xref_mod.DETAIL_BUG_CONFIRMED, self._provenance(conn, fp)['detail'])
        # A bug never settles a category: only the content 'unknown' decision is live.
        cats = [row['category'] for row in ledger_mod.live_decisions(conn) if row['fingerprint'] == fp]
        self.assertEqual(['unknown'], cats)

    def test_unknown_bug_is_flagged_unconfirmed(self):
        conn = self._run(bug_rows=[(999999, 'nginx', 'pending')])
        self.assertEqual(xref_mod.DETAIL_CLAIM_UNCONFIRMED,
                         self._provenance(conn, _fp(BUG_FIX))['detail'])
