# Cura MCP — tool implementations.
#
# Every method that touches Cura state runs on the Qt main thread via
# MainThreadInvoker: the HTTP server handles requests on worker threads, but
# CuraApplication / container stacks / the backend are not thread-safe, so all
# access is marshalled onto the main thread and the worker blocks for the result.
#
# The scope is deliberately narrow: read machines, read/write known settings,
# load a model, slice, export g-code, upload to OctoPrint. No arbitrary code,
# no filesystem access beyond the paths a tool is explicitly given.

import json
import os
import threading
import time
import urllib.request
import uuid

from PyQt6.QtCore import QObject, pyqtSignal, Qt, QUrl, QBuffer, QByteArray, QIODevice

from UM.Logger import Logger

from cura.CuraApplication import CuraApplication


# --------------------------------------------------------------------------- #
# Main-thread marshalling
# --------------------------------------------------------------------------- #

class MainThreadInvoker(QObject):
    """Run a callable on the Qt main thread and return its result synchronously.

    Must be constructed on the main thread (the plugin is registered there) so
    the queued-connection slot executes on the main thread.
    """

    _submit = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self._submit.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def _run(self, job):
        fn, box, ev = job
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - propagated to caller
            box["error"] = exc
        finally:
            ev.set()

    def call(self, fn, timeout=60.0):
        box = {}
        ev = threading.Event()
        self._submit.emit((fn, box, ev))
        if not ev.wait(timeout):
            raise TimeoutError("Cura main-thread call timed out after %ss" % timeout)
        if "error" in box:
            raise box["error"]
        return box.get("result")


# --------------------------------------------------------------------------- #
# Curated settings surface for get_settings (read-only convenience list).
# set_setting accepts any valid definition key, not just these.
# --------------------------------------------------------------------------- #

CURATED_KEYS = [
    "layer_height", "layer_height_0", "line_width",
    "wall_thickness", "wall_line_count", "top_bottom_thickness",
    "infill_sparse_density", "infill_pattern",
    "material_print_temperature", "material_print_temperature_layer_0",
    "material_bed_temperature", "material_bed_temperature_layer_0",
    "material_flow", "retraction_enable", "retraction_distance",
    "speed_print", "speed_travel", "speed_layer_0", "speed_wall",
    "cool_fan_enabled", "cool_fan_speed", "cool_fan_speed_0",
    "adhesion_type", "brim_width", "skirt_line_count",
    "support_enable", "support_structure",
]


def _fmt_seconds(secs):
    secs = int(secs or 0)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s)


