"""Tests for divergulent.classify.triage -- the injectable, claim-blind LLM tier.

All tests are OFFLINE: the LLM ``call`` boundary is injected as a fake that
returns a canned string, so the suite never touches the network. Coverage:
claim-blindness (the DEP-3 header never reaches the prompt), ``diff_body``
header-stripping, JSON parsing through code fences and surrounding prose,
out-of-enum coercion to ``unknown``, the ``LlmVerdict`` carrying its raw
response / model / prompt_version, model pass-through, and the absent-extra
error path for ``anthropic_call`` (skipped if the SDK happens to be installed).
"""
import json
import importlib

import testtools

import subprocess
from unittest import mock

from divergulent.classify.triage import (
    DEFAULT_MODEL, LlmVerdict, PROMPT_VERSION, TRIAGE_CATEGORIES,
    anthropic_call, build_prompt, claude_cli_call, diff_body, triage)


# ---------------------------------------------------------------------------
# Fixtures -- a patch WITH a DEP-3 header whose description must never leak.
# ---------------------------------------------------------------------------

_DESCRIPTION = 'This patch fixes a remote heap overflow exploited via CVE-2024-9999.'
_SUBJECT = 'Add brand-new turbo-encabulator feature support'

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


def _patch_with_subject(subject=_SUBJECT, diff=_DIFF):
    return f'Subject: {subject}\nForwarded: yes\n\n' + diff


def _fake_call(response, *, recorder=None):
    """A fake ``call`` returning a canned ``response``; records (prompt, model)."""
    def call(prompt, *, model):
        if recorder is not None:
            recorder.append((prompt, model))
        return response
    return call


def _json_response(category='bugfix', confidence='high', reasoning='Enlarges a stack buffer.'):
    return json.dumps({'category': category, 'confidence': confidence, 'reasoning': reasoning})


# ---------------------------------------------------------------------------
# diff_body -- strips the author-controlled header
# ---------------------------------------------------------------------------

class DiffBodyTestCase(testtools.TestCase):

    def test_strips_dep3_description_header(self):
        body = diff_body(_patch_with_description())
        self.assertNotIn(_DESCRIPTION, body)
        self.assertNotIn('Forwarded:', body)
        self.assertNotIn('Author:', body)

    def test_keeps_the_readable_diff(self):
        body = diff_body(_patch_with_description())
        # Real diff retained verbatim (not the normalised hash form): paths,
        # hunk header, and change lines all present.
        self.assertTrue(body.startswith('--- a/src/parser.c'))
        self.assertIn('@@ -10,7 +10,7 @@', body)
        self.assertIn('+    char buf[64];', body)

    def test_no_diff_body_returns_empty(self):
        self.assertEqual('', diff_body('Description: just a header, no diff\n'))

    def test_empty_input_returns_empty(self):
        self.assertEqual('', diff_body(''))


# ---------------------------------------------------------------------------
# build_prompt -- claim-blindness and determinism
# ---------------------------------------------------------------------------

class BuildPromptTestCase(testtools.TestCase):

    def test_prompt_is_blind_to_the_description(self):
        # The loudest guarantee: a patch WITH a description must produce a
        # prompt that contains the diff but NOT the author's claim.
        body = diff_body(_patch_with_description())
        prompt = build_prompt(body)
        self.assertNotIn(_DESCRIPTION, prompt)
        self.assertNotIn('CVE-2024-9999', prompt)
        self.assertIn('+    char buf[64];', prompt)

    def test_prompt_is_blind_to_the_subject(self):
        body = diff_body(_patch_with_subject())
        prompt = build_prompt(body)
        self.assertNotIn(_SUBJECT, prompt)
        self.assertNotIn('turbo-encabulator', prompt)

    def test_prompt_lists_every_category(self):
        prompt = build_prompt(diff_body(_patch_with_description()))
        for category in TRIAGE_CATEGORIES:
            self.assertIn(category, prompt)

    def test_prompt_is_deterministic(self):
        body = diff_body(_patch_with_description())
        self.assertEqual(build_prompt(body), build_prompt(body))
        self.assertNotEqual(
            build_prompt(body, prompt_version=1),
            build_prompt(body, prompt_version=2))


