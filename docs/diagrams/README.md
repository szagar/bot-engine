# Diagrams

Excalidraw sources for the integration docs. Open any `.excalidraw` file at
[excalidraw.com](https://excalidraw.com) (File → Open), or directly in your
editor with the Excalidraw VS Code / JetBrains extension. Edit freely and
re-export PNG/SVG from there.

| File | Shows |
|---|---|
| `architecture.excalidraw` | The host/engine boundary: your adapters and port implementations (left), the context + your bots (middle), engine components (right), and every arrow that crosses the boundary. |
| `run-lifecycle.excalidraw` | One `BotExecutor.run()` fire, top to bottom: calendar gate → run-id → recorder open → class load → enable gate → `execute()` → the three outcomes (result / skipped / error). |
| `signals-and-accounts.excalidraw` | Signal-driven fires (consumer loop → `match_triggers` → `trigger_now`) and the one-scheduler / N-account wiring with namespaced job ids. |
