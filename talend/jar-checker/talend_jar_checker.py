#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Talend JAR Dependency Checker
=============================
Analyse TOUS les jobs d'un projet Talend et liste, pour chaque job,
les librairies JAR manquantes.

Le script trouve automatiquement où sont stockées les librairies
disponibles dans le projet (aucun argument à fournir, sauf le
répertoire racine).

Sources des JAR requis (par job, dans les .item) :
  - tLibraryLoad                   -> paramètre LIBRARY
  - tDB* / tJDBC*                  -> paramètre DRIVER_JAR (plat ou imbriqué)
  - Tout paramètre avec JAR/LIBRARY/MODULE/DRIVER dans le nom
  - <required moduleName="X.jar"/> (Talend ESB / Routes)
  - Tout attribut moduleName/library terminant par .jar

Sources des JAR disponibles (auto-découverte sous le projet) :
  - lib/, libs/, library/, libraries/
  - Dossier configuration/ du Studio s'il est inclus dans le projet
  - Tout dossier qui contient au moins un .jar (heuristique de fallback)

Exemples :
    # Tout le projet, rapport texte par job
    python talend_jar_checker.py -p ~/projet

    # Export CSV des manquants par job pour Excel
    python talend_jar_checker.py -p ~/projet -f csv -o manquants.csv

    # Avec un dossier de librairies supplémentaire
    python talend_jar_checker.py -p ~/projet -l ~/Talend-Studio/lib

