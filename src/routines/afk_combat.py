"""AFK combat in a resource extraction site (see Type 10 AFK videos).

Monitors the shields; if they are lost it boosts and supercruises away, drops out later,
sets pips for shield recovery and attempts an NPC fighter launch. Launches require journal
confirmation; hangar readiness is not checked. The ship must point away from the site at the start."""

from time import monotonic, sleep
from typing import Literal

from controls.power import set_power_distribution
from core.constants import (
    FlagsCargoScoopDeployed,
    FlagsDocked,
    FlagsFsdCharging,
    FlagsFsdCooldown,
    FlagsFsdJump,
    FlagsFsdMassLocked,
    FlagsHardpointsDeployed,
    FlagsInMainShip,
    FlagsLanded,
    FlagsLandingGearDown,
    FlagsSupercruise,
)
from core.logger import logger

SUPERCRUISE_ENTRY_TIMEOUT = 120.0
SUPERCRUISE_EXIT_TIMEOUT = 30.0
FIGHTER_LAUNCH_TIMEOUT = 30.0
POLL_INTERVAL = 0.5


class AfkCombat:
    def __init__(self, k, journal, status=None):
        self.k = k
        self.jn = journal
        self.fighter_bay = 2
        self.status = status
        self.failure_reason = None

    def _fail(self, message):
        self.failure_reason = message
        logger.warning("AFK combat: %s", message)
        return False

    def _flags(self, deadline=None):
        timeout = 5.0 if deadline is None else min(5.0, max(0.0, deadline - monotonic()))
        return self.status.get_cleaned_data(timeout=timeout)["Flags"]

    def _set_power(self, priority: Literal["engine", "system"]) -> bool:
        if set_power_distribution(self.k, self.status, priority):
            return True
        return self._fail("Power distribution was not confirmed; take control of the ship.")

    def check_shields_up(self, state=None):
        if state is None:
            state = self.jn.ship_state()
        return state["shieldsup"]

    def check_fighter_destroyed(self, state=None):
        if state is None:
            state = self.jn.ship_state()
        des = state["fighter_destroyed"]
        state["fighter_destroyed"] = False
        return des

    def evade(self):
        self.failure_reason = None
        if self.status is None:
            return self._fail("Status monitoring is unavailable; evasion was not started.")
        deadline = monotonic() + SUPERCRUISE_ENTRY_TIMEOUT
        flags = self._flags(deadline)
        if not flags & FlagsInMainShip or flags & (FlagsDocked | FlagsLanded | FlagsSupercruise):
            return self._fail("Evasion requires the main ship in normal space.")
        self.k.send("SetSpeed100", repeat=2)
        for flag, binding in (
            (FlagsHardpointsDeployed, "DeployHardpointToggle"),
            (FlagsLandingGearDown, "LandingGearToggle"),
            (FlagsCargoScoopDeployed, "ToggleCargoScoop"),
        ):
            if flags & flag:
                self.k.send(binding)
        if not self._set_power("engine"):
            return False
        self.k.send("UseBoostJuice")
        sleep(2)

        attempt = 0
        awaiting_charge = False
        next_attempt = monotonic()
        while monotonic() < deadline:
            flags = self._flags(deadline)
            if flags & FlagsSupercruise:
                logger.debug("AFK combat: supercruise entry confirmed")
                break
            if not flags & FlagsInMainShip or flags & (FlagsDocked | FlagsLanded):
                return self._fail("The main ship is no longer in flight; evasion stopped.")
            if awaiting_charge and flags & (FlagsFsdCharging | FlagsFsdJump):
                logger.debug("AFK combat: FSD charging/jump detected for supercruise attempt %s", attempt)
                awaiting_charge = False
            if monotonic() >= next_attempt:
                if not flags & (FlagsFsdCharging | FlagsFsdJump):
                    if awaiting_charge:
                        logger.debug(
                            "AFK combat: no FSD charging detected after supercruise attempt %s; "
                            "retry window reached, cause unknown",
                            attempt,
                        )
                        awaiting_charge = False
                    self.k.send("UseBoostJuice")
                    blocked = flags & (
                        FlagsFsdMassLocked
                        | FlagsFsdCooldown
                        | FlagsHardpointsDeployed
                        | FlagsLandingGearDown
                        | FlagsCargoScoopDeployed
                    )
                    if not blocked:
                        attempt += 1
                        logger.debug("AFK combat: supercruise attempt %s, flags=%s", attempt, flags)
                        self.k.send("Supercruise")
                        awaiting_charge = True
                    else:
                        logger.debug("AFK combat: boosting while supercruise is blocked, flags=%s", flags)
                next_attempt = monotonic() + 10.0
            sleep(POLL_INTERVAL)
        else:
            return self._fail("Supercruise entry was not confirmed within 120 seconds; take control of the ship.")

        sleep(1)
        self.k.send("SetSpeed100", repeat=2)
        sleep(20)
        self.k.send("SetSpeedZero")
        sleep(10)
        if not self._flags() & FlagsSupercruise:
            return self._fail("Supercruise ended before the planned exit; take control of the ship.")
        self.k.send("Supercruise")
        deadline = monotonic() + SUPERCRUISE_EXIT_TIMEOUT
        while monotonic() < deadline:
            flags = self._flags(deadline)
            if not flags & FlagsSupercruise and not flags & (FlagsFsdCharging | FlagsFsdJump):
                if not flags & FlagsInMainShip or flags & (FlagsDocked | FlagsLanded):
                    return self._fail("Normal-space flight was not confirmed after evasion.")
                logger.debug("AFK combat: normal-space exit confirmed")
                break
            sleep(POLL_INTERVAL)
        else:
            return self._fail("Supercruise exit was not confirmed within 30 seconds; take control of the ship.")
        return self._set_power("system")

    def launch_fighter(self):
        self.failure_reason = None
        if self.status is not None:
            flags = self._flags()
            if not flags & FlagsInMainShip or flags & (
                FlagsDocked | FlagsLanded | FlagsSupercruise | FlagsFsdCharging | FlagsFsdJump
            ):
                return self._fail("Fighter deployment requires the main ship in normal space.")
        cursor = self.jn.ship_state()["fighter_launch_sequence"]
        self.k.send("HeadLookReset")
        try:
            self.k.send("UIFocus", state=1)
            self.k.send("UI_Down")
        finally:
            self.k.send("UIFocus", state=0)
        sleep(0.2)

        self.k.send("UI_Left", repeat=2)
        self.k.send("UI_Up", repeat=3)
        sleep(0.1)

        self.k.send("UI_Down")
        self.k.send("UI_Right")
        sleep(0.1)

        self.k.send("UI_Up")

        # Menu rows alternate without checking hangar readiness.
        logger.debug("AFK combat: selecting fighter bay menu row %s", self.fighter_bay)
        if self.fighter_bay == 2:
            self.k.send("UI_Down")
            self.fighter_bay = 1
        else:
            self.fighter_bay = 2

        self.k.send("UI_Select")

        self.k.send("UI_Up", repeat=4)  # ensure top entry selected
        self.k.send("UI_Down")  # go down one and select the hired pilot
        self.k.send("UI_Select")

        sleep(0.2)
        self.k.send("UI_Back")
        self.k.send("HeadLookReset")
        self.k.send("SetSpeedZero")
        logger.debug("AFK combat: fighter deployment input sequence sent; waiting for LaunchFighter")
        deadline = monotonic() + FIGHTER_LAUNCH_TIMEOUT
        while monotonic() < deadline:
            state = self.jn.ship_state()
            if state["fighter_launch_sequence"] > cursor:
                if state["fighter_launch_player_controlled"] is not False:
                    return self._fail("The fighter launch was not confirmed as NPC-controlled; assist stopped.")
                logger.debug("AFK combat: NPC fighter launch confirmed by LaunchFighter")
                return True
            sleep(POLL_INTERVAL)
        return self._fail("NPC fighter launch was not confirmed within 30 seconds; assist stopped.")
