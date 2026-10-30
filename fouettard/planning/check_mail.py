"""Vérifie la connexion IMAP et le tri, sans lancer le bot : python -m fouettard.planning.check_mail

N'affiche que des nombres et des domaines d'expéditeurs, jamais le contenu d'un mail.
La boîte reste en lecture seule et aucun mail n'est marqué comme lu.
"""

from collections import Counter

from fouettard import timeutils
from fouettard.config import load_config
from fouettard.planning.config import load_planning_config
from fouettard.planning.mail import (MailError, classify, fetch_new_messages, find_due_date,
                                     lookback_start)
from fouettard.planning.service import mail_rules


def main() -> None:
    load_config()
    config = load_planning_config()
    if config is None or config.imap is None:
        print("IMAP non configuré : il manque IMAP_HOST, IMAP_USER ou IMAP_PASSWORD dans le .env.")
        return
    now = timeutils.now()
    try:
        messages = fetch_new_messages(config.imap, lookback_start(now.date(), config.mail_lookback_days),
                                      set(), now)
    except MailError as exc:
        print(f"Échec : {exc}")
        return
    rules = mail_rules(config)
    categories = Counter(classify(message, rules, find_due_date(f"{message.subject}. {message.text}",
                                                                message.received.date()))
                         for message in messages)
    domains = Counter(message.sender.rpartition("@")[2] for message in messages)
    print(f"Connexion réussie : {len(messages)} mails lus sur {config.mail_lookback_days} jours.")
    print("Tri :", dict(categories))
    print("Domaines les plus fréquents :", domains.most_common(10))


if __name__ == "__main__":
    main()
