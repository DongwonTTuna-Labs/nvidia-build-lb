function renderProblemEvidence(valueId, problem) {
  const container = byId(valueId);
  container.replaceChildren();
  if (!problem) return;
  const message = document.createElement("p");
  message.className = "request-evidence-message";
  message.textContent = problem.message;
  const list = document.createElement("dl");
  for (const [term, value] of [["Code", problem.code], ["Request", problem.request_id]]) {
    const label = document.createElement("dt");
    const data = document.createElement("dd");
    label.textContent = term;
    data.textContent = value;
    data.className = "machine-id";
    list.append(label, data);
  }
  container.append(message, list);
}
function setProblemEvidence(errorId, problem = null) {
  const evidence = byId(`${errorId}-evidence`);
  evidence.open = false;
  evidence.hidden = !problem;
  renderProblemEvidence(`${errorId}-evidence-value`, problem);
}
