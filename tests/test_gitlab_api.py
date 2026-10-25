"""Tests du client GitLab, avec un faux serveur (httpx.MockTransport).

Aucune vraie requête n'est envoyée : la fonction "handler" joue le rôle de GitLab.
"""

import asyncio

import httpx
import pytest

from fouettard.gitlab_api import GitLabAuthError, GitLabClient, GitLabUnreachable


def make_client(handler) -> GitLabClient:
    """Client branché sur un faux GitLab. retry_delay=0 : pas d'attente dans les tests."""
    return GitLabClient("https://gitlab.example", "token", "groupe/jeu",
                        transport=httpx.MockTransport(handler), retry_delay=0)


def run(coroutine):
    """Exécute une fonction async depuis un test normal."""
    return asyncio.run(coroutine)


def test_project_path_is_encoded():
    client = make_client(lambda request: httpx.Response(200, json={}))
    assert client.project_path == "/projects/groupe%2Fjeu"


def test_pagination_reads_all_pages():
    def handler(request):
        page = request.url.params["page"]
        if page == "1":
            return httpx.Response(200, json=[{"iid": 1}, {"iid": 2}], headers={"X-Next-Page": "2"})
        return httpx.Response(200, json=[{"iid": 3}], headers={"X-Next-Page": ""})

    issues = run(make_client(handler).get_issues())
    assert [issue["iid"] for issue in issues] == [1, 2, 3]


def test_invalid_token_raises_auth_error():
    client = make_client(lambda request: httpx.Response(401))
    with pytest.raises(GitLabAuthError):
        run(client.get_project())


def test_network_error_raises_unreachable_after_retries():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("pas de réseau")

    with pytest.raises(GitLabUnreachable):
        run(make_client(handler).get_project())
    assert len(calls) == 3  # 3 essais avant d'abandonner


def test_rate_limit_then_success():
    responses = [httpx.Response(429, headers={"Retry-After": "0"}),
                 httpx.Response(200, json={"name": "Jeu"})]
    client = make_client(lambda request: responses.pop(0))
    assert run(client.get_project())["name"] == "Jeu"


def test_find_unknown_user_returns_none():
    client = make_client(lambda request: httpx.Response(200, json=[]))
    assert run(client.find_user("inconnu")) is None


def test_file_size_uses_head_request():
    def handler(request):
        assert request.method == "HEAD"
        # raw_path : l'URL telle qu'envoyée, avec le "/" encodé en "%2F"
        assert b"/repository/files/docs%2Finstall.md" in request.url.raw_path
        return httpx.Response(200, headers={"X-Gitlab-Size": "1234"})

    assert run(make_client(handler).get_file_size("docs/install.md", "main")) == 1234


def test_missing_file_size_is_none():
    client = make_client(lambda request: httpx.Response(404))
    assert run(client.get_file_size("docs/install.md", "main")) is None


def test_detect_features_free_version():
    """Version gratuite : pas de champs payants, adresses payantes en 404."""
    def handler(request):
        path = request.url.path
        if path.endswith("/issues"):
            return httpx.Response(200, json=[{"iid": 1, "assignees": []}])
        if path.endswith("/iterations"):
            return httpx.Response(404)
        if path.endswith("/epics"):
            return httpx.Response(403)
        return httpx.Response(200, json={"namespace": {"kind": "group", "id": 7}})

    features = run(make_client(handler).detect_features())
    assert features.enabled_names() == []
    assert features.confirmed


def test_detect_features_without_issues_is_not_confirmed():
    """Projet sans issue : on ne peut pas conclure, il faudra recommencer."""
    def handler(request):
        if request.url.path.endswith("/issues"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/iterations"):
            return httpx.Response(403)
        return httpx.Response(200, json={"namespace": {"kind": "user", "id": 7}})

    features = run(make_client(handler).detect_features())
    assert not features.confirmed


def test_detect_features_paid_version():
    """Version payante : les issues contiennent les champs supplémentaires."""
    issue = {"iid": 1, "assignees": [{"id": 1}, {"id": 2}], "weight": None,
             "health_status": None, "iteration": None, "epic_iid": None}
    client = make_client(lambda request: httpx.Response(200, json=[issue]))
    features = run(client.detect_features())
    assert features.weights and features.health_status and features.iterations
    assert features.epics and features.multiple_assignees
