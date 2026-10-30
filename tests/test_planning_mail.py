"""Mails : lecture d'un message, tri, dates, accès IMAP (faux serveur) et intégration au brief."""

import asyncio
import imaplib
import logging
import ssl
import traceback
from datetime import date, datetime, timedelta
from email.message import EmailMessage

import httpx
import pytest

from fouettard.database import Database
from fouettard.planning import mail
from fouettard.planning.config import ImapSettings, PlanningConfig, load_planning_config
from fouettard.planning.mail import (A_SAVOIR, A_TRAITER, BRUIT, MailError, MailMessage, MailRules,
                                     classify, fetch_new_messages, find_due_date, is_urgent,
                                     parse_message)
from fouettard.planning.messages import safe_text
from fouettard.planning.service import PlanningService
from fouettard.planning.store import PlanningStore
from fouettard.timeutils import PARIS

NOW = datetime(2026, 11, 9, 10, tzinfo=PARIS)  # un lundi
PASSWORD = "m0t-de-passe-secret"
SETTINGS = ImapSettings("imap.example", "moi@example", PASSWORD)
RULES = MailRules(school_domains=["epita.fr"], prof_senders=["prof@gmail.com"],
                  noise_domains=["linkedin.com"], noise_keywords=["newsletter"],
                  action_keywords=["à rendre", "merci de"], urgent_keywords=["urgent", "annulé"])


def raw_mail(subject="Sujet", sender="Fanny F <fanny@epita.fr>", body="Bonjour", html=None, headers=None,
             date_header="Mon, 09 Nov 2026 09:00:00 +0100"):
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = subject
    if date_header:
        message["Date"] = date_header
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")
    return message.as_bytes()


def make(subject="Sujet", sender="fanny@epita.fr", text="", bulk=False, received=NOW):
    return MailMessage("k", sender, "", subject, received, text, bulk)


# --- Lecture d'un message ------------------------------------------------------

def test_parse_decodes_accents_and_date():
    message = parse_message(raw_mail("Échéance du TD", "Élodie <E.Lodie@EPITA.fr>"), "k", NOW)
    assert message.subject == "Échéance du TD"
    assert message.sender == "e.lodie@epita.fr"
    assert message.sender_name == "Élodie"
    assert message.received == datetime(2026, 11, 9, 9, tzinfo=PARIS)


def test_parse_html_only_mail_and_removes_scripts():
    raw = (b"From: a@epita.fr\r\nSubject: x\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
           b"<style>p{}</style><p>Rendu avant&nbsp;jeudi</p><script>evil()</script>")
    text = parse_message(raw, "k", NOW).text
    assert "Rendu avant" in text and "jeudi" in text and "evil" not in text


def test_parse_ignores_quoted_replies():
    body = "Merci !\n> à rendre avant jeudi\nLe 8 nov. 2026, Prof a écrit :\nà rendre avant vendredi"
    text = parse_message(raw_mail(body=body), "k", NOW).text
    assert "jeudi" not in text and "vendredi" not in text and "Merci" in text


@pytest.mark.parametrize("raw", [
    b"From: a@b.fr\r\nSubject: x\r\nContent-Type: text/plain; charset=inconnu-42\r\n\r\ncorps",
    b"",
    b"\xff\xfe pas un mail du tout",
    b"From: a@b.fr\r\nDate: pas une date\r\nContent-Transfer-Encoding: base64\r\n\r\n!!!",
])
def test_broken_mail_gives_partial_result(raw):
    assert parse_message(raw, "k", NOW).received == NOW


def test_mailing_list_headers_mark_bulk():
    assert parse_message(raw_mail(headers={"List-Unsubscribe": "<mailto:x@y>"}), "k", NOW).bulk
    assert parse_message(raw_mail(headers={"Precedence": "bulk"}), "k", NOW).bulk
    assert not parse_message(raw_mail(), "k", NOW).bulk


# --- Tri -------------------------------------------------------------------------

@pytest.mark.parametrize("message, due, expected", [
    (make("Projet à rendre"), None, A_TRAITER),
    (make("Info", text="Merci de répondre"), None, A_TRAITER),
    (make("Salle"), date(2026, 11, 12), A_TRAITER),
    (make("Info générale"), None, A_SAVOIR),
    (make("Lettre de l'école", bulk=True), None, A_SAVOIR),        # l'école n'est jamais du bruit
    (make("Tp", sender="prof@gmail.com", text="à rendre"), None, A_TRAITER),
    (make("Bonjour", sender="ami@gmail.com"), None, A_SAVOIR),
    (make("Dossier à rendre", sender="ami@gmail.com"), None, A_TRAITER),
    (make("Salut", sender="ami@gmail.com", text="Merci de répondre"), None, A_SAVOIR),
    (make("Offre", sender="x@news.linkedin.com"), None, BRUIT),
    (make("Offre", sender="x@shop.fr", bulk=True), None, BRUIT),
    (make("Compte", sender="no-reply@shop.fr"), None, BRUIT),
    (make("Notre newsletter", sender="x@shop.fr"), None, BRUIT),
])
def test_classify(message, due, expected):
    assert classify(message, RULES, due) == expected


