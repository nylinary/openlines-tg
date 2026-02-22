"""Speech-to-text service — transcribes voice messages using OpenAI Whisper API.

Uses the same ``OPENAI_API_KEY`` as the LLM provider.  The Whisper API
accepts audio files (mp3, mp4, mpeg, mpga, m4a, wav, webm, ogg) up to 25 MB
and returns a text transcription.

Usage::

    stt = SpeechToText(api_key="sk-...", base_url=None)
    text = await stt.transcribe(audio_bytes, filename="voice.ogg")
    await stt.close()
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

log = logging.getLogger("app.speech")

OPENAI_TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"

# Audio MIME types we recognise as voice messages
VOICE_MIME_TYPES = frozenset({
    "audio/ogg",
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "audio/m4a",
    "audio/wav",
    "audio/x-wav",
    "audio/webm",
    "audio/x-m4a",
    "audio/aac",
    "audio/opus",
    "video/ogg",       # ogg container can be video/ogg with audio-only
    "application/ogg",
})

# File extensions that are likely voice/audio
VOICE_EXTENSIONS = frozenset({
    ".ogg", ".oga", ".mp3", ".wav", ".m4a", ".webm", ".opus",
    ".mp4", ".mpeg", ".mpga", ".aac", ".wma", ".flac",
})


class SpeechToTextError(RuntimeError):
    """Any STT failure."""
    pass


class SpeechToText:
    """OpenAI Whisper-based speech-to-text."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "whisper-1",
        base_url: Optional[str] = None,
        timeout_s: float = 60.0,
    ):
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for speech-to-text")
        self.api_key = api_key
        self.model = model
        self._url = base_url or OPENAI_TRANSCRIPTION_URL
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))

    async def close(self) -> None:
        await self._client.aclose()

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        filename: str = "voice.ogg",
        language: str = "ru",
    ) -> str:
        """Transcribe audio bytes to text via Whisper API.

        Args:
            audio_bytes: Raw audio file content.
            filename: Filename with extension (helps Whisper detect format).
            language: ISO-639-1 language code hint.

        Returns:
            Transcribed text string.

        Raises:
            SpeechToTextError: On any failure.
        """
        if not audio_bytes:
            raise SpeechToTextError("Empty audio data")

        if len(audio_bytes) > 25 * 1024 * 1024:
            raise SpeechToTextError(
                f"Audio file too large ({len(audio_bytes)} bytes, max 25 MB)"
            )

        log.info("stt_request", extra={
            "file_name": filename,
            "size_bytes": len(audio_bytes),
            "language": language,
            "model": self.model,
        })

        headers = {"Authorization": f"Bearer {self.api_key}"}

        files = {
            "file": (filename, audio_bytes),
        }
        form_data = {
            "model": self.model,
            "language": language,
            "response_format": "text",
        }

        try:
            r = await self._client.post(
                self._url,
                headers=headers,
                files=files,
                data=form_data,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            log.error("stt_network_error", extra={"error": str(e)})
            raise SpeechToTextError(f"Whisper API network error: {e}") from e

        if r.status_code != 200:
            detail = r.text[:500] if r.text else "no body"
            log.error("stt_api_error", extra={
                "status": r.status_code,
                "detail": detail,
            })
            raise SpeechToTextError(
                f"Whisper API error {r.status_code}: {detail}"
            )

        # response_format=text returns plain text
        text = r.text.strip()

        log.info("stt_response_ok", extra={
            "text_length": len(text),
            "text_preview": text[:100],
        })

        return text


def is_voice_file(*, mime_type: str = "", filename: str = "", viewer_type: str = "") -> bool:
    """Check if a file looks like a voice/audio message by MIME, extension, or Bitrix viewerType."""
    # Bitrix sends viewerAttrs.viewerType == "audio" for voice messages
    if viewer_type.lower().strip() == "audio":
        return True

    mime = mime_type.lower().strip()
    if mime and mime in VOICE_MIME_TYPES:
        return True
    if mime and mime.startswith("audio/"):
        return True

    name = filename.lower().strip()
    if name:
        for ext in VOICE_EXTENSIONS:
            if name.endswith(ext):
                return True
    return False
