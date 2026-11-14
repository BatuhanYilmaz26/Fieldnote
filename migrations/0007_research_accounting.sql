-- Research requests share monetary reservations without consuming model tokens.
ALTER TABLE runs ADD COLUMN research_call_count integer NOT NULL DEFAULT 0 CHECK (research_call_count >= 0);
ALTER TABLE provider_calls ADD COLUMN call_kind text NOT NULL DEFAULT 'model' CHECK (call_kind IN ('model','research'));
ALTER TABLE provider_calls ADD COLUMN reserved_urls integer NOT NULL DEFAULT 0 CHECK (reserved_urls BETWEEN 0 AND 8);
CREATE INDEX provider_calls_research_daily ON provider_calls(provider_scope,created_at) WHERE call_kind='research';
