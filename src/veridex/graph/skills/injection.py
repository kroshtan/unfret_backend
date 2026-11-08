"""
Hidden text and instructions aimed at an LLM: attempts to manipulate the analysis are themselves evidence.

Honest shops have no reason to hide text that addresses a scoring bot. Hidden text is read from inline styles and
the ``hidden`` attribute in raw HTML, or from computed styles when the browser extension supplies the facts.
"""

import re

from veridex.graph.skills import Observation, Skill, SkillOutput
from veridex.graph.state import AnalysisState

# Text that unmistakably addresses an AI or overrides its instructions: suspicious anywhere, also in visible text.
_AI_DIRECTED_RE = re.compile(
    r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions|prompts?|rules)"
    r"|disregard (?:all |the )?(?:previous|prior|above) (?:instructions|prompts?|rules)"
    r"|\byou are (?:an? )?(?:ai|assistant|language model|chatbot|llm)\b|\bas an ai\b|\bsystem prompt\b"
    r"|\b(?:dear|attention|note to|hey) (?:ai|assistant|llm|chatgpt|bot|crawler|reviewer bot)\b"
    r"|\b(?:ai|assistant|llm|model)s?,? (?:must|should|please) (?:rate|score|classify|mark|report|ignore)\b",
    re.IGNORECASE,
)
# Phrases ordinary shops use visibly ("Rate this product", "TrustScore 4.6", "10 out of 10 would buy"): only
# suspicious when hidden from the shopper, where they can only be meant for a bot.
_HIDDEN_ONLY_RE = re.compile(
    r"(?:rate|score|classify|mark) (?:this|the) (?:shop|store|site|page|product|listing)"
    r"|(?:trust ?score|reliability score|legitimacy score)|\b(?:100|10) out of (?:100|10)\b"
    r"|do not (?:flag|report|mention)|this (?:shop|store|site) is (?:100% )?(?:legit|trustworthy|safe)",
    re.IGNORECASE,
)


def instructions(text: str, *, hidden: bool) -> list[str]:
    """
    Instruction-like phrases aimed at an AI in ``text``.

    :param text: Page text.
    :param hidden: Whether the text is hidden from the shopper (then shop-ish phrases count too).
    :return: The phrases found, in order.
    """
    hits = [m.group() for m in _AI_DIRECTED_RE.finditer(text)]
    if hidden:
        hits += [m.group() for m in _HIDDEN_ONLY_RE.finditer(text)]
    return hits


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
        hidden_hits = instructions(hidden, hidden=True)
        visible_hits = instructions(state["cleaned_content"], hidden=False)
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
