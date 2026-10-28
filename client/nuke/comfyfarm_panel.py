"""ComfyFarm panel for Nuke: submit and track farm jobs without the web browser.

The panel builds its form from the farm's workflow definitions, so it shows the same
fields as the web dashboard and stays in sync when a workflow changes. Artists
prerender the plate to a PNG sequence (or use a movhistory .mov), then:

  1. pick the sequence: "From selected" (Read/Write node), "Browse…", or paste a path
  2. check the destination line (exact folder and file names, like the web form)
  3. Submit. The job list updates live; a Read node is created when the job finishes.

Layout of this file:
  PanelModel      everything the panel does, without Qt or Nuke (unit-tested)
  ComfyFarmPanel  the Qt widget (PySide6 in Nuke 16, PySide2 in Nuke 13-15)

Outside Nuke, `python comfyfarm_panel.py` opens it as a window (needs PySide).
"""

from __future__ import annotations

import getpass
import html
import threading
import time

try:
    import comfyfarm_client as farm
except ImportError:   # run straight from a checkout: python client/nuke/comfyfarm_panel.py
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    import comfyfarm_client as farm

STATUS_COLORS = {"preparing": "#ecc98a", "queued": "#cdbdf0", "dispatched": "#b8c6d8", "running": "#9fcdf2",
                 "postprocessing": "#9fcdf2", "done": "#93d6a6", "failed": "#f0a59c", "cancelled": "#a3a9b0"}
COLUMNS = ["Job", "Shot / name", "User", "Status", "Progress", "Wait", "Submitted", "Took"]


def _ago(t: float | None, now: float) -> str:
    if not t:
        return ""
    s = now - t
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _dur(s: float | None) -> str:
    if s is None:
        return ""
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    m = s // 60
    return f"{m}m {s % 60:02d}s" if m < 60 else f"{m // 60}h {m % 60:02d}m"


