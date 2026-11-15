"""Confirmed power distribution shared by travel and combat assists."""

from time import monotonic, sleep
from typing import Literal

from core.logger import logger

POWER_CONFIRM_TIMEOUT = 5.0
POWER_POLL_INTERVAL = 0.5


def _wait_for_power(status, expected: dict[str, float]) -> bool:
    deadline = monotonic() + POWER_CONFIRM_TIMEOUT
    observed = None
    while monotonic() < deadline:
        observed = status.get_cleaned_data(timeout=max(0.0, deadline - monotonic())).get("pips")
        if observed == expected:
            logger.debug("Power distribution confirmed: %s", observed)
            return True
        sleep(POWER_POLL_INTERVAL)
    logger.warning("Power distribution was not confirmed: expected=%s observed=%s", expected, observed)
    return False


def set_power_distribution(keys, status, priority: Literal["engine", "system"], *, skip_if_set: bool = False) -> bool:
    expected = (
        {"system": 2.0, "engine": 4.0, "weapons": 0.0}
        if priority == "engine"
        else {"system": 4.0, "engine": 2.0, "weapons": 0.0}
    )
    try:
        if skip_if_set and status.get_cleaned_data(timeout=POWER_CONFIRM_TIMEOUT).get("pips") == expected:
            logger.debug("Power distribution already set: %s", expected)
            return True
        logger.debug("Resetting power distribution: priority=%s", priority)
        keys.send("ResetPowerDistribution")
        if not _wait_for_power(status, {"system": 2.0, "engine": 2.0, "weapons": 2.0}):
            return False
        # Saturate the secondary capacitor first, then the priority capacitor, draining weapons.
        if priority == "engine":
            keys.send("IncreaseSystemsPower", repeat=4)
            keys.send("IncreaseEnginesPower", repeat=6)
        else:
            keys.send("IncreaseEnginesPower", repeat=4)
            keys.send("IncreaseSystemsPower", repeat=6)
        return _wait_for_power(status, expected)
    except TimeoutError:
        logger.warning("Status.json could not be read while confirming power distribution.")
        return False
