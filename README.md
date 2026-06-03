# DebugTrace

AI-powered C++ static analysis + runtime error diagnosis inside VS Code.

## What it does

- **On every save** of a `.cpp`/`.cc`/`.h` file:
  - Runs `g++`/`clang++` and captures errors → shows red squiggles inline
  - Parses the AST with `tree-sitter-cpp` → cyclomatic complexity per function
  - Sends error + function context to Claude API → plain-English diagnosis + fix
  - Opens a side panel with everything

## Setup (5 minutes)

### 1. Install Python engine dependencies

```bash
cd engine/
pip install -r requirements.txt
```

### 2. Make sure you have g++ or clang++

```bash
g++ --version   # or: clang++ --version
```

### 3. Install the VS Code extension

```bash
cd extension/
npm install
npm run compile
```

Then in VS Code: `Ctrl+Shift+P` → **Extensions: Install from VSIX** — or just press **F5** inside the `extension/` folder to launch a dev host.

### 4. Set your Anthropic API key

`Ctrl+Shift+P` → **DebugTrace: Set Anthropic API Key**

### 5. Configure engine path (if needed)

In VS Code settings (`settings.json`):

```json
{
  "debugtrace.enginePath": "/absolute/path/to/debugtrace/engine",
  "debugtrace.pythonPath": "python3"
}
```

## Usage

- **Auto:** Just save any `.cpp` file — analysis runs automatically
- **Manual:** `Ctrl+Shift+P` → **DebugTrace: Analyze Current File**

## Project structure

```
debugtrace/
├── engine/
│   ├── analyze.py      ← tree-sitter AST parser + g++ error capture
│   ├── diagnose.py     ← Claude API call for diagnosis
│   └── requirements.txt
└── extension/
    ├── src/extension.ts  ← VS Code extension (shells out to Python)
    ├── package.json
    └── tsconfig.json
```

## Complexity legend

| Score | Meaning |
|-------|---------|
| 1–4   | ✅ Simple |
| 5–9   | ⚠️ Moderate — consider refactoring |
| 10+   | 🔴 High — hard to test and maintain |
