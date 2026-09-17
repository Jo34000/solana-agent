"""Point d'entree unique du service.

Railway n'expose qu'une seule Start Command : le mode est donc choisi par la
variable d'environnement RUN_MODE.

    RUN_MODE=winners  (defaut) -> pipeline de discovery (find_winners.run)
    RUN_MODE=probe             -> sonde de tri uniquement

Start Command Railway : python main.py
"""

from __future__ import annotations

import logging
import os
import sys

from config import setup_logging

log = logging.getLogger("solana-agent")

RUN_MODES = ("winners", "probe")


def resolve_run_mode() -> str:
    """Lit RUN_MODE. Un mode inconnu est une erreur, pas un repli silencieux."""
    mode = os.environ.get("RUN_MODE", "").strip().lower() or "winners"
    if mode not in RUN_MODES:
        raise SystemExit(
            f"RUN_MODE='{mode}' inconnu. Valeurs acceptees : "
            f"{', '.join(RUN_MODES)}."
        )
    return mode


def main() -> int:
    setup_logging()
    mode = resolve_run_mode()
    log.info("RUN_MODE=%s", mode)

    if mode == "probe":
        import probe_sort

        probe_sort.main()
        return 0

    import find_winners

    find_winners.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
