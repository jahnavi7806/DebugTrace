#!/usr/bin/env python3
"""
DebugTrace Analysis Engine
Parses C++ files with tree-sitter, extracts function complexity + call graph,
and captures compiler/runtime error context.
"""

import sys
import json
import argparse
import subprocess
from pathlib import Path

try:
    import tree_sitter_cpp as tscpp
    from tree_sitter import Language, Parser
    CPP_LANGUAGE = Language(tscpp.language())
    parser = Parser(CPP_LANGUAGE)
    HAS_TREE_SITTER = True
except Exception:
    HAS_TREE_SITTER = False


# ── Cyclomatic complexity ────────────────────────────────────────────────────

BRANCH_TYPES = {
    "if_statement", "for_statement", "while_statement", "do_statement",
    "switch_statement", "case_statement", "catch_clause",
    "conditional_expression",  # ternary
    "binary_expression",       # && and ||
}

def cyclomatic_complexity(node) -> int:
    """Walk subtree and count decision points."""
    count = 0
    if node.type in BRANCH_TYPES:
        # Only count && / || inside binary_expression
        if node.type == "binary_expression":
            op_node = node.child_by_field_name("operator")
            if op_node and op_node.type in ("&&", "||"):
                count += 1
        else:
            count += 1
    for child in node.children:
        count += cyclomatic_complexity(child)
    return count


# ── Function extraction ──────────────────────────────────────────────────────

def extract_functions(source: bytes, tree):
    """Return list of {name, start_line, end_line, complexity, body} dicts."""
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
            functions.append({
                "name": name,
                "start_line": start,
                "end_line": end,
                "complexity": complexity,
                "body": snippet,
            })
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return functions


def _get_function_name(declarator) -> str:
    """Recursively find function name from declarator node."""
    if declarator is None:
        return "<anonymous>"
    if declarator.type == "identifier":
        return declarator.text.decode("utf-8", errors="replace")
    if declarator.type == "qualified_identifier":
        return declarator.text.decode("utf-8", errors="replace")
    # function_declarator wraps the actual name declarator
    inner = declarator.child_by_field_name("declarator")
    if inner:
        return _get_function_name(inner)
    for child in declarator.children:
        if child.type in ("identifier", "qualified_identifier"):
            return child.text.decode("utf-8", errors="replace")
    return declarator.text.decode("utf-8", errors="replace") if declarator.text else "<anonymous>"


# ── Compiler error parsing + runtime execution ───────────────────────────────

def compile_and_capture(filepath: str):
    """
    1. Compile with g++/clang++ (no ASAN — Windows compatible)
    2. Parse compile-time errors
    3. If clean, run binary and catch crashes
    4. Also do static out-of-bounds detection via AST
    """
    import tempfile, os, platform
    errors = []
    binary = None

    for compiler in ("g++", "clang++"):
        try:
            suffix = ".exe" if platform.system() == "Windows" else ""
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp.close()
            binary = tmp.name

            # Compile without ASAN (Windows compatible), but with all warnings
            compile_result = subprocess.run(
                [compiler, "-std=c++17", "-Wall", "-Wextra",
                 "-Warray-bounds", "-g", "-o", binary, filepath],
                capture_output=True, text=True, timeout=30
            )

            # Parse compile-time errors/warnings
            for line in compile_result.stderr.splitlines():
                parts = line.split(":")
                if len(parts) >= 4:
                    try:
                        err_line = int(parts[1])
                        err_col  = int(parts[2])
                        severity = parts[3].strip()
                        message  = ":".join(parts[4:]).strip()
                        errors.append({
                            "file": parts[0],
                            "line": err_line,
                            "col": err_col,
                            "severity": severity,
                            "message": message,
                            "raw": line,
                        })
                    except (ValueError, IndexError):
                        if "error:" in line or "warning:" in line:
                            errors.append({"raw": line, "line": None, "message": line})

            # If compiled clean, run the binary and catch crashes
            if compile_result.returncode == 0 and os.path.exists(binary):
                try:
                    run_result = subprocess.run(
                        [binary],
                        capture_output=True, text=True,
                        timeout=10, input=""
                    )
                    if run_result.returncode != 0:
                        runtime_output = (run_result.stderr + run_result.stdout).strip()
                        if runtime_output:
                            errors.append({
                                "raw": runtime_output[:500],
                                "line": None,
                                "message": runtime_output[:500],
                                "severity": "runtime error",
                            })
                        else:
                            errors.append({
                                "raw": f"Program crashed (exit code {run_result.returncode})",
                                "line": None,
                                "message": f"Runtime crash — exit code {run_result.returncode}. Possible causes: array out-of-bounds, null pointer dereference, stack overflow.",
                                "severity": "runtime error",
                            })
                except subprocess.TimeoutExpired:
                    errors.append({
                        "raw": "Program timed out (>10s)",
                        "line": None,
                        "message": "Infinite loop or deadlock detected — program did not finish in 10 seconds.",
                        "severity": "runtime error",
                    })
                except Exception as e:
                    pass
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

    # Static out-of-bounds check via source scan (catches arr[N] where N >= declared size)
    errors.extend(_static_bounds_check(filepath))
    return errors


