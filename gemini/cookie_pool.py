"""
Multi-cookie pool for Gemini (up to 10 slots).

Each slot is a single ``slot_<N>.txt`` file under ``COOKIES_DIR`` containing
either:

* the raw ``Cookie:`` header (``key=value; key=value; …``)
* or a JSON array exported from a "Cookie-Editor"-style browser extension.

The pool round-robins between healthy slots on every Gemini request so a
single account doesn't hit quota / rate-limit walls.  When a slot returns
an authentication failure (`AuthCookieError`) it's marked sick for one
hour and skipped — once every slot has been tried, the sickness is
ignored as a last-resort fallback.

Slot management is exposed over the ``/admin/cookies`` HTTP routes (and
mirrored as ``/cookie`` chat commands the developer can run from
WhatsApp), so the operator never has to redeploy to rotate credentials.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MAX_SLOTS = 10
SICK_PENALTY_SECS = 60 * 60  # mark a failing slot sick for one hour


def _normalize_cookie(raw: str) -> str:
    """Accept either a raw ``Cookie:`` header or a JSON cookie-export and
    return a plain ``key=value; key=value; …`` string."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            return "; ".join(f"{c['name']}={c['value']}" for c in data)
        except Exception:
            pass
    return raw


class CookiePool:
    def __init__(self, root_dir: str):
        self.root = Path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cursor = -1
        self._sick_until: Dict[int, float] = {}

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------
    def _slot_path(self, slot: int) -> Path:
        return self.root / f"slot_{slot}.txt"

    def _occupied_slots(self) -> List[int]:
        return sorted(s for s in range(1, MAX_SLOTS + 1)
                      if self._slot_path(s).exists())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def count(self) -> int:
        return len(self._occupied_slots())

    def has_any(self) -> bool:
        return self.count() > 0

    def get(self, slot: int) -> Optional[str]:
        p = self._slot_path(slot)
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8").strip()

    def list(self) -> List[Dict]:
        now = time.time()
        out: List[Dict] = []
        for slot in self._occupied_slots():
            raw = self.get(slot) or ""
            sick = self._sick_until.get(slot, 0)
            preview = raw[:24] + ("…" if len(raw) > 24 else "")
            out.append({
                "slot": slot,
                "size": len(raw),
                "preview": preview,
                "sick": sick > now,
                "sick_remaining_secs": max(0, int(sick - now)),
            })
        return out

    def add(self, raw: str, slot: Optional[int] = None) -> int:
        """Save ``raw`` into ``slot``.  If ``slot`` is None, pick the
        first free slot.  Raises ``ValueError`` when full or invalid."""
        cookie = _normalize_cookie(raw)
        if not cookie:
            raise ValueError("empty cookie")
        with self._lock:
            if slot is None:
                for s in range(1, MAX_SLOTS + 1):
                    if not self._slot_path(s).exists():
                        slot = s
                        break
                if slot is None:
                    raise ValueError(
                        f"all {MAX_SLOTS} slots are full — remove one first"
                    )
            if not (1 <= slot <= MAX_SLOTS):
                raise ValueError(f"slot must be between 1 and {MAX_SLOTS}")
            self._slot_path(slot).write_text(cookie, encoding="utf-8")
            self._sick_until.pop(slot, None)
            return slot

    def remove(self, slot: int) -> bool:
        p = self._slot_path(slot)
        if not p.exists():
            return False
        p.unlink()
        with self._lock:
            self._sick_until.pop(slot, None)
        return True

    def pick(self) -> Optional[Tuple[int, str]]:
        """Round-robin pick of a healthy cookie.

        Returns ``(slot, cookie_header)`` or ``None`` when the pool is
        empty.  When every slot is sick, sickness is ignored so we still
        attempt the call instead of hard-failing the user."""
        with self._lock:
            occupied = self._occupied_slots()
            if not occupied:
                return None
            now = time.time()
            healthy = [s for s in occupied if self._sick_until.get(s, 0) <= now]
            pool = healthy or occupied
            self._cursor = (self._cursor + 1) % len(pool)
            slot = pool[self._cursor]
            return slot, self.get(slot) or ""

    def mark_bad(self, slot: int) -> None:
        with self._lock:
            self._sick_until[slot] = time.time() + SICK_PENALTY_SECS

    def mark_good(self, slot: int) -> None:
        with self._lock:
            self._sick_until.pop(slot, None)

    # ------------------------------------------------------------------
    # Migration helper
    # ------------------------------------------------------------------
    def import_legacy_file(self, legacy_path: str) -> Optional[int]:
        """If a legacy single ``cookies.txt`` exists and the pool is
        empty, import it into slot 1."""
        if self.has_any():
            return None
        p = Path(legacy_path)
        if not p.exists():
            return None
        try:
            raw = p.read_text(encoding="utf-8")
            return self.add(raw, slot=1)
        except Exception:
            return None
