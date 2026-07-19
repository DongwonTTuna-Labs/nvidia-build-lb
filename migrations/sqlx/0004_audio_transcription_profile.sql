-- Add the hosted Parakeet ASR profile used by the OpenAI-compatible
-- /v1/audio/transcriptions surface.  The constraint is recreated so existing
-- installations and fresh databases share the same routing contract.
ALTER TABLE nblb.routing_state
    DROP CONSTRAINT IF EXISTS routing_state_profile_id_check;

ALTER TABLE nblb.routing_state
    ADD CONSTRAINT routing_state_profile_id_check
    CHECK (profile_id IN (
        'z-ai/glm-5.2',
        'microsoft/phi-4-multimodal-instruct',
        'nvidia/vila',
        'nvidia/nvclip',
        'black-forest-labs/flux.1-kontext-dev',
        'stabilityai/stable-video-diffusion',
        'nvidia/magpie-tts-multilingual',
        'nvidia/parakeet-ctc-1.1b'
    ));
