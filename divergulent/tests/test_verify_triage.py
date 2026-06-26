"""Tests for the adversarial verifier and routing in divergulent.classify.triage.

All tests are OFFLINE: the LLM ``call`` boundary is injected as a fake that
returns canned JSON, so the suite never touches the network. The fake routes on
a substring of the prompt -- the verify prompt is uniquely identifiable -- so a
single ``call`` can answer the triage draft and the verification differently.

Coverage: the verify prompt is claim-blind and states the proposed category;
``agrees=False`` is the safe default for a garbled verifier answer; and the full
routing matrix (agree+high+matching claim -> verified; refute -> needs_human;
low confidence on either side -> needs_human; claim mismatch -> needs_human;
dangerous-construct even when verified -> needs_human; garbled verify ->
needs_human).
"""
import json

import testtools

from divergulent.classify.triage import (
    CallResult, DEFAULT_MODEL, TriageResult, Usage, VERIFY_PROMPT_VERSION,
    Verification, diff_body, triage_and_verify, verify, verify_system_prompt,
    verify_user_message)


# ---------------------------------------------------------------------------
# Fixtures -- a patch WITH a DEP-3 header whose description must never leak.
# ---------------------------------------------------------------------------

_DESCRIPTION = 'This patch fixes a remote heap overflow exploited via CVE-2024-9999.'

_DIFF = (
    '--- a/src/parser.c\n'
    '+++ b/src/parser.c\n'
    '@@ -10,7 +10,7 @@\n'
    ' int parse(const char *s) {\n'
    '-    char buf[8];\n'
    '+    char buf[64];\n'
    '     strcpy(buf, s);\n'
    '     return 0;\n'
    ' }\n'
)


def _patch_with_description(description=_DESCRIPTION, diff=_DIFF):
    return (
        f'Description: {description}\n'
        'Forwarded: no\n'
        'Author: Some Maintainer <maint@example.org>\n'
        '\n'
        + diff)


def _triage_json(category='bugfix', confidence='high', reasoning='Enlarges a stack buffer.'):
    return json.dumps({'category': category, 'confidence': confidence, 'reasoning': reasoning})


def _verify_json(agrees=True, confidence='high', reasoning='The diff supports the category.'):
    return json.dumps({'agrees': agrees, 'confidence': confidence, 'reasoning': reasoning})


# The verify prompt carries this phrase; the triage prompt does not. The fake
# ``call`` routes on it to answer draft vs verification differently.
_VERIFY_MARKER = 'adversarial reviewer'


def _routing_call(*, triage_response, verify_response, recorder=None):
    """A fake ``call`` that returns different JSON for the triage vs verify prompt.

    Routes on the verify-prompt marker substring (in the SYSTEM rubric) so one
    injected ``call`` serves both passes of ``triage_and_verify``. Records
    (system, user, model) when asked.
    """
    def call(system, user, *, model, schema=None):
        if recorder is not None:
            recorder.append((system, user, model))
        if _VERIFY_MARKER in system:
            return CallResult(text=verify_response)
        return CallResult(text=triage_response)
    return call


# ---------------------------------------------------------------------------
# Verify prompt split -- cacheable adversarial rubric + variable category/diff
# ---------------------------------------------------------------------------

class VerifyPromptTestCase(testtools.TestCase):

    def test_user_message_is_blind_to_the_description(self):
        body = diff_body(_patch_with_description())
        user = verify_user_message(body, 'bugfix')
        self.assertNotIn(_DESCRIPTION, user)
        self.assertNotIn('CVE-2024-9999', user)
        self.assertIn('+    char buf[64];', user)

    def test_user_message_states_the_proposed_category(self):
        # The category varies per patch, so it is PINNED in the user message; the
        # cached system rubric stays generic (it only lists the enum). That the
        # system does not vary by category is proven by the constancy test below.
        user = verify_user_message(diff_body(_patch_with_description()), 'security')
        self.assertIn('Proposed category: security', user)
        self.assertNotIn('Proposed category:', verify_system_prompt())

    def test_system_prompt_is_adversarial_and_constant(self):
        system = verify_system_prompt()
        self.assertIn('REFUTE', system)
        self.assertIn(_VERIFY_MARKER, system)
        self.assertEqual(verify_system_prompt(), verify_system_prompt())  # cache-stable
        self.assertNotEqual(
            verify_system_prompt(prompt_version=1),
            verify_system_prompt(prompt_version=2))

    def test_different_proposed_categories_differ_in_the_user_message(self):
        body = diff_body(_patch_with_description())
        self.assertNotEqual(
            verify_user_message(body, 'bugfix'),
            verify_user_message(body, 'security'))


