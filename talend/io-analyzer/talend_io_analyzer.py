#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Talend Job I/O Analyzer
=======================
Analyse un job Talend et tous ses sous-jobs (tRunJob) afin de produire
un rapport des fichiers / répertoires en ENTRÉE et en SORTIE,
en résolvant les variables de contexte (context.XXX).

Compatible Python 3.10+

Exemples :
    python talend_io_analyzer.py -p /chemin/projet -j MON_JOB
    python talend_io_analyzer.py -p /chemin/projet -j MON_JOB -c PRD -f markdown
    python talend_io_analyzer.py -p /chemin/projet -j MON_JOB -f json -o report.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET


# --------------------------------------------------------------------------
# Catalogue des composants Talend (entrée / sortie)
# --------------------------------------------------------------------------
INPUT_COMPONENTS = {
    # Fichiers locaux
    "tFileInputDelimited", "tFileInputXML", "tFileInputJSON",
    "tFileInputExcel", "tFileInputPositional", "tFileInputRegex",
    "tFileInputFullRow", "tFileInputRaw", "tFileInputMSXML",
    "tFileInputProperties", "tFileInputLDIF", "tFileInputARFF",
    "tFileList", "tFileExist", "tFileUnarchive", "tFileFetch",
    "tFileCompare", "tFileRowCount", "tFileInputMail",
    # Transferts
    "tFTPGet", "tFTPFileExist", "tFTPFileList", "tFTPFileProperties",
    "tSFTPGet", "tSFTPFileExist", "tSFTPFileList",
    # Cloud / GCP / AWS / Azure
    "tGSGet", "tGSList", "tGSBucketExist",
    "tS3Get", "tS3List", "tS3BucketExist",
    "tAzureStorageGet", "tAzureStorageList",
    # Hadoop
    "tHDFSGet", "tHDFSExist", "tHDFSList", "tHDFSGetProperties",
    "tHDFSInput", "tHDFSInputRaw",
}

OUTPUT_COMPONENTS = {
    # Fichiers locaux
    "tFileOutputDelimited", "tFileOutputXML", "tFileOutputJSON",
    "tFileOutputExcel", "tFileOutputPositional", "tFileOutputRaw",
    "tFileOutputMSXML", "tFileOutputProperties", "tFileOutputLDIF",
    "tFileOutputARFF", "tFileArchive", "tFileTouch",
    # Transferts
    "tFTPPut", "tFTPRename", "tFTPDelete",
    "tSFTPPut", "tSFTPRename", "tSFTPDelete",
    # Cloud
    "tGSPut", "tGSDelete",
    "tS3Put", "tS3Delete",
    "tAzureStoragePut", "tAzureStorageDelete",
    # Hadoop
    "tHDFSPut", "tHDFSDelete", "tHDFSOutput", "tHDFSOutputRaw",
}

# Composants polyvalents : on les considère à la fois en entrée ET en sortie
DUAL_COMPONENTS = {
    "tFileCopy", "tFileDelete", "tFileRename",
    "tGSCopy",
}

# Noms des paramètres XML qui contiennent des chemins / fichiers / buckets
PATH_PARAMETER_NAMES = {
    "FILENAME", "FILE", "FILE_NAME", "FILE_PATH", "PATH",
    "DIRECTORY", "FOLDER", "FOLDER_NAME",
    "TARGET_DIRECTORY", "LOCAL_DIRECTORY", "REMOTE_DIRECTORY",
    "SOURCEDIRECTORY", "TARGETDIRECTORY",
    "SOURCE_FILE", "SOURCE_FILENAME", "TARGET_FILE", "TARGET_FILENAME",
    "DESTINATION", "DESTINATION_FOLDER", "DESTINATION_FILE",
    "REMOTEDIR", "LOCALDIR",
    "BUCKET", "KEY", "PREFIX", "OBJECT", "OBJECT_NAME",
    "URI", "DATASET", "FILEMASK",
}


# --------------------------------------------------------------------------
# Recherche de fichiers dans le projet
# --------------------------------------------------------------------------
def find_job_item(project_dir: Path, job_name: str) -> Optional[Path]:
    """
    Trouve le fichier .item d'un job dans le projet Talend.
    On accepte aussi bien 'MON_JOB' que 'MON_JOB_0.1'.
    """
    pattern = re.compile(rf"^{re.escape(job_name)}(_\d+\.\d+)?\.item$", re.IGNORECASE)
    for item in project_dir.rglob("*.item"):
        # On évite les fichiers de contexte qui sont aussi des .item
        if "/context/" in str(item).replace("\\", "/"):
            continue
        if pattern.match(item.name):
            return item
    return None


