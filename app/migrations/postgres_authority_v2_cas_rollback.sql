-- Safe incremental rollback: caller must first prove there are no v2 objects/receipts.
DROP FUNCTION IF EXISTS __AUTHORITY_SCHEMA__.reconcile_cas_publication_v2(TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT);
DROP FUNCTION IF EXISTS __AUTHORITY_SCHEMA__.publish_fenced_cas_v2(UUID,BYTEA);
DROP FUNCTION IF EXISTS __AUTHORITY_SCHEMA__.issue_cas_attestation_v2(JSONB);
DROP TABLE IF EXISTS __AUTHORITY_SCHEMA__.cas_publication_receipts_v2;
DROP TABLE IF EXISTS __AUTHORITY_SCHEMA__.cas_attestations_v2;
DROP TABLE IF EXISTS __AUTHORITY_SCHEMA__.cas_objects_v2;
DROP TABLE IF EXISTS __AUTHORITY_SCHEMA__.cas_readers_v2;
DROP TABLE IF EXISTS __AUTHORITY_SCHEMA__.cas_publishers_v2;
DROP TABLE IF EXISTS __AUTHORITY_SCHEMA__.cas_extension_meta_v2;
