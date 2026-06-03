import * as vscode from "vscode";
import * as cp from "child_process";
import * as path from "path";

// ── Types ────────────────────────────────────────────────────────────────────

interface FunctionInfo {
  name: string;
  start_line: number;
  end_line: number;
  complexity: number;
  body: string;
}

interface CompilerError {
  file?: string;
  line?: number;
  col?: number;
  severity?: string;
  message: string;
  raw: string;
}

interface AnalysisResult {
  file: string;
  functions: FunctionInfo[];
  compiler_errors: CompilerError[];
  error_context: FunctionInfo | null;
  tree_sitter_available: boolean;
  warning?: string;
  error?: string;
}

interface DiagnosisResult {
  diagnosis: string;
  functions: FunctionInfo[];
  compiler_errors: CompilerError[];
  error?: string;
}

// ── Globals ──────────────────────────────────────────────────────────────────

let diagnosticCollection: vscode.DiagnosticCollection;
let outputChannel: vscode.OutputChannel;
let panel: vscode.WebviewPanel | undefined;
let statusBar: vscode.StatusBarItem;

// ── Activate ─────────────────────────────────────────────────────────────────

export function activate(context: vscode.ExtensionContext) {
  diagnosticCollection = vscode.languages.createDiagnosticCollection("debugtrace");
  outputChannel = vscode.window.createOutputChannel("DebugTrace");
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBar.text = "$(bug) DebugTrace";
  statusBar.command = "debugtrace.analyze";
  statusBar.show();

  context.subscriptions.push(
    diagnosticCollection,
    outputChannel,
    statusBar,

    vscode.commands.registerCommand("debugtrace.analyze", () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        vscode.window.showErrorMessage("DebugTrace: No active editor.");
        return;
      }
      runAnalysis(editor.document, context);
    }),

    vscode.commands.registerCommand("debugtrace.setApiKey", async () => {
      const key = await vscode.window.showInputBox({
        prompt: "Enter your Gemini API Key (get free key at aistudio.google.com/apikey)",
        password: true,
        ignoreFocusOut: true,
      });
      if (key) {
        await context.secrets.store("debugtrace.apiKey", key);
        vscode.window.showInformationMessage("DebugTrace: Gemini API key saved.");
      }
    }),

    vscode.workspace.onDidSaveTextDocument((doc) => {
      const config = vscode.workspace.getConfiguration("debugtrace");
      if (config.get<boolean>("autoAnalyzeOnSave") && isCppFile(doc)) {
        runAnalysis(doc, context);
      }
    })
  );

  outputChannel.appendLine("DebugTrace activated. Open a .cpp file and save, or run 'DebugTrace: Analyze Current File'.");
}

export function deactivate() {
  diagnosticCollection?.dispose();
}

// ── Core pipeline ─────────────────────────────────────────────────────────────

async function runAnalysis(doc: vscode.TextDocument, context: vscode.ExtensionContext) {
  if (!isCppFile(doc)) {
    vscode.window.showWarningMessage("DebugTrace only supports C/C++ files.");
    return;
  }

  const config = vscode.workspace.getConfiguration("debugtrace");
  const python = config.get<string>("pythonPath") || "python3";
  const engineDir = config.get<string>("enginePath") || getDefaultEnginePath(context);

  statusBar.text = "$(sync~spin) DebugTrace: analyzing…";
  outputChannel.appendLine(`\n[${new Date().toLocaleTimeString()}] Analyzing: ${doc.fileName}`);

  try {
    // Step 1: AST + compile analysis
    const analysis = await runPython<AnalysisResult>(
      python,
      path.join(engineDir, "analyze.py"),
      [doc.fileName]
    );

    if (analysis.error) {
      vscode.window.showErrorMessage(`DebugTrace: ${analysis.error}`);
      statusBar.text = "$(bug) DebugTrace";
      return;
    }

    // Push compiler error squiggles
    pushDiagnostics(doc, analysis.compiler_errors);

    // Step 2: AI diagnosis via Python (confirmed works on this machine)
    let diagnosis: DiagnosisResult | null = null;
    if (analysis.compiler_errors.length > 0 || analysis.error_context) {
      const apiKey = await context.secrets.get("debugtrace.apiKey");
      if (!apiKey) {
        vscode.window.showWarningMessage(
          "DebugTrace: No API key set. Run 'DebugTrace: Set Anthropic API Key' to enable AI diagnosis."
        );
      } else {
        diagnosis = await runPythonWithStdin<DiagnosisResult>(
          python,
          path.join(engineDir, "diagnose.py"),
          ["-"],
          JSON.stringify(analysis),
          { OPENROUTER_API_KEY: apiKey }
        );
      }
    }

    // Step 3: Show panel
    showPanel(analysis, diagnosis, context);

    const errCount = analysis.compiler_errors.length;
    statusBar.text = errCount > 0
      ? `$(error) DebugTrace: ${errCount} error${errCount > 1 ? "s" : ""}`
      : "$(pass) DebugTrace: clean";

  } catch (err: any) {
    outputChannel.appendLine(`ERROR: ${err.message}`);
    vscode.window.showErrorMessage(`DebugTrace: ${err.message}`);
    statusBar.text = "$(bug) DebugTrace";
  }
}

