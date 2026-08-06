-- Per-unit publish outbox for topic publication.
--
-- The publisher previously recorded results only AFTER sending the entire
-- sequence; a crash between Discord accepting a message and the final update
-- caused silent loss. This outbox persists each publish unit's status as it
-- is sent, so a retry reconciles and resends only pending/failed units.

CREATE TABLE IF NOT EXISTS public.topic_publish_outbox (
    topic_id uuid NOT NULL REFERENCES public.topics(topic_id) ON DELETE CASCADE,
    unit_index integer NOT NULL,
    unit jsonb NOT NULL,
    send_kind text NOT NULL,
    status text NOT NULL DEFAULT 'pending',   -- pending | sending | sent | failed
    discord_message_id bigint,
    error text,
    environment text NOT NULL DEFAULT 'prod',
    run_id uuid REFERENCES public.topic_editor_runs(run_id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
    updated_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
    PRIMARY KEY (topic_id, unit_index),
    CONSTRAINT topic_publish_outbox_environment_check
        CHECK (environment IN ('prod', 'dev')),
    CONSTRAINT topic_publish_outbox_status_check
        CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
    CONSTRAINT topic_publish_outbox_unit_object_check
        CHECK (jsonb_typeof(unit) = 'object')
);

CREATE INDEX IF NOT EXISTS topic_publish_outbox_env_status_idx
    ON public.topic_publish_outbox (environment, status);

DROP TRIGGER IF EXISTS topic_publish_outbox_set_updated_at ON public.topic_publish_outbox;
CREATE TRIGGER topic_publish_outbox_set_updated_at
BEFORE UPDATE ON public.topic_publish_outbox
FOR EACH ROW
EXECUTE FUNCTION public.set_topic_editor_updated_at();
