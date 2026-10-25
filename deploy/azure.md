# Héberger Fouettard sur Azure

Le bot tourne sur une petite machine virtuelle (VM) Linux, en continu.
Il n'a besoin d'**aucun port ouvert** à part SSH pour l'administrer : c'est lui
qui contacte Discord et GitLab.

> ⚠️ Ne faites tourner le bot qu'à **un seul endroit à la fois** (pas sur un PC
> et sur Azure en même temps), sinon les commandes et alertes seront en double.

## 1. Créer la VM (portail Azure)

1. Activez **Azure for Students** (https://azure.microsoft.com/free/students) avec l'email de l'école.
2. Dans https://portal.azure.com : **Machines virtuelles > Créer > Machine virtuelle Azure**.
3. Réglages :
   - **Image** : Ubuntu Server 24.04 LTS
   - **Taille** : `Standard_B1s` (1 vCPU, 1 Go de RAM : largement suffisant)
   - **Région** : France Central (ou une autre région européenne)
   - **Authentification** : clé publique SSH, nom d'utilisateur `azureuser`.
     Téléchargez la clé `.pem` proposée et gardez-la précieusement.
   - **Ports d'entrée publics** : SSH (22) uniquement
4. Onglet **Gestion** : vérifiez que **l'arrêt automatique est désactivé**.
5. **Vérifier + créer**, puis notez l'**adresse IP publique** de la VM.

Conseil : dans **Réseau** de la VM, limitez la règle SSH à votre propre adresse IP.

## 2. Envoyer le bot sur la VM (depuis Windows, PowerShell)

Dans le dossier du bot (avec votre `.env` rempli) :

```powershell
# Archive du projet sans l'environnement Windows ni la base locale
tar --exclude=.venv --exclude=data --exclude=__pycache__ -czf ..\fouettard.tar.gz .

# Envoi sur la VM (remplacez IP_DE_LA_VM et le chemin de la clé)
scp -i C:\chemin\vers\cle.pem ..\fouettard.tar.gz azureuser@IP_DE_LA_VM:~
```

Le `.env` voyage dans l'archive ; `scp` chiffre le transfert.

## 3. Installer et démarrer

```powershell
ssh -i C:\chemin\vers\cle.pem azureuser@IP_DE_LA_VM
```

Puis, sur la VM :

```bash
mkdir -p fouettard && tar -xzf fouettard.tar.gz -C fouettard
cd fouettard
sudo bash deploy/install.sh
rm ~/fouettard.tar.gz   # l'archive contient le .env : on la supprime
```

Le bot démarre, et redémarre tout seul après un plantage ou un redémarrage de la VM.

## 4. Commandes utiles (sur la VM)

| Action | Commande |
|---|---|
| Voir les logs en direct | `sudo journalctl -u fouettard -f` |
| État du bot | `sudo systemctl status fouettard` |
| Redémarrer | `sudo systemctl restart fouettard` |
| Arrêter | `sudo systemctl stop fouettard` |
| Modifier la config | `sudo nano /opt/fouettard/.env` puis redémarrer |

## 5. Mettre à jour le bot

Refaites les étapes 2 et 3 avec la nouvelle version. Le script garde le `.env`
et la base `data/` déjà présents sur la VM.

## Notes

- **Heure** : la VM est en heure UTC, mais le bot calcule toujours ses dates en
  `Europe/Paris`. Rien à régler.
- **Coût** : surveillez votre crédit dans **Cost Management** du portail.
  Si le projet est terminé, **supprimez le groupe de ressources** de la VM pour
  ne plus rien consommer.