// ── Python runner helpers ────────────────────────────────────────────────────

function runPython<T>(python: string, script: string, args: string[]): Promise<T> {
  return new Promise((resolve, reject) => {
    const proc = cp.spawn(python, [script, ...args]);
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("close", (code) => {
      if (stdout) {
        try { resolve(JSON.parse(stdout)); }
        catch { reject(new Error(`JSON parse failed: ${stdout.slice(0, 200)}`)); }
      } else {
        reject(new Error(`Python exited ${code}: ${stderr.slice(0, 400)}`));
      }
    });
    proc.on("error", (e) => reject(new Error(`Could not start Python: ${e.message}`)));
  });
}

function runPythonWithStdin<T>(python: string, script: string, args: string[], stdin: string, extraEnv: Record<string, string> = {}): Promise<T> {
  return new Promise((resolve, reject) => {
    const proc = cp.spawn(python, [script, ...args], {
      env: { ...process.env, ...extraEnv }
    });
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("close", (code) => {
      if (stdout) {
        try { resolve(JSON.parse(stdout)); }
        catch { reject(new Error(`JSON parse failed: ${stdout.slice(0, 200)}`)); }
      } else {
        reject(new Error(`Python exited ${code}: ${stderr.slice(0, 400)}`));
      }
    });
    proc.on("error", (e) => reject(new Error(`Could not start Python: ${e.message}`)));
    proc.stdin.write(stdin);
    proc.stdin.end();
  });
}

// ── OpenRouter API (Node.js fetch) ───────────────────────────────────────────

function buildPrompt(analysis: AnalysisResult): string {
  const lines: string[] = [];
  lines.push(`You are a C++ debugging assistant. Analyze the following code and errors from \`${analysis.file}\`.\n`);

  if (analysis.compiler_errors.length > 0) {
    lines.push("## Compiler / Runtime Errors\n");
    for (const e of analysis.compiler_errors.slice(0, 10)) {
      lines.push(`- ${e.raw || e.message}`);
    }
    lines.push("");
  }

  const ctx = analysis.error_context;
  if (ctx) {
    lines.push(`## Function containing the error: \`${ctx.name}\` (lines ${ctx.start_line}–${ctx.end_line}, complexity=${ctx.complexity})\n`);
    lines.push("```cpp");
    lines.push(ctx.body);
    lines.push("```\n");
  }

  lines.push("## Task");
  lines.push("1. Explain what is wrong in plain English (2-3 sentences max).");
  lines.push("2. Show the corrected code snippet.");
  lines.push("3. In one sentence, explain why this fix works.");
  lines.push("\nBe concise. Format your response in Markdown.");
  return lines.join("\n");
}

async function callOpenRouter(analysis: AnalysisResult, apiKey: string): Promise<DiagnosisResult> {
  const prompt = buildPrompt(analysis);
  try {
    const resp = await fetch("https://openrouter.ai/api/v1/chat/completions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${apiKey}`,
        "HTTP-Referer": "https://github.com/jahnavi7806/debugtrace",
        "X-Title": "DebugTrace",
      },
      body: JSON.stringify({
        model: "meta-llama/llama-3.3-8b-instruct:free",
        messages: [{ role: "user", content: prompt }],
        max_tokens: 1000,
      }),
    });
    const data = await resp.json() as any;
    if (!resp.ok) {
      return { diagnosis: "", functions: analysis.functions, compiler_errors: analysis.compiler_errors, error: `API error ${resp.status}: ${JSON.stringify(data)}` };
    }
    const text = data.choices[0].message.content;
    return { diagnosis: text, functions: analysis.functions, compiler_errors: analysis.compiler_errors };
  } catch (e: any) {
    return { diagnosis: "", functions: analysis.functions, compiler_errors: analysis.compiler_errors, error: e.message };
  }
}

// ── Diagnostics ──────────────────────────────────────────────────────────────

function pushDiagnostics(doc: vscode.TextDocument, errors: CompilerError[]) {
  const diags: vscode.Diagnostic[] = [];
  for (const e of errors) {
    if (!e.line) { continue; }
    const line = Math.max(0, e.line - 1);
    const col = Math.max(0, (e.col ?? 1) - 1);
    const range = new vscode.Range(line, col, line, col + 1);
    const sev = e.severity?.includes("error")
      ? vscode.DiagnosticSeverity.Error
      : vscode.DiagnosticSeverity.Warning;
    const d = new vscode.Diagnostic(range, e.message || e.raw, sev);
    d.source = "DebugTrace";
    diags.push(d);
  }
  diagnosticCollection.set(doc.uri, diags);
}

// ── Webview panel ─────────────────────────────────────────────────────────────

