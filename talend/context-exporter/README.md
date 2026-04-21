# Talend Context Exporter

Lit les variables de contexte d'un projet Talend et génère un CSV avec, par défaut, deux colonnes : `variable;valeur`.

## Ce qu'il fait

- Scanne les **contextes externes** du dossier `context/`
- Scanne les **contextes embarqués** dans les `.item` de jobs
- Permet de filtrer par environnement (DEV / HOMOL / PROD / ...) et/ou par job
- Peut **résoudre** les références `context.X` et les concaténations Java triviales pour obtenir la valeur finale prête à l'emploi
- Produit un CSV UTF-8 avec BOM reconnu par Excel, délimiteur `;` par défaut

## Prérequis

Python 3.10+. Stdlib uniquement.

## Usage

```bash
python talend_context_exporter.py \
    --project <RÉPERTOIRE_PROJET> \
    [--output <FICHIER.csv>] \
    [--context DEV|HOMOL|PROD|...] \
    [--job <NOM_JOB>] \
    [--resolve] [--detailed] \
    [--delimiter ';'] [--no-dedupe] \
    [--no-external] [--no-embedded]
```

### Arguments

| Argument | Court | Description |
|---|---|---|
| `--project` | `-p` | Racine du projet Talend (obligatoire) |
| `--output` | `-o` | Chemin du CSV généré (défaut: `contexts.csv`) |
| `--context` | `-c` | Filtrer sur un environnement (insensible à la casse). Défaut: tous. |
| `--job` | `-j` | Limiter aux contextes embarqués d'un seul job |
| `--resolve` |  | Résout `context.X` et évalue les concaténations Java, nettoie les guillemets |
| `--detailed` |  | Ajoute les colonnes `source_type`, `source_file`, `job_name`, `env` |
| `--delimiter` | `-d` | Séparateur CSV (défaut `;`). `\t` pour TSV. |
| `--no-dedupe` |  | Garde les doublons (par défaut, dédupliqué sur tuple complet) |
| `--no-external` |  | Ne pas inclure les contextes externes (dossier `context/`) |
| `--no-embedded` |  | Ne pas inclure les contextes embarqués dans les jobs |
| `--verbose` | `-v` | Mode diagnostic |

### Exemples

```bash
# Le plus simple : tout le projet, 2 colonnes, tous environnements
python talend_context_exporter.py -p ~/projet -o contexts.csv

# Un seul environnement, valeurs prêtes à l'emploi (sans guillemets Talend)
python talend_context_exporter.py -p ~/projet -c PROD --resolve -o ctx_prod.csv

# Un seul job (utile pour documenter un job précis)
python talend_context_exporter.py -p ~/projet -j LUD_AJU_ORCHESTRATION_DELTA_REO \
    --resolve -o ctx_job.csv

# Export détaillé pour analyse dans Excel (filtres par env, source, etc.)
python talend_context_exporter.py -p ~/projet --detailed --resolve -o ctx_full.csv

# Comparer DEV vs PROD d'un coup en mode détaillé
python talend_context_exporter.py -p ~/projet --detailed --resolve -o ctx_all.csv
# puis dans Excel, pivoter sur variable / env pour voir les écarts
```

## Formats de sortie

### Sortie par défaut (2 colonnes)

```csv
variable;valeur
INPUT_FILE;"/prod/data/customers.csv"
ARCHIVE_DIR;"/prod/archive"
LOCAL_FILE_NAME;"customers.csv"
```

### Avec `--resolve` (valeurs nettoyées)

```csv
variable;valeur
INPUT_FILE;/prod/data/customers.csv
ARCHIVE_DIR;/prod/archive
LOCAL_FILE_NAME;customers.csv
```

### Avec `--detailed --resolve`

```csv
source_type;source_file;job_name;env;variable;valeur;valeur_resolue
external;context/CTX_GLOBAL_0.1.item;;PROD;INPUT_FILE;"/prod/data/customers.csv";/prod/data/customers.csv
embedded;process/LOAD_CUSTOMERS_0.1.item;LOAD_CUSTOMERS;Default;LOCAL_FILE_NAME;"customers.csv";customers.csv
```

## Notes sur le format

- **Encodage** : UTF-8 avec BOM (`utf-8-sig`) pour qu'Excel le reconnaisse automatiquement sans se retrouver avec des caractères accentués cassés.
- **Valeurs brutes** : Talend stocke les chaînes littérales avec leurs guillemets. Sans `--resolve`, ces guillemets sont préservés dans le CSV — ce qui est utile pour distinguer une chaîne littérale (`"customers.csv"`) d'une expression (`context.X + "suffix"`).
- **Déduplication** : activée par défaut, évite les doublons dans les cas où une même variable a la même valeur dans plusieurs environnements ou plusieurs fichiers.

## Limites connues

- Les expressions Java complexes (ternaires, appels de méthode) ne sont pas entièrement évaluées avec `--resolve` — seules les concaténations `"a" + context.X + "b"` le sont. Les valeurs non résolues restent lisibles avec leurs parties brutes.
- La résolution utilise le dernier environnement rencontré en cas de collision sans filtre `--context`. Pour une résolution propre, combiner `--resolve` avec `--context`.
- Les `globalMap.get(...)` (valeurs d'exécution) ne sont pas interprétés.

## Dépannage

**« UnicodeEncodeError » sur Windows**
→ Corrigé : le script force `utf-8` sur stdout/stderr à l'import.

**Excel affiche des caractères bizarres**
→ Le CSV est écrit en UTF-8 avec BOM. Si Excel ne détecte toujours pas, ouvrir via `Données > Depuis un fichier texte` et choisir UTF-8 explicitement.

**Aucune entrée trouvée**
→ Vérifier avec `-v`. Si le projet n'utilise que des contextes embarqués, `--no-external` ne change rien. Si le projet n'utilise que des contextes externes, même remarque pour `--no-embedded`.
