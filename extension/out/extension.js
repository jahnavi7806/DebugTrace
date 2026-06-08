"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = __importStar(require("vscode"));
const cp = __importStar(require("child_process"));
const path = __importStar(require("path"));
// ── Globals ──────────────────────────────────────────────────────────────────
let diagnosticCollection;
let outputChannel;
let panel;
let statusBar;
// ── Activate ─────────────────────────────────────────────────────────────────
function activate(context) {
    diagnosticCollection = vscode.languages.createDiagnosticCollection("debugtrace");
    outputChannel = vscode.window.createOutputChannel("DebugTrace");
    statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusBar.text = "$(bug) DebugTrace";
    statusBar.command = "debugtrace.analyze";
    statusBar.show();
    context.subscriptions.push(diagnosticCollection, outputChannel, statusBar, vscode.commands.registerCommand("debugtrace.analyze", () => {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            vscode.window.showErrorMessage("DebugTrace: No active editor.");
            return;
        }
        runAnalysis(editor.document, context);
    }), vscode.commands.registerCommand("debugtrace.setApiKey", async () => {
        const key = await vscode.window.showInputBox({
            prompt: "Enter your Gemini API Key (get free key at aistudio.google.com/apikey)",
            password: true,
            ignoreFocusOut: true,
        });
        if (key) {
            await context.secrets.store("debugtrace.apiKey", key);
            vscode.window.showInformationMessage("DebugTrace: Gemini API key saved.");
        }
    }), vscode.workspace.onDidSaveTextDocument((doc) => {
        const config = vscode.workspace.getConfiguration("debugtrace");
        if (config.get("autoAnalyzeOnSave") && isCppFile(doc)) {
            runAnalysis(doc, context);
        }
    }));
    outputChannel.appendLine("DebugTrace activated. Open a .cpp file and save, or run 'DebugTrace: Analyze Current File'.");
}
function deactivate() {
    diagnosticCollection?.dispose();
}
// ── Core pipeline ─────────────────────────────────────────────────────────────
async function runAnalysis(doc, context) {
    if (!isCppFile(doc)) {
        vscode.window.showWarningMessage("DebugTrace only supports C/C++ files.");
        return;
    }
    const config = vscode.workspace.getConfiguration("debugtrace");
    const python = config.get("pythonPath") || "python3";
    const engineDir = config.get("enginePath") || getDefaultEnginePath(context);
    statusBar.text = "$(sync~spin) DebugTrace: analyzing…";
    outputChannel.appendLine(`\n[${new Date().toLocaleTimeString()}] Analyzing: ${doc.fileName}`);
    try {
        // Step 1: AST + compile analysis
        const analysis = await runPython(python, path.join(engineDir, "analyze.py"), [doc.fileName]);
        if (analysis.error) {
            vscode.window.showErrorMessage(`DebugTrace: ${analysis.error}`);
            statusBar.text = "$(bug) DebugTrace";
            return;
        }
        // Push compiler error squiggles
        pushDiagnostics(doc, analysis.compiler_errors);
        // Step 2: AI diagnosis via Python (confirmed works on this machine)
        let diagnosis = null;
        if (analysis.compiler_errors.length > 0 || analysis.error_context) {
            const apiKey = await context.secrets.get("debugtrace.apiKey");
            if (!apiKey) {
                vscode.window.showWarningMessage("DebugTrace: No API key set. Run 'DebugTrace: Set Anthropic API Key' to enable AI diagnosis.");
            }
            else {
                diagnosis = await runPythonWithStdin(python, path.join(engineDir, "diagnose.py"), ["-"], JSON.stringify(analysis), { OPENROUTER_API_KEY: apiKey });
            }
        }
        // Step 3: Show panel
        showPanel(analysis, diagnosis, context);
        const errCount = analysis.compiler_errors.length;
        statusBar.text = errCount > 0
            ? `$(error) DebugTrace: ${errCount} error${errCount > 1 ? "s" : ""}`
            : "$(pass) DebugTrace: clean";
    }
    catch (err) {
        outputChannel.appendLine(`ERROR: ${err.message}`);
        vscode.window.showErrorMessage(`DebugTrace: ${err.message}`);
        statusBar.text = "$(bug) DebugTrace";
    }
}
// ── Python runner helpers ────────────────────────────────────────────────────
function runPython(python, script, args) {
    return new Promise((resolve, reject) => {
        const proc = cp.spawn(python, [script, ...args]);
        let stdout = "";
        let stderr = "";
        proc.stdout.on("data", (d) => (stdout += d));
        proc.stderr.on("data", (d) => (stderr += d));
        proc.on("close", (code) => {
            if (stdout) {
                try {
                    resolve(JSON.parse(stdout));
                }
                catch {
                    reject(new Error(`JSON parse failed: ${stdout.slice(0, 200)}`));
                }
            }
            else {
                reject(new Error(`Python exited ${code}: ${stderr.slice(0, 400)}`));
            }
        });
        proc.on("error", (e) => reject(new Error(`Could not start Python: ${e.message}`)));
    });
}
function runPythonWithStdin(python, script, args, stdin, extraEnv = {}) {
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
                try {
                    resolve(JSON.parse(stdout));
                }
                catch {
                    reject(new Error(`JSON parse failed: ${stdout.slice(0, 200)}`));
                }
            }
            else {
                reject(new Error(`Python exited ${code}: ${stderr.slice(0, 400)}`));
            }
        });
        proc.on("error", (e) => reject(new Error(`Could not start Python: ${e.message}`)));
        proc.stdin.write(stdin);
        proc.stdin.end();
    });
}
// ── OpenRouter API (Node.js fetch) ───────────────────────────────────────────
function buildPrompt(analysis) {
    const lines = [];
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
async function callOpenRouter(analysis, apiKey) {
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
        const data = await resp.json();
        if (!resp.ok) {
            return { diagnosis: "", functions: analysis.functions, compiler_errors: analysis.compiler_errors, error: `API error ${resp.status}: ${JSON.stringify(data)}` };
        }
        const text = data.choices[0].message.content;
        return { diagnosis: text, functions: analysis.functions, compiler_errors: analysis.compiler_errors };
    }
    catch (e) {
        return { diagnosis: "", functions: analysis.functions, compiler_errors: analysis.compiler_errors, error: e.message };
    }
}
// ── Diagnostics ──────────────────────────────────────────────────────────────
function pushDiagnostics(doc, errors) {
    const diags = [];
    for (const e of errors) {
        if (!e.line) {
            continue;
        }
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
function showPanel(analysis, diagnosis, context) {
    if (!panel) {
        panel = vscode.window.createWebviewPanel("debugtrace", "DebugTrace", vscode.ViewColumn.Beside, { enableScripts: true });
        panel.onDidDispose(() => { panel = undefined; });
    }
    panel.webview.html = buildHtml(analysis, diagnosis);
    panel.reveal(vscode.ViewColumn.Beside, true);
}
function buildHtml(analysis, diagnosis) {
    const errCount = analysis.compiler_errors.length;
    const lang = analysis.language || "unknown";
    const allFiles = analysis.all_files_checked || [analysis.file];
    const langIcon = { cpp: "⚙️", python: "🐍", javascript: "🟨", unknown: "📄" };
    const fnRows = analysis.functions.map(f => {
        const color = f.complexity >= 10 ? "#ff5f5f" : f.complexity >= 5 ? "#ffaa00" : "#4ec9b0";
        return `<tr>
      <td>${esc(f.name)}</td>
      <td>${f.start_line}–${f.end_line}</td>
      <td style="color:${color};font-weight:600">${f.complexity}</td>
    </tr>`;
    }).join("");
    const errRows = analysis.compiler_errors.map(e => {
        const fileName = e.file ? e.file.split(/[\\/]/).pop() : "";
        const cross = e.cross_file_source
            ? `<div class="cross-file">↳ Cross-file: ${esc(e.cross_file_source.reason)}</div>` : "";
        return `<tr>
      <td>${fileName ? `<span class="file-tag">${esc(fileName)}</span>` : ""}${e.line ?? "?"}</td>
      <td class="err-msg">${esc(e.message || e.raw)}${cross}</td>
    </tr>`;
    }).join("");
    const filesHtml = allFiles.length > 1
        ? `<section><h2>${langIcon[lang]} Files Scanned</h2><div style="display:flex;flex-wrap:wrap;gap:6px">${allFiles.map(f => `<span class="file-chip">${esc(f.split(/[\\/]/).pop() ?? f)}</span>`).join("")}</div></section>` : "";
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
  td { padding:4px 8px; border-bottom:1px solid #2a2a2a; vertical-align:top; }
  .err-msg { color:var(--err); font-family:monospace; font-size:11px; word-break:break-all; }
  .cross-file { color:var(--warn); font-size:10px; margin-top:3px; font-family:'Segoe UI',sans-serif; }
  .file-tag { background:#2a2a2a; color:#9cdcfe; border-radius:3px; padding:1px 4px; font-size:10px; margin-right:4px; }
  .file-chip { background:#2a3a4a; color:#9cdcfe; border-radius:10px; padding:2px 8px; font-size:11px; }
  .diagnosis { line-height:1.7; }
  .diagnosis pre { background:#1a1a1a; padding:10px; border-radius:3px; overflow-x:auto; margin:8px 0; }
  .diagnosis code { font-family:'Cascadia Code','Consolas',monospace; font-size:12px; }
  .diagnosis p { margin:6px 0; }
  .muted { color:#666; }
  .warn { color:var(--warn); }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; }
  .badge-err { background:#4f1e1e; color:var(--err); }
  .badge-ok { background:#1e3a2a; color:#4ec9b0; }
  .lang-badge { background:#2a2a2a; color:#888; padding:2px 8px; border-radius:10px; font-size:11px; }
</style>
</head>
<body>
<h1>⚡ DebugTrace</h1>
<div style="margin-bottom:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
  <span class="badge ${errCount > 0 ? 'badge-err' : 'badge-ok'}">
    ${errCount > 0 ? `${errCount} error${errCount > 1 ? 's' : ''}` : '✓ Clean'}
  </span>
  <span class="lang-badge">${langIcon[lang]} ${lang}</span>
  <span style="color:#666">${esc(analysis.file.split(/[\\/]/).pop() ?? analysis.file)}</span>
</div>

${filesHtml}
${diagHtml}

<section>
  <h2>Functions</h2>
  ${analysis.functions.length === 0
        ? '<p class="muted">No functions found</p>'
        : `<table><thead><tr><th>Name</th><th>Lines</th><th>Complexity</th></tr></thead><tbody>${fnRows}</tbody></table>`}
</section>

${errCount > 0 ? `<section>
  <h2>Errors</h2>
  <table><thead><tr><th>File / Line</th><th>Message</th></tr></thead><tbody>${errRows}</tbody></table>
</section>` : ""}

</body>
</html>`;
}
// ── Helpers ──────────────────────────────────────────────────────────────────
function isCppFile(doc) {
    return ["cpp", "c", "cc", "cxx", "h", "hpp", "py", "js", "ts", "jsx", "tsx", "mjs"].includes(doc.fileName.split(".").pop()?.toLowerCase() ?? "");
}
function getDefaultEnginePath(context) {
    return path.join(context.extensionPath, "..", "engine");
}
function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function markdownToHtml(md) {
    // Minimal markdown → HTML (code blocks, bold, inline code)
    return md
        .replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => `<pre><code class="language-${lang}">${esc(code.trim())}</code></pre>`)
        .replace(/`([^`]+)`/g, (_, c) => `<code>${esc(c)}</code>`)
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/\n\n/g, "</p><p>")
        .replace(/^/, "<p>")
        .replace(/$/, "</p>");
}
//# sourceMappingURL=extension.js.map