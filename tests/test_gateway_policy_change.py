import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch

import importlib.util

_spec = importlib.util.spec_from_file_location("gateway_policy_change", Path(__file__).resolve().parents[1] / "scripts" / "gateway_policy_change.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def base():
    return {'providers': [{'name': 'local', 'type': 'mock', 'models': ['cheap', 'expensive'], 'latency': 0}],
            'token_auth': {'enabled': True, 'issuer': 'local-issuer', 'audience': 'local-gateway',
                           'scope_models': {'llm:complete': ['cheap'], 'llm:premium': ['expensive']}}}


def cases():
    return [{'name': 'cheap', 'scope': ['llm:complete'], 'model': 'cheap', 'expect_status': 200},
            {'name': 'expensive', 'scope': ['llm:complete'], 'model': 'expensive', 'expect_status': 403},
            {'name': 'missing-required', 'scope': ['llm:premium'], 'model': 'expensive', 'expect_status': 403},
            {'name': 'unknown-model', 'scope': ['llm:complete'], 'model': 'absent', 'expect_status': 404}]


class NativeWorkflow(unittest.TestCase):
    def run_compare(self, before, after, supplied):
        return asyncio.run(gate.compare(before, after, supplied))

    def test_identical_native_route_auth_and_execution(self):
        result = self.run_compare(base(), base(), cases())
        self.assertEqual(result['state'], 'pass')
        self.assertEqual(result['candidate_matched'], 4)
        self.assertTrue(result['cases'][0]['after']['provider_executed'])
        self.assertFalse(result['cases'][1]['after']['provider_executed'])

    def test_mapping_widening_requires_review_even_if_expected(self):
        candidate = base(); candidate['token_auth']['scope_models']['llm:complete'].append('expensive')
        supplied = cases(); supplied[1]['expect_status'] = 200
        result = self.run_compare(base(), candidate, supplied)
        self.assertEqual(result['state'], 'review_required')
        self.assertEqual(result['newly_reachable'], 1)
        self.assertEqual(result['candidate_matched'], 4)

    def test_required_scope_edit_changes_actual_admission(self):
        candidate = base(); candidate['token_auth']['required_scope'] = 'llm:premium'
        result = self.run_compare(base(), candidate, cases())
        self.assertEqual(result['newly_reachable'], 1)
        self.assertEqual(result['newly_refused'], 1)

    def test_removed_provider_alias_is_refused_not_policy_pass(self):
        candidate = base(); candidate['providers'][0]['models'] = ['cheap']
        supplied = cases(); supplied[1]['expect_status'] = 404; supplied[2]['expect_status'] = 403
        result = self.run_compare(base(), candidate, supplied)
        self.assertEqual(result['state'], 'review_required')
        self.assertEqual(result['refusal_changed'], 1)
        self.assertEqual(result['cases'][1]['after']['status'], 404)

    def test_real_route_rejected_before_app_construction(self):
        candidate = base(); candidate['providers'].append({'name': 'real', 'type': 'openai_compatible', 'models': ['real'], 'api_key_env': 'GROQ_API_KEY'})
        with patch('llmgate.config.build_app', side_effect=AssertionError('must not boot')):
            with self.assertRaises(ValueError):
                self.run_compare(base(), candidate, cases())

    def test_caller_store_and_provider_credentials_are_not_used(self):
        with patch.dict('os.environ', {'GROQ_API_KEY': 'not-a-real-key', 'UPSTASH_REDIS_REST_URL': 'https://must-not-call.invalid', 'UPSTASH_REDIS_REST_TOKEN': 'not-a-real-token'}):
            result = self.run_compare(base(), base(), cases())
        self.assertEqual(result['state'], 'pass')
        self.assertNotIn('not-a-real', json.dumps(result))

    def test_missing_dependencies_cli_returns_sanitized_error_exit_two(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            for name, payload in [('before', base()), ('after', base()), ('cases', cases())]:
                (p / (name + '.json')).write_text(json.dumps(payload))
            source = Path(__import__('llmgate').__file__).resolve().parents[1]
            result = subprocess.run([sys.executable, '-S', str(Path(gate.__file__).resolve()), '--gateway-source', str(source), '--baseline', str(p/'before.json'), '--candidate', str(p/'after.json'), '--cases', str(p/'cases.json')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            report = json.loads(result.stdout)
            self.assertEqual(report['state'], 'error')
            self.assertEqual(report['error_type'], 'ModuleNotFoundError')
            self.assertNotIn('Traceback', result.stderr + result.stdout)
            self.assertNotIn(str(source), result.stdout)

    def test_ordinary_native_error_is_not_policy_regression(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            for name, payload in [('before', base()), ('after', base()), ('cases', cases())]:
                (p / (name + '.json')).write_text(json.dumps(payload))
            source = Path(__import__('llmgate').__file__).resolve().parents[1]
            argv = ['policy', '--gateway-source', str(source), '--baseline', str(p/'before.json'), '--candidate', str(p/'after.json'), '--cases', str(p/'cases.json')]
            import io
            from contextlib import redirect_stdout
            output = io.StringIO()
            with patch.object(sys, 'argv', argv), patch.object(gate, 'source_identity', side_effect=RuntimeError('private internal detail')), redirect_stdout(output):
                rc = gate.main()
            self.assertEqual(rc, 2)
            self.assertEqual(json.loads(output.getvalue())['error_type'], 'RuntimeError')
            self.assertNotIn('private internal detail', output.getvalue())

    def test_invalid_duplicate_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, 'a.json'); p.write_text('{"providers":[],"providers":[]}')
            with self.assertRaises(ValueError):
                gate.read_json(p)

    def test_case_schema_and_nondeterministic_config_fail(self):
        for candidate in [dict(base(), token_auth={'enabled': 'true'}), dict(base(), providers=[{'name':'x','type':'mock','models':['m'],'latency':1}])]:
            with self.assertRaises(ValueError):
                self.run_compare(base(), candidate, cases())
        with self.assertRaises(ValueError):
            self.run_compare(base(), base(), [dict(cases()[0], scope='llm:complete')])


if __name__ == '__main__':
    unittest.main()
