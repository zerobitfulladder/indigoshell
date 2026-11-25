"""The agent's voices: their names and colours, the shell's.

Pure Python: the top bar draws a tab the agent holds in its voice's
colour (applink's `tabs` kind), and Flux draws the same colour in its
page (its frame round a page the agent drives), so both import this.
An app names the agent by voice id (`"agent": {"voice": "lilac", ...}`,
a `voice:lilac` colour role); a voice this table does not know is drawn
in the default voice's colour.
"""

# The hub's voices (its welcome names them): name and colour.
VOICES = {"lilac": ("Lilac", "#c8a2c8"), "indigo": ("Indigo", "#b967ff")}
DEFAULT_VOICE = "lilac"


def voice_id(voice) -> str:
    """The voice as messages carry it: its id, lowercase; the default
    voice's for none."""
    if isinstance(voice, str) and voice.strip():
        return voice.strip().lower()[:32]
    return DEFAULT_VOICE


def voice_name(voice) -> str:
    """"Lilac"; a voice not in VOICES by its own id, capitalized, when
    that is a plain word, else the default voice's name."""
    vid = voice_id(voice)
    if vid in VOICES:
        return VOICES[vid][0]
    if vid.replace("-", "").replace("_", "").isalnum() and len(vid) <= 24:
        return vid.capitalize()
    return VOICES[DEFAULT_VOICE][0]


def voice_color(voice) -> str:
    """"#c8a2c8"; the default voice's for one VOICES does not know."""
    return VOICES.get(voice_id(voice), VOICES[DEFAULT_VOICE])[1]


def voice_names() -> dict[str, str]:
    """Every voice's name by id: what the shell tells an app (`attached`)."""
    return {vid: name for vid, (name, _color) in VOICES.items()}
