#!/usr/bin/env python3
"""Page-level SSD simulator for greedy garbage collection.

The model follows Bux and Iliadis (2010):
  * out-of-place page updates,
  * one clean block reserved for garbage collection,
  * GC when the current write block is full,
  * the victim is a non-clean block with the fewest valid pages,
  * valid victim pages are copied to the clean block,
  * write amplification = (host page writes + copied page writes) / host page writes.

Only write amplification is printed by the CLI.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import random
import re
import sys
from array import array
from collections import deque
from pathlib import Path
from typing import Deque, Iterator, Optional, Sequence, TextIO

FREE = -1
INVALID = -2


class GreedyGCSimulator:
    """A compact page-mapped FTL simulator with greedy block GC."""

    def __init__(
        self,
        *,
        blocks: int,
        pages_per_block: int,
        user_blocks: int,
        seed: int = 1,
        gc_tie_break: str = "random",
    ) -> None:
        if blocks < 3:
            raise ValueError("blocks must be at least 3")
        if pages_per_block < 1:
            raise ValueError("pages_per_block must be positive")
        # One clean block is reserved. Requiring at least one additional block of
        # over-provisioning avoids the zero-free-page deadlock at user_blocks=b-1.
        if not 1 <= user_blocks <= blocks - 2:
            raise ValueError(
                "user_blocks must satisfy 1 <= user_blocks <= blocks - 2 "
                "(one clean block plus at least one block of over-provisioning)"
            )
        if gc_tie_break not in {"random", "lowest"}:
            raise ValueError("gc_tie_break must be 'random' or 'lowest'")

        self.block_count = blocks
        self.pages_per_block = pages_per_block
        self.user_blocks = user_blocks
        self.logical_pages = user_blocks * pages_per_block
        self.gc_tie_break = gc_tie_break
        self._tie_rng = random.Random(seed ^ 0x9E3779B97F4A7C15)

        self.pages: list[list[int]] = [
            [FREE] * pages_per_block for _ in range(blocks)
        ]
        self.valid_count: list[int] = [0] * blocks
        self.map_block: list[int] = [-1] * self.logical_pages
        self.map_page: list[int] = [-1] * self.logical_pages

        # buckets[v] contains every non-clean block having v valid pages.
        self.buckets: list[set[int]] = [
            set() for _ in range(pages_per_block + 1)
        ]

        self.clean_block = blocks - 1
        self.write_block = blocks - 2
        self.write_pos = pages_per_block

        self.host_page_writes = 0
        self.physical_page_writes = 0
        self.relocation_page_writes = 0

        self._precondition(seed)

    @property
    def occupancy(self) -> float:
        return self.user_blocks / self.block_count

    def _precondition(self, seed: int) -> None:
        """Fill every non-clean physical page without counting statistics.

        First, every logical page is written once. Remaining physical pages are
        filled with uniformly random updates. This creates exactly one clean
        block and a fully used data area before workload warm-up begins.
        """

        rng = random.Random(seed ^ 0xD1B54A32D192ED03)
        usable_physical_pages = (self.block_count - 1) * self.pages_per_block

        for physical_index in range(usable_physical_pages):
            if physical_index < self.logical_pages:
                lpn = physical_index
            else:
                lpn = rng.randrange(self.logical_pages)

            block = physical_index // self.pages_per_block
            page = physical_index % self.pages_per_block

            old_block = self.map_block[lpn]
            if old_block != -1:
                old_page = self.map_page[lpn]
                if self.pages[old_block][old_page] != lpn:
                    raise AssertionError("mapping corruption during preconditioning")
                self.pages[old_block][old_page] = INVALID
                self.valid_count[old_block] -= 1

            if self.pages[block][page] != FREE:
                raise AssertionError("preconditioning target is not free")
            self.pages[block][page] = lpn
            self.valid_count[block] += 1
            self.map_block[lpn] = block
            self.map_page[lpn] = page

        for block in range(self.block_count - 1):
            self.buckets[self.valid_count[block]].add(block)

        self.reset_statistics()

    def reset_statistics(self) -> None:
        self.host_page_writes = 0
        self.physical_page_writes = 0
        self.relocation_page_writes = 0

    def _move_bucket(self, block: int, old_count: int, new_count: int) -> None:
        if block == self.clean_block:
            raise AssertionError("clean block must not be in GC buckets")
        self.buckets[old_count].remove(block)
        self.buckets[new_count].add(block)

    def _invalidate(self, lpn: int) -> None:
        block = self.map_block[lpn]
        page = self.map_page[lpn]
        if block < 0 or self.pages[block][page] != lpn:
            raise AssertionError(f"invalid logical-to-physical mapping for LPN {lpn}")

        old_count = self.valid_count[block]
        self.pages[block][page] = INVALID
        self.valid_count[block] = old_count - 1
        self._move_bucket(block, old_count, old_count - 1)

    def _choose_victim(self) -> tuple[int, int]:
        for valid_pages, candidates in enumerate(self.buckets):
            if not candidates:
                continue
            if self.gc_tie_break == "lowest":
                return min(candidates), valid_pages
            # Converting only the tie set keeps the common path compact while
            # preserving unbiased tie breaking.
            return self._tie_rng.choice(tuple(candidates)), valid_pages
        raise RuntimeError("no garbage-collection victim is available")

    def _garbage_collect(self) -> None:
        victim, victim_valid = self._choose_victim()
        destination = self.clean_block

        if victim == destination:
            raise AssertionError("clean block selected as a victim")
        if self.valid_count[destination] != 0 or any(
            page != FREE for page in self.pages[destination]
        ):
            raise AssertionError("GC destination is not clean")
        if victim_valid >= self.pages_per_block:
            raise RuntimeError(
                "greedy GC reclaimed no free page; increase over-provisioning"
            )

        self.buckets[victim_valid].remove(victim)

        copied = 0
        for lpn in self.pages[victim]:
            if lpn < 0:
                continue
            self.pages[destination][copied] = lpn
            self.map_block[lpn] = destination
            self.map_page[lpn] = copied
            copied += 1

        if copied != victim_valid:
            raise AssertionError("valid-page count does not match victim contents")

        self.valid_count[destination] = copied
        self.pages[victim] = [FREE] * self.pages_per_block
        self.valid_count[victim] = 0

        # The old victim becomes the sole clean block. The old clean block now
        # contains the relocated pages and becomes the current write block.
        self.clean_block = victim
        self.write_block = destination
        self.write_pos = copied
        self.buckets[copied].add(destination)

        self.relocation_page_writes += copied
        self.physical_page_writes += copied

    def write_page(self, lpn: int) -> None:
        """Issue one host page update."""

        if not 0 <= lpn < self.logical_pages:
            raise ValueError(
                f"LPN {lpn} is outside [0, {self.logical_pages - 1}]"
            )

        if self.write_pos == self.pages_per_block:
            self._garbage_collect()

        # Out-of-place update: invalidate the old physical page, then program a
        # new page in the current write block.
        self._invalidate(lpn)

        block = self.write_block
        page = self.write_pos
        if self.pages[block][page] != FREE:
            raise AssertionError("current write position is not free")

        old_count = self.valid_count[block]
        self.pages[block][page] = lpn
        self.valid_count[block] = old_count + 1
        self.map_block[lpn] = block
        self.map_page[lpn] = page
        self._move_bucket(block, old_count, old_count + 1)
        self.write_pos += 1

        self.host_page_writes += 1
        self.physical_page_writes += 1

    def run(self, workload: Iterator[int], page_writes: int) -> None:
        if page_writes < 0:
            raise ValueError("page_writes must be non-negative")
        for completed in range(page_writes):
            try:
                lpn = next(workload)
            except StopIteration as exc:
                raise RuntimeError(
                    f"workload ended after {completed} of {page_writes} requested "
                    "host page writes; use a longer trace or --trace-repeat"
                ) from exc
            self.write_page(lpn)

    @property
    def write_amplification(self) -> float:
        if self.host_page_writes == 0:
            raise ZeroDivisionError("no measured host page writes")
        return self.physical_page_writes / self.host_page_writes

    def check_invariants(self) -> None:
        """Expensive consistency check intended for debugging and tests."""

        if sum(self.valid_count) != self.logical_pages:
            raise AssertionError("total number of valid pages changed")
        if self.valid_count[self.clean_block] != 0:
            raise AssertionError("clean block has valid pages")
        if any(page != FREE for page in self.pages[self.clean_block]):
            raise AssertionError("clean block contains programmed pages")

        membership = [0] * self.block_count
        for count, blocks in enumerate(self.buckets):
            for block in blocks:
                membership[block] += 1
                if block == self.clean_block:
                    raise AssertionError("clean block is in a bucket")
                if self.valid_count[block] != count:
                    raise AssertionError("block is in the wrong valid-count bucket")

        for block in range(self.block_count):
            expected = 0 if block == self.clean_block else 1
            if membership[block] != expected:
                raise AssertionError("block bucket membership is inconsistent")

        for lpn, (block, page) in enumerate(zip(self.map_block, self.map_page)):
            if block < 0 or page < 0 or self.pages[block][page] != lpn:
                raise AssertionError(f"mapping for LPN {lpn} is inconsistent")

        for block in range(self.block_count):
            actual = sum(1 for lpn in self.pages[block] if lpn >= 0)
            if actual != self.valid_count[block]:
                raise AssertionError("stored valid-page count is inconsistent")

        # Free pages are allowed only in the clean block and current write block.
        for block in range(self.block_count):
            free = sum(1 for page in self.pages[block] if page == FREE)
            if block == self.clean_block:
                if free != self.pages_per_block:
                    raise AssertionError("clean block is not fully free")
            elif block == self.write_block:
                if free != self.pages_per_block - self.write_pos:
                    raise AssertionError("write-block free-page count is inconsistent")
            elif free != 0:
                raise AssertionError("a closed block contains free pages")


class UniformWorkload(Iterator[int]):
    def __init__(self, logical_pages: int, seed: int) -> None:
        self.logical_pages = logical_pages
        self.rng = random.Random(seed)

    def __iter__(self) -> "UniformWorkload":
        return self

    def __next__(self) -> int:
        return self.rng.randrange(self.logical_pages)


class SequentialWorkload(Iterator[int]):
    def __init__(self, logical_pages: int, start_lpn: int = 0) -> None:
        self.logical_pages = logical_pages
        self.current = start_lpn % logical_pages

    def __iter__(self) -> "SequentialWorkload":
        return self

    def __next__(self) -> int:
        result = self.current
        self.current = (self.current + 1) % self.logical_pages
        return result


class HotColdWorkload(Iterator[int]):
    def __init__(
        self,
        logical_pages: int,
        *,
        hot_fraction: float,
        hot_write_ratio: float,
        seed: int,
    ) -> None:
        if not 0.0 < hot_fraction <= 1.0:
            raise ValueError("hot_fraction must be in (0, 1]")
        if not 0.0 <= hot_write_ratio <= 1.0:
            raise ValueError("hot_write_ratio must be in [0, 1]")
        self.logical_pages = logical_pages
        self.hot_pages = max(1, min(logical_pages, round(logical_pages * hot_fraction)))
        self.hot_write_ratio = hot_write_ratio
        self.rng = random.Random(seed)

    def __iter__(self) -> "HotColdWorkload":
        return self

    def __next__(self) -> int:
        cold_pages = self.logical_pages - self.hot_pages
        choose_hot = cold_pages == 0 or self.rng.random() < self.hot_write_ratio
        if choose_hot:
            return self.rng.randrange(self.hot_pages)
        return self.hot_pages + self.rng.randrange(cold_pages)


class ZipfWorkload(Iterator[int]):
    """Exact finite Zipf sampler over logical page ranks 1..N."""

    def __init__(self, logical_pages: int, *, alpha: float, seed: int) -> None:
        if alpha <= 0.0:
            raise ValueError("zipf_alpha must be positive")
        self.rng = random.Random(seed)
        self.cdf = array("d")
        total = 0.0
        for rank in range(1, logical_pages + 1):
            total += rank ** (-alpha)
            self.cdf.append(total)
        self.total = total

    def __iter__(self) -> "ZipfWorkload":
        return self

    def __next__(self) -> int:
        sample = self.rng.random() * self.total
        return bisect.bisect_left(self.cdf, sample)


class TraceWorkload(Iterator[int]):
    """Stream page writes from a generic text/CSV block-I/O trace.

    Accepted automatically:
      * one logical page number per line,
      * W,lba,size
      * timestamp,W,lba,size

    For other layouts, provide explicit 0-based op/lba/size columns.
    Reads are ignored. Multi-page writes are expanded into page updates.
    """

    WRITE_OPS = {"w", "write", "wr", "ws", "write_sync"}
    READ_OPS = {"r", "read", "rd", "rs", "read_sync"}

    def __init__(
        self,
        path: Path,
        *,
        logical_pages: int,
        page_size: int,
        unit: str,
        sector_size: int,
        repeat: bool,
        addressing: str,
        lba_offset: int,
        delimiter: Optional[str],
        op_col: Optional[int],
        lba_col: Optional[int],
        size_col: Optional[int],
    ) -> None:
        if page_size <= 0 or sector_size <= 0:
            raise ValueError("page_size and sector_size must be positive")
        if unit not in {"page", "sector", "byte"}:
            raise ValueError("trace unit must be page, sector, or byte")
        if addressing not in {"strict", "wrap"}:
            raise ValueError("trace addressing must be strict or wrap")
        if not path.is_file():
            raise FileNotFoundError(path)
        explicit = [op_col, lba_col, size_col]
        if any(value is not None for value in explicit) and lba_col is None:
            raise ValueError("--trace-lba-col is required with explicit columns")
        for value in explicit:
            if value is not None and value < 0:
                raise ValueError("trace column indices must be non-negative")

        self.path = path
        self.logical_pages = logical_pages
        self.page_size = page_size
        self.unit = unit
        self.sector_size = sector_size
        self.repeat = repeat
        self.addressing = addressing
        self.lba_offset = lba_offset
        self.delimiter = delimiter
        self.op_col = op_col
        self.lba_col = lba_col
        self.size_col = size_col

        self.pending: Deque[int] = deque()
        self.file: Optional[TextIO] = None
        self.line_number = 0
        self.writes_this_pass = 0
        self._open()

    def __iter__(self) -> "TraceWorkload":
        return self

    def _open(self) -> None:
        if self.file is not None:
            self.file.close()
        self.file = self.path.open("r", encoding="utf-8", errors="replace", newline="")
        self.line_number = 0
        self.writes_this_pass = 0

    def _split(self, line: str) -> list[str]:
        if self.delimiter is not None:
            return next(csv.reader([line], delimiter=self.delimiter))
        if "," in line:
            return next(csv.reader([line]))
        return re.split(r"\s+", line.strip())

    @staticmethod
    def _parse_int(token: str) -> int:
        token = token.strip()
        try:
            return int(token, 0)
        except ValueError:
            value = float(token)
            if not value.is_integer():
                raise ValueError(f"expected an integer, got {token!r}")
            return int(value)

    def _convert_request(self, lba: int, size: int) -> list[int]:
        lba -= self.lba_offset
        if size <= 0:
            return []

        if self.unit == "page":
            first_page = lba
            last_page = lba + size - 1
        else:
            unit_bytes = self.sector_size if self.unit == "sector" else 1
            start_byte = lba * unit_bytes
            length_bytes = size * unit_bytes
            if start_byte < 0:
                raise ValueError("trace address became negative after lba_offset")
            first_page = start_byte // self.page_size
            last_page = (start_byte + length_bytes - 1) // self.page_size

        pages: list[int] = []
        for lpn in range(first_page, last_page + 1):
            if self.addressing == "wrap":
                pages.append(lpn % self.logical_pages)
            elif 0 <= lpn < self.logical_pages:
                pages.append(lpn)
            else:
                raise ValueError(
                    f"trace LPN {lpn} at line {self.line_number} is outside "
                    f"[0, {self.logical_pages - 1}]; use --trace-addressing wrap, "
                    "adjust --trace-lba-offset, or enlarge the logical device"
                )
        return pages

    def _parse_tokens(self, tokens: Sequence[str]) -> list[int]:
        tokens = [token.strip() for token in tokens]
        if not tokens or all(not token for token in tokens):
            return []

        # Explicit column layout supports traces whose operation field follows
        # the LBA/size fields (e.g., many FIU-style traces).
        if self.lba_col is not None:
            max_col = max(
                col
                for col in (self.op_col, self.lba_col, self.size_col)
                if col is not None
            )
            if max_col >= len(tokens):
                raise ValueError("line has fewer columns than configured")
            if self.op_col is not None:
                op = tokens[self.op_col].lower()
                if op in self.READ_OPS:
                    return []
                if op not in self.WRITE_OPS:
                    # A textual first line is treated as a header.
                    if self.line_number == 1:
                        return []
                    raise ValueError(f"unknown operation {tokens[self.op_col]!r}")
            lba = self._parse_int(tokens[self.lba_col])
            size = 1 if self.size_col is None else self._parse_int(tokens[self.size_col])
            return self._convert_request(lba, size)

        # One-column format is always interpreted as an LPN, independent of
        # --trace-unit, to make hand-written traces convenient.
        if len(tokens) == 1:
            try:
                lpn = self._parse_int(tokens[0])
            except ValueError:
                if self.line_number == 1:
                    return []
                raise
            if self.addressing == "wrap":
                return [lpn % self.logical_pages]
            if not 0 <= lpn < self.logical_pages:
                raise ValueError(
                    f"trace LPN {lpn} at line {self.line_number} is outside "
                    f"[0, {self.logical_pages - 1}]"
                )
            return [lpn]

        op_index: Optional[int] = None
        op_value: Optional[str] = None
        for index, token in enumerate(tokens):
            normalized = token.lower()
            if normalized in self.WRITE_OPS or normalized in self.READ_OPS:
                op_index = index
                op_value = normalized
                break

        if op_index is not None:
            if op_value in self.READ_OPS:
                return []
            if op_index + 1 >= len(tokens):
                raise ValueError("operation is not followed by an LBA")
            lba = self._parse_int(tokens[op_index + 1])
            size = (
                self._parse_int(tokens[op_index + 2])
                if op_index + 2 < len(tokens) and tokens[op_index + 2] != ""
                else 1
            )
            return self._convert_request(lba, size)

        # No operation token: interpret the first two numeric fields as lba,size.
        try:
            lba = self._parse_int(tokens[0])
            size = self._parse_int(tokens[1])
        except ValueError:
            if self.line_number == 1:
                return []
            raise ValueError(
                "could not infer trace layout; provide --trace-op-col, "
                "--trace-lba-col, and --trace-size-col"
            )
        return self._convert_request(lba, size)

    def __next__(self) -> int:
        while not self.pending:
            assert self.file is not None
            line = self.file.readline()
            if line == "":
                if not self.repeat:
                    self.file.close()
                    raise StopIteration
                if self.writes_this_pass == 0:
                    raise RuntimeError("trace contains no write pages")
                self._open()
                continue

            self.line_number += 1
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                pages = self._parse_tokens(self._split(stripped))
            except Exception as exc:
                raise ValueError(
                    f"failed to parse {self.path} line {self.line_number}: {stripped}"
                ) from exc
            self.pending.extend(pages)
            self.writes_this_pass += len(pages)

        return self.pending.popleft()

    def close(self) -> None:
        if self.file is not None and not self.file.closed:
            self.file.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def occupancy_to_user_blocks(blocks: int, occupancy: float) -> int:
    if not 0.0 < occupancy < 1.0:
        raise ValueError("occupancy must be in (0, 1)")
    # Avoid Python's ties-to-even round() so CLI behavior is unsurprising.
    return int(math.floor(blocks * occupancy + 0.5))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Simulate page-level greedy SSD garbage collection and print only "
            "write amplification."
        )
    )
    parser.add_argument("--blocks", type=int, default=1000)
    parser.add_argument("--pages-per-block", type=int, default=128)
    parser.add_argument(
        "--occupancy",
        type=float,
        default=0.80,
        help="requested user/physical block ratio; rounded to an integer user-block count",
    )
    parser.add_argument(
        "--user-blocks",
        type=int,
        default=None,
        help="exact user block count; overrides --occupancy",
    )
    parser.add_argument(
        "--workload",
        choices=("uniform", "sequential", "hotcold", "zipf", "trace"),
        default="uniform",
    )
    parser.add_argument(
        "--writes",
        type=int,
        default=1_000_000,
        help="number of measured host page writes",
    )
    parser.add_argument(
        "--warmup-writes",
        type=int,
        default=None,
        help="discarded host page writes; default: 2 x logical page count",
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--gc-tie-break", choices=("random", "lowest"), default="random"
    )

    parser.add_argument("--start-lpn", type=int, default=0)
    parser.add_argument("--hot-fraction", type=float, default=0.10)
    parser.add_argument("--hot-write-ratio", type=float, default=0.90)
    parser.add_argument("--zipf-alpha", type=float, default=1.20)

    parser.add_argument("--trace", type=Path, default=None)
    parser.add_argument(
        "--trace-unit",
        choices=("page", "sector", "byte"),
        default="sector",
        help="unit of LBA and size in multi-column traces; one-column traces are LPNs",
    )
    parser.add_argument("--page-size", type=int, default=4096)
    parser.add_argument("--sector-size", type=int, default=512)
    parser.add_argument("--trace-repeat", action="store_true")
    parser.add_argument(
        "--trace-addressing", choices=("strict", "wrap"), default="strict"
    )
    parser.add_argument("--trace-lba-offset", type=int, default=0)
    parser.add_argument(
        "--trace-delimiter",
        default=None,
        help="single-character delimiter; auto-detect comma/whitespace when omitted",
    )
    parser.add_argument("--trace-op-col", type=int, default=None)
    parser.add_argument("--trace-lba-col", type=int, default=None)
    parser.add_argument("--trace-size-col", type=int, default=None)

    parser.add_argument("--output", choices=("plain", "json"), default="plain")
    parser.add_argument("--check-invariants", action="store_true")
    return parser


def make_workload(args: argparse.Namespace, logical_pages: int) -> Iterator[int]:
    workload_seed = args.seed ^ 0x94D049BB133111EB
    if args.workload == "uniform":
        return UniformWorkload(logical_pages, workload_seed)
    if args.workload == "sequential":
        return SequentialWorkload(logical_pages, args.start_lpn)
    if args.workload == "hotcold":
        return HotColdWorkload(
            logical_pages,
            hot_fraction=args.hot_fraction,
            hot_write_ratio=args.hot_write_ratio,
            seed=workload_seed,
        )
    if args.workload == "zipf":
        return ZipfWorkload(
            logical_pages, alpha=args.zipf_alpha, seed=workload_seed
        )
    if args.workload == "trace":
        if args.trace is None:
            raise ValueError("--trace PATH is required for --workload trace")
        if args.trace_delimiter is not None and len(args.trace_delimiter) != 1:
            raise ValueError("--trace-delimiter must be one character")
        return TraceWorkload(
            args.trace,
            logical_pages=logical_pages,
            page_size=args.page_size,
            unit=args.trace_unit,
            sector_size=args.sector_size,
            repeat=args.trace_repeat,
            addressing=args.trace_addressing,
            lba_offset=args.trace_lba_offset,
            delimiter=args.trace_delimiter,
            op_col=args.trace_op_col,
            lba_col=args.trace_lba_col,
            size_col=args.trace_size_col,
        )
    raise AssertionError("unreachable workload type")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        user_blocks = (
            args.user_blocks
            if args.user_blocks is not None
            else occupancy_to_user_blocks(args.blocks, args.occupancy)
        )
        simulator = GreedyGCSimulator(
            blocks=args.blocks,
            pages_per_block=args.pages_per_block,
            user_blocks=user_blocks,
            seed=args.seed,
            gc_tie_break=args.gc_tie_break,
        )
        workload = make_workload(args, simulator.logical_pages)

        warmup_writes = (
            args.warmup_writes
            if args.warmup_writes is not None
            else 2 * simulator.logical_pages
        )
        simulator.run(workload, warmup_writes)
        simulator.reset_statistics()
        simulator.run(workload, args.writes)

        if args.check_invariants:
            simulator.check_invariants()

        wa = simulator.write_amplification
        if args.output == "json":
            print(json.dumps({"write_amplification": wa}, separators=(",", ":")))
        else:
            print(f"{wa:.6f}")
        return 0
    except (OSError, RuntimeError, ValueError, ZeroDivisionError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
