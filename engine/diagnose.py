#!/usr/bin/env python3
"""
DebugTrace Diagnosis Engine
Takes analysis JSON, calls OpenRouter API, returns diagnosis.
"""

import sys
import json
import os
import argparse
import urllib.request
import urllib.error
from pathlib import Path

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "openrouter/auto"


def build_prompt(analysis: dict) -> str:
    lines = []
    file_path = analysis.get("file", "unknown")
    lang = analysis.get("language", "unknown")
    all_files = analysis.get("all_files_checked", [file_path])

    lines.append(f"You are a {lang} debugging assistant. Analyze errors from `{Path(file_path).name}`.\n")

    if len(all_files) > 1:
        lines.append(f"**Project files checked:** {', '.join(Path(f).name for f in all_files)}\n")

    compiler_errors = analysis.get("compiler_errors", [])
    if compiler_errors:
        lines.append("## Errors / Warnings\n")
        for e in compiler_errors[:10]:
            err_file = e.get("file", "")
            prefix = f"`{Path(err_file).name}` line {e['line']}: " if e.get("line") and err_file else ""
            lines.append(f"- {prefix}{e.get('message', e.get('raw', ''))}")
            if e.get("cross_file_source"):
                cross = e["cross_file_source"]
                lines.append(f"  ↳ Cross-file: {cross.get('reason', '')} (`{Path(cross['file']).name}`)")
        lines.append("")

    ctx = analysis.get("error_context")
    if ctx:
        lines.append(f"## Function with error: `{ctx['name']}` (lines {ctx['start_line']}–{ctx['end_line']}, complexity={ctx['complexity']})\n")
        lines.append(f"```{lang if lang != 'javascript' else 'js'}")
        lines.append(ctx["body"][:1000])
        lines.append("```\n")

    import_graph = analysis.get("import_graph", {})
    if import_graph:
        lines.append("## File dependencies\n")
        for f, deps in list(import_graph.items())[:5]:
            lines.append(f"- `{Path(f).name}` imports: {', '.join(Path(d).name for d in deps)}")
        lines.append("")

    lines.append("## Task")
    lines.append("1. Explain what is wrong in plain English (2-3 sentences). If it's a cross-file issue, say which file is the root cause.")
    lines.append("2. Show the corrected code snippet.")
    lines.append("3. In one sentence, explain why this fix works.")
    lines.append("4. If complexity >= 5, suggest one refactor tip.")
    lines.append("\nBe concise. Format in Markdown.")
    return "\n".join(lines)


def diagnose(analysis: dict, api_key: str) -> dict:
    if not analysis.get("compiler_errors") and not analysis.get("error_context"):
        return {
            "diagnosis": "No errors detected. Your code compiled cleanly.",
            "functions": analysis.get("functions", []),
        }

    prompt = build_prompt(analysis)

    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1000,
    }).encode("utf-8")

    req = urllib.request.Request(
        OPENROUTER_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/jahnavi7806/debugtrace",
            "X-Title": "DebugTrace",
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"]
            return {
                "diagnosis": text,
                "functions": analysis.get("functions", []),
                "compiler_errors": analysis.get("compiler_errors", []),
            }
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        return {"error": f"API error {e.code}: {body}"}
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DebugTrace diagnosis via OpenRouter API")
    ap.add_argument("analysis_json", help="Path to analysis JSON or '-' for stdin")
    ap.add_argument("--api-key", default=os.environ.get("OPENROUTER_API_KEY", ""), help="OpenRouter API key")
    args = ap.parse_args()

    if not args.api_key:
        print(json.dumps({"error": "OPENROUTER_API_KEY not set"}))
        sys.exit(1)

    if args.analysis_json == "-":
        analysis = json.load(sys.stdin)
    else:
        with open(args.analysis_json) as f:
            analysis = json.load(f)

    result = diagnose(analysis, args.api_key)
    print(json.dumps(result, indent=2))