# ---------------------------------------------------------------------------
# verify -- parsing and the safe default
# ---------------------------------------------------------------------------

class VerifyTestCase(testtools.TestCase):

    def _fake_call(self, response, *, recorder=None):
        def call(system, user, *, model, schema=None):
            if recorder is not None:
                recorder.append((system, user, model))
            return CallResult(text=response)
        return call

    def test_agrees_true_high(self):
        v = verify(_patch_with_description(), 'bugfix',
                   call=self._fake_call(_verify_json(agrees=True, confidence='high')))
        self.assertIsInstance(v, Verification)
        self.assertTrue(v.agrees)
        self.assertEqual('high', v.confidence)

    def test_agrees_false(self):
        v = verify(_patch_with_description(), 'bugfix',
                   call=self._fake_call(_verify_json(agrees=False)))
        self.assertFalse(v.agrees)

    def test_garbled_response_degrades_to_unverified(self):
        v = verify(_patch_with_description(), 'bugfix',
                   call=self._fake_call('no json here at all'))
        self.assertFalse(v.agrees)
        self.assertEqual('low', v.confidence)

    def test_missing_agrees_field_degrades_to_unverified(self):
        v = verify(_patch_with_description(), 'bugfix',
                   call=self._fake_call(json.dumps({'confidence': 'high'})))
        self.assertFalse(v.agrees)
        self.assertEqual('low', v.confidence)

    def test_non_boolean_agrees_degrades_to_unverified(self):
        v = verify(_patch_with_description(), 'bugfix',
                   call=self._fake_call(json.dumps({'agrees': 'yes', 'confidence': 'high'})))
        self.assertFalse(v.agrees)

    def test_carries_raw_response_model_and_prompt_version(self):
        raw = _verify_json()
        v = verify(_patch_with_description(), 'bugfix',
                   call=self._fake_call(raw), model='claude-opus-4-8')
        self.assertEqual(raw, v.raw_response)
        self.assertEqual('claude-opus-4-8', v.model)
        self.assertEqual(VERIFY_PROMPT_VERSION, v.prompt_version)

    def test_default_model_used_when_unspecified(self):
        v = verify(_patch_with_description(), 'bugfix', call=self._fake_call(_verify_json()))
        self.assertEqual(DEFAULT_MODEL, v.model)

    def test_verify_call_receives_claim_blind_prompt(self):
        recorder = []
        verify(_patch_with_description(), 'bugfix',
               call=self._fake_call(_verify_json(), recorder=recorder))
        system, user, _model = recorder[0]
        self.assertNotIn(_DESCRIPTION, system + user)
        self.assertIn('char buf[64]', user)

    def test_verification_carries_the_call_usage(self):
        def call(system, user, *, model, schema=None):
            return CallResult(text=_verify_json(), usage=Usage(input_tokens=80, output_tokens=12))
        v = verify(_patch_with_description(), 'bugfix', call=call)
        self.assertEqual(80, v.usage.input_tokens)


# ---------------------------------------------------------------------------
# triage_and_verify -- the routing matrix
# ---------------------------------------------------------------------------

