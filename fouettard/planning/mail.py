"""Lecture des mails (IMAP) et tri par règles simples, sans IA.

La boîte est ouverte en lecture seule et les messages sont lus sans être marqués
comme lus. Le mot de passe n'apparaît dans aucun message d'erreur, et seuls
l'expéditeur, le sujet et la date extraite sont conservés : jamais le corps du mail.
"""

import email
import html
import imaplib
import logging
import re
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email import policy
from email.utils import parseaddr, parsedate_to_datetime

from fouettard.planning import nlp
from fouettard.planning.config import ImapSettings
from fouettard.planning.textutils import normalize
from fouettard.timeutils import PARIS

log = logging.getLogger(__name__)

A_TRAITER = "a_traiter"
A_SAVOIR = "a_savoir"
BRUIT = "bruit"

IMAP_TIMEOUT = 30
MAX_MESSAGE_BYTES = 200_000  # le serveur est petit : on ne lit pas les pièces jointes
MAX_PER_READ = 50
MAX_TEXT_CHARS = 5000
IMAP_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_DUE_TRIGGER = re.compile(r"\b(avant|pour|d'ici|jusqu'au|jusqu'a|au plus tard|date limite|deadline|"
                          r"a rendre|echeance)\b([^.!?]{0,40})")
_QUOTE_START = re.compile(r"^(-{2,}\s*(original message|message d'origine)|le .{5,80} a ecrit\s*:|on .{5,80} wrote:)")
_NOREPLY = re.compile(r"^(no-?reply|ne-?pas-?repondre|donotreply|do-not-reply|mailer-daemon|notifications?)\b")


class MailError(Exception):
    """Boîte inaccessible. Le message ne contient ni mot de passe ni contenu de mail."""


@dataclass(frozen=True)
class MailRules:
    school_domains: list[str] = field(default_factory=list)
    prof_senders: list[str] = field(default_factory=list)
    noise_domains: list[str] = field(default_factory=list)
    noise_keywords: list[str] = field(default_factory=list)
    action_keywords: list[str] = field(default_factory=list)
    urgent_keywords: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MailMessage:
    uid_key: str
    sender: str
    sender_name: str
    subject: str
    received: datetime
    text: str = ""
    bulk: bool = False  # liste de diffusion ou envoi de masse


# --- Lecture d'un message ----------------------------------------------------

def _html_to_text(content: str) -> str:
    content = re.sub(r"(?is)<(script|style).*?</\1>", " ", content)
    content = re.sub(r"(?i)<(br|/p|/div|/tr)[^>]*>", "\n", content)
    return html.unescape(re.sub(r"<[^>]+>", " ", content))


def _body_text(message) -> str:
    """Texte du mail sans les citations des messages précédents."""
    try:
        part = message.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        content = part.get_content()
        if part.get_content_subtype() == "html":
            content = _html_to_text(content)
    except (LookupError, ValueError, KeyError, AttributeError):  # encodage inconnu ou mail cassé
        return ""
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if _QUOTE_START.match(normalize(stripped)):
            break
        if not stripped.startswith(">"):
            lines.append(stripped)
    return "\n".join(lines)[:MAX_TEXT_CHARS]


def parse_message(raw: bytes, uid_key: str, fallback: datetime) -> MailMessage:
    """Transforme un message brut. Un mail mal formé donne un résultat partiel, pas une erreur."""
    message = email.message_from_bytes(raw, policy=policy.default)
    name, address = parseaddr(str(message.get("From", "")))
    try:
        received = parsedate_to_datetime(str(message.get("Date", "")))
        received = received.replace(tzinfo=PARIS) if received.tzinfo is None else received.astimezone(PARIS)
    except (TypeError, ValueError):
        received = fallback
    headers = {name.lower() for name in message.keys()}
    precedence = str(message.get("Precedence", "")).lower()
    return MailMessage(
        uid_key=uid_key, sender=address.lower(), sender_name=name,
        subject=" ".join(str(message.get("Subject", "")).split()), received=received,
        text=_body_text(message),
        bulk="list-unsubscribe" in headers or "list-id" in headers or precedence in ("bulk", "list"))


# --- Tri -----------------------------------------------------------------------

def _domain_matches(address: str, domains: list[str]) -> bool:
    domain = address.rpartition("@")[2]
    return any(domain == item.lower() or domain.endswith("." + item.lower()) for item in domains)