def test_keyword_is_not_found_inside_another_word():
    rules = MailRules(action_keywords=["rendu"])
    assert classify(make("Compte-rendus"), rules) == A_SAVOIR
    assert classify(make("Compte rendu"), rules) == A_TRAITER


@pytest.mark.parametrize("message, due, expected", [
    (make("URGENT : salle"), None, True),
    (make("Cours annulé"), None, True),
    (make("Info"), date(2026, 11, 10), True),
    (make("Info"), date(2026, 11, 20), False),
    (make("Info", text="urgent dans le corps"), None, False),
    (make("URGENT : payez", sender="arnaque@spam.fr"), None, False),
])
def test_is_urgent(message, due, expected):
    assert is_urgent(message, RULES, due) is expected


# --- Dates écrites -------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("À rendre avant jeudi.", date(2026, 11, 12)),
    ("Date limite : 15/12", date(2026, 12, 15)),
    ("deadline le 6 décembre", date(2026, 12, 6)),
    ("à faire pour demain", date(2026, 11, 10)),
    ("D'ici vendredi merci", date(2026, 11, 13)),
    ("Soit avant jeudi, soit pour demain", date(2026, 11, 10)),
    ("à rendre du 6 au 8 décembre", date(2026, 12, 8)),
    ("Bonjour, jeudi on se voit", None),
    ("Pour information. Jeudi cours normal.", None),
    ("avant le 31/02", None),
])
def test_find_due_date(text, expected):
    assert find_due_date(text, date(2026, 11, 9)) == expected


def test_dates_are_counted_from_reception_not_today():
    assert find_due_date("à rendre avant jeudi", date(2026, 11, 17)) == date(2026, 11, 19)


# --- Texte extérieur -----------------------------------------------------------

def test_safe_text_neutralizes_markdown_mentions_and_length():
    text = safe_text("**gras** @everyone [lien](http://x)\n# titre `c`")
    assert "\n" not in text
    for char in ("*", "@", "[", "(", "`", "#"):
        assert f"\\{char}" in text
    assert len(safe_text("x" * 500)) == 90


# --- Accès IMAP ------------------------------------------------------------------

class FakeImap:
    def __init__(self, mails, validity=b"77"):
        self.mails, self.validity = mails, validity
        self.calls, self.logged_out, self.readonly = [], False, None

    def select(self, folder, readonly=False):
        self.readonly = readonly
        return "OK", [b"1"]

    def response(self, name):
        return name, [self.validity]

    def uid(self, command, *args):
        self.calls.append((command, args))
        if command == "SEARCH":
            return "OK", [b" ".join(str(uid).encode() for uid in self.mails)]
        uid = int(args[0])
        return "OK", [(b"1 (BODY[] {10}", self.mails[uid]), b")"]

    def logout(self):
        self.logged_out = True


def fetch(fake, known=frozenset()):
    return fetch_new_messages(SETTINGS, date(2026, 11, 2), set(known), NOW, connect=lambda _: fake)


def test_fetch_is_read_only_and_never_marks_as_read():
    fake = FakeImap({1: raw_mail("A"), 2: raw_mail("B")})
    assert [message.subject for message in fetch(fake)] == ["A", "B"]
    assert fake.readonly is True
    assert fake.logged_out
    fetches = [args for command, args in fake.calls if command == "FETCH"]
    assert fetches and all("BODY.PEEK[]" in args[1] for args in fetches)
    assert {command for command, _ in fake.calls} == {"SEARCH", "FETCH"}  # jamais STORE


def test_search_date_is_in_english_whatever_the_locale():
    fake = FakeImap({})
    fetch(fake)
    assert fake.calls[0][1] == (None, "SINCE", "02-Nov-2026")
    assert mail._imap_date(date(2027, 8, 1)) == "01-Aug-2027"


