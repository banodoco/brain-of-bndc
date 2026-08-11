"""Idea-pattern exemplars for the live-update social proposal loop.

Unlike finished-post exemplars (which teach a model to *copy* a tweet),
each entry here teaches a **pattern**: given a kind of source material,
what theme to propose and — critically — what the media *should be*.

The media strategy is the load-bearing part. Two real Banodoco posts
contrast it directly:

* The MiniMax H3 thread compilation — 76 five-second clips, all local,
  by one creator → media strategy is "compile 60+ clips from the thread
  into a single montage". The range of styles IS the story.
* The Wan Animate 2 showcase — a handful of examples by Kijai → media
  strategy is "6+ example clips as a montage". Breadth of motion transfer
  styles is the story.

Same family of posts, different source material, different media scale.
The agent should learn to *count and characterize the source media* with
video understanding, then propose a strategy that fits what is actually
there — never just "attach the video".

Fields
------
pattern:      stable name of the idea pattern
archetype:    what kind of post this is (showcase / achievement / meme / …)
context:      source signals the agent should look for before proposing
theme:        the angle / headline proposed for the post
media_strategy: what the media SHOULD be, with source and scale
why:          why this lands (credit, stat, humor, restraint, …)
example_tweet: the real tweet this pattern produced (shown to the model)
media_description: short grounding description of the asset(s)
source_url:   human reference link — NEVER injected into prompts

Only ``pattern``, ``context``, ``theme``, ``media_strategy`` and ``why``
are rendered into the LLM prompt; the rest is reference material.
"""

from __future__ import annotations

from typing import Any, Dict, List


