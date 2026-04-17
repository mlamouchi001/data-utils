# Talend Filename Finder

Recherche inverse : à partir d'un **nom de fichier** (ou pattern), retrouve **où** il apparaît dans un projet Talend et **comment** il y est utilisé.

Complément direct du [`io-analyzer`](../io-analyzer/) : l'io-analyzer part d'un job et liste ses fichiers, le filename-finder part d'un fichier et liste les jobs.

## Ce qu'il trouve

Pour un pattern donné, le script remonte trois sections :

1. **Déclarations dans les contextes** — variables de contexte dont la valeur matche le pattern
   - Contextes externes (dossier `context/`)
   - Contextes embarqués dans les `.item` de jobs
   - Par environnement (DEV / HOMOL / PROD / ...)
2. **Usages directs** — jobs / composants qui ont le pattern **en dur** dans un paramètre (chemin hardcodé)
3. **Usages indirects via contexte** — jobs qui référencent une variable `context.X` où X apparaît dans la section 1

## Prérequis

Python 3.10+. Aucune dépendance externe.

## Usage

```bash
python talend_filename_finder.py \
    --project <RÉPERTOIRE_PROJET_TALEND> \
    --name <NOM_FICHIER_OU_PATTERN> \
    [--regex] [--case-sensitive] \
    [--format text|markdown|json] [--output <CHEMIN>]
```

### Arguments

| Argument | Court | Requis | Description |
|---|---|---|---|
| `--project` | `-p` | oui | Racine du projet Talend |
| `--name` | `-n` | oui | Nom de fichier ou pattern à chercher |
| `--regex` |  | non | Interpréter `--name` comme une regex Python (insensible à la casse sauf si `--case-sensitive`) |
| `--case-sensitive` |  | non | Recherche sensible à la casse (insensible par défaut) |
| `--format` | `-f` | non | `text` (défaut), `markdown`, `json` |
| `--output` | `-o` | non | Fichier de sortie pour markdown/json |
| `--verbose` | `-v` | non | Mode diagnostic (stderr) |

### Exemples

```bash
# Recherche simple (substring, insensible à la casse)
python talend_filename_finder.py -p ~/projet -n customers.csv

# Regex : tous les CSV
python talend_filename_finder.py -p ~/projet -n "\.csv" --regex

# Préfixe spécifique
python talend_filename_finder.py -p ~/projet -n "REF_" --regex

# Export Markdown pour Confluence
python talend_filename_finder.py -p ~/projet -n customers.csv -f markdown

# Export JSON pour traitement aval
python talend_filename_finder.py -p ~/projet -n customers.csv -f json
```

## Exemple de sortie

```
================================================================================
                            RECHERCHE : customers.csv
================================================================================

📋 DÉCLARATIONS DANS LES CONTEXTES (2 variable(s), 4 occurrence(s))
--------------------------------------------------------------------------------

  • Variable : INPUT_FILE
    Source    : ~/projet/context/CTX_GLOBAL_0.1.item
                contexte externe
    Contextes :
      - DEV        : "/dev/data/customers.csv"
      - HOMOL      : "/hom/data/customers.csv"
      - PROD       : "/prod/data/customers.csv"

  • Variable : LOCAL_FILE_NAME
    Source    : ~/projet/process/LOAD_WITH_EMBEDDED_CTX_0.1.item
                LOAD_WITH_EMBEDDED_CTX (embarqué dans le job)
    Contextes :
      - Default    : "customers.csv"

🎯 USAGES DIRECTS (1)
--------------------------------------------------------------------------------

  • Job       : ARCHIVE_CUSTOMERS
    Composant : tFileOutputDelimited / tFileOutputDelimited_1
    Paramètre : FILENAME
    Valeur    : "/tmp/backup/customers.csv"

🔗 USAGES INDIRECTS VIA CONTEXTE (2)
--------------------------------------------------------------------------------

  • Job       : LOAD_CUSTOMERS
    Composant : tFileInputDelimited / tFileInputDelimited_1
    Paramètre : FILENAME
    Expression: context.INPUT_FILE
    Variable(s): INPUT_FILE

📊 SYNTHÈSE
  Variables de contexte    : 2 (matches dans 4 environnement(s))
  Jobs avec usage direct   : 1
  Jobs avec usage indirect : 2
  Jobs concernés au total  : 3
```

## Périmètre de recherche

- **Tous les fichiers `.item`** du projet sont scannés (sauf ceux du dossier `context/` pour les usages — utilisés uniquement pour les déclarations).
- **Tous les paramètres** des composants sont inspectés (pas uniquement les paramètres de chemin). Ça permet de trouver un nom de fichier mentionné dans une requête SQL, un `tJava`, un header de `tLogRow`, etc.
- Matching **insensible à la casse** par défaut (utile pour Windows / chemins réseau).

## Limites connues

- Les valeurs sont recherchées telles qu'elles apparaissent dans le XML, **guillemets compris**. Avec `--regex`, attention aux ancres : `^.*\.csv$` ne matchera pas `"..../file.csv"` (qui finit par `"`). Préférer `\.csv` sans ancre.
- Les références transitives entre variables (`context.A = context.B`) ne sont pas suivies — seule la valeur littérale de la variable est examinée.
- Les `globalMap.get("...")` ne sont pas interprétés (valeurs d'exécution).

## Dépannage

**« UnicodeEncodeError » sur Windows**
→ Déjà corrigé : le script force `sys.stdout/stderr.reconfigure(encoding='utf-8')` à l'import.

**Rien n'est trouvé alors que le fichier existe**
→ Tester avec `-v` pour voir combien de contextes / jobs sont scannés. Vérifier que `--project` pointe bien sur la racine contenant `process/` et `context/`.

**Trop de bruit dans les résultats**
→ Utiliser `--regex` avec un pattern plus restrictif, ou `--case-sensitive` si la casse discrimine.