def test_known_mails_are_not_downloaded_again():
    fake = FakeImap({1: raw_mail("A"), 2: raw_mail("B")})
    assert [message.subject for message in fetch(fake, {"77:1"})] == ["B"]
    assert [args[0] for command, args in fake.calls if command == "FETCH"] == [b"2"]


def test_new_uid_validity_means_new_keys():
    assert fetch(FakeImap({1: raw_mail("A")}, validity=b"78"), {"77:1"})


def test_read_is_capped_to_newest_mails():
    fake = FakeImap({uid: raw_mail(str(uid)) for uid in range(1, 80)})
    subjects = [message.subject for message in fetch(fake)]
    assert len(subjects) == mail.MAX_PER_READ and subjects[-1] == "79"


@pytest.mark.parametrize("error", [
    imaplib.IMAP4.error(f"LOGIN failed for {PASSWORD}"),
    OSError(f"timeout {PASSWORD}"),
    ssl.SSLCertVerificationError(f"bad cert {PASSWORD}"),
])
def test_errors_never_contain_the_password(error):
    def connect(_):
        raise error
    with pytest.raises(MailError) as caught:
        fetch_new_messages(SETTINGS, date(2026, 11, 2), set(), NOW, connect=connect)
    assert PASSWORD not in "".join(traceback.format_exception(caught.value))


def test_password_is_hidden_from_repr():
    assert PASSWORD not in repr(SETTINGS)
    config = PlanningConfig(owner_id=1, urls={"zeus": "https://secret"}, database_path=":memory:", imap=SETTINGS)
    assert PASSWORD not in repr(config) and "secret" not in repr(config)


def test_connection_checks_the_server_certificate(monkeypatch):
    seen = {}

    class FakeSsl:
        def __init__(self, host, port, ssl_context=None, timeout=None):
            seen.update(context=ssl_context, timeout=timeout)

        def login(self, user, password):
            pass

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeSsl)
    mail._connect(SETTINGS)
    assert seen["context"].verify_mode == ssl.CERT_REQUIRED
    assert seen["context"].check_hostname and seen["timeout"]


def test_imap_config_is_optional(monkeypatch):
    monkeypatch.setenv("PLANNING_OWNER_ID", "1")
    for name in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "IMAP_PORT", "IMAP_FOLDER"):
        monkeypatch.delenv(name, raising=False)
    assert load_planning_config().imap is None
    monkeypatch.setenv("IMAP_HOST", "imap.example")
    monkeypatch.setenv("IMAP_USER", "moi")
    assert load_planning_config().imap is None
    monkeypatch.setenv("IMAP_PASSWORD", "x")
    settings = load_planning_config().imap
    assert (settings.port, settings.folder) == (993, "INBOX")


# --- Intégration au brief ----------------------------------------------------------

class EmptyGitLab:
    async def get_issues(self, state="all"):
        return []

    async def get_merge_requests(self, state="opened"):
        return []

    async def get_milestones(self, state="active"):
        return []


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setenv("PLANNING_OWNER_ID", "1")
    monkeypatch.setenv("IMAP_HOST", "imap.example")
    monkeypatch.setenv("IMAP_USER", "moi")
    monkeypatch.setenv("IMAP_PASSWORD", PASSWORD)
    monkeypatch.delenv("ZEUS_ICS_URL", raising=False)
    monkeypatch.delenv("OUTLOOK_ICS_URL", raising=False)
    config = load_planning_config()
    config.database_path = ":memory:"
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    return PlanningService(config, PlanningStore(":memory:"), EmptyGitLab(), Database(":memory:"), [], http)


def serve(monkeypatch, *mails):
    """Le faux serveur renvoie ces mails, ou lève MailError si on lui passe une exception."""
    def fake(settings, since, known, now):
        if mails and isinstance(mails[0], Exception):
            raise mails[0]
        return [message for message in mails if message.uid_key not in known]
    monkeypatch.setattr(mail, "fetch_new_messages", fake)


def received(uid, subject, sender="fanny@epita.fr", text="", when=NOW):
    return MailMessage(uid, sender, "Fanny", subject, when, text)


def refresh(service, now=NOW):
    return asyncio.run(service.refresh_mail(now))


def brief(service, now=NOW):
    return asyncio.run(service.build_brief(now))


def test_first_read_never_alerts_but_later_urgent_mail_does(service, monkeypatch):
    serve(monkeypatch, received("1", "URGENT : ancien"))
    assert refresh(service) == []
    serve(monkeypatch, received("1", "URGENT : ancien"), received("2", "URGENT : cours annulé"),
          received("3", "Info tranquille"))
    alerts = refresh(service)
    assert len(alerts) == 1 and "cours annulé" in alerts[0] and "Fanny" in alerts[0]
    assert refresh(service) == []  # une seule fois


