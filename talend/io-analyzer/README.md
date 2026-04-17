# data-utils

Collection de scripts utilitaires pour l'ingénierie de données — Talend, Snowflake, dbt, BigQuery, GCP, Python.

## Structure du dépôt

```
data-utils/
├── talend/
│   └── io-analyzer/        # Analyse des I/O d'un job Talend et de ses sous-jobs
├── snowflake/              # (à venir) Scripts Snowflake / dbt
├── gcp/                    # (à venir) Scripts BigQuery / Cloud Functions
└── common/                 # (à venir) Helpers Python transverses
```

Chaque outil vit dans son propre dossier avec son `README.md` et, si besoin, son `requirements.txt`.

## Conventions

- **Python 3.10+** par défaut sauf mention contraire dans le README de l'outil.
- **Aucun secret** dans le dépôt : credentials via variables d'environnement ou fichiers locaux ignorés par git.
- **Commits conventionnels** : `feat:`, `fix:`, `docs:`, `refactor:`, `chore:`.
- Un outil = un dossier = un `README.md` qui documente usage, arguments, exemples, limites.

## Ajouter un nouvel outil

1. Créer un dossier dans la catégorie appropriée (`talend/`, `snowflake/`, `gcp/`, `common/`).
2. Y déposer le script + un `README.md` suivant le modèle de `talend/io-analyzer/`.
3. Mettre à jour le tableau des outils ci-dessous.

## Outils disponibles

| Catégorie | Outil | Description |
|---|---|---|
| Talend | [`io-analyzer`](talend/io-analyzer/) | Extrait les fichiers/répertoires en entrée/sortie d'un job Talend et de ses sous-jobs, avec résolution des contextes. |
