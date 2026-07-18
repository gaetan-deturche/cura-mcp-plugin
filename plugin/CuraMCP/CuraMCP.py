# Cura MCP — UM.Extension entry point.
#
# Starts a localhost-only MCP-over-HTTP server inside Cura and exposes a narrow,
# fixed set of tools. Designed to be fronted by the mcp-proxy aggregator as an
# 'http' downstream. No arbitrary code execution; no outbound connections except
# the explicit, confirm-gated OctoPrint upload.

import importlib
import json
import os

from UM.Extension import Extension
from UM.Logger import Logger
from UM.Message import Message

from . import cura_tools               # module ref so it can be importlib.reload()'d
from .cura_tools import MainThreadInvoker
from .mcp_http import MCPServer

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8974,
    "token": "",
    "autostart": True,
    "octoprint_url": "",
    "octoprint_api_key": "",
}


class CuraMCP(Extension):
    def __init__(self):
        super().__init__()
        self.setMenuName("Cura MCP")

        self._plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self._config = self._load_config()
        self._invoker = MainThreadInvoker()          # created on the Qt main thread
        self._tools = cura_tools.CuraTools(self._invoker, self._config)
        self._server = None

        self.addMenuItem("Start server", self.start)
        self.addMenuItem("Stop server", self.stop)
        self.addMenuItem("Show status / URL", self.show_status)
        self.addMenuItem("Reload tools (no restart)", self.reload_tools)

        if self._config.get("autostart", True):
            self.start()

    # -- config ---------------------------------------------------------- #

    def _load_config(self):
        cfg = dict(DEFAULT_CONFIG)
        path = os.path.join(self._plugin_dir, "config.json")
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    cfg.update(json.load(fh))
            except Exception as exc:  # noqa: BLE001
                Logger.log("w", "Cura MCP: could not read config.json (%s); using defaults", exc)
        return cfg

    # -- lifecycle ------------------------------------------------------- #

    def start(self):
        if self._server is not None:
            self._notify("Cura MCP already running at %s" % self._server.url)
            return
        try:
            self._server = MCPServer(
                host=self._config["host"],
                port=int(self._config["port"]),
                tool_defs=cura_tools.TOOL_DEFS,
                dispatch=self._tools.dispatch,
                token=self._config.get("token") or None,
                reload_cb=self.reload_tools,
            )
            self._server.start()
            self._notify("Cura MCP server started at %s" % self._server.url)
        except Exception as exc:  # noqa: BLE001
            self._server = None
            Logger.logException("e", "Cura MCP: failed to start server")
            self._notify("Cura MCP failed to start: %s" % exc, error=True)

    def stop(self):
        if self._server is None:
            self._notify("Cura MCP is not running")
            return
        self._server.stop()
        self._server = None
        self._notify("Cura MCP server stopped")

    def reload_tools(self):
        """Hot-reload cura_tools.py and swap the live tool table + dispatch, without
        restarting Cura. Only reloads the tool code — changes to CuraMCP.py or
        mcp_http.py still need a Cura restart. Reloads config.json too."""
        try:
            self._config = self._load_config()
            importlib.reload(cura_tools)
            old = self._tools
            new_tools = cura_tools.CuraTools(self._invoker, self._config)
            if old is not None:
                old.teardown()
            self._tools = new_tools
            if self._server is not None:
                self._server.update(tool_defs=cura_tools.TOOL_DEFS, dispatch=new_tools.dispatch)
            msg = "Reloaded %d tools without restart" % len(cura_tools.TOOL_DEFS)
            self._notify("Cura MCP: %s" % msg)
            return msg
        except Exception as exc:  # noqa: BLE001
            Logger.logException("e", "Cura MCP: reload_tools failed")
            self._notify("Cura MCP reload failed: %s" % exc, error=True)
            return "reload failed: %s" % exc

    def show_status(self):
        if self._server is None:
            self._notify("Cura MCP: stopped. Configured port: %s" % self._config.get("port"))
        else:
            tok = "yes" if self._config.get("token") else "no"
            self._notify("Cura MCP running at %s (token: %s)" % (self._server.url, tok))

    # -- ui -------------------------------------------------------------- #

    @staticmethod
    def _notify(text, error=False):
        # Auto-dismissing toast: an absolute lifetime (use_inactivity_timer=False so
        # the countdown isn't reset by ongoing activity) means no manual dismiss.
        Logger.log("e" if error else "i", "Cura MCP: %s", text)
        lifetime = 12 if error else 6
        try:
            Message(text, title="Cura MCP", lifetime=lifetime, use_inactivity_timer=False,
                    message_type=Message.MessageType.ERROR if error else Message.MessageType.POSITIVE).show()
        except Exception:
            try:
                Message(text, title="Cura MCP", lifetime=lifetime, use_inactivity_timer=False).show()
            except Exception:
                pass
