#!/usr/bin/env python3
"""
DebugTrace Analysis Engine v2
Multi-language, cross-file static analysis + runtime error capture.
Supports: C/C++, Python, JavaScript/TypeScript
"""

import sys
import json
import argparse
import subprocess
import tempfile
import os
import re
import platform
from pathlib import Path

try:
    import tree_sitter_cpp as tscpp
    from tree_sitter import Language, Parser
    CPP_LANGUAGE = Language(tscpp.language())
    cpp_parser = Parser(CPP_LANGUAGE)
    HAS_TREE_SITTER = True
except Exception:
    HAS_TREE_SITTER = False

IS_WINDOWS = platform.system() == "Windows"

CPP_EXTENSIONS   = {".cpp", ".cc", ".cxx", ".c"}
HEADER_EXTENSIONS = {".h", ".hpp"}
PY_EXTENSIONS    = {".py"}
JS_EXTENSIONS    = {".js", ".ts", ".jsx", ".tsx", ".mjs"}


# ════════════════════════════════════════════════════════════════════════════
# Language detection
# ════════════════════════════════════════════════════════════════════════════

def detect_language(filepath: str) -> str:
    ext = Path(filepath).suffix.lower()
    if ext in CPP_EXTENSIONS | HEADER_EXTENSIONS: return "cpp"
    if ext in PY_EXTENSIONS:                       return "python"
    if ext in JS_EXTENSIONS:                       return "javascript"
    return "unknown"


# ════════════════════════════════════════════════════════════════════════════
# File discovery
# ════════════════════════════════════════════════════════════════════════════

def find_project_files(filepath: str, lang: str) -> list[str]:
    """Find all related files in the same folder (and one level up if needed)."""
    folder = Path(filepath).parent
    if lang == "cpp":
        exts = CPP_EXTENSIONS | HEADER_EXTENSIONS
    elif lang == "python":
        exts = PY_EXTENSIONS
    elif lang == "javascript":
        exts = JS_EXTENSIONS
    else:
        return [filepath]

    files = [str(f) for f in folder.rglob("*") if f.suffix.lower() in exts
             and "node_modules" not in f.parts
             and "__pycache__" not in f.parts
             and ".git" not in f.parts]
    return files if files else [filepath]


# ════════════════════════════════════════════════════════════════════════════
# Import / include graph
# ════════════════════════════════════════════════════════════════════════════

def build_import_graph(files: list[str], lang: str) -> dict[str, list[str]]:
    """
    Returns {file: [files it imports/includes]} for cross-file error tracing.
    """
    graph = {}
    for f in files:
        try:
            source = Path(f).read_text(encoding="utf-8", errors="replace")
            deps = []
            if lang == "cpp":
                # #include "localfile.h"
                for m in re.finditer(r'#include\s+"([^"]+)"', source):
                    inc = m.group(1)
                    # Resolve relative to file's folder
                    candidate = Path(f).parent / inc
                    if candidate.exists():
                        deps.append(str(candidate))
            elif lang == "python":
                # import x / from x import y
                for m in re.finditer(r'^(?:from|import)\s+([\w.]+)', source, re.MULTILINE):
                    mod = m.group(1).split(".")[0]
                    candidate = Path(f).parent / (mod + ".py")
                    if candidate.exists():
                        deps.append(str(candidate))
            elif lang == "javascript":
                # require("./x") or import from "./x"
                for m in re.finditer(r'''(?:require|import)[^'"]*['"]([./][^'"]+)['"]''', source):
                    imp = m.group(1)
                    for ext in ["", ".js", ".ts", ".jsx", ".tsx"]:
                        candidate = Path(f).parent / (imp + ext)
                        if candidate.exists():
                            deps.append(str(candidate))
                            break
            graph[f] = deps
        except Exception:
            graph[f] = []
    return graph


def find_error_source(error_file: str, error_line: int, graph: dict[str, list[str]]) -> dict | None:
    """
    Given an error in error_file:error_line, check if any file that imports it
    is the likely cause (e.g. wrong call signature).
    Returns {file, line, reason} or None.
    """
    # Find who imports the erroring file
    importers = [f for f, deps in graph.items() if error_file in deps and f != error_file]
    if not importers:
        return None
    # Return the first importer as the cross-file context
    return {"file": importers[0], "reason": f"This file imports '{Path(error_file).name}' where the error originates"}


