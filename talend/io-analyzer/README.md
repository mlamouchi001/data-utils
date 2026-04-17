# Talend I/O Analyzer

Analyse un job Talend et **tous ses sous-jobs** (`tRunJob`, `tRunJobOnGrid`) pour produire un rapport clair des fichiers / répertoires en **entrée** et en **sortie**, avec résolution des variables de contexte (`context.XXX`).

## Fonctionnalités

- ✅ Parcours récursif des `tRunJob` / `tRunJobOnGrid`
  - Format plat : `<elementParameter name="PROCESS:PROCESS_TYPE_PROCESS" value="..."/>`
  - Format imbriqué : `<elementParameter name="PROCESS"><elementValue elementRef="PROCESS_TYPE_PROCESS" .../></elementParameter>`
  - Référence par ID interne Talend (résolue via les fichiers `.properties`)
- ✅ Résolution des références `context.XXX` (y compris imbriquées)
- ✅ Gestion des concaténations Java triviales : `"/tmp/" + context.DIR + "/file.csv"`
- ✅ Lecture des contextes à deux niveaux : embarqués dans le `.item` du job **et** externes (dossier `context/`)
- ✅ Sélection de l'environnement : `dev` / `homol` / `prod` / `DEV` / `UAT` / `PRD` / `Default` — insensible à la casse, validé contre les contextes détectés dans le projet
- ✅ `--list-contexts` pour découvrir les environnements disponibles
- ✅ `--all-contexts` pour générer un rapport par environnement en une commande (parfait pour comparer DEV / HOMOL / PROD)
- ✅ Catalogue étendu de composants : fichiers locaux, FTP/SFTP, GCS, S3, Azure Storage, HDFS
- ✅ 3 formats de sortie : console, Markdown (Confluence-ready), JSON
- ✅ Mode `--verbose` pour diagnostiquer la résolution des sous-jobs

## Prérequis

- Python 3.10 ou supérieur
- Aucune dépendance externe (stdlib uniquement)

## Installation

```bash
# Depuis la racine du repo
cd talend/io-analyzer
# Rien à installer, stdlib only
python talend_io_analyzer.py --help
```

## Usage

### Syntaxe générale

```bash
python talend_io_analyzer.py \
    --project <RÉPERTOIRE_PROJET_TALEND> \
    --job <NOM_DU_JOB> \
    [--context DEV|UAT|PRD|Default] \
    [--format text|markdown|json] \
    [--output <CHEMIN_SORTIE>]
```

### Arguments

| Argument | Court | Requis | Description |
|---|---|---|---|
| `--project` | `-p` | oui | Répertoire racine du projet Talend (contient `process/`, `context/`, ...) |
| `--job` | `-j` | oui* | Nom du job à analyser (sans le suffixe `_x.y`). *Optionnel avec `--list-contexts` seul. |
| `--context` | `-c` | non | Environnement : `dev` / `homol` / `prod` / `DEV` / `UAT` / `PRD` / `Default` ... — insensible à la casse, validé contre les contextes détectés |
| `--list-contexts` | `-l` | non | Liste les environnements disponibles et sort |
| `--all-contexts` |  | non | Génère un rapport par environnement détecté (fichiers `rapport_<job>_<env>.<ext>`) |
| `--format` | `-f` | non | `text` (défaut), `markdown` ou `json` |
| `--output` | `-o` | non | Fichier de sortie pour markdown/json (ignoré avec `--all-contexts`) |
| `--verbose` | `-v` | non | Mode diagnostic : résolution des sous-jobs (stderr) |

### Exemples

```bash
# Lister les contextes disponibles dans le projet
python talend_io_analyzer.py -p ~/workspaces/PROJ_DLZ --list-contexts

# Lister les contextes spécifiques à un job (embarqués + externes)
python talend_io_analyzer.py -p ~/workspaces/PROJ_DLZ -j MAIN_JOB -l

# Rapport console pour l'environnement HOMOL (insensible à la casse)
python talend_io_analyzer.py -p ~/workspaces/PROJ_DLZ -j MAIN_JOB -c homol

# Rapport Markdown pour PROD, prêt pour Confluence
python talend_io_analyzer.py \
    -p ~/workspaces/PROJ_DLZ \
    -j MAIN_JOB \
    -c prod \
    -f markdown \
    -o docs/io_MAIN_JOB_prod.md

# Un rapport par environnement (dev + homol + prod) en une seule commande
python talend_io_analyzer.py \
    -p ~/workspaces/PROJ_DLZ \
    -j MAIN_JOB \
    --all-contexts \
    -f markdown
# => génère rapport_MAIN_JOB_dev.md, rapport_MAIN_JOB_homol.md, rapport_MAIN_JOB_prod.md

# Export JSON pour comparaison automatisée entre environnements
python talend_io_analyzer.py \
    -p ~/workspaces/PROJ_DLZ \
    -j MAIN_JOB \
    --all-contexts \
    -f json
```

## Exemple de sortie

### Format `text`

```
================================================================================
          RAPPORT D'ANALYSE DES ENTRÉES / SORTIES TALEND
================================================================================

[JOB PRINCIPAL] MAIN_JOB
  📄 fichier : /workspace/PROJ_DLZ/process/MAIN_JOB_0.1.item
  🔗 appelle : LOAD_CUSTOMERS, EXPORT_REPORT

  ▼ ENTRÉES (2)
    • [tFileInputDelimited] tFileInputDelimited_1
        FILENAME: /data/input/customers.csv
          ↳ brut: context.INPUT_DIR + "/customers.csv"
    • [tFTPGet] tFTPGet_1
        REMOTE_DIRECTORY: /incoming/daily/
        LOCAL_DIRECTORY: /data/landing/

  ▲ SORTIES (1)
    • [tFileOutputDelimited] tFileOutputDelimited_1
        FILENAME: /data/output/processed_customers.csv
          ↳ brut: context.OUTPUT_DIR + "/processed_customers.csv"
--------------------------------------------------------------------------------

[SOUS-JOB] LOAD_CUSTOMERS
  ...

📊 SYNTHÈSE GLOBALE
  Total jobs analysés : 3
  Entrées uniques     : 5
  Sorties uniques     : 4
```

