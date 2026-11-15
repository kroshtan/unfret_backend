"""
The product on the EU's Safety Gate: an alert by EU authorities about a dangerous non-food product.

Matches the page to the vendored Safety Gate index (:mod:`unfret.safety_gate`): its barcode exactly, or its brand and
a model number. A factual finding with the alert's reference, date, risk and public page; local, no lookups.
"""

import asyncio

from unfret import safety_gate
from unfret.graph.skills import Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState


class SafetyGateSkill(Skill):
    """The page's product in the EU Safety Gate alerts."""

    name = "safety_gate"
    description = "Checks whether the product is on the EU's Safety Gate list of dangerous products."
    features = ("barcode_match", "model_match")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Look the product up in the index.

        :param state: Current graph state; uses ``facts``.
        :return: The skill's features and findings.
        """
        index = await asyncio.to_thread(safety_gate.index)  # loaded once; the first load parses the file
        found = safety_gate.match(state["facts"], index)
        if found is None:
            return self.output({}, note=f"Not in the {index.alerts} Safety Gate alerts.")
        alert = found.alert
        what = ", ".join(p for p in (alert.product, alert.brand, alert.name) if p)[:150]
        risk = f"{alert.risk} ({alert.level.lower()})" if alert.level else alert.risk
        evidence = (
            f"Safety Gate alert {alert.case} of {alert.date}: {what}"
            + (f", model {alert.model[:60]}" if alert.model else "")
            + f"; risk: {risk[:80]}; matched on {found.on} {found.value}. {alert.url}"
        )
        return self.output(
            {"barcode_match": found.on == "barcode", "model_match": found.on == "model"},
            [Observation(type="safety_gate_alert", evidence=evidence)],
        )