# ════════════════════════════════════════════════════════════════════════════
# C++ Analysis
# ════════════════════════════════════════════════════════════════════════════

BRANCH_TYPES = {
    "if_statement", "for_statement", "while_statement", "do_statement",
    "switch_statement", "case_statement", "catch_clause",
    "conditional_expression", "binary_expression",
}

def cyclomatic_complexity(node) -> int:
    count = 0
    if node.type in BRANCH_TYPES:
        if node.type == "binary_expression":
            op = node.child_by_field_name("operator")
            if op and op.type in ("&&", "||"):
                count += 1
        else:
            count += 1
    for child in node.children:
        count += cyclomatic_complexity(child)
    return count

def extract_functions(source: bytes, tree) -> list:
    functions = []
    def walk(node):
        if node.type in ("function_definition", "function_declaration"):
            declarator = node.child_by_field_name("declarator")
            name = _get_function_name(declarator) if declarator else "<anonymous>"
            start = node.start_point[0] + 1
            end = node.end_point[0] + 1
            body = node.child_by_field_name("body")
            complexity = cyclomatic_complexity(body) + 1 if body else 1
            snippet = source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
            functions.append({"name": name, "start_line": start, "end_line": end, "complexity": complexity, "body": snippet})
        for child in node.children:
            walk(child)
    walk(tree.root_node)
    return functions

def _get_function_name(declarator) -> str:
    if declarator is None: return "<anonymous>"
    if declarator.type == "identifier": return declarator.text.decode("utf-8", errors="replace")
    if declarator.type == "qualified_identifier": return declarator.text.decode("utf-8", errors="replace")
    inner = declarator.child_by_field_name("declarator")
    if inner: return _get_function_name(inner)
    for child in declarator.children:
        if child.type in ("identifier", "qualified_identifier"):
            return child.text.decode("utf-8", errors="replace")
    return declarator.text.decode("utf-8", errors="replace") if declarator.text else "<anonymous>"

def analyze_cpp(filepath: str, all_files: list[str]) -> dict:
    errors = []
    functions = []
    binary = None

    # AST analysis on the trigger file
    if HAS_TREE_SITTER:
        source = Path(filepath).read_bytes()
        tree = cpp_parser.parse(source)
        functions = extract_functions(source, tree)

    # Separate .cpp and .h files — only compile .cpp files
    cpp_files = [f for f in all_files if Path(f).suffix.lower() in CPP_EXTENSIONS]
    if not cpp_files:
        cpp_files = [filepath]

    for compiler in ("g++", "clang++"):
        try:
            suffix = ".exe" if IS_WINDOWS else ""
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp.close()
            binary = tmp.name

            result = subprocess.run(
                [compiler, "-std=c++17", "-Wall", "-Wextra", "-Warray-bounds", "-g",
                 "-o", binary] + cpp_files,
                capture_output=True, text=True, timeout=30
            )

            for line in result.stderr.splitlines():
                parts = line.split(":")
                if len(parts) >= 4:
                    try:
                        err_file = parts[0].strip()
                        err_line = int(parts[1])
                        err_col  = int(parts[2])
                        severity = parts[3].strip()
                        message  = ":".join(parts[4:]).strip()
                        errors.append({"file": err_file, "line": err_line, "col": err_col,
                                       "severity": severity, "message": message, "raw": line})
                    except (ValueError, IndexError):
                        if "error:" in line or "warning:" in line:
                            errors.append({"raw": line, "line": None, "message": line})

            # Run binary if compiled clean
            if result.returncode == 0 and os.path.exists(binary):
                try:
                    run = subprocess.run([binary], capture_output=True, text=True, timeout=10, input="")
                    if run.returncode != 0:
                        out = (run.stderr + run.stdout).strip()
                        errors.append({
                            "raw": out[:500] if out else f"Runtime crash (exit code {run.returncode})",
                            "line": None,
                            "message": out[:500] if out else f"Runtime crash — exit code {run.returncode}. Possible: out-of-bounds, null pointer, stack overflow.",
                            "severity": "runtime error",
                        })
                except subprocess.TimeoutExpired:
                    errors.append({"raw": "Infinite loop detected", "line": None,
                                   "message": "Program timed out (>10s) — possible infinite loop.", "severity": "runtime error"})
                finally:
                    try: os.unlink(binary)
                    except: pass

            break
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            errors.append({"raw": "Compilation timed out", "line": None, "message": "Compilation timed out"})
            break
        finally:
            if binary:
                try: os.unlink(binary)
                except: pass

    # Static out-of-bounds check
    errors.extend(_static_bounds_check(filepath))
    return {"functions": functions, "compiler_errors": errors}


