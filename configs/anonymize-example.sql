-- Anonymization script that runs on every clone created from the main replica.
-- Copy this file to configs/anonymize.sql and customize for your data model:
--   cp configs/anonymize-example.sql configs/anonymize.sql
--
-- NOTE: this script runs ONLY when a clone is spawned from the live main
-- replica, NOT when cloning from an existing snapshot. If you need
-- anonymization on snapshot-derived clones, run it manually after the clone
-- comes up.
--
-- Keep this file in source control (it's a contract for what gets scrubbed).
-- Keep the real configs/anonymize.sql gitignored if it contains sensitive
-- internal field names or hashed credentials you don't want public.

\set ON_ERROR_STOP on

-- ─── Authentication ──────────────────────────────────────────────
-- Replace all real password hashes with a known dev value so anyone testing
-- the clone can log in. The example below is bcrypt of the literal string
-- "asdf1234!" — replace with your own dev hash.
-- UPDATE auth_user_login
--    SET password_hash = '$2a$12$cR7ABtNInV5UxTNk2zK2Jeo0i3LfyThJrTslnOuiKv0tGEVJNk9Km';

-- ─── Personally identifiable info ────────────────────────────────
-- Mask email addresses so dev-side sends don't reach real users.
-- UPDATE user_account
--    SET email = 'dev+' || id || '@example.local';

-- Mask phone numbers.
-- UPDATE user_account
--    SET phone = '+10000000000';

-- Mask names if your model includes them and dev shouldn't see real values.
-- UPDATE user_account
--    SET full_name = 'Dev User ' || id;

-- ─── Push / device identifiers ───────────────────────────────────
-- Invalidate push tokens so no real device receives dev notifications.
-- UPDATE device_info
--    SET push_token = 'invalid-dev-token', is_push_enabled = false;

-- ─── Payment / financial ─────────────────────────────────────────
-- Wipe payment tokens and PG provider artifacts.
-- UPDATE payment_method
--    SET provider_token = NULL,
--        card_last4 = '0000',
--        billing_address = NULL;

-- UPDATE invoice
--    SET external_merchant_uid = concat('fake_', right(random()::text, 12)),
--        external_payment_uid  = concat('fake_', right(random()::text, 12));

-- ─── Business / org data (if applicable) ─────────────────────────
-- Mask business contact info for B2B accounts.
-- UPDATE organization
--    SET contact_email = 'dev+' || id || '@example.local',
--        contact_phone = '+10000000000';

-- ─── Disable constraints that block dev experimentation ──────────
-- Remove environment-only check constraints (e.g. ones that block dev
-- buckets in asset URLs).
-- ALTER TABLE asset DROP CONSTRAINT IF EXISTS asset_no_dev_bucket_check;