SOCIAL_IDEA_EXEMPLARS: List[Dict[str, Any]] = [
    {
        "pattern": "thread_to_compilation",
        "archetype": "community_achievement",
        "context": (
            "A thread (or long source window) from ONE creator with MANY "
            "short generations of the same model — often dozens of clips, "
            "all generated locally. Individual clips are too short to carry "
            "a post alone."
        ),
        "theme": (
            "Stat-driven achievement: the volume + the range of styles is "
            "the flex, with the creator credited."
        ),
        "media_strategy": (
            "COMPILE 60+ clips from the thread into a single montage reel. "
            "The range of animation styles across all clips is the story, "
            "not any one clip. Mention the clip count and the creator."
        ),
        "why": (
            "One creator, one model, all local — the breadth is the "
            "achievement. A compilation shows it in one watch."
        ),
        "example_tweet": (
            "76 five-second clips exploring different animation styles "
            "with MiniMax H3. All generated locally on a six-year-old GPU "
            "by the_shadow_nyc:"
        ),
        "media_description": (
            "Rapid succession of distinct animated segments (hand-drawn, "
            "stop-motion, 3D, motion graphics); each ~5s with its own "
            "sound; high energy, fast pacing."
        ),
        "source_url": "https://x.com/banodoco/status/2085089486032027790",
    },
    {
        "pattern": "showcase_montage",
        "archetype": "model_showcase",
        "context": (
            "A model feature / capability demo with a handful of strong "
            "examples (roughly 3–10 clips) from one or a few creators."
        ),
        "theme": (
            "One capability claim, then 'Examples by <creator>:' — the "
            "claim is short, the examples prove it."
        ),
        "media_strategy": (
            "MONTAGE of the 6+ example clips (or pick the strongest 6+) "
            "into one reel. When the source has fewer strong clips, keep "
            "them all; when it has many, cap the montage around 6–8 and "
            "pick the most stylistically varied. Credit the creator."
        ),
        "why": (
            "The claim needs proof, and the proof is variety — several "
            "examples side by side show the model's range better than one."
        ),
        "example_tweet": (
            "Wan Animate 2 allows for highly versatile motion transfer in "
            "a wide variety of styles. Examples by Kijai:"
        ),
        "media_description": (
            "Split-screen motion transfer across styles: live-action "
            "reference vs generated anime/cartoon character; pop vocal in "
            "anime segments; ~1:30."
        ),
        "source_url": "https://x.com/banodoco/status/2086115862138798302",
    },
    {
        "pattern": "technique_hook",
        "archetype": "experiment",
        "context": (
            "A novel technique, parameter choice, or workflow trick — "
            "'watch what happens when…'. Single clip is enough; the hook "
            "is the curiosity."
        ),
        "theme": (
            "Curiosity framing: 'see how it handles X' — a low-claim "
            "invitation, not a hype claim."
        ),
        "media_strategy": (
            "ONE clip — the single result that demonstrates the technique. "
            "No compilation; the experiment is the unit."
        ),
        "why": (
            "The technique post is about watching one specific outcome. "
            "Adding more clips dilutes the curiosity."
        ),
        "example_tweet": (
            "seitanism injecting frames into specific positions with h3 - "
            "see how it handles 1 frame every 1 second:"
        ),
        "media_description": (
            "Fast montage of natural/cosmic phenomena (eye, Earth, waves, "
            "jellyfish, black hole) with whooshing SFX synced to cuts; "
            "high production quality, ~10s."
        ),
        "source_url": "https://x.com/banodoco/status/2086894470176383288",
    },
    {
        "pattern": "meme_personification",
        "archetype": "humor",
        "context": (
            "A relatable failure/human moment — often personified (a GPU "
            "suffering, a model panicking). Humor IS the content."
        ),
        "theme": (
            "The joke, front and center: 'How your 6 y/o potato GPU reacts "
            "when you try to run Minimax'. Credit still included, in "
            "parentheses or after the joke."
        ),
        "media_strategy": (
            "ONE clip — the funny one. Keep the credit in the caption "
            "(gen'd by X)."
        ),
        "why": (
            "Meme formats get reach; the credit keeps it community-first. "
            "One clip carries the joke; a montage would kill it."
        ),
        "example_tweet": (
            "How your 6 y/o potato GPU reacts when you try to run Minimax "
            "(gen'd by _kruss_)"
        ),
        "media_description": (
            "Animated GPU character inside a PC case wakes up as the PC "
            "boots, then reacts with growing distress as the model runs; "
            "door creak, fans, GPU voice acting; ~15s."
        ),
        "source_url": "https://x.com/banodoco/status/2085728107751678141",
    },
    {
        "pattern": "restrained_observation",
        "archetype": "editorial_opinion",
        "context": (
            "A model / technique with an interesting trajectory — the post "
            "is an observation, not a demo. The clip is evidence for a "
            "larger point."
        ),
        "theme": (
            "Editorial take WITHOUT superlatives: 'The next generation of "
            "vid2vid will go to some magnificent, disturbing places'. "
            "Preserve conditions (local, model, creator)."
        ),
        "media_strategy": (
            "ONE clip as supporting evidence — the point is the observation, "
            "not the clip count. Credit the creator in parentheses."
        ),
        "why": (
            "Taste posts build the brand's editorial voice. No hype "
            "inflation; the clip proves the claim without narrating it."
        ),
        "example_tweet": (
            "The next generation of vid2vid will go to some magnificent, "
            "disturbing places (generated locally by Dawnll using Minimax M3)"
        ),
        "media_description": (
            "Split-screen K-pop dance vs 'Reference Replace' with "
            "Teletubbies doing the same choreography; upbeat track, clean "
            "sync; ~15s."
        ),
        "source_url": "https://x.com/banodoco/status/2084630805549027688",
    },
    {
        "pattern": "minimal_credit",
        "archetype": "showcase",
        "context": (
            "A single outstanding generation that speaks for itself. The "
            "work is the entire post."
        ),
        "theme": (
            "The credit line IS the tweet: '(gen by <creator>)'. No claim, "
            "no explanation."
        ),
        "media_strategy": (
            "ONE clip — the generation itself. Nothing else. The visual "
            "does the talking."
        ),
        "why": (
            "When the work is self-evidently good, an explanation would "
            "diminish it. Absolute minimalism."
        ),
        "example_tweet": "(gen by orabazes)",
        "media_description": (
            "Breaking Bad meme: Walter White explains a node graph, 'the "
            "seed must remain constant', Jesse randomized it — 'You did "
            "what?'; ~10s."
        ),
        "source_url": "https://x.com/banodoco/status/2086516883704913923",
    },
    {
        "pattern": "feature_demo_with_reference",
        "archetype": "model_showcase",
        "context": (
            "A feature demo built around a REFERENCE (inpainting on a "
            "reference image, style transfer from a reference, etc.). The "
            "before/after relationship is the content."
        ),
        "theme": (
            "Feature + reference framing: 'Inpainting based on a reference "
            "w/ Minimax H3. Examples by <creator>, feat. <collaborator>:'"
        ),
        "media_strategy": (
            "ONE clip showing the reference-to-result relationship, OR a "
            "short montage of 3–5 different references if the source has "
            "them. Credit creator AND any collaborator."
        ),
        "why": (
            "The reference relationship is the story — the post teaches "
            "what the feature does while showcasing it."
        ),
        "example_tweet": (
            "Inpainting based on a reference w/ Minimax H3. Examples by "
            "AbleJones, feat. @PurzBeats:"
        ),
        "media_description": (
            "LOTR scene with Frodo's face sequentially swapped to a "
            "cartoon, a bearded man, and a woman; orchestral score; "
            "steady, clean; ~20s."
        ),
        "source_url": "https://x.com/banodoco/status/2085448168246739016",
    },
]

# Patterns that should NOT be proposed — with the reason. Rendered so the
# agent can decline confidently instead of forcing a post.
SOCIAL_IDEA_NEGATIVES: List[Dict[str, Any]] = [
    {
        "pattern": "routine_chatter",
        "context": (
            "Casual discussion, questions, troubleshooting, or announcements "
            "with no visual payoff and no community achievement."
        ),
        "why": "Nothing to show — a text-only retread of Discord chatter is not a Banodoco post. Skip.",
    },
    {
        "pattern": "hype_without_proof",
        "context": (
            "An exciting claim with weak or unresolved media — no clip, "
            "a broken link, or media that could not be understood."
        ),
        "why": "Banodoco never posts hype without the work. Request review or skip.",
    },
    {
        "pattern": "duplicate_coverage",
        "context": (
            "The same model / creator / technique was already posted "
            "recently with no new angle or new examples."
        ),
        "why": "Duplicate coverage burns the audience. Skip unless there is a genuinely new angle.",
    },
]
