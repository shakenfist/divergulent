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
    DEFAULT_MODEL, CallResult, LlmVerdict, PROMPT_VERSION, TRIAGE_CATEGORIES, Usage,
    anthropic_call, claude_cli_call, diff_body, triage, triage_system_prompt,
    triage_user_message)


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


def _fake_call(response, *, recorder=None, usage=None):
    """A fake ``call`` returning a canned ``response``; records (system, user, model)."""
    def call(system, user, *, model):
        if recorder is not None:
            recorder.append((system, user, model))
        return CallResult(text=response, usage=usage or Usage())
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
# Prompt split -- cacheable system rubric + variable user diff
# ---------------------------------------------------------------------------

class TriagePromptTestCase(testtools.TestCase):

    def test_user_message_is_blind_to_the_description(self):
        # The loudest guarantee: a patch WITH a description must produce a user
        # message that contains the diff but NOT the author's claim.
        body = diff_body(_patch_with_description())
        user = triage_user_message(body)
        self.assertNotIn(_DESCRIPTION, user)
        self.assertNotIn('CVE-2024-9999', user)
        self.assertIn('+    char buf[64];', user)

    def test_user_message_is_blind_to_the_subject(self):
        body = diff_body(_patch_with_subject())
        user = triage_user_message(body)
        self.assertNotIn(_SUBJECT, user)
        self.assertNotIn('turbo-encabulator', user)

    def test_system_prompt_holds_the_rubric_and_no_diff(self):
        # The cacheable prefix lists every category and carries NO per-patch diff,
        # so it is constant across patches (the cache key).
        system = triage_system_prompt()
        for category in TRIAGE_CATEGORIES:
            self.assertIn(category, system)
        self.assertNotIn('char buf[64]', system)

    def test_system_prompt_is_constant_across_patches_and_versioned(self):
        # Constant for a fixed version (no diff in it) -> a stable cache prefix.
        self.assertEqual(triage_system_prompt(), triage_system_prompt())
        self.assertNotEqual(
            triage_system_prompt(prompt_version=1),
            triage_system_prompt(prompt_version=2))

    def test_relocation_preserves_the_flat_prompt_content(self):
        # The rubric content is moved verbatim: system + the original separator +
        # the user message reproduce the prior single flat prompt byte-for-byte.
        body = diff_body(_patch_with_description())
        flat = triage_system_prompt() + '\n' + triage_user_message(body)
        self.assertIn('grounded in the diff.\n\nDiff body:\n\n', flat)
        self.assertTrue(flat.rstrip().endswith('}'))  # ends with the diff's last line


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
        _system, _user, model = recorder[0]
        self.assertEqual('claude-opus-4-8', model)

    def test_call_receives_claim_blind_prompt(self):
        # End-to-end: nothing the boundary actually sends (system or user) may
        # contain the author's description; the diff rides in the user message.
        recorder = []
        call = _fake_call(_json_response(), recorder=recorder)
        triage(_patch_with_description(), call=call)
        system, user, _model = recorder[0]
        self.assertNotIn(_DESCRIPTION, system + user)
        self.assertIn('char buf[64]', user)

    def test_verdict_carries_the_call_usage(self):
        usage = Usage(input_tokens=120, output_tokens=18, cache_read_tokens=900)
        verdict = triage(_patch_with_description(), call=_fake_call(_json_response(), usage=usage))
        self.assertEqual(usage, verdict.usage)


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
            RuntimeError, anthropic_call, 'system', 'user', model=DEFAULT_MODEL)
        self.assertIn('divergulent[triage]', str(exc))

    def test_caches_the_system_rubric_and_parses_usage(self):
        try:
            importlib.import_module('anthropic')
        except ImportError:
            self.skipTest('anthropic SDK not installed; the metered path is unavailable')

        fake_usage = mock.Mock(
            input_tokens=12, output_tokens=8,
            cache_creation_input_tokens=500, cache_read_input_tokens=4000)
        fake_response = mock.Mock(
            content=[mock.Mock(type='text', text='{"category": "bugfix"}')], usage=fake_usage)
        fake_client = mock.Mock()
        fake_client.messages.create.return_value = fake_response

        with mock.patch('anthropic.Anthropic', return_value=fake_client):
            out = anthropic_call('RUBRIC', 'DIFF', model='claude-sonnet-4-6')

        self.assertEqual('{"category": "bugfix"}', out.text)
        self.assertEqual(4000, out.usage.cache_read_tokens)
        self.assertEqual(500, out.usage.cache_creation_tokens)
        # The rubric went to a CACHED system block; the diff to the user message.
        _args, kwargs = fake_client.messages.create.call_args
        self.assertEqual('RUBRIC', kwargs['system'][0]['text'])
        self.assertEqual('ephemeral', kwargs['system'][0]['cache_control']['type'])
        self.assertEqual('DIFF', kwargs['messages'][0]['content'])