# ---------------------------------------------------------------------------
# triage -- JSON parsing variants
# ---------------------------------------------------------------------------

class TriageParsingTestCase(testtools.TestCase):

    def test_plain_json_response(self):
        call = _fake_call(_json_response(category='bugfix'))
        verdict = triage(_patch_with_description(), call=call)
        self.assertEqual('bugfix', verdict.category)
        self.assertEqual('high', verdict.confidence)

    def test_json_in_code_fence(self):
        fenced = '```json\n' + _json_response(category='security', confidence='medium') + '\n```'
        verdict = triage(_patch_with_description(), call=_fake_call(fenced))
        self.assertEqual('security', verdict.category)
        self.assertEqual('medium', verdict.confidence)

    def test_json_with_surrounding_prose(self):
        wrapped = (
            'Sure, here is my classification of the diff:\n\n'
            + _json_response(category='documentation', confidence='low')
            + '\n\nLet me know if you need anything else.')
        verdict = triage(_patch_with_description(), call=_fake_call(wrapped))
        self.assertEqual('documentation', verdict.category)
        self.assertEqual('low', verdict.confidence)

    def test_unparseable_response_degrades_to_unknown(self):
        verdict = triage(_patch_with_description(), call=_fake_call('no json here at all'))
        self.assertEqual('unknown', verdict.category)
        self.assertEqual('low', verdict.confidence)

    def test_unknown_confidence_degrades_to_low(self):
        call = _fake_call(_json_response(category='bugfix', confidence='certain'))
        verdict = triage(_patch_with_description(), call=call)
        self.assertEqual('low', verdict.confidence)


# ---------------------------------------------------------------------------
# triage -- out-of-enum coercion
# ---------------------------------------------------------------------------

class TriageEnumTestCase(testtools.TestCase):

    def test_out_of_enum_category_coerced_to_unknown(self):
        call = _fake_call(_json_response(category='refactor', confidence='high'))
        verdict = triage(_patch_with_description(), call=call)
        self.assertEqual('unknown', verdict.category)
        # The coercion is noted so the ledger evidence explains the unknown.
        self.assertIn('refactor', verdict.reasoning)

    def test_every_valid_category_passes_through(self):
        for category in TRIAGE_CATEGORIES:
            call = _fake_call(_json_response(category=category))
            verdict = triage(_patch_with_description(), call=call)
            self.assertEqual(category, verdict.category)


# ---------------------------------------------------------------------------
# triage -- the verdict carries auditable evidence and the injected model
# ---------------------------------------------------------------------------

class TriageVerdictTestCase(testtools.TestCase):

    def test_verdict_carries_raw_response(self):
        raw = _json_response(category='feature', reasoning='Adds a new option.')
        verdict = triage(_patch_with_description(), call=_fake_call(raw))
        self.assertIsInstance(verdict, LlmVerdict)
        self.assertEqual(raw, verdict.raw_response)
        self.assertEqual('Adds a new option.', verdict.reasoning)

    def test_verdict_carries_model_and_prompt_version(self):
        call = _fake_call(_json_response())
        verdict = triage(_patch_with_description(), call=call, model='claude-opus-4-8')
        self.assertEqual('claude-opus-4-8', verdict.model)
        self.assertEqual(PROMPT_VERSION, verdict.prompt_version)

    def test_default_model_used_when_unspecified(self):
        verdict = triage(_patch_with_description(), call=_fake_call(_json_response()))
        self.assertEqual(DEFAULT_MODEL, verdict.model)

    def test_model_passed_through_to_call(self):
        recorder = []
        call = _fake_call(_json_response(), recorder=recorder)
        triage(_patch_with_description(), call=call, model='claude-opus-4-8')
        self.assertEqual(1, len(recorder))
        _prompt, model = recorder[0]
        self.assertEqual('claude-opus-4-8', model)

    def test_call_receives_claim_blind_prompt(self):
        # End-to-end: the prompt the boundary actually sends must not contain
        # the author's description.
        recorder = []
        call = _fake_call(_json_response(), recorder=recorder)
        triage(_patch_with_description(), call=call)
        prompt, _model = recorder[0]
        self.assertNotIn(_DESCRIPTION, prompt)
        self.assertIn('char buf[64]', prompt)


