#!/usr/bin/env python3
"""Filter a CodeQL CSV report, excluding findings from specified path prefixes."""

import csv
import sys

exclude_prefixes = ("/datasets/",)
findings = []

try:
    with open(sys.argv[1]) as f:
        for row in csv.reader(f):
            if not row:
                continue
            # CSV columns: name, desc, severity, message, file, start_line, start_col, end_line, end_col
            file_path = row[4] if len(row) > 4 else ""
            if not any(file_path.startswith(p) for p in exclude_prefixes):
                findings.append(row)
except FileNotFoundError:
    pass  # no findings file yet — print (none) below

if findings:
    writer = csv.writer(sys.stdout)
    writer.writerows(findings)
else:
    print("(none)")
