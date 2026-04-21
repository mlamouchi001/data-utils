#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Talend Context Exporter
=======================
Lit les contextes d'un projet Talend et génère un CSV avec
deux colonnes : variable;valeur

Par défaut scanne :
  - les contextes externes du dossier context/
  - les contextes embarqués dans les .item de jobs

Exemples :
    # Export de tous les contextes du projet, tous environnements
    python talend_context_exporter.py -p ~/projet -o contexts.csv

    # Un seul environnement
    python talend_context_exporter.py -p ~/projet -c PROD -o ctx_prod.csv

    # Un seul job
    python talend_context_exporter.py -p ~/projet -j MON_JOB -o ctx_job.csv

    # Avec les colonnes détaillées (source + environnement)
    python talend_context_exporter.py -p ~/projet --detailed -o ctx.csv

    # Résoudre les références context.X -> valeur finale
    python talend_context_exporter.py -p ~/projet -c PROD --resolve -o ctx.csv

Compatible Python 3.10+
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

# Force UTF-8 sur stdout/stderr (Windows cp1252 crash dès qu'on pipe)
for _stream_name in ("stdout", "stderr"):
    _s = getattr(sys, _stream_name, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# --------------------------------------------------------------------------
# Helpers XML namespace-agnostic
# --------------------------------------------------------------------------
def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def iter_local(root: ET.Element, name: str):
    for elem in root.iter():
        if local_name(elem.tag) == name:
            yield elem


# --------------------------------------------------------------------------
# Découverte des fichiers
# --------------------------------------------------------------------------
def find_context_files(project_dir: Path) -> list[Path]:
    """Fichiers .item du dossier context/."""
    return [p for p in project_dir.rglob("*.item")
            if "/context/" in str(p).replace("\\", "/")]


def find_job_files(project_dir: Path) -> list[Path]:
    """Fichiers .item de jobs (hors context/)."""
    return [p for p in project_dir.rglob("*.item")
            if "/context/" not in str(p).replace("\\", "/")]


def find_job_item(project_dir: Path, job_name: str) -> Optional[Path]:
    """Trouve le .item d'un job donné."""
    pattern = re.compile(rf"^{re.escape(job_name)}(_\d+\.\d+)?\.item$",
                         re.IGNORECASE)
    for item in find_job_files(project_dir):
        if pattern.match(item.name):
            return item
    return None


def job_name_from_item(path: Path) -> str:
    """'MY_JOB_0.1.item' -> 'MY_JOB'."""
    name = re.sub(r"_\d+\.\d+\.item$", "", path.name)
    return re.sub(r"\.item$", "", name)


# --------------------------------------------------------------------------
# Extraction des contextes
# --------------------------------------------------------------------------
def extract_contexts_from_file(item_path: Path
                               ) -> dict[str, dict[str, str]]:
    """
    Lit un .item et retourne {env_name: {var_name: raw_value}}.
    Gère les formats avec et sans <context> englobant, et les namespaces.
    """
    out: dict[str, dict[str, str]] = defaultdict(dict)
    try:
        tree = ET.parse(item_path)
    except ET.ParseError as e:
        print(f"  ⚠ Erreur parsing {item_path.name}: {e}", file=sys.stderr)
        return out

    root = tree.getroot()

    # Format standard : <context name="ENV"><contextParameter .../></context>
    for ctx in iter_local(root, "context"):
        env = ctx.get("name", "Default")
        for cp in iter_local(ctx, "contextParameter"):
            pname = cp.get("name")
            pvalue = cp.get("value") or cp.get("rawValue") or ""
            if pname:
                out[env][pname] = pvalue

    # Format dégénéré : <contextParameter> directement sous la racine
    if not out:
        for cp in iter_local(root, "contextParameter"):
            pname = cp.get("name")
            pvalue = cp.get("value", "")
            if pname:
                out["Default"][pname] = pvalue

    return out


# --------------------------------------------------------------------------
# Résolution des valeurs (optionnelle)
# --------------------------------------------------------------------------
CONTEXT_REF_RE = re.compile(r"context\.(\w+)")


def resolve_value(raw: str, context: dict[str, str], depth: int = 0) -> str:
    """
    Résout une valeur Talend :
      - substitue context.XXX par sa valeur
      - évalue les concaténations Java triviales : "abc" + "def"
      - retire les guillemets entourant une chaîne simple
    """
    if raw is None:
        return ""
    if depth > 15:
        return raw

    value = raw.strip()

    def _sub(match: re.Match) -> str:
        ref = match.group(1)
        if ref in context:
            return resolve_value(context[ref], context, depth + 1)
        return f"<context.{ref}?>"

    value = CONTEXT_REF_RE.sub(_sub, value)

    if "+" in value:
        parts = re.split(r"\s*\+\s*", value)
        cleaned = []
        for p in parts:
            p = p.strip()
            if len(p) >= 2 and p.startswith('"') and p.endswith('"'):
                cleaned.append(p[1:-1])
            else:
                cleaned.append(p)
        value = "".join(cleaned)
    elif len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]

    return value