function showPanel(
  analysis: AnalysisResult,
  diagnosis: DiagnosisResult | null,
  context: vscode.ExtensionContext
) {
  if (!panel) {
    panel = vscode.window.createWebviewPanel(
      "debugtrace",
      "DebugTrace",
      vscode.ViewColumn.Beside,
      { enableScripts: true }
    );
    panel.onDidDispose(() => { panel = undefined; });
  }

  panel.webview.html = buildHtml(analysis, diagnosis);
  panel.reveal(vscode.ViewColumn.Beside, true);
}

function buildHtml(analysis: AnalysisResult, diagnosis: DiagnosisResult | null): string {
  const errCount = analysis.compiler_errors.length;
  const fnRows = analysis.functions.map(f => {
    const color = f.complexity >= 10 ? "#ff5f5f" : f.complexity >= 5 ? "#ffaa00" : "#4ec9b0";
    return `<tr>
      <td>${esc(f.name)}</td>
      <td>${f.start_line}–${f.end_line}</td>
      <td style="color:${color};font-weight:600">${f.complexity}</td>
    </tr>`;
  }).join("");

  const errRows = analysis.compiler_errors.map(e =>
    `<tr><td>${e.line ?? "?"}</td><td class="err-msg">${esc(e.raw)}</td></tr>`
  ).join("");

  const diagHtml = diagnosis?.diagnosis
    ? `<section><h2>🤖 AI Diagnosis</h2><div class="diagnosis">${markdownToHtml(diagnosis.diagnosis)}</div></section>`
    : diagnosis?.error
    ? `<section><h2>🤖 AI Diagnosis</h2><p class="warn">API error: ${esc(diagnosis.error)}</p></section>`
    : `<section><h2>🤖 AI Diagnosis</h2><p class="muted">No errors detected — nothing to diagnose.</p></section>`;

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  :root { --bg:#1e1e1e; --panel:#252526; --border:#3c3c3c; --text:#d4d4d4; --accent:#4ec9b0; --err:#f44747; --warn:#ffaa00; }
  * { box-sizing: border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:'Segoe UI',sans-serif; font-size:13px; padding:16px; }
  h1 { font-size:16px; color:var(--accent); margin-bottom:16px; }
  h2 { font-size:13px; color:#9cdcfe; margin:16px 0 8px; text-transform:uppercase; letter-spacing:.05em; }
  section { background:var(--panel); border:1px solid var(--border); border-radius:4px; padding:12px; margin-bottom:12px; }
  table { width:100%; border-collapse:collapse; }
  th { text-align:left; color:#888; font-weight:400; padding:4px 8px; border-bottom:1px solid var(--border); }
  td { padding:4px 8px; border-bottom:1px solid #2a2a2a; }
  .err-msg { color:var(--err); font-family:monospace; font-size:11px; word-break:break-all; }
  .diagnosis { line-height:1.7; }
  .diagnosis pre { background:#1a1a1a; padding:10px; border-radius:3px; overflow-x:auto; margin:8px 0; }
  .diagnosis code { font-family:'Cascadia Code','Consolas',monospace; font-size:12px; }
  .diagnosis p { margin:6px 0; }
  .muted { color:#666; }
  .warn { color:var(--warn); }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; }
  .badge-err { background:#4f1e1e; color:var(--err); }
  .badge-ok { background:#1e3a2a; color:#4ec9b0; }
</style>
</head>
<body>
<h1>⚡ DebugTrace</h1>
<div style="margin-bottom:12px">
  <span class="badge ${errCount > 0 ? 'badge-err' : 'badge-ok'}">
    ${errCount > 0 ? `${errCount} error${errCount > 1 ? 's' : ''}` : '✓ Clean'}
  </span>
  &nbsp;
  <span style="color:#666">${esc(analysis.file.split(/[\\/]/).pop() ?? analysis.file)}</span>
</div>

${diagHtml}

<section>
  <h2>Functions</h2>
  ${analysis.functions.length === 0
    ? '<p class="muted">No functions found (tree-sitter not available or no functions defined)</p>'
    : `<table><thead><tr><th>Name</th><th>Lines</th><th>Complexity</th></tr></thead><tbody>${fnRows}</tbody></table>`
  }
</section>

${errCount > 0 ? `<section>
  <h2>Compiler Errors</h2>
  <table><thead><tr><th>Line</th><th>Message</th></tr></thead><tbody>${errRows}</tbody></table>
</section>` : ""}

</body>
</html>`;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function isCppFile(doc: vscode.TextDocument): boolean {
  return ["cpp", "c", "cc", "cxx", "h", "hpp"].includes(
    doc.fileName.split(".").pop()?.toLowerCase() ?? ""
  );
}

function getDefaultEnginePath(context: vscode.ExtensionContext): string {
  return path.join(context.extensionPath, "..", "engine");
}

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function markdownToHtml(md: string): string {
  // Minimal markdown → HTML (code blocks, bold, inline code)
  return md
    .replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) =>
      `<pre><code class="language-${lang}">${esc(code.trim())}</code></pre>`)
    .replace(/`([^`]+)`/g, (_, c) => `<code>${esc(c)}</code>`)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\n\n/g, "</p><p>")
    .replace(/^/, "<p>")
    .replace(/$/, "</p>");
}
