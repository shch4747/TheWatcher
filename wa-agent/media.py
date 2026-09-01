"""
Media handling, kept behind an interface so the expensive/optional bits
(OCR, vision captioning, doc extraction, transcription) are pluggable and
mockable. Preprocessing depends only on `MediaProcessor`.

MVP policy (decided):
  - image     : OCR first (screenshots are the common case), fall back to a
                short caption. Produce a text stand-in either way.
  - voice     : NOT transcribed. Return None text; preprocessing marks it
                for a human TLDR only if it looks important.
  - document  : extract text if small, else just name it.
  - sticker/gif: dropped upstream, never reaches here.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class MediaProcessor(ABC):
    @abstractmethod
    def image_to_text(self, media_ref: Optional[str], caption: str) -> str:
        """OCR + optional caption -> text stand-in for an image."""

    @abstractmethod
    def document_to_text(self, media_ref: Optional[str], filename: Optional[str]) -> str:
        """Extract text from a document, or return a name-only placeholder."""

    @abstractmethod
    def transcribe_voice(self, media_ref: Optional[str]) -> Optional[str]:
        """Return transcript, or None if transcription is disabled (MVP)."""


class StubMediaProcessor(MediaProcessor):
    """No real OCR/vision/ASR — uses whatever caption text exists and
    otherwise emits clearly-marked placeholders. Swap for a real processor
    (e.g. tesseract/paddleocr + a vision model + whisper) later without
    touching preprocessing."""

    def image_to_text(self, media_ref, caption):
        caption = (caption or "").strip()
        if caption:
            return f"[image: {caption}]"
        return "[image: no text extracted]"

    def document_to_text(self, media_ref, filename):
        return f"[document: {filename or 'unnamed'}]"

    def transcribe_voice(self, media_ref):
        return None  # MVP: never transcribe