def test_old_urgent_mail_found_late_does_not_alert(service, monkeypatch):
    serve(monkeypatch)
    refresh(service)
    serve(monkeypatch, received("1", "URGENT", when=NOW - timedelta(days=3)))
    assert refresh(service) == []


def test_brief_sections_buttons_and_noise(service, monkeypatch):
    serve(monkeypatch, received("1", "TD à rendre avant jeudi"), received("2", "Rentrée des clubs"),
          received("3", "Soldes", sender="x@shop.fr", text="newsletter du mois"))
    refresh(service)
    reply = brief(service)
    assert "**Mails à traiter**" in reply.content and "date repérée : jeu. 12/11" in reply.content
    assert "**Mails à savoir**" in reply.content and "Rentrée des clubs" in reply.content
    assert "Soldes" not in reply.content
    assert "Mails" in [button.label for button in reply.buttons]
    # « à savoir » une seule fois, « à traiter » jusqu'à ce que ce soit fait
    second = brief(service, NOW + timedelta(days=1)).content
    assert "Rentrée des clubs" not in second and "TD à rendre" in second


def test_noise_is_kept_only_as_an_identifier(service, monkeypatch):
    serve(monkeypatch, received("3", "Soldes secrets", sender="x@shop.fr", text="newsletter"))
    refresh(service)
    row = service.store._conn.execute("SELECT * FROM mails").fetchone()
    assert (row["subject"], row["sender"]) == ("", "")


def test_mail_body_is_never_stored(service, monkeypatch):
    serve(monkeypatch, received("1", "TD", text="contenu confidentiel à rendre avant jeudi"))
    refresh(service)
    row = dict(service.store._conn.execute("SELECT * FROM mails").fetchone())
    assert not any("confidentiel" in str(value) for value in row.values())


def test_mark_handled_and_add_to_plan_with_confirmation(service, monkeypatch):
    serve(monkeypatch, received("1", "TD à rendre avant jeudi"), received("2", "Merci de répondre"))
    refresh(service)
    reply = asyncio.run(service.handle_text("mails", NOW))
    actions = [(button.action, button.label) for button in reply.buttons]
    assert ("mail_add", "1 au planning") in actions or ("mail_add", "2 au planning") in actions
    add = next(button for button in reply.buttons if button.action == "mail_add")
    done = next(button for button in reply.buttons if button.action == "mail_done" and button.arg != add.arg)

    # Rien n'entre au planning sans clic
    assert service.store.tasks_for(NOW.date()) == []
    answer = asyncio.run(service.handle_action("mail_add", add.arg, NOW))[0].content
    assert "jeudi 12 novembre" in answer
    tasks = service.store.tasks_for(NOW.date())
    assert len(tasks) == 1 and tasks[0]["due_on"] == "2026-11-12"
    assert "déjà traité" in asyncio.run(service.handle_action("mail_add", add.arg, NOW))[0].content
    assert len(service.store.tasks_for(NOW.date())) == 1

    asyncio.run(service.handle_action("mail_done", done.arg, NOW))
    assert asyncio.run(service.handle_text("mails", NOW)).content == "Aucun mail à traiter."


def test_outage_keeps_stored_mails_and_is_said_once_a_day(service, monkeypatch, caplog):
    serve(monkeypatch, received("1", "TD à rendre"))
    refresh(service)
    serve(monkeypatch, MailError("boîte inaccessible (TimeoutError)"))
    with caplog.at_level(logging.DEBUG):
        assert refresh(service, NOW + timedelta(hours=1)) == []
    assert PASSWORD not in caplog.text
    content = brief(service, NOW + timedelta(hours=2)).content
    assert "TD à rendre" in content and "La boîte mail injoignable" in content
    assert "injoignable" not in brief(service, NOW + timedelta(hours=3)).content


def test_hostile_subject_cannot_format_the_brief(service, monkeypatch):
    serve(monkeypatch, received("1", "@everyone **à rendre** [clic](http://x)"))
    refresh(service)
    content = brief(service).content
    assert "@everyone" not in content.replace("\\@everyone", "")
    assert "**à rendre**" not in content


def test_without_imap_nothing_changes(service):
    service.config.imap = None
    assert refresh(service) == []
    reply = brief(service)
    assert "Mails" not in reply.content
    assert [button.label for button in reply.buttons] == ["Fait", "Reporter", "Semaine"]
    assert "pas configurée" in asyncio.run(service.handle_text("mails", NOW)).content
