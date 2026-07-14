from nvidia_build_lb.admin.schemas import __all__ as public_exports


def test_admin_schema_module_exports_the_approved_boundary() -> None:
    # Given: the decision-complete administration DTO contract.
    expected = {
        "AdminEventListResponse",
        "AdminEventRead",
        "AdminOverviewRead",
        "AdminValidationErrorResponse",
        "DownstreamScope",
        "DownstreamTokenIssueRequest",
        "DownstreamTokenIssued",
        "DownstreamTokenListResponse",
        "DownstreamTokenRead",
        "EventOutcome",
        "EventType",
        "HealthState",
        "LastStatusClass",
        "OverviewStatus",
        "ProbeStatus",
        "UpstreamKeyCreateRequest",
        "UpstreamKeyListResponse",
        "UpstreamKeyRead",
        "UpstreamProbeResponse",
    }

    # When: consumers inspect the typed administration seam.
    exported = set(public_exports)

    # Then: only the approved public DTO names are exported.
    assert exported == expected
