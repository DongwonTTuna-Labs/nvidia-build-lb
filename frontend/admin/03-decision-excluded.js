function recommendExcludedKey(upstreams, overview, reference) {
  const trackedReplacement = replacementContext
    ? upstreams.find((item) => item.id === replacementContext.replacementId)
    : null;
  if (trackedReplacement?.routing_state === "disabled") {
    if (trackedReplacement.health_state === "healthy"
      && keyEnableEvidenceCurrent(trackedReplacement)) {
      setText("decision-state", excludedState(overview));
      setText("decision-title", `Enable ${keyHandle(trackedReplacement)}`);
      setText("decision-explanation", `${keyHandle(trackedReplacement)} is the verified replacement. Enable it before retiring the rejected key.`);
      setRecommendation("enable", `Enable ${keyHandle(trackedReplacement)}`, trackedReplacement.id);
    } else if (trackedReplacement.health_state === "healthy") {
      setText("decision-state", excludedState(overview));
      setText("decision-title", `Probe ${keyHandle(trackedReplacement)}`);
      setText("decision-explanation", `${keyHandle(trackedReplacement)} is the replacement, but its latest version has no current probe evidence in this tab. Probe it before enabling.`);
      setRecommendation("probe", `Probe ${keyHandle(trackedReplacement)}`, trackedReplacement.id);
    } else recommendCredentialRecovery(trackedReplacement, overview, upstreams);
    return true;
  }
  if (trackedReplacement?.routing_state === "cooldown") {
    recommendCoolingKey(trackedReplacement, overview, reference);
    return true;
  }
  if (trackedReplacement?.routing_state === "quarantined") {
    recommendCredentialRecovery(trackedReplacement, overview, upstreams);
    return true;
  }
  if (trackedReplacement?.routing_state === "eligible") {
    const source = upstreams.find((item) => item.id === replacementContext.sourceId);
    if (source) {
      const action = source.enabled ? "disable" : "delete";
      setText("decision-state", `Replacement ready · ${upstreams.length} registered keys`);
      setText("decision-title", `${source.enabled ? "Disable" : "Delete"} replaced ${keyHandle(source)}`);
      setText("decision-explanation", `${keyHandle(trackedReplacement)} is verified and enabled. ${source.enabled ? `Disable ${keyHandle(source)} now; after confirmation, delete it.` : `Delete ${keyHandle(source)} now`} to return to exactly two registered, eligible keys.`);
      setRecommendation("review", `${source.enabled ? "Review disable for" : "Review delete for"} ${keyHandle(source)}`, source.id, action);
      return true;
    }
  }
  const rejected = upstreams.find((item) => item.last_status_class === "invalid_credential");
  if (rejected && overview.upstream_keys.eligible > 0) {
    recommendCredentialRecovery(rejected, overview, upstreams);
    return true;
  }
  const verifiedDisabled = upstreams.find((item) => item.routing_state === "disabled"
    && item.health_state === "healthy" && keyEnableEvidenceCurrent(item));
  if (verifiedDisabled) {
    setText("decision-state", excludedState(overview));
    setText("decision-title", `Enable ${keyHandle(verifiedDisabled)}`);
    setText("decision-explanation", `${keyHandle(verifiedDisabled)} is verified but disabled. Enable it to restore routing capacity.`);
    setRecommendation("enable", `Enable ${keyHandle(verifiedDisabled)}`, verifiedDisabled.id);
    return true;
  }
  const recoveryCandidate = upstreams.find((item) => item.id !== rejected?.id
    && item.last_status_class !== "invalid_credential"
    && ["disabled", "quarantined", "cooldown"].includes(item.routing_state)
    && !deliberatelyPausedKeyIds.has(item.id));
  if (rejected && recoveryCandidate) {
    if (recoveryCandidate.routing_state === "cooldown") recommendCoolingKey(recoveryCandidate, overview, reference);
    else if (recoveryCandidate.routing_state === "disabled" && recoveryCandidate.health_state === "healthy" && keyEnableEvidenceCurrent(recoveryCandidate)) {
      setText("decision-state", excludedState(overview));
      setText("decision-title", `Enable replacement ${keyHandle(recoveryCandidate)}`);
      setText("decision-explanation", `${keyHandle(recoveryCandidate)} is verified and the rejected key remains excluded. Enable this existing replacement before adding another.`);
      setRecommendation("enable", `Enable ${keyHandle(recoveryCandidate)}`, recoveryCandidate.id);
    } else recommendCredentialRecovery(recoveryCandidate, overview, upstreams);
    return true;
  }
  const unverifiedDisabled = upstreams.find((item) =>
    item.routing_state === "disabled" && item.health_state !== "healthy"
    && item.last_status_class !== "invalid_credential" && !deliberatelyPausedKeyIds.has(item.id));
  if (unverifiedDisabled) {
    recommendCredentialRecovery(unverifiedDisabled, overview, upstreams);
    return true;
  }
  const quarantined = upstreams.find((item) => item.routing_state === "quarantined"
    && item.last_status_class !== "invalid_credential" && !deliberatelyPausedKeyIds.has(item.id));
  if (quarantined) {
    recommendCredentialRecovery(quarantined, overview, upstreams);
    return true;
  }
  const cooling = upstreams.find((item) => item.routing_state === "cooldown"
    && item.last_status_class !== "invalid_credential" && !deliberatelyPausedKeyIds.has(item.id));
  if (cooling) {
    recommendCoolingKey(cooling, overview, reference);
    return true;
  }
  if (rejected) {
    recommendCredentialRecovery(rejected, overview, upstreams);
    return true;
  }
  return false;
}
