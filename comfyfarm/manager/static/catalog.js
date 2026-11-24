// The Submit tab's pickers: Provider > Task > Workflow (decision 0054).
//
// The same menus every client draws, from GET /api/v1/catalog?hidden=true. The dashboard
// lists the farm's hidden workflows too (its GPU test), marked. No show is picked here: it
// comes from the input path, so a workflow the policy refuses a show is said when the job is
// planned, as before.
//
// The form is still built from GET /api/v1/workflows, and #wfSelect stays the workflow
// picker: what reads it is unchanged. When the catalogue can't be read, #wfSelect keeps the
// plain list it had and the other two pickers stay hidden.
//
// Loaded before the dashboard's own script, whose helpers it uses when called ($, esc, api,
// state, switchWorkflow). Its entry points: catalogInit() once the workflows are loaded, and
// catalogShow(name) to pick a workflow (a deep link). catalogPick(), catalogWhere() and the
// label functions touch no page, so a test runs them alone (tests/test_dashboard_catalog.py).
"use strict";

let catalogTree = null;            // the catalogue's providers, or null: the plain list

// A provider's name in its picker.
function catalogProviderLabel(p) {
  return p.label + (p.offsite ? " (off-site)" : "");
}

// A workflow's name in its picker, with what an artist's menu wouldn't show.
function catalogWorkflowLabel(w) {
  const notes = [];
  if (w.hidden) notes.push("hidden from artists");
  if (w.enabled === false) notes.push("turned off");
  return w.label + (notes.length ? ` (${notes.join(", ")})` : "");
}

// Where a workflow is in the menus: {provider, task} as the pickers' values ("" for no
// task), or null.
function catalogWhere(providers, name) {
  for (const p of providers || []) {
    for (const t of p.tasks || []) {
      if ((t.workflows || []).some(w => w.name === name)) return { provider: p.name, task: t.name ?? "" };
    }
  }
  return null;
}

// What the three pickers show for a choice: each one's options and what it has picked. A
// choice that isn't there (none yet, or gone) is the first. Values are names; a task with
// no name ("Other") is "".
function catalogPick(providers, provider, task, workflow) {
  const ps = providers || [];
  const p = ps.find(x => x.name === provider) || ps[0];
  const ts = p ? p.tasks || [] : [];
  const t = (task != null && ts.find(x => (x.name ?? "") === task)) || ts[0];
  const ws = t ? t.workflows || [] : [];
  const w = ws.find(x => x.name === workflow) || ws[0];
  return {
    providers: ps.map(x => ({ value: x.name, label: catalogProviderLabel(x) })),
    tasks: ts.map(x => ({ value: x.name ?? "", label: x.label })),
    workflows: ws.map(x => ({ value: x.name, label: catalogWorkflowLabel(x), title: x.description || "" })),
    provider: p ? p.name : null, task: t ? t.name ?? "" : null, workflow: w ? w.name : null,
  };
}

// Fill the three pickers for a choice.
function catalogFill(choice) {
  const pick = catalogPick(catalogTree, choice.provider, choice.task, choice.workflow);
  const fill = (sel, options, value) => {
    $(sel).innerHTML = options.map(o => `<option value="${esc(o.value)}"${o.title ? ` title="${esc(o.title)}"` : ""}>` +
                                        `${esc(o.label)}</option>`).join("");
    $(sel).value = value ?? "";
  };
  fill("#provSelect", pick.providers, pick.provider);
  fill("#taskSelect", pick.tasks, pick.task);
  fill("#wfSelect", pick.workflows, pick.workflow);
  return pick;
}

// Once GET /api/v1/workflows is in: read the catalogue and draw the pickers. A workflow the
// catalogue doesn't place (it can't be read, or it is older than the workflow list) keeps
// the plain list, so no workflow is ever out of reach.
async function catalogInit() {
  try {
    catalogTree = (await api("/api/v1/catalog?hidden=true")).providers || null;
  } catch {
    catalogTree = null;
  }
  if (catalogTree && (!catalogTree.length || state.workflows.some(w => !catalogWhere(catalogTree, w.name)))) {
    catalogTree = null;
  }
  $("#catalogPickers").classList.toggle("plain", !catalogTree);
  if (!catalogTree) return;
  catalogFill({});
  $("#provSelect").onchange = () => { catalogFill({ provider: $("#provSelect").value }); switchWorkflow(); };
  $("#taskSelect").onchange = () => {
    catalogFill({ provider: $("#provSelect").value, task: $("#taskSelect").value });
    switchWorkflow();
  };
}

// Pick a workflow by name; its provider and task follow.
function catalogShow(name) {
  const at = catalogTree && catalogWhere(catalogTree, name);
  if (at) catalogFill({ ...at, workflow: name });
  else $("#wfSelect").value = name;
}