class PanelModel:
    """What the panel does, independent of Qt and Nuke."""

    def __init__(self, client=farm, user: str | None = None):
        self.client = client
        self.user = user or getpass.getuser()
        self.workflows: list[dict] = []
        self.session_jobs: dict[str, str] = {}   # id -> last status, for jobs submitted here
        self.sources: dict[str, str] = {}        # id -> Nuke node the input came from

    # ------------------------------------------------------------ workflows / form
    def load_workflows(self) -> list[dict]:
        self.workflows = self.client.workflows()
        return self.workflows

    def workflow(self, name: str) -> dict | None:
        return next((w for w in self.workflows if w["name"] == name), None)

    @staticmethod
    def sequence_param(wf: dict) -> str | None:
        return next((p["name"] for p in wf["params"] if p["type"] == "sequence"), None)

    @staticmethod
    def output_param(wf: dict) -> str | None:
        return next((p["name"] for p in wf["params"] if p["type"] == "output_folder"), None)

    @staticmethod
    def params_from_form(wf: dict, raw: dict) -> dict:
        """Form values -> request params. Empty fields are left out (the farm applies its
        defaults); numbers go as text and the farm validates them."""
        out = {}
        for p in wf["params"]:
            name = p["name"]
            if name not in raw:
                continue
            value = raw[name]
            if p["type"] == "bool":
                out[name] = bool(value)
                continue
            if value is None:
                continue
            if p["type"] == "choice":
                out[name] = next((c for c in p.get("choices") or [] if str(c) == str(value)), value)
                continue
            text = value if p["type"] == "text" else str(value).strip()
            if text.strip() == "":
                continue
            out[name] = text
        return out

    # ------------------------------------------------------------ actions
    def plan(self, wf_name: str, raw: dict) -> dict:
        return self.client.plan(wf_name, self.params_from_form(self.workflow(wf_name), raw))

    def submit(self, wf_name: str, raw: dict, name: str | None = None, source_node: str | None = None) -> dict:
        job = self.client.submit(wf_name, self.params_from_form(self.workflow(wf_name), raw), name=name or None)
        self.session_jobs[job["id"]] = job["status"]
        if source_node:
            self.sources[job["id"]] = source_node
        return job

    def submit_many(self, wf_name: str, raw: dict, inputs: list[tuple[str, str]]) -> list[tuple[str, dict | None, str | None]]:
        """One job per (node name, input) with the rest of the form shared."""
        seq = self.sequence_param(self.workflow(wf_name))
        results = []
        for node_name, value in inputs:
            try:
                results.append((node_name, self.submit(wf_name, dict(raw, **{seq: value}), source_node=node_name), None))
            except farm.FarmError as e:
                results.append((node_name, None, str(e)))
        return results

    def poll(self, mine: bool = True, active_only: bool = True, limit: int = 200) -> tuple[list[dict], list[dict]]:
        """(jobs to show, session jobs that finished since the last poll)."""
        jobs = self.client.jobs(user=self.user if mine else None, active=active_only, limit=limit)
        by_id = {j["id"]: j for j in jobs}
        finished = []
        for jid, last in list(self.session_jobs.items()):
            job = by_id.get(jid)
            if job is None:
                try:
                    job = self.client.get(jid)
                except farm.FarmError:
                    continue
            if job["status"] in farm.FINISHED:
                if last not in farm.FINISHED:
                    finished.append(job)
                del self.session_jobs[jid]
            else:
                self.session_jobs[jid] = job["status"]
        return jobs, finished

    # ------------------------------------------------------------ text for the view
    @staticmethod
    def destination_html(plan: dict) -> str:
        """The 'Will write …' preview, as the web dashboard shows it."""
        e = html.escape
        files = f"<code>{e(plan['first_file'])}</code>"
        if plan.get("last_file") and plan["last_file"] != plan["first_file"]:
            files += f" … <code>{e(plan['last_file'])}</code>"
        frames = plan.get("what") or (f"{plan['frames']} frames" if plan.get("frames") else "frames")
        out = (f"<b style='color:#93d6a6'>Will write</b> {e(frames)} to "
               f"<code>{e(plan.get('output_dir_posix') or plan.get('output_dir') or '')}/</code><br>as {files}")
        if plan.get("maps"):
            out += "<br>Maps: " + " ".join(f"<code>{e(m)}</code>" for m in plan["maps"])
        c = plan.get("convert") or {}
        if c.get("kind") == "exr" and c.get("mode") == "colorconvert":
            out += (f"<br>EXR textures converted first: <code>{e(c.get('from', ''))}</code> → "
                    f"<code>{e(c.get('to', ''))}</code>")
        elif c.get("kind") == "movie":
            out += (f"<br>Movie: {e(str(c.get('codec') or ''))} {c.get('width') or '?'}×{c.get('height') or '?'} "
                    f"at {round(c.get('fps') or 0, 3):g} fps, ")
            out += (f"timecode <code>{e(c['timecode'])}</code> → frames {c['start']}-{c['end']}" if c.get("timecode")
                    else f"frames {c['start']}-{c['end']}")
            if c.get("slate_frame") is not None:
                out += f", first frame ({c['slate_frame']}) is a slate and is skipped"
        elif c.get("kind") == "exr":
            out += (f"<br>EXR converted first: <code>{e(c.get('input_colorspace', ''))}</code> → "
                    f"<code>{e(c.get('display', ''))} / {e(c.get('view', ''))}</code>, "
                    + (f"shot LUTs from <code>{e(c['bundle'])}</code>" if c.get("bundle") else "show default LUTs"))
        if (plan.get("version") or 1) > 1:
            out += (f"<br><span style='color:#e0a24a'>Earlier versions already exist there (or are queued), "
                    f"so this run is v{plan['version']:03d}.</span>")
        for note in plan.get("notes") or []:
            out += f"<br><span style='color:#e0a24a'>{e(note)}</span>"
        if not plan.get("picked") and plan.get("shot"):
            out += f"<br>Shot <code>{e(plan['shot'])}</code>, show <code>{e(plan.get('show') or '')}</code>."
        elif not plan.get("picked") and plan.get("asset"):
            out += f"<br>Asset <code>{e(plan['asset'])}</code>, show <code>{e(plan.get('show') or '')}</code>."
        return out

    @staticmethod
    def row(job: dict, now: float | None = None) -> list[str]:
        now = now or time.time()
        status = job["status"]
        start = job.get("started_at") or job.get("dispatched_at")
        took = _dur(job["finished_at"] - start) if job.get("finished_at") and start else (
            _dur(now - job["started_at"]) if job.get("started_at") else "")
        if status == "queued":
            wait = f"#{job.get('queue_position') or '?'}" + (f" · ~{_dur(job['eta_seconds'])}" if job.get("eta_seconds") else "")
        elif status in ("dispatched", "running") and job.get("eta_seconds"):
            wait = f"~{_dur(job['eta_seconds'])} left"
        else:
            wait = ""
        if status == "failed":
            progress = job.get("error") or ""
        elif status in ("running", "postprocessing", "dispatched", "done"):
            progress = f"{round((job.get('progress') or 0) * 100)}%  {job.get('stage') or ''}".strip()
        else:
            progress = job.get("stage") or ""
        name = job.get("name") or ""
        version = (job.get("info") or {}).get("version")
        if version:
            name += f"  v{version:03d}"
        return [job.get("short_id") or job["id"][:8], name, job.get("user", ""), status, progress, wait,
                _ago(job.get("created_at"), now), took]


