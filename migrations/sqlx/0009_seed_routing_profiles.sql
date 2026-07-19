-- Every advertised profile needs a cursor row before request_attempts can be
-- inserted. This also repairs installations upgraded from the early Rust
-- schema where the profile rows were created lazily by the first mutation.
INSERT INTO nblb.routing_state (profile_id, next_slot, generation)
VALUES
    ('z-ai/glm-5.2', 1, 0),
    ('microsoft/phi-4-multimodal-instruct', 1, 0),
    ('nvidia/vila', 1, 0),
    ('nvidia/nvclip', 1, 0),
    ('black-forest-labs/flux.1-kontext-dev', 1, 0),
    ('stabilityai/stable-video-diffusion', 1, 0),
    ('nvidia/magpie-tts-multilingual', 1, 0),
    ('nvidia/parakeet-ctc-1.1b', 1, 0)
ON CONFLICT (profile_id) DO NOTHING;