def is_school(message: MailMessage, rules: MailRules) -> bool:
    return (message.sender in {sender.lower() for sender in rules.prof_senders}
            or _domain_matches(message.sender, rules.school_domains))


def _contains(text: str, keywords: list[str]) -> bool:
    return any(re.search(rf"(?<!\w){re.escape(normalize(keyword))}(?!\w)", text) for keyword in keywords)


def classify(message: MailMessage, rules: MailRules, due_on: date | None = None) -> str:
    """Trie un mail en « à traiter », « à savoir » ou « bruit »."""
    subject = normalize(message.subject)
    whole = f"{subject} {normalize(message.text)}"
    if is_school(message, rules):
        # L'école n'est jamais du bruit, même envoyée par une liste de diffusion
        if due_on is not None or _contains(whole, rules.action_keywords):
            return A_TRAITER
        return A_SAVOIR
    if (message.bulk or _domain_matches(message.sender, rules.noise_domains)
            or _NOREPLY.match(message.sender) or _contains(whole, rules.noise_keywords)):
        return BRUIT
    # Un inconnu ne passe « à traiter » que s'il le dit dans le sujet
    return A_TRAITER if _contains(subject, rules.action_keywords) else A_SAVOIR


def is_urgent(message: MailMessage, rules: MailRules, due_on: date | None) -> bool:
    """Mail urgent d'un prof ou de l'école : mot-clé dans le sujet ou échéance immédiate."""
    if not is_school(message, rules):
        return False
    if due_on is not None and (due_on - message.received.date()).days <= 1:
        return True
    return _contains(normalize(message.subject), rules.urgent_keywords)


def find_due_date(text: str, received: date) -> date | None:
    """Date écrite dans le mail (« à rendre avant jeudi »), comptée depuis sa réception."""
    candidates = []
    for match in _DUE_TRIGGER.finditer(normalize(text)):
        spec = nlp.parse_dates(match.group(2), received)
        if spec and spec.dates and spec.dates[-1] >= received:
            candidates.append(spec.dates[-1])
    return min(candidates) if candidates else None


# --- Accès à la boîte ------------------------------------------------------------

def _connect(settings: ImapSettings):
    # Sans ce contexte, imaplib ne vérifie pas le certificat du serveur
    connection = imaplib.IMAP4_SSL(settings.host, settings.port, timeout=IMAP_TIMEOUT,
                                   ssl_context=ssl.create_default_context())
    connection.login(settings.user, settings.password)
    return connection


def _imap_date(day: date) -> str:
    # strftime dépend de la langue du serveur, IMAP exige l'anglais
    return f"{day.day:02d}-{IMAP_MONTHS[day.month - 1]}-{day.year}"


def fetch_new_messages(settings: ImapSettings, since: date, known: set[str], now: datetime,
                       connect=_connect) -> list[MailMessage]:
    """Lit les messages pas encore connus, en lecture seule."""
    connection = None
    try:
        connection = connect(settings)
        status, _ = connection.select(settings.folder, readonly=True)
        if status != "OK":
            raise MailError("dossier de mails introuvable")
        _, validity = connection.response("UIDVALIDITY")
        prefix = (validity[0].decode() if validity and validity[0] else "0") + ":"

        status, data = connection.uid("SEARCH", None, "SINCE", _imap_date(since))
        if status != "OK":
            raise MailError("recherche de mails refusée")
        uids = data[0].split() if data and data[0] else []

        messages = []
        for uid in [uid for uid in uids if prefix + uid.decode() not in known][-MAX_PER_READ:]:
            # BODY.PEEK : le mail ne passe pas à « lu »
            status, parts = connection.uid("FETCH", uid, f"(BODY.PEEK[]<0.{MAX_MESSAGE_BYTES}>)")
            raw = next((part[1] for part in parts or [] if isinstance(part, tuple)), None)
            if status == "OK" and raw:
                messages.append(parse_message(raw, prefix + uid.decode(), now))
        return messages
    except (imaplib.IMAP4.error, OSError) as exc:
        raise MailError(f"boîte inaccessible ({exc.__class__.__name__})") from None
    finally:
        if connection is not None:
            try:
                connection.logout()
            except (imaplib.IMAP4.error, OSError):
                pass


def lookback_start(today: date, days: int) -> date:
    return today - timedelta(days=days)
