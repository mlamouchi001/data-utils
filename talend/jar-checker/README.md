# Talend JAR Dependency Checker

Analyse **tous les jobs** d'un projet Talend et liste, **pour chaque job**, les librairies JAR manquantes ou présentes dans une autre version.

## Comportement par défaut

```bash
python talend_jar_checker.py -p ~/projet
```

Le script :
- analyse **tous les jobs** du projet (tous les `.item` hors dossier `context/`)
- **trouve seul** les dépôts de librairies JAR sous le projet (aucun argument à fournir)
- produit un rapport **groupé par job** : pour chacun, on voit quels JAR manquent

## Auto-découverte des librairies

Le script cherche les JAR disponibles dans plusieurs emplacements typiques :

1. Dossiers nommés `lib/`, `libs/`, `library/`, `libraries/` à n'importe quelle profondeur du projet
2. Dossier `org.talend.designer.codegen.lib.java` (configuration Studio) si inclus dans le projet
3. **Fallback** : tout dossier qui contient au moins un fichier `.jar`

Si tes JAR sont stockés ailleurs, ajoute le dossier avec `--lib-dir` (répétable).

## Usage

```bash
python talend_jar_checker.py \
    --project <RACINE_PROJET> \
    [--lib-dir <DOSSIER_SUPP>]... \
    [--show-ok-jobs] [--missing-only] \
    [--format text|csv|json] [--output <FICHIER>]
```

### Arguments

| Argument | Court | Description |
|---|---|---|
| `--project` | `-p` | Racine du projet Talend (obligatoire) |
| `--lib-dir` | `-l` | Dossier de JAR supplémentaire à inclure. Répétable. Optionnel. |
| `--show-ok-jobs` |  | Lister aussi les jobs sans aucun problème de dépendance |
| `--missing-only` |  | (CSV uniquement) N'exporter que les statuts `missing` / `version_mismatch` |
| `--format` | `-f` | `text` (défaut), `csv`, `json` |
| `--output` | `-o` | Fichier de sortie pour CSV/JSON |
| `--delimiter` | `-d` | Séparateur CSV (défaut `;`) |
| `--verbose` | `-v` | Mode diagnostic (liste les dossiers de libs trouvés) |

### Exemples

```bash
# Cas le plus simple : tout le projet, rapport texte
python talend_jar_checker.py -p ~/projet

# Avec verbose pour voir où le script cherche les libs
python talend_jar_checker.py -p ~/projet -v

# Si certaines libs sont stockées hors du projet
python talend_jar_checker.py -p ~/projet -l ~/.Talend/lib/java

# Export CSV des manquants pour analyse Excel
python talend_jar_checker.py -p ~/projet -f csv --missing-only -o manquants.csv

# Export JSON pour intégration en pipeline CI
python talend_jar_checker.py -p ~/projet -f json -o jars.json
```

## Sources des JAR requis

Le script extrait les références JAR de chaque `.item` à partir de :
- Composants `tLibraryLoad` → paramètre `LIBRARY`
- Composants `tDB*` / `tJDBC*` → paramètre `DRIVER_JAR` (plat ou imbriqué dans `<elementValue>`)
- Tout `elementParameter` dont le nom contient `JAR` / `LIBRARY` / `MODULE` / `DRIVER` et dont la valeur termine par `.jar`
- Balises `<required moduleName="..."/>` et `<requiredIf>` (Talend ESB / Routes)
- Tout attribut `moduleName` ou `library` se terminant par `.jar` (catch-all)

## Exemple de sortie

```
🔍 Analyse du projet : ~/projet
📚 2 dépôt(s) de librairies trouvé(s)

================================================================================
========================= ANALYSE DES DÉPENDANCES JAR ==========================
================================================================================

Dépôts de librairies trouvés (2, 87 JAR indexés) :
  • ~/projet/lib
  • ~/projet/process/CUSTOM_JOB_0.1/lib

────────────────────────────────────────────────────────────────────────────────
📋 JOBS AVEC DÉPENDANCES MANQUANTES OU DIVERGENTES (3 / 47)
────────────────────────────────────────────────────────────────────────────────

📦 LOAD_REFERENTIEL
   Fichier : ~/projet/process/LOAD_REFERENTIEL_0.1.item
   Dépendances : 6 requise(s) (3 ok, 1 version différente, 2 manquante(s))

   ❌ MANQUANTS (2) :
      ✗ ojdbc8.jar
          via tLibraryLoad (tLibraryLoad_2)
      ✗ postgresql-42.7.1.jar
          via tJDBCInput (tJDBCInput_1)

   ⚠️  VERSION DIFFÉRENTE (1) :
      ≈ commons-lang3-3.12.0.jar
          via <required> ((global))
          ↳ disponible : commons-lang3-3.14.0.jar

📦 EXPORT_REPORT
   Fichier : ~/projet/process/EXPORT_REPORT_0.1.item
   Dépendances : 2 requise(s) (0 ok, 1 version différente, 1 manquante(s))

   ❌ MANQUANTS (1) :
      ✗ poi-5.2.3.jar
          via tFileInputXLS (tFileInputXLS_1)

[...]

================================================================================
📊 SYNTHÈSE GLOBALE
  Jobs analysés                   : 47
  Jobs avec dépendances manquantes: 3
  Total dépendances requises      : 124
  JARs uniques manquants          : 5
```

