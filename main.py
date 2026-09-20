"""Point d'entree unique du service.

Railway n'expose qu'une seule Start Command : le mode est donc choisi par la
variable d'environnement RUN_MODE.

    RUN_MODE=winners      (defaut) -> phase 1 : discovery des tokens winners
    RUN_MODE=discovery             -> phase 2 : early buyers des winners
    RUN_MODE=validation            -> phase 3 : backtest des wallets
    RUN_MODE=validation_v2         -> phase 3 bis : backtest prix d'entree reel
    RUN_MODE=validation_v3         -> phase 3 ter : PnL realise en SOL
    RUN_MODE=probe                 -> sonde de tri (CoinGecko)
    RUN_MODE=probe_helius          -> sonde Helius
    RUN_MODE=probe_transfers       -> sonde getTransfersByAddress

Start Command Railway : python main.py
"""

from __future__ import annotations

import logging
import os
import sys

from config import setup_logging

log = logging.getLogger("solana-agent")

RUN_MODES = (
    "winners", "discovery", "validation", "validation_v2", "validation_v3",
    "probe", "probe_helius", "probe_transfers",
)


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

    if mode == "validation_v3":
        import wallet_validation_v3

        wallet_validation_v3.run()
        return 0

    if mode == "validation_v2":
        import wallet_validation_v2

        wallet_validation_v2.run()
        return 0

    if mode == "validation":
        import wallet_validation

        wallet_validation.run()
        return 0

    if mode == "discovery":
        import wallet_discovery

        wallet_discovery.run()
        return 0

    if mode == "probe_transfers":
        import probe_transfers

        probe_transfers.main()
        return 0

    if mode == "probe_helius":
        import probe_helius

        probe_helius.main()
        return 0

    if mode == "probe":
        import probe_sort

        probe_sort.main()
        return 0

    import find_winners

    find_winners.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
