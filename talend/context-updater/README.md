# Talend Context Updater

Met à jour les variables de contexte d'un job Talend **et de tous ses sous-jobs** (récursivement via `tRunJob`) à partir d'un fichier CSV.

Complément naturel du [`context-exporter`](../context-exporter/) : le cycle typique est **exporter → éditer dans Excel → réimporter**.

## ⚠️ Protections intégrées

Comme cet outil **modifie** les `.item` du projet, plusieurs garde-fous sont actifs par défaut :

- **Dry-run par défaut** : aucun fichier n'est modifié sans `--apply`. La simulation affiche ligne par ligne l'ancien et le nouveau.
- **Backup `.bak` automatique** avant toute écriture (désactivable avec `--no-backup`).
- **Variables absentes ignorées silencieusement** par défaut (ajoutées avec `--add-missing`, signalées avec `--warn-missing`).

## Comportement par défaut

Sans argument optionnel de ciblage, le script :
- modifie **tous les environnements** présents (DEV, HOMOL, PROD, ...)
- modifie **les contextes externes** (dossier `context/`) **ET** les **contextes embarqués** dans le job racine et ses sous-jobs
- ignore silencieusement les variables du CSV qui ne correspondent à aucune variable existante

Pour restreindre, utiliser `--context DEV`, `--no-external`, `--no-embedded` selon le besoin.

## Prérequis

Python 3.10+. Stdlib uniquement.

## Format CSV attendu

Le CSV doit contenir au minimum deux colonnes `variable` et `valeur`, avec `;` comme séparateur par défaut :

```csv
variable;valeur
INPUT_DIR;/prod/data/input
OUTPUT_DIR;/prod/data/output
BATCH_SIZE;10000
```

Le script accepte directement les CSV produits par `context-exporter`, y compris en mode `--detailed` (dans ce cas il lit la colonne `valeur_resolue` si elle existe, sinon `valeur`).

## Usage

```bash
python talend_context_updater.py \
    --project <RACINE_PROJET> \
    --job <JOB_RACINE> \
    --input <CSV> \
    (--context <ENV> | --all-envs) \
    [--apply] [--add-missing] [--no-backup] \
    [--no-external] [--no-embedded]
```

### Arguments

| Argument | Court | Requis | Description |
|---|---|---|---|
| `--project` | `-p` | oui | Racine du projet Talend |
| `--job` | `-j` | oui | Job racine. Ses sous-jobs sont traités récursivement via `tRunJob`. |
| `--input` | `-i` | oui | Fichier CSV des mises à jour |
| `--context` | `-c` | non | Environnement à cibler (DEV/HOMOL/PROD/...). Insensible à la casse. **Défaut : tous les environnements présents.** |
| `--all-envs` |  | non | Explicitement tous les environnements (redondant avec le défaut). |
| `--apply` |  | non | **SANS ce flag, dry-run**. Avec, les modifications sont écrites. |
| `--add-missing` |  | non | Créer les variables du CSV absentes dans le `.item`. Par défaut, elles sont ignorées silencieusement. |
| `--warn-missing` |  | non | Afficher un ⚠ pour chaque variable du CSV absente (par défaut : silencieux). |
| `--no-backup` |  | non | Désactiver la création des `.bak` |
| `--no-external` |  | non | Ne pas modifier les contextes externes (dossier `context/`) |
| `--no-embedded` |  | non | Ne pas modifier les contextes embarqués dans les jobs |
| `--delimiter` | `-d` | non | Séparateur CSV (défaut `;`) |
| `--verbose` | `-v` | non | Mode diagnostic |

### Exemples

```bash
# Le plus simple : dry-run sur tous les environnements
python talend_context_updater.py \
    -p ~/projet -j MAIN_JOB -i updates.csv

# Application réelle sur tous les environnements (défaut)
python talend_context_updater.py \
    -p ~/projet -j MAIN_JOB -i updates.csv --apply

# Cibler un seul environnement
python talend_context_updater.py \
    -p ~/projet -j MAIN_JOB -i updates.csv -c PROD --apply

# Ajouter les variables manquantes au passage
python talend_context_updater.py \
    -p ~/projet -j MAIN_JOB -i updates.csv --apply --add-missing

# Afficher les warnings sur les variables absentes (silencieux par défaut)
python talend_context_updater.py \
    -p ~/projet -j MAIN_JOB -i updates.csv --warn-missing

# Restreindre aux contextes embarqués uniquement
python talend_context_updater.py \
    -p ~/projet -j MAIN_JOB -i updates.csv --no-external --apply
```

## Cycle de travail recommandé

