#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Talend Context Updater
======================
Met à jour les variables de contexte d'un job Talend et de ses sous-jobs
à partir d'un fichier CSV au format:

    variable;valeur
    INPUT_DIR;/prod/data/input
    OUTPUT_DIR;/prod/data/output

Complément du context-exporter : export -> édition dans Excel -> réimport.

PAR DÉFAUT LE SCRIPT EST EN DRY-RUN : il affiche ce qu'il modifierait
sans rien écrire. Passer --apply pour effectuer réellement les changements.

Exemples :
    # Simulation sur l'env PROD
    python talend_context_updater.py -p ~/projet -j MON_JOB -i ctx.csv -c PROD

    # Application réelle avec backup .bak
    python talend_context_updater.py -p ~/projet -j MON_JOB -i ctx.csv \\
        -c PROD --apply

    # Pour tous les environnements
    python talend_context_updater.py -p ~/projet -j MON_JOB -i ctx.csv \\
        --all-envs --apply

    # Ajouter les variables manquantes au lieu de les signaler
    python talend_context_updater.py -p ~/projet -j MON_JOB -i ctx.csv \\
        -c DEV --add-missing --apply

Compatible Python 3.10+
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

# Force UTF-8 sur stdout/stderr (Windows cp1252 crash quand on pipe)
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
# Gestion du format Talend XML (namespaces à préserver à l'écriture)
# --------------------------------------------------------------------------
def register_namespaces(xml_path: Path) -> None:
    """Pré-enregistre les namespaces d'un fichier XML pour que ET.write()
    conserve les préfixes originaux au lieu de générer ns0, ns1, ...
    """
    try:
        for event, (prefix, uri) in ET.iterparse(
                str(xml_path), events=["start-ns"]):
            ET.register_namespace(prefix, uri)
    except ET.ParseError:
        pass


def format_talend_value(raw: str) -> str:
    """
    Formate une valeur pour qu'elle soit valide dans un .item Talend.
    Les chaînes littérales doivent être entourées de guillemets :
      /prod/in       ->  "/prod/in"
      "/prod/in"     ->  "/prod/in"   (déjà ok)
      context.X      ->  context.X    (expression, pas de guillemets)
      "a" + context.X ->  "a" + context.X  (expression composite, ok)
    """
    if not raw:
        return raw
    s = raw.strip()

    # Déjà une chaîne entre guillemets simples sans concat
    if s.startswith('"') and s.endswith('"') and s.count('"') == 2:
        return s

    # Expression (contient context. ou globalMap. ou un + au top-level)
    # -> on laisse tel quel
    if "context." in s or "globalMap." in s or re.search(r"\w\s*\+\s*\w", s):
        return s

    # Sinon : chaîne littérale, on ajoute les guillemets
    # Échappe les guillemets internes
    escaped = s.replace('"', '\\"')
    return f'"{escaped}"'


# --------------------------------------------------------------------------
# Découverte des fichiers
# --------------------------------------------------------------------------
def find_context_files(project_dir: Path) -> list[Path]:
    return [p for p in project_dir.rglob("*.item")
            if "/context/" in str(p).replace("\\", "/")]


def find_job_files(project_dir: Path) -> list[Path]:
    return [p for p in project_dir.rglob("*.item")
            if "/context/" not in str(p).replace("\\", "/")]


def find_job_item(project_dir: Path, job_name: str) -> Optional[Path]:
    pattern = re.compile(rf"^{re.escape(job_name)}(_\d+\.\d+)?\.item$",
                         re.IGNORECASE)
    for item in find_job_files(project_dir):
        if pattern.match(item.name):
            return item
    return None