Compatible Python 3.10+
"""

from __future__ import annotations

import argparse
import csv
import json
import re
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


def validate_project_dir(raw_path) -> Path:
    """Valide le chemin et retourne un Path absolu nettoyé.
    Gère guillemets, espaces non échappés, OneDrive, séparateurs Windows.
    Si raw_path est une liste (nargs='+'), reconstitue le chemin coupé."""
    if isinstance(raw_path, list):
        if len(raw_path) > 1:
            joined = " ".join(raw_path)
            print("⚠ Chemin reçu en plusieurs morceaux. Reconstruction :",
                  file=sys.stderr)
            print("   " + joined, file=sys.stderr)
            print('   (Conseil : entourer le chemin de guillemets pour '
                  'éviter ce comportement : -p "' + joined + '")',
                  file=sys.stderr)
            s = joined
        else:
            s = raw_path[0]
    else:
        s = str(raw_path)
    s = s.strip()
    if (s.startswith('"') and s.endswith('"')) or \
       (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    s = s.rstrip("\\/")
    if not s:
        sys.exit("❌ Le chemin du projet est vide.")
    try:
        p_resolved = Path(s).expanduser().resolve()
    except (OSError, RuntimeError) as e:
        err_msg = "❌ Chemin invalide : " + s
        err_msg += "\n   Erreur système : " + str(e)
        sys.exit(err_msg)
    if not p_resolved.exists():
        msg = ["❌ Projet introuvable : " + s,
               "   Chemin résolu  : " + str(p_resolved)]
        parent = p_resolved.parent
        if parent.is_dir():
            siblings = sorted([d.name for d in parent.iterdir()
                               if d.is_dir()])[:8]
            msg.append("   Dossiers disponibles dans " + str(parent) + " :")
            for s_name in siblings:
                msg.append("     - " + s_name)
        else:
            msg.append("   Le parent " + str(parent) + " n'existe pas non plus.")
            msg.append("   Vérifier les espaces, accents ou caractères "
                       "spéciaux dans le chemin.")
        sys.exit("\n".join(msg))
    if not p_resolved.is_dir():
        sys.exit("❌ Le chemin existe mais n'est pas un dossier : "
                 + str(p_resolved))
    return p_resolved


# --------------------------------------------------------------------------
# Helpers XML namespace-agnostic
# --------------------------------------------------------------------------
def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def iter_local(root: ET.Element, name: str):
    for elem in root.iter():
        if local_name(elem.tag) == name:
            yield elem


def find_job_files(project_dir: Path) -> list[Path]:
    return [p for p in project_dir.rglob("*.item")
            if "/context/" not in str(p).replace("\\", "/")]


def job_name_from_item(path: Path) -> str:
    name = re.sub(r"_\d+\.\d+\.item$", "", path.name)
    return re.sub(r"\.item$", "", name)


# --------------------------------------------------------------------------
# Auto-découverte des dépôts de librairies dans le projet
# --------------------------------------------------------------------------
LIB_DIR_NAMES = {"lib", "libs", "library", "libraries"}
EXCLUDED_DIRS = {".svn", ".git", "node_modules", ".idea", ".vscode",
                 "target", "build"}


def discover_lib_dirs(project_dir: Path,
                      extra_dirs: list[Path],
                      verbose: bool) -> list[Path]:
    """Trouve tous les dossiers de JAR dans le projet."""
    found: set[Path] = set()

    # 1) Dossiers conventionnels (lib/, libs/, library/, libraries/)
    for path in project_dir.rglob("*"):
        if not path.is_dir():
            continue
        if any(part in EXCLUDED_DIRS or part.startswith(".")
               for part in path.parts):
            continue
        if path.name.lower() in LIB_DIR_NAMES:
            found.add(path)

    # 2) Dossier Studio si présent dans le projet
    for path in project_dir.rglob("org.talend.designer.codegen.lib.java"):
        if path.is_dir():
            found.add(path)

    # 3) Fallback : tout dossier qui contient au moins un .jar
    already_covered = lambda p: any(
        str(p).startswith(str(f) + "/") or str(p) == str(f) for f in found)
    for jar_path in project_dir.rglob("*.jar"):
        parent = jar_path.parent
        if not already_covered(parent):
            found.add(parent)

    # 4) Dossiers passés explicitement
    for d in extra_dirs:
        if d.is_dir():
            found.add(d.resolve())
        else:
            print(f"  ⚠ Dossier de libs ignoré (introuvable) : {d}",
                  file=sys.stderr)

    result = sorted(found, key=lambda p: str(p))

    if verbose:
        print(f"  [debug] {len(result)} dossier(s) de libs auto-découvert(s) :",
              file=sys.stderr)
        for d in result:
            n = sum(1 for _ in d.rglob("*.jar"))
            print(f"          {d} ({n} JAR)", file=sys.stderr)

    return result


# --------------------------------------------------------------------------
# Extraction des JAR requis depuis un .item
# --------------------------------------------------------------------------
JAR_NAME_RE = re.compile(r"[\w\-\.+]+\.jar", re.IGNORECASE)


@dataclass
class JarRequirement:
    jar_name: str
    job_name: str
    job_file: Path
    component: str
    unique_name: str
    source: str


def normalize_jar_name(name: str) -> str:
    if not name:
        return ""
    s = name.strip().strip('"').strip("'").strip()
    if "/" in s or "\\" in s:
        s = Path(s.replace("\\", "/")).name
    return s


def extract_jars_from_value(value: str) -> list[str]:
    if not value:
        return []
    return [m.group(0) for m in JAR_NAME_RE.finditer(value)]


def get_node_param(node: ET.Element, name: str) -> Optional[str]:
    for ep in iter_local(node, "elementParameter"):
        if ep.get("name") == name:
            return ep.get("value")
    return None


def get_unique_name(node: ET.Element) -> str:
    raw = get_node_param(node, "UNIQUE_NAME") or ""
    return raw.strip('"') or node.get("componentName", "")


def extract_jars_from_job(job_path: Path,
                          job_name: str) -> list[JarRequirement]:
    requirements: list[JarRequirement] = []
    seen: set[tuple[str, str, str]] = set()

    try:
        tree = ET.parse(job_path)
    except ET.ParseError as e:
        print(f"  ⚠ Erreur parsing {job_path.name}: {e}", file=sys.stderr)
        return requirements

    root = tree.getroot()

    def add_requirement(jar: str, comp: str, unique: str, source: str):
        norm = normalize_jar_name(jar)
        if not norm:
            return
        key = (norm, comp, unique)
        if key in seen:
            return
        seen.add(key)
        requirements.append(JarRequirement(
            jar_name=norm, job_name=job_name, job_file=job_path,
            component=comp, unique_name=unique, source=source,
        ))

    for node in iter_local(root, "node"):
        comp = node.get("componentName", "")
        unique = get_unique_name(node)

        for ep in iter_local(node, "elementParameter"):
            pname = (ep.get("name") or "").upper()
            pvalue = ep.get("value") or ""

            param_suggests_jar = any(k in pname for k in (
                "LIBRARY", "JAR", "DRIVER", "MODULE_NAME", "MODULE", "JDBC_JAR"
            ))

            if ".jar" in pvalue.lower():
                for jar in extract_jars_from_value(pvalue):
                    add_requirement(jar, comp, unique,
                                    f"node/{ep.get('name')}")

            if param_suggests_jar:
                for ev in iter_local(ep, "elementValue"):
                    evval = ev.get("value", "")
                    if ".jar" in evval.lower():
                        for jar in extract_jars_from_value(evval):
                            add_requirement(jar, comp, unique,
                                            f"{ep.get('name')}/elementValue")

    for tag_name in ("required", "requiredIf"):
        for req_elem in iter_local(root, tag_name):
            mod = req_elem.get("moduleName") or req_elem.get("name") or ""
            if mod.lower().endswith(".jar"):
                add_requirement(mod, f"<{tag_name}>", "(global)", tag_name)

    for elem in root.iter():
        for attr_name in ("moduleName", "library"):
            v = elem.get(attr_name)
            if v and v.lower().endswith(".jar"):
                add_requirement(v, local_name(elem.tag),
                                f"(@{attr_name})", f"attr/{attr_name}")

    return requirements


# --------------------------------------------------------------------------
# Index des JAR disponibles + matching tolérant aux versions
# --------------------------------------------------------------------------
def build_available_jar_index(lib_dirs: list[Path]
                              ) -> tuple[set[str], dict[str, list[Path]]]:
    exact: set[str] = set()
    by_basename: dict[str, list[Path]] = defaultdict(list)
    for lib_dir in lib_dirs:
        for jar_path in lib_dir.rglob("*.jar"):
            exact.add(jar_path.name)
            by_basename[jar_path.stem].append(jar_path)
    return exact, dict(by_basename)


VERSION_SUFFIX_RE = re.compile(r"[-_]\d+(\.\d+)*([-_][\w]+)?$")


def strip_version(jar_basename: str) -> str:
    return VERSION_SUFFIX_RE.sub("", jar_basename)


def find_match(jar_required: str,
               exact: set[str],
               by_basename: dict[str, list[Path]]
               ) -> tuple[str, Optional[list[Path]]]:
    if jar_required in exact:
        stem = Path(jar_required).stem
        return "exact", by_basename.get(stem, [])

    required_stem = Path(jar_required).stem
    required_root = strip_version(required_stem)

    if required_root and required_root != required_stem:
        candidates: list[Path] = []
        for stem, paths in by_basename.items():
            if strip_version(stem) == required_root:
                candidates.extend(paths)
        if candidates:
            return "version_mismatch", candidates

    return "missing", None


# --------------------------------------------------------------------------
# Analyse
# --------------------------------------------------------------------------
@dataclass
class JobReport:
    job_name: str
    job_file: Path
    requirements: list[JarRequirement] = field(default_factory=list)
    missing: list[JarRequirement] = field(default_factory=list)
    version_mismatch: list[tuple[JarRequirement, list[Path]]] = \
        field(default_factory=list)
    ok: list[JarRequirement] = field(default_factory=list)


def analyze_project(project_dir: Path,
                    lib_dirs: list[Path],
                    verbose: bool) -> tuple[list[JobReport], int]:
    job_files = find_job_files(project_dir)
    if not job_files:
        sys.exit(f"❌ Aucun job (.item) trouvé sous {project_dir}")

    if verbose:
        print(f"  [debug] {len(job_files)} job(s) à analyser",
              file=sys.stderr)

    exact, by_basename = build_available_jar_index(lib_dirs)
    indexed = sum(len(p) for p in by_basename.values())

    reports: list[JobReport] = []
    for jp in job_files:
        jname = job_name_from_item(jp)
        rpt = JobReport(job_name=jname, job_file=jp)
        rpt.requirements = extract_jars_from_job(jp, jname)

        unique_jars: dict[str, JarRequirement] = {}
        for req in rpt.requirements:
            unique_jars.setdefault(req.jar_name, req)

        for jar, req in sorted(unique_jars.items()):
            status, paths = find_match(jar, exact, by_basename)
            if status == "missing":
                rpt.missing.append(req)
            elif status == "version_mismatch":
                rpt.version_mismatch.append((req, paths or []))
            else:
                rpt.ok.append(req)

        reports.append(rpt)

    return reports, indexed


# --------------------------------------------------------------------------
# Restitution
# --------------------------------------------------------------------------
def print_report_text(reports: list[JobReport],
                      lib_dirs: list[Path],
                      indexed: int,
                      show_ok_jobs: bool) -> None:
    print("\n" + "=" * 80)
    print(" ANALYSE DES DÉPENDANCES JAR ".center(80, "="))
    print("=" * 80)

    print(f"\nDépôts de librairies trouvés ({len(lib_dirs)}, "
          f"{indexed} JAR indexés) :")
    for d in lib_dirs:
        print(f"  • {d}")
    if not lib_dirs:
        print("  ⚠ Aucun dépôt de librairies trouvé sous le projet.")
        print("    Tous les JAR vont apparaître comme manquants.")

    reports_sorted = sorted(
        reports,
        key=lambda r: (-len(r.missing), -len(r.version_mismatch), r.job_name)
    )
    jobs_with_issues = [r for r in reports_sorted
                        if r.missing or r.version_mismatch]
    jobs_clean = [r for r in reports_sorted
                  if not r.missing and not r.version_mismatch
                  and r.requirements]

    print(f"\n{'─' * 80}")
    print(f"📋 JOBS AVEC DÉPENDANCES MANQUANTES OU DIVERGENTES "
          f"({len(jobs_with_issues)} / {len(reports)})")
    print(f"{'─' * 80}")

    if not jobs_with_issues:
        print("\n  ✓ Aucun job en défaut, toutes les dépendances sont "
              "satisfaites !")
    else:
        for r in jobs_with_issues:
            print(f"\n📦 {r.job_name}")
            print(f"   Fichier : {r.job_file}")
            print(f"   Dépendances : {len(r.requirements)} requise(s) "
                  f"({len(r.ok)} ok, "
                  f"{len(r.version_mismatch)} version différente, "
                  f"{len(r.missing)} manquante(s))")

            if r.missing:
                print(f"\n   ❌ MANQUANTS ({len(r.missing)}) :")
                for req in r.missing:
                    print(f"      ✗ {req.jar_name}")
                    print(f"          via {req.component} "
                          f"({req.unique_name})")

            if r.version_mismatch:
                print(f"\n   ⚠️  VERSION DIFFÉRENTE ({len(r.version_mismatch)}) :")
                for req, paths in r.version_mismatch:
                    print(f"      ≈ {req.jar_name}")
                    print(f"          via {req.component} ({req.unique_name})")
                    for p in paths[:3]:
                        print(f"          ↳ disponible : {p.name}")

    if show_ok_jobs and jobs_clean:
        print(f"\n{'─' * 80}")
        print(f"✅ JOBS AVEC TOUTES LES DÉPENDANCES SATISFAITES "
              f"({len(jobs_clean)})")
        print(f"{'─' * 80}")
        for r in jobs_clean:
            print(f"  ✓ {r.job_name:<50} "
                  f"{len(r.requirements)} dépendance(s)")

    total_required = sum(len(r.requirements) for r in reports)
    total_unique_missing = len({req.jar_name
                                for r in reports for req in r.missing})
    total_jobs_impacted = len([r for r in reports if r.missing])

    print(f"\n{'=' * 80}")
    print("📊 SYNTHÈSE GLOBALE")
    print(f"  Jobs analysés                   : {len(reports)}")
    print(f"  Jobs avec dépendances manquantes: {total_jobs_impacted}")
    print(f"  Total dépendances requises      : {total_required}")
    print(f"  JARs uniques manquants          : {total_unique_missing}")
    print()


def export_csv_per_job(reports: list[JobReport],
                       out_path: Path,
                       delimiter: str,
                       missing_only: bool) -> int:
    rows = []
    for r in reports:
        seen: set[str] = set()
        for req in r.requirements:
            if req.jar_name in seen:
                continue
            seen.add(req.jar_name)

            if any(req.jar_name == m.jar_name for m in r.missing):
                status = "missing"
            elif any(req.jar_name == vm[0].jar_name
                     for vm in r.version_mismatch):
                status = "version_mismatch"
            else:
                status = "exact"

            if missing_only and status == "exact":
                continue

            rows.append({
                "job": r.job_name,
                "job_file": str(r.job_file),
                "jar_required": req.jar_name,
                "status": status,
                "component": req.component,
                "unique_name": req.unique_name,
                "source": req.source,
            })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["job", "job_file", "jar_required", "status",
                  "component", "unique_name", "source"]
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames,
                                delimiter=delimiter,
                                quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


def export_json(reports: list[JobReport],
                lib_dirs: list[Path],
                out_path: Path) -> None:
    payload = {
        "lib_dirs": [str(d) for d in lib_dirs],
        "jobs": [],
    }
    for r in reports:
        payload["jobs"].append({
            "job_name": r.job_name,
            "job_file": str(r.job_file),
            "missing": [
                {"jar": m.jar_name, "component": m.component,
                 "unique_name": m.unique_name, "source": m.source}
                for m in r.missing
            ],
            "version_mismatch": [
                {"jar": req.jar_name, "component": req.component,
                 "unique_name": req.unique_name,
                 "available": [str(p) for p in paths]}
                for req, paths in r.version_mismatch
            ],
            "ok_count": len(r.ok),
            "total_required": len(r.requirements),
        })
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Analyse tous les jobs d'un projet Talend et liste les "
                    "dépendances JAR manquantes pour chacun.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--project", "-p", required=True, nargs="+",
                        help="Racine du projet Talend. Sur Windows, "
                             "entourer de guillemets si le chemin contient "
                             "des espaces : -p \"C:\\Mes Projets\\BSY\"")
    parser.add_argument("--lib-dir", "-l", action="append", type=Path,
                        default=[],
                        help="Dossier supplémentaire de JAR à inclure dans la "
                             "comparaison. Répétable. Optionnel : le script "
                             "trouve seul les dépôts du projet.")
    parser.add_argument("--show-ok-jobs", action="store_true",
                        help="Lister aussi les jobs sans souci de dépendance")
    parser.add_argument("--missing-only", action="store_true",
                        help="(CSV uniquement) N'exporter que les lignes avec "
                             "status missing/version_mismatch")
    parser.add_argument("--format", "-f",
                        choices=["text", "csv", "json"],
                        default="text",
                        help="Format de sortie")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Fichier de sortie pour csv/json")
    parser.add_argument("--delimiter", "-d", default=";",
                        help="Séparateur CSV (défaut: ';')")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Mode diagnostic")
    args = parser.parse_args()

    # Validation du chemin projet avec diagnostic détaillé
    project_dir = validate_project_dir(args.project)

    delimiter = args.delimiter.encode().decode("unicode_escape")
    if len(delimiter) != 1:
        sys.exit("❌ Délimiteur invalide")

    print(f"🔍 Analyse du projet : {project_dir}")

    lib_dirs = discover_lib_dirs(project_dir, args.lib_dir, args.verbose)
    print(f"📚 {len(lib_dirs)} dépôt(s) de librairies trouvé(s)")

    reports, indexed = analyze_project(project_dir, lib_dirs, args.verbose)

    if args.format == "text":
        print_report_text(reports, lib_dirs, indexed, args.show_ok_jobs)
    elif args.format == "csv":
        out = args.output or Path("jar_check_per_job.csv")
        n = export_csv_per_job(reports, out, delimiter, args.missing_only)
        print(f"\n✓ {n} ligne(s) écrites dans : {out}")
    elif args.format == "json":
        out = args.output or Path("jar_check_per_job.json")
        export_json(reports, lib_dirs, out)
        print(f"\n✓ Rapport JSON écrit dans : {out}")

    jobs_impacted = sum(1 for r in reports if r.missing)
    sys.exit(0 if jobs_impacted == 0 else 1)


if __name__ == "__main__":
    main()
