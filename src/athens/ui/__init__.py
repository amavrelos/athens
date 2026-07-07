"""The desktop shell: a pywebview window over the local WebSocket API.

One process: BridgeService (device + DAW) + WebSocket JSON-RPC + a native
system-webview window rendering `web/`. No Electron, no bundled browser, no
Node — the frontend is zero-build vanilla HTML/CSS/JS on the design tokens.
"""