def _static_bounds_check(filepath: str) -> list:
    errors = []
    try:
        source = Path(filepath).read_text(encoding="utf-8", errors="replace")
        lines = source.splitlines()
        decl_pattern = re.compile(r'\b(\w+)\s*\[(\d+)\]')
        array_sizes: dict[str, int] = {}
        for line in lines:
            for m in decl_pattern.finditer(line):
                array_sizes[m.group(1)] = int(m.group(2))
        access_pattern = re.compile(r'\b(\w+)\s*\[(\d+)\]')
        for i, line in enumerate(lines, 1):
            for m in access_pattern.finditer(line):
                name, idx = m.group(1), int(m.group(2))
                if name in array_sizes and idx >= array_sizes[name]:
                    msg = f"Array out-of-bounds: '{name}' has size {array_sizes[name]} but accessed at index {idx}"
                    errors.append({"raw": f"{filepath}:{i}: warning: {msg}", "line": i,
                                   "col": m.start(), "severity": "warning", "message": msg})
    except Exception:
        pass
    return errors


# ════════════════════════════════════════════════════════════════════════════
# Python Analysis
# ════════════════════════════════════════════════════════════════════════════

def analyze_python(filepath: str, all_files: list[str]) -> dict:
    errors = []
    functions = []

    # Syntax check all files
    for f in all_files:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", f],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                for line in (result.stderr or "").splitlines():
                    m = re.search(r'File "([^"]+)", line (\d+)', line)
                    if m:
                        errors.append({"file": m.group(1), "line": int(m.group(2)),
                                       "severity": "error", "message": line, "raw": line})
                    elif line.strip():
                        errors.append({"file": f, "line": None, "severity": "error",
                                       "message": line, "raw": line})
        except Exception as e:
            errors.append({"file": f, "line": None, "severity": "error", "message": str(e), "raw": str(e)})

    # Run the trigger file and capture runtime errors
    if not any(e.get("file") == filepath for e in errors):
        try:
            result = subprocess.run(
                [sys.executable, filepath],
                capture_output=True, text=True, timeout=10, input=""
            )
            if result.returncode != 0:
                tb = result.stderr.strip()
                # Parse traceback for all files involved
                for m in re.finditer(r'File "([^"]+)", line (\d+)', tb):
                    err_file, err_line = m.group(1), int(m.group(2))
                    if any(err_file == f or Path(err_file).name == Path(f).name for f in all_files):
                        errors.append({"file": err_file, "line": err_line,
                                       "severity": "runtime error", "message": tb[-300:], "raw": tb[-300:]})
                if not errors:
                    errors.append({"file": filepath, "line": None, "severity": "runtime error",
                                   "message": tb[-500:], "raw": tb[-500:]})
        except subprocess.TimeoutExpired:
            errors.append({"file": filepath, "line": None, "severity": "runtime error",
                           "message": "Program timed out (>10s) — possible infinite loop.", "raw": "Timeout"})

    # Extract function names via regex (no tree-sitter for Python yet)
    try:
        source = Path(filepath).read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(source, 1):
            m = re.match(r'\s*def (\w+)\s*\(', line)
            if m:
                functions.append({"name": m.group(1), "start_line": i, "end_line": i, "complexity": 1, "body": line.strip()})
    except Exception:
        pass

    return {"functions": functions, "compiler_errors": errors}


# ════════════════════════════════════════════════════════════════════════════
# JavaScript / TypeScript Analysis
# ════════════════════════════════════════════════════════════════════════════

