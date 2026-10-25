#!/usr/bin/env bash
# Installe (ou met à jour) Fouettard sur une machine Ubuntu.
#
# Utilisation, depuis le dossier du bot copié sur la machine :
#     sudo bash deploy/install.sh
#
# Le fichier .env et la base (data/) déjà présents sur la machine
# ne sont jamais écrasés lors d'une mise à jour.

set -euo pipefail  # arrête le script à la première erreur

APP_DIR=/opt/fouettard
SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installation des paquets nécessaires"
apt-get update -q
apt-get install -y -q python3 python3-venv rsync

echo "==> Création de l'utilisateur système 'fouettard'"
id fouettard >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin fouettard

echo "==> Copie du code dans $APP_DIR"
mkdir -p "$APP_DIR"
# On n'écrase ni l'environnement Python, ni la base, ni le .env déjà sur la machine
rsync -a --delete \
    --exclude ".venv" --exclude "data" --exclude ".env" \
    --exclude "__pycache__" --exclude ".pytest_cache" \
    "$SOURCE_DIR/" "$APP_DIR/"
if [ -f "$SOURCE_DIR/.env" ] && [ ! -f "$APP_DIR/.env" ]; then
    cp "$SOURCE_DIR/.env" "$APP_DIR/.env"
fi
if [ ! -f "$APP_DIR/.env" ]; then
    echo "!! Pas de fichier .env : crée-le avec 'sudo nano $APP_DIR/.env' puis relance ce script."
    exit 1
fi

echo "==> Installation des dépendances Python"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> Droits : seul l'utilisateur 'fouettard' peut lire le .env"
chown -R fouettard:fouettard "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> Installation et (re)démarrage du service"
cp "$APP_DIR/deploy/fouettard.service" /etc/systemd/system/fouettard.service
systemctl daemon-reload
systemctl enable fouettard
systemctl restart fouettard

echo "==> Terminé. Logs en direct : sudo journalctl -u fouettard -f"
