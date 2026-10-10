"use strict";

const $ = (id) => document.getElementById(id);

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function badge(text, kind) {
  return node("span", `badge ${kind}`, text);
}

function renderMission(mission) {
  const article = node("article", "mission");
  const top = node("div", "mission-top");
  const title = node("div", "mission-title");
  title.append(node("p", "mission-id", mission.mission_id));
  title.append(node("p", "mission-name", mission.title));
  top.append(title);
  const badges = node("div", "badges");
  const verdictStyle = { GREEN: "green", YELLOW: "yellow", RED: "red" }[mission.coordination_verdict] || "yellow";
  badges.append(badge(mission.coordination_verdict, verdictStyle));
  const statusStyle = mission.status === "VALIDATED" ? "validated"
    : ["BLOCKED", "RED", "CORRECTION_REQUIRED"].includes(mission.status) ? "red"
      : mission.status === "YELLOW" || mission.status === "WAITING_FOR_RESOURCES" ? "yellow" : "";
  badges.append(badge(mission.status, statusStyle));
  top.append(badges);
  article.append(top);

  const meta = node("div", "mission-meta");
  meta.append(node("span", "", `Priorité : ${mission.priority}`));
  if (mission.updated_at) meta.append(node("span", "", `Mise à jour registre : ${mission.updated_at}`));
  article.append(meta);

  if (mission.dependencies.length) {
    const list = node("ul", "dep-list");
    for (const dep of mission.dependencies) {
      list.append(node("li", "", `${dep.dependency_type} ${dep.mission_id} · ${dep.status}`));
    }
    article.append(list);
  }
  if (mission.verdict_reasons.length) {
    const list = node("ul", "dep-list");
    for (const reason of mission.verdict_reasons) list.append(node("li", "", reason));
    article.append(list);
  }
  return article;
}

function renderAgents(agents) {
  const container = $("agents");
  container.replaceChildren();
  for (const agent of agents) {
    const card = node("article", "agent-card panel");
    const head = node("div", "agent-head");
    const info = node("div");
    info.append(node("h3", "", agent.agent_id));
    info.append(node("p", "agent-role", agent.role));
    head.append(info);
    head.append(node("span", "agent-count", `${agent.mission_count} mission(s) · ${agent.validated_count} validée(s)`));
    card.append(head);
    const list = node("div", "mission-list");
    if (!agent.missions.length) list.append(node("p", "empty", "Aucune mission dans le registre."));
    for (const mission of agent.missions) list.append(renderMission(mission));
    card.append(list);
    container.append(card);
  }
}

function renderAlerts(alerts) {
  const container = $("alerts");
  container.replaceChildren();
  if (!alerts.length) {
    container.append(node("p", "empty", "Aucune alerte calculée à partir du registre actuel."));
    return;
  }
  for (const alert of alerts) {
    const row = node("article", "alert-row");
    const info = node("div");
    info.append(node("h3", "", `${alert.agent_id} · ${alert.mission_id}`));
    info.append(node("p", "mission-name", alert.title));
    if (alert.reasons.length) {
      const reasons = node("ul", "alert-reasons");
      for (const reason of alert.reasons) reasons.append(node("li", "", reason));
      info.append(reasons);
    }
    row.append(info);
    const style = alert.coordination_verdict === "RED" ? "red"
      : alert.coordination_verdict === "YELLOW" ? "yellow" : "validated";
    const status = node("div", "badges");
    status.append(badge(alert.coordination_verdict, style));
    status.append(badge(alert.status, alert.status === "VALIDATED" ? "validated" : "yellow"));
    row.append(status);
    container.append(row);
  }
}

function renderReports(reports) {
  const container = $("reports");
  container.replaceChildren();
  if (!reports.length) {
    container.append(node("p", "empty", "Aucun rapport Markdown référencé et accessible sous handoffs/."));
    return;
  }
  for (const report of reports) {
    const row = node("article", "report-row");
    const details = node("div");
    details.append(node("h3", "", report.name));
    details.append(node("p", "report-path", report.relative_path));
    const refs = report.references.map((ref) => `${ref.agent_id} · ${ref.mission_id}`).join("; ");
    details.append(node("p", "mission-meta", `Référencé par : ${refs}`));
    row.append(details);
    const info = node("div", "report-info");
    info.append(node("div", "", "Fichier présent · contenu non affiché"));
    info.append(node("div", "", new Date(report.modified_at).toLocaleString()));
    if (report.sha256) info.append(node("div", "", `SHA-256 ${report.sha256.slice(0, 12)}…`));
    row.append(info);
    container.append(row);
  }
}

async function refresh() {
  const button = $("refresh");
  const error = $("error");
  button.disabled = true;
  error.hidden = true;
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store", credentials: "same-origin" });
    if (!response.ok) throw new Error("Les données locales ne sont pas disponibles.");
    const data = await response.json();
    if (data.error) throw new Error(data.error);
    renderAgents(data.agents);
    renderAlerts(data.alerts);
    renderReports(data.reports);
    $("mission-count").textContent = String(data.mission_count);
    $("alert-count").textContent = String(data.alerts.length);
    $("knowledge-count").textContent = String(data.knowledge.documents);
    $("knowledge-detail").textContent = `${data.knowledge.versions} versions · ${data.knowledge.chunks} chunks · FTS5 ${data.knowledge.fts5 ? "actif" : "absent"}`;
    $("updated-at").textContent = `Données lues à ${new Date(data.generated_at).toLocaleTimeString()}`;
  } catch (_error) {
    error.textContent = "Impossible de lire l’état local. Vérifie que les bases autorisées sont disponibles, puis actualise.";
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
}

$("refresh").addEventListener("click", refresh);
refresh();