def find_context_files(project_dir: Path) -> dict[str, Path]:
    """Retourne {nom_du_groupe_de_contexte: chemin_item}."""
    contexts: dict[str, Path] = {}
    for path in project_dir.rglob("*.item"):
        if "/context/" not in str(path).replace("\\", "/"):
            continue
        name = re.sub(r"_\d+\.\d+\.item$", "", path.name)
        name = re.sub(r"\.item$", "", name)
        contexts[name] = path
    return contexts


# --------------------------------------------------------------------------
# Parsing des contextes
# --------------------------------------------------------------------------
def parse_context_file(item_path: Path,
                       context_env: Optional[str] = None) -> dict[str, str]:
    """
    Lit un fichier de contexte Talend (dossier context/)
    et retourne {nom_param: valeur} pour l'environnement choisi.
    """
    values: dict[str, str] = {}
    try:
        tree = ET.parse(item_path)
    except ET.ParseError as e:
        print(f"  ⚠ Erreur parsing du contexte {item_path.name}: {e}",
              file=sys.stderr)
        return values

    root = tree.getroot()
    contexts_by_env: dict[str, dict[str, str]] = defaultdict(dict)

    # Format multi-environnements: <context name="ENV"><contextParameter ...>
    for ctx in root.iter("context"):
        env_name = ctx.get("name", "Default")
        for cp in ctx.iter("contextParameter"):
            pname = cp.get("name")
            pvalue = cp.get("value", "") or cp.get("rawValue", "")
            if pname:
                contexts_by_env[env_name][pname] = pvalue

    if not contexts_by_env:
        # Format simple: <contextParameter name="..." value="...">
        for cp in root.iter("contextParameter"):
            pname = cp.get("name")
            pvalue = cp.get("value", "")
            if pname:
                values[pname] = pvalue
        return values

    # Choix de l'environnement
    if context_env:
        for env, vals in contexts_by_env.items():
            if env.upper() == context_env.upper():
                return dict(vals)

    if "Default" in contexts_by_env:
        return dict(contexts_by_env["Default"])

    return dict(next(iter(contexts_by_env.values())))


def extract_job_contexts(job_item: Path,
                         context_env: Optional[str] = None) -> dict[str, str]:
    """Extrait les contextes embarqués directement dans le .item du job."""
    return parse_context_file(job_item, context_env)


# --------------------------------------------------------------------------
# Résolution des expressions de contexte
# --------------------------------------------------------------------------
CONTEXT_REF_RE = re.compile(r"context\.(\w+)")


def resolve_value(raw: str,
                  context: dict[str, str],
                  depth: int = 0) -> str:
    """
    Résout une valeur Talend :
      - remplace context.XXX par sa valeur
      - évalue les concaténations Java triviales : "abc" + "def" + var
      - retire les guillemets entourant une chaîne simple
    """
    if raw is None:
        return ""
    if depth > 15:
        return raw  # Sécurité anti-boucle

    value = raw.strip()

    # Substitution des références de contexte
    def _sub(match: re.Match) -> str:
        ref = match.group(1)
        if ref in context:
            return resolve_value(context[ref], context, depth + 1)
        return f"<context.{ref}?>"

    value = CONTEXT_REF_RE.sub(_sub, value)

    # Concaténations Java très simples
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
# Analyse d'un job
# --------------------------------------------------------------------------
class JobIO:
    """Conteneur des résultats d'analyse pour un job."""

    def __init__(self, job_name: str, item_path: Path):
        self.job_name = job_name
        self.item_path = item_path
        self.inputs: list[dict] = []
        self.outputs: list[dict] = []
        self.subjobs: list[str] = []  # noms des sous-jobs (tRunJob)


def get_node_param(node: ET.Element, param_name: str) -> Optional[str]:
    """Récupère la valeur d'un <elementParameter name=...>."""
    for ep in node.findall("elementParameter"):
        if ep.get("name") == param_name:
            return ep.get("value")
    return None