def build_id_to_name_map(project_dir: Path) -> dict[str, str]:
    """Map {id_talend: label} pour résoudre les tRunJob par ID."""
    mapping: dict[str, str] = {}
    for props in project_dir.rglob("*.properties"):
        if "/context/" in str(props).replace("\\", "/"):
            continue
        try:
            tree = ET.parse(props)
        except ET.ParseError:
            continue
        root = tree.getroot()

        rlabel = root.get("label")
        rid = root.get("id")
        if rlabel and rid:
            mapping[rid] = rlabel
            continue

        for prop in iter_local(root, "Property"):
            lbl = prop.get("label") or prop.get("displayName")
            pid = prop.get("id")
            if lbl and pid:
                mapping[pid] = lbl
                break
    return mapping


def extract_subjob_names(job_path: Path,
                         id_to_name: dict[str, str]) -> list[str]:
    """Retourne les noms des sous-jobs appelés par tRunJob* dans un .item."""
    subs: list[str] = []
    try:
        tree = ET.parse(job_path)
    except ET.ParseError:
        return subs
    root = tree.getroot()

    for node in iter_local(root, "node"):
        comp = node.get("componentName", "")
        if not comp.startswith("tRunJob"):
            continue

        raw: Optional[str] = None
        # Format plat
        for ep in iter_local(node, "elementParameter"):
            if ep.get("name") in ("PROCESS:PROCESS_TYPE_PROCESS",
                                  "PROCESS_TYPE_PROCESS"):
                raw = ep.get("value")
                break
        # Format imbriqué
        if not raw:
            for ep in iter_local(node, "elementParameter"):
                if ep.get("name") != "PROCESS":
                    continue
                for ev in iter_local(ep, "elementValue"):
                    ref = ev.get("elementRef") or ev.get("name") or ""
                    if "PROCESS_TYPE_PROCESS" in ref:
                        raw = ev.get("value")
                        break
                if raw:
                    break

        if not raw:
            continue
        cleaned = raw.strip().strip('"').strip()
        if ":" in cleaned and not cleaned.startswith("_"):
            cleaned = cleaned.split(":")[0]
        if not cleaned:
            continue
        if cleaned.startswith("_") and cleaned in id_to_name:
            cleaned = id_to_name[cleaned]
        if cleaned not in subs:
            subs.append(cleaned)
    return subs


def collect_jobs_recursively(project_dir: Path,
                             root_job: str,
                             visited: Optional[set[str]] = None
                             ) -> list[Path]:
    """Retourne [root.item, sub1.item, sub2.item, ...] en descendant
    récursivement dans les tRunJob."""
    if visited is None:
        visited = set()
    if root_job in visited:
        return []
    visited.add(root_job)

    path = find_job_item(project_dir, root_job)
    if not path:
        print(f"  ⚠ Job introuvable : {root_job}", file=sys.stderr)
        return []

    result = [path]
    id_map = build_id_to_name_map(project_dir)
    for sub in extract_subjob_names(path, id_map):
        result.extend(collect_jobs_recursively(project_dir, sub, visited))
    return result


# --------------------------------------------------------------------------
# Lecture du CSV
# --------------------------------------------------------------------------
def read_csv_updates(csv_path: Path, delimiter: str) -> dict[str, str]:
    """
    Lit le CSV variable;valeur et retourne {variable: valeur}.
    Accepte les CSV générés par context-exporter en mode simple ou détaillé
    (dans le détaillé on prend valeur_resolue si présente, sinon valeur).
    """
    updates: dict[str, str] = {}
    try:
        # utf-8-sig pour manger le BOM d'Excel
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            if not reader.fieldnames:
                sys.exit(f"❌ CSV vide ou invalide : {csv_path}")

            fns = [f.strip().lower() for f in reader.fieldnames]
            # on accepte plusieurs noms de colonnes
            var_col = next((reader.fieldnames[i]
                            for i, f in enumerate(fns)
                            if f in ("variable", "var", "name", "nom")),
                           None)
            # priorité à valeur_resolue > valeur > value
            val_col = None
            for candidate in ("valeur_resolue", "valeur", "value", "val"):
                for i, f in enumerate(fns):
                    if f == candidate:
                        val_col = reader.fieldnames[i]
                        break
                if val_col:
                    break

            if not var_col or not val_col:
                sys.exit(f"❌ CSV doit contenir au moins les colonnes "
                         f"'variable' et 'valeur'. "
                         f"Colonnes trouvées : {reader.fieldnames}")

            for row in reader:
                var = (row.get(var_col) or "").strip()
                val = row.get(val_col) or ""
                if not var:
                    continue
                # dernière valeur gagne en cas de doublons
                updates[var] = val

    except FileNotFoundError:
        sys.exit(f"❌ Fichier CSV introuvable : {csv_path}")
    except csv.Error as e:
        sys.exit(f"❌ Erreur de lecture CSV : {e}")

    if not updates:
        sys.exit(f"❌ Aucune mise à jour trouvée dans {csv_path}")

    return updates