# ============================================================== Qt view
try:
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
    except ImportError:
        from PySide2 import QtCore, QtGui, QtWidgets
except ImportError:   # no Qt: the model above is still importable (tests, scripts)
    QtCore = QtGui = QtWidgets = None

try:
    import nuke  # noqa: F401
    import comfyfarm_nuke as nk
except ImportError:
    nuke = nk = None


if QtWidgets is not None:

    class _Relay(QtCore.QObject):
        """Delivers results from worker threads to the UI thread."""
        finished = QtCore.Signal(object, object, object)   # callback, result, error

    class ComfyFarmPanel(QtWidgets.QWidget):
        instances: list = []

        def __init__(self, parent=None):
            super().__init__(parent)
            ComfyFarmPanel.instances.append(self)
            self.model = PanelModel()
            self.settings = QtCore.QSettings("ComfyFarm", "NukePanel")
            if not farm.base_url() and self.settings.value("farm_url"):
                farm.set_url(str(self.settings.value("farm_url")))
            self.relay = _Relay(self)
            self.relay.finished.connect(self._deliver, QtCore.Qt.QueuedConnection)
            self.fields: dict[str, tuple[dict, object]] = {}
            self.name_edit = None
            self._jobs: dict[str, dict] = {}       # shown in the table, by id
            self._selected_node: tuple[str, str] | None = None   # (node, input) from "From selected"
            self._pending: tuple | None = None   # prefill waiting for the workflows to load
            self._plan_seq = 0
            self._polling = False
            self._polls = 0
            self._build()
            self.plan_timer = QtCore.QTimer(self)
            self.plan_timer.setSingleShot(True)
            self.plan_timer.timeout.connect(self._update_plan)
            self.poll_timer = QtCore.QTimer(self)
            self.poll_timer.setInterval(3000)
            self.poll_timer.timeout.connect(lambda: self._poll())
            self.poll_timer.start()
            self._connect()

        # -------------------------------------------------------- async plumbing
        def _async(self, fn, callback):
            def work():
                try:
                    result, error = fn(), None
                except Exception as e:  # noqa: BLE001 - shown in the panel
                    result, error = None, e
                try:
                    self.relay.finished.emit(callback, result, error)
                except RuntimeError:   # the panel was closed while the request ran
                    pass
            threading.Thread(target=work, daemon=True).start()

        def _deliver(self, callback, result, error):
            try:
                callback(result, error)
            except RuntimeError:   # the panel was closed while the request ran
                pass

        # -------------------------------------------------------- layout
        def _build(self):
            outer = QtWidgets.QVBoxLayout(self)
            outer.setContentsMargins(6, 6, 6, 6)

            # Three panes with draggable handles: farm status | workflow form | job list.
            split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
            outer.addWidget(split, 1)

            header = QtWidgets.QWidget()
            head = QtWidgets.QHBoxLayout(header)
            head.setContentsMargins(0, 0, 0, 0)
            head.setAlignment(QtCore.Qt.AlignTop)
            self.status_label = QtWidgets.QLabel("Connecting…")
            self.status_label.setTextFormat(QtCore.Qt.RichText)
            self.status_label.setWordWrap(True)   # wraps into the pane's height when narrow
            self.url_edit = QtWidgets.QLineEdit(farm.base_url() or "")
            self.url_edit.setPlaceholderText("http://comfyfarm01:8000")
            self.url_edit.setToolTip("Farm address (normally from $COMFYFARM_URL)")
            self.url_edit.editingFinished.connect(self._url_changed)
            dash = QtWidgets.QPushButton("Dashboard")
            dash.setToolTip("Open the web dashboard")
            dash.clicked.connect(lambda *_: self._open_dashboard())
            head.addWidget(self.status_label, 1)
            head.addWidget(QtWidgets.QLabel("Farm:"))
            head.addWidget(self.url_edit)
            head.addWidget(dash)
            split.addWidget(header)

            top = QtWidgets.QWidget()
            tl = QtWidgets.QVBoxLayout(top)
            tl.setContentsMargins(0, 0, 0, 0)
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel("Workflow:"))
            self.wf_combo = QtWidgets.QComboBox()
            self.wf_combo.currentIndexChanged.connect(self._workflow_changed)
            row.addWidget(self.wf_combo, 1)
            tl.addLayout(row)
            self.desc_label = QtWidgets.QLabel("")
            self.desc_label.setWordWrap(True)
            tl.addWidget(self.desc_label)
            self.form_host = QtWidgets.QWidget()
            self.form = QtWidgets.QFormLayout(self.form_host)
            self.form.setContentsMargins(0, 0, 0, 0)
            tl.addWidget(self.form_host)
            self.dest_label = QtWidgets.QLabel("")
            self.dest_label.setTextFormat(QtCore.Qt.RichText)
            self.dest_label.setWordWrap(True)
            self.dest_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            self.dest_label.setStyleSheet("QLabel { border-left: 2px solid #555; padding: 4px 8px; }")
            tl.addWidget(self.dest_label)
            actions = QtWidgets.QHBoxLayout()
            self.submit_btn = QtWidgets.QPushButton("Submit")
            self.submit_btn.clicked.connect(lambda *_: self._submit())
            self.batch_btn = QtWidgets.QPushButton("Submit selected nodes")
            self.batch_btn.setToolTip("One job per selected Read/Write node, with the settings above")
            self.batch_btn.clicked.connect(lambda *_: self._submit_selected())
            self.batch_btn.setEnabled(nk is not None)
            self.msg_label = QtWidgets.QLabel("")
            self.msg_label.setWordWrap(True)
            self.msg_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            actions.addWidget(self.submit_btn)
            actions.addWidget(self.batch_btn)
            actions.addWidget(self.msg_label, 1)
            tl.addLayout(actions)
            self.autoread_check = QtWidgets.QCheckBox("Create a Read node when my jobs finish")
            self.autoread_check.setChecked(str(self.settings.value("auto_read", "true")) == "true" and nk is not None)
            self.autoread_check.setEnabled(nk is not None)
            self.autoread_check.toggled.connect(lambda v: self.settings.setValue("auto_read", "true" if v else "false"))
            tl.addWidget(self.autoread_check)
            split.addWidget(top)

            bottom = QtWidgets.QWidget()
            bl = QtWidgets.QVBoxLayout(bottom)
            bl.setContentsMargins(0, 0, 0, 0)
            filt = QtWidgets.QHBoxLayout()
            filt.addWidget(QtWidgets.QLabel("Jobs:"))
            self.mine_check = QtWidgets.QCheckBox("Only mine")
            self.mine_check.setChecked(False)   # show everyone's jobs, like the dashboard
            self.active_check = QtWidgets.QCheckBox("Only active")
            self.active_check.setChecked(False)
            refresh = QtWidgets.QPushButton("Refresh")
            for w in (self.mine_check, self.active_check):
                w.toggled.connect(lambda _v: self._poll(force=True))
            refresh.clicked.connect(lambda *_: self._poll(force=True))
            filt.addWidget(self.mine_check)
            filt.addWidget(self.active_check)
            filt.addStretch(1)
            filt.addWidget(refresh)
            bl.addLayout(filt)
            self.table = QtWidgets.QTreeWidget()
            self.table.setColumnCount(len(COLUMNS))
            self.table.setHeaderLabels(COLUMNS)
            self.table.setRootIsDecorated(False)
            self.table.setUniformRowHeights(True)
            self.table.setAlternatingRowColors(True)
            self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
            self.table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            self.table.customContextMenuRequested.connect(self._job_menu)
            self.table.itemDoubleClicked.connect(lambda item, _c: self._read_for(item))
            bl.addWidget(self.table, 1)
            split.addWidget(bottom)
            split.setStretchFactor(0, 0)   # header and form keep their size;
            split.setStretchFactor(1, 0)
            split.setStretchFactor(2, 1)   # the job list takes the extra height

        # -------------------------------------------------------- farm / workflows
        def _connect(self):
            self.status_label.setText("Connecting…")
            self._async(lambda: (farm.health(), farm.workers(), self.model.load_workflows()), self._connected)

        def _connected(self, result, error):
            if error:
                self.status_label.setText(f"<span style='color:#e0675b'>{html.escape(str(error))}</span>")
                return
            health, workers, workflows = result
            self._show_status(health, workers)
            current = self.wf_combo.currentData() or self.settings.value("workflow")
            self.wf_combo.blockSignals(True)
            self.wf_combo.clear()
            for wf in workflows:
                self.wf_combo.addItem(wf["label"] + ("" if wf["enabled"] else "  (disabled)"), wf["name"])
            idx = max(0, self.wf_combo.findData(current)) if current else 0
            self.wf_combo.setCurrentIndex(idx)
            self.wf_combo.blockSignals(False)
            self._workflow_changed()
            self._poll(force=True)

        def _show_status(self, health, workers):
            bits = []
            for w in workers:
                dot = "#93d6a6" if w.get("online") and w.get("connected") else "#e0675b"
                gpu = ((w.get("gpu") or {}).get("name") or "").split(" : ")[0]
                gpu = gpu.split(" ", 1)[1] if gpu.startswith("cuda:") and " " in gpu else gpu   # "cuda:0 NVIDIA …"
                bits.append(f"<span style='color:{dot}'>●</span> <b>{html.escape(w['name'])}</b> "
                            f"{html.escape(gpu)} · {w.get('inflight', 0)}/{w.get('max_inflight', 0)} slots")
            bits.append(f"{health.get('queued', 0)} queued")
            self.status_label.setText("  ·  ".join(bits) + f"  ·  you are <b>{html.escape(self.model.user)}</b>")

        def _url_changed(self):
            url = self.url_edit.text().strip().rstrip("/")
            if url == (farm.base_url() or ""):
                return
            farm.set_url(url or None)
            self.settings.setValue("farm_url", url)
            self._connect()

        def _open_dashboard(self):
            import webbrowser
            if farm.base_url():
                webbrowser.open(farm.base_url() + "/")

        def _wf(self):
            return self.model.workflow(self.wf_combo.currentData()) if self.wf_combo.count() else None

        def _workflow_changed(self, *_):
            wf = self._wf()
            keep = {p["type"]: w.text() for p, w in self.fields.values() if p["type"] in ("sequence", "output_folder")}
            while self.form.rowCount():
                self.form.removeRow(0)
            self.fields.clear()
            if not wf:
                return
            self.settings.setValue("workflow", wf["name"])
            self.desc_label.setText(wf.get("description") or "")
            if wf.get("problems") or wf.get("worker_problems"):
                probs = list(wf.get("problems") or []) + [p for ps in (wf.get("worker_problems") or {}).values() for p in ps]
                self.desc_label.setText((wf.get("description") or "") + "\n\nProblems: " + "; ".join(probs))
            for p in wf["params"]:
                widget, row = self._field(wf, p)
                label = p["label"] + (" *" if p.get("required") else "")
                self.form.addRow(label, row)
                if p.get("help"):
                    widget.setToolTip(p["help"])
                self.fields[p["name"]] = (p, widget)
                if keep.get(p["type"]):
                    widget.setText(keep[p["type"]])
            self.name_edit = QtWidgets.QLineEdit()
            self.name_edit.setPlaceholderText("defaults to the shot")
            self.form.addRow("Job name", self.name_edit)
            self.submit_btn.setEnabled(bool(wf.get("enabled", True)))
            self._schedule_plan()
            if self._pending:
                pending, self._pending = self._pending, None
                self.prefill(*pending)

        def _field(self, wf, p):
            """(value widget, row widget or layout) for one parameter."""
            key = f"last/{wf['name']}/{p['name']}"
            saved = self.settings.value(key)
            t = p["type"]
            if t == "bool":
                w = QtWidgets.QCheckBox()
                w.setChecked(str(saved) == "true" if saved is not None else bool(p.get("default")))
                w.toggled.connect(lambda v, k=key: self.settings.setValue(k, "true" if v else "false"))
                return w, w
            if t == "choice":
                w = QtWidgets.QComboBox()
                w.addItems([str(c) for c in p.get("choices") or []])
                current = saved if saved is not None else p.get("default")
                if current is not None and w.findText(str(current)) >= 0:
                    w.setCurrentIndex(w.findText(str(current)))
                w.currentTextChanged.connect(lambda v, k=key: self.settings.setValue(k, v))
                if p.get("naming"):   # in the output file names: refresh the preview
                    w.currentTextChanged.connect(self._schedule_plan)
                return w, w
            if t == "text":
                w = QtWidgets.QPlainTextEdit(str(saved if saved is not None else p.get("default") or ""))
                w.setFixedHeight(54)
                w.textChanged.connect(lambda k=key, w=w: self.settings.setValue(k, w.toPlainText()))
                return w, w
            w = QtWidgets.QLineEdit()
            if t in ("sequence", "output_folder"):
                # Never pre-filled from last time: a path from another shot would send
                # renders to the wrong place.
                w.setPlaceholderText(p.get("placeholder") or ("/rdo/shows/…/shot.step.%04d.png 1001-1100"
                                                              if t == "sequence" else ""))
                w.textChanged.connect(self._schedule_plan)
                row = QtWidgets.QHBoxLayout()
                row.addWidget(w, 1)
                if t == "sequence":
                    sel = QtWidgets.QPushButton("From selected")
                    sel.setToolTip("Use the selected Read or Write node's file and frame range")
                    sel.setEnabled(nk is not None)
                    sel.clicked.connect(lambda *_: self.fill_from_selection())
                    row.addWidget(sel)
                browse = QtWidgets.QPushButton("Browse…")
                browse.clicked.connect(lambda _c=False, w=w, t=t: self._browse(w, t))
                row.addWidget(browse)
                return w, row
            w.setText(str(saved if saved is not None else ("" if p.get("default") is None else p["default"])))
            w.textChanged.connect(lambda v, k=key: self.settings.setValue(k, v))
            if p.get("naming"):
                w.textChanged.connect(self._schedule_plan)
            return w, w

        def _browse(self, widget, kind):
            if nk is not None:
                value = nk.browse_sequence() if kind == "sequence" else nk.browse_folder()
            elif kind == "sequence":
                value = QtWidgets.QFileDialog.getOpenFileName(self, "Input")[0]
            else:
                value = QtWidgets.QFileDialog.getExistingDirectory(self, "Output folder")
            if value:
                widget.setText(value)

        def prefill(self, workflow=None, found=None):
            """Pick a workflow and fill the input from selected nodes, once the form exists."""
            if not self.fields:
                self._pending = (workflow, found)
                return
            if workflow:
                idx = self.wf_combo.findData(workflow)
                if idx >= 0 and idx != self.wf_combo.currentIndex():
                    self.wf_combo.setCurrentIndex(idx)
            self.fill_from_selection(found)

        def fill_from_selection(self, found=None):
            if nk is None:
                return
            found = found if found is not None else nk.selected_sequences()
            seq = PanelModel.sequence_param(self._wf() or {"params": []})
            if not found or not seq:
                self._message("Select a Read or Write node with a rendered PNG sequence (or a movhistory .mov).", True)
                return
            self.fields[seq][1].setText(found[0][1])
            self._selected_node = found[0]
            if len(found) > 1:
                self._message(f"{len(found)} nodes selected: used {found[0][0]}. "
                              f"'Submit selected nodes' submits all of them.")

        def _raw(self) -> dict:
            raw = {}
            for name, (p, w) in self.fields.items():
                if p["type"] == "bool":
                    raw[name] = w.isChecked()
                elif p["type"] == "choice":
                    raw[name] = w.currentText()
                elif p["type"] == "text":
                    raw[name] = w.toPlainText()
                else:
                    raw[name] = w.text()
            return raw

        # -------------------------------------------------------- destination preview
        def _schedule_plan(self, *_):
            self.plan_timer.start(450)

        def _update_plan(self):
            wf = self._wf()
            if not wf:
                return
            raw = self._raw()
            seq = PanelModel.sequence_param(wf)
            if seq and not raw.get(seq, "").strip():
                self.dest_label.setText("")
                return
            self._plan_seq += 1
            n = self._plan_seq
            self.dest_label.setText("Checking…")

            def done(result, error):
                if n != self._plan_seq:
                    return   # a newer request is on its way
                if error:
                    self.dest_label.setText(f"<span style='color:#e0675b'>{html.escape(str(error))}</span>")
                else:
                    self.dest_label.setText(PanelModel.destination_html(result))
            self._async(lambda: self.model.plan(wf["name"], raw), done)

        # -------------------------------------------------------- submit
        def _message(self, text, error=False):
            color = "#e0675b" if error else "#93d6a6"
            self.msg_label.setText(f"<span style='color:{color}'>{html.escape(text)}</span>")

        def _submit(self):
            wf = self._wf()
            if not wf:
                return
            raw, name = self._raw(), self.name_edit.text().strip() if self.name_edit else ""
            seq = PanelModel.sequence_param(wf)
            node = self._selected_node[0] if self._selected_node and seq and \
                raw.get(seq, "").strip() == self._selected_node[1] else None
            self.submit_btn.setEnabled(False)
            self.msg_label.setText("Submitting…")

            def done(job, error):
                self.submit_btn.setEnabled(True)
                if error:
                    self._message(str(error), True)
                    return
                info = job.get("info") or {}
                self._message(f"Queued {job['short_id']} → {info.get('output_dir_posix') or info.get('output_dir')}")
                self._schedule_plan()   # that version is taken now
                self._poll(force=True)
            self._async(lambda: self.model.submit(wf["name"], raw, name, source_node=node), done)

        def _submit_selected(self):
            wf = self._wf()
            if not wf or nk is None:
                return
            found = nk.selected_sequences()
            if not found:
                self._message("Select one or more Read/Write nodes first.", True)
                return
            raw = self._raw()
            self.batch_btn.setEnabled(False)
            self.msg_label.setText(f"Submitting {len(found)} jobs…")

            def done(results, error):
                self.batch_btn.setEnabled(True)
                if error:
                    self._message(str(error), True)
                    return
                ok = [r for r in results if r[1]]
                bad = [f"{n}: {e}" for n, _j, e in results if e]
                self._message(f"Queued {len(ok)} of {len(results)}." + (" Not submitted: " + " | ".join(bad) if bad else ""),
                              bool(bad))
                self._poll(force=True)
            self._async(lambda: self.model.submit_many(wf["name"], raw, found), done)

        # -------------------------------------------------------- jobs
        def _poll(self, force=False):
            # Hidden tab: still follow jobs submitted from here (for their Read nodes).
            if self._polling or (not force and not self.isVisible() and not self.model.session_jobs):
                return
            self._polling = True
            self._polls += 1
            header = force or self._polls % 5 == 0
            mine, active = self.mine_check.isChecked(), self.active_check.isChecked()

            def work():
                jobs, finished = self.model.poll(mine=mine, active_only=active, limit=150)
                status = (farm.health(), farm.workers()) if header else None
                return jobs, finished, status

            def done(result, error):
                self._polling = False
                if error:
                    self.status_label.setText(f"<span style='color:#e0675b'>{html.escape(str(error))}</span>")
                    return
                jobs, finished, status = result
                if status:
                    self._show_status(*status)
                self._fill_table(jobs)
                for job in finished:
                    self._job_finished(job)
            self._async(work, done)

        def _fill_table(self, jobs):
            """Update rows in place, so scrolling and selection survive the refresh."""
            now = time.time()
            items = {}
            for i in range(self.table.topLevelItemCount()):
                item = self.table.topLevelItem(i)
                items[item.data(0, QtCore.Qt.UserRole)] = item
            wanted = {j["id"] for j in jobs}
            for jid, item in list(items.items()):
                if jid not in wanted:
                    self.table.takeTopLevelItem(self.table.indexOfTopLevelItem(item))
                    del items[jid]
            added = False
            for idx, job in enumerate(jobs):
                item = items.get(job["id"])
                if item is None:
                    item = QtWidgets.QTreeWidgetItem()
                    item.setData(0, QtCore.Qt.UserRole, job["id"])
                    self.table.insertTopLevelItem(idx, item)
                    added = True
                elif self.table.indexOfTopLevelItem(item) != idx:
                    self.table.takeTopLevelItem(self.table.indexOfTopLevelItem(item))
                    self.table.insertTopLevelItem(idx, item)
                for col, text in enumerate(PanelModel.row(job, now)):
                    item.setText(col, text)
                item.setForeground(3, QtGui.QBrush(QtGui.QColor(STATUS_COLORS.get(job["status"], "#d9dde2"))))
                item.setToolTip(4, job.get("error") or "")
            self._jobs = {j["id"]: j for j in jobs}
            if added:
                for col in range(len(COLUMNS)):
                    if col != 4:
                        self.table.resizeColumnToContents(col)

        def _job_finished(self, job):
            if job["status"] == "done":
                out = job.get("outputs") or {}
                where = (job.get("info") or {}).get("output_dir_posix") if out.get("maps") else out.get("frame_pattern_posix")
                self._message(f"{job['short_id']} finished: {where}")
                if self.autoread_check.isChecked() and nk is not None:
                    nk.create_read(job, near=self.model.sources.get(job["id"]))
            elif job["status"] == "failed":
                self._message(f"{job['short_id']} failed: {job.get('error')}", True)
            if nk is not None:
                nuke.tprint(f"[comfyfarm] {farm.describe(job)}")

        def _job_of(self, item):
            return self._jobs.get(item.data(0, QtCore.Qt.UserRole)) if item else None

        def _read_for(self, item):
            job = self._job_of(item)
            if job and job["status"] == "done" and nk is not None:
                nk.create_read(job, near=self.model.sources.get(job["id"]))

        def _job_menu(self, pos):
            job = self._job_of(self.table.itemAt(pos))
            if not job:
                return
            menu = QtWidgets.QMenu(self)
            read = menu.addAction("Create Read node")
            read.setEnabled(job["status"] == "done" and nk is not None)
            read.triggered.connect(lambda *_: nk.create_read(job, near=self.model.sources.get(job["id"])))
            copy = menu.addAction("Copy output path")
            info = job.get("info") or {}
            path = (info.get("output_dir_posix") if (job.get("outputs") or {}).get("maps") else None) \
                or farm.output_pattern(job) or info.get("output_dir_posix") or ""
            copy.setEnabled(bool(path))
            copy.triggered.connect(lambda *_: QtWidgets.QApplication.clipboard().setText(path))
            reuse = menu.addAction("Use this job's input")
            reuse.triggered.connect(lambda *_: self._reuse(job))
            menu.addSeparator()
            cancel = menu.addAction("Cancel job")
            cancel.setEnabled(job["status"] in ("preparing", "queued", "dispatched", "running"))
            cancel.triggered.connect(lambda *_: self._async(lambda: farm.cancel(job["id"]),
                                                         lambda _r, e: self._after_action(e)))
            again = menu.addAction("Run again")
            again.setEnabled(job["status"] in farm.FINISHED)
            again.triggered.connect(lambda *_: self._async(lambda: self._retry(job["id"]),
                                                        lambda _r, e: self._after_action(e)))
            if job.get("error"):
                err = menu.addAction("Show error")
                err.triggered.connect(lambda *_: QtWidgets.QMessageBox.warning(self, "ComfyFarm", job["error"]))
            run = getattr(menu, "exec", None) or getattr(menu, "exec_")
            run(self.table.viewport().mapToGlobal(pos))

        def _retry(self, job_id):
            job = farm.retry(job_id)
            self.model.session_jobs[job["id"]] = job["status"]
            return job

        def _after_action(self, error):
            if error:
                self._message(str(error), True)
            self._poll(force=True)

        def _reuse(self, job):
            seq = PanelModel.sequence_param(self._wf() or {"params": []})
            info = job.get("info") or {}
            src = (info.get("input") or {}).get("raw")
            if seq and src and seq in self.fields:
                self.fields[seq][1].setText(src)

        def closeEvent(self, event):  # noqa: N802 (Qt naming)
            self.poll_timer.stop()
            super().closeEvent(event)


def main() -> int:
    import sys
    if QtWidgets is None:
        print("PySide6 or PySide2 is needed to open the panel outside Nuke.")
        return 1
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    w = ComfyFarmPanel()
    w.setWindowTitle("ComfyFarm")
    w.resize(900, 700)
    w.show()
    run = getattr(app, "exec", None) or getattr(app, "exec_")
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