def analyze_job(job_name: str,
                item_path: Path,
                context_values: dict[str, str]) -> JobIO:
    """Analyse un job Talend et extrait ses I/O + ses sous-jobs."""
    result = JobIO(job_name, item_path)

    try:
        tree = ET.parse(item_path)
    except ET.ParseError as e:
        print(f"  ⚠ Erreur parsing {item_path}: {e}", file=sys.stderr)
        return result

    root = tree.getroot()

    for node in root.iter("node"):
        comp_name = node.get("componentName", "")
        unique_name_raw = get_node_param(node, "UNIQUE_NAME") or ""
        unique_name = unique_name_raw.strip('"') or comp_name

        # tRunJob -> on enregistre le sous-job pour analyse récursive
        if comp_name in ("tRunJob", "tRunJobOnGrid"):
            subjob_raw = get_node_param(node, "PROCESS:PROCESS_TYPE_PROCESS")
            if subjob_raw:
                subjob_name = subjob_raw.strip('"').split(":")[0]
                if subjob_name and subjob_name not in result.subjobs:
                    result.subjobs.append(subjob_name)
            continue

        is_input = comp_name in INPUT_COMPONENTS
        is_output = comp_name in OUTPUT_COMPONENTS
        is_dual = comp_name in DUAL_COMPONENTS

        if not (is_input or is_output or is_dual):
            continue

        # On collecte tous les paramètres de chemin
        paths_found = []
        for ep in node.findall("elementParameter"):
            pname = ep.get("name", "")
            pvalue = ep.get("value", "")
            if pname.upper() in PATH_PARAMETER_NAMES and pvalue:
                resolved = resolve_value(pvalue, context_values)
                paths_found.append({
                    "param": pname,
                    "raw": pvalue,
                    "resolved": resolved,
                })

        if not paths_found:
            continue

        entry = {
            "component": comp_name,
            "unique_name": unique_name,
            "paths": paths_found,
        }

        if is_input or is_dual:
            result.inputs.append(entry)
        if is_output or is_dual:
            result.outputs.append(entry)

    return result


def analyze_recursive(project_dir: Path,
                      job_name: str,
                      context_env: Optional[str],
                      ext_contexts_cache: Optional[dict[str, dict[str, str]]] = None,
                      visited: Optional[set[str]] = None) -> list[JobIO]:
    """Analyse un job et descend récursivement dans tous ses tRunJob."""
    if visited is None:
        visited = set()
    if job_name in visited:
        return []
    visited.add(job_name)

    item_path = find_job_item(project_dir, job_name)
    if not item_path:
        print(f"  ⚠ Job introuvable dans le projet : {job_name}",
              file=sys.stderr)
        return []

    # Cache des contextes externes (lecture une seule fois)
    if ext_contexts_cache is None:
        ext_contexts_cache = {}
        for cname, cpath in find_context_files(project_dir).items():
            ext_contexts_cache[cname] = parse_context_file(cpath, context_env)

    # Contextes embarqués dans le .item du job
    job_ctx = extract_job_contexts(item_path, context_env)

    # Fusion : on complète avec les contextes externes (sans écraser)
    for ext_values in ext_contexts_cache.values():
        for k, v in ext_values.items():
            job_ctx.setdefault(k, v)

    job_io = analyze_job(job_name, item_path, job_ctx)
    results = [job_io]

    for subjob in job_io.subjobs:
        results.extend(analyze_recursive(
            project_dir, subjob, context_env,
            ext_contexts_cache, visited,
        ))

    return results


# --------------------------------------------------------------------------
# Restitution
# --------------------------------------------------------------------------
def print_report_text(jobs: list[JobIO]) -> None:
    print("\n" + "=" * 80)
    print(" RAPPORT D'ANALYSE DES ENTRÉES / SORTIES TALEND".center(80))
    print("=" * 80)

    for idx, job in enumerate(jobs, 1):
        marker = "JOB PRINCIPAL" if idx == 1 else "SOUS-JOB"
        print(f"\n[{marker}] {job.job_name}")
        print(f"  📄 fichier : {job.item_path}")
        if job.subjobs:
            print(f"  🔗 appelle : {', '.join(job.subjobs)}")

        print(f"\n  ▼ ENTRÉES ({len(job.inputs)})")
        if not job.inputs:
            print("    (aucune)")
        for io in job.inputs:
            print(f"    • [{io['component']}] {io['unique_name']}")
            for p in io["paths"]:
                print(f"        {p['param']}: {p['resolved']}")
                if p["resolved"] != p["raw"].strip('"'):
                    print(f"          ↳ brut: {p['raw']}")

        print(f"\n  ▲ SORTIES ({len(job.outputs)})")
        if not job.outputs:
            print("    (aucune)")
        for io in job.outputs:
            print(f"    • [{io['component']}] {io['unique_name']}")
            for p in io["paths"]:
                print(f"        {p['param']}: {p['resolved']}")
                if p["resolved"] != p["raw"].strip('"'):
                    print(f"          ↳ brut: {p['raw']}")
        print("-" * 80)

    # Synthèse globale
    all_inputs = {p["resolved"]
                  for j in jobs for io in j.inputs for p in io["paths"]}
    all_outputs = {p["resolved"]
                   for j in jobs for io in j.outputs for p in io["paths"]}
    print("\n📊 SYNTHÈSE GLOBALE")
    print(f"  Total jobs analysés : {len(jobs)}")
    print(f"  Entrées uniques     : {len(all_inputs)}")
    print(f"  Sorties uniques     : {len(all_outputs)}")
    print()