# --------------------------------------------------------------------------
# Application des mises à jour sur un fichier XML
# --------------------------------------------------------------------------
@dataclass
class FileChangeReport:
    path: Path
    updated: list[tuple[str, str, str, str]] = field(default_factory=list)
    # (env, var, old_value, new_value)
    added: list[tuple[str, str, str]] = field(default_factory=list)
    # (env, var, new_value)
    missing: list[tuple[str, str]] = field(default_factory=list)
    # (env, var) pour chaque var du CSV absente de chaque env scanné
    envs_found: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.updated or self.added)


def update_file(item_path: Path,
                updates: dict[str, str],
                target_envs: Optional[list[str]],  # None = tous
                add_missing: bool) -> FileChangeReport:
    """
    Modifie en mémoire les <contextParameter> du fichier et retourne le
    rapport de changements. N'écrit rien sur le disque.
    """
    report = FileChangeReport(path=item_path)

    register_namespaces(item_path)
    try:
        tree = ET.parse(item_path)
    except ET.ParseError as e:
        print(f"  ⚠ Erreur parsing {item_path.name}: {e}", file=sys.stderr)
        return report

    root = tree.getroot()

    # On regroupe les <context name=...> par nom
    contexts = list(iter_local(root, "context"))
    if not contexts:
        return report  # rien à modifier (pas de bloc de contexte)

    report.envs_found = sorted({c.get("name", "Default") for c in contexts})

    # Filtrage par environnement
    if target_envs is not None:
        contexts_to_update = [c for c in contexts
                              if c.get("name", "Default").lower()
                              in {e.lower() for e in target_envs}]
    else:
        contexts_to_update = contexts

    if not contexts_to_update:
        return report

    for ctx in contexts_to_update:
        env = ctx.get("name", "Default")
        # Index des variables existantes
        existing: dict[str, ET.Element] = {}
        for cp in iter_local(ctx, "contextParameter"):
            pname = cp.get("name")
            if pname:
                existing[pname] = cp

        for var, new_raw in updates.items():
            formatted = format_talend_value(new_raw)
            if var in existing:
                cp = existing[var]
                old = cp.get("value", "")
                if old != formatted:
                    cp.set("value", formatted)
                    # certaines versions ont aussi rawValue
                    if cp.get("rawValue") is not None:
                        cp.set("rawValue", formatted)
                    report.updated.append((env, var, old, formatted))
            else:
                if add_missing:
                    # On recopie le namespace du parent si besoin
                    # Récupère le tag complet d'un contextParameter existant
                    # (pour garder le bon namespace), sinon on fallback
                    if existing:
                        sample_tag = next(iter(existing.values())).tag
                    else:
                        # Construit à partir du namespace du context
                        ns_match = re.match(r"(\{[^}]+\})", ctx.tag)
                        ns = ns_match.group(1) if ns_match else ""
                        sample_tag = f"{ns}contextParameter"
                    new_el = ET.SubElement(ctx, sample_tag)
                    new_el.set("name", var)
                    new_el.set("value", formatted)
                    # Attributs typiques (type String par défaut)
                    new_el.set("repositoryContextId", "")
                    new_el.set("type", "id_String")
                    report.added.append((env, var, formatted))
                else:
                    report.missing.append((env, var))

    # On stocke le tree modifié dans l'objet pour réécriture ultérieure
    report._tree = tree  # type: ignore[attr-defined]
    return report