def analyze_javascript(filepath: str, all_files: list[str]) -> dict:
    errors = []
    functions = []

    # Check if Node.js is available
    node_available = False
    for node_cmd in ("node", "node.exe"):
        try:
            subprocess.run([node_cmd, "--version"], capture_output=True, timeout=5)
            node_available = True
            node_exec = node_cmd
            break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    # TypeScript: tsc syntax check
    if Path(filepath).suffix.lower() in (".ts", ".tsx"):
        for tsc_cmd in ("tsc", "npx tsc"):
            try:
                result = subprocess.run(
                    tsc_cmd.split() + ["--noEmit", "--strict", filepath],
                    capture_output=True, text=True, timeout=30
                )
                for line in result.stdout.splitlines() + result.stderr.splitlines():
                    m = re.match(r'(.+)\((\d+),(\d+)\):\s+(error|warning)\s+(TS\d+):\s+(.+)', line)
                    if m:
                        errors.append({"file": m.group(1), "line": int(m.group(2)), "col": int(m.group(3)),
                                       "severity": m.group(4), "message": f"{m.group(5)}: {m.group(6)}", "raw": line})
                break
            except FileNotFoundError:
                continue

    # Run JS file with Node and capture runtime errors
    if node_available and Path(filepath).suffix.lower() in (".js", ".mjs"):
        try:
            result = subprocess.run(
                [node_exec, filepath],
                capture_output=True, text=True, timeout=10, input=""
            )
            if result.returncode != 0:
                tb = result.stderr.strip()
                # Parse Node.js stack trace: at file.js:line
                for m in re.finditer(r'at .+ \((.+):(\d+):\d+\)', tb):
                    err_file, err_line = m.group(1), int(m.group(2))
                    if any(Path(err_file).name == Path(f).name for f in all_files):
                        errors.append({"file": err_file, "line": err_line,
                                       "severity": "runtime error", "message": tb[:500], "raw": tb[:500]})
                        break
                if not errors:
                    errors.append({"file": filepath, "line": None, "severity": "runtime error",
                                   "message": tb[:500], "raw": tb[:500]})
        except subprocess.TimeoutExpired:
            errors.append({"file": filepath, "line": None, "severity": "runtime error",
                           "message": "Program timed out (>10s) — possible infinite loop.", "raw": "Timeout"})

    # Extract function names via regex
    try:
        source = Path(filepath).read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(source, 1):
            m = re.match(r'\s*(?:async\s+)?function\s+(\w+)|const\s+(\w+)\s*=\s*(?:async\s+)?\(', line)
            if m:
                name = m.group(1) or m.group(2)
                if name:
                    functions.append({"name": name, "start_line": i, "end_line": i, "complexity": 1, "body": line.strip()})
    except Exception:
        pass

    return {"functions": functions, "compiler_errors": errors}


# ════════════════════════════════════════════════════════════════════════════
# Main entry point
# ════════════════════════════════════════════════════════════════════════════

def analyze(filepath: str, error_line: int = None):
    p = Path(filepath)
    if not p.exists():
        return {"error": f"File not found: {filepath}"}

    lang = detect_language(filepath)
    all_files = find_project_files(filepath, lang)
    import_graph = build_import_graph(all_files, lang)

    if lang == "cpp":
        result = analyze_cpp(filepath, all_files)
    elif lang == "python":
        result = analyze_python(filepath, all_files)
    elif lang == "javascript":
        result = analyze_javascript(filepath, all_files)
    else:
        result = {"functions": [], "compiler_errors": [
            {"raw": f"Unsupported file type: {p.suffix}", "line": None,
             "message": f"DebugTrace supports C/C++, Python, JS/TS. Got: {p.suffix}", "severity": "info"}
        ]}

    # Cross-file error enrichment
    for err in result["compiler_errors"]:
        err_file = err.get("file", filepath)
        err_line = err.get("line")
        if err_file and err_line:
            cross = find_error_source(err_file, err_line, import_graph)
            if cross:
                err["cross_file_source"] = cross

    # Error context (function containing first error)
    target_line = error_line
    if target_line is None and result["compiler_errors"]:
        for e in result["compiler_errors"]:
            if e.get("line") and (e.get("file", filepath) == filepath or
               Path(e.get("file", "")).name == p.name):
                target_line = e["line"]
                break

    error_context = None
    if target_line and result["functions"]:
        for fn in result["functions"]:
            if fn["start_line"] <= target_line <= fn["end_line"]:
                error_context = fn
                break

    return {
        "file": filepath,
        "language": lang,
        "all_files_checked": all_files,
        "functions": result["functions"],
        "compiler_errors": result["compiler_errors"],
        "error_context": error_context,
        "tree_sitter_available": HAS_TREE_SITTER,
        "import_graph": {k: v for k, v in import_graph.items() if v},
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DebugTrace multi-language analysis engine")
    ap.add_argument("filepath", help="Path to source file")
    ap.add_argument("--line", type=int, default=None, help="Error line number for context")
    args = ap.parse_args()
    print(json.dumps(analyze(args.filepath, args.line), indent=2))
