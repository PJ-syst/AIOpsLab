# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Power-aware fault problems: outages, brownouts, load shedding,
generator cutover and UPS battery drain (all with battery backup)."""

from .power_base import PowerFaultBaseTask, POWER_LOCALIZATION_DESC
from .power_outage import (
    PowerOutageBaseTask,
    PowerOutageDetection,
    PowerOutageLocalization,
    PowerOutageAnalysis,
    PowerOutageMitigation,
)
from .power_brownout import (
    PowerBrownoutBaseTask,
    PowerBrownoutDetection,
    PowerBrownoutLocalization,
    PowerBrownoutAnalysis,
    PowerBrownoutMitigation,
)
from .power_load_shedding import (
    PowerLoadSheddingBaseTask,
    PowerLoadSheddingDetection,
    PowerLoadSheddingLocalization,
    PowerLoadSheddingAnalysis,
    PowerLoadSheddingMitigation,
)
from .power_generator_cutover import (
    PowerGeneratorCutoverBaseTask,
    PowerGeneratorCutoverDetection,
    PowerGeneratorCutoverLocalization,
    PowerGeneratorCutoverAnalysis,
    PowerGeneratorCutoverMitigation,
)
from .power_ups_drain import (
    PowerUpsDrainBaseTask,
    PowerUpsDrainDetection,
    PowerUpsDrainLocalization,
    PowerUpsDrainAnalysis,
    PowerUpsDrainMitigation,
)

__all__ = [
    "PowerFaultBaseTask",
    "POWER_LOCALIZATION_DESC",
    # Outage
    "PowerOutageBaseTask",
    "PowerOutageDetection",
    "PowerOutageLocalization",
    "PowerOutageAnalysis",
    "PowerOutageMitigation",
    # Brownout
    "PowerBrownoutBaseTask",
    "PowerBrownoutDetection",
    "PowerBrownoutLocalization",
    "PowerBrownoutAnalysis",
    "PowerBrownoutMitigation",
    # Load shedding
    "PowerLoadSheddingBaseTask",
    "PowerLoadSheddingDetection",
    "PowerLoadSheddingLocalization",
    "PowerLoadSheddingAnalysis",
    "PowerLoadSheddingMitigation",
    # Generator cutover
    "PowerGeneratorCutoverBaseTask",
    "PowerGeneratorCutoverDetection",
    "PowerGeneratorCutoverLocalization",
    "PowerGeneratorCutoverAnalysis",
    "PowerGeneratorCutoverMitigation",
    # UPS battery drain
    "PowerUpsDrainBaseTask",
    "PowerUpsDrainDetection",
    "PowerUpsDrainLocalization",
    "PowerUpsDrainAnalysis",
    "PowerUpsDrainMitigation",
]
