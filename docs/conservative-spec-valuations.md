# Conservative specification conflicts (0.10.58)

When title and stored specifications contradict each other, sold valuation
evaluates the plausible CPU, RAM and storage alternatives and uses the lowest
supported estimate. Prices, rather than CPU rankings, determine the minimum.
Each plausible configuration must satisfy the existing evidence requirements;
an unsupported alternative prevents sold valuation and active-price fallback.

An exact title CPU can contradict an exact stored CPU. A generic contradictory
CPU family/generation is expanded only to exact CPUs found in same-model sold
evidence. No exact SKU is invented. Research queries include conflicting
configurations or the advertised generic CPU wording when evidence is missing.

Conflicting valuations have LOW confidence. Existing promotion thresholds
therefore exclude them from Telegram promotion. The basis records all assessed
configurations and the selected configuration, and the full-report evidence
uses that selected configuration. Original aspect RAM/storage values are
retained on future listing saves before title reconciliation. Values already
discarded by older versions cannot be reconstructed.

The local cache sweep also reevaluates existing conflicting valuations and
clears unsupported ones. Model precision, condition review, Windows 11 and
USB-C decisions continue to use their existing requirements; price conservatism
does not resolve those uncertainties.

Tests cover capacity combinations, lower prices on a nominally higher CPU,
generic family conflicts, missing alternative evidence, conflict research
queries, and clearing an unsupported existing valuation. No live research or
Telegram calls occur in these tests.