# ---------------------------------------------------------------------------
# anthropic_call -- absent-extra path (offline; never hits the network)
# ---------------------------------------------------------------------------

class AnthropicCallTestCase(testtools.TestCase):

    def test_clear_error_when_sdk_absent(self):
        try:
            importlib.import_module('anthropic')
        except ImportError:
            pass
        else:
            self.skipTest('anthropic SDK is installed; absent-extra path not exercised')

        exc = self.assertRaises(
            RuntimeError, anthropic_call, 'prompt', model=DEFAULT_MODEL)
        self.assertIn('divergulent[triage]', str(exc))


# ---------------------------------------------------------------------------
# claude_cli_call -- the default subprocess backend (mocked; never spawns claude)
# ---------------------------------------------------------------------------

class ClaudeCliCallTestCase(testtools.TestCase):

    def test_invokes_claude_print_mode_with_prompt_on_stdin(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"category": "bugfix"}', stderr='')
        with mock.patch('divergulent.classify.triage.subprocess.run',
                        return_value=completed) as run:
            out = claude_cli_call('PROMPT-TEXT', model='claude-sonnet-4-6')
        self.assertEqual('{"category": "bugfix"}', out)
        args, kwargs = run.call_args
        self.assertEqual(['claude', '-p', '--model', 'claude-sonnet-4-6',
                          '--output-format', 'text'], args[0])
        self.assertEqual('PROMPT-TEXT', kwargs['input'])

    def test_missing_claude_cli_raises_clear_error(self):
        with mock.patch('divergulent.classify.triage.subprocess.run',
                        side_effect=FileNotFoundError):
            exc = self.assertRaises(RuntimeError, claude_cli_call, 'p', model=DEFAULT_MODEL)
        self.assertIn('claude', str(exc))

    def test_nonzero_exit_raises_with_stderr(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout='', stderr='boom')
        with mock.patch('divergulent.classify.triage.subprocess.run',
                        return_value=completed):
            exc = self.assertRaises(RuntimeError, claude_cli_call, 'p', model=DEFAULT_MODEL)
        self.assertIn('boom', str(exc))

    def test_triage_through_the_claude_backend(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_json_response(category='feature'), stderr='')
        with mock.patch('divergulent.classify.triage.subprocess.run', return_value=completed):
            verdict = triage(_patch_with_description(), call=claude_cli_call)
        self.assertEqual('feature', verdict.category)


class ClaudeCliErrorDetailTestCase(testtools.TestCase):
    """claude writes its failure reason (auth, usage limit) to stdout, so the
    error must surface stdout + the command, not just an exit code."""

    def test_nonzero_exit_surfaces_stdout_and_command(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout='Usage limit reached. Try again later.', stderr='')
        with mock.patch('divergulent.classify.triage.subprocess.run', return_value=completed):
            exc = self.assertRaises(RuntimeError, claude_cli_call, 'p', model=DEFAULT_MODEL)
        self.assertIn('Usage limit reached', str(exc))
        self.assertIn('claude -p', str(exc))

    def test_empty_response_on_zero_exit_raises(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='   \n', stderr='')
        with mock.patch('divergulent.classify.triage.subprocess.run', return_value=completed):
            exc = self.assertRaises(RuntimeError, claude_cli_call, 'p', model=DEFAULT_MODEL)
        self.assertIn('empty response', str(exc))
