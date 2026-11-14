"""Choose which pending files go on the next disc."""

BLOCK = 2048  # UDF sector size


def space_on_disc(size):
    """Bytes a file really takes on a UDF disc: data rounded up to a sector, plus a file-entry sector."""
    return -(-size // BLOCK) * BLOCK + BLOCK


def plan(pending, usable_bytes):
    """First-fit in path order. Returns (chosen, bytes_used, oversized)."""
    chosen, used, oversized = [], 0, []
    for f in pending:
        need = space_on_disc(f["size"])
        if need > usable_bytes:
            oversized.append(f)
        elif used + need <= usable_bytes:
            chosen.append(f)
            used += need
    return chosen, used, oversized