## Format CSV de sortie

```csv
job;job_file;jar_required;status;component;unique_name;source
LOAD_REFERENTIEL;process/LOAD_REFERENTIEL_0.1.item;ojdbc8.jar;missing;tLibraryLoad;tLibraryLoad_2;node/LIBRARY
LOAD_REFERENTIEL;process/LOAD_REFERENTIEL_0.1.item;commons-lang3-3.12.0.jar;version_mismatch;<required>;(global);required
EXPORT_REPORT;process/EXPORT_REPORT_0.1.item;poi-5.2.3.jar;missing;tFileInputXLS;tFileInputXLS_1;node/MODULE_NAME
```

Une ligne par couple **(job, JAR requis)**. Avec `--missing-only`, seuls les statuts `missing` et `version_mismatch` sont exportés.

## Statuts possibles

| Statut | Signification |
|---|---|
| `exact` | Le JAR avec le nom exact (incluant la version) est trouvé |
| `version_mismatch` | Un JAR avec le même nom de base mais une version différente est disponible |
| `missing` | Aucun JAR correspondant trouvé |

### Heuristique de matching tolérant aux versions

Pour `mysql-connector-java-8.0.30.jar` requis :
1. Match exact d'abord : on cherche `mysql-connector-java-8.0.30.jar`
2. Sinon, on supprime le suffixe de version (`-8.0.30`) → `mysql-connector-java`
3. On cherche tous les JAR dont le nom de base (sans version) correspond → résultat = liste des versions disponibles

Pattern de version reconnu : `[-_]X.Y[.Z]+` éventuellement suivi de `-SNAPSHOT` / `-RELEASE` / etc.

## Code de retour

- **0** : aucun job avec dépendance manquante
- **1** : au moins un job a une dépendance manquante (les `version_mismatch` ne déclenchent pas d'erreur)

→ Utilisable directement en CI pour bloquer un build si une dépendance manque.

## Limites connues

- **Détection par regex sur le XML** : les composants qui chargent des JAR uniquement via du code Java pur (`tJava` avec `Class.forName(...)` sans `tLibraryLoad`) ne sont pas détectés.
- **Pas de résolution Maven** : si ton projet utilise un build Maven (`pom.xml`), ce script ne lit pas les dépendances du POM. Il scanne uniquement le contenu des `.item`.
- **Auto-discovery best-effort** : le script déduit les dossiers de libs à partir de leur nom ou de la présence de `.jar`. Pour une configuration atypique, utiliser `--lib-dir`.
- **Versionning custom** : les versions au format `X.Y.Z` sont reconnues. Pour des versionning par date/hash, le matching exact reste fonctionnel mais le mode tolérant peut ne pas reconnaître la similarité.

## Dépannage

**Tous les JAR signalés comme manquants alors que le projet fonctionne**
→ Lancer avec `-v` pour voir quels dossiers ont été détectés. Si la liste est vide ou incomplète, ajouter manuellement avec `--lib-dir`.

**Trop de dossiers détectés (faux positifs)**
→ Le fallback "tout dossier avec ≥1 .jar" peut ramasser des dépendances de tests. Le rapport reste correct (plus de candidats = plus de chances de matcher), mais la liste des dépôts peut paraître bavarde.

**Versions différentes alors que le projet tourne**
→ La majorité des JAR rétrocompatibles (commons-*, log4j 2.x) tolèrent un upgrade mineur. Le statut `version_mismatch` est informatif, pas bloquant.

**`tFileInputXLS` / `tFileInputJSON` apparaissent comme manquants**
→ Talend télécharge ces JAR à la première exécution s'ils ne sont pas présents dans le dépôt. Le script signale leur absence physique du dépôt scanné.
