"""Sérialisation et désérialisation pour SmartHRT (ADR-041).

Ce module fournit un encodeur/décodeur centralisé pour les types
Python non-JSON-natifs (datetime, time, deque, StrEnum).

Utilise un format auto-descriptif avec __type__ pour identifier
le type lors de la désérialisation.
"""

from datetime import datetime, time as dt_time
from collections import deque
from enum import StrEnum
from typing import Any


class JSONEncoder:
    """Encodeurs/décodeurs pour types non-JSON-natifs."""

    @staticmethod
    def encode(value: Any) -> Any:
        """Encode une valeur Python en type JSON-compatible.

        Args:
            value: La valeur Python à encoder

        Returns:
            Une valeur JSON-compatible (dict avec __type__ pour types spéciaux)
        """
        if value is None:
            return None
        if isinstance(value, datetime):
            return {"__type__": "datetime", "value": value.isoformat()}
        if isinstance(value, dt_time):
            return {"__type__": "time", "value": value.isoformat()}
        if isinstance(value, deque):
            return {
                "__type__": "deque",
                "value": list(value),
                "maxlen": value.maxlen,
            }
        if isinstance(value, StrEnum):
            return {"__type__": "enum", "value": str(value)}
        # Types natifs JSON (int, float, str, bool, list, dict)
        return value

    @staticmethod
    def decode(data: Any, expected_type: type | None = None) -> Any:
        """Décode une valeur JSON en type Python.

        Args:
            data: La valeur JSON à décoder
            expected_type: Type attendu pour les enums (optionnel)

        Returns:
            La valeur Python décodée
        """
        if not isinstance(data, dict) or "__type__" not in data:
            return data

        type_name = data["__type__"]
        value = data["value"]

        if type_name == "datetime":
            return datetime.fromisoformat(value) if value else None
        if type_name == "time":
            return dt_time.fromisoformat(value) if value else None
        if type_name == "deque":
            maxlen = data.get("maxlen")
            return deque(value if value else [], maxlen=maxlen)
        if type_name == "enum" and expected_type:
            return expected_type(value) if value else None

        return value

    @staticmethod
    def decode_legacy(
        value: Any, field_type: str, expected_enum: type | None = None
    ) -> Any:
        """Décode une valeur au format legacy (PERSISTED_FIELDS).

        Utilisé pour la migration depuis l'ancien format de stockage.

        Args:
            value: La valeur stockée
            field_type: Type de champ ("datetime", "time", "list", "state", etc.)
            expected_enum: Classe enum attendue pour "state"

        Returns:
            La valeur Python décodée
        """
        if value is None:
            return None

        if field_type == "datetime":
            try:
                return datetime.fromisoformat(value)
            except (ValueError, TypeError):
                return None
        if field_type == "time":
            try:
                # Supporte HH:MM:SS et HH:MM
                return dt_time.fromisoformat(value)
            except (ValueError, TypeError):
                return None
        if field_type == "list":
            if isinstance(value, list):
                return deque(value, maxlen=50)  # ADR-037: 50 samples
            return deque(maxlen=50)
        if field_type == "state" and expected_enum:
            try:
                return expected_enum(value)
            except ValueError:
                return None

        # Types directs (float, bool, str)
        return value
