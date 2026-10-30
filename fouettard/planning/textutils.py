"""Petites fonctions sur le texte français."""

import re
import unicodedata


def normalize(text: str) -> str:
    """Minuscules, sans accents, apostrophes droites, espaces simples."""
    text = unicodedata.normalize("NFKD", text.replace("’", "'"))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text).strip().lower()
