"""Gestionnaire centralisé des timers (ADR-051).

Ce module fournit une abstraction pour la gestion des timers Home Assistant,
garantissant qu'un seul timer par clé est actif à tout moment et simplifiant
le nettoyage lors du déchargement.

ADR-051: Centralisation de la Gestion des Timers
- Élimination des race conditions (annulation automatique)
- Nettoyage robuste via cancel_all()
- Visibilité améliorée pour le debugging
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_track_point_in_time

if TYPE_CHECKING:
    from homeassistant.core import HassJob

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimerInfo:
    """Information sur un timer actif.

    Attributes:
        key: Identifiant unique du timer
        scheduled_time: Heure de déclenchement prévue
        callback_name: Nom de la fonction callback (pour le debugging)
        unsub: Fonction pour annuler le timer
    """

    key: str
    scheduled_time: datetime
    callback_name: str
    unsub: CALLBACK_TYPE


class TimerManager:
    """Gestionnaire centralisé des timers.

    Garantit qu'un seul timer par clé est actif à tout moment.
    L'appel à schedule() annule automatiquement tout timer existant
    pour la même clé avant d'en créer un nouveau.

    Example:
        ```python
        timer_manager = TimerManager(hass)

        # Planifier un timer (l'ancien est annulé automatiquement)
        timer_manager.schedule(
            TimerKey.RECOVERY_START,
            callback=self._on_recovery_start,
            target_time=target_datetime,
        )

        # Annuler un timer spécifique
        timer_manager.cancel(TimerKey.RECOVERY_START)

        # Annuler tous les timers (utile pour async_unload)
        timer_manager.cancel_all()
        ```
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise le gestionnaire de timers.

        Args:
            hass: Instance Home Assistant
        """
        self._hass = hass
        self._timers: dict[str, TimerInfo] = {}

    def schedule(
        self,
        key: str,
        callback: Callable[[datetime], None],
        target_time: datetime,
    ) -> None:
        """Planifie un timer, annulant tout timer existant pour cette clé.

        ADR-051: L'annulation automatique élimine les race conditions.
        Si schedule() est appelée deux fois avec la même clé, le premier
        timer est automatiquement annulé.

        Args:
            key: Identifiant unique du timer (utiliser TimerKey)
            callback: Fonction à appeler au déclenchement
            target_time: Heure de déclenchement (doit être timezone-aware)
        """
        # Annulation atomique de l'ancien timer
        self.cancel(key)

        # Création du nouveau timer
        unsub = async_track_point_in_time(
            self._hass,
            callback,
            target_time,
        )

        self._timers[key] = TimerInfo(
            key=key,
            scheduled_time=target_time,
            callback_name=callback.__name__,
            unsub=unsub,
        )

        _LOGGER.debug(
            "Timer '%s' programmé pour %s (callback: %s)",
            key,
            target_time.isoformat(),
            callback.__name__,
        )

    def cancel(self, key: str) -> bool:
        """Annule un timer par sa clé.

        Args:
            key: Identifiant du timer à annuler

        Returns:
            True si un timer a été annulé, False si aucun timer n'existait
        """
        timer_info = self._timers.pop(key, None)
        if timer_info:
            timer_info.unsub()
            _LOGGER.debug("Timer '%s' annulé", key)
            return True
        return False

    def cancel_all(self) -> int:
        """Annule tous les timers.

        Utilisé principalement lors du déchargement (async_unload).

        Returns:
            Nombre de timers annulés
        """
        count = len(self._timers)
        for timer_info in self._timers.values():
            timer_info.unsub()
        self._timers.clear()
        if count > 0:
            _LOGGER.debug("Tous les timers annulés (%d)", count)
        return count

    def is_active(self, key: str) -> bool:
        """Vérifie si un timer est actif.

        Args:
            key: Identifiant du timer

        Returns:
            True si le timer est actif
        """
        return key in self._timers

    def get_info(self, key: str) -> TimerInfo | None:
        """Retourne les informations d'un timer.

        Args:
            key: Identifiant du timer

        Returns:
            TimerInfo si le timer existe, None sinon
        """
        return self._timers.get(key)

    @property
    def active_timers(self) -> list[str]:
        """Liste des clés de timers actifs.

        Returns:
            Liste des identifiants de timers actifs
        """
        return list(self._timers.keys())

    @property
    def timer_count(self) -> int:
        """Nombre de timers actifs.

        Returns:
            Nombre de timers actifs
        """
        return len(self._timers)

    def get_diagnostics(self) -> dict:
        """Retourne l'état des timers pour le diagnostic.

        Returns:
            Dictionnaire avec les informations de tous les timers actifs
        """
        return {
            "active_count": self.timer_count,
            "timers": [
                {
                    "key": info.key,
                    "scheduled_time": info.scheduled_time.isoformat(),
                    "callback": info.callback_name,
                }
                for info in self._timers.values()
            ],
        }

    def __repr__(self) -> str:
        """Représentation string pour le debugging."""
        return f"TimerManager(active={self.active_timers})"
