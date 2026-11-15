"""Bounded background recognition of the supercruise exit prompt."""

import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass

import cv2
import numpy as np

from core.logger import logger
from i18n.localization import normalize_text

DISENGAGE_SIMILARITY = 0.35
DISENGAGE_MAX_AGE = 5.0


@dataclass
class DisengageReading:
    similarity: float
    text: list[str] | None
    image: np.ndarray
    debug_images: dict[str, np.ndarray]
    readable: bool

    @property
    def matched(self) -> bool:
        return self.similarity > DISENGAGE_SIMILARITY


def read_disengage_prompt(ocr, messages, image_for_variant, cancelled=None):
    similarity = 0.0
    best_text = None
    debug_images = {}
    readable = False
    for variant in ({}, {"raw": True}, {"raw": True, "upscale": 2}):
        if cancelled is not None and cancelled.is_set():
            return None
        image = image_for_variant(**variant)
        texts = ocr.image_simple_ocr(image)
        if logger.isEnabledFor(logging.DEBUG):
            name = "masked" if not variant else ("raw-upscaled" if variant.get("upscale") else "raw")
            debug_images[name] = image.copy()
            readable = readable or any(text.strip() for text in (texts or []))
        if texts is None:
            logger.debug("Disengage OCR returned no text (%s).", variant or "masked")
            continue
        text = normalize_text(" ".join(texts))
        score = max(ocr.string_similarity(normalize_text(message), text) for message in messages)
        logger.debug("Disengage similarity with %s is %s (%s)", texts, score, variant or "masked")
        if score > similarity or best_text is None:
            similarity, best_text = score, texts
        if similarity > DISENGAGE_SIMILARITY:
            break
    return DisengageReading(similarity, best_text, image, debug_images, readable)


class DisengageWorker:
    """Keep at most one inference in flight, with no pending image queue."""

    def __init__(self):
        self._future: Future[DisengageReading | None] | None = None
        self._cancelled = threading.Event()
        self._target = None
        self._started = 0.0

    def invalidate(self):
        self._cancelled.set()

    def poll(self, target):
        if self._future is None:
            return None
        if target != self._target:
            self.invalidate()
        if not self._future.done():
            return None
        future, self._future = self._future, None
        result = future.result()
        age = time.monotonic() - self._started
        if self._cancelled.is_set() or (result is not None and result.matched and age > DISENGAGE_MAX_AGE):
            logger.debug("Discarding obsolete disengage OCR result (age %.2fs).", age)
            return None
        logger.debug("Disengage background OCR finished in %.2fs.", age)
        return result

    def submit(self, target, ocr, messages, masked, raw):
        if self._future is not None:
            return
        self._cancelled = threading.Event()
        cancelled = self._cancelled
        self._target = target
        self._started = time.monotonic()
        self._future = future = Future()
        masked, raw = masked.copy(), raw.copy()

        def image_for_variant(raw=False, upscale=1):
            image = images[raw]
            if upscale != 1:
                image = cv2.resize(image, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
            return image

        images = {False: masked, True: raw}

        def run():
            try:
                future.set_result(read_disengage_prompt(ocr, messages, image_for_variant, cancelled))
            except Exception as error:
                logger.exception("Background disengage OCR failed.")
                future.set_exception(error)

        threading.Thread(target=run, name="disengage-ocr", daemon=True).start()

    @property
    def busy(self):
        return self._future is not None