```bash
# 1. Export des contextes actuels en PROD (valeurs résolues pour édition facile)
python ../context-exporter/talend_context_exporter.py \
    -p ~/projet -c PROD --resolve -o ctx_prod.csv

# 2. Ouvrir ctx_prod.csv dans Excel, modifier les valeurs voulues
#    (ne garder que les lignes à changer — les variables non mentionnées
#    dans le CSV ne seront pas touchées)

# 3. Dry-run pour vérifier les changements prévus
python talend_context_updater.py \
    -p ~/projet -j MAIN_JOB -i ctx_prod.csv -c PROD

# 4. Application réelle
python talend_context_updater.py \
    -p ~/projet -j MAIN_JOB -i ctx_prod.csv -c PROD --apply
```

## Exemple de sortie (dry-run)

```
📥 Lecture du CSV : updates.csv
   3 variable(s) à mettre à jour
🔍 Collecte des fichiers (job racine : ROOT_JOB)
   3 fichier(s) à examiner

📄 process/ROOT_JOB_0.1.item
   Environnements présents : DEV, PROD
  [DRY-RUN] ✎ [PROD] INPUT_DIR
      ancien : "/prod/input_old"
      nouveau: "/new/input/path"
  ⚠ [PROD] NEW_VAR absente (utiliser --add-missing pour l'ajouter)

📄 process/CHILD_JOB_0.1.item
   Environnements présents : DEV, PROD
  [DRY-RUN] ✎ [PROD] OUTPUT_DIR
      ancien : "/prod/out_old"
      nouveau: "/new/output/path"

📄 context/GLOBAL_CTX_0.1.item
  [DRY-RUN] ✎ [PROD] INPUT_DIR
      ancien : "/prod/input"
      nouveau: "/new/input/path"
  [DRY-RUN] ✎ [PROD] OUTPUT_DIR
      ancien : "/prod/output"
      nouveau: "/new/output/path"

======================================================================
SYNTHÈSE [DRY-RUN]
  Fichiers concernés     : 3
  Variables modifiées    : 4
  Variables ajoutées     : 0
  Variables manquantes   : 2

  ℹ️  Pour appliquer réellement, relancer avec --apply
```

## Formatage des valeurs

Le script reformate automatiquement les valeurs selon les conventions Talend :

| Entrée CSV | Écrit dans le `.item` | Raison |
|---|---|---|
| `/prod/data` | `"/prod/data"` | Chaîne littérale → encadrée de guillemets |
| `"/prod/data"` | `"/prod/data"` | Déjà formatée, pas de double-encadrement |
| `context.OTHER_VAR` | `context.OTHER_VAR` | Expression, pas de guillemets |
| `"prefix_" + context.X` | `"prefix_" + context.X` | Concaténation, pas touchée |

## Revenir en arrière

En cas de problème après `--apply`, les fichiers `.bak` contiennent la version précédente :

```bash
# Restauration manuelle d'un fichier
mv chemin/JOB_0.1.item.bak chemin/JOB_0.1.item

# Restauration en masse (PowerShell)
Get-ChildItem -Recurse -Filter *.bak | ForEach-Object {
    Move-Item $_.FullName ($_.FullName -replace '\.bak$','') -Force
}

# Restauration en masse (bash)
find . -name "*.bak" -exec sh -c 'mv "$1" "${1%.bak}"' _ {} \;
```

## Limites connues

- **Pas de rollback automatique** si la modification d'un fichier échoue en cours de route : les fichiers déjà écrits restent modifiés. Les `.bak` permettent la restauration manuelle.
- Les contextes embarqués sont recherchés dans le job racine **et tous ses sous-jobs accessibles via `tRunJob`**. Les jobs orphelins (jamais appelés) ne sont pas mis à jour — par conception.
- L'ajout de variables (`--add-missing`) utilise `type="id_String"` par défaut. Pour des types spéciaux (Password, Directory, ...), éditer manuellement après coup.
- Le script préserve les namespaces XML mais peut reformater légèrement l'indentation du fichier. Diff avant commit si besoin de vérifier.

## Dépannage

**« Fichier CSV introuvable »**
→ Chemin relatif par rapport au répertoire courant, pas au projet.

**« Aucune mise à jour trouvée dans <csv> »**
→ Le CSV est vide ou n'a pas les bonnes colonnes. Le script cherche `variable` / `valeur` (ou `valeur_resolue` en mode détaillé). Vérifier les en-têtes et le délimiteur.

**« Job introuvable : X »**
→ Vérifier le nom exact du job (sans suffixe `_x.y`) et que `--project` pointe sur la racine qui contient `process/`.

**Variables absentes inattendues**
→ Utiliser `-v` pour voir la liste des variables du CSV et comparer avec ce qui existe réellement dans les contextes du projet via un export préalable.

**Excel a ajouté des quotes supplémentaires à l'enregistrement**
→ Enregistrer en "CSV UTF-8 (délimité par des points-virgules)" plutôt qu'en "CSV (Comma delimited)". Ou utiliser LibreOffice Calc qui gère mieux les conventions Talend.