def export_markdown(jobs: list[JobIO], output_path: Path) -> None:
    lines = ["# Rapport I/O Talend\n"]
    for idx, job in enumerate(jobs, 1):
        kind = "Job principal" if idx == 1 else "Sous-job"
        lines.append(f"## {kind} : `{job.job_name}`\n")
        lines.append(f"- Fichier : `{job.item_path}`")
        if job.subjobs:
            subs = ", ".join(f"`{s}`" for s in job.subjobs)
            lines.append(f"- Sous-jobs appelés : {subs}")

        lines.append("\n### Entrées\n")
        if not job.inputs:
            lines.append("_Aucune_\n")
        else:
            lines.append("| Composant | Nom | Paramètre | Chemin résolu |")
            lines.append("|---|---|---|---|")
            for io in job.inputs:
                for p in io["paths"]:
                    lines.append(
                        f"| {io['component']} | {io['unique_name']} | "
                        f"{p['param']} | `{p['resolved']}` |"
                    )

        lines.append("\n### Sorties\n")
        if not job.outputs:
            lines.append("_Aucune_\n")
        else:
            lines.append("| Composant | Nom | Paramètre | Chemin résolu |")
            lines.append("|---|---|---|---|")
            for io in job.outputs:
                for p in io["paths"]:
                    lines.append(
                        f"| {io['component']} | {io['unique_name']} | "
                        f"{p['param']} | `{p['resolved']}` |"
                    )
        lines.append("\n---\n")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n✓ Rapport Markdown écrit dans : {output_path}")


def export_json(jobs: list[JobIO], output_path: Path) -> None:
    data = []
    for job in jobs:
        data.append({
            "job_name": job.job_name,
            "item_path": str(job.item_path),
            "subjobs": job.subjobs,
            "inputs": job.inputs,
            "outputs": job.outputs,
        })
    output_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n✓ Rapport JSON écrit dans : {output_path}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Analyse les I/O d'un job Talend et de ses sous-jobs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--project", "-p", required=True, type=Path,
                        help="Répertoire racine du projet Talend "
                             "(contient process/, context/, ...)")
    parser.add_argument("--job", "-j", required=True,
                        help="Nom du job à analyser (sans suffixe _x.y)")
    parser.add_argument("--context", "-c", default=None,
                        help="Environnement de contexte : DEV / UAT / PRD / "
                             "Default (défaut: Default)")
    parser.add_argument("--format", "-f",
                        choices=["text", "markdown", "json"],
                        default="text",
                        help="Format de sortie")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Fichier de sortie pour markdown/json "
                             "(défaut: rapport_<job>.md ou .json)")
    args = parser.parse_args()

    if not args.project.is_dir():
        sys.exit(f"❌ Le répertoire projet n'existe pas : {args.project}")

    print(f"🔍 Analyse du job '{args.job}' dans {args.project}")
    if args.context:
        print(f"   Contexte ciblé : {args.context}")

    jobs = analyze_recursive(args.project, args.job, args.context)

    if not jobs:
        sys.exit(f"❌ Aucune analyse possible pour le job : {args.job}")

    if args.format == "text":
        print_report_text(jobs)
    elif args.format == "markdown":
        out = args.output or Path(f"rapport_{args.job}.md")
        export_markdown(jobs, out)
    elif args.format == "json":
        out = args.output or Path(f"rapport_{args.job}.json")
        export_json(jobs, out)


if __name__ == "__main__":
    main()
