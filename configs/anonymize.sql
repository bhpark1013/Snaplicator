-- Snaplicator anonymization script
-- This file runs inside the clone's Postgres container on every clone creation.
-- Use ON_ERROR_STOP to abort on any error.
\set ON_ERROR_STOP on

-- Example: obfuscate emails on a demo users table (adjust to your schema)
-- UPDATE users SET email = concat('user+', id, '@example.local');

-- Place your anonymization SQL below.

update users set name = 'anonymous' where name = 'apple'; 