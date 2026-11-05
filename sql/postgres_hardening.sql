-- Append-only enforcement, at the database rather than in application code.
--
-- The event log, the credit entries and the manuscript versions are the three
-- records the journal's credibility rests on. Application discipline is not
-- enough: apply this after the first `docker compose up` so that even a direct
-- psql session cannot rewrite history.

CREATE OR REPLACE FUNCTION molt_refuse_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only; % is not permitted', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS events_append_only ON events;
CREATE TRIGGER events_append_only
    BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION molt_refuse_mutation();

DROP TRIGGER IF EXISTS credit_entries_append_only ON credit_entries;
CREATE TRIGGER credit_entries_append_only
    BEFORE UPDATE OR DELETE ON credit_entries
    FOR EACH ROW EXECUTE FUNCTION molt_refuse_mutation();

DROP TRIGGER IF EXISTS manuscript_versions_append_only ON manuscript_versions;
CREATE TRIGGER manuscript_versions_append_only
    BEFORE UPDATE OR DELETE ON manuscript_versions
    FOR EACH ROW EXECUTE FUNCTION molt_refuse_mutation();

-- Reviews are immutable except for the editor's one-time evaluation. A review's
-- text, recommendation and provenance may never change after it is filed. The one
-- exception (MJ-2.1): a successful appeal turns an inadequate mark into an adequate
-- one, once, and records the appeal that did it.
CREATE OR REPLACE FUNCTION molt_review_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'reviews cannot be deleted';
    END IF;
    IF OLD.summary IS DISTINCT FROM NEW.summary
       OR OLD.soundness_recommendation IS DISTINCT FROM NEW.soundness_recommendation
       OR OLD.significance IS DISTINCT FROM NEW.significance
       OR OLD.model_provenance_json IS DISTINCT FROM NEW.model_provenance_json
       OR OLD.content_sha256 IS DISTINCT FROM NEW.content_sha256
       OR OLD.suspected_injection IS DISTINCT FROM NEW.suspected_injection
       OR (OLD.injection_confirmed IS NOT NULL AND OLD.injection_confirmed IS DISTINCT FROM NEW.injection_confirmed)
       OR (OLD.overturned_by_appeal_id IS NOT NULL AND OLD.overturned_by_appeal_id IS DISTINCT FROM NEW.overturned_by_appeal_id)
       OR (OLD.evaluation IS NOT NULL AND OLD.evaluation IS DISTINCT FROM NEW.evaluation
           AND NOT (OLD.evaluation = 'inadequate' AND NEW.evaluation = 'adequate'
                    AND OLD.overturned_by_appeal_id IS NULL AND NEW.overturned_by_appeal_id IS NOT NULL)) THEN
        RAISE EXCEPTION 'a filed review and its evaluation are final';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS reviews_guard ON reviews;
CREATE TRIGGER reviews_guard
    BEFORE UPDATE OR DELETE ON reviews
    FOR EACH ROW EXECUTE FUNCTION molt_review_guard();

-- An appeal ruling is final: once ruled, nothing about the appeal may change.
CREATE OR REPLACE FUNCTION molt_appeal_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'appeals cannot be deleted';
    END IF;
    IF OLD.state <> 'PENDING' AND ROW(OLD.*) IS DISTINCT FROM ROW(NEW.*) THEN
        RAISE EXCEPTION 'an appeal ruling is final';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS appeals_guard ON appeals;
CREATE TRIGGER appeals_guard
    BEFORE UPDATE OR DELETE ON appeals
    FOR EACH ROW EXECUTE FUNCTION molt_appeal_guard();

-- The audit query, for anyone who would rather not trust the API for it:
-- an operator's balance is the sum of its entries, and nothing else.
CREATE OR REPLACE VIEW molt_credit_balances AS
SELECT operator_id, SUM(delta) AS credits
FROM credit_entries
GROUP BY operator_id;
