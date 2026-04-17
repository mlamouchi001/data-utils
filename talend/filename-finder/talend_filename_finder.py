#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Talend Filename Finder
======================
Recherche l'usage d'un nom de fichier (ou d'un pattern) dans un projet Talend :

  1. DÉCLARATIONS DE CONTEXTE : dans quelles variables de contexte apparaît
     cette valeur (contextes externes du dossier context/ ET contextes
     embarqués dans les .item de jobs), et pour quels environnements
     (DEV / HOMOL / INT / PROD / ...).

  2. USAGES DIRECTS : quels jobs / composants ont le pattern en dur dans
     un paramètre (ex. FILENAME = "/data/customers.csv").

  3. USAGES INDIRECTS : quels jobs / composants référencent une variable
     de contexte qui, elle-même, matche le pattern (usage transitif).

Compatible Python 3.10+

Exemples :
    # Recherche simple d'un nom de fichier
    python talend_filename_finder.py -p ~/projet -n customers.csv

    # Recherche par regex
    python talend_filename_finder.py -p ~/projet -n "^.*_REFERENTIEL_.*\\.csv$" --regex

    # Recherche sensible à la casse
    python talend_filename_finder.py -p ~/projet -n Customers.CSV --case-sensitive

    # Export Markdown (Confluence-ready)
    python talend_filename_finder.py -p ~/projet -n customers.csv -f markdown
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
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
    """'{ns}node' -> 'node' ; 'node' -> 'node'."""
    return tag.rsplit("}", 1)[-1]


def iter_local(root: ET.Element, name: str):
    """Itère récursivement sur les éléments de nom local `name`."""
    for elem in root.iter():
        if local_name(elem.tag) == name:
            yield elem


# --------------------------------------------------------------------------
# Structures de données
# --------------------------------------------------------------------------
@dataclass
class ContextDeclaration:
    """Une variable de contexte dont la valeur matche le pattern recherché."""
    source_type: str                  # 'external' ou 'embedded'
    source_file: Path
    job_name: Optional[str]           # renseigné si embedded
    context_group: str                # 'DEV', 'PROD', 'Default', ...
    var_name: str
    value: str                        # valeur brute (telle que dans le XML)


@dataclass
class JobUsage:
    """Un composant de job qui utilise le pattern (direct ou indirect)."""
    job_name: str
    job_file: Path
    component: str                    # componentName (ex. tFileInputDelimited)
    unique_name: str
    param_name: str
    raw_value: str
    match_type: str                   # 'direct' ou 'indirect'
    via_variables: list[str] = field(default_factory=list)


@dataclass
class SearchResult:
    pattern: str
    declarations: list[ContextDeclaration] = field(default_factory=list)
    direct_usages: list[JobUsage] = field(default_factory=list)
    indirect_usages: list[JobUsage] = field(default_factory=list)

    @property
    def matching_var_names(self) -> set[str]:
        """Noms de variables de contexte dont au moins une valeur matche."""
        return {d.var_name for d in self.declarations}


# --------------------------------------------------------------------------
# Logique de matching
# --------------------------------------------------------------------------
class Matcher:
    """Encapsule la logique de comparaison pattern / valeur."""

    def __init__(self, pattern: str, regex: bool, case_sensitive: bool):
        self.pattern_raw = pattern
        self.regex = regex
        self.case_sensitive = case_sensitive
        if regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            self._re = re.compile(pattern, flags)
        else:
            self._needle = pattern if case_sensitive else pattern.lower()

    def matches(self, value: Optional[str]) -> bool:
        if not value:
            return False
        if self.regex:
            return bool(self._re.search(value))
        hay = value if self.case_sensitive else value.lower()
        return self._needle in hay


# --------------------------------------------------------------------------
# Découverte des fichiers du projet
# --------------------------------------------------------------------------
def find_context_files(project_dir: Path) -> list[Path]:
    """Retourne la liste des .item du dossier context/ (contextes externes)."""
    return [p for p in project_dir.rglob("*.item")
            if "/context/" in str(p).replace("\\", "/")]


def find_job_files(project_dir: Path) -> list[Path]:
    """Retourne la liste des .item de jobs (hors dossier context/)."""
    return [p for p in project_dir.rglob("*.item")
            if "/context/" not in str(p).replace("\\", "/")]


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
    Lit un .item (contexte externe OU job) et retourne
    {context_group: {var_name: raw_value}}.
    """
    out: dict[str, dict[str, str]] = defaultdict(dict)
    try:
        tree = ET.parse(item_path)
    except ET.ParseError:
        return out

    root = tree.getroot()

    # Format avec <context name="ENV">
    for ctx in iter_local(root, "context"):
        env = ctx.get("name", "Default")
        for cp in iter_local(ctx, "contextParameter"):
            pname = cp.get("name")
            pvalue = cp.get("value") or cp.get("rawValue") or ""
            if pname:
                out[env][pname] = pvalue

    # Format simple sans <context> englobant
    if not out:
        for cp in iter_local(root, "contextParameter"):
            pname = cp.get("name")
            pvalue = cp.get("value", "")
            if pname:
                out["Default"][pname] = pvalue

    return out


def find_declarations(project_dir: Path,
                      matcher: Matcher) -> list[ContextDeclaration]:
    """Scanne tous les contextes (externes + embarqués) et retourne les
    déclarations de variables dont la valeur matche."""
    decls: list[ContextDeclaration] = []

    # 1) Contextes externes (dossier context/)
    for ctx_path in find_context_files(project_dir):
        data = extract_contexts_from_file(ctx_path)
        for env, params in data.items():
            for var_name, value in params.items():
                if matcher.matches(value):
                    decls.append(ContextDeclaration(
                        source_type="external",
                        source_file=ctx_path,
                        job_name=None,
                        context_group=env,
                        var_name=var_name,
                        value=value,
                    ))

    # 2) Contextes embarqués dans les jobs
    for job_path in find_job_files(project_dir):
        data = extract_contexts_from_file(job_path)
        for env, params in data.items():
            for var_name, value in params.items():
                if matcher.matches(value):
                    decls.append(ContextDeclaration(
                        source_type="embedded",
                        source_file=job_path,
                        job_name=job_name_from_item(job_path),
                        context_group=env,
                        var_name=var_name,
                        value=value,
                    ))

    return decls


# --------------------------------------------------------------------------
# Scan des jobs pour les usages
# --------------------------------------------------------------------------
CONTEXT_REF_RE = re.compile(r"context\.(\w+)")


def extract_context_refs(value: str) -> list[str]:
    """Retourne la liste des variables context.XXX référencées dans value."""
    if not value:
        return []
    return CONTEXT_REF_RE.findall(value)


def find_usages(project_dir: Path,
                matcher: Matcher,
                matching_vars: set[str]
                ) -> tuple[list[JobUsage], list[JobUsage]]:
    """
    Scanne tous les jobs et retourne (direct_usages, indirect_usages).

    - Direct : la valeur brute du paramètre matche le pattern
    - Indirect : la valeur référence une context.VAR où VAR appartient à
                 matching_vars (déclarations déjà trouvées)
    """
    direct: list[JobUsage] = []
    indirect: list[JobUsage] = []

    for job_path in find_job_files(project_dir):
        job_name = job_name_from_item(job_path)
        try:
            tree = ET.parse(job_path)
        except ET.ParseError:
            continue

        root = tree.getroot()

        for node in iter_local(root, "node"):
            comp_name = node.get("componentName", "")
            unique = ""
            for ep in iter_local(node, "elementParameter"):
                if ep.get("name") == "UNIQUE_NAME":
                    unique = (ep.get("value") or "").strip('"')
                    break
            unique = unique or comp_name

            for ep in iter_local(node, "elementParameter"):
                pname = ep.get("name", "")
                pvalue = ep.get("value", "") or ""
                # Certains paramètres stockent la valeur dans <elementValue>
                if not pvalue:
                    children_vals = [ev.get("value", "")
                                     for ev in iter_local(ep, "elementValue")
                                     if ev.get("value")]
                    pvalue = " | ".join(children_vals)

                if not pvalue:
                    continue

                # USAGE DIRECT : le pattern apparait en dur dans la valeur
                if matcher.matches(pvalue):
                    direct.append(JobUsage(
                        job_name=job_name,
                        job_file=job_path,
                        component=comp_name,
                        unique_name=unique,
                        param_name=pname,
                        raw_value=pvalue,
                        match_type="direct",
                    ))
                    continue  # pas la peine de chercher en indirect

                # USAGE INDIRECT : la valeur référence une variable matchante
                refs = extract_context_refs(pvalue)
                matching_refs = [r for r in refs if r in matching_vars]
                if matching_refs:
                    indirect.append(JobUsage(
                        job_name=job_name,
                        job_file=job_path,
                        component=comp_name,
                        unique_name=unique,
                        param_name=pname,
                        raw_value=pvalue,
                        match_type="indirect",
                        via_variables=matching_refs,
                    ))

    return direct, indirect


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def search(project_dir: Path, matcher: Matcher,
           verbose: bool = False) -> SearchResult:
    result = SearchResult(pattern=matcher.pattern_raw)

    if verbose:
        print(f"  [debug] Scan des contextes...", file=sys.stderr)
    result.declarations = find_declarations(project_dir, matcher)
    if verbose:
        print(f"  [debug] {len(result.declarations)} déclarations trouvées",
              file=sys.stderr)
        print(f"  [debug] Scan des jobs...", file=sys.stderr)

    matching_vars = result.matching_var_names
    direct, indirect = find_usages(project_dir, matcher, matching_vars)
    result.direct_usages = direct
    result.indirect_usages = indirect

    if verbose:
        print(f"  [debug] {len(direct)} usages directs, "
              f"{len(indirect)} usages indirects",
              file=sys.stderr)

    return result


# --------------------------------------------------------------------------
# Restitution
# --------------------------------------------------------------------------
def print_report_text(r: SearchResult) -> None:
    print("\n" + "=" * 80)
    print(f" RECHERCHE : {r.pattern}".center(80))
    print("=" * 80)

    # Déclarations de contexte, groupées par variable
    by_var: dict[str, list[ContextDeclaration]] = defaultdict(list)
    for d in r.declarations:
        by_var[d.var_name].append(d)

    print(f"\n📋 DÉCLARATIONS DANS LES CONTEXTES ({len(by_var)} variable(s), "
          f"{len(r.declarations)} occurrence(s))")
    print("-" * 80)
    if not by_var:
        print("  (aucune)")
    else:
        for var_name, entries in sorted(by_var.items()):
            # On groupe les entrées par fichier source pour compacter
            by_source: dict[tuple, list[ContextDeclaration]] = defaultdict(list)
            for e in entries:
                key = (e.source_type, str(e.source_file),
                       e.job_name or "")
                by_source[key].append(e)

            print(f"\n  • Variable : {var_name}")
            for (stype, sfile, jname), envs in by_source.items():
                source_label = (f"{jname} (embarqué dans le job)"
                                if stype == "embedded" else "contexte externe")
                print(f"    Source    : {sfile}")
                print(f"                {source_label}")
                print(f"    Contextes :")
                for e in sorted(envs, key=lambda x: x.context_group):
                    print(f"      - {e.context_group:<10} : {e.value}")

    # Usages directs
    print(f"\n🎯 USAGES DIRECTS ({len(r.direct_usages)})")
    print("-" * 80)
    if not r.direct_usages:
        print("  (aucun)")
    else:
        for u in r.direct_usages:
            print(f"\n  • Job       : {u.job_name}")
            print(f"    Fichier   : {u.job_file}")
            print(f"    Composant : {u.component} / {u.unique_name}")
            print(f"    Paramètre : {u.param_name}")
            print(f"    Valeur    : {u.raw_value}")

    # Usages indirects
    print(f"\n🔗 USAGES INDIRECTS VIA CONTEXTE ({len(r.indirect_usages)})")
    print("-" * 80)
    if not r.indirect_usages:
        print("  (aucun)")
    else:
        for u in r.indirect_usages:
            print(f"\n  • Job       : {u.job_name}")
            print(f"    Fichier   : {u.job_file}")
            print(f"    Composant : {u.component} / {u.unique_name}")
            print(f"    Paramètre : {u.param_name}")
            print(f"    Expression: {u.raw_value}")
            print(f"    Variable(s): {', '.join(u.via_variables)}")

    # Synthèse
    print("\n" + "=" * 80)
    print("📊 SYNTHÈSE")
    print(f"  Pattern recherché        : {r.pattern}")
    print(f"  Variables de contexte    : {len(by_var)} (matches dans "
          f"{len(r.declarations)} environnement(s))")
    print(f"  Jobs avec usage direct   : "
          f"{len({u.job_name for u in r.direct_usages})}")
    print(f"  Jobs avec usage indirect : "
          f"{len({u.job_name for u in r.indirect_usages})}")
    total_jobs = len({u.job_name for u in r.direct_usages + r.indirect_usages})
    print(f"  Jobs concernés au total  : {total_jobs}")
    print()


def export_markdown(r: SearchResult, out_path: Path) -> None:
    lines: list[str] = []
    lines.append(f"# Recherche : `{r.pattern}`\n")

    # Déclarations
    by_var: dict[str, list[ContextDeclaration]] = defaultdict(list)
    for d in r.declarations:
        by_var[d.var_name].append(d)

    lines.append(f"## Déclarations dans les contextes "
                 f"({len(by_var)} variables)\n")
    if not by_var:
        lines.append("_Aucune_\n")
    else:
        lines.append("| Variable | Source | Type | Contexte | Valeur |")
        lines.append("|---|---|---|---|---|")
        for var_name, entries in sorted(by_var.items()):
            for e in sorted(entries,
                            key=lambda x: (str(x.source_file),
                                           x.context_group)):
                src = (f"{e.job_name}" if e.source_type == "embedded"
                       else e.source_file.name)
                stype = ("embarqué" if e.source_type == "embedded"
                         else "externe")
                lines.append(f"| `{var_name}` | `{src}` | {stype} | "
                             f"{e.context_group} | `{e.value}` |")

    lines.append(f"\n## Usages directs ({len(r.direct_usages)})\n")
    if not r.direct_usages:
        lines.append("_Aucun_\n")
    else:
        lines.append("| Job | Composant | Nom | Paramètre | Valeur |")
        lines.append("|---|---|---|---|---|")
        for u in r.direct_usages:
            lines.append(f"| `{u.job_name}` | {u.component} | "
                         f"{u.unique_name} | {u.param_name} | "
                         f"`{u.raw_value}` |")

    lines.append(f"\n## Usages indirects via contexte "
                 f"({len(r.indirect_usages)})\n")
    if not r.indirect_usages:
        lines.append("_Aucun_\n")
    else:
        lines.append("| Job | Composant | Nom | Paramètre | Expression "
                     "| Variable(s) |")
        lines.append("|---|---|---|---|---|---|")
        for u in r.indirect_usages:
            lines.append(f"| `{u.job_name}` | {u.component} | "
                         f"{u.unique_name} | {u.param_name} | "
                         f"`{u.raw_value}` | "
                         f"{', '.join('`' + v + '`' for v in u.via_variables)} |")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n✓ Rapport Markdown écrit dans : {out_path}")


def export_json(r: SearchResult, out_path: Path) -> None:
    def serialize(obj):
        if isinstance(obj, Path):
            return str(obj)
        if hasattr(obj, "__dict__"):
            return asdict(obj)
        return obj

    data = {
        "pattern": r.pattern,
        "declarations": [{**asdict(d), "source_file": str(d.source_file)}
                         for d in r.declarations],
        "direct_usages": [{**asdict(u), "job_file": str(u.job_file)}
                          for u in r.direct_usages],
        "indirect_usages": [{**asdict(u), "job_file": str(u.job_file)}
                            for u in r.indirect_usages],
    }
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"\n✓ Rapport JSON écrit dans : {out_path}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Recherche l'usage d'un nom de fichier dans un projet Talend "
                    "(contextes + jobs, directs + indirects).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--project", "-p", required=True, type=Path,
                        help="Répertoire racine du projet Talend")
    parser.add_argument("--name", "-n", required=True,
                        help="Nom de fichier ou pattern à rechercher "
                             "(substring par défaut)")
    parser.add_argument("--regex", action="store_true",
                        help="Interpréter --name comme une regex Python")
    parser.add_argument("--case-sensitive", action="store_true",
                        help="Rendre la recherche sensible à la casse "
                             "(insensible par défaut)")
    parser.add_argument("--format", "-f",
                        choices=["text", "markdown", "json"],
                        default="text",
                        help="Format de sortie")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Fichier de sortie pour markdown/json")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Mode diagnostic")
    args = parser.parse_args()

    if not args.project.is_dir():
        sys.exit(f"❌ Le répertoire projet n'existe pas : {args.project}")

    try:
        matcher = Matcher(args.name, args.regex, args.case_sensitive)
    except re.error as e:
        sys.exit(f"❌ Regex invalide : {e}")

    mode = ("regex" if args.regex else "substring")
    case = ("sensible" if args.case_sensitive else "insensible")
    print(f"🔍 Recherche de '{args.name}' dans {args.project}")
    print(f"   Mode : {mode}, casse {case}")

    result = search(args.project, matcher, verbose=args.verbose)

    if args.format == "text":
        print_report_text(result)
    elif args.format == "markdown":
        safe = re.sub(r"[^\w\-.]+", "_", args.name)[:40]
        out = args.output or Path(f"recherche_{safe}.md")
        export_markdown(result, out)
    elif args.format == "json":
        safe = re.sub(r"[^\w\-.]+", "_", args.name)[:40]
        out = args.output or Path(f"recherche_{safe}.json")
        export_json(result, out)


if __name__ == "__main__":
    main()
