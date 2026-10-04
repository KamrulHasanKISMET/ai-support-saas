-- =====================================================================
-- OPEN LANGUAGE TAG — Phase 4 corrected scope, §5.2 row 1 follow-up
-- docs/GENERAL_LANGUAGE_BRAIN.md §5.1 / §5.2 / §9 item 3's note
--
-- Migration 016 (script/is_transliterated/transliterated_from/
-- code_mixing) deliberately left `detected_language` as-is: a closed
-- enum ("bn" | "en" | "mixed" | "other"). §5.1 names that enum itself
-- as "a ceiling" -- §5.2's corrected model calls for a genuinely open
-- language tag alongside it, never a fifth enum value bolted on.
--
-- This migration adds exactly that one remaining field:
--
--   language  -- open BCP-47-style tag ("bn", "en", "hi", "ar", "es",
--                ...). A comma-separated string (e.g. "bn,en") when the
--                message is genuinely code-mixed (see code_mixing) --
--                deliberately a scalar VARCHAR, not a new array/JSON
--                column, matching every other open-set field this
--                table already has (script, transliterated_from).
--
-- `detected_language` is UNCHANGED and stays populated exactly as
-- before -- this is additive alongside it, not a replacement (§5.2's
-- explicit instruction). No existing row needs backfilling: every
-- pre-migration row gets the honest default 'und' (ISO 639-2
-- "undetermined"), the same "we have no signal, say so plainly rather
-- than guessing" discipline code_mixing's 'none' default already
-- follows.
-- =====================================================================

ALTER TABLE language_experiences
    ADD COLUMN IF NOT EXISTS language VARCHAR(30) NOT NULL DEFAULT 'und';
        -- Open tag, never validated against a fixed set anywhere in the
        -- application (that closed-set validation is exactly what §5.1
        -- prohibits) -- only checked for being a non-empty string.
        -- 'und' = no signal / not yet captured (pre-migration rows,
        -- and any future row where the Language Engine's response
        -- omitted this field).

CREATE INDEX IF NOT EXISTS idx_language_experiences_tenant_language
    ON language_experiences(tenant_id, language);
