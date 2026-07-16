function pendingProbeEvidenceMatches(current, evidence) {
  const currentInstant = utcTimestampInstant(current.updated_at);
  const observedInstant = utcTimestampInstant(evidence.observedAt);
  return !current.enabled
    && current.health_state === "healthy"
    && current.last_status_class === "success"
    && currentInstant !== null
    && observedInstant !== null
    && currentInstant <= observedInstant;
}

function pendingPauseEvidenceMatches(current, events) {
  return events.some((event) => event.upstream_key_id === current.id
    && event.event_type === "upstream_key_disabled"
    && event.outcome_class === "succeeded"
    && event.occurred_at === current.updated_at);
}

function reconcileLocalIntent(upstreams, events) {
  if (!snapshotCurrent) return;
  const currentById = new Map(upstreams.map((item) => [item.id, item]));
  for (const [id, evidence] of enableReadyKeyEvidence) {
    const current = currentById.get(id);
    if (!current || (evidence.version !== null && current.updated_at !== evidence.version)) {
      enableReadyKeyEvidence.delete(id);
    } else if (evidence.version === null) {
      if (pendingProbeEvidenceMatches(current, evidence)) {
        enableReadyKeyEvidence.set(id, {...evidence, version: current.updated_at});
      } else {
        enableReadyKeyEvidence.delete(id);
      }
    }
  }
  for (const [id, evidence] of deliberatelyPausedKeyIds) {
    const current = currentById.get(id);
    if (!current || current.enabled
      || (evidence.version !== null && current.updated_at !== evidence.version)) {
      deliberatelyPausedKeyIds.delete(id);
    } else if (evidence.version === null) {
      if (pendingPauseEvidenceMatches(current, events)) {
        deliberatelyPausedKeyIds.set(id, {...evidence, version: current.updated_at});
      } else {
        deliberatelyPausedKeyIds.delete(id);
      }
    }
  }
  if (replacementContext && deliberatelyPausedKeyIds.has(replacementContext.replacementId)) {
    replacementContext = null;
  }
}

function keyEnableEvidenceCurrent(item) {
  return enableReadyKeyEvidence.has(item.id) && !deliberatelyPausedKeyIds.has(item.id);
}
