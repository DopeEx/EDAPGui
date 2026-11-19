"""Conservative progress tracking after losing a previously acquired supercruise target."""

from typing import Literal

RecoveryAction = Literal["SetSpeed25", "SetSpeedZero", "recovered", "stop"]


class ScLoopRecovery:
    def __init__(self):
        self.stage = 0
        self.reduced_at: float | None = None
        self.previous: tuple[float, bool, float] | None = None
        self.best_error = 180.0
        self.progress_at = 0.0
        self.samples = 0
        self.corrections = 0
        self.centered_at: float | None = None

    def observe(self, off: dict[str, float] | None, now: float, *, corrected: bool = False) -> RecoveryAction | None:
        if off is None:
            self.previous = None
            self.centered_at = None
            return None

        error = max(abs(off["yaw"]), abs(off["pit"]))
        ahead = off["z"] > 0
        consistent = (
            self.previous is not None
            and now - self.previous[2] <= 15
            and ahead == self.previous[1]
            and abs(error - self.previous[0]) <= 30
        )
        self.previous = error, ahead, now
        if not consistent:
            self.best_error = error
            self.progress_at = now
            self.samples = self.corrections = 0
            self.centered_at = None
            return None

        self.samples += 1
        self.corrections += int(corrected)
        if ahead and error < 10:
            self.progress_at = now
            self.best_error = error
            self.corrections = 0
            if self.centered_at is None:
                self.centered_at = now
            if now - self.centered_at >= (10 if self.stage else 3):
                return "recovered"
            return None
        self.centered_at = None

        # Ignore jitter: only a cumulative improvement of ten degrees renews the progress window.
        if error <= self.best_error - 10:
            self.best_error = error
            self.progress_at = now
            self.samples = self.corrections = 0
        stalled = now - self.progress_at
        if self.samples < 3 or self.corrections < 3:
            return None
        if self.stage == 0 and stalled >= 15:
            self.stage = 1
            self.reduced_at = now
            action: RecoveryAction = "SetSpeed25"
        elif self.stage == 1 and stalled >= 10:
            self.stage = 2
            action = "SetSpeedZero"
        elif self.stage == 2 and (
            stalled >= 30 or (self.reduced_at is not None and now - self.reduced_at >= 60 and stalled >= 15)
        ):
            return "stop"
        else:
            return None
        self.best_error = error
        self.progress_at = now
        self.samples = self.corrections = 0
        return action