class CuraTools:
    def __init__(self, invoker, config):
        self.invoker = invoker
        self.cfg = config or {}
        self._hooks_ready = False
        self._slice_done = threading.Event()
        self._slice_error = [None]
        self._last_times = {}
        self._last_material = []

    def teardown(self):
        """Disconnect backend signal hooks so this instance can be discarded on a
        hot-reload without leaving duplicate slice callbacks connected."""
        if not self._hooks_ready:
            return

        def undo():
            backend = CuraApplication.getInstance().getBackend()
            for sig, handler in ((backend.printDurationMessage, self._on_duration),
                                 (backend.backendStateChange, self._on_state)):
                try:
                    sig.disconnect(handler)
                except Exception:
                    pass
        try:
            self.invoker.call(undo)
        except Exception:
            pass

    # -- dispatch -------------------------------------------------------- #

    def dispatch(self, name, args):
        args = args or {}
        handler = {
            "list_printers": lambda: self.list_printers(),
            "get_settings": lambda: self.get_settings(args.get("keys")),
            "set_setting": lambda: self.set_setting(args["key"], args["value"]),
            "load_model": lambda: self.load_model(args["path"]),
            "slice": lambda: self.slice_scene(),
            "export_gcode": lambda: self.export_gcode(args["path"], args.get("confirm", False)),
            "send_to_octoprint": lambda: self.send_to_octoprint(
                args.get("path"), args.get("confirm", False), args.get("start_print", False)),
            "get_plate_view": lambda: self.get_plate_view(
                args.get("width", 400), args.get("height", 400), args.get("focus", True)),
            "rotate_model": lambda: self.rotate_model(
                args.get("deg_x", 0), args.get("deg_y", 0), args.get("deg_z", 0),
                args.get("name"), args.get("absolute", False)),
            "reset_orientation": lambda: self.reset_orientation(args.get("name")),
            "lay_flat": lambda: self.lay_flat(args.get("name")),
            "arrange_all": lambda: self.arrange_all(),
            "set_camera": lambda: self.set_camera(args.get("view", "iso"), args.get("zoom", 1.0)),
        }.get(name)
        if handler is None:
            raise ValueError("Unknown tool: %s" % name)
        return handler()

    # -- helpers (run on main thread) ------------------------------------ #

    def _stacks(self):
        app = CuraApplication.getInstance()
        gs = app.getGlobalContainerStack()
        if gs is None:
            raise RuntimeError("No active machine in Cura")
        ex = gs.extruderList[0] if gs.extruderList else None
        return app, gs, ex

    def _get_value_mt(self, key):
        _, gs, ex = self._stacks()
        per_ext = gs.getProperty(key, "settable_per_extruder")
        src = ex if (per_ext and ex is not None) else gs
        return src.getProperty(key, "value")

    # -- 1. list_printers ------------------------------------------------ #

    def list_printers(self):
        def work():
            app = CuraApplication.getInstance()
            reg = app.getContainerRegistry()
            active = app.getGlobalContainerStack()
            active_id = active.getId() if active else None
            printers = []
            for m in reg.findContainerStacks(type="machine"):
                try:
                    nozzle = None
                    try:
                        nozzle = m.extruderList[0].getProperty("machine_nozzle_size", "value")
                    except Exception:
                        pass
                    printers.append({
                        "id": m.getId(),
                        "name": m.getName(),
                        "definition": m.definition.getId() if m.definition else None,
                        "active": m.getId() == active_id,
                        "volume_mm": {
                            "x": m.getProperty("machine_width", "value"),
                            "y": m.getProperty("machine_depth", "value"),
                            "z": m.getProperty("machine_height", "value"),
                        },
                        "nozzle_mm": nozzle,
                        "extruder_count": len(m.extruderList),
                    })
                except Exception as exc:  # noqa: BLE001
                    printers.append({"id": m.getId(), "error": str(exc)})
            return {"active_machine": active_id, "printers": printers}
        return self.invoker.call(work)

    # -- 2. get_settings ------------------------------------------------- #

    def get_settings(self, keys=None):
        keys = keys or CURATED_KEYS

        def work():
            _, gs, _ = self._stacks()
            out = {}
            for key in keys:
                stype = gs.getProperty(key, "type")
                if stype is None:
                    out[key] = {"error": "unknown setting"}
                    continue
                entry = {
                    "value": self._get_value_mt(key),
                    "type": stype,
                    "unit": gs.getProperty(key, "unit"),
                    "per_extruder": bool(gs.getProperty(key, "settable_per_extruder")),
                }
                if stype == "enum":
                    opts = gs.getProperty(key, "options") or {}
                    entry["options"] = list(opts.keys())
                out[key] = entry
            return out
        return self.invoker.call(work)

    # -- 3. set_setting -------------------------------------------------- #

    def set_setting(self, key, value):
        def work():
            _, gs, ex = self._stacks()
            stype = gs.getProperty(key, "type")
            if stype is None:
                raise ValueError("Unknown setting '%s'" % key)
            coerced = self._coerce(value, stype, key, gs)
            old = self._get_value_mt(key)
            per_ext = gs.getProperty(key, "settable_per_extruder")
            if per_ext and ex is not None:
                ex.setProperty(key, "value", coerced)  # writes to the T0 user container
                target = "extruder0"
            else:
                gs.setProperty(key, "value", coerced)
                target = "global"
            return {
                "key": key, "old": old, "new": self._get_value_mt(key),
                "type": stype, "target": target,
            }
        return self.invoker.call(work)

    @staticmethod
    def _coerce(value, stype, key, gs):
        if stype == "float":
            return float(value)
        if stype == "int":
            return int(float(value))
        if stype == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        if stype == "enum":
            opts = gs.getProperty(key, "options") or {}
            if value not in opts:
                raise ValueError("Invalid value %r for %s; options: %s" % (value, key, list(opts.keys())))
            return value
        return str(value)

    # -- 4. load_model --------------------------------------------------- #

    ALLOWED_MODEL_EXT = {".stl", ".obj", ".3mf", ".ply", ".amf", ".x3d", ".gltf", ".glb"}

    def load_model(self, path):
        if not path or not os.path.isfile(path):
            raise FileNotFoundError("Model file not found: %s" % path)
        ext = os.path.splitext(path)[1].lower()
        if ext not in self.ALLOWED_MODEL_EXT:
            raise ValueError("Unsupported model type '%s'. Allowed: %s"
                             % (ext, ", ".join(sorted(self.ALLOWED_MODEL_EXT))))

        before = self.invoker.call(self._count_sliceable)
        self.invoker.call(lambda: CuraApplication.getInstance().readLocalFile(
            QUrl.fromLocalFile(path), add_to_recent_files=False))

        deadline = time.time() + 30.0
        count = before
        while time.time() < deadline:
            time.sleep(0.25)
            count = self.invoker.call(self._count_sliceable)
            if count > before:
                break
        return {"path": path, "loaded": count > before,
                "sliceable_objects_on_plate": count}

    @staticmethod
    def _count_sliceable():
        from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
        scene = CuraApplication.getInstance().getController().getScene()
        n = 0
        for node in DepthFirstIterator(scene.getRoot()):
            if node.callDecoration("isSliceable"):
                n += 1
        return n

    # -- 5. slice -------------------------------------------------------- #

    def _ensure_hooks(self):
        if self._hooks_ready:
            return

        def setup():
            backend = CuraApplication.getInstance().getBackend()
            backend.printDurationMessage.connect(self._on_duration)
            backend.backendStateChange.connect(self._on_state)
            return True
        self.invoker.call(setup)
        self._hooks_ready = True

    def _on_duration(self, build_plate, times, material_amounts):
        try:
            self._last_times = dict(times) if times else {}
            self._last_material = list(material_amounts) if material_amounts else []
        except Exception:
            self._last_times = {}
        self._slice_done.set()

    def _on_state(self, state):
        try:
            s = int(state)
        except Exception:
            return
        if s == 4:  # BackendState.Error
            self._slice_error[0] = "backend reported an error"
            self._slice_done.set()
        elif s == 3:  # BackendState.Done
            self._slice_done.set()

    def slice_scene(self):
        self._ensure_hooks()
        if self.invoker.call(self._count_sliceable) == 0:
            raise RuntimeError("No sliceable model on the build plate. Load a model first.")
        self._slice_done.clear()
        self._slice_error[0] = None
        self._last_times = {}
        self.invoker.call(lambda: CuraApplication.getInstance().getBackend().forceSlice())
        if not self._slice_done.wait(300.0):
            raise TimeoutError("Slicing timed out after 300s")
        if self._slice_error[0]:
            raise RuntimeError("Slicing failed: %s" % self._slice_error[0])
        return self.invoker.call(self._read_estimates)

    def _read_estimates(self):
        app = CuraApplication.getInstance()
        secs = int(sum(self._last_times.values())) if self._last_times else 0
        grams = length_m = None
        pi = app.getPrintInformation()
        if pi is not None:
            try:
                grams = round(sum(pi.materialWeights), 2)
            except Exception:
                pass
            try:
                length_m = round(sum(pi.materialLengths), 3)
            except Exception:
                pass
        return {
            "print_time": _fmt_seconds(secs),
            "print_time_seconds": secs,
            "material_weight_g": grams,
            "material_length_m": length_m,
            "per_feature_seconds": self._last_times,
        }

    # -- 6. export_gcode ------------------------------------------------- #

    def export_gcode(self, path, confirm=False):
        if not confirm:
            raise PermissionError("export_gcode requires confirm=true (writes a file to disk)")
        if os.path.splitext(path)[1].lower() != ".gcode":
            raise ValueError("Export path must end with .gcode")
        data = self.invoker.call(self._render_gcode)
        parent = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(parent):
            raise NotADirectoryError("Destination folder does not exist: %s" % parent)
        with open(path, "w", newline="") as fh:
            fh.write(data)
        return {
            "path": path,
            "bytes": len(data),
            "post_processed": ";POSTPROCESSED" in data,
        }

    def _render_gcode(self):
        """Main thread: apply the active machine's post-processing scripts then
        render the active build plate's g-code to a string."""
        from io import StringIO
        from UM.PluginRegistry import PluginRegistry
        app = CuraApplication.getInstance()
        scene = app.getController().getScene()
        gdict = getattr(scene, "gcode_dict", None)
        if not gdict:
            raise RuntimeError("No sliced g-code available. Run 'slice' first.")
        # writeStarted triggers PostProcessingPlugin.execute (idempotent: it tags
        # the g-code with ;POSTPROCESSED and won't double-apply).
        odm = app.getOutputDeviceManager()
        device = None
        try:
            device = odm.getOutputDevice("local_file")
        except Exception:
            pass
        odm.writeStarted.emit(device)
        writer = PluginRegistry.getInstance().getPluginObject("GCodeWriter")
        stream = StringIO()
        if not writer.write(stream, None):
            raise RuntimeError("GCodeWriter failed to render g-code")
        data = stream.getvalue()
        if not data:
            raise RuntimeError("GCodeWriter produced empty output")
        return data

    # -- 7. send_to_octoprint -------------------------------------------- #

    def send_to_octoprint(self, path=None, confirm=False, start_print=False):
        if not confirm:
            raise PermissionError("send_to_octoprint requires confirm=true (uploads to the printer)")
        base = (self.cfg.get("octoprint_url") or "").rstrip("/")
        key = self.cfg.get("octoprint_api_key") or ""
        if not base or not key:
            raise RuntimeError(
                "OctoPrint not configured. Set 'octoprint_url' and 'octoprint_api_key' "
                "in the plugin config.json.")

        if path:
            if os.path.splitext(path)[1].lower() != ".gcode":
                raise ValueError("path must point to a .gcode file")
            with open(path, "r") as fh:
                data = fh.read()
            filename = os.path.basename(path)
        else:
            data = self.invoker.call(self._render_gcode)
            filename = "cura_mcp_%s.gcode" % uuid.uuid4().hex[:8]

        body, content_type = self._multipart(filename, data.encode("utf-8"), start_print)
        req = urllib.request.Request(
            base + "/api/files/local", data=body, method="POST",
            headers={"X-Api-Key": key, "Content-Type": content_type})
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - user-configured host
            status = resp.status
            resp.read()
        return {"uploaded": True, "http_status": status,
                "filename": filename, "print_started": bool(start_print)}

    @staticmethod
    def _multipart(filename, file_bytes, start_print):
        boundary = "----CuraMCP%s" % uuid.uuid4().hex
        crlf = b"\r\n"
        parts = []
        parts.append(("--" + boundary).encode())
        parts.append(('Content-Disposition: form-data; name="file"; filename="%s"'
                      % filename).encode())
        parts.append(b"Content-Type: application/octet-stream")
        parts.append(b"")
        parts.append(file_bytes)
        for name, val in (("select", "true" if start_print else "false"),
                          ("print", "true" if start_print else "false")):
            parts.append(("--" + boundary).encode())
            parts.append(('Content-Disposition: form-data; name="%s"' % name).encode())
            parts.append(b"")
            parts.append(val.encode())
        parts.append(("--" + boundary + "--").encode())
        parts.append(b"")
        return crlf.join(parts), "multipart/form-data; boundary=%s" % boundary

    # -- 8. get_plate_view ----------------------------------------------- #

    def get_plate_view(self, width=400, height=400, focus=True):
        width = max(64, min(int(width), 1024))
        height = max(64, min(int(height), 1024))
        # The OpenGL snapshot only renders when Cura's window is actually drawing
        # frames. If focus=True, bring the window forward and let the render thread
        # produce a frame (worker-thread sleep keeps the main event loop running),
        # then snapshot — retrying once if the first render came back empty.
        attempts = 3 if focus else 1
        result = None
        for i in range(attempts):
            if focus:
                self.invoker.call(self._focus_window)
                time.sleep(0.5 + 0.4 * i)  # progressive settle for a cold (deep-background) window
            result = self.invoker.call(lambda: self._render_plate_view(width, height))
            if any(b.get("type") == "image" for b in result.get("__mcp_content__", [])):
                break
        return result

    def _focus_window(self):
        # Best-effort nudge so the snapshot is more likely to render: raise the window
        # and mark the scene dirty. NOTE: Cura throttles its render loop while its
        # window is not the active/foreground one, so a clean snapshot is only
        # guaranteed when Cura is focused. We do NOT steal keyboard focus (Windows
        # blocks that from a background app anyway) and don't fight it further.
        win = CuraApplication.getInstance().getMainWindow()
        if win is None:
            return False
        try:
            win.raise_()   # bring forward WITHOUT changing maximised/minimised state
        except Exception:
            pass
        try:
            win._onSceneChanged()   # -> _full_render_required = True; self.update()
        except Exception:
            try:
                win.update()
            except Exception:
                pass
        return True

    def _render_plate_view(self, width, height):
        """Main thread: render a framed snapshot of the build plate to PNG and
        summarise object placement. Returns MCP content blocks (image + text).

        Coordinate note (Uranium world space): X = plate left/right,
        Z = plate front/back, Y = height above the plate.
        """
        from cura.Snapshot import Snapshot
        from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator

        app = CuraApplication.getInstance()
        gs = app.getGlobalContainerStack()
        plate_w = float(gs.getProperty("machine_width", "value")) if gs else None
        plate_d = float(gs.getProperty("machine_depth", "value")) if gs else None

        objects = []
        scene = app.getController().getScene()
        for node in DepthFirstIterator(scene.getRoot()):
            if not node.callDecoration("isSliceable"):
                continue
            item = {"name": node.getName() or "model"}
            bb = node.getBoundingBox()
            if bb is not None:
                item["size_mm"] = {"x": round(bb.width, 1), "y": round(bb.depth, 1),
                                   "z": round(bb.height, 1)}
                item["center_on_plate_mm"] = {"x": round(bb.center.x, 1),
                                              "y": round(bb.center.z, 1)}
                item["bottom_z_mm"] = round(bb.bottom, 1)
                if plate_w and plate_d:
                    m = 0.01
                    item["within_plate"] = bool(
                        bb.minimum.x >= -plate_w / 2 - m and bb.maximum.x <= plate_w / 2 + m
                        and bb.minimum.z >= -plate_d / 2 - m and bb.maximum.z <= plate_d / 2 + m)
            objects.append(item)

        summary = {
            "plate_mm": {"x": plate_w, "y": plate_d},
            "object_count": len(objects),
            "objects": objects,
        }

        content = []
        image = None
        # NB: forcing the camera matrix (Snapshot.isometricSnapshot) does NOT help when
        # Cura is unfocused — the bottleneck is the absent current OpenGL context while
        # the window's render loop is throttled, not the camera framing. So we use the
        # standard snapshot(), which renders fine once Cura is the active window.
        try:
            image = Snapshot.snapshot(width=width, height=height)
        except Exception as exc:  # noqa: BLE001
            Logger.log("w", "Cura MCP: snapshot render failed: %s", exc)
        b64 = None
        png_len = 0
        if image is not None:
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buf, "PNG")
            buf.close()
            png_len = ba.size()
            b64 = bytes(ba.toBase64()).decode("ascii")
        # A throttled/partial render (Cura not the active window) yields a tiny,
        # near-uniform PNG (a "sliver"). Reject it rather than return a broken image.
        min_bytes = max(4000, (width * height) // 80)
        if b64 is not None and png_len >= min_bytes:
            content.append({"type": "image", "data": b64, "mimeType": "image/png"})
        elif objects:
            summary["note"] = ("No usable image: Cura throttles its render loop while its window is not "
                               "the active/foreground one, so the snapshot came back empty or partial. "
                               "Click Cura to bring it to the front, then retry. The layout data above "
                               "is accurate regardless.")
        else:
            summary["note"] = "No image: the build plate is empty."
        content.append({"type": "text",
                        "text": json.dumps(summary, ensure_ascii=False, indent=2)})
        return {"__mcp_content__": content}

    # -- 9-11. placement / orientation ----------------------------------- #

    def _target_nodes(self, name=None):
        from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
        scene = CuraApplication.getInstance().getController().getScene()
        nodes = [n for n in DepthFirstIterator(scene.getRoot()) if n.callDecoration("isSliceable")]
        if name:
            nodes = [n for n in nodes if (n.getName() or "") == name]
        return nodes

    def rotate_model(self, deg_x=0, deg_y=0, deg_z=0, name=None, absolute=False):
        """Rotate the model(s) by the given degrees around each world axis.
        absolute=True resets orientation first (so the angles are absolute)."""
        def work():
            import math as _m
            from UM.Math.Quaternion import Quaternion
            from UM.Math.Vector import Vector
            from UM.Scene.SceneNode import SceneNode
            nodes = self._target_nodes(name)
            if not nodes:
                raise RuntimeError("No model on the plate to rotate")
            done = []
            for node in nodes:
                if absolute:
                    node.setOrientation(Quaternion(), SceneNode.TransformSpace.World)
                for deg, axis in ((deg_x, Vector.Unit_X), (deg_y, Vector.Unit_Y), (deg_z, Vector.Unit_Z)):
                    if deg:
                        node.rotate(Quaternion.fromAngleAxis(_m.radians(float(deg)), axis),
                                    SceneNode.TransformSpace.World)
                done.append(node.getName() or "model")
            return done
        names = self.invoker.call(work)
        time.sleep(0.4)  # let PlatformPhysics drop the model back onto the plate
        return {"rotated": names, "applied_deg": {"x": deg_x, "y": deg_y, "z": deg_z},
                "absolute": bool(absolute)}

    def reset_orientation(self, name=None):
        def work():
            from UM.Math.Quaternion import Quaternion
            from UM.Scene.SceneNode import SceneNode
            nodes = self._target_nodes(name)
            if not nodes:
                raise RuntimeError("No model on the plate")
            for node in nodes:
                node.setOrientation(Quaternion(), SceneNode.TransformSpace.World)
            return [n.getName() or "model" for n in nodes]
        r = self.invoker.call(work)
        time.sleep(0.4)
        return {"reset": r}

    def arrange_all(self):
        """Auto-arrange every model on the plate (Cura's nesting algorithm)."""
        def work():
            from cura.Arranging.Nest2DArrange import Nest2DArrange
            app = CuraApplication.getInstance()
            nodes = self._target_nodes()
            if not nodes:
                raise RuntimeError("No models to arrange")
            ok = Nest2DArrange(nodes, app.getBuildVolume(), [], factor=1000).arrange()
            return bool(ok)
        ok = self.invoker.call(work)
        time.sleep(0.3)
        return {"arranged": ok,
                "note": "" if ok else "could not fit all objects within the build volume"}

    def lay_flat(self, name=None):
        """Rotate the model(s) so a flat face rests fully on the plate (fixes a model
        left resting on an edge/point after rotation). Uses Cura's lay-flat heuristic
        (levels the lowest vertices), then lets it drop onto the plate."""
        def work():
            from UM.Operations.LayFlatOperation import LayFlatOperation
            nodes = self._target_nodes(name)
            if not nodes:
                raise RuntimeError("No model on the plate to lay flat")
            done = []
            for node in nodes:
                if node.getMeshData() is None:
                    continue
                LayFlatOperation(node).process()   # rotates node so its lowest face is level
                done.append(node.getName() or "model")
            return done
        names = self.invoker.call(work, timeout=180)  # scans all vertices; big meshes are slower
        time.sleep(0.5)  # let PlatformPhysics seat it on the plate
        return {"laid_flat": names}

    CAMERA_PRESETS = {
        "iso": ("3d", 0), "3d": ("3d", 0),
        "front": ("home", 0), "home": ("home", 0),
        "back": ("x", 180),
        "left": ("x", 90),
        "right": ("x", 270),
        "top": ("y", 90),
        "bottom": ("y", 270),
    }

    def set_camera(self, view="iso", zoom=1.0):
        """Set Cura's 3D view to a standard preset and zoom to frame the model.

        The preset (via setCameraRotation) fixes the orientation + up vector; then the
        camera is moved to a distance that frames the object's bounding box for Cura's
        30° perspective FOV (zoom>1 = closer, <1 = further). Falls back to Cura's default
        plate framing if there is no model on the plate."""
        view = (view or "iso").lower()
        if view not in self.CAMERA_PRESETS:
            raise ValueError("Unknown view '%s'. Options: %s"
                             % (view, ", ".join(sorted(self.CAMERA_PRESETS))))
        coord, angle = self.CAMERA_PRESETS[view]
        zoom = max(0.1, min(float(zoom), 10.0))

        def work():
            import math as _m
            from UM.Math.Vector import Vector
            from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
            app = CuraApplication.getInstance()
            ctrl = app.getController()
            scene = ctrl.getScene()
            ctrl.setCameraRotation(coord, angle)   # orientation + up + default plate framing
            cam = scene.getActiveCamera()
            if cam is None:
                return {"view": view, "zoomed": False}
            bb = None
            for node in DepthFirstIterator(scene.getRoot()):
                if node.callDecoration("isSliceable"):
                    bb = node.getBoundingBox() if bb is None else bb + node.getBoundingBox()
            if bb is None:
                return {"view": view, "zoomed": False, "note": "no model on the plate; showing build plate"}
            center = bb.center
            size = max(bb.width, bb.height, bb.depth)
            # Keep the preset's orientation, just move along the current view axis to a
            # distance that frames 'size' at the 30° vertical FOV (half-angle 15°).
            offset = cam.getPosition() - center
            if offset.length() < 1e-3:
                offset = Vector(-1.0, 0.8, 0.9)
            dist = (size * 0.5) / _m.tan(_m.radians(15.0)) * 1.5 / zoom
            cam.setPosition(center + offset.normalized() * dist)
            try:
                ct = ctrl.getTool("CameraTool")
                if ct is not None:
                    ct.setOrigin(center)   # pivot subsequent interaction on the object
            except Exception:
                pass
            return {"view": view, "zoomed": True, "zoom": zoom,
                    "object_size_mm": round(size, 1),
                    "camera_distance_mm": round(dist, 1)}
        return self.invoker.call(work)


# --------------------------------------------------------------------------- #
# MCP tool schema advertised to the client (via the proxy).
# --------------------------------------------------------------------------- #

TOOL_DEFS = [
    {
        "name": "list_printers",
        "description": "List the machines configured in Cura with build volume, nozzle "
                       "size and which one is active.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_settings",
        "description": "Read active slicing settings (temperature, speed, cooling, "
                       "adhesion, layer height, etc.). Optionally pass 'keys' to read "
                       "specific Cura setting keys.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keys": {"type": "array", "items": {"type": "string"},
                         "description": "Cura setting keys to read; omit for a curated default set."}
            },
        },
    },
    {
        "name": "set_setting",
        "description": "Change one setting on the active stack. Per-extruder settings are "
                       "written to the first extruder (T0). Returns old and new value.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Cura setting key, e.g. 'material_print_temperature'."},
                "value": {"description": "New value (coerced to the setting's type)."},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "load_model",
        "description": "Load a 3D model onto the build plate (.stl, .obj, .3mf, .ply, .amf, ...). "
                       "Path must be a local file readable by Cura.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path to the model file."}},
            "required": ["path"],
        },
    },
    {
        "name": "slice",
        "description": "Slice the current scene and return estimated print time and material use. "
                       "Requires a model on the plate.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "export_gcode",
        "description": "Export the sliced g-code to a .gcode file. The active machine's "
                       "post-processing scripts are applied. Side-effect: writes a file — requires confirm=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Destination .gcode path."},
                "confirm": {"type": "boolean", "description": "Must be true to proceed."},
            },
            "required": ["path", "confirm"],
        },
    },
    {
        "name": "send_to_octoprint",
        "description": "Upload the current (or a given) g-code file to OctoPrint. Side-effect: "
                       "network upload to the printer — requires confirm=true. Set start_print=true "
                       "to also start printing (default: only upload).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Optional .gcode to upload; omit to render the current slice."},
                "confirm": {"type": "boolean", "description": "Must be true to proceed."},
                "start_print": {"type": "boolean", "description": "Also start the print (default false)."},
            },
            "required": ["confirm"],
        },
    },
    {
        "name": "rotate_model",
        "description": "Rotate the model(s) on the plate by degrees around each axis (Z is vertical). "
                       "The model auto-drops back onto the plate. Pass absolute=true to reset "
                       "orientation before applying the angles. Optional 'name' targets one object.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "deg_x": {"type": "number", "description": "Rotation around X (tilt front/back), degrees."},
                "deg_y": {"type": "number", "description": "Rotation around Y (tilt left/right), degrees."},
                "deg_z": {"type": "number", "description": "Rotation around Z (spin on the plate), degrees."},
                "absolute": {"type": "boolean", "description": "Reset orientation first (default false)."},
                "name": {"type": "string", "description": "Target object name; omit for all."},
            },
        },
    },
    {
        "name": "reset_orientation",
        "description": "Reset model orientation to its loaded (identity) rotation. Optional 'name'.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Target object name; omit for all."}},
        },
    },
    {
        "name": "lay_flat",
        "description": "Rotate the model so a flat face rests fully on the plate (fixes a model left "
                       "sitting on an edge/point after rotation, which would print with a gap). "
                       "Optional 'name' targets one object.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Target object name; omit for all."}},
        },
    },
    {
        "name": "arrange_all",
        "description": "Auto-arrange all models on the build plate (Cura's nesting), spacing them "
                       "out and placing them within the printable area.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_camera",
        "description": "Point Cura's 3D view at a standard preset angle (iso, front, back, left, "
                       "right, top, bottom) and zoom to frame the model on the plate. Useful before "
                       "capturing the Cura window so the model is shown from a known viewpoint.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": {"type": "string",
                         "enum": ["iso", "front", "back", "left", "right", "top", "bottom"],
                         "description": "Preset viewpoint (default iso)."},
                "zoom": {"type": "number",
                         "description": "Zoom on the model: 1.0 frames it with margin (default), "
                                        ">1 closer, <1 further."},
            },
        },
    },
    {
        "name": "get_plate_view",
        "description": "Render a snapshot image (PNG) of the current build plate showing how the "
                       "model(s) are placed, plus a text summary of each object's size, position "
                       "and whether it fits within the plate. Use this to visually check the layout.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "width": {"type": "integer", "description": "Image width in px (64-1024, default 400)."},
                "height": {"type": "integer", "description": "Image height in px (64-1024, default 400)."},
                "focus": {"type": "boolean", "description": "Bring Cura's window to the front so the "
                          "OpenGL snapshot renders reliably (default true; set false to avoid stealing focus)."},
            },
        },
    },
]
