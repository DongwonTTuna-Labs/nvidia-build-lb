from .browser_evidence import AdversarialProbe, AdversarialReceipt


def adversarial_receipt() -> AdversarialReceipt:
    return AdversarialReceipt(
        probes=(
            AdversarialProbe(
                probe_class="malformed_input",
                status="passed",
                observable="safe 422 query/body tests and non-reflecting UI errors",
            ),
            AdversarialProbe(
                probe_class="stale_state",
                status="passed",
                observable="aborted overview refresh labels retained rows STALE then recovers",
            ),
            AdversarialProbe(
                probe_class="dirty_worktree",
                status="passed",
                observable="untracked product files remain preserved without reset or clean",
            ),
            AdversarialProbe(
                probe_class="cancel_resume",
                status="passed",
                observable="browser dialog Escape returns focus and remains keyboard usable",
            ),
            AdversarialProbe(
                probe_class="misleading_success_output",
                status="passed",
                observable="target requires real browser and nonempty evidence manifests",
            ),
            AdversarialProbe(
                probe_class="prompt_injection",
                status="passed",
                observable="instruction and XSS-shaped label stays inert text",
            ),
        )
    )
