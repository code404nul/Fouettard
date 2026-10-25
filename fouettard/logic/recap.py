"""Récap des contributions : commits et issues fermées par membre sur une période.

Attribuer un commit à un membre n'est pas évident : un commit ne contient que
le nom et l'email réglés dans "git config" sur l'ordinateur, pas le compte
GitLab. Mais GitLab sait QUI a poussé (push) chaque commit. On apprend donc,
grâce aux événements de push, quel email git correspond à quel compte GitLab.
"""

import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from fouettard.logic.team import is_bot_account
from fouettard.timeutils import parse_gitlab_datetime


@dataclass
class MemberRecap:
    """Contributions d'un membre sur la période."""

    gitlab_id: int
    name: str
    username: str
    commits: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)


def normalize(text: str) -> str:
    """Minuscules sans accents : "Farès" -> "fares"."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(char for char in text if not unicodedata.combining(char)).lower().strip()


def is_merge_commit(commit: dict) -> bool:
    """Un commit de merge a deux parents. On l'exclut : ce n'est pas du travail en soi."""
    return len(commit.get("parent_ids") or []) > 1


def closed_in_period(issues: list[dict], start: date, end: date) -> list[dict]:
    """Issues fermées entre start et end (inclus)."""
    result = []
    for issue in issues:
        if issue.get("state") != "closed" or not issue.get("closed_at"):
            continue
        if start <= parse_gitlab_datetime(issue["closed_at"]).date() <= end:
            result.append(issue)
    return result


def learn_commit_emails(push_events: list[dict], commits_by_sha: dict[str, dict]) -> dict[str, int]:
    """Associe chaque email git au compte GitLab qui pousse ses commits.

    Pour chaque push, on regarde l'auteur du dernier commit poussé. Si un
    email est vu avec plusieurs comptes (quelqu'un a poussé le commit d'un
    autre), on garde le compte le plus fréquent.
    """
    votes: dict[str, Counter] = {}
    for event in push_events:
        sha = (event.get("push_data") or {}).get("commit_to")
        commit = commits_by_sha.get(sha)
        if commit is None:
            continue
        email = normalize(commit.get("author_email"))
        votes.setdefault(email, Counter())[event["author_id"]] += 1
    return {email: counter.most_common(1)[0][0] for email, counter in votes.items()}


def find_commit_author(commit: dict, members: list[dict], email_to_user: dict[str, int]) -> int | None:
    """Compte GitLab de l'auteur d'un commit, ou None si on ne sait pas.

    1. email appris grâce aux push ;
    2. sinon, nom git identique au nom GitLab, ou email "username@..." ;
    """
    email = normalize(commit.get("author_email"))
    if email in email_to_user:
        return email_to_user[email]

    author_name = normalize(commit.get("author_name"))
    email_user = email.split("@")[0]
    for member in members:
        if author_name and author_name in (normalize(member["name"]), normalize(member["username"])):
            return member["id"]
        if email_user and email_user == normalize(member["username"]):
            return member["id"]
    return None


def issue_owners(issue: dict) -> list[int]:
    """Qui a fait une issue : ses assignés, ou à défaut la personne qui l'a fermée."""
    assignees = [assignee["id"] for assignee in issue.get("assignees") or []]
    if assignees:
        return assignees
    closed_by = issue.get("closed_by")
    return [closed_by["id"]] if closed_by else []


def build_recap(members: list[dict], commits: list[dict], push_events: list[dict],
                closed_issues: list[dict]) -> tuple[list[MemberRecap], list[dict]]:
    """Construit le récap. Renvoie (récap par membre, commits non attribués)."""
    real_members = [member for member in members if not is_bot_account(member)]
    recaps = {member["id"]: MemberRecap(member["id"], member["name"], member["username"])
              for member in real_members}

    commits = [commit for commit in commits if not is_merge_commit(commit)]
    email_to_user = learn_commit_emails(push_events, {commit["id"]: commit for commit in commits})

    unattributed = []
    # Du plus ancien au plus récent, pour un récap chronologique
    for commit in sorted(commits, key=lambda c: c["created_at"]):
        author_id = find_commit_author(commit, real_members, email_to_user)
        if author_id in recaps:
            recaps[author_id].commits.append(commit)
        else:
            unattributed.append(commit)

    for issue in sorted(closed_issues, key=lambda i: i["closed_at"]):
        for owner_id in issue_owners(issue):
            if owner_id in recaps:
                recaps[owner_id].issues.append(issue)

    ordered = sorted(recaps.values(), key=lambda recap: recap.name.lower())
    return ordered, unattributed
