# Cura MCP

A **minimal, localhost-only** MCP server that exposes a narrow, safe set of
Ultimaker Cura controls, designed to be fronted by the
[`mcp-proxy`](https://github.com/gaetan-deturche/mcp-proxy) aggregator as an
`http` downstream.

Machine-agnostic: it drives whatever machine is active in Cura. G-code export
goes through Cura's normal write path, so any post-processing scripts configured
for the active machine are applied. Developed/tested on Windows 11 with Cura 5.13.

---

## Architecture

```
Claude Desktop / Claude Code
        │  connects once (HTTP)  →  http://127.0.0.1:6390/mcp
        ▼
   mcp-proxy  (aggregator, %LOCALAPPDATA%\mcp-proxy)
        │  downstream "cura" (HTTP)   →  http://127.0.0.1:8974/mcp
        ▼
   Cura plugin  "CuraMCP"  (UM.Extension, runs INSIDE Cura)
        └─ stdlib http.server, JSON-RPC 2.0, live access to CuraApplication
```

Why this shape:

- **The plugin lives inside Cura** so it reads/writes the live GUI settings,
  loads models, and drives the real slicing backend (`getBackend().forceSlice()`).
- Cura 5.13 ships a **frozen** Python 3.12 (no `pip`), so the plugin uses the
  **standard library only** — no `mcp` SDK inside Cura.
- The **proxy** is the single stable endpoint the client talks to; it fronts the
  Cura downstream (and any others you add) and survives client/session restarts.
  The plugin only has to be a simple JSON-RPC-over-HTTP downstream (it replies with
  plain `application/json`; the proxy accepts JSON or SSE, so no SSE is needed).

Tools appear in the client as `mcp__mcp-proxy__cura__<tool>`.

---

## Security model (the point of this project)

- **No arbitrary code / command execution.** The plugin exposes a fixed set of 8
  tools and nothing else. There is no Python `exec`, no shell, no generic file
  read/write.
- **Localhost only.** The HTTP server binds strictly to `127.0.0.1`. It also
  refuses cross-origin browser requests (DNS-rebinding guard) and supports an
  optional shared bearer token.
- **No outbound connections** except the one explicit, opt-in OctoPrint upload.
  No telemetry, no auto-update.
- **Side effects require confirmation.** `export_gcode` and `send_to_octoprint`
  refuse to run unless called with `confirm=true`, and the MCP client prompts for
  tool approval on top of that.
- File access is limited to the exact paths a tool is given (a model to load, a
  `.gcode` to write), with extension checks.

---

## Tools

| Tool | Effect | Side effect |
|---|---|---|
| `list_printers` | Machines + build volume, nozzle, active flag | none |
| `get_settings` | Read active settings (temp, speed, cooling, adhesion, layers…) | none |
| `set_setting` | Change one setting on the active stack (per-extruder → T0) | none |
| `load_model` | Load a model onto the plate (`.stl`, `.obj`, `.3mf`, …) | none |
| `slice` | Slice; returns estimated time + material | none |
| `export_gcode` | Write `.gcode` (active machine's post-processing applied) | writes a file — `confirm=true` |
| `send_to_octoprint` | Upload g-code to OctoPrint | network upload — `confirm=true` |
| `get_plate_view` | PNG snapshot of the build-plate layout + per-object size/position/fit | none |

---

## Install

### 1. Plugin (inside Cura)

Copy the plugin folder to Cura's plugin directory:

```
copy  cura-mcp\plugin\CuraMCP  →  %APPDATA%\cura\5.13\plugins\CuraMCP
```

Copy `config.example.json` to `config.json` in that folder and adjust if needed:

```json
{
  "host": "127.0.0.1",
  "port": 8974,
  "token": "",
  "autostart": true,
  "octoprint_url": "",
  "octoprint_api_key": ""
}
```

Restart Cura. A green notification **“Cura MCP server started at
http://127.0.0.1:8974/mcp”** confirms it. Menu **Extensions → Cura MCP** lets you
Start / Stop / show status.

> To use `send_to_octoprint`, fill `octoprint_url` (e.g. `http://octopi.local`)
> and `octoprint_api_key`. Left blank, that one tool simply refuses to run.

### 2. Proxy

The aggregator proxy is a separate project — get it from its repository:
**https://github.com/gaetan-deturche/mcp-proxy** (build from source with Go, or
download `mcp-proxy.exe` from the [releases page](https://github.com/gaetan-deturche/mcp-proxy/releases)).

Put `mcp-proxy.exe` in a stable folder (e.g. `%LOCALAPPDATA%\mcp-proxy`) with a
`downstreams.json` next to it:

```json
{
  "callTimeoutSeconds": 300,
  "downstreams": [
    { "name": "cura", "transport": "http", "url": "http://127.0.0.1:8974/mcp" }
  ]
}
```

Run it once as a persistent HTTP listener:

```
mcp-proxy.exe -http 127.0.0.1:6390
```

**Autostart at logon:** run `register-startup-task.bat` **from an interactive
shell** (it registers a per-user ONLOGON task; this may be blocked by EDR when run
non-interactively). Alternatively drop a *shortcut* to `start-proxy-hidden.vbs`
into the Startup folder.

### 3. Client (Claude Desktop)

`%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mcp-proxy": { "type": "http", "url": "http://127.0.0.1:6390/mcp" }
  }
}
```

Restart Claude Desktop. Cura tools show up as `mcp__mcp-proxy__cura__*`. After
launching Cura, call the proxy's `reload` tool to hot-attach its tools without
restarting the client.

> The proxy can front several downstreams at once — add more entries to
> `downstreams.json` and call `reload`. Each server's tools are namespaced under
> its own `<name>__` prefix.

---

## Test procedure

With Cura running and the plugin loaded, hit the plugin directly (bypasses the
proxy, isolates the plugin) or go through the proxy with the `cura__` prefix:

1. `load_model` → `C:\Users\...\model.stl`
2. `get_settings` → check `material_print_temperature`, `material_bed_temperature`
3. `set_setting` → `material_bed_temperature = 65`
4. `slice` → returns `print_time` + `material_weight_g`
5. (optional) `export_gcode` with `confirm=true`

---

## Files

```
cura-mcp/
  plugin/CuraMCP/
    plugin.json          plugin manifest (api 8)
    __init__.py          register() → Extension
    CuraMCP.py           Extension: lifecycle, config, menu
    mcp_http.py          stdlib MCP-over-HTTP (JSON-RPC 2.0)
    cura_tools.py        the 8 tools + main-thread marshalling + tool schemas
    config.example.json  copy to config.json
  README.md
  downstreams.snippet.json
```