# ---------------------------------------------------------------------------
# claude_cli_call -- the default subprocess backend (mocked; never spawns claude)
# ---------------------------------------------------------------------------

def _claude_json(result_text, **usage):
    """The `claude -p --output-format json` stdout shape, with a usage block."""
    return json.dumps({
        'type': 'result', 'subtype': 'success', 'is_error': False,
        'result': result_text,
        'total_cost_usd': usage.pop('total_cost_usd', 0.0123),
        'usage': {
            'input_tokens': usage.get('input_tokens', 11),
            'output_tokens': usage.get('output_tokens', 22),
            'cache_creation_input_tokens': usage.get('cache_creation_input_tokens', 0),
            'cache_read_input_tokens': usage.get('cache_read_input_tokens', 0),
        },
    })


class ClaudeCliCallTestCase(testtools.TestCase):

    def test_sends_rubric_as_system_prompt_and_diff_on_stdin(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_claude_json('{"category": "bugfix"}'), stderr='')
        with mock.patch('divergulent.classify.triage.subprocess.run',
                        return_value=completed) as run:
            out = claude_cli_call('RUBRIC', 'DIFF-TEXT', model='claude-sonnet-4-6')
        self.assertEqual('{"category": "bugfix"}', out.text)
        args, kwargs = run.call_args
        self.assertEqual(['claude', '-p', '--model', 'claude-sonnet-4-6',
                          '--system-prompt', 'RUBRIC', '--output-format', 'json'], args[0])
        self.assertEqual('DIFF-TEXT', kwargs['input'])  # the diff, not the rubric

    def test_parses_usage_and_cost_from_the_json(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=_claude_json('{"category": "bugfix"}', input_tokens=9,
                                output_tokens=52, cache_read_input_tokens=17283,
                                cache_creation_input_tokens=9384, total_cost_usd=0.0207),
            stderr='')
        with mock.patch('divergulent.classify.triage.subprocess.run', return_value=completed):
            out = claude_cli_call('RUBRIC', 'DIFF', model=DEFAULT_MODEL)
        self.assertEqual(9, out.usage.input_tokens)
        self.assertEqual(52, out.usage.output_tokens)
        self.assertEqual(17283, out.usage.cache_read_tokens)
        self.assertEqual(9384, out.usage.cache_creation_tokens)
        self.assertEqual(0.0207, out.usage.cost_usd)

    def test_non_json_stdout_on_zero_exit_raises(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='not json at all', stderr='')
        with mock.patch('divergulent.classify.triage.subprocess.run', return_value=completed):
            exc = self.assertRaises(
                RuntimeError, claude_cli_call, 'RUBRIC', 'DIFF', model=DEFAULT_MODEL)
        self.assertIn('parseable JSON', str(exc))

    def test_missing_claude_cli_raises_clear_error(self):
        with mock.patch('divergulent.classify.triage.subprocess.run',
                        side_effect=FileNotFoundError):
            exc = self.assertRaises(
                RuntimeError, claude_cli_call, 'sys', 'p', model=DEFAULT_MODEL)
        self.assertIn('claude', str(exc))

    def test_nonzero_exit_raises_with_stderr(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout='', stderr='boom')
        with mock.patch('divergulent.classify.triage.subprocess.run',
                        return_value=completed):
            exc = self.assertRaises(
                RuntimeError, claude_cli_call, 'sys', 'p', model=DEFAULT_MODEL)
        self.assertIn('boom', str(exc))

    def test_triage_through_the_claude_backend(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_claude_json(_json_response(category='feature')),
            stderr='')
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
            exc = self.assertRaises(
                RuntimeError, claude_cli_call, 'sys', 'p', model=DEFAULT_MODEL)
        self.assertIn('Usage limit reached', str(exc))
        self.assertIn('claude -p', str(exc))

    def test_empty_response_on_zero_exit_raises(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='   \n', stderr='')
        with mock.patch('divergulent.classify.triage.subprocess.run', return_value=completed):
            exc = self.assertRaises(
                RuntimeError, claude_cli_call, 'sys', 'p', model=DEFAULT_MODEL)
        self.assertIn('empty response', str(exc))
