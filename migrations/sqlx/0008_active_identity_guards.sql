-- Close the in-process-only uniqueness window when more than one gateway
-- process shares PostgreSQL. Retired credentials may remain for audit history;
-- active identities must still be unique at the database boundary.
CREATE UNIQUE INDEX IF NOT EXISTS upstream_keys_active_fingerprint_idx
    ON nblb.upstream_keys (fingerprint)
    WHERE retired = false;

CREATE UNIQUE INDEX IF NOT EXISTS upstream_keys_active_label_idx
    ON nblb.upstream_keys (label)
    WHERE retired = false;

CREATE UNIQUE INDEX IF NOT EXISTS downstream_credentials_active_label_idx
    ON nblb.downstream_credentials (label)
    WHERE active = true;