class TriageAndVerifyTestCase(testtools.TestCase):

    def test_fake_call_distinguishes_triage_from_verify(self):
        # Sanity: the two passes get different prompts and the fake answers each.
        recorder = []
        call = _routing_call(
            triage_response=_triage_json(category='bugfix'),
            verify_response=_verify_json(agrees=True),
            recorder=recorder)
        triage_and_verify(_patch_with_description(), call=call)
        self.assertEqual(2, len(recorder))
        triage_system, _tu, _ = recorder[0]
        verify_system, _vu, _ = recorder[1]
        self.assertNotIn(_VERIFY_MARKER, triage_system)
        self.assertIn(_VERIFY_MARKER, verify_system)

    def test_agree_high_matching_claim_no_flag_verified(self):
        call = _routing_call(
            triage_response=_triage_json(category='bugfix', confidence='high'),
            verify_response=_verify_json(agrees=True, confidence='high'))
        result = triage_and_verify(_patch_with_description(), call=call,
                                   claim_category='bugfix', has_dangerous_construct=False)
        self.assertIsInstance(result, TriageResult)
        self.assertEqual('verified', result.routing)
        self.assertEqual('bugfix', result.draft.category)
        self.assertTrue(result.verification.agrees)

    def test_no_claim_given_still_verifies(self):
        call = _routing_call(
            triage_response=_triage_json(category='bugfix', confidence='high'),
            verify_response=_verify_json(agrees=True, confidence='high'))
        result = triage_and_verify(_patch_with_description(), call=call)
        self.assertEqual('verified', result.routing)

    def test_unknown_claim_does_not_count_as_mismatch(self):
        call = _routing_call(
            triage_response=_triage_json(category='bugfix', confidence='high'),
            verify_response=_verify_json(agrees=True, confidence='high'))
        result = triage_and_verify(_patch_with_description(), call=call,
                                   claim_category='unknown')
        self.assertEqual('verified', result.routing)

    def test_refute_routes_to_needs_human(self):
        call = _routing_call(
            triage_response=_triage_json(category='bugfix', confidence='high'),
            verify_response=_verify_json(agrees=False, confidence='high'))
        result = triage_and_verify(_patch_with_description(), call=call,
                                   claim_category='bugfix')
        self.assertEqual('needs_human', result.routing)
        self.assertIn('refuted', result.reason)

    def test_low_draft_confidence_routes_to_needs_human(self):
        call = _routing_call(
            triage_response=_triage_json(category='bugfix', confidence='low'),
            verify_response=_verify_json(agrees=True, confidence='high'))
        result = triage_and_verify(_patch_with_description(), call=call,
                                   claim_category='bugfix')
        self.assertEqual('needs_human', result.routing)
        self.assertIn('draft confidence is low', result.reason)

    def test_low_verify_confidence_routes_to_needs_human(self):
        call = _routing_call(
            triage_response=_triage_json(category='bugfix', confidence='high'),
            verify_response=_verify_json(agrees=True, confidence='low'))
        result = triage_and_verify(_patch_with_description(), call=call,
                                   claim_category='bugfix')
        self.assertEqual('needs_human', result.routing)
        self.assertIn('verification confidence is low', result.reason)

    def test_claim_mismatch_routes_to_needs_human(self):
        call = _routing_call(
            triage_response=_triage_json(category='bugfix', confidence='high'),
            verify_response=_verify_json(agrees=True, confidence='high'))
        result = triage_and_verify(_patch_with_description(), call=call,
                                   claim_category='documentation')
        self.assertEqual('needs_human', result.routing)
        self.assertIn('claim/content mismatch', result.reason)

    def test_dangerous_construct_routes_to_needs_human_even_when_verified(self):
        call = _routing_call(
            triage_response=_triage_json(category='security', confidence='high'),
            verify_response=_verify_json(agrees=True, confidence='high'))
        result = triage_and_verify(_patch_with_description(), call=call,
                                   claim_category='security', has_dangerous_construct=True)
        self.assertEqual('needs_human', result.routing)
        self.assertIn('dangerous-construct', result.reason)

    def test_garbled_verify_response_safe_default_needs_human(self):
        call = _routing_call(
            triage_response=_triage_json(category='bugfix', confidence='high'),
            verify_response='the diff looks fine to me, no json here')
        result = triage_and_verify(_patch_with_description(), call=call,
                                   claim_category='bugfix')
        self.assertFalse(result.verification.agrees)
        self.assertEqual('needs_human', result.routing)

    def test_multiple_reasons_all_recorded(self):
        call = _routing_call(
            triage_response=_triage_json(category='bugfix', confidence='low'),
            verify_response=_verify_json(agrees=False, confidence='high'))
        result = triage_and_verify(_patch_with_description(), call=call,
                                   claim_category='documentation', has_dangerous_construct=True)
        self.assertEqual('needs_human', result.routing)
        self.assertIn('refuted', result.reason)
        self.assertIn('draft confidence is low', result.reason)
        self.assertIn('claim/content mismatch', result.reason)
        self.assertIn('dangerous-construct', result.reason)