def _static_bounds_check(filepath: str) -> list:
    """Simple regex-based static check for obvious array out-of-bounds access."""
    import re
    errors = []
    try:
        source = Path(filepath).read_text(encoding="utf-8", errors="replace")
        lines = source.splitlines()

        # Find array declarations: type name[SIZE]
        decl_pattern = re.compile(r'\b(\w+)\s*\[(\d+)\]')
        array_sizes: dict[str, int] = {}
        for line in lines:
            for m in decl_pattern.finditer(line):
                array_sizes[m.group(1)] = int(m.group(2))

        # Find array accesses: name[INDEX] where INDEX is a literal
        access_pattern = re.compile(r'\b(\w+)\s*\[(\d+)\]')
        for i, line in enumerate(lines, 1):
            for m in access_pattern.finditer(line):
                name, idx = m.group(1), int(m.group(2))
                if name in array_sizes and idx >= array_sizes[name]:
                    msg = f"Array out-of-bounds: '{name}' has size {array_sizes[name]} but accessed at index {idx}"
                    errors.append({
                        "raw": f"{filepath}:{i}: warning: {msg}",
                        "line": i,
                        "col": m.start(),
                        "severity": "warning",
                        "message": msg,
                    })
    except Exception:
        pass
    return errors






# ── Context extraction ───────────────────────────────────────────────────────

def get_context_for_line(functions, error_line: int):
    """Find which function contains the error line, return its body."""
    if error_line is None:
        return None
    for fn in functions:
        if fn["start_line"] <= error_line <= fn["end_line"]:
            return fn
    return None


# ── Main ─────────────────────────────────────────────────────────────────────

def analyze(filepath: str, error_line: int = None):
    path = Path(filepath)
    if not path.exists():
        return {"error": f"File not found: {filepath}"}

    source = path.read_bytes()
    result = {
        "file": filepath,
        "functions": [],
        "compiler_errors": [],
        "error_context": None,
        "tree_sitter_available": HAS_TREE_SITTER,
    }

    # AST analysis
    if HAS_TREE_SITTER:
        tree = parser.parse(source)
        result["functions"] = extract_functions(source, tree)
    else:
        result["warning"] = "tree-sitter-cpp not installed; skipping AST analysis"

    # Compile + capture errors
    result["compiler_errors"] = compile_and_capture(filepath)

    # If error_line given or first error found, extract context
    target_line = error_line
    if target_line is None and result["compiler_errors"]:
        for e in result["compiler_errors"]:
            if e.get("line"):
                target_line = e["line"]
                break

    if target_line and result["functions"]:
        result["error_context"] = get_context_for_line(result["functions"], target_line)

    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DebugTrace C++ analysis engine")
    ap.add_argument("filepath", help="Path to .cpp/.cc/.h file")
    ap.add_argument("--line", type=int, default=None, help="Error line number for context extraction")
    args = ap.parse_args()

    output = analyze(args.filepath, args.line)
    print(json.dumps(output, indent=2))
