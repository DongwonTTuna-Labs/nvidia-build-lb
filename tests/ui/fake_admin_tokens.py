from uuid import UUID

from nvidia_build_lb.admin.schemas import (
    DownstreamTokenIssued,
    DownstreamTokenIssueRequest,
    DownstreamTokenRead,
    EventOutcome,
    EventType,
)

from .fake_admin_models import BASE_TIME, FakeAdminData, FakeAdminError, FakeEventSpec, fake_uuid


def issue_token(
    data: FakeAdminData,
    request: DownstreamTokenIssueRequest,
    issued_bearer: str,
) -> DownstreamTokenIssued:
    if any(item.label == request.label for item in data.tokens):
        raise FakeAdminError(409, "resource_conflict", "downstream label already exists")
    item = DownstreamTokenRead(
        id=fake_uuid(data.token_sequence),
        label=request.label,
        scopes=request.scopes,
        revoked_at=None,
        request_count=0,
        last_used_at=None,
        created_at=BASE_TIME,
    )
    data.token_sequence += 1
    data.tokens.append(item)
    data.events.insert(
        0,
        data.record_event(
            FakeEventSpec(
                EventType.DOWNSTREAM_ISSUED,
                EventOutcome.SUCCEEDED,
                None,
                item.id,
            )
        ),
    )
    return DownstreamTokenIssued(
        id=item.id,
        label=item.label,
        scopes=item.scopes,
        token=issued_bearer,
        revoked_at=item.revoked_at,
        request_count=item.request_count,
        last_used_at=item.last_used_at,
        created_at=item.created_at,
    )


def revoke_token(data: FakeAdminData, item_id: UUID) -> None:
    index = next(
        (offset for offset, item in enumerate(data.tokens) if item.id == item_id),
        None,
    )
    if index is None or data.tokens[index].revoked_at is not None:
        raise FakeAdminError(404, "resource_not_found", "downstream token not found")
    item = data.tokens[index]
    data.tokens[index] = item.model_copy(update={"revoked_at": BASE_TIME})
    data.events.insert(
        0,
        data.record_event(
            FakeEventSpec(
                EventType.DOWNSTREAM_REVOKED,
                EventOutcome.SUCCEEDED,
                None,
                item.id,
            )
        ),
    )
