"""Fail-closed admission regressions; all upstream responses here are synthetic."""
from __future__ import annotations

import copy

import pytest

from app.portfolio_bridge import BridgeRejected, run_page
from test_portfolio_bridge import FakeUpstream, PAGE, REPO, SOURCE, NOW, run


class ReadOnlyPreflight(FakeUpstream):
    """A synthetic admitted lane; any attempted enqueue fails this test."""

    def __init__(self, endpoint=None, occurrence=1, change=None):
        super().__init__()
        self.endpoint = endpoint
        self.occurrence = occurrence
        self.change = change
        self.visits = {}

    def request(self, service, method, url, token, data=None):
        if service == "worker":
            self.calls.append((service, method, url))
            assert method == "GET" and url.endswith("/v1/repositories"), "unexpected_enqueue"
            return 200, [{"repository": REPO, "enabled": True, "max_in_flight": 1}]
        kind = ("page" if service == "notion" else
                "issue" if "/issues/" in url else
                "ref" if "/git/ref/" in url else "repo")
        self.visits[kind] = self.visits.get(kind, 0) + 1
        if kind == "ref" and url.endswith("/heads/develop"):
            self.calls.append((service, method, url))
            return 200, {"ref": "refs/heads/develop", "object": self.ref["object"]}
        status, value = super().request(service, method, url, token, data)
        if kind == self.endpoint and self.visits[kind] == self.occurrence:
            value = copy.deepcopy(value)
            if callable(self.change):
                value = self.change(value)
            else:
                value.update(self.change)
        return status, value


@pytest.mark.parametrize(("change", "reason"), [
    ({"id": "00000000-0000-0000-0000-000000000000"}, "notion_page_id_mismatch"),
    ({"id": "PRIVATE_VALUE"}, "notion_page_id_invalid"),
    ({"id": None}, "notion_page_id_invalid"),
    ({"id": 1}, "notion_page_id_invalid"),
    ({"archived": True}, "notion_page_not_active"),
    ({"in_trash": True}, "notion_page_not_active"),
    ({"archived": 0}, "notion_page_not_active"),
    ({"in_trash": "false"}, "notion_page_not_active"),
    ({"in_trash": None}, "notion_page_not_active"),
    ({"parent": "PRIVATE_VALUE"}, "notion_source_mismatch"),
    ({"parent": {"type": "data_source_id", "data_source_id": 1}}, "notion_source_mismatch"),
])
@pytest.mark.parametrize("occurrence", [1, 2])
def test_invalid_or_replaced_page_never_enqueues(change, reason, occurrence):
    fake = ReadOnlyPreflight("page", occurrence, change)
    with pytest.raises(BridgeRejected, match="^" + reason + "$") as error:
        run(fake, execute=True)
    assert "PRIVATE_VALUE" not in str(error.value)
    assert not any(m == "POST" for _, m, _ in fake.calls)


@pytest.mark.parametrize("missing", ["id", "archived", "in_trash"])
def test_missing_page_identity_or_activity_fails_closed(missing):
    def remove(value):
        value.pop(missing)
        return value
    fake = ReadOnlyPreflight("page", 1, remove)
    with pytest.raises(BridgeRejected):
        run(fake)
    assert [s for s, _, _ in fake.calls] == ["notion"]


@pytest.mark.parametrize("change", [
    {"state": "closed"}, {"pull_request": {}}, {"number": 14},
    {"number": True}, {"html_url": "https://example.invalid/PRIVATE_VALUE"},
])
def test_issue_changed_before_enqueue_is_blocked(change):
    fake = ReadOnlyPreflight("issue", 2, change)
    with pytest.raises(BridgeRejected, match="^github_issue_not_open_or_mismatch$"):
        run(fake, execute=True)
    assert fake.visits["issue"] == 2
    assert not any(m == "POST" for _, m, _ in fake.calls)


@pytest.mark.parametrize("occurrence", [1, 2])
def test_wrong_ref_identity_rejected_even_with_matching_sha(occurrence):
    fake = ReadOnlyPreflight("ref", occurrence, {"ref": "refs/heads/not-main"})
    with pytest.raises(BridgeRejected, match="^github_ref_identity_mismatch$"):
        run(fake, execute=True)
    assert not any(m == "POST" for _, m, _ in fake.calls)


def test_changed_default_branch_cannot_reuse_equal_sha():
    fake = ReadOnlyPreflight("repo", 2, {"default_branch": "develop"})
    with pytest.raises(BridgeRejected, match="^github_default_branch_changed_before_dispatch$"):
        run(fake, execute=True)
    assert not any(m == "POST" for _, m, _ in fake.calls)


@pytest.mark.parametrize("ref_object", [None, [], "PRIVATE_VALUE", {"sha": "invalid"}])
def test_malformed_ref_is_sanitized(ref_object):
    fake = ReadOnlyPreflight("ref", 2, {"object": ref_object})
    with pytest.raises(BridgeRejected, match="^github_ref_missing$") as error:
        run(fake, execute=True)
    assert "PRIVATE_VALUE" not in str(error.value)
    assert not any(m == "POST" for _, m, _ in fake.calls)


def test_uuid_normalization_is_accepted_and_dry_run_replay_has_no_worker_calls():
    fake = ReadOnlyPreflight("page", 1, {"id": PAGE.replace("-", "").upper()})
    first = run(fake)
    second = run(ReadOnlyPreflight())
    assert first == second
    assert first["dispatched"] is False
    assert not any(s == "worker" for s, _, _ in fake.calls)


def test_final_issue_read_failure_cannot_fall_back_to_old_open_state():
    class FailingIssue(ReadOnlyPreflight):
        def request(self, service, method, url, token, data=None):
            status, value = super().request(service, method, url, token, data)
            if "/issues/" in url and self.visits["issue"] == 2:
                return 503, {"error": "PRIVATE_VALUE"}
            return status, value
    fake = FailingIssue()
    with pytest.raises(BridgeRejected, match="^github_get_failed$"):
        run(fake, execute=True)
    assert not any(m == "POST" for _, m, _ in fake.calls)


def test_final_notion_read_failure_blocks():
    class FailingPage(ReadOnlyPreflight):
        def request(self, service, method, url, token, data=None):
            status, value = super().request(service, method, url, token, data)
            if service == "notion" and self.visits["page"] == 2:
                return 503, {}
            return status, value
    fake = FailingPage()
    with pytest.raises(BridgeRejected, match="^notion_get_failed$"):
        run(fake, execute=True)
    assert not any(m == "POST" for _, m, _ in fake.calls)


def test_closed_issue_rejection_is_idempotent():
    for _ in range(2):
        fake = ReadOnlyPreflight("issue", 2, {"state": "closed"})
        with pytest.raises(BridgeRejected, match="^github_issue_not_open_or_mismatch$"):
            run(fake, execute=True)
        assert not any(m == "POST" for _, m, _ in fake.calls)


def test_replay_does_not_bypass_new_canonical_read():
    fake = ReadOnlyPreflight("page", 1, {"in_trash": True})
    with pytest.raises(BridgeRejected, match="^notion_page_not_active$"):
        run_page(page_id=PAGE, data_source_id=SOURCE, transport=fake,
                 notion_token="notion-test", github_token="github-test",
                 worker_token="worker-test", worker_url="http://127.0.0.1:9000",
                 execute=True, environment="dev", now=NOW)
    assert not any(s == "worker" for s, _, _ in fake.calls)
