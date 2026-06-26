"""Tests for divergulent.classify.risk -- the claim-blind security-risk gate.

All OFFLINE: the LLM ``call`` boundary is injected as a fake returning canned
JSON, so no real claude -p / network. Coverage: the gate is claim-blind (the
DEP-3 description never reaches the prompt), parses the coarse level, degrades
unparseable/out-of-scale responses to ``elevated`` (recall-safe, never buried),
carries (model, prompt_version) + usage, and records a supersedable
``security-risk`` observation that re-scoring replaces (exactly one live).
"""
import json
import os
import tempfile

import testtools

from divergulent.classify import ledger as ledger_mod
from divergulent.classify import risk
from divergulent.classify import triage as triage_mod

WHEN = '2026-06-26T00:00:00Z'
LATER = '2026-06-27T00:00:00Z'

_DESCRIPTION = 'Trivial cleanup, totally harmless.'  # the author claim that must not leak
_DIFF = (
    '--- a/src/parser.c\n'
    '+++ b/src/parser.c\n'
    '@@ -10,7 +10,7 @@\n'
    ' int parse(const char *s) {\n'
    '-    char buf[8];\n'
    '+    char buf[64];\n'
    '     strcpy(buf, s);\n'
    ' }\n')


def _patch():
    return 'Description: %s\nForwarded: no\n\n%s' % (_DESCRIPTION, _DIFF)


def _fake_call(text, *, usage=None, recorder=None):
    def call(system, user, *, model):
        if recorder is not None:
            recorder.append((system, user, model))
        return triage_mod.CallResult(text=text, usage=usage or triage_mod.Usage())
    return call


def _risk_json(risk_level='elevated', reason='Touches a fixed buffer near strcpy.'):
    return json.dumps({'risk': risk_level, 'reason': reason})


class ScoreRiskTestCase(testtools.TestCase):

    def test_parses_level_rank_and_reason(self):
        score = risk.score_risk(_patch(), call=_fake_call(_risk_json('high', 'plausible overflow')))
        self.assertEqual('high', score.level)
        self.assertEqual(risk.RISK_RANK['high'], score.rank)
        self.assertEqual('plausible overflow', score.reason)

    def test_is_claim_blind(self):
        recorder = []
        risk.score_risk(_patch(), call=_fake_call(_risk_json(), recorder=recorder))
        system, user, _model = recorder[0]
        self.assertNotIn(_DESCRIPTION, system + user)   # the author claim never leaks
        self.assertIn('char buf[64]', user)             # the diff rides in the user message

    def test_carries_model_prompt_version_and_usage(self):
        usage = triage_mod.Usage(input_tokens=600, output_tokens=40)
        score = risk.score_risk(
            _patch(), call=_fake_call(_risk_json(), usage=usage), model='claude-sonnet-4-6')
        self.assertEqual('claude-sonnet-4-6', score.model)
        self.assertEqual(risk.RISK_PROMPT_VERSION, score.prompt_version)
        self.assertEqual(usage, score.usage)

    def test_default_model_is_the_bakeoff_pick(self):
        score = risk.score_risk(_patch(), call=_fake_call(_risk_json()))
        self.assertEqual(risk.DEFAULT_RISK_MODEL, score.model)

    def test_out_of_scale_level_degrades_to_elevated(self):
        # A model returning 'critical' (out of the 4-level scale) must not be
        # silently dropped -- it routes for review.
        score = risk.score_risk(_patch(), call=_fake_call(_risk_json('critical')))
        self.assertEqual('elevated', score.level)
        self.assertIn('out-of-scale', score.reason)

    def test_unparseable_response_degrades_to_elevated(self):
        score = risk.score_risk(_patch(), call=_fake_call('no json at all'))
        self.assertEqual('elevated', score.level)  # recall-safe: never buried