# --------------------------------------------------------------------------
# Collecte
# --------------------------------------------------------------------------
def collect_entries(project_dir: Path,
                    job_name: Optional[str],
                    context_env: Optional[str],
                    include_jobs: bool,
                    include_external: bool
                    ) -> list[dict]:
    """
    Retourne une liste d'entrées [{source_type, source_file, job_name,
    env, var_name, raw_value}].
    """
    entries: list[dict] = []

    # Si un job spécifique est demandé, on ignore les autres jobs
    if job_name:
        target = find_job_item(project_dir, job_name)
        if not target:
            sys.exit(f"❌ Job introuvable : {job_name}")
        jobs_to_scan = [target]
    else:
        jobs_to_scan = find_job_files(project_dir) if include_jobs else []

    # Contextes externes
    if include_external and not job_name:
        for ctx_path in find_context_files(project_dir):
            data = extract_contexts_from_file(ctx_path)
            for env, params in data.items():
                if context_env and env.lower() != context_env.lower():
                    continue
                for var, val in params.items():
                    entries.append({
                        "source_type": "external",
                        "source_file": ctx_path,
                        "job_name": "",
                        "env": env,
                        "var_name": var,
                        "raw_value": val,
                    })

    # Contextes embarqués dans les jobs
    for job_path in jobs_to_scan:
        data = extract_contexts_from_file(job_path)
        jname = job_name_from_item(job_path)
        for env, params in data.items():
            if context_env and env.lower() != context_env.lower():
                continue
            for var, val in params.items():
                entries.append({
                    "source_type": "embedded",
                    "source_file": job_path,
                    "job_name": jname,
                    "env": env,
                    "var_name": var,
                    "raw_value": val,
                })

    return entries


