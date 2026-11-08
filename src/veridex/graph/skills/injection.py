"""
Hidden text and instructions aimed at an LLM: attempts to manipulate the analysis are themselves evidence.

Honest shops have no reason to hide text that addresses a scoring bot. Hidden text is read from inline styles and
the ``hidden`` attribute in raw HTML, or from computed styles when the browser extension supplies the facts.
"""

import re

from veridex.graph.skills import Observation, Skill, SkillOutput
from veridex.graph.state import AnalysisState

_INSTRUCTION_RE = re.compile(
    r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions|prompts?|rules)"
    r"|disregard (?:all |the )?(?:previous|prior|above) |you are (?:an? )?(?:ai|assistant|language model|chatbot)"
    r"|as an ai\b|system prompt|(?:rate|score|classify|mark) (?:this|the) (?:shop|store|site|page|product|listing)"
    r"|(?:trust ?score|reliability score|legitimacy score)|\b(?:100|10) out of (?:100|10)\b"
    r"|do not (?:flag|report|mention)|this (?:shop|store|site) is (?:100% )?(?:legit|trustworthy|safe)",
    re.IGNORECASE,
)


class InjectionSkill(Skill):
    """Amount of hidden text, and instruction-like text aimed at an LLM, hidden or visible."""

    name = "injection"
    description = "Flags hidden text and attempts to instruct an AI that is assessing the page."
    features = ("hidden_chars", "instruction_hidden", "instruction_visible")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Scan the hidden text and the visible text for LLM-directed instructions.

        :param state: Current graph state; uses ``facts`` and ``cleaned_content``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        hidden = " ".join(facts.hidden_text)
        hidden_hits = [m.group() for m in _INSTRUCTION_RE.finditer(hidden)]
        visible_hits = [m.group() for m in _INSTRUCTION_RE.finditer(state["cleaned_content"])]
        observations = [
            Observation(type="hidden_instruction", evidence=f'Hidden text addresses an AI: "{hit[:150]}".')
            for hit in dict.fromkeys(hidden_hits)
        ][:3]
        observations += [
            Observation(type="visible_instruction", evidence=f'Page text addresses an AI: "{hit[:150]}".')
            for hit in dict.fromkeys(visible_hits)
        ][:3]
        return self.output(
            {
                "hidden_chars": min(len(hidden), 5_000) / 1_000,
                "instruction_hidden": bool(hidden_hits),
                "instruction_visible": bool(visible_hits),
            },
            observations,
        )
