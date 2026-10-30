"""Accès à l'API GitLab (v4).

C'est le SEUL fichier du bot qui parle à GitLab. Le reste du code appelle
les méthodes de GitLabClient, ce qui permet de le remplacer par un faux
client dans les tests.

Le bot ne fait que des lectures (requêtes GET) : le token n'a besoin que
du scope "read_api".
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

# Nombre d'essais pour une requête avant d'abandonner
MAX_ATTEMPTS = 3
# Attente maximale (en secondes) si GitLab nous demande de ralentir
MAX_RETRY_AFTER = 60


# --- Erreurs ---------------------------------------------------------------

class GitLabError(Exception):
    """Erreur générique en parlant à GitLab."""


class GitLabUnreachable(GitLabError):
    """GitLab ne répond pas (réseau, serveur en panne, trop de requêtes)."""


class GitLabAuthError(GitLabError):
    """Token invalide, expiré ou sans les droits nécessaires."""


class GitLabNotFound(GitLabError):
    """La ressource demandée n'existe pas (ou n'est pas visible avec ce token)."""


# --- Fonctionnalités payantes ----------------------------------------------

@dataclass
class Features:
    """Fonctionnalités GitLab disponibles sur ce projet.

    Certaines ne sont présentes qu'avec une licence Premium ou Ultimate.
    Par défaut on suppose qu'aucune n'est disponible.
    """

    weights: bool = False
    iterations: bool = False
    epics: bool = False
    multiple_assignees: bool = False
    health_status: bool = False
    # Vrai si la détection a pu examiner au moins une issue.
    # Sans issue, on ne peut pas savoir : il faudra recommencer plus tard.
    confirmed: bool = False

    def enabled_names(self) -> list[str]:
        """Noms lisibles des fonctionnalités disponibles."""
        labels = {
            "weights": "poids",
            "iterations": "itérations",
            "epics": "epics",
            "multiple_assignees": "assignés multiples",
            "health_status": "health status",
        }
        return [label for attribute, label in labels.items() if getattr(self, attribute)]


# --- Client ----------------------------------------------------------------

class GitLabClient:
    """Client asynchrone (lecture seule) pour un projet GitLab."""

    def __init__(self, url: str, token: str, project_id: str,
                 transport: httpx.AsyncBaseTransport | None = None,
                 retry_delay: float = 2.0):
        # L'identifiant peut être un nombre ("1234") ou un chemin ("groupe/jeu") :
        # dans le second cas il faut l'encoder ("groupe%2Fjeu") pour l'URL.
        self.project_path = "/projects/" + quote(str(project_id), safe="")
        self.retry_delay = retry_delay
        # "transport" ne sert que dans les tests, pour simuler GitLab.
        self._http = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/api/v4",
            headers={"PRIVATE-TOKEN": token},
            timeout=20.0,
            transport=transport,
        )

    async def close(self) -> None:
        """Ferme proprement la connexion HTTP."""
        await self._http.aclose()

    # --- Mécanique des requêtes --------------------------------------------

    async def _request(self, path: str, params: dict | None = None,
                       method: str = "GET") -> httpx.Response:
        """Fait une requête de lecture (GET ou HEAD), avec plusieurs essais
        en cas de problème temporaire."""
        problem = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            wait = self.retry_delay * attempt
            try:
                response = await self._http.request(method, path, params=params)
            except httpx.TransportError as exc:
                # Pas de réseau, DNS, délai dépassé...
                problem = f"connexion impossible ({exc.__class__.__name__})"
            else:
                if response.status_code == 429:
                    # Limite de requêtes atteinte : GitLab indique combien attendre
                    problem = "trop de requêtes (429)"
                    wait = self._retry_after(response)
                elif response.status_code >= 500:
                    problem = f"erreur du serveur ({response.status_code})"
                else:
                    self._raise_for_status(response, path)
                    return response

            log.warning("GitLab : %s sur %s (essai %d/%d)", problem, path, attempt, MAX_ATTEMPTS)
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(wait)

        raise GitLabUnreachable(f"GitLab ne répond pas correctement : {problem}")

    def _retry_after(self, response: httpx.Response) -> float:
        """Nombre de secondes à attendre d'après l'en-tête Retry-After."""
        value = response.headers.get("Retry-After", "")
        if value.isdigit():
            return min(int(value), MAX_RETRY_AFTER)
        return MAX_RETRY_AFTER

    def _raise_for_status(self, response: httpx.Response, path: str) -> None:
        """Transforme les codes d'erreur HTTP en exceptions claires."""
        status = response.status_code
        if status == 401:
            raise GitLabAuthError("token GitLab invalide ou expiré (401)")
        if status == 403:
            raise GitLabAuthError(f"accès refusé par GitLab (403) sur {path}")
        if status == 404:
            raise GitLabNotFound(f"introuvable sur GitLab (404) : {path}")
        if status >= 400:
            raise GitLabError(f"erreur GitLab {status} sur {path}")

    async def _get(self, path: str, params: dict | None = None):
        """GET simple : renvoie le JSON de la réponse."""
        response = await self._request(path, params)
        return response.json()

    async def _get_all(self, path: str, params: dict | None = None) -> list[dict]:
        """GET d'une liste paginée : récupère toutes les pages."""
        params = dict(params or {})
        params["per_page"] = 100
        results = []
        page = 1
        while True:
            params["page"] = page
            response = await self._request(path, params)
            results.extend(response.json())
            # GitLab indique le numéro de la page suivante (vide s'il n'y en a plus)
            next_page = response.headers.get("X-Next-Page")
            if not next_page:
                return results
            page = int(next_page)

    async def _endpoint_exists(self, path: str) -> bool:
        """Vrai si l'adresse répond, faux si elle est absente ou interdite."""
        try:
            await self._request(path, {"per_page": 1})
        except (GitLabNotFound, GitLabAuthError):
            return False
        return True

    # --- Méthodes utilisées par le bot -------------------------------------

    async def get_project(self) -> dict:
        """Informations sur le projet (nom, lien, branche par défaut...)."""
        return await self._get(self.project_path)

    async def find_user(self, username: str) -> dict | None:
        """Cherche un utilisateur GitLab par son username. None s'il n'existe pas."""
        users = await self._get("/users", {"username": username})
        return users[0] if users else None

    async def get_members(self) -> list[dict]:
        """Membres du projet (y compris ceux hérités du groupe)."""
        return await self._get_all(f"{self.project_path}/members/all")

    async def get_issues(self, state: str = "all") -> list[dict]:
        """Issues du projet. state : "all", "opened" ou "closed"."""
        return await self._get_all(f"{self.project_path}/issues", {"state": state})

    async def get_milestones(self, state: str = "active") -> list[dict]:
        """Milestones du projet. state : "active" ou "closed"."""
        return await self._get_all(f"{self.project_path}/milestones", {"state": state})

    async def get_labels(self) -> list[dict]:
        """Labels du projet (avec leur priorité éventuelle)."""
        return await self._get_all(f"{self.project_path}/labels")

    async def get_events(self, after: date, before: date | None = None,
                         action: str | None = None) -> list[dict]:
        """Activité du projet (push, commentaires, issues, MR...).

        GitLab renvoie les événements strictement après "after" et strictement
        avant "before". action : par exemple "pushed" pour n'avoir que les push.
        """
        params = {"after": after.isoformat()}
        if before:
            params["before"] = before.isoformat()
        if action:
            params["action"] = action
        return await self._get_all(f"{self.project_path}/events", params)

    async def get_merge_requests(self, state: str = "opened") -> list[dict]:
        """Merge requests du projet. state : "opened", "merged", "closed" ou "all"."""
        return await self._get_all(f"{self.project_path}/merge_requests", {"state": state})

    async def is_merge_request_approved(self, mr_iid: int) -> bool:
        """Vrai si la merge request a reçu les approbations nécessaires.

        Si GitLab ne fournit pas cette information, on considère qu'elle
        n'est pas approuvée.
        """
        try:
            approvals = await self._get(f"{self.project_path}/merge_requests/{mr_iid}/approvals")
        except (GitLabNotFound, GitLabAuthError):
            return False
        return bool(approvals.get("approved"))

    async def get_commits(self, since: datetime, until: datetime) -> list[dict]:
        """Commits de toutes les branches entre deux dates."""
        return await self._get_all(f"{self.project_path}/repository/commits", {
            "all": "true",
            "since": since.isoformat(),
            "until": until.isoformat(),
        })

    async def get_file_size(self, file_path: str, ref: str) -> int | None:
        """Taille en octets d'un fichier du dépôt, ou None s'il n'existe pas.

        On utilise une requête HEAD : GitLab donne la taille dans l'en-tête
        X-Gitlab-Size sans envoyer le contenu du fichier.
        """
        path = f"{self.project_path}/repository/files/{quote(file_path, safe='')}"
        try:
            response = await self._request(path, {"ref": ref}, method="HEAD")
        except GitLabNotFound:
            return None
        return int(response.headers.get("X-Gitlab-Size", 0))

    async def detect_features(self) -> Features:
        """Regarde quelles fonctionnalités payantes sont disponibles.

        GitLab n'ajoute certains champs dans les issues que si la licence
        le permet : on regarde donc si ces champs sont présents.
        """
        features = Features()
        sample = await self._get(f"{self.project_path}/issues", {"state": "all", "per_page": 20})

        if sample:
            features.confirmed = True
            first = sample[0]
            features.weights = "weight" in first
            features.health_status = "health_status" in first
            features.iterations = "iteration" in first
            features.epics = "epic_iid" in first or "epic" in first
            features.multiple_assignees = any(len(issue.get("assignees") or []) > 1
                                              for issue in sample)

        # Sans issue à examiner, on interroge directement les adresses concernées
        if not features.iterations:
            features.iterations = await self._endpoint_exists(f"{self.project_path}/iterations")
        if not features.epics:
            project = await self.get_project()
            namespace = project.get("namespace") or {}
            if namespace.get("kind") == "group":
                features.epics = await self._endpoint_exists(f"/groups/{namespace['id']}/epics")

        # Les assignés multiples font partie de la même licence (Premium) que les poids
        features.multiple_assignees = features.multiple_assignees or features.weights
        return features
