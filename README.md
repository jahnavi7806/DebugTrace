# DebugTrace

AI-powered debugging for VS Code that understands errors **across files**, not just the one line in the stack trace.

## The problem

The real cause of a bug is often in a different file from the one in the error message. Pasting a single error into a chatbot usually misses it, because the chatbot has no idea what the other files contain. Fixing that by hand means copying code between your editor and a chat window again and again.

## What DebugTrace does

1. Detects the error in your code
2. Follows it to the related files across your project
3. Sends the error plus that cross-file context to an LLM through OpenRouter
4. Shows the likely root cause and a suggested fix, right inside VS Code

It works with multiple languages, and it handles Windows file paths correctly when parsing stack traces.

## Install

DebugTrace is packaged as a `.vsix` file and is not on the VS Code Marketplace yet.

1. Go to the **Releases** page of this repository and download the latest `.vsix` file
2. In VS Code, open the **Extensions** panel
3. Click the **...** menu at the top of the panel and choose **Install from VSIX...**
4. Select the downloaded file and reload VS Code

## Setup

DebugTrace uses [OpenRouter](https://openrouter.ai) to call the language model, so you need your own API key.

1. Create a free account at openrouter.ai and generate an API key
2. Add the key to the DebugTrace setting in VS Code (Settings, search for "DebugTrace")

Your key stays on your machine and is only used to call OpenRouter.

## How to use

1. Open a project that has an error
2. Open the Command Palette (`Ctrl+Shift+P` / `Cmd+Shift+P`)
3. Run the DebugTrace command
4. Read the diagnosis and the suggested fix

## Project structure

| File | Role |
|---|---|
| `extension.ts` | Extension entry point and VS Code integration |
| `analyzer.ts` | Error detection and cross-file analysis |
| `diagnose.ts` | Builds the prompt and calls the OpenRouter API |
| `stats.ts` | Statistics helpers |

## Why OpenRouter

One API gives access to many models. DebugTrace is not tied to a single provider, and the model can be swapped without changing the code.

## Built with

TypeScript, VS Code Extension API, OpenRouter API

## Roadmap

- Publish to the VS Code Marketplace
- UI/UX improvements
- Better context selection for large projects

## Author

Jahnavi Boddupalli - [GitHub](https://github.com/jahnavi7806) - [LinkedIn](https://www.linkedin.com/in/jahnavi-boddupalli-858b6b32a)
