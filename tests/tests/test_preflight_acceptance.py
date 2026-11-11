import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import importlib.util

_spec = importlib.util.spec_from_file_location("preflight_acceptance", Path(__file__).resolve().parents[1] / "scripts" / "preflight_acceptance.py")
a = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(a)


class ExecutionTests(unittest.TestCase):
    def test_real_pass_and_failure_codes(self):
        for rc, state in ((0, 'pass'), (1, 'regression'), (2, 'error'), (5, 'error')):
            with self.subTest(rc=rc):
                x = a.execute('fixture', [sys.executable, '-c', f'raise SystemExit({rc})'], '.', 3)
                self.assertEqual(x['state'], state)
                self.assertEqual(x['exit_code'], rc)

    def test_output_is_not_disclosed(self):
        x = a.execute('fixture', [sys.executable, '-c', 'print("private fixture")'], '.', 3)
        self.assertNotIn('private fixture', json.dumps(x))
        self.assertEqual(x['stdout']['sha256'], a.digest(b'private fixture\n'))

    def test_caller_secrets_not_inherited(self):
        with patch.dict(os.environ, {'GROQ_API_KEY': 'private', 'UPSTASH_REDIS_REST_TOKEN': 'private', 'PYTHONPATH': '/not/a/path'}):
            x = a.execute('fixture', [sys.executable, '-c', 'import os; assert "GROQ_API_KEY" not in os.environ; assert "UPSTASH_REDIS_REST_TOKEN" not in os.environ; assert "PYTHONPATH" not in os.environ'], '.', 3)
        self.assertEqual(x['state'], 'pass')

    def test_timeout_fails_not_passes(self):
        x = a.execute('fixture', [sys.executable, '-c', 'import time; time.sleep(9)'], '.', 1)
        self.assertEqual(x['state'], 'error')
        self.assertTrue(x['timed_out'])

    def test_missing_executable_is_error(self):
        x = a.execute('fixture', ['/no/such/executable'], '.', 3)
        self.assertEqual(x['state'], 'error')

    def test_git_identity_detects_dirty_source(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(['git', 'init', '-q', d], check=True)
            subprocess.run(['git', '-C', d, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'fixture'], check=True)
            self.assertFalse(a.identity(d)['dirty'])
            Path(d, 'changed.txt').write_text('new untracked input')
            self.assertTrue(a.identity(d)['dirty'])

    def test_policy_report_must_be_valid_even_if_child_returns_zero(self):
        x = a.execute('actas-policy-comparison', [sys.executable, '-c', 'print("not JSON")'], '.', 3)
        self.assertEqual(x['state'], 'error')

    def test_policy_counts_exclude_case_contents(self):
        report = {'total': 2, 'newly_allowed': 1, 'newly_denied': 0, 'unchanged': 1, 'candidate': {'matched': 2}, 'private_case': 'do not share'}
        x = a.execute('actas-policy-comparison', [sys.executable, '-c', 'print(' + repr(json.dumps(report)) + ')'], '.', 3)
        self.assertEqual(x['policy_counts']['newly_allowed'], 1)
        self.assertEqual(x['state'], 'error')
        self.assertNotIn('do not share', json.dumps(x))

    def test_gateway_success_without_native_junit_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, 'tests/test_redislimit.py'); p.parent.mkdir(); p.write_bytes(b'fixture')
            with patch.object(a, 'SUPPORTED_GATEWAY_CONTRACT_SHA256', a.digest(b'fixture')), patch.object(a, 'execute', return_value={'state': 'pass', 'exit_code': 0}):
                x = a.gateway_contract(sys.executable, d, 3)
        self.assertEqual(x['state'], 'error')
        self.assertIn('Native test-count', x['evidence_error'])

    def test_gateway_all_skips_is_not_pass(self):
        def fake(name, argv, cwd, timeout):
            path = next(s.split('=', 1)[1] for s in argv if s.startswith('--junitxml='))
            Path(path).write_text('<testsuites><testsuite tests="1" skipped="1" errors="0" failures="0"/></testsuites>')
            return {'state': 'pass', 'exit_code': 0}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, 'tests/test_redislimit.py'); p.parent.mkdir(); p.write_bytes(b'fixture')
            with patch.object(a, 'SUPPORTED_GATEWAY_CONTRACT_SHA256', a.digest(b'fixture')), patch.object(a, 'execute', side_effect=fake):
                x = a.gateway_contract(sys.executable, d, 3)
        self.assertEqual(x['state'], 'error')
        self.assertEqual(x['native_test_counts']['skipped'], 1)

    def test_inconsistent_policy_counts_fail_closed(self):
        cases = [(0, 0, 0, 0, 0), (2, 1, 0, 2, 2), (2, 0, 0, 2, 3), (2, 1, 0, 1, 2)]
        for total, allowed, denied, unchanged, matched in cases:
            report = {'total': total, 'newly_allowed': allowed, 'newly_denied': denied, 'unchanged': unchanged, 'candidate': {'matched': matched}}
            x = a.execute('actas-policy-comparison', [sys.executable, '-c', 'print(' + repr(json.dumps(report)) + ')'], '.', 3)
            self.assertEqual(x['state'], 'error')

    def test_unknown_gateway_contract_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, 'tests/test_redislimit.py'); p.parent.mkdir(); p.write_text('old emulated contract')
            x = a.gateway_contract(sys.executable, d, 3)
            self.assertEqual(x['state'], 'error')
            self.assertIn('Unsupported', x['evidence_error'])

    def test_input_cap(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, 'large.json')
            p.write_bytes(b' ' * 65537)
            with self.assertRaises(ValueError):
                a.resolve_file(p)


if __name__ == '__main__':
    unittest.main()