def write_file(report: FileChangeReport, backup: bool) -> None:
    """Écrit physiquement le fichier modifié."""
    tree = getattr(report, "_tree", None)
    if tree is None:
        return
    if backup:
        bak = report.path.with_suffix(report.path.suffix + ".bak")
        shutil.copy2(report.path, bak)
    tree.write(report.path, encoding="UTF-8", xml_declaration=True)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def collect_target_files(project_dir: Path,
                         job_name: str,
                         include_external: bool,
                         include_embedded: bool,
                         verbose: bool) -> list[Path]:
    files: list[Path] = []

    # Jobs (root + sous-jobs récursivement)
    if include_embedded:
        job_files = collect_jobs_recursively(project_dir, job_name)
        files.extend(job_files)
        if verbose:
            names = [f.stem.split("_")[0] for f in job_files]
            print(f"  [debug] Jobs concernés ({len(job_files)}): "
                  f"{job_files[0].stem if job_files else '?'} + "
                  f"{len(job_files)-1} sous-job(s)", file=sys.stderr)

    # Contextes externes (portée projet entier)
    if include_external:
        ext = find_context_files(project_dir)
        files.extend(ext)
        if verbose:
            print(f"  [debug] Fichiers de contexte externes : {len(ext)}",
                  file=sys.stderr)

    return files