### Format `markdown`

Génère des tableaux directement injectables dans Confluence :

```markdown
## Job principal : `MAIN_JOB`

- Fichier : `/workspace/PROJ_DLZ/process/MAIN_JOB_0.1.item`
- Sous-jobs appelés : `LOAD_CUSTOMERS`, `EXPORT_REPORT`

### Entrées

| Composant | Nom | Paramètre | Chemin résolu |
|---|---|---|---|
| tFileInputDelimited | tFileInputDelimited_1 | FILENAME | `/data/input/customers.csv` |
| tFTPGet | tFTPGet_1 | REMOTE_DIRECTORY | `/incoming/daily/` |
```

## Composants détectés

### Entrée

Fichiers locaux • `tFileInputDelimited`, `tFileInputXML`, `tFileInputJSON`, `tFileInputExcel`, `tFileInputPositional`, `tFileInputRegex`, `tFileInputFullRow`, `tFileInputRaw`, `tFileInputMSXML`, `tFileInputProperties`, `tFileInputLDIF`, `tFileInputARFF`, `tFileList`, `tFileExist`, `tFileUnarchive`, `tFileFetch`, `tFileCompare`, `tFileRowCount`, `tFileInputMail`

Transferts • `tFTPGet`, `tFTPFileExist`, `tFTPFileList`, `tFTPFileProperties`, `tSFTPGet`, `tSFTPFileExist`, `tSFTPFileList`

Cloud • `tGSGet`, `tGSList`, `tGSBucketExist`, `tS3Get`, `tS3List`, `tS3BucketExist`, `tAzureStorageGet`, `tAzureStorageList`

Hadoop • `tHDFSGet`, `tHDFSExist`, `tHDFSList`, `tHDFSGetProperties`, `tHDFSInput`, `tHDFSInputRaw`

### Sortie

Fichiers locaux • `tFileOutputDelimited`, `tFileOutputXML`, `tFileOutputJSON`, `tFileOutputExcel`, `tFileOutputPositional`, `tFileOutputRaw`, `tFileOutputMSXML`, `tFileOutputProperties`, `tFileOutputLDIF`, `tFileOutputARFF`, `tFileArchive`, `tFileTouch`

Transferts • `tFTPPut`, `tFTPRename`, `tFTPDelete`, `tSFTPPut`, `tSFTPRename`, `tSFTPDelete`

Cloud • `tGSPut`, `tGSDelete`, `tS3Put`, `tS3Delete`, `tAzureStoragePut`, `tAzureStorageDelete`

Hadoop • `tHDFSPut`, `tHDFSDelete`, `tHDFSOutput`, `tHDFSOutputRaw`

### Polyvalents (entrée ET sortie)

`tFileCopy`, `tFileDelete`, `tFileRename`, `tGSCopy`

## Extension

### Ajouter un composant

Éditer les sets dans le script :

```python
INPUT_COMPONENTS.add("tMonComposantCustom")
OUTPUT_COMPONENTS.add("tAutreComposant")
```

### Ajouter un nom de paramètre

Si un composant utilise un attribut XML inhabituel (ex. `SOURCE_URI`) :

```python
PATH_PARAMETER_NAMES.add("SOURCE_URI")
```

### Supporter les bases de données

Pour capturer aussi `tDBInput` / `tDBOutput` (Snowflake, BigQuery, Teradata, etc.), ajouter les composants aux catalogues et les paramètres `TABLE`, `SCHEMA`, `DBNAME`, `DATASET` à `PATH_PARAMETER_NAMES`.

## Limites connues

- Les expressions Java complexes (ternaires, appels de méthode, concaténations avec variables non-contexte) ne sont pas évaluées — la valeur reste partiellement brute mais lisible.
- Les **Joblets** ne sont pas suivis récursivement (seulement `tRunJob` / `tRunJobOnGrid`).
- Les références `globalMap.get("...")` ne sont pas résolues (valeurs calculées à l'exécution).
- Les contextes externes sont mergés avec les contextes embarqués, les embarqués étant prioritaires.

## Dépannage

**« Job introuvable dans le projet »**
→ Vérifier que `--project` pointe bien sur la racine du projet Talend (niveau contenant `process/`) et que le nom du job est exact (sensible aux majuscules selon le FS).

**Valeurs affichées comme `<context.XXX?>`**
→ La variable n'est définie dans aucun contexte chargé. Vérifier `--context` et la présence de la variable dans le `.item` du job ou dans le dossier `context/`.

**Chemin résolu bizarre avec des `+`**
→ Probablement une expression Java non triviale (ternaire, appel de méthode). La valeur brute reste affichée à côté pour investigation manuelle.

**Sous-jobs non détectés**
→ Relancer avec `-v` pour voir, sur stderr, comment chaque `tRunJob` est résolu. Trois formats sont gérés : plat (`PROCESS:PROCESS_TYPE_PROCESS`), imbriqué (`<elementValue elementRef="PROCESS_TYPE_PROCESS"/>`), et ID interne Talend (résolu via les `.properties`). Si un sous-job reste invisible, vérifier que son `.properties` est bien présent dans le périmètre de `--project`.

## Licence

Usage interne.