class RecordRiskObservationTestCase(testtools.TestCase):

    def _ledger(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = ledger_mod.create_ledger(os.path.join(tmp.name, 'l.sqlite'))
        self.addCleanup(conn.close)
        return conn

    def _score(self, level='elevated', model='claude-opus-4-8'):
        return risk.RiskScore(level=level, rank=risk.RISK_RANK[level], reason='r',
                              model=model, prompt_version=risk.RISK_PROMPT_VERSION,
                              raw_response=_risk_json(level))

    def test_records_observation_with_provenance(self):
        conn = self._ledger()
        risk.record_risk_observation(conn, 'fp1', self._score('high'), now=WHEN)
        obs = [o for o in ledger_mod.live_observations(conn) if o['kind'] == risk.RISK_KIND]
        self.assertEqual(1, len(obs))
        self.assertEqual('high', obs[0]['detail'])
        self.assertEqual('risk-gate:claude-opus-4-8', obs[0]['observed_by'])
        self.assertEqual(risk.RISK_PROMPT_VERSION, obs[0]['rule_version'])
        self.assertEqual('high', json.loads(obs[0]['evidence'])['level'])

    def test_rescore_supersedes_the_prior_live_observation(self):
        conn = self._ledger()
        risk.record_risk_observation(conn, 'fp1', self._score('low'), now=WHEN)
        # Re-score (even from a different model) leaves exactly ONE live row.
        risk.record_risk_observation(conn, 'fp1', self._score('high', model='claude-sonnet-4-6'),
                                     now=LATER)
        live = [o for o in ledger_mod.live_observations(conn)
                if o['kind'] == risk.RISK_KIND and o['fingerprint'] == 'fp1']
        self.assertEqual(1, len(live))
        self.assertEqual('high', live[0]['detail'])
        # The superseded original is still in the audit trail.
        self.assertEqual(2, len([o for o in ledger_mod.observations_for(conn, 'fp1')
                                 if o['kind'] == risk.RISK_KIND]))

    def test_risk_rank_by_fingerprint(self):
        conn = self._ledger()
        risk.record_risk_observation(conn, 'fp-high', self._score('high'), now=WHEN)
        risk.record_risk_observation(conn, 'fp-none', self._score('none'), now=WHEN)
        ranks = risk.risk_rank_by_fingerprint(conn)
        self.assertEqual(risk.RISK_RANK['high'], ranks['fp-high'])
        self.assertEqual(risk.RISK_RANK['none'], ranks['fp-none'])
        self.assertNotIn('fp-unscored', ranks)


def _diff(old, new, a='src/x.c', b=None):
    b = b or a
    return '--- a/%s\n+++ b/%s\n@@ -1 +1 @@\n-%s\n+%s\n' % (a, b, old, new)


class ProvablyBenignTestCase(testtools.TestCase):

    def test_documentation_only_is_culled(self):
        self.assertIsNotNone(risk.provably_benign(_diff('old text', 'new text', a='doc/guide.md')))

    def test_whitespace_only_is_culled(self):
        # Re-indentation of a code line -- no behaviour change, safe to cull.
        ws = ('--- a/src/x.c\n+++ b/src/x.c\n@@ -1,2 +1,2 @@\n'
              ' int main(){\n-return 0;\n+  return 0;\n')
        self.assertIsNotNone(risk.provably_benign(ws))

    def test_translation_is_culled(self):
        self.assertIsNotNone(risk.provably_benign(
            _diff('msgstr "a"', 'msgstr "b"', a='po/de.po')))

    def test_changelog_is_culled(self):
        self.assertIsNotNone(risk.provably_benign(
            _diff('old entry', 'new entry', a='debian/changelog')))

    def test_real_code_change_is_not_culled(self):
        self.assertIsNone(risk.provably_benign(_diff('do_thing();', 'do_other();', a='src/x.c')))

    def test_debian_rules_hardening_change_is_not_culled(self):
        # The security-critical case: a build-flag change must reach the gate.
        self.assertIsNone(risk.provably_benign(
            _diff('CFLAGS = -O2', 'CFLAGS = -O2 -fstack-protector', a='debian/rules')))


class RunRiskGateTestCase(testtools.TestCase):

    def _setup(self):
        from divergulent.tests.test_triage_driver import _build_corpus, _seed_ledger
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        index_path, fingerprints = _build_corpus(tmp.name)
        conn = ledger_mod.create_ledger(os.path.join(tmp.name, 'ledger.sqlite'))
        self.addCleanup(conn.close)
        _seed_ledger(conn, fingerprints)
        return conn, tmp.name, index_path, fingerprints

    def test_culls_the_doc_patch_and_scores_the_code_patches(self):
        conn, corpus_dir, index_path, fingerprints = self._setup()
        stats = risk.run_risk_gate(
            conn, corpus_dir, index_path, call=_fake_call(_risk_json('elevated')),
            now=WHEN, limit=10, model='claude-sonnet-4-6')
        # The doc-only patch is culled (deterministic none); the others scored.
        self.assertGreaterEqual(stats.culled, 1)
        self.assertGreaterEqual(stats.scored, 1)
        # The doc fingerprint carries a culled 'none' from the deterministic source.
        doc = [o for o in ledger_mod.observations_for(conn, fingerprints['doc.patch'])
               if o['kind'] == risk.RISK_KIND][-1]
        self.assertEqual('none', doc['detail'])
        self.assertEqual(risk.RISK_CULL_OBSERVED_BY, doc['observed_by'])
        # A code fingerprint carries an LLM 'elevated' from the gate.
        bug = [o for o in ledger_mod.observations_for(conn, fingerprints['bug-a.patch'])
               if o['kind'] == risk.RISK_KIND][-1]
        self.assertEqual('elevated', bug['detail'])
        self.assertTrue(bug['observed_by'].startswith(risk.RISK_OBSERVED_BY_PREFIX))

    def test_rerun_skips_already_scored(self):
        conn, corpus_dir, index_path, _ = self._setup()
        risk.run_risk_gate(conn, corpus_dir, index_path, call=_fake_call(_risk_json()),
                           now=WHEN, limit=10)
        again = risk.run_risk_gate(conn, corpus_dir, index_path, call=_fake_call(_risk_json()),
                                   now=LATER, limit=10)
        self.assertEqual(0, again.scored + again.culled)  # nothing left to score
