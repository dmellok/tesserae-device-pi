"""OTA rollback guard: run by systemd before every start (ExecStartPre).

Standalone on purpose: standard library only, and run as a *copy* that the
previous, known-good version wrote next to the pending-update record
(``ota-pending.json`` in the cache dir), so an update that breaks the
package cannot also break the thing that undoes it.

While an update is pending (installed, not yet confirmed by a healthy
round-trip with the server), each start counts as a boot. A version that
never gets as far as confirming is restarted by systemd again and again;
once that has happened ``MAX_BOOTS`` times, this reinstalls what was there
before, records ``rolled_back`` / ``boot_failed`` where the client's OTA
reporter reads its state, and clears the record. No record, nothing to do.

    python ota-guard.py ~/.cache/tesserae-pi-client/ota-pending.json
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

#: Starts allowed without confirming before the previous version comes back.
MAX_BOOTS = 3


def guard(pending_path: Path) -> str:
    """One pre-start check. Returns what it did: none, counted, rolled_back."""
    try:
        record = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "none"
    if not isinstance(record, dict):
        return "none"
    boots = int(record.get("boots") or 0) + 1
    if boots <= MAX_BOOTS:
        record["boots"] = boots
        pending_path.write_text(json.dumps(record), encoding="utf-8")
        return "counted"

    previous = [str(arg) for arg in record.get("previous") or []]
    if previous:
        subprocess.run(
            [str(record["python"]), "-m", "pip", "install", "--no-deps", "--force-reinstall"]
            + previous,
            check=False,
        )
    state = {
        "phase": "rolled_back",
        "reason": "boot_failed",
        "target_fw": str(record.get("target") or ""),
        "attempt_id": str(record.get("attempt_id") or ""),
    }
    state_path = record.get("state_path")
    if state_path:
        try:
            Path(state_path).write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            pass
    pending_path.unlink(missing_ok=True)
    return "rolled_back"


if __name__ == "__main__":  # pragma: no cover - exercised through guard()
    if len(sys.argv) == 2:
        print(f"ota-guard: {guard(Path(sys.argv[1]))}")
    sys.exit(0)
