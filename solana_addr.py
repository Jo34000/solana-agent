"""Adresses Solana : base58 et derivation de PDA, sans dependance externe.

Le projet s'interdit toute dependance au-dela de requirements.txt : ni
solders ni solana-py. Les deux primitives necessaires sont donc
reimplementees ici.

Une PDA (Program Derived Address) est le sha256 de
    seeds || bump || program_id || "ProgramDerivedAddress"
pour le plus grand bump (255 -> 1) donnant un point HORS de la courbe
ed25519. C'est cette contrainte "hors courbe" qui garantit qu'aucune cle
privee ne peut signer pour cette adresse.
"""

from __future__ import annotations

import hashlib

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_INDEX = {char: index for index, char in enumerate(B58_ALPHABET)}

PDA_MARKER = b"ProgramDerivedAddress"

# Corps fini et constante d de la courbe ed25519.
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def b58encode(raw: bytes) -> str:
    """Encodage base58 Bitcoin, celui de Solana."""
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number > 0:
        number, remainder = divmod(number, 58)
        encoded = B58_ALPHABET[remainder] + encoded
    # Chaque octet nul de tete devient un '1'.
    leading = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * leading + (encoded or "")


def b58decode(text: str) -> bytes:
    """Decodage base58. Leve ValueError sur un caractere invalide."""
    number = 0
    for char in text:
        if char not in B58_INDEX:
            raise ValueError(f"caractere base58 invalide : {char!r}")
        number = number * 58 + B58_INDEX[char]
    leading = len(text) - len(text.lstrip("1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * leading + body


def is_on_curve(point: bytes) -> bool:
    """Le point compresse est-il sur la courbe ed25519 ?

    Decompression standard : y est lu en petit-boutiste, le bit de poids
    fort porte le signe de x. On resout x^2 = (y^2 - 1) / (d*y^2 + 1) et
    on verifie qu'une racine existe.
    """
    if len(point) != 32:
        return False
    number = int.from_bytes(point, "little")
    sign = (number >> 255) & 1
    y = number & ((1 << 255) - 1)
    if y >= _P:
        return False

    y2 = y * y % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    if v == 0:
        return False

    # x = u * v^3 * (u * v^7)^((p-5)/8)
    v3 = pow(v, 3, _P)
    v7 = pow(v, 7, _P)
    x = (u * v3 % _P) * pow(u * v7 % _P, (_P - 5) // 8, _P) % _P

    check = v * x % _P * x % _P
    if (check - u) % _P == 0:
        pass
    elif (check + u) % _P == 0:
        # racine a un facteur sqrt(-1) pres
        x = x * pow(2, (_P - 1) // 4, _P) % _P
    else:
        return False

    if x == 0 and sign:
        return False
    return True


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    """(adresse PDA en base58, bump). Leve si aucun bump ne convient.

    Le bump descend de 255 a 1, comme Pubkey::find_program_address : le
    premier point HORS courbe gagne, ce qui rend la derivation
    deterministe et donne le bump dit canonique.
    """
    program_bytes = b58decode(program_id)
    if len(program_bytes) != 32:
        raise ValueError(f"program_id invalide : {program_id}")
    for seed in seeds:
        if len(seed) > 32:
            raise ValueError(f"seed trop longue ({len(seed)} > 32)")

    prefix = b"".join(seeds)
    for bump in range(255, 0, -1):
        digest = hashlib.sha256(
            prefix + bytes([bump]) + program_bytes + PDA_MARKER
        ).digest()
        if not is_on_curve(digest):
            return b58encode(digest), bump
    raise ValueError("aucun bump ne donne une adresse hors courbe")


def bonding_curve_address(mint: str, program_id: str) -> tuple[str, int]:
    """PDA de la bonding curve pump.fun : seeds ["bonding-curve", mint]."""
    return find_program_address([b"bonding-curve", b58decode(mint)], program_id)
