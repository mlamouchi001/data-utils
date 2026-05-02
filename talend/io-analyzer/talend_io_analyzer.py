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

# Force UTF-8 sur stdout/stderr pour éviter les UnicodeEncodeError sur Windows
# lorsque la sortie est redirigée ou pipée (cp1252 par défaut).
for _stream_name in ("stdout", "stderr"):
    _s = getattr(sys, _stream_name, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


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
# Helpers XML tolérants aux namespaces
# --------------------------------------------------------------------------
def local_name(tag: str) -> str:
    """Retourne le nom local d'une balise en strippant le namespace
    ('{ns}node' -> 'node', 'node' -> 'node')."""
    return tag.rsplit("}", 1)[-1]



def _is_excluded_path(p) -> bool:
    """Retourne True si le chemin doit être ignoré du scan (artefacts Maven,
    dossiers cachés, gestionnaires de version). Évite les FileNotFoundError
    sur OneDrive et les faux positifs sur les pom.properties Maven."""
    sp = str(p).replace("\\", "/")
    if "/target/" in sp or "/META-INF/" in sp or "/poms/" in sp:
        return True
    if "/.git/" in sp or "/.svn/" in sp or "/node_modules/" in sp:
        return True
    if "/.idea/" in sp or "/.vscode/" in sp:
        return True
    return False

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


def iter_local(root: ET.Element, name: str):
    """Itère récursivement sur les éléments dont le nom local vaut `name`,
    peu importe le namespace."""
    for elem in root.iter():
        if local_name(elem.tag) == name:
            yield elem


def find_local(elem: ET.Element, name: str) -> list[ET.Element]:
    """Retourne les enfants directs dont le nom local vaut `name`."""
    return [c for c in elem if local_name(c.tag) == name]


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
        if _is_excluded_path(item):
            continue
        if pattern.match(item.name):
            return item
    return None


def build_id_to_name_map(project_dir: Path,
                         verbose: bool = False) -> dict[str, str]:
    """
    Construit un mapping {job_id_talend: label_du_job} à partir des fichiers
    .properties de Talend. Utile quand tRunJob référence un sous-job par son
    ID interne plutôt que par son nom.

    Structure réelle des .properties Talend Studio :
        <xmi:XMI xmlns:xmi="..." xmlns:TalendProperties="...">
          <TalendProperties:Property id="_xxx" label="JOB_NAME" ...>
            ...
          </TalendProperties:Property>
        </xmi:XMI>

    Certains export plus anciens / simplifiés ont Property directement en
    racine. On gère les deux cas.
    """
    mapping: dict[str, str] = {}
    scanned = 0
    errors = 0

    for props in project_dir.rglob("*.properties"):
        # Normalisation du chemin pour matcher les exclusions sur Win/Linux
        sp = str(props).replace("\\", "/")

        # Exclure les contextes Talend
        if "/context/" in sp:
            continue

        # Exclure les artefacts Maven (target/, META-INF/maven/, pom.properties)
        # qui contiennent des fichiers non Talend et qui peuvent disparaître
        # pendant le scan (OneDrive sync, antivirus, build en cours)
        if "/target/" in sp or "/META-INF/" in sp or props.name == "pom.properties":
            continue

        # Exclure le dossier poms/ à la racine (projets Talend exportés Maven)
        if "/poms/" in sp:
            continue

        scanned += 1
        try:
            tree = ET.parse(props)
        except (ET.ParseError, FileNotFoundError, OSError, PermissionError) as e:
            errors += 1
            if verbose:
                print(f"  [debug] ignoré: {props.name} ({type(e).__name__})",
                      file=sys.stderr)
            continue

        root = tree.getroot()

        # 1. Racine = Property (ancien format / fixture simple)
        rlabel = root.get("label")
        rid = root.get("id")
        if rlabel and rid:
            mapping[rid] = rlabel
            continue

        # 2. Property imbriqué quelque part (format Talend Studio standard)
        #    On cherche tout élément dont le nom local est 'Property'
        found = False
        for prop in iter_local(root, "Property"):
            lbl = prop.get("label") or prop.get("displayName")
            pid = prop.get("id")
            if lbl and pid:
                mapping[pid] = lbl
                found = True
                break

        # 3. Fallback ultime : n'importe quel élément avec label+id
        if not found:
            for elem in root.iter():
                lbl = elem.get("label") or elem.get("displayName")
                eid = elem.get("id")
                if lbl and eid and eid.startswith("_"):
                    mapping[eid] = lbl
                    break

    if verbose:
        print(f"  [debug] {scanned} .properties scannés, "
              f"{len(mapping)} IDs indexés"
              + (f" ({errors} erreurs de parsing)" if errors else ""),
              file=sys.stderr)

    return mapping


def collect_available_contexts(project_dir: Path,
                               job_name: Optional[str] = None) -> list[str]:
    """
    Retourne la liste triée des environnements de contexte détectés dans le
    projet. Si job_name est fourni, on agrège les environnements du .item du
    job ET ceux des fichiers de contexte externes (union).
    """
    envs: set[str] = set()

    # Contextes embarqués dans le .item du job cible
    if job_name:
        item = find_job_item(project_dir, job_name)
        if item:
            try:
                for ctx in iter_local(ET.parse(item).getroot(), "context"):
                    name = ctx.get("name")
                    if name:
                        envs.add(name)
            except ET.ParseError:
                pass

    # Contextes externes (dossier context/)
    for cpath in find_context_files(project_dir).values():
        try:
            for ctx in iter_local(ET.parse(cpath).getroot(), "context"):
                name = ctx.get("name")
                if name:
                    envs.add(name)
        except ET.ParseError:
            pass

    return sorted(envs)


def match_context(requested: str, available: list[str]) -> Optional[str]:
    """Retourne le nom exact du contexte (casse préservée) correspondant à la
    demande de l'utilisateur (insensible à la casse). None si introuvable."""
    for env in available:
        if env.lower() == requested.lower():
            return env
    return None


def find_context_files(project_dir: Path) -> dict[str, Path]:
    """Retourne {nom_du_groupe_de_contexte: chemin_item}."""
    contexts: dict[str, Path] = {}
    for path in project_dir.rglob("*.item"):
        if _is_excluded_path(path):
            continue
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
    for ctx in iter_local(root, "context"):
        env_name = ctx.get("name", "Default")
        for cp in iter_local(ctx, "contextParameter"):
            pname = cp.get("name")
            pvalue = cp.get("value", "") or cp.get("rawValue", "")
            if pname:
                contexts_by_env[env_name][pname] = pvalue

    if not contexts_by_env:
        # Format simple: <contextParameter name="..." value="...">
        for cp in iter_local(root, "contextParameter"):
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
    """Récupère la valeur d'un <elementParameter name=...>
    (tolérant aux namespaces)."""
    for ep in iter_local(node, "elementParameter"):
        if ep.get("name") == param_name:
            return ep.get("value")
    return None


def extract_subjob_name(node: ET.Element,
                        id_to_name: dict[str, str],
                        verbose: bool = False) -> Optional[str]:
    """
    Extrait le nom du sous-job appelé par un tRunJob / tRunJobOnGrid.
    Supporte tous les formats Talend connus :
      1. Plat        : <elementParameter name="PROCESS:PROCESS_TYPE_PROCESS" value="..."/>
      2. Imbriqué    : <elementParameter name="PROCESS">
                           <elementValue elementRef="PROCESS_TYPE_PROCESS" value="..."/>
                       </elementParameter>
      3. Variante    : elementValue avec attribut @name au lieu de @elementRef
    Si la valeur ressemble à un ID Talend (_xxxxx), on la résout via id_to_name.
    """
    raw: Optional[str] = None

    # Format 1 : paramètre plat
    for candidate in ("PROCESS:PROCESS_TYPE_PROCESS",
                      "PROCESS_TYPE_PROCESS"):
        flat = get_node_param(node, candidate)
        if flat:
            raw = flat
            break

    # Format 2 : paramètre imbriqué avec <elementValue>
    if not raw:
        for ep in iter_local(node, "elementParameter"):
            pname = ep.get("name", "")
            if pname not in ("PROCESS", "PROCESS_TYPE_PROCESS"):
                continue
            # on essaie @value direct d'abord
            if ep.get("value"):
                raw = ep.get("value")
                break
            # puis les <elementValue> enfants
            for ev in iter_local(ep, "elementValue"):
                ref = ev.get("elementRef") or ev.get("name") or ""
                if "PROCESS_TYPE_PROCESS" in ref or ref == "PROCESS":
                    raw = ev.get("value")
                    if raw:
                        break
            if raw:
                break

    # Format 3 : fallback ultime - tout elementParameter avec PROCESS dans le nom
    if not raw:
        for ep in iter_local(node, "elementParameter"):
            if "PROCESS_TYPE_PROCESS" in (ep.get("name") or ""):
                raw = ep.get("value")
                if raw:
                    break

    if not raw:
        if verbose:
            unique = get_node_param(node, "UNIQUE_NAME") or "?"
            print(f"    [debug] tRunJob sans paramètre PROCESS reconnu: {unique}",
                  file=sys.stderr)
        return None

    # Nettoyage : guillemets
    cleaned = raw.strip().strip('"').strip()
    # Certains formats stockent "JOB_NAME:version:ID" -> on garde le premier
    if ":" in cleaned and not cleaned.startswith("_"):
        cleaned = cleaned.split(":")[0].strip()

    if not cleaned:
        return None

    # Résolution par ID Talend
    if cleaned.startswith("_"):
        if cleaned in id_to_name:
            resolved = id_to_name[cleaned]
            if verbose:
                print(f"    [debug] ID {cleaned} résolu -> {resolved}",
                      file=sys.stderr)
            return resolved
        else:
            print(f"  ⚠ ID Talend '{cleaned}' introuvable dans les "
                  f".properties scannés. Vérifier que le sous-job existe "
                  f"dans le projet ciblé.", file=sys.stderr)
            return cleaned

    return cleaned


def analyze_job(job_name: str,
                item_path: Path,
                context_values: dict[str, str],
                id_to_name: Optional[dict[str, str]] = None,
                verbose: bool = False,
                dump_runjobs: bool = False) -> JobIO:
    """Analyse un job Talend et extrait ses I/O + ses sous-jobs."""
    result = JobIO(job_name, item_path)
    id_to_name = id_to_name or {}

    try:
        tree = ET.parse(item_path)
    except ET.ParseError as e:
        print(f"  ⚠ Erreur parsing {item_path}: {e}", file=sys.stderr)
        return result

    root = tree.getroot()

    all_nodes = list(iter_local(root, "node"))
    if verbose:
        print(f"    [debug] {len(all_nodes)} <node> trouvés dans {item_path.name}",
              file=sys.stderr)

    for node in all_nodes:
        comp_name = node.get("componentName", "")
        unique_name_raw = get_node_param(node, "UNIQUE_NAME") or ""
        unique_name = unique_name_raw.strip('"') or comp_name

        # Détection élargie : tRunJob, tRunJobOnGrid, toute variante tRunJob*
        if comp_name.startswith("tRunJob"):
            if dump_runjobs:
                print(f"\n--- DUMP tRunJob: {unique_name} "
                      f"(componentName={comp_name}) dans {job_name} ---",
                      file=sys.stderr)
                # On reconstruit un XML lisible du node
                dump = ET.tostring(node, encoding="unicode")
                print(dump, file=sys.stderr)
                print("--- FIN DUMP ---", file=sys.stderr)

            subjob_name = extract_subjob_name(node, id_to_name, verbose)
            if subjob_name and subjob_name not in result.subjobs:
                result.subjobs.append(subjob_name)
                if verbose:
                    print(f"    [debug] tRunJob {unique_name} -> {subjob_name}",
                          file=sys.stderr)
            elif not subjob_name:
                # On liste les paramètres présents pour aider au diagnostic
                params = [ep.get("name") for ep in iter_local(node, "elementParameter")]
                print(f"  ⚠ tRunJob '{unique_name}' dans {job_name} : "
                      f"nom du sous-job non résolu",
                      file=sys.stderr)
                if verbose:
                    print(f"    [debug] paramètres présents: {params}",
                          file=sys.stderr)
            continue

        is_input = comp_name in INPUT_COMPONENTS
        is_output = comp_name in OUTPUT_COMPONENTS
        is_dual = comp_name in DUAL_COMPONENTS

        if not (is_input or is_output or is_dual):
            continue

        # On collecte tous les paramètres de chemin
        paths_found = []
        for ep in iter_local(node, "elementParameter"):
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
                      id_to_name: Optional[dict[str, str]] = None,
                      visited: Optional[set[str]] = None,
                      verbose: bool = False,
                      dump_runjobs: bool = False) -> list[JobIO]:
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

    # Caches initialisés une seule fois au premier appel
    if ext_contexts_cache is None:
        ext_contexts_cache = {}
        for cname, cpath in find_context_files(project_dir).items():
            ext_contexts_cache[cname] = parse_context_file(cpath, context_env)
    if id_to_name is None:
        id_to_name = build_id_to_name_map(project_dir, verbose=verbose)

    # Contextes embarqués dans le .item du job
    job_ctx = extract_job_contexts(item_path, context_env)

    # Fusion : on complète avec les contextes externes (sans écraser)
    for ext_values in ext_contexts_cache.values():
        for k, v in ext_values.items():
            job_ctx.setdefault(k, v)

    if verbose:
        print(f"  [debug] Analyse de {job_name} ({item_path})", file=sys.stderr)

    job_io = analyze_job(job_name, item_path, job_ctx, id_to_name,
                         verbose, dump_runjobs)
    results = [job_io]

    for subjob in job_io.subjobs:
        results.extend(analyze_recursive(
            project_dir, subjob, context_env,
            ext_contexts_cache, id_to_name, visited, verbose, dump_runjobs,
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


def run_report(jobs: list[JobIO],
               fmt: str,
               output: Optional[Path],
               job_name: str,
               env: Optional[str]) -> None:
    """Sort le rapport au format demandé. env optionnel pour nommer les fichiers."""
    if fmt == "text":
        print_report_text(jobs)
    elif fmt == "markdown":
        default = f"rapport_{job_name}"
        if env:
            default += f"_{env}"
        default += ".md"
        out = output or Path(default)
        export_markdown(jobs, out)
    elif fmt == "json":
        default = f"rapport_{job_name}"
        if env:
            default += f"_{env}"
        default += ".json"
        out = output or Path(default)
        export_json(jobs, out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Analyse les I/O d'un job Talend et de ses sous-jobs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--project", "-p", required=True, nargs="+",
                        help="Répertoire racine du projet Talend "
                             "(contient process/, context/, ...)")
    parser.add_argument("--job", "-j", default=None,
                        help="Nom du job à analyser (sans suffixe _x.y). "
                             "Requis sauf avec --list-contexts sans --job.")
    parser.add_argument("--context", "-c", default=None,
                        help="Environnement de contexte à utiliser "
                             "(ex. dev / homol / prod / DEV / UAT / PRD / Default). "
                             "Insensible à la casse. Défaut: Default")
    parser.add_argument("--list-contexts", "-l", action="store_true",
                        help="Liste les environnements de contexte détectés "
                             "dans le projet et sort. Utilisable avec ou sans --job.")
    parser.add_argument("--all-contexts", action="store_true",
                        help="Génère un rapport par environnement détecté "
                             "(fichiers séparés rapport_<job>_<env>.<ext>).")
    parser.add_argument("--format", "-f",
                        choices=["text", "markdown", "json"],
                        default="text",
                        help="Format de sortie")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Fichier de sortie pour markdown/json "
                             "(ignoré avec --all-contexts). "
                             "Défaut: rapport_<job>[_<env>].<ext>")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Mode diagnostic : affiche les étapes d'analyse "
                             "et la résolution des sous-jobs sur stderr")
    parser.add_argument("--dump-runjobs", action="store_true",
                        help="Dump sur stderr le XML brut de chaque composant "
                             "tRunJob* trouvé. Utile pour me partager le format "
                             "exact de ton projet si les sous-jobs ne sont pas "
                             "détectés.")
    args = parser.parse_args()

    args.project = validate_project_dir(args.project)

    # --list-contexts peut fonctionner sans --job
    if args.list_contexts:
        envs = collect_available_contexts(args.project, args.job)
        scope = f"pour le job '{args.job}' + externes" if args.job else "dans le projet"
        print(f"🔍 Contextes détectés {scope}:")
        if envs:
            for e in envs:
                print(f"  • {e}")
        else:
            print("  (aucun)")
        sys.exit(0)

    # Au-delà, --job devient obligatoire
    if not args.job:
        parser.error("--job est requis (sauf avec --list-contexts seul)")

    # Construction de la liste des contextes disponibles pour validation
    available = collect_available_contexts(args.project, args.job)

    # Validation de --context
    resolved_context = None
    if args.context:
        match = match_context(args.context, available)
        if available and not match:
            print(f"❌ Contexte '{args.context}' introuvable dans le projet.",
                  file=sys.stderr)
            print(f"   Contextes disponibles : {', '.join(available) or '(aucun)'}",
                  file=sys.stderr)
            print(f"   Astuce: lancer avec --list-contexts pour voir les options.",
                  file=sys.stderr)
            sys.exit(2)
        resolved_context = match or args.context

    # Mode --all-contexts
    if args.all_contexts:
        if not available:
            sys.exit("❌ Aucun contexte détecté, impossible d'utiliser --all-contexts.")
        print(f"🔍 Analyse du job '{args.job}' pour {len(available)} contexte(s) : "
              f"{', '.join(available)}")
        for env in available:
            print(f"\n{'#' * 80}")
            print(f"#  CONTEXTE : {env}")
            print(f"{'#' * 80}")
            jobs = analyze_recursive(args.project, args.job, env,
                                     verbose=args.verbose,
                                     dump_runjobs=args.dump_runjobs)
            if not jobs:
                print(f"  ⚠ Aucune analyse possible pour {env}", file=sys.stderr)
                continue
            # Avec --all-contexts, --output est ignoré (nom auto par env)
            run_report(jobs, args.format, None, args.job, env)
        return

    # Mode standard (un seul contexte)
    print(f"🔍 Analyse du job '{args.job}' dans {args.project}")
    if resolved_context:
        print(f"   Contexte ciblé : {resolved_context}")
    elif available:
        print(f"   Contexte ciblé : Default (disponibles: {', '.join(available)})")

    jobs = analyze_recursive(args.project, args.job, resolved_context,
                             verbose=args.verbose,
                             dump_runjobs=args.dump_runjobs)

    if not jobs:
        sys.exit(f"❌ Aucune analyse possible pour le job : {args.job}")

    run_report(jobs, args.format, args.output, args.job, resolved_context)


if __name__ == "__main__":
    main()
