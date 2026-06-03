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

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "openrouter/auto"


def build_prompt(analysis: dict) -> str:
    lines = []
    file_path = analysis.get("file", "unknown")
    lines.append(f"You are a C++ debugging assistant. Analyze the following code and errors from `{file_path}`.\n")

    compiler_errors = analysis.get("compiler_errors", [])
    if compiler_errors:
        lines.append("## Compiler / Runtime Errors\n")
        for e in compiler_errors[:10]:
            lines.append(f"- {e.get('raw', e.get('message', ''))}")
        lines.append("")

    ctx = analysis.get("error_context")
    if ctx:
        lines.append(f"## Function containing the error: `{ctx['name']}` (lines {ctx['start_line']}–{ctx['end_line']}, complexity={ctx['complexity']})\n")
        lines.append("```cpp")
        lines.append(ctx["body"])
        lines.append("```\n")

    functions = analysis.get("functions", [])
    high_complexity = [f for f in functions if f["complexity"] >= 5]
    if high_complexity and not ctx:
        lines.append("## High-Complexity Functions (possible problem areas)\n")
        for fn in high_complexity[:3]:
            lines.append(f"- `{fn['name']}` — complexity {fn['complexity']} (lines {fn['start_line']}–{fn['end_line']})")
            lines.append("```cpp")
            lines.append(fn["body"][:800])
            lines.append("```")
        lines.append("")

    lines.append("## Task")
    lines.append("1. Explain what is wrong in plain English (2-3 sentences max).")
    lines.append("2. Show the corrected code snippet.")
    lines.append("3. In one sentence, explain why this fix works.")
    lines.append("4. If complexity is high (>=5), suggest one refactor tip.")
    lines.append("\nBe concise. Format your response in Markdown.")

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
