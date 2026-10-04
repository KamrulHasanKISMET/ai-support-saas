-- =====================================================================
-- LANGUAGE-AGNOSTIC SCHEMA — Phase 4 corrected scope (§9 item 3)
-- docs/GENERAL_LANGUAGE_BRAIN.md §5 / §5.2
--
-- Problem this fixes: `language_experiences.detected_language` was the
-- ONLY field capturing anything about script/writing-system, and it was
-- a closed enum ("bn" | "en" | "mixed" | "other") that collapsed four
-- independent concepts into one value:
--
--   1. what language the customer used                 → detected_language
--   2. what script/writing-system they wrote in        → NEW: script
--   3. whether it is a transliteration of a non-Latin  → NEW: is_transliterated
--      language (e.g. "amar parcel ta koi?" = Bangla
--      language, Latin script — not a different language)
--   4. how languages are mixed (none / sentence-level  → NEW: code_mixing
--      / within-sentence)
--
-- With the old schema, "price koto?" (Banglish) and "দাম কত?" (Bangla)
-- landed in different `detected_language` buckets ("mixed"/"bn") even
-- though they express the same customer goal and should produce the same
-- cluster learning signal. `script` + `is_transliterated` lets the
-- Brain map both to the same semantic representation without confusing
-- the writing system with the underlying language.
--
-- Additive only — `detected_language` is unchanged (left as-is; it is
-- still populated and still consumed by Language Engine / Kernel;
-- these new columns sit alongside it, not replacing it). No existing
-- row needs backfilling (every existing row defaults to the safe
-- fallback values; the columns' comments explain why those defaults
-- are the right ones for pre-migration rows).
--
-- Hard boundary (§5.3): none of these columns may be used to infer
-- nationality, ethnicity, identity, geography, or customer location.
-- This constraint is enforced by convention across every component
-- that reads them, not by a DB constraint (a DB cannot enforce a
-- semantic prohibition). It is stated here in the schema's canonical
-- location so it travels with the data definition wherever this file is
-- read.
-- =====================================================================

ALTER TABLE language_experiences
    ADD COLUMN IF NOT EXISTS script VARCHAR(20) NOT NULL DEFAULT 'latin',
        -- The writing system (script) actually used in the original message.
        -- Independent of the language itself:
        --   "latin"      -- Latin alphabet (covers English, Banglish,
        --                   romanized Arabic, Hinglish, etc.)
        --   "bengali"    -- Bengali/Bangla script (বাংলা)
        --   "devanagari" -- Devanagari (Hindi, Marathi, Nepali, etc.)
        --   "arabic"     -- Arabic script (Arabic, Urdu, Pashto, etc.)
        --   "cjk"        -- CJK unified ideographs (Chinese, Japanese,
        --                   Korean)
        --   "mixed"      -- Multiple scripts in the same message
        --                   (e.g. "আমার order ta কোথায়?" mixes Bengali
        --                   and Latin)
        --   "other"      -- Any other / unknown writing system
        --
        -- Default "latin": the vast majority of pre-migration rows are
        -- English or Banglish (both Latin script), so "latin" is the
        -- lowest-distortion default for backfill. Post-migration rows
        -- use the value Language Engine actually emits.

    ADD COLUMN IF NOT EXISTS is_transliterated BOOLEAN NOT NULL DEFAULT FALSE,
        -- TRUE when the message is written in Latin script but the
        -- underlying language is non-Latin. The canonical example in
        -- this codebase: "amar parcel ta koi?" is Bengali *language*
        -- written in Latin *script* — it is transliterated Bangla, not
        -- a distinct fourth language. is_transliterated=TRUE +
        -- script="latin" + detected_language="bn" is the correct
        -- triple for Banglish.
        --
        -- Why this matters: "price koto?" and "দাম কত?" carry identical
        -- customer goals and should map to the same intent cluster and
        -- the same learning signal. Without is_transliterated, the
        -- Brain sees them as unrelated surface forms.
        --
        -- Default FALSE: safe for pre-migration rows (most were pure
        -- English, genuinely not transliterated).

    ADD COLUMN IF NOT EXISTS transliterated_from VARCHAR(20),
        -- Only meaningful when is_transliterated=TRUE. The script the
        -- customer would normally use for this language — the LLM's
        -- best guess at the "native" script. Examples: "bengali" for
        -- Banglish, "devanagari" for romanized Hindi, "arabic" for
        -- romanized Arabic. NULL when is_transliterated=FALSE.
        -- Open string (not a closed enum) so new languages never
        -- require a migration. NULL default is correct for all
        -- pre-migration rows.

    ADD COLUMN IF NOT EXISTS code_mixing VARCHAR(20) NOT NULL DEFAULT 'none';
        -- Replaces the coarse binary `detected_language="mixed"` with
        -- the actual linguistic phenomenon:
        --
        --   "none"              -- single language throughout the message
        --   "inter_sentential"  -- different languages in different
        --                          sentences within the same message
        --   "intra_sentential"  -- language switch within a single
        --                          sentence (the common Bangla+English
        --                          case: "আমার order ta কোথায়?")
        --
        -- This matters because inter_sentential vs intra_sentential
        -- are linguistically different phenomena: intra_sentential
        -- (within-sentence code-switching) typically signals a
        -- domain-specific term from one language embedded in the
        -- grammar of another, whereas inter_sentential is closer to
        -- sequential bilingualism. Future cluster generalisation
        -- (§5.5) needs to distinguish them.
        --
        -- Default "none": safe for pre-migration rows. A monolingual
        -- "none" row is always less wrong than a misclassified "mixed"
        -- row when we have no actual signal.

-- Index: tenant + script lookup (cluster building may want to group
-- by script to evaluate whether script-stratified clusters outperform
-- script-agnostic ones in a future Phase 5/6 experiment).
CREATE INDEX IF NOT EXISTS idx_language_experiences_tenant_script
    ON language_experiences(tenant_id, script);

-- Index: transliteration flag — useful for finding all Banglish rows
-- for a tenant without scanning `script` + `detected_language` together.
CREATE INDEX IF NOT EXISTS idx_language_experiences_transliterated
    ON language_experiences(tenant_id, is_transliterated)
    WHERE is_transliterated = TRUE;
