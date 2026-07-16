from nvidia_build_lb.admin.schemas import __all__ as public_exports


def test_admin_schema_module_exports_the_approved_boundary() -> None:
    # Given: the decision-complete administration DTO contract.
    expected = {
        "AdminDashboardEventListResponse",
        "AdminDashboardEventRead",
        "AdminDashboardRead",
        "AdminEventListResponse",
        "AdminEventRead",
        "AdminLedgerRead",
        "AdminOperatorReadinessRead",
        "AdminOverviewRead",
        "AdminValidationErrorResponse",
        "CapacityBlocker",
        "DownstreamScope",
        "DownstreamTokenIssueRequest",
        "DownstreamTokenIssued",
        "DownstreamTokenListResponse",
        "DownstreamTokenRead",
        "EventOutcome",
        "EventType",
        "HealthState",
        "LastStatusClass",
        "LedgerStatus",
        "OverviewStatus",
        "ProbeStatus",
        "ReadinessCause",
        "RuntimeState",
        "UpstreamKeyCreateRequest",
        "UpstreamKeyListResponse",
        "UpstreamKeyRead",
        "UpstreamProbeResponse",
        "UpstreamRoutingState",
    }

    # When: consumers inspect the typed administration seam.
    exported = set(public_exports)

    # Then: only the approved public DTO names are exported.
    assert exported == expected
