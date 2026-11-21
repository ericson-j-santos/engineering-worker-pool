"""Negative, replay and boundary tests for issue #8 owner authorization lookup.

This suite uses fake comments/HTTPS responses only; it does not qualify a
physical worker, owner authority outside the API, or a live Ollama model.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from app.issue_draft_pr import HandoffBlocked, OWNER, PROFILE, REPO
from scripts import issue_rag_real_qwen_e2e as live

CURRENT = "e22aabb14e1a646e274c331e83cc06c470607a38"
OLD = "87a8de64ea5ed2e4aef1a9cde51861edb61ffe5e"


def comment(comment_id: int, sha: str = CURRENT) -> dict:
    return {
        "id": comment_id,
        "user": {"login": OWNER, "type": "User"},
        "author_association": "OWNER",
        "issue_url": f"https://api.github.com/repos/{REPO}/issues/8",
        "body": f"/local-agent authorize {PROFILE} sha:{sha}",
    }


def test_current_sha_selects_exact_latest_owner_comment_without_mutation() -> None:
    entries = [comment(10, OLD), comment(20), comment(30), comment(40, OLD)]
    before = json.dumps(entries, sort_keys=True)
    assert live.select_owner_authorization(entries, CURRENT) == 30
    assert live.select_owner_authorization(entries, CURRENT) == 30
    assert json.dumps(entries, sort_keys=True) == before


def test_old_owner_authorization_never_approves_current_main() -> None:
    with pytest.raises(HandoffBlocked, match="^issue8_current_owner_authorization_missing$"):
        live.select_owner_authorization([comment(1, OLD)], CURRENT)


@pytest.mark.parametrize("kind", [
    "wrong_owner", "wrong_type", "not_owner", "wrong_issue",
    "command_suffix", "different_profile", "wrong_sha", "wrong_id",
])
def test_nonmatching_or_spoofed_authorization_fails_closed(kind: str) -> None:
    item = comment(11)
    if kind == "wrong_owner":
        item["user"]["login"] = "untrusted"
    elif kind == "wrong_type":
        item["user"]["type"] = "Bot"
    elif kind == "not_owner":
        item["author_association"] = "CONTRIBUTOR"
    elif kind == "wrong_issue":
        item["issue_url"] += "0"
    elif kind == "command_suffix":
        item["body"] += " --force"
    elif kind == "different_profile":
        item["body"] = item["body"].replace(PROFILE, "other-profile")
    elif kind == "wrong_sha":
        item["body"] = item["body"].replace(CURRENT, OLD)
    else:
        item["id"] = "11"
    with pytest.raises(HandoffBlocked, match="issue8_"):
        live.select_owner_authorization([item], CURRENT)


@pytest.mark.parametrize("items,sha", [
    ([], CURRENT), ([comment(1)], "invalid"), ([comment(1), comment(1)], CURRENT),
    ([{"id": 1}], CURRENT), (["bad-item"], CURRENT),
])
def test_empty_duplicate_or_malformed_source_is_rejected(items, sha) -> None:
    with pytest.raises(HandoffBlocked, match="issue8_"):
        live.select_owner_authorization(items, sha)


class FakeResponse:
    status = 200

    def __init__(self, content: bytes) -> None:
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size: int) -> bytes:
        return self.content[:size]


def test_fixed_api_get_pagination_without_token_leak(monkeypatch) -> None:
    seen = []

    class FakeOpener:
        def open(self, request, timeout):
            seen.append((request.full_url, request.get_method(), timeout,
                         request.get_header("Authorization")))
            page = int(request.full_url.split("page=")[-1])
            payload = [comment(i) for i in range(1, 101)] if page == 1 else [comment(101)]
            return FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(live.urllib.request, "build_opener", lambda _: FakeOpener())
    items = live.list_issue_comments("ephemeral-token")
    assert len(items) == 101
    assert live.select_owner_authorization(items, CURRENT) == 101
    assert [x[1] for x in seen] == ["GET", "GET"]
    assert all(x[0].startswith(f"https://api.github.com/repos/{REPO}/issues/8/comments?")
               and x[2] == 12 and x[3] == "Bearer ephemeral-token" for x in seen)


def test_redirect_or_transport_failure_never_discloses_token(monkeypatch) -> None:
    class BlockedOpener:
        def open(self, request, timeout):
            raise urllib.error.URLError("private redirect failure")
    monkeypatch.setattr(live.urllib.request, "build_opener", lambda _: BlockedOpener())
    with pytest.raises(HandoffBlocked, match="^issue8_comment_lookup_unavailable$") as error:
        live.list_issue_comments("sensitive-temporary-token")
    assert "sensitive" not in str(error.value)


def test_truncated_history_is_not_silently_accepted(monkeypatch) -> None:
    class FullPages:
        def open(self, request, timeout):
            return FakeResponse(json.dumps([comment(i) for i in range(1, 101)]).encode())
    monkeypatch.setattr(live.urllib.request, "build_opener", lambda _: FullPages())
    with pytest.raises(HandoffBlocked, match="^issue8_comment_history_truncated$"):
        live.list_issue_comments(None)
