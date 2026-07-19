-- A credential is not routable merely because it was encrypted.  Persist the
-- successful provider probe receipt so a restart cannot bypass the
-- add -> probe -> enable safety boundary.
ALTER TABLE nblb.upstream_keys
    ADD COLUMN IF NOT EXISTS verified boolean NOT NULL DEFAULT false;
