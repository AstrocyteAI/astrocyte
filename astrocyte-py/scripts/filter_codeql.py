#!/usr/bin/env python3
"""Filter a CodeQL CSV report, excluding findings from specified path prefixes."""

import csv
import sys

exclude_prefixes = ("/datasets/", "/node_modules/")
findings = []

try:
    with open(sys.argv[1]) as f:
        for row in csv.reader(f):
            if not row:
                continue
            # CSV columns: name, desc, severity, message, file, start_line, start_col, end_line, end_col
            file_path = row[4] if len(row) > 4 else ""
            # Match `/datasets/` anywhere in the path so the filter works
            # whether codeql ran from `astrocyte-py/` (paths like
            # `/datasets/locomo/...`) or from the repo root (paths like
            # `/astrocyte-py/datasets/locomo/...`).
            if not any(p in file_path for p in exclude_prefixes):
                findings.append(row)
except FileNotFoundError:
    pass  # no findings file yet — print (none) below

if findings:
    writer = csv.writer(sys.stdout)
    writer.writerows(findings)
else:
    print("(none)")
