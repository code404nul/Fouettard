# Module planning perso : configurer et redéployer

Le bot tourne depuis `/opt/fouettard` (service `fouettard`). Le dépôt cloné sur le
serveur sert de source : `deploy/install.sh` copie le code et installe les dépendances.
Il ne remplace jamais `/opt/fouettard/.env` ni `/opt/fouettard/data/`.

## Variables du `.env`

| Variable | Rôle | Si absente |
|---|---|---|
| `PLANNING_OWNER_ID` | Ton identifiant Discord | Module désactivé, le reste du bot tourne |
| `ZEUS_ICS_URL` | Flux ICS de Zeus (secret) | Source Zeus désactivée |
| `OUTLOOK_ICS_URL` | Flux ICS publié d'Outlook (secret) | Source Outlook désactivée |
| `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` | Boîte mail du domaine perso (secret) | Mails désactivés |
| `IMAP_PORT`, `IMAP_FOLDER` | Facultatifs (993, `INBOX`) | Valeurs par défaut |

## Obtenir le lien Outlook

Outlook web > Paramètres > Calendrier > Calendriers partagés > **Publier un calendrier** :
choisis ton calendrier, l'autorisation **« Peut afficher tous les détails »**, puis copie le
lien **ICS**. Avec « disponibilité seulement », tous les titres deviennent « Occupé ».

## Redéployer

Sur ton PC, envoie le code :

```bash
git add .
git status        # ni .env ni data/ ne doivent apparaître
git commit -m "Module planning perso"
git push
```

Sur le serveur :

```bash
ssh ubuntu@141.253.106.36
cd CHEMIN_DU_CLONE
git pull
sudo nano /opt/fouettard/.env     # ajoute les variables ci-dessus
sudo bash deploy/install.sh
sudo journalctl -u fouettard -f
```

Le log doit afficher `Module planning activé (sources : zeus, outlook).`
Le bot ne doit tourner qu'à un seul endroit à la fois : ne le lance pas aussi sur ton PC.

## Ajouter Outlook plus tard

```bash
sudo nano /opt/fouettard/.env     # OUTLOOK_ICS_URL=...
sudo systemctl restart fouettard
```

À la première lecture, le bot enregistre le calendrier sans envoyer d'alerte.

## Vérifier les mails

Dans le dossier du bot, cette commande teste la connexion et le tri sans lancer le bot.
Elle n'affiche que des nombres et des domaines, et ne marque aucun mail comme lu :

```bash
cd /opt/fouettard
sudo -u fouettard .venv/bin/python -m fouettard.planning.check_mail
```

Si un domaine utile est trié en bruit, ajoute-le à `mail_school_domains` dans `config/planning.yaml`.

## Changer un réglage

`config/planning.yaml` se modifie dans le dépôt, puis se redéploie comme ci-dessus. Certains
réglages se changent aussi en message privé : « objectif club 6h », « bloc minimum 45 »,
« créneau club jeudi 18h-20h ».
