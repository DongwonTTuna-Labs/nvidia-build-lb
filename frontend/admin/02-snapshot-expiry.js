function scheduleSnapshotExpiry(refreshStartedAt) {
  clearSnapshotTimer();
  const generatedAt = Date.parse(currentSnapshot.overview.generated_at);
  const cooldownDeadlines = currentSnapshot.upstreams
    .map((item) => Date.parse(item.cooldown_until))
    .filter((deadline) => Number.isFinite(deadline) && deadline > generatedAt);
  const cooldownDelay = cooldownDeadlines.length
    ? Math.min(...cooldownDeadlines) - generatedAt
    : snapshotTtlMs;
  const snapshotLifetime = Math.min(snapshotTtlMs, cooldownDelay);
  const elapsedDuringRefresh = Math.max(0, Date.now() - refreshStartedAt);
  const delay = Math.max(0, snapshotLifetime - elapsedDuringRefresh);
  const cooldownExpiresFirst = cooldownDelay < snapshotTtlMs;
  snapshotTimer = window.setTimeout(() => {
    const dialogOwnsAnnouncement = Boolean(document.querySelector("dialog[open]"));
    const reason = cooldownExpiresFirst
      ? "A known cooldown transition is due. Refresh before making changes."
      : "This snapshot is older than one minute. Refresh before making changes.";
    markSnapshotStale(reason);
    if (!dialogOwnsAnnouncement) announce("Administration state is stale. Mutations are locked until refresh succeeds.");
  }, delay);
}
