"""Isolated static policy tests; no model, network, Git credentials or Docker."""
from __future__ import annotations

import importlib.util
import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = HERE / "app" / "ollama_transport_repair_profile.py"
spec = importlib.util.spec_from_file_location("ollama_transport_repair_profile", MODULE_PATH)
assert spec and spec.loader
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)

BASE = '''import json
import urllib.error
import urllib.request

class PilotBlocked(RuntimeError):
    pass

def http_json(path: str, *, payload=None, timeout=10):
    if path not in {"/api/tags", "/api/generate", "/api/ps"}:
        raise PilotBlocked("endpoint_not_allowed")
    req = urllib.request.Request("http://127.0.0.1:11434" + path)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(131073)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        raise PilotBlocked("ollama_unreachable") from None
    return json.loads(raw)
'''
REPLACEMENT = '''    except urllib.error.HTTPError as exc:
        if type(exc.code) is int and 400 <= exc.code <= 599:
            raise PilotBlocked(f"ollama_http_{exc.code}") from None
        raise PilotBlocked("ollama_http_error") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise PilotBlocked("ollama_timeout") from None
        raise PilotBlocked("ollama_connection_error") from None
    except TimeoutError:
        raise PilotBlocked("ollama_timeout") from None
    except ConnectionError:
        raise PilotBlocked("ollama_connection_error") from None
    except OSError:
        raise PilotBlocked("ollama_os_error") from None'''
OLD = '''    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        raise PilotBlocked("ollama_unreachable") from None'''
CANDIDATE = BASE.replace(OLD, REPLACEMENT)


def check(before=BASE, after=CANDIDATE, **identity):
    args = dict(repository=profile.REPOSITORY, path=profile.PATH,
                base_sha=profile.BASE_SHA, before=before.encode(), after=after.encode())
    args.update(identity)
    return profile.classify_candidate(**args)


class ProfileChecks(unittest.TestCase):
    def reject(self, code, before=BASE, after=CANDIDATE, **identity):
        with self.assertRaises(profile.ProfileBlocked) as ctx:
            check(before, after, **identity)
        self.assertEqual(str(ctx.exception), code)

    def test_positive_scoped_patch_and_replay(self):
        a = check()
        self.assertEqual(a["state"], "static_profile_admitted_only")
        self.assertEqual(a, check())
        self.assertEqual(len(a["after_sha256"]), 64)
        self.assertNotEqual(a["before_sha256"], a["after_sha256"])

    def test_old_baseline_is_not_accepted_as_new_patch(self):
        self.reject("patch_empty_or_invalid", after=BASE)

    def test_bad_repository(self):
        self.reject("identity_or_base_drift", repository="attacker/other")

    def test_bad_path(self):
        self.reject("identity_or_base_drift", path="app/portfolio_bridge.py")

    def test_bad_main_sha(self):
        self.reject("identity_or_base_drift", base_sha="f" * 40)

    def test_nonreproduced_baseline(self):
        self.reject("baseline_not_reproduced", before=BASE.replace("ollama_unreachable", "different"))

    def test_import_side_effect_denied(self):
        self.reject("candidate_scope_changed", after=CANDIDATE.replace("import json", "import subprocess"))

    def test_endpoint_mutation_denied(self):
        self.reject("candidate_scope_changed", after=CANDIDATE.replace('"/api/generate"', '"/api/pull"'))

    def test_network_transport_mutation_denied(self):
        self.reject("candidate_scope_changed", after=CANDIDATE.replace("build_opener", "urlopen"))

    def test_secret_exfiltration_denied(self):
        evil = CANDIDATE.replace('raise PilotBlocked("ollama_timeout") from None',
                                 'raise PilotBlocked(str(exc)) from None', 1)
        self.reject("handler_call_forbidden", after=evil)

    def test_shell_invocation_denied(self):
        evil = CANDIDATE.replace('raise PilotBlocked("ollama_os_error") from None',
                                 'raise PilotBlocked(__import__("os").system("cmd")) from None')
        self.reject("handler_call_forbidden", after=evil)

    def test_arbitrary_exception_denied(self):
        self.reject("handler_classes_invalid", after=CANDIDATE.replace("except ConnectionError:", "except BaseException:"))

    def test_arbitrary_raise_denied(self):
        self.reject("handler_grammar_invalid", after=CANDIDATE.replace('raise PilotBlocked("ollama_os_error") from None', "return False"))

    def test_added_module_comment_denied(self):
        self.reject("module_text_changed", after=CANDIDATE + "# another unreviewed change\n")

    def test_invalid_source_bytes_denied(self):
        with self.assertRaises(profile.ProfileBlocked) as ctx:
            profile.classify_candidate(repository=profile.REPOSITORY, path=profile.PATH,
                base_sha=profile.BASE_SHA, before=BASE.encode(), after=b"\xff\xfe")
        self.assertEqual(str(ctx.exception), "source_syntax_invalid")

    def test_handler_silent_success_denied(self):
        self.reject("handler_fallback_missing", after=CANDIDATE.replace(
            '        raise PilotBlocked("ollama_os_error") from None',
            '        if 400 == 400:\n            raise PilotBlocked("ollama_os_error") from None'))


if __name__ == "__main__":
    unittest.main()