def build_resolver_context(entries: list[dict]) -> dict[str, str]:
    """Construit un dict {var_name: raw_value} pour la résolution des refs.
    En cas de collision (même var dans plusieurs envs), la dernière gagne.
    Appelé APRÈS filtrage par env pour que le context reflète l'env choisi."""
    ctx: dict[str, str] = {}
    for e in entries:
        ctx[e["var_name"]] = e["raw_value"]
    return ctx


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
def write_csv(entries: list[dict],
              out_path: Path,
              delimiter: str,
              detailed: bool,
              resolve: bool,
              dedupe: bool) -> int:
    """Écrit le CSV et retourne le nombre de lignes écrites (hors en-tête)."""
    resolver_ctx = build_resolver_context(entries) if resolve else {}

    if detailed:
        fieldnames = ["source_type", "source_file", "job_name",
                      "env", "variable", "valeur"]
        if resolve:
            fieldnames.append("valeur_resolue")
    else:
        fieldnames = ["variable", "valeur"]

    rows_written = []
    seen: set[tuple] = set()

    for e in entries:
        raw_value = e["raw_value"]
        resolved = resolve_value(raw_value, resolver_ctx) if resolve else None

        if detailed:
            row = {
                "source_type": e["source_type"],
                "source_file": str(e["source_file"]),
                "job_name": e["job_name"],
                "env": e["env"],
                "variable": e["var_name"],
                "valeur": raw_value,
            }
            if resolve:
                row["valeur_resolue"] = resolved
            # dedupe par tuple complet si demandé
            key = tuple(row.values()) if dedupe else None
        else:
            value_out = resolved if resolve else raw_value
            row = {"variable": e["var_name"], "valeur": value_out}
            key = (row["variable"], row["valeur"]) if dedupe else None

        if dedupe:
            if key in seen:
                continue
            seen.add(key)
        rows_written.append(row)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # utf-8-sig pour que Excel reconnaisse l'UTF-8 sans BOM manuel
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames,
                                delimiter=delimiter,
                                quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for row in rows_written:
            writer.writerow(row)

    return len(rows_written)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Exporte les variables de contexte d'un projet Talend en CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--project", "-p", required=True, type=Path,
                        help="Répertoire racine du projet Talend")
    parser.add_argument("--output", "-o", type=Path, default=Path("contexts.csv"),
                        help="Chemin du fichier CSV généré (défaut: contexts.csv)")
    parser.add_argument("--context", "-c", default=None,
                        help="Filtrer sur un environnement (DEV/HOMOL/PROD/...). "
                             "Insensible à la casse. Défaut: tous.")
    parser.add_argument("--job", "-j", default=None,
                        help="Limiter à un seul job (ses contextes embarqués). "
                             "Incompatible avec le scan global.")
    parser.add_argument("--no-external", action="store_true",
                        help="Ne pas inclure les contextes externes "
                             "(dossier context/).")
    parser.add_argument("--no-embedded", action="store_true",
                        help="Ne pas inclure les contextes embarqués dans les jobs.")
    parser.add_argument("--detailed", action="store_true",
                        help="Ajouter des colonnes: source_type, source_file, "
                             "job_name, env. Utile pour filtrer dans Excel.")
    parser.add_argument("--resolve", action="store_true",
                        help="Résoudre les références context.X et concaténations "
                             "Java triviales. Ajoute une colonne valeur_resolue "
                             "en mode --detailed.")
    parser.add_argument("--delimiter", "-d", default=";",
                        help="Séparateur CSV (défaut: ';'). Utiliser '\\t' pour TSV.")
    parser.add_argument("--no-dedupe", action="store_true",
                        help="Garder les doublons (par défaut, dédupliqué).")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Mode diagnostic")
    args = parser.parse_args()

    if not args.project.is_dir():
        sys.exit(f"❌ Le répertoire projet n'existe pas : {args.project}")

    if args.no_external and args.no_embedded and not args.job:
        sys.exit("❌ --no-external et --no-embedded sans --job : rien à scanner.")

    # Traduction des échappements du delimiter
    delimiter = args.delimiter.encode().decode("unicode_escape")
    if len(delimiter) != 1:
        sys.exit(f"❌ Délimiteur invalide : doit être un seul caractère.")

    scope = f"job '{args.job}'" if args.job else "projet entier"
    env_filter = f" (env={args.context})" if args.context else ""
    print(f"🔍 Export contextes : {scope}{env_filter}")

    entries = collect_entries(
        project_dir=args.project,
        job_name=args.job,
        context_env=args.context,
        include_jobs=not args.no_embedded,
        include_external=not args.no_external,
    )

    if args.verbose:
        print(f"  [debug] {len(entries)} entrées collectées avant "
              f"déduplication", file=sys.stderr)

    if not entries:
        msg = "Aucune variable de contexte trouvée"
        if args.context:
            msg += f" pour l'environnement '{args.context}'"
        sys.exit(f"⚠ {msg}.")

    n = write_csv(entries, args.output,
                  delimiter=delimiter,
                  detailed=args.detailed,
                  resolve=args.resolve,
                  dedupe=not args.no_dedupe)

    print(f"✓ {n} ligne(s) écrites dans : {args.output}")


if __name__ == "__main__":
    main()