def print_report(reports: list[FileChangeReport], dry_run: bool,
                 show_missing: bool = True) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    total_updated = 0
    total_added = 0
    total_missing = 0
    modified_files = 0

    for r in reports:
        has_content = r.updated or r.added or (show_missing and r.missing)
        if not has_content:
            continue
        if r.has_changes:
            modified_files += 1
        print(f"\n📄 {r.path}")
        if r.envs_found:
            print(f"   Environnements présents : {', '.join(r.envs_found)}")

        for env, var, old, new in r.updated:
            total_updated += 1
            print(f"  {prefix}✎ [{env}] {var}")
            print(f"      ancien : {old}")
            print(f"      nouveau: {new}")

        for env, var, new in r.added:
            total_added += 1
            print(f"  {prefix}+ [{env}] {var} = {new}")

        for env, var in r.missing:
            total_missing += 1
            if show_missing:
                print(f"  ⚠ [{env}] {var} absente (utiliser --add-missing "
                      f"pour l'ajouter)")

    print("\n" + "=" * 70)
    print(f"SYNTHÈSE {prefix}")
    print(f"  Fichiers concernés     : {modified_files}")
    print(f"  Variables modifiées    : {total_updated}")
    print(f"  Variables ajoutées     : {total_added}")
    if show_missing:
        print(f"  Variables manquantes   : {total_missing}")
    elif total_missing:
        print(f"  Variables ignorées     : {total_missing} "
              f"(absentes des .item, silencieux)")
    if dry_run and (total_updated or total_added):
        print(f"\n  ℹ️  Pour appliquer réellement, relancer avec --apply")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Met à jour les variables de contexte d'un job Talend "
                    "et de ses sous-jobs depuis un CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--project", "-p", required=True, type=Path,
                        help="Racine du projet Talend")
    parser.add_argument("--job", "-j", required=True,
                        help="Nom du job racine (ses sous-jobs seront "
                             "traités récursivement)")
    parser.add_argument("--input", "-i", required=True, type=Path,
                        help="Fichier CSV avec colonnes variable;valeur")

    env_group = parser.add_mutually_exclusive_group()
    env_group.add_argument("--context", "-c",
                           help="Environnement à mettre à jour "
                                "(DEV/HOMOL/PROD/...). Défaut : tous les "
                                "environnements présents.")
    env_group.add_argument("--all-envs", action="store_true",
                           help="Mettre à jour tous les environnements "
                                "(comportement par défaut, ce flag est "
                                "redondant mais explicite).")

    parser.add_argument("--apply", action="store_true",
                        help="Applique réellement les modifications. Sans ce "
                             "flag, le script fait un dry-run (aucune "
                             "écriture).")
    parser.add_argument("--add-missing", action="store_true",
                        help="Ajouter au contexte les variables du CSV "
                             "absentes du .item. Par défaut, elles sont "
                             "ignorées silencieusement.")
    parser.add_argument("--warn-missing", action="store_true",
                        help="Afficher un warning pour chaque variable du "
                             "CSV absente d'un .item (par défaut : silencieux).")
    parser.add_argument("--no-backup", action="store_true",
                        help="Ne pas créer de fichier .bak avant modification")
    parser.add_argument("--no-external", action="store_true",
                        help="Ignorer les contextes externes (dossier context/)")
    parser.add_argument("--no-embedded", action="store_true",
                        help="Ignorer les contextes embarqués dans les jobs")
    parser.add_argument("--delimiter", "-d", default=";",
                        help="Séparateur CSV (défaut: ';')")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Mode diagnostic")
    args = parser.parse_args()

    if not args.project.is_dir():
        sys.exit(f"❌ Projet introuvable : {args.project}")

    if args.no_external and args.no_embedded:
        sys.exit("❌ --no-external et --no-embedded simultanés : rien à faire.")

    delimiter = args.delimiter.encode().decode("unicode_escape")
    if len(delimiter) != 1:
        sys.exit("❌ Délimiteur invalide")

    # Lecture CSV
    print(f"📥 Lecture du CSV : {args.input}")
    updates = read_csv_updates(args.input, delimiter)
    print(f"   {len(updates)} variable(s) à mettre à jour")
    if args.verbose:
        for var, val in list(updates.items())[:5]:
            print(f"   [debug] {var} = {val}", file=sys.stderr)
        if len(updates) > 5:
            print(f"   [debug] ... et {len(updates) - 5} autre(s)",
                  file=sys.stderr)

    # Détermination de la cible :
    #  - --context X : un seul environnement
    #  - pas d'argument OU --all-envs : tous les environnements
    target_envs = [args.context] if args.context else None

    env_label = (args.context if args.context
                 else "tous les environnements")
    print(f"🎯 Environnement(s) ciblé(s) : {env_label}")

    # Collecte des fichiers à modifier
    print(f"🔍 Collecte des fichiers (job racine : {args.job})")
    target_files = collect_target_files(
        args.project, args.job,
        include_external=not args.no_external,
        include_embedded=not args.no_embedded,
        verbose=args.verbose,
    )
    if not target_files:
        sys.exit("❌ Aucun fichier à traiter.")

    print(f"   {len(target_files)} fichier(s) à examiner")

    # Application en mémoire
    reports: list[FileChangeReport] = []
    for fp in target_files:
        rpt = update_file(fp, updates, target_envs, args.add_missing)
        reports.append(rpt)

    # Écriture si --apply
    dry_run = not args.apply
    if not dry_run:
        for r in reports:
            if r.has_changes:
                write_file(r, backup=not args.no_backup)

    # Restitution (les variables absentes sont silencieuses par défaut)
    print_report(reports, dry_run=dry_run, show_missing=args.warn_missing)

    if not dry_run:
        backup_msg = (" (fichiers .bak créés)" if not args.no_backup
                      else " (sans backup)")
        modified = sum(1 for r in reports if r.has_changes)
        print(f"\n✅ {modified} fichier(s) modifié(s){backup_msg}")


if __name__ == "__main__":
    main()
