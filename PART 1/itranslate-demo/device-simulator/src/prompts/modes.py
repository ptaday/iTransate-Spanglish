# Prompting configuration module — controls whether keyterm boosting is active.
#
# AssemblyAI V3 supports `keyterms_prompt`: a list of words/phrases that the
# model should weight more heavily during decoding. This is useful for product
# names, proper nouns, and domain-specific jargon that may otherwise be
# transcribed as phonetically similar common words (e.g. "AssemblyAI" → "assembly I").
#
# Important: `keyterms_prompt` and `prompt` are mutually exclusive in the V3 API.
# This codebase uses keyterms only — no free-text prompts.
#
# Modes:
#   DEFAULT — no boosting, standard transcription
#   CUSTOM  — caller-supplied list of keyterms passed via `--keyterms` CLI flag

from dataclasses import dataclass, field
from enum import Enum


class PromptMode(Enum):
    DEFAULT = "default"
    CUSTOM = "custom"


@dataclass
class PromptConfig:
    mode: PromptMode = PromptMode.DEFAULT
    custom_keyterms: list[str] = field(default_factory=list)

    def get_keyterms_prompt(self) -> list[str] | None:
        """Return the deduplicated keyterms list, or None if boosting is off."""
        if self.mode == PromptMode.DEFAULT or not self.custom_keyterms:
            return None
        # dict.fromkeys preserves insertion order while removing duplicates
        return list(dict.fromkeys(self.custom_keyterms))

    def get_description(self) -> str:
        if self.mode == PromptMode.DEFAULT:
            return "Standard transcription (no boosting)"
        keyterms = self.get_keyterms_prompt()
        count = len(keyterms) if keyterms else 0
        return f"Custom keyterms ({count} terms)"


def create_prompt_config(
    mode: str = "default",
    custom_keyterms: list[str] | None = None,
) -> PromptConfig:
    """Factory used by main.py to construct the config from CLI arguments."""
    return PromptConfig(
        mode=PromptMode(mode),
        custom_keyterms=custom_keyterms or [],
    )
